from __future__ import annotations

from app.main_shared import *


class GroupAtmosphereServiceMixin:
    def _next_group_atmosphere_account_key(self, *, region: str = '', language: str = '') -> str:
        source = str(region or language or 'account').strip().lower()
        mapping = {
            '印尼': 'indo', 'indonesia': 'indo', 'id': 'indo', 'indonesian': 'indo',
            '墨西哥': 'mx', 'mexico': 'mx', 'mx': 'mx', 'spanish': 'es', '西语': 'es',
            '巴西': 'br', 'brazil': 'br', 'br': 'br', 'portuguese': 'pt', '葡语': 'pt',
        }
        slug = mapping.get(source) or re.sub(r'[^a-z0-9]+', '-', source).strip('-') or 'account'
        prefix = f'atmosphere-{slug}'
        with self.db.connect() as conn:
            rows = [str(row[0] or '') for row in conn.execute(
                "SELECT account_key FROM whatsapp_approval_accounts WHERE account_key LIKE ?",
                (f'{prefix}-%',),
            ).fetchall()]
        max_index = 0
        for key in rows:
            match = re.search(r'-(\d+)$', key)
            if match:
                max_index = max(max_index, int(match.group(1)))
        return f'{prefix}-{max_index + 1:02d}'

    def _next_group_atmosphere_learning_account_key(self, *, region: str = '', language: str = '') -> str:
        source = str(region or language or 'learning').strip().lower()
        mapping = {
            '印尼': 'indo', 'indonesia': 'indo', 'id': 'indo', 'indonesian': 'indo',
            '墨西哥': 'mx', 'mexico': 'mx', 'mx': 'mx', 'spanish': 'es', '西语': 'es',
            '巴西': 'br', 'brazil': 'br', 'br': 'br', 'portuguese': 'pt', '葡语': 'pt',
        }
        slug = mapping.get(source) or re.sub(r'[^a-z0-9]+', '-', source).strip('-') or 'learning'
        prefix = f'learn-{slug}'
        with self.db.connect() as conn:
            rows = [str(row[0] or '') for row in conn.execute(
                "SELECT learning_account_key FROM whatsapp_group_atmosphere_learning_accounts WHERE learning_account_key LIKE ?",
                (f'{prefix}-%',),
            ).fetchall()]
            rows += [str(row[0] or '') for row in conn.execute(
                "SELECT account_key FROM whatsapp_approval_accounts WHERE responsible_type='group_atmosphere_learning' AND account_key LIKE ?",
                (f'{prefix}-%',),
            ).fetchall()]
        if self.db.db_path != ':memory:':
            try:
                rows += [p.name for p in WHATSAPP_APPROVAL_WORKER_AUTH_ACCOUNTS_DIR.glob(f'{prefix}-*') if p.is_dir()]
                rows += [p.stem for p in WHATSAPP_APPROVAL_WORKER_RUNTIME_DIR.glob(f'{prefix}-*.json') if p.is_file()]
            except Exception:
                pass
        max_index = 0
        for key in rows:
            match = re.search(r'-(\d+)$', key)
            if match:
                max_index = max(max_index, int(match.group(1)))
        return f'{prefix}-{max_index + 1:02d}'

    @staticmethod
    def _group_atmosphere_group_identity_candidates(group: Dict[str, Any]) -> List[str]:
        candidates: List[str] = []
        if isinstance(group, dict):
            for key in ('target_group', 'link', 'group_id', 'runtime_probe_group_id'):
                value = str(group.get(key) or '').strip()
                if value and value not in candidates:
                    candidates.append(value)
        return candidates

    def _sync_group_atmosphere_role_bindings_after_account_groups_update(
        self,
        conn: sqlite3.Connection,
        *,
        account_key: str,
        previous_groups: List[Dict[str, Any]],
        next_groups: List[Dict[str, Any]],
        now: str,
    ) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            return {'deleted_count': 0, 'updated_count': 0}
        next_by_identifier: Dict[str, tuple[int, Dict[str, Any]]] = {}
        for index, group in enumerate(next_groups):
            if not isinstance(group, dict):
                continue
            for candidate in self._group_atmosphere_group_identity_candidates(group):
                next_by_identifier.setdefault(candidate, (index, group))
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM whatsapp_group_atmosphere_role_bindings WHERE account_key=? ORDER BY created_at ASC, binding_id ASC",
            (normalized_key,),
        ).fetchall()]
        if not rows:
            return {'deleted_count': 0, 'updated_count': 0}
        target_matches_by_binding_id: Dict[str, tuple[int, Dict[str, Any]]] = {}
        target_claimed_final_keys: set[tuple[str, int]] = set()
        for row in rows:
            binding_id = str(row.get('binding_id') or '').strip()
            role_key = str(row.get('role_key') or '').strip()
            old_index = int(row.get('group_index') or 0)
            candidates = [str(row.get('target_group') or '').strip()]
            if 0 <= old_index < len(previous_groups) and isinstance(previous_groups[old_index], dict):
                candidates.extend(self._group_atmosphere_group_identity_candidates(previous_groups[old_index]))
            for candidate in candidates:
                if candidate and candidate in next_by_identifier:
                    match = next_by_identifier[candidate]
                    target_matches_by_binding_id[binding_id] = match
                    target_claimed_final_keys.add((role_key, int(match[0])))
                    break
        deleted_binding_ids: List[str] = []
        update_items: List[Dict[str, Any]] = []
        seen_final_keys: set[tuple[str, int]] = set()
        for row in rows:
            binding_id = str(row.get('binding_id') or '').strip()
            role_key = str(row.get('role_key') or '').strip()
            old_index = int(row.get('group_index') or 0)
            match: Optional[tuple[int, Dict[str, Any]]] = target_matches_by_binding_id.get(binding_id)
            if not match and 0 <= old_index < len(next_groups) and isinstance(next_groups[old_index], dict):
                fallback_final_key = (role_key, old_index)
                if fallback_final_key not in target_claimed_final_keys:
                    match = (old_index, next_groups[old_index])
            if not match:
                if binding_id:
                    deleted_binding_ids.append(binding_id)
                continue
            next_index, next_group = match
            final_key = (role_key, int(next_index))
            if final_key in seen_final_keys:
                if binding_id:
                    deleted_binding_ids.append(binding_id)
                continue
            seen_final_keys.add(final_key)
            next_target = str(next_group.get('target_group') or next_group.get('group_id') or next_group.get('link') or '').strip()
            if not next_target:
                if binding_id:
                    deleted_binding_ids.append(binding_id)
                continue
            next_group_name = str(next_group.get('group_name') or '').strip() or next_target
            current_permission = bool(row.get('group_send_permission_enabled'))
            next_permission = 0 if next_group.get('enabled') is False else (1 if current_permission else 0)
            needs_update = (
                old_index != int(next_index)
                or str(row.get('target_group') or '').strip() != next_target
                or str(row.get('group_name') or '').strip() != next_group_name
                or int(row.get('group_send_permission_enabled') or 0) != next_permission
            )
            if needs_update and binding_id:
                update_items.append({
                    'binding_id': binding_id,
                    'group_index': int(next_index),
                    'target_group': next_target,
                    'group_name': next_group_name,
                    'group_send_permission_enabled': next_permission,
                })
        for binding_id in deleted_binding_ids:
            conn.execute("DELETE FROM whatsapp_group_atmosphere_role_bindings WHERE binding_id=?", (binding_id,))
            conn.execute(
                """
                UPDATE whatsapp_group_atmosphere_configs
                SET enabled=0, status='disabled_deleted_role_binding', next_due_at=NULL, updated_at=?
                WHERE config_name=?
                """,
                (now, f'binding-{binding_id}'),
            )
        for temp_index, item in enumerate(update_items, start=1):
            conn.execute(
                "UPDATE whatsapp_group_atmosphere_role_bindings SET group_index=?, updated_at=? WHERE binding_id=?",
                (-temp_index, now, item['binding_id']),
            )
        for item in update_items:
            conn.execute(
                """
                UPDATE whatsapp_group_atmosphere_role_bindings
                SET group_index=?, target_group=?, group_name=?, group_send_permission_enabled=?, updated_at=?
                WHERE binding_id=?
                """,
                (
                    item['group_index'],
                    item['target_group'],
                    item['group_name'],
                    item['group_send_permission_enabled'],
                    now,
                    item['binding_id'],
                ),
            )
        if deleted_binding_ids or update_items:
            self._record_audit_event(
                conn,
                event_type='group_atmosphere_account_groups_synced_to_bindings',
                event_source='group_atmosphere',
                payload={
                    'account_key': normalized_key,
                    'deleted_binding_ids': deleted_binding_ids,
                    'deleted_count': len(deleted_binding_ids),
                    'updated_binding_ids': [item['binding_id'] for item in update_items],
                    'updated_count': len(update_items),
                },
            )
        return {
            'deleted_count': len(deleted_binding_ids),
            'updated_count': len(update_items),
            'deleted_binding_ids': deleted_binding_ids,
            'updated_binding_ids': [item['binding_id'] for item in update_items],
        }

    def upsert_group_atmosphere_whatsapp_account(self, payload: GroupAtmosphereWhatsAppAccountRequest) -> Dict[str, Any]:
        account_key = str(payload.account_key or '').strip()
        if not account_key:
            account_key = self._next_group_atmosphere_account_key(region=str(payload.region or '').strip(), language=str(payload.language or '').strip())
        region = str(payload.region or '').strip()
        language = str(payload.language or '').strip() or _mcn_language_for_region(region)
        role_positioning = self._resolve_group_atmosphere_phrase_type_key(str(payload.role_positioning or '').strip())
        baileys_account_id = str(
            payload.baileys_account_id
            or payload.provider_account_id
            or payload.account_id
            or self._group_atmosphere_account_baileys_account_id(account_key)
            or _default_baileys_account_id_for_whatsapp_account(account_key)
        ).strip()
        baileys_provider_mode = _baileys_default_provider_mode_for_responsible_type('group_atmosphere')
        baileys_base_url = _default_baileys_provider_base_url()
        role_style_map = {
            'community_seed': 'friendly_local_admin',
            'newcomer_guide': 'patient_step_by_step',
            'faq_helper': 'concise_answer_first',
            'motivation_admin': 'positive_low_pressure',
        }
        speaking_style = str(payload.speaking_style or '').strip() or role_style_map.get(role_positioning, 'friendly_local_admin')
        randomness_level = str(payload.randomness_level or '').strip() or 'medium'
        daily_max_messages = _coerce_positive_int(payload.daily_max_messages, 3)
        min_interval_seconds = _group_atmosphere_interval_seconds(payload.min_interval_seconds, payload.min_interval_minutes, 120)
        max_interval_seconds = max(
            _group_atmosphere_interval_seconds(payload.max_interval_seconds, payload.max_interval_minutes, max(min_interval_seconds, 240)),
            min_interval_seconds,
        )
        raw_groups = list(payload.groups or [])
        if not raw_groups and str(payload.target_group or '').strip():
            raw_groups = [GroupAtmosphereAccountGroupRequest(
                target_group=str(payload.target_group or '').strip(),
                group_name=str(payload.group_name or '').strip() or None,
                enabled=True,
            )]
        if not raw_groups:
            raise HTTPException(status_code=400, detail='at least one group is required')
        if len(raw_groups) > 5:
            raise HTTPException(status_code=400, detail='each group atmosphere speaking account can manage at most 5 groups')
        account_name = str(payload.account_name or '').strip() or account_key
        bindings = []
        for index, group in enumerate(raw_groups, start=1):
            target_group = str(group.target_group or '').strip()
            if not target_group:
                raise HTTPException(status_code=400, detail=f'group #{index} target_group is required')
            group_daily_max = _coerce_positive_int(group.daily_max_messages, daily_max_messages)
            group_min_interval = _group_atmosphere_interval_seconds(group.min_interval_seconds, group.min_interval_minutes, min_interval_seconds)
            group_max_interval = _group_atmosphere_interval_seconds(group.max_interval_seconds, group.max_interval_minutes, max(max_interval_seconds, group_min_interval))
            if group_max_interval < group_min_interval:
                group_max_interval = group_min_interval
            group_language = str(group.language or language or '').strip()
            group_name = str(group.group_name or '').strip() or target_group
            bindings.append({
                'link': target_group if target_group.startswith('https://chat.whatsapp.com/') else '',
                'group_name': group_name,
                'target_group': target_group,
                'group_id': target_group if target_group.endswith('@g.us') else '',
                'provider_mode': baileys_provider_mode,
                'group_assistant_runtime': baileys_provider_mode,
                'baileys_base_url': baileys_base_url,
                'provider_base_url': baileys_base_url,
                'baileys_account_id': baileys_account_id,
                'provider_account_id': baileys_account_id,
                'account_id': baileys_account_id,
                'enabled': False if payload.enabled is False or group.enabled is False else True,
                'language': group_language,
                'speech_plan_config_name': str(group.speech_plan_config_name or '').strip(),
                'daily_max_messages': group_daily_max,
                'min_interval_seconds': group_min_interval,
                'max_interval_seconds': group_max_interval,
                'min_interval_minutes': group_min_interval,
                'max_interval_minutes': group_max_interval,
                'allowed_windows': group.allowed_windows if isinstance(group.allowed_windows, list) else [],
                'registration_group': '',
                'area': region,
                'notify_profile_name': '',
                'approval_count_threshold': WHATSAPP_APPROVAL_DEFAULT_COUNT_THRESHOLD,
                'approval_timeout_minutes': WHATSAPP_APPROVAL_DEFAULT_TIMEOUT_MINUTES,
                'auto_recover_worker': True,
                'schedule_windows': [],
            })
        metadata = {
            'feature': 'group_atmosphere',
            'provider_mode': baileys_provider_mode,
            'group_assistant_runtime': baileys_provider_mode,
            'baileys_base_url': baileys_base_url,
            'provider_base_url': baileys_base_url,
            'baileys_account_id': baileys_account_id,
            'provider_account_id': baileys_account_id,
            'account_id': baileys_account_id,
            'region': region,
            'language': language,
            'role_positioning': role_positioning,
            'speaking_style': speaking_style,
            'randomness_level': randomness_level,
            'daily_max_messages': daily_max_messages,
            'min_interval_seconds': min_interval_seconds,
            'max_interval_seconds': max_interval_seconds,
            'min_interval_minutes': min_interval_seconds,
            'max_interval_minutes': max_interval_seconds,
            'allowed_windows': payload.allowed_windows if isinstance(payload.allowed_windows, list) else [],
        }
        now = utc_now()
        binding_sync_result: Dict[str, Any] = {'deleted_count': 0, 'updated_count': 0}
        with self.db.connect() as conn:
            previous_row = conn.execute(
                "SELECT group_links FROM whatsapp_approval_accounts WHERE account_key=? AND responsible_type='group_atmosphere'",
                (account_key,),
            ).fetchone()
            try:
                previous_groups = json.loads(str(previous_row['group_links'] or '[]')) if previous_row else []
            except Exception:
                previous_groups = []
            if not isinstance(previous_groups, list):
                previous_groups = []
            conn.execute(
                """
                INSERT INTO whatsapp_approval_accounts (
                    account_key, account_name, responsible_type, group_links, area, notify_profile_name,
                    approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker,
                    schedule_windows, enabled, verification_status, notes, created_at, updated_at
                ) VALUES (?, ?, 'group_atmosphere', ?, ?, '', 'template_controlled', ?, ?, 1, ?, ?, 'pending_login', ?, ?, ?)
                ON CONFLICT(account_key) DO UPDATE SET
                    account_name=excluded.account_name,
                    responsible_type='group_atmosphere',
                    group_links=excluded.group_links,
                    area=excluded.area,
                    approval_count_threshold=excluded.approval_count_threshold,
                    approval_timeout_minutes=excluded.approval_timeout_minutes,
                    schedule_windows=excluded.schedule_windows,
                    enabled=excluded.enabled,
                    verification_status='pending_login',
                    notes=excluded.notes,
                    created_at=COALESCE(NULLIF(whatsapp_approval_accounts.created_at, ''), excluded.created_at),
                    updated_at=excluded.updated_at
                """,
                (
                    account_key,
                    account_name,
                    json.dumps(bindings, ensure_ascii=False),
                    region,
                    daily_max_messages,
                    min_interval_seconds,
                    json.dumps(payload.allowed_windows if isinstance(payload.allowed_windows, list) else [], ensure_ascii=False),
                    1 if payload.enabled else 0,
                    json.dumps(metadata, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            for idx, group in enumerate(bindings):
                if group.get('enabled') is False:
                    conn.execute(
                        """
                        UPDATE whatsapp_group_atmosphere_role_bindings
                        SET group_send_permission_enabled=0, updated_at=?
                        WHERE account_key=? AND (group_index=? OR target_group=?)
                        """,
                        (now, account_key, idx, str(group.get('target_group') or '').strip()),
                    )
            binding_sync_result = self._sync_group_atmosphere_role_bindings_after_account_groups_update(
                conn,
                account_key=account_key,
                previous_groups=[dict(item or {}) for item in previous_groups if isinstance(item, dict)],
                next_groups=bindings,
                now=now,
            )
            conn.commit()
        row = self._get_whatsapp_approval_account_row(account_key)
        runtime_state = {
            'account_key': account_key,
            'mode': 'baileys_provider_runtime',
            'source': 'baileys_config',
            'provider_name': 'baileys',
            'provider_mode': baileys_provider_mode,
            'baileys_account_id': baileys_account_id,
            'provider_account_id': baileys_account_id,
            'account_id': baileys_account_id,
            'configured': bool(baileys_base_url and baileys_account_id),
            'active': bool(baileys_base_url),
            'base_url': baileys_base_url or None,
            'status': 'configured' if baileys_base_url else 'not_started',
            'ready': False,
            'authenticated': False,
            'session_target_match': True if baileys_account_id else None,
            'status_text': 'Baileys provider 已配置，等待账号登录' if baileys_base_url else 'Baileys provider 服务地址未配置',
        }
        session_state = enrich_whatsapp_login_state(
            {
                'account_key': account_key,
                'mode': 'baileys_provider',
                'auth_strategy': 'baileys',
                'client_id': baileys_account_id,
                'expected_client_id': baileys_account_id,
                'session_target_match': True if baileys_account_id else None,
                'ready': False,
                'authenticated': False,
                'bound': False,
                'login_verified': False,
                'login_check_status': 'pending_runtime' if baileys_base_url else 'runtime_unavailable',
                'login_check_message': 'Baileys 账号尚未初始化，点击“二维码”生成登录会话。' if baileys_base_url else 'Baileys POC 服务地址未配置。',
                'qr_available': False,
                'can_show_qr': bool(baileys_base_url),
                'can_probe': False,
                'baileys_account_id': baileys_account_id,
                'provider_account_id': baileys_account_id,
                'provider_base_url': baileys_base_url or None,
            },
            runtime_state=runtime_state,
            account_enabled=bool(payload.enabled),
        )
        post_save_probe: Dict[str, Any] = {'attempted': False, 'refreshed_count': 0}
        try:
            live_runtime_state, live_session_state = self._build_group_atmosphere_account_runtime_display_state(
                account_key,
                account_enabled=bool(payload.enabled),
                skip_health_check=False,
            )
            can_probe = bool(
                live_session_state.get('login_verified')
                or live_session_state.get('can_probe')
                or (live_runtime_state.get('ready') and live_runtime_state.get('authenticated'))
            )
            if can_probe:
                probed_groups = self._refresh_group_atmosphere_group_names_from_runtime(
                    account_key,
                    live_runtime_state,
                    live_session_state,
                    force_probe=True,
                )
                if probed_groups:
                    runtime_state = live_runtime_state
                    session_state = live_session_state
                    post_save_probe = {
                        'attempted': True,
                        'refreshed_count': sum(
                            1
                            for group in probed_groups
                            if isinstance(group, dict)
                            and (
                                group.get('self_participant_found') is not None
                                or group.get('last_probe_self_participant_found') is not None
                                or str(group.get('group_id') or '').strip()
                                or str(group.get('runtime_probe_group_id') or '').strip()
                            )
                        ),
                    }
                    row = self._get_whatsapp_approval_account_row(account_key) or row
        except Exception as exc:
            post_save_probe = {'attempted': True, 'refreshed_count': 0, 'error': str(exc)}
        return {
            'ok': True,
            'account_key': account_key,
            'account': self._serialize_group_atmosphere_account_row(row or {}, runtime_state=runtime_state, session_state=session_state),
            'runtime': runtime_state,
            'session': session_state,
            'binding_sync': binding_sync_result,
            'post_save_probe': post_save_probe,
        }

    @staticmethod
    def _group_atmosphere_group_name_is_placeholder(group_name: str, target_group: str) -> bool:
        normalized_name = str(group_name or '').strip()
        normalized_target = str(target_group or '').strip()
        if not normalized_name:
            return True
        return normalized_name == normalized_target or normalized_name.startswith('https://chat.whatsapp.com/') or normalized_name.endswith('@g.us')

    def _group_atmosphere_cached_group_identity(self, account_key: str, target_group: str) -> Dict[str, str]:
        normalized_key = str(account_key or '').strip()
        normalized_target = str(target_group or '').strip()
        if not normalized_key or not normalized_target:
            return {}
        meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
        cached_groups = meta.get('last_resolved_groups') if isinstance(meta.get('last_resolved_groups'), dict) else {}
        cached = cached_groups.get(normalized_target) if isinstance(cached_groups, dict) else {}
        if isinstance(cached, dict):
            cached_name = str(cached.get('group_name') or '').strip()
            cached_id = str(cached.get('group_id') or '').strip()
            if cached_name or cached_id:
                return {'group_name': cached_name, 'group_id': cached_id}
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT group_name FROM whatsapp_group_atmosphere_role_bindings WHERE account_key = ? AND target_group = ? ORDER BY updated_at DESC LIMIT 1",
                (normalized_key, normalized_target),
            ).fetchone()
        binding_name = str((dict(row).get('group_name') if row else '') or '').strip()
        if self._group_atmosphere_group_name_is_placeholder(binding_name, normalized_target):
            return {}
        return {'group_name': binding_name, 'group_id': ''}

    def _cache_group_atmosphere_group_identity(self, account_key: str, target_group: str, *, group_name: str = '', group_id: str = '') -> None:
        normalized_key = str(account_key or '').strip()
        normalized_target = str(target_group or '').strip()
        normalized_name = str(group_name or '').strip()
        normalized_id = str(group_id or '').strip()
        if not normalized_key or not normalized_target or (not normalized_name and not normalized_id):
            return
        try:
            meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
            if not isinstance(meta, dict):
                meta = {}
            cached_groups = meta.get('last_resolved_groups') if isinstance(meta.get('last_resolved_groups'), dict) else {}
            next_cached = dict(cached_groups)
            current = next_cached.get(normalized_target) if isinstance(next_cached.get(normalized_target), dict) else {}
            next_cached[normalized_target] = {
                'target_group': normalized_target,
                'group_name': normalized_name or str(current.get('group_name') or '').strip(),
                'group_id': normalized_id or str(current.get('group_id') or '').strip(),
                'resolved_at': utc_now(),
            }
            meta['last_resolved_groups'] = next_cached
            self._write_whatsapp_approval_runtime_meta(normalized_key, meta)
        except Exception:
            return

    def _serialize_group_atmosphere_account_row(self, row: Dict[str, Any], *, runtime_state: Optional[Dict[str, Any]] = None, session_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raw_notes = str(row.get('notes') or '').strip()
        try:
            metadata = json.loads(raw_notes) if raw_notes else {}
        except Exception:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        groups: List[Dict[str, Any]] = []
        try:
            raw_groups = json.loads(row.get('group_links') or '[]')
        except Exception:
            raw_groups = []
        if isinstance(raw_groups, list):
            for item in raw_groups:
                if not isinstance(item, dict):
                    continue
                target_group = str(item.get('target_group') or item.get('group_id') or item.get('link') or item.get('group_name') or '').strip()
                if not target_group:
                    continue
                current_group_name = str(item.get('group_name') or '').strip()
                known_identity = self._group_atmosphere_cached_group_identity(str(row.get('account_key') or '').strip(), target_group)
                display_group_name = current_group_name
                if self._group_atmosphere_group_name_is_placeholder(display_group_name, target_group):
                    display_group_name = str(known_identity.get('group_name') or '').strip() or display_group_name
                group_id = str(item.get('group_id') or '').strip() or str(known_identity.get('group_id') or '').strip()
                group_min_interval = _group_atmosphere_mapping_interval_seconds(
                    item,
                    'min_interval_seconds',
                    'min_interval_minutes',
                    _group_atmosphere_mapping_interval_seconds(metadata, 'min_interval_seconds', 'min_interval_minutes', 120),
                )
                group_max_interval = max(
                    group_min_interval,
                    _group_atmosphere_mapping_interval_seconds(
                        item,
                        'max_interval_seconds',
                        'max_interval_minutes',
                        _group_atmosphere_mapping_interval_seconds(metadata, 'max_interval_seconds', 'max_interval_minutes', max(group_min_interval, 240)),
                    ),
                )
                self_participant_found = item.get('self_participant_found')
                if self_participant_found is None:
                    self_participant_found = item.get('last_probe_self_participant_found')
                self_is_admin = item.get('self_is_admin')
                if self_is_admin is None:
                    self_is_admin = item.get('last_probe_self_is_admin')
                can_manage_membership_requests = item.get('can_manage_membership_requests')
                if can_manage_membership_requests is None:
                    can_manage_membership_requests = item.get('last_probe_can_manage_membership_requests')
                groups.append({
                    'target_group': target_group,
                    'group_id': group_id,
                    'group_name': display_group_name or target_group,
                    'baileys_account_id': str(item.get('baileys_account_id') or metadata.get('baileys_account_id') or '').strip(),
                    'enabled': False if item.get('enabled') is False else True,
                    'language': str(item.get('language') or metadata.get('language') or '').strip(),
                    'speech_plan_config_name': str(item.get('speech_plan_config_name') or '').strip(),
                    'daily_max_messages': _coerce_positive_int(item.get('daily_max_messages'), _coerce_positive_int(metadata.get('daily_max_messages'), 3)),
                    'min_interval_seconds': group_min_interval,
                    'max_interval_seconds': group_max_interval,
                    'min_interval_minutes': group_min_interval,
                    'max_interval_minutes': group_max_interval,
                    'allowed_windows': item.get('allowed_windows') if isinstance(item.get('allowed_windows'), list) else [],
                    'self_participant_found': self_participant_found,
                    'last_probe_self_participant_found': item.get('last_probe_self_participant_found'),
                    'self_is_admin': self_is_admin,
                    'last_probe_self_is_admin': item.get('last_probe_self_is_admin'),
                    'can_manage_membership_requests': can_manage_membership_requests,
                    'last_probe_can_manage_membership_requests': item.get('last_probe_can_manage_membership_requests'),
                    'last_probe_at': str(item.get('last_probe_at') or '').strip(),
                    'last_probe_status': str(item.get('last_probe_status') or '').strip(),
                    'participants_load_status': str(item.get('participants_load_status') or '').strip(),
                    'participants_count_raw': item.get('participants_count_raw'),
                    'last_probe_member_count': item.get('last_probe_member_count'),
                })
        account_min_interval = _group_atmosphere_mapping_interval_seconds(metadata, 'min_interval_seconds', 'min_interval_minutes', 120)
        account_max_interval = max(
            account_min_interval,
            _group_atmosphere_mapping_interval_seconds(metadata, 'max_interval_seconds', 'max_interval_minutes', max(account_min_interval, 240)),
        )
        login_phone_identity = self._extract_whatsapp_login_phone(
            session_state or {},
            runtime_state or {},
        )
        login_phone = str(login_phone_identity.get('phone') or '').strip()
        return {
            'account_key': str(row.get('account_key') or '').strip(),
            'account_name': str(row.get('account_name') or '').strip(),
            'responsible_type': 'group_atmosphere',
            'baileys_account_id': str(metadata.get('baileys_account_id') or '').strip(),
            'login_phone': login_phone,
            'login_phone_source': str(login_phone_identity.get('source') or '').strip(),
            'login_phone_raw': str(login_phone_identity.get('raw') or '').strip(),
            'enabled': bool(row.get('enabled')),
            'region': str(metadata.get('region') or row.get('area') or '').strip(),
            'language': str(metadata.get('language') or '').strip(),
            'role_positioning': str(metadata.get('role_positioning') or '').strip(),
            'speaking_style': str(metadata.get('speaking_style') or '').strip(),
            'randomness_level': str(metadata.get('randomness_level') or '').strip(),
            'daily_max_messages': _coerce_positive_int(metadata.get('daily_max_messages'), 3),
            'min_interval_seconds': account_min_interval,
            'max_interval_seconds': account_max_interval,
            'min_interval_minutes': account_min_interval,
            'max_interval_minutes': account_max_interval,
            'allowed_windows': metadata.get('allowed_windows') if isinstance(metadata.get('allowed_windows'), list) else [],
            'groups': groups,
            'target_group': str(groups[0].get('target_group') if groups else '').strip(),
            'group_name': str(groups[0].get('group_name') if groups else '').strip(),
            'group_count': len(groups),
            'runtime': runtime_state or {},
            'session': session_state or {},
            'updated_at': row.get('updated_at'),
        }

    def _group_atmosphere_account_baileys_account_id(self, account_key: str) -> str:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            return ''
        row = self._get_whatsapp_approval_account_row(normalized_key)
        if not row:
            return ''
        try:
            metadata = json.loads(str(row.get('notes') or '').strip() or '{}')
        except Exception:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return str(metadata.get('baileys_account_id') or metadata.get('provider_account_id') or metadata.get('account_id') or '').strip()

    def _probe_group_atmosphere_actual_group_names(
        self,
        *,
        account_key: str,
        row: Dict[str, Any],
        base_url: str,
        session_state: Dict[str, Any],
        runtime_state: Optional[Dict[str, Any]] = None,
        responsible_type: str = 'group_atmosphere',
        force_probe: bool = False,
    ) -> List[Dict[str, Any]]:
        try:
            groups = json.loads(row.get('group_links') or '[]')
        except Exception:
            groups = []
        if not isinstance(groups, list) or not groups:
            return []
        if not bool((session_state or {}).get('login_verified')):
            return groups
        normalized_base_url = str(base_url or '').strip().rstrip('/')
        if not normalized_base_url:
            return groups
        metadata = self._group_atmosphere_runtime_metadata_from_row(row)
        runtime_row = dict(runtime_state or {})
        changed = False
        resolved_groups_by_target: Dict[str, Dict[str, str]] = {}
        enriched_groups: List[Dict[str, Any]] = []

        def coerce_optional_bool(value: Any) -> Optional[bool]:
            if value is True or value is False:
                return value
            if value is None:
                return None
            normalized = str(value).strip().lower()
            if normalized in {'true', '1', 'yes', 'y', 'on'}:
                return True
            if normalized in {'false', '0', 'no', 'n', 'off'}:
                return False
            return None

        def first_present(payload: Dict[str, Any], *keys: str) -> Any:
            for key in keys:
                if key in payload and payload.get(key) is not None:
                    return payload.get(key)
            return None

        def set_group_field(group_payload: Dict[str, Any], key: str, value: Any) -> bool:
            if value is None:
                return False
            if group_payload.get(key) == value:
                return False
            group_payload[key] = value
            return True

        for item in groups:
            if not isinstance(item, dict):
                continue
            group = dict(item)
            target_group = str(group.get('target_group') or group.get('group_id') or group.get('link') or group.get('group_name') or '').strip()
            current_name = str(group.get('group_name') or '').strip()
            needs_probe = bool(force_probe and target_group) or bool(target_group and (not current_name or current_name == target_group or current_name.startswith('https://chat.whatsapp.com/')))
            if needs_probe:
                try:
                    group_provider_mode = str(
                        group.get('provider_mode')
                        or group.get('group_assistant_runtime')
                        or metadata.get('group_assistant_runtime')
                        or metadata.get('provider_mode')
                        or runtime_row.get('provider_mode')
                        or ''
                    ).strip().lower()
                    runtime_provider_name = str(runtime_row.get('provider_name') or runtime_row.get('provider') or '').strip().lower()
                    runtime_mode = str(runtime_row.get('mode') or runtime_row.get('source') or runtime_row.get('runtime_source') or '').strip().lower()
                    use_baileys_probe = bool(
                        runtime_provider_name == 'baileys'
                        or runtime_mode.startswith('baileys')
                        or group_provider_mode.startswith('baileys')
                    )
                    if use_baileys_probe:
                        baileys_account_id = str(
                            group.get('baileys_account_id')
                            or group.get('provider_account_id')
                            or group.get('account_id')
                            or metadata.get('baileys_account_id')
                            or metadata.get('provider_account_id')
                            or metadata.get('account_id')
                            or runtime_row.get('baileys_account_id')
                            or runtime_row.get('provider_account_id')
                            or runtime_row.get('account_id')
                            or self._group_atmosphere_account_baileys_account_id(str(account_key or '').strip())
                            or _default_baileys_account_id_for_whatsapp_account(str(account_key or '').strip())
                        ).strip()
                        baileys_base_url = str(
                            group.get('baileys_base_url')
                            or group.get('provider_base_url')
                            or metadata.get('baileys_base_url')
                            or metadata.get('provider_base_url')
                            or runtime_row.get('base_url')
                            or normalized_base_url
                            or _default_baileys_provider_base_url()
                        ).strip().rstrip('/')
                        account_for_executor = {
                            **dict(row or {}),
                            **metadata,
                            'account_key': str(account_key or '').strip(),
                            'responsible_type': responsible_type,
                            'provider_mode': group_provider_mode or metadata.get('provider_mode') or 'baileys_primary',
                            'group_assistant_runtime': group_provider_mode or metadata.get('group_assistant_runtime') or 'baileys_primary',
                            'baileys_base_url': baileys_base_url,
                            'provider_base_url': baileys_base_url,
                            'baileys_account_id': baileys_account_id,
                            'provider_account_id': baileys_account_id,
                            'account_id': baileys_account_id,
                        }
                        binding_for_executor = {
                            **group,
                            'provider_mode': group_provider_mode or group.get('provider_mode') or 'baileys_primary',
                            'group_assistant_runtime': group_provider_mode or group.get('group_assistant_runtime') or 'baileys_primary',
                            'baileys_base_url': baileys_base_url,
                            'provider_base_url': baileys_base_url,
                            'baileys_account_id': baileys_account_id,
                            'provider_account_id': baileys_account_id,
                            'account_id': baileys_account_id,
                        }
                        executor = self._build_runtime_baileys_registration_group_executor(
                            account=account_for_executor,
                            binding=binding_for_executor,
                            runtime_state={
                                **runtime_row,
                                'provider_name': 'baileys',
                                'mode': runtime_row.get('mode') or 'baileys_provider_runtime',
                                'provider_mode': group_provider_mode or runtime_row.get('provider_mode') or 'baileys_primary',
                                'base_url': baileys_base_url,
                                'baileys_account_id': baileys_account_id,
                                'provider_account_id': baileys_account_id,
                                'account_id': baileys_account_id,
                            },
                        )
                        group_link = str(group.get('link') or (target_group if _looks_like_whatsapp_invite_link(target_group) else '') or '').strip()
                        group_id_for_probe = str(group.get('group_id') or '').strip()
                        if not group_id_for_probe and _sanitize_whatsapp_group_jid(target_group):
                            group_id_for_probe = _sanitize_whatsapp_group_jid(target_group)
                        extra_payload = {
                            'account_key': str(account_key or '').strip(),
                            'accountId': baileys_account_id or None,
                            'baileys_account_id': baileys_account_id or None,
                            'provider_account_id': baileys_account_id or None,
                            'account_id': baileys_account_id or None,
                            'group_id': group_id_for_probe or None,
                            'groupId': group_id_for_probe or None,
                            'group_name': current_name or None,
                            'link': group_link or None,
                            'groupLink': group_link or None,
                            'provider_mode': group_provider_mode or None,
                            'login_verified': True,
                        }
                        payload = executor.group_state(
                            target_group,
                            extra_payload=extra_payload,
                        )
                    else:
                        response = requests.post(
                            f'{normalized_base_url}/probe-group-state',
                            json={'registration_group': target_group},
                            timeout=8.0,
                        )
                        response.raise_for_status()
                        payload = response.json()
                    payload = payload if isinstance(payload, dict) else {}
                    actual_name = str(payload.get('group_name') or payload.get('groupName') or payload.get('groupSubject') or '').strip()
                    actual_id = str(payload.get('group_id') or payload.get('groupId') or payload.get('groupJid') or '').strip()
                    if actual_name and self._group_atmosphere_group_name_is_placeholder(actual_name, target_group):
                        actual_name = ''
                    probe_at = utc_now()
                    self_participant_found = coerce_optional_bool(first_present(
                        payload,
                        'self_participant_found',
                        'selfParticipantFound',
                        'selfInGroup',
                        'self_in_group',
                    ))
                    self_is_admin = coerce_optional_bool(first_present(payload, 'self_is_admin', 'selfIsAdmin'))
                    can_manage_membership_requests = coerce_optional_bool(first_present(
                        payload,
                        'can_manage_membership_requests',
                        'canManageMembershipRequests',
                    ))
                    participants_load_status = str(first_present(payload, 'participants_load_status', 'participantsLoadStatus') or '').strip()
                    participants_count_raw = first_present(payload, 'participants_count_raw', 'participantsCountRaw', 'participants_count', 'participantsCount', 'member_count', 'memberCount')
                    changed = set_group_field(group, 'last_probe_at', probe_at) or changed
                    changed = set_group_field(group, 'last_probe_status', 'completed') or changed
                    changed = set_group_field(group, 'runtime_probe_group_id', actual_id) or changed
                    changed = set_group_field(group, 'runtime_probe_group_name', actual_name) or changed
                    changed = set_group_field(group, 'self_participant_found', self_participant_found) or changed
                    changed = set_group_field(group, 'last_probe_self_participant_found', self_participant_found) or changed
                    changed = set_group_field(group, 'self_is_admin', self_is_admin) or changed
                    changed = set_group_field(group, 'last_probe_self_is_admin', self_is_admin) or changed
                    changed = set_group_field(group, 'can_manage_membership_requests', can_manage_membership_requests) or changed
                    changed = set_group_field(group, 'last_probe_can_manage_membership_requests', can_manage_membership_requests) or changed
                    changed = set_group_field(group, 'participants_load_status', participants_load_status) or changed
                    changed = set_group_field(group, 'participants_count_raw', participants_count_raw) or changed
                    changed = set_group_field(group, 'last_probe_member_count', participants_count_raw) or changed
                    if actual_name or actual_id:
                        resolved_groups_by_target[target_group] = {'group_name': actual_name, 'group_id': actual_id}
                        self._cache_group_atmosphere_group_identity(
                            str(account_key or '').strip(),
                            target_group,
                            group_name=actual_name,
                            group_id=actual_id,
                        )
                    if actual_name and actual_name != current_name:
                        group['group_name'] = actual_name
                        changed = True
                    if actual_id and actual_id.endswith('@g.us') and actual_id != str(group.get('group_id') or '').strip():
                        group['group_id'] = actual_id
                        changed = True
                except Exception:
                    fallback_identity = self._group_atmosphere_cached_group_identity(str(account_key or '').strip(), target_group)
                    fallback_name = str(fallback_identity.get('group_name') or '').strip()
                    fallback_id = str(fallback_identity.get('group_id') or '').strip()
                    if fallback_name:
                        resolved_groups_by_target[target_group] = {'group_name': fallback_name, 'group_id': fallback_id}
                    if fallback_name and fallback_name != current_name:
                        group['group_name'] = fallback_name
                        changed = True
                    if fallback_id and fallback_id.endswith('@g.us') and fallback_id != str(group.get('group_id') or '').strip():
                        group['group_id'] = fallback_id
                        changed = True
            enriched_groups.append(group)
        if changed or resolved_groups_by_target:
            now = utc_now()
            group_links_json = json.dumps(enriched_groups, ensure_ascii=False)
            normalized_responsible_type = str(responsible_type or 'group_atmosphere').strip() or 'group_atmosphere'
            with self.db.connect() as conn:
                if changed:
                    conn.execute(
                        "UPDATE whatsapp_approval_accounts SET group_links = ?, updated_at = ? WHERE account_key = ? AND responsible_type = ?",
                        (group_links_json, now, str(account_key or '').strip(), normalized_responsible_type),
                    )
                    if normalized_responsible_type == 'group_atmosphere_learning':
                        conn.execute(
                            "UPDATE whatsapp_group_atmosphere_learning_accounts SET group_links = ?, updated_at = ? WHERE learning_account_key = ?",
                            (group_links_json, now, str(account_key or '').strip()),
                        )
                if normalized_responsible_type == 'group_atmosphere':
                    for target_group, resolved in resolved_groups_by_target.items():
                        actual_name = str(resolved.get('group_name') or '').strip()
                        if actual_name:
                            conn.execute(
                                "UPDATE whatsapp_group_atmosphere_role_bindings SET group_name = ?, updated_at = ? WHERE account_key = ? AND target_group = ?",
                                (actual_name, now, str(account_key or '').strip(), target_group),
                            )
                conn.commit()
        return enriched_groups

    def _build_group_atmosphere_account_runtime_display_state(
        self,
        account_key: str,
        *,
        account_enabled: bool = True,
        skip_health_check: bool = True,
        allow_live_test_health: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            return {}, {}
        account_row = self._get_whatsapp_approval_account_row(normalized_key)
        if account_row:
            baileys_runtime_state, baileys_session_state, baileys_used = self._build_baileys_whatsapp_approval_runtime_and_session(
                account_row,
                include_qr_ascii=False,
            )
            if baileys_used:
                baileys_session_state = enrich_whatsapp_login_state(
                    baileys_session_state,
                    runtime_state=baileys_runtime_state,
                    account_enabled=bool(account_enabled),
                )
                cached_baileys_session_state = self._cached_whatsapp_approval_session_snapshot(
                    normalized_key,
                    max_age_seconds=259200.0,
                )
                if str(cached_baileys_session_state.get('mode') or '').strip() != 'baileys_provider':
                    cached_baileys_session_state = {}
                cached_baileys_ready = bool(
                    cached_baileys_session_state.get('login_verified')
                    or cached_baileys_session_state.get('can_probe')
                )
                if (
                    not str(baileys_runtime_state.get('health_error') or '').strip()
                    and (
                        self._recent_group_atmosphere_baileys_success(normalized_key, max_age_seconds=259200.0)
                        or (not bool(baileys_session_state.get('login_verified')) and cached_baileys_ready)
                    )
                    and self._baileys_session_can_be_marked_operational(
                        baileys_session_state,
                        baileys_runtime_state,
                    )
                ):
                    baileys_runtime_state, baileys_session_state = self._mark_baileys_session_operational(
                        baileys_runtime_state,
                        baileys_session_state,
                        message='Baileys 最近一次状态已确认登录，账号可用于群聊天助手。',
                    )
                    for field in ('login_phone', 'login_phone_source', 'login_phone_raw'):
                        if not str(baileys_session_state.get(field) or '').strip() and str(cached_baileys_session_state.get(field) or '').strip():
                            baileys_session_state[field] = cached_baileys_session_state[field]
                        if not str(baileys_runtime_state.get(field) or '').strip() and str(cached_baileys_session_state.get(field) or '').strip():
                            baileys_runtime_state[field] = cached_baileys_session_state[field]
                return baileys_runtime_state, baileys_session_state
        runtime_state = self._build_whatsapp_approval_runtime_state(
            normalized_key,
            allow_shared_fallback=False,
            skip_health_check=skip_health_check,
        )
        session_state = self._cached_whatsapp_approval_session_snapshot(normalized_key)
        if not bool(runtime_state.get('active')):
            session_state = {}
        if bool(runtime_state.get('active')) and not bool(session_state.get('login_verified')):
            meta_snapshot = self._read_whatsapp_approval_runtime_meta(normalized_key)
            cached_worker_health = meta_snapshot.get('last_worker_health') if isinstance(meta_snapshot.get('last_worker_health'), dict) else {}
            cached_client = cached_worker_health.get('approval_client') if isinstance(cached_worker_health.get('approval_client'), dict) else cached_worker_health
            if bool(cached_client.get('authenticated')) and bool(cached_client.get('ready')):
                session_state = self._build_whatsapp_approval_session_state(
                    normalized_key,
                    worker_health=cached_worker_health,
                    include_qr_ascii=False,
                )
        if not session_state:
            session_state = self._build_whatsapp_approval_session_state(
                normalized_key,
                worker_health={},
                include_qr_ascii=False,
            )
        session_state = enrich_whatsapp_login_state(
            session_state,
            runtime_state=runtime_state,
            account_enabled=bool(account_enabled),
        )
        if bool(account_enabled) and not bool(runtime_state.get('active')) and self._whatsapp_approval_has_local_auth_session(normalized_key):
            session_state['login_verified'] = False
            session_state['login_state'] = 'recoverable'
            session_state['login_check_status'] = 'runtime_recoverable'
            session_state['login_check_message'] = '登录态可恢复，点击实时学习恢复。'
            session_state['qr_available'] = False
            session_state['can_show_qr'] = False
        elif bool(account_enabled) and not bool(runtime_state.get('active')):
            session_state['login_verified'] = False
            session_state['login_state'] = 'not_logged_in'
            session_state['login_check_status'] = 'not_logged_in'
            session_state['login_check_message'] = '未登录，请点击二维码登录。'
            session_state['qr_available'] = False
            session_state['can_show_qr'] = True
        if allow_live_test_health and bool(runtime_state.get('authenticated')):
            session_state['login_verified'] = True
            session_state['login_check_status'] = 'authenticated'
            session_state['login_check_message'] = '账号已登录，可以正常使用。'
        return runtime_state, session_state

    def get_group_atmosphere_whatsapp_accounts(self) -> Dict[str, Any]:
        """List chat-assistant WhatsApp accounts from server-side snapshots only.

        Page/list access must not call WhatsApp worker health, start sessions, or probe
        groups. Explicit buttons own live actions.
        """
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute(
                """
                SELECT *
                FROM whatsapp_approval_accounts
                WHERE responsible_type = 'group_atmosphere'
                ORDER BY COALESCE(NULLIF(created_at, ''), updated_at) ASC, account_key ASC
                """
            ).fetchall()]
        output: List[Dict[str, Any]] = []
        for row in rows:
            account_key = str(row.get('account_key') or '').strip()
            existing_meta = self._read_whatsapp_approval_runtime_meta(account_key) if account_key else {}
            runtime_state, session_state = self._build_group_atmosphere_account_runtime_display_state(
                account_key,
                account_enabled=bool(row.get('enabled')),
                skip_health_check=(
                    not self._group_atmosphere_allow_test_worker_urls
                    or (not bool(row.get('enabled')) and not str((existing_meta or {}).get('started_at') or '').strip())
                ),
                allow_live_test_health=self._group_atmosphere_allow_test_worker_urls,
            ) if account_key else ({}, {})
            row_for_serialize = dict(row)
            serialized = self._serialize_group_atmosphere_account_row(
                row_for_serialize,
                runtime_state=runtime_state,
                session_state=session_state,
            )
            serialized['list_mode'] = 'snapshot'
            output.append(serialized)
        return {'rows': output, 'count': len(output), 'region_options': self.list_mcn_region_options(include_disabled=False).get('enabled_options', [])}

    def _refresh_group_atmosphere_group_names_from_runtime(
        self,
        account_key: str,
        runtime_state: Optional[Dict[str, Any]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        *,
        force_probe: bool = False,
    ) -> List[Dict[str, Any]]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            return []
        runtime_state = runtime_state if isinstance(runtime_state, dict) else self._build_whatsapp_approval_runtime_state(normalized_key)
        session_state = session_state if isinstance(session_state, dict) else self._build_whatsapp_approval_session_state(
            normalized_key,
            runtime_state=runtime_state,
            worker_health=runtime_state.get('worker_health') if isinstance(runtime_state, dict) else None,
        )
        if not bool((session_state or {}).get('login_verified')):
            return []
        base_url = str((runtime_state or {}).get('base_url') or '').strip()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM whatsapp_approval_accounts WHERE account_key=? AND responsible_type='group_atmosphere'",
                (normalized_key,),
            ).fetchone()
        if not row:
            return []
        row_data = dict(row)
        if not base_url:
            provider_context = self._group_atmosphere_account_provider_context(normalized_key, runtime_state=runtime_state)
            if provider_context.get('is_baileys'):
                base_url = str(provider_context.get('baileys_base_url') or '').strip()
        if not base_url:
            return []
        try:
            groups = json.loads(row_data.get('group_links') or '[]')
        except Exception:
            groups = []
        if not isinstance(groups, list):
            groups = []
        if not groups:
            first_target = str(row_data.get('target_group') or '').strip()
            if first_target:
                groups = [{'target_group': first_target, 'group_name': str(row_data.get('group_name') or '').strip(), 'enabled': True}]
        if not groups:
            return []
        row_data['group_links'] = json.dumps(groups, ensure_ascii=False)
        return self._probe_group_atmosphere_actual_group_names(
            account_key=normalized_key,
            row=row_data,
            base_url=base_url,
            session_state=session_state,
            runtime_state=runtime_state,
            responsible_type='group_atmosphere',
            force_probe=force_probe,
        )

    def get_group_atmosphere_whatsapp_account_session(self, account_key: str) -> Dict[str, Any]:
        result = self.get_whatsapp_approval_account_session(account_key)
        runtime_state = result.get('runtime') if isinstance(result, dict) and isinstance(result.get('runtime'), dict) else {}
        session_state = result.get('session') if isinstance(result, dict) and isinstance(result.get('session'), dict) else {}
        probed_groups = self._refresh_group_atmosphere_group_names_from_runtime(account_key, runtime_state, session_state)
        if probed_groups:
            result = dict(result)
            result['groups'] = probed_groups
        return result

    def start_group_atmosphere_whatsapp_account_session(self, account_key: str, *, reset: bool = False) -> Dict[str, Any]:
        result = self.start_whatsapp_approval_account_session(account_key, reset=reset)
        runtime_state = result.get('runtime') if isinstance(result, dict) and isinstance(result.get('runtime'), dict) else {}
        session_state = result.get('session') if isinstance(result, dict) and isinstance(result.get('session'), dict) else {}
        probed_groups = self._refresh_group_atmosphere_group_names_from_runtime(account_key, runtime_state, session_state)
        if probed_groups:
            result = dict(result)
            result['groups'] = probed_groups
        return result

    def refresh_group_atmosphere_whatsapp_account_group_names(self, account_key: str) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            raise HTTPException(status_code=400, detail='account_key_required')
        row = self._get_whatsapp_approval_account_row(normalized_key)
        if not row or str(row.get('responsible_type') or '').strip() != 'group_atmosphere':
            raise HTTPException(status_code=404, detail='group_atmosphere_account_not_found')
        result = self.get_whatsapp_approval_account_session(normalized_key)
        runtime_state = result.get('runtime') if isinstance(result, dict) and isinstance(result.get('runtime'), dict) else {}
        session_state = result.get('session') if isinstance(result, dict) and isinstance(result.get('session'), dict) else {}
        probed_groups = self._refresh_group_atmosphere_group_names_from_runtime(normalized_key, runtime_state, session_state, force_probe=True)
        refreshed_row = self._get_whatsapp_approval_account_row(normalized_key) or row
        account = self._serialize_group_atmosphere_account_row(
            dict(refreshed_row),
            runtime_state=runtime_state,
            session_state=session_state,
        )
        groups = probed_groups or list(account.get('groups') or [])
        refreshed_count = 0
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_name = str(group.get('group_name') or '').strip()
            target_group = str(group.get('target_group') or group.get('link') or '').strip()
            if group_name and not self._group_atmosphere_group_name_is_placeholder(group_name, target_group):
                refreshed_count += 1
        return {
            'ok': True,
            'account_key': normalized_key,
            'account': account,
            'runtime': runtime_state,
            'session': session_state,
            'groups': groups,
            'refreshed_count': refreshed_count,
            'unresolved_count': max(0, len(groups) - refreshed_count),
        }

    def stop_group_atmosphere_whatsapp_account_runtime(self, account_key: str) -> Dict[str, Any]:
        return self.stop_whatsapp_approval_account_runtime(account_key)

    @staticmethod
    def _group_atmosphere_config_kind(config_name: str, status: str = '') -> str:
        name = str(config_name or '').strip()
        state = str(status or '').strip()
        if name.startswith('auto-') or state == 'candidate_pool':
            return 'candidate_pool'
        if name.startswith('role-') or state in {'role_container', 'role_type_deleted', 'plan_ready', 'library_only'}:
            return 'speech_role'
        if name.startswith('deliver-') or name.startswith('binding-') or state == 'enabled':
            return 'delivery_runtime'
        return 'generic_config'

    @staticmethod
    def _group_atmosphere_phrase_type_from_config(config_name: str, templates: Optional[List[Dict[str, Any]]] = None) -> str:
        for item in list(templates or []):
            if not isinstance(item, dict):
                continue
            value = str(item.get('phrase_type') or item.get('role_positioning') or item.get('source_role') or item.get('category') or '').strip()
            if value:
                return value
        name = str(config_name or '').strip()
        if name.startswith('auto-') or name.startswith('role-'):
            parts = name.split('-', 2)
            if len(parts) >= 3 and parts[2] and parts[2] not in GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS:
                return parts[2]
        for role in ['community_seed', 'newcomer_guide', 'faq_helper', 'motivation_admin']:
            if role in name:
                return ''
        return ''

    @classmethod
    def _normalize_group_atmosphere_config_semantics(cls, *, config_name: str, status: str, enabled: bool) -> tuple[str, bool, str]:
        name = str(config_name or '').strip()
        state = str(status or '').strip() or ('enabled' if enabled else 'disabled')
        next_enabled = bool(enabled)
        kind = cls._group_atmosphere_config_kind(name, state)
        if name.startswith('auto-'):
            # Canonical root guard: auto-* is a candidate/phrase pool only. It cannot become a role or runnable plan.
            state = 'candidate_pool'
            next_enabled = False
            kind = 'candidate_pool'
        elif name.startswith('role-'):
            kind = 'speech_role'
            if state == 'candidate_pool':
                state = 'role_container'
            next_enabled = False
        return state, next_enabled, kind

    def _row_to_group_atmosphere_config(self, row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        template_pool = json.loads(data.get('template_pool') or '[]')
        faq_rules = json.loads(data.get('faq_rules') or '[]')
        allowed_windows = json.loads(data.get('allowed_windows') or '[]')
        data['enabled'] = bool(data.get('enabled'))
        data['mention_reply_enabled'] = bool(data.get('mention_reply_enabled'))
        data['template_pool'] = template_pool
        data['faq_rules'] = faq_rules
        data['allowed_windows'] = allowed_windows
        data['min_interval_seconds'] = _group_atmosphere_mapping_interval_seconds(data, 'min_interval_seconds', 'min_interval_minutes', 60)
        data['max_interval_seconds'] = max(
            data['min_interval_seconds'],
            _group_atmosphere_mapping_interval_seconds(data, 'max_interval_seconds', 'max_interval_minutes', max(data['min_interval_seconds'], 240)),
        )
        data['phrase_type'] = self._group_atmosphere_phrase_type_from_config(str(data.get('config_name') or ''), template_pool)
        data['config_kind'] = self._group_atmosphere_config_kind(str(data.get('config_name') or ''), str(data.get('status') or ''))
        data['template_count'] = len(template_pool)
        data['faq_rule_count'] = len(faq_rules)
        return data

    def _sync_group_atmosphere_candidates_from_config(self, conn: sqlite3.Connection, config_name: str, templates: List[Dict[str, Any]], *, language: str = '') -> None:
        normalized_config = str(config_name or '').strip()
        if not normalized_config:
            return
        now = utc_now()
        conn.execute("DELETE FROM whatsapp_group_atmosphere_candidates WHERE config_name=?", (normalized_config,))
        for index, raw_item in enumerate(list(templates or []), start=1):
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            text = str(item.get('text') or '').strip()
            candidate_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
            if not candidate_id:
                seed = f"{normalized_config}:{index}:{text}"
                candidate_id = hashlib.sha1(seed.encode('utf-8')).hexdigest()[:24]
                item['candidate_id'] = candidate_id
            template_id = str(item.get('template_id') or candidate_id).strip()
            role = str(item.get('role_positioning') or item.get('source_role') or item.get('category') or self._group_atmosphere_role_from_key(normalized_config)).strip()
            normalized_key = str(item.get('normalized_key') or self._normalize_group_atmosphere_phrase_key(text)).strip()
            semantic_key = str(item.get('semantic_key') or self._normalize_group_atmosphere_semantic_phrase_key(text)).strip()
            source_type = str(item.get('source_type') or 'upload_file').strip()
            sort_order = item.get('sort_order')
            try:
                sort_order_value = int(sort_order) if sort_order is not None and str(sort_order).strip() != '' else None
            except Exception:
                sort_order_value = None
            conn.execute(
                """
                INSERT INTO whatsapp_group_atmosphere_candidates (
                    candidate_row_id, config_name, candidate_id, template_id, language,
                    role_positioning, source_type, normalized_key, semantic_key, text,
                    safe_to_send, enabled, sort_order, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_name, candidate_id) DO UPDATE SET
                    template_id=excluded.template_id,
                    language=excluded.language,
                    role_positioning=excluded.role_positioning,
                    source_type=excluded.source_type,
                    normalized_key=excluded.normalized_key,
                    semantic_key=excluded.semantic_key,
                    text=excluded.text,
                    safe_to_send=excluded.safe_to_send,
                    enabled=excluded.enabled,
                    sort_order=excluded.sort_order,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    f"gacand_{hashlib.sha1(f'{normalized_config}:{candidate_id}'.encode('utf-8')).hexdigest()[:24]}",
                    normalized_config,
                    candidate_id,
                    template_id,
                    str(language or '').strip(),
                    role,
                    source_type,
                    normalized_key,
                    semantic_key,
                    text,
                    1 if item.get('safe_to_send') is True else 0,
                    1 if item.get('enabled') is True else 0,
                    sort_order_value,
                    json.dumps(item, ensure_ascii=False),
                    now,
                ),
            )

    def _sync_group_atmosphere_candidate_table_from_configs(self) -> None:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT config_name, language, template_pool FROM whatsapp_group_atmosphere_configs").fetchall()
            for row in rows:
                try:
                    templates = json.loads(row['template_pool'] or '[]')
                except Exception:
                    templates = []
                if not isinstance(templates, list):
                    templates = []
                self._sync_group_atmosphere_candidates_from_config(
                    conn,
                    str(row['config_name'] or '').strip(),
                    [dict(item or {}) for item in templates if isinstance(item, dict)],
                    language=str(row['language'] or '').strip(),
                )
            conn.commit()

    def _group_atmosphere_candidate_payloads_by_config(self) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT config_name, payload_json
                FROM whatsapp_group_atmosphere_candidates
                ORDER BY config_name ASC, COALESCE(sort_order, 999999) ASC, updated_at ASC, candidate_id ASC
                """
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row['payload_json'] or '{}')
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            grouped.setdefault(str(row['config_name'] or '').strip(), []).append(payload)
        return grouped

    def _sanitize_group_atmosphere_worker_base_url(self, raw_url: Optional[str]) -> str:
        base_url = _sanitize_legacy_shared_webjs_worker_base_url(raw_url)
        if not base_url:
            return ''
        parsed = urlparse(base_url)
        if parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password or parsed.params or parsed.query or parsed.fragment:
            raise HTTPException(status_code=400, detail='invalid_worker_base_url')
        host = str(parsed.hostname or '').strip().lower()
        if host in {'127.0.0.1', 'localhost', '::1'}:
            if parsed.port is None:
                raise HTTPException(status_code=400, detail='invalid_worker_base_url')
            return base_url
        if self._group_atmosphere_allow_test_worker_urls and host.endswith('.local'):
            return base_url
        raise HTTPException(status_code=400, detail='invalid_worker_base_url')

    def _validate_group_atmosphere_worker_base_url(self, raw_url: Optional[str]) -> str:
        return self._sanitize_group_atmosphere_worker_base_url(raw_url)

    @staticmethod
    def _normalize_group_atmosphere_baileys_base_url(raw_url: Optional[str]) -> str:
        base_url = str(raw_url or '').strip().rstrip('/')
        if not base_url:
            return ''
        parsed = urlparse(base_url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.params or parsed.query or parsed.fragment:
            return ''
        return base_url

    @staticmethod
    def _group_atmosphere_runtime_metadata_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
        raw_notes = str((row or {}).get('notes') or '').strip()
        if not raw_notes:
            return {}
        try:
            metadata = json.loads(raw_notes)
        except Exception:
            return {}
        return dict(metadata or {}) if isinstance(metadata, dict) else {}

    @staticmethod
    def _group_atmosphere_group_bindings_from_row(row: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            parsed = json.loads(str((row or {}).get('group_links') or '[]'))
        except Exception:
            parsed = []
        if not isinstance(parsed, list):
            return []
        return [dict(item or {}) for item in parsed if isinstance(item, dict)]

    def _group_atmosphere_account_provider_context(self, account_key: str, *, runtime_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            return {}
        row = self._get_whatsapp_approval_account_row(normalized_key)
        if not row:
            return {}
        responsible_type = str(row.get('responsible_type') or '').strip()
        if responsible_type not in {'group_atmosphere', 'group_atmosphere_learning'}:
            return {}
        metadata = self._group_atmosphere_runtime_metadata_from_row(row)
        bindings = self._group_atmosphere_group_bindings_from_row(row)
        preferred_binding = _preferred_group_binding(bindings)
        runtime_config = _merge_whatsapp_approval_runtime_configs(
            _whatsapp_approval_runtime_config_from_dict(preferred_binding),
            _whatsapp_approval_runtime_config_from_dict(metadata),
        )
        account = {**dict(row or {}), **metadata, **runtime_config, 'responsible_type': responsible_type}
        runtime_row = dict(runtime_state or {})
        baileys_account_id = self._resolve_baileys_runtime_value(
            preferred_binding,
            metadata,
            runtime_config,
            runtime_row,
            account,
            keys=['baileys_account_id', 'provider_account_id', 'account_id'],
        ) or _default_baileys_account_id_for_whatsapp_account(normalized_key)
        if baileys_account_id:
            account = _apply_baileys_runtime_assignment_defaults(
                account,
                responsible_type=responsible_type,
                baileys_account_id=baileys_account_id,
            )
            preferred_binding = _apply_baileys_runtime_assignment_defaults(
                preferred_binding,
                responsible_type=responsible_type,
                baileys_account_id=baileys_account_id,
            ) if preferred_binding else {
                'baileys_account_id': baileys_account_id,
                'provider_account_id': baileys_account_id,
                'account_id': baileys_account_id,
            }
        decision = self._resolve_wa_provider_decision(
            account=account,
            binding=preferred_binding,
            runtime_state=runtime_row,
            responsible_type=responsible_type,
        )
        provider_mode = str(decision.get('provider_mode') or account.get('provider_mode') or '').strip().lower()
        provider_name = str(decision.get('provider_name') or ('baileys' if provider_mode.startswith('baileys') else 'legacy_playwright')).strip().lower()
        is_baileys = bool(provider_name == 'baileys' or provider_mode.startswith('baileys'))
        baileys_base_url = ''
        if is_baileys:
            baileys_base_url = self._resolve_baileys_runtime_base_url(
                account=account,
                binding=preferred_binding,
                runtime_state=runtime_row,
            ) or _default_baileys_provider_base_url()
            baileys_base_url = self._normalize_group_atmosphere_baileys_base_url(baileys_base_url)
        return {
            'account': account,
            'binding': preferred_binding,
            'provider_decision': decision,
            'provider_name': provider_name,
            'provider_mode': provider_mode,
            'is_baileys': is_baileys,
            'baileys_account_id': str(baileys_account_id or '').strip(),
            'baileys_base_url': baileys_base_url,
        }

    def _resolve_group_atmosphere_send_runtime(
        self,
        account_key: str,
        *,
        configured_worker_base_url: Optional[str] = '',
        runtime_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        runtime_row = dict(runtime_state or {})
        context = self._group_atmosphere_account_provider_context(normalized_key, runtime_state=runtime_row)
        if context.get('is_baileys'):
            return {
                **context,
                'base_url': str(context.get('baileys_base_url') or '').strip().rstrip('/'),
                'source': 'baileys_provider',
                'legacy_worker_ignored': bool(str(configured_worker_base_url or '').strip()),
            }
        worker_base_url = self._validate_group_atmosphere_worker_base_url(configured_worker_base_url)
        if not worker_base_url:
            if not runtime_row:
                runtime_row = self._build_whatsapp_approval_runtime_state(normalized_key, allow_shared_fallback=False)
            if runtime_row.get('active') and runtime_row.get('base_url'):
                worker_base_url = self._validate_group_atmosphere_worker_base_url(runtime_row.get('base_url'))
        return {
            **context,
            'base_url': worker_base_url,
            'source': 'legacy_worker',
            'runtime_state': runtime_row,
            'legacy_worker_ignored': False,
            'is_baileys': False,
            'baileys_account_id': str(context.get('baileys_account_id') or '').strip(),
        }

    def upsert_group_atmosphere_config(self, payload: GroupAtmosphereConfigRequest) -> Dict[str, Any]:
        now = utc_now()
        status = str(payload.status or ('enabled' if payload.enabled else 'disabled')).strip() or 'enabled'
        status, normalized_enabled, _config_kind = self._normalize_group_atmosphere_config_semantics(
            config_name=payload.config_name,
            status=status,
            enabled=bool(payload.enabled),
        )
        worker_base_url = self._validate_group_atmosphere_worker_base_url(payload.worker_base_url)
        min_interval_seconds = _group_atmosphere_interval_seconds(payload.min_interval_seconds, payload.min_interval_minutes, 60)
        max_interval_seconds = max(
            min_interval_seconds,
            _group_atmosphere_interval_seconds(payload.max_interval_seconds, payload.max_interval_minutes, max(min_interval_seconds, 240)),
        )
        template_pool = [item.model_dump() for item in payload.template_pool]
        for item in template_pool:
            phrase_type = str(item.get('phrase_type') or item.get('role_positioning') or item.get('source_role') or item.get('category') or '').strip()
            if phrase_type:
                item['phrase_type'] = phrase_type
                item.setdefault('role_positioning', phrase_type)
                item.setdefault('source_role', phrase_type)
                item.setdefault('category', phrase_type)
        faq_rules = [item.model_dump() for item in payload.faq_rules]
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO whatsapp_group_atmosphere_configs (
                    config_name, enabled, account_key, target_group, group_name, language, timezone,
                    worker_base_url, daily_max_messages, min_interval_minutes, max_interval_minutes, allowed_windows,
                    template_pool, mention_reply_enabled, faq_rules, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_name) DO UPDATE SET
                    enabled=excluded.enabled, account_key=excluded.account_key, target_group=excluded.target_group,
                    group_name=excluded.group_name, language=excluded.language, timezone=excluded.timezone,
                    worker_base_url=excluded.worker_base_url, daily_max_messages=excluded.daily_max_messages,
                    min_interval_minutes=excluded.min_interval_minutes, max_interval_minutes=excluded.max_interval_minutes,
                    allowed_windows=excluded.allowed_windows,
                    template_pool=excluded.template_pool, mention_reply_enabled=excluded.mention_reply_enabled,
                    faq_rules=excluded.faq_rules, status=excluded.status, updated_at=excluded.updated_at
                """,
                (
                    payload.config_name.strip(), 1 if normalized_enabled else 0, payload.account_key.strip(),
                    payload.target_group.strip(), str(payload.group_name or '').strip() or None,
                    str(payload.language or 'en').strip() or 'en', str(payload.timezone or 'UTC').strip() or 'UTC',
                    worker_base_url, int(payload.daily_max_messages), min_interval_seconds,
                    max_interval_seconds,
                    json.dumps(payload.allowed_windows, ensure_ascii=False), json.dumps(template_pool, ensure_ascii=False),
                    1 if payload.mention_reply_enabled else 0, json.dumps(faq_rules, ensure_ascii=False), status, now,
                ),
            )
            self._sync_group_atmosphere_candidates_from_config(
                conn,
                payload.config_name.strip(),
                template_pool,
                language=str(payload.language or 'en').strip() or 'en',
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM whatsapp_group_atmosphere_configs WHERE config_name=?",
                (payload.config_name.strip(),),
            ).fetchone()
        return self._row_to_group_atmosphere_config(row)

    def list_group_atmosphere_configs(self) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM whatsapp_group_atmosphere_configs ORDER BY updated_at DESC, config_name ASC"
            ).fetchall()
        return [self._row_to_group_atmosphere_config(row) for row in rows]

    def _get_group_atmosphere_config(self, config_name: str) -> Optional[Dict[str, Any]]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM whatsapp_group_atmosphere_configs WHERE config_name=?",
                (str(config_name or '').strip(),),
            ).fetchone()
        return self._row_to_group_atmosphere_config(row) if row else None

    def write_event_ledger(self, *, event_type: str, object_type: str, object_key: str, status: str, evidence_level: str = '', external_id: str = '', payload: Optional[Dict[str, Any]] = None, actor_type: str = 'system', actor_id: str = '', conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
        if not self.event_ledger_enabled:
            return None
        event_type = str(event_type or '').strip()
        object_type = str(object_type or '').strip()
        object_key = str(object_key or '').strip()
        external_id = str(external_id or '').strip()
        if not event_type or not object_type or not object_key:
            return None
        if event_type == 'group_message_sent' and not external_id:
            return None
        now = utc_now()
        record = {
            'event_id': create_id('mcn_evt'),
            'event_type': event_type,
            'object_type': object_type,
            'object_key': object_key,
            'actor_type': str(actor_type or 'system').strip() or 'system',
            'actor_id': str(actor_id or '').strip(),
            'status': str(status or '').strip() or 'success',
            'evidence_level': str(evidence_level or '').strip(),
            'external_id': external_id,
            'payload_json': json.dumps(dict(payload or {}), ensure_ascii=False),
            'created_at': now,
        }
        target_conn = conn or self.db.connect()
        target_conn.execute(
            """
            INSERT INTO mcn_event_ledger (
                event_id, event_type, object_type, object_key, actor_type, actor_id,
                status, evidence_level, external_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record['event_id'], record['event_type'], record['object_type'], record['object_key'],
                record['actor_type'], record['actor_id'], record['status'], record['evidence_level'],
                record['external_id'], record['payload_json'], record['created_at'],
            ),
        )
        if conn is None:
            target_conn.commit()
        return record

    @staticmethod
    def _normalize_group_atmosphere_message_text(value: Any) -> str:
        return re.sub(r'\s+', ' ', Service._format_group_atmosphere_outbound_message_text(value)).strip()

    @staticmethod
    def _auto_break_group_atmosphere_message_line(value: str) -> str:
        line = re.sub(r'[ \t\f\v]+', ' ', str(value or '').strip())
        if len(line) < 100:
            return line
        bullet_markers = r'(?:✅|⚠️|⚠|💡|🤖|🎯|🔥|🚀|📈|💰|👑|🌸|🎁|📝|📌|👉|📷|🔹|🔸|•)'
        line = re.sub(
            rf'([^\n\S]+)({bullet_markers}(?:\s*{bullet_markers})*\s*)',
            lambda match: ('\n' + match.group(2)) if match.start() > 0 else match.group(0),
            line,
        )
        line = re.sub(
            r'([^\n\S]+)((?:\d{1,2}[.)]|[①②③④⑤⑥⑦⑧⑨⑩])\s+)',
            lambda match: ('\n' + match.group(2)) if match.start() > 0 else match.group(0),
            line,
        )
        return line

    @staticmethod
    def _format_group_atmosphere_outbound_message_text(value: Any) -> str:
        text = str(value or '').strip()
        if not text:
            return ''
        text = re.sub(r'\u200e|\u200f|\ufeff', '', text)
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = text.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\\r', '\n').replace('\\t', ' ')
        lines: List[str] = []
        for raw_line in text.split('\n'):
            line = Service._auto_break_group_atmosphere_message_line(raw_line)
            lines.extend(part.strip() for part in line.split('\n'))
        return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()

    @staticmethod
    def _group_atmosphere_client_send_key(*, account_key: str, target_group: str, trigger_type: str, message_text: str, media_id: str = '', scheduled_at: str = '') -> str:
        payload = {
            'account_key': str(account_key or '').strip(),
            'target_group': str(target_group or '').strip(),
            'trigger_type': str(trigger_type or '').strip(),
            'message_text': Service._normalize_group_atmosphere_message_text(message_text),
            'media_id': str(media_id or '').strip(),
            'scheduled_at': str(scheduled_at or '').strip(),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]

    def _find_existing_group_atmosphere_send_by_key(self, client_send_key: str) -> Optional[sqlite3.Row]:
        normalized = str(client_send_key or '').strip()
        if not normalized:
            return None
        with self.db.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM whatsapp_group_atmosphere_logs
                WHERE direction='outbound'
                  AND client_send_key=?
                  AND delivery_state IN (
                      'sending', 'api_accepted', 'runtime_observed',
                      'readback_missing', 'readback_ambiguous', 'frontend_verified'
                  )
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()

    @staticmethod
    def _group_atmosphere_match_readback_record(records: List[Dict[str, Any]], *, target_group: str, returned_message_id: str, message_text: str, send_request_at: str, expected_group_ids: Optional[List[str]] = None, time_window_seconds: int = 15) -> Dict[str, Any]:
        normalized_group = str(target_group or '').strip()
        accepted_groups = {normalized_group}
        accepted_groups.update(str(item or '').strip() for item in list(expected_group_ids or []) if str(item or '').strip())
        normalized_message_id = str(returned_message_id or '').strip()
        normalized_text = Service._normalize_group_atmosphere_message_text(message_text)
        send_dt = parse_iso_datetime(send_request_at) if str(send_request_at or '').strip() else datetime.now(timezone.utc)
        same_group = [
            dict(item or {}) for item in list(records or [])
            if str((item or {}).get('chat_id') or normalized_group).strip() in accepted_groups
        ]
        if normalized_message_id:
            exact = next((item for item in same_group if str(item.get('message_id') or '').strip() == normalized_message_id), None)
            if exact:
                return {'matched': True, 'ambiguous': False, 'reason': 'message_id', 'record': exact, 'attempt_count': 0}
        candidates = []
        for item in same_group:
            if item.get('from_me') is not True:
                continue
            if Service._normalize_group_atmosphere_message_text(item.get('text') or '') != normalized_text:
                continue
            created_at = str(item.get('created_at') or '').strip()
            if not created_at:
                continue
            try:
                delta = abs((parse_iso_datetime(created_at) - send_dt).total_seconds())
            except Exception:
                continue
            if delta <= max(1, int(time_window_seconds or 15)):
                candidates.append(item)
        if len(candidates) == 1:
            return {'matched': True, 'ambiguous': False, 'reason': 'from_me_text_time_window', 'record': candidates[0], 'attempt_count': 0}
        if len(candidates) > 1:
            return {'matched': False, 'ambiguous': True, 'reason': 'multiple_candidates_in_time_window', 'candidates': candidates, 'attempt_count': 0}
        return {'matched': False, 'ambiguous': False, 'reason': 'message_not_found_in_runtime_history', 'attempt_count': 0}

    def _group_atmosphere_preflight_check(self, *, base_url: str, target_group: str, account_key: str, group_index: int, group_name: str = '', baileys_account_id: str = '') -> Dict[str, Any]:
        payload = {
            'target_group': str(target_group or '').strip(),
            'limit': 1,
            'metadata': {
                'account_key': str(account_key or '').strip(),
                'group_index': int(group_index or 0),
                'group_name': str(group_name or '').strip(),
            },
        }
        normalized_baileys_account_id = str(baileys_account_id or '').strip()
        if normalized_baileys_account_id:
            payload['accountId'] = normalized_baileys_account_id
            payload['baileys_account_id'] = normalized_baileys_account_id
            payload['metadata']['baileys_account_id'] = normalized_baileys_account_id
        try:
            resp = requests.post(f'{base_url}/fetch-group-messages', json=payload, timeout=20)
            try:
                body = resp.json()
            except Exception:
                body = {'text': getattr(resp, 'text', '')}
        except Exception as exc:
            return {'ok': False, 'status': 'preflight_failed', 'reason': 'worker_preflight_exception', 'details': {'error': str(exc)}}
        if int(getattr(resp, 'status_code', 500)) >= 400:
            reason = str(body.get('result_code') or body.get('result_reason') or 'worker_preflight_failed').strip() or 'worker_preflight_failed'
            lowered = reason.lower()
            if 'group not found' in lowered or 'not a group' in lowered:
                reason = 'group_not_found'
            elif 'awaiting qr' in lowered or 'not ready' in lowered:
                reason = 'runtime_not_authenticated'
            return {'ok': False, 'status': 'preflight_failed', 'reason': reason, 'details': body}
        return {
            'ok': True,
            'status': 'preflight_ok',
            'reason': '',
            'details': {**dict(body or {}), 'is_account_in_group': True, 'group_resolve_status': 'resolved'},
        }

    def _group_atmosphere_verify_runtime_readback(self, *, base_url: str, target_group: str, returned_message_id: str, message_text: str, send_request_at: str, baileys_account_id: str = '') -> Dict[str, Any]:
        delays = (0.0, 0.2, 0.5, 1.0)
        last_reason = 'readback_not_found_after_retries'
        last_candidates: List[Dict[str, Any]] = []
        for index, delay_seconds in enumerate(delays, start=1):
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            try:
                resp = requests.post(
                    f'{base_url}/fetch-group-messages',
                    json={
                        'target_group': str(target_group or '').strip(),
                        'limit': 20,
                        **({
                            'accountId': str(baileys_account_id or '').strip(),
                            'baileys_account_id': str(baileys_account_id or '').strip(),
                        } if str(baileys_account_id or '').strip() else {}),
                    },
                    timeout=20,
                )
                try:
                    body = resp.json()
                except Exception:
                    body = {'text': getattr(resp, 'text', '')}
            except Exception as exc:
                last_reason = f'readback_request_exception:{exc}'
                continue
            if int(getattr(resp, 'status_code', 500)) >= 400:
                last_reason = str(body.get('result_code') or body.get('result_reason') or 'readback_request_failed').strip() or 'readback_request_failed'
                continue
            matched = self._group_atmosphere_match_readback_record(
                list(body.get('records') or []),
                target_group=target_group,
                returned_message_id=returned_message_id,
                message_text=message_text,
                send_request_at=send_request_at,
                expected_group_ids=[
                    body.get('group_id'),
                    body.get('groupId'),
                    body.get('resolvedGroupId'),
                ],
            )
            matched['attempt_count'] = index
            if matched.get('matched') or matched.get('ambiguous'):
                return matched
            last_reason = str(matched.get('reason') or last_reason)
            last_candidates = list(matched.get('candidates') or [])
        return {'matched': False, 'ambiguous': False, 'reason': last_reason, 'candidates': last_candidates, 'attempt_count': len(delays)}

    def _execute_group_atmosphere_worker_send(self, *, base_url: str, target_group: str, account_key: str, group_index: int, group_name: str, trigger_type: str, message_text: str, media_payload: Optional[Dict[str, Any]] = None, client_send_key: str = '', scheduled_at: str = '', baileys_account_id: str = '') -> Dict[str, Any]:
        normalized_text = self._format_group_atmosphere_outbound_message_text(message_text)
        media_payload = dict(media_payload or {})
        normalized_baileys_account_id = str(baileys_account_id or '').strip()
        normalized_client_send_key = str(client_send_key or '').strip() or self._group_atmosphere_client_send_key(
            account_key=account_key,
            target_group=target_group,
            trigger_type=trigger_type,
            message_text=normalized_text,
            media_id=str(media_payload.get('media_id') or '').strip(),
            scheduled_at=str(scheduled_at or '').strip(),
        )
        existing = self._find_existing_group_atmosphere_send_by_key(normalized_client_send_key)
        if existing:
            existing_item = dict(existing)
            existing_raw = json.loads(existing_item.get('raw_result') or '{}')
            existing_preflight = json.loads(existing_item.get('preflight_details') or '{}')
            existing_state = str(existing_item.get('delivery_state') or 'unknown')
            existing_accepted_states = {'api_accepted', 'runtime_observed', 'readback_missing', 'readback_ambiguous', 'frontend_verified'}
            return {
                'deduped': True,
                'client_send_key': normalized_client_send_key,
                'sent': existing_state in existing_accepted_states,
                'accepted': existing_state in existing_accepted_states,
                'delivery_verified': False,
                'delivery_state': existing_state,
                'evidence_level': existing_item.get('evidence_level') or 'none',
                'status': existing_item.get('status') or 'success',
                'result_code': existing_item.get('result_code') or 'duplicate_send_prevented',
                'result_reason': 'duplicate send prevented; reuse existing record',
                'raw_result': existing_raw,
                'preflight_status': existing_item.get('preflight_status') or '',
                'preflight_reason': existing_item.get('preflight_reason') or '',
                'preflight_details': existing_preflight,
                'readback_matched': bool(existing_item.get('readback_matched')),
                'readback_match_reason': existing_item.get('readback_match_reason') or '',
                'readback_message_id': existing_item.get('readback_message_id') or '',
                'readback_text': existing_item.get('readback_text') or '',
                'readback_timestamp': existing_item.get('readback_timestamp') or '',
                'readback_attempt_count': int(existing_item.get('readback_attempt_count') or 0),
                'legacy_status': existing_item.get('legacy_status') or existing_item.get('status') or '',
                'legacy_result_code': existing_item.get('legacy_result_code') or existing_item.get('result_code') or '',
                'legacy_message_id': existing_item.get('legacy_message_id') or '',
                'migration_note': existing_item.get('migration_note') or '',
            }
        preflight = self._group_atmosphere_preflight_check(
            base_url=base_url,
            target_group=target_group,
            account_key=account_key,
            group_index=group_index,
            group_name=group_name,
            baileys_account_id=normalized_baileys_account_id,
        )
        if not preflight.get('ok'):
            return {
                'deduped': False,
                'client_send_key': normalized_client_send_key,
                'sent': False,
                'accepted': False,
                'delivery_verified': False,
                'delivery_state': 'preflight_failed',
                'evidence_level': 'none',
                'status': 'failed',
                'result_code': str(preflight.get('reason') or 'preflight_failed'),
                'result_reason': str(preflight.get('reason') or 'preflight_failed'),
                'raw_result': dict(preflight.get('details') or {}),
                'preflight_status': str(preflight.get('status') or 'preflight_failed'),
                'preflight_reason': str(preflight.get('reason') or 'preflight_failed'),
                'preflight_details': dict(preflight.get('details') or {}),
                'readback_matched': False,
                'readback_match_reason': '',
                'readback_message_id': '',
                'readback_text': '',
                'readback_timestamp': '',
                'readback_attempt_count': 0,
                'legacy_status': 'failed',
                'legacy_result_code': str(preflight.get('reason') or 'preflight_failed'),
                'legacy_message_id': '',
                'migration_note': '',
            }

        send_payload = {
            'target_group': target_group,
            'message_text': normalized_text,
            'metadata': {
                'account_key': account_key,
                'group_index': int(group_index or 0),
                'group_name': group_name,
                'trigger_type': trigger_type,
                'client_send_key': normalized_client_send_key,
            },
        }
        if normalized_baileys_account_id:
            send_payload['accountId'] = normalized_baileys_account_id
            send_payload['baileys_account_id'] = normalized_baileys_account_id
            send_payload['metadata']['baileys_account_id'] = normalized_baileys_account_id
        for media_key in ['media_id', 'media_path', 'media_mime_type', 'media_filename']:
            media_value = media_payload.get(media_key)
            if media_value:
                send_payload[media_key] = media_value

        raw_result: Dict[str, Any] = {}
        status = 'failed'
        result_code = 'worker_send_exception'
        result_reason = 'worker_send_exception'
        send_request_at = utc_now()

        def _send_once() -> tuple[str, str, str, Dict[str, Any]]:
            resp = requests.post(f'{base_url}/send-group-message', json=send_payload, timeout=30)
            body = resp.json() if hasattr(resp, 'json') else {'text': getattr(resp, 'text', '')}
            if int(getattr(resp, 'status_code', 500)) >= 400:
                return ('failed', str(body.get('result_code') or 'worker_send_failed'), str(body.get('result_reason') or getattr(resp, 'text', '')), body)
            return ('success', str(body.get('result_code') or body.get('status') or 'sent'), str(body.get('result_reason') or 'message accepted by whatsapp web runtime api'), body)

        try:
            status, result_code, result_reason, raw_result = _send_once()
            recoverable_reason = f'{result_code} {result_reason}'.lower()
            if status == 'failed' and (result_code == 'bridge_internal_error' or 'detached frame' in recoverable_reason or 'execution context was destroyed' in recoverable_reason):
                first_error = dict(raw_result or {})
                status, result_code, result_reason, raw_result = _send_once()
                raw_result = dict(raw_result or {})
                raw_result['retry_after_recoverable_error'] = True
                raw_result['first_error'] = first_error
        except Exception as exc:
            raw_result = {'error': str(exc)}
            status = 'failed'
            result_code = 'worker_send_exception'
            result_reason = str(exc)

        worker_message_id = str((raw_result or {}).get('message_id') or ((raw_result or {}).get('raw_result') or {}).get('message_id') or '').strip()
        accepted_by_worker = status in {'success', 'sent'} and result_code != 'dry_run' and bool(worker_message_id) and not bool((raw_result or {}).get('dry_run'))
        readback_matched = False
        readback_match_reason = ''
        readback_message_id = ''
        readback_text = ''
        readback_timestamp = ''
        readback_attempt_count = 0
        delivery_state = 'send_failed'
        evidence_level = 'none'
        if accepted_by_worker:
            readback = self._group_atmosphere_verify_runtime_readback(
                base_url=base_url,
                target_group=target_group,
                returned_message_id=worker_message_id,
                message_text=normalized_text,
                send_request_at=send_request_at,
                baileys_account_id=normalized_baileys_account_id,
            )
            readback_attempt_count = int(readback.get('attempt_count') or 0)
            if readback.get('matched'):
                record = dict(readback.get('record') or {})
                readback_matched = True
                readback_match_reason = str(readback.get('reason') or 'message_id')
                readback_message_id = str(record.get('message_id') or worker_message_id)
                readback_text = str(record.get('text') or '')
                readback_timestamp = str(record.get('created_at') or '')
                delivery_state = 'runtime_observed'
                evidence_level = 'observed_in_runtime_history'
                result_reason = 'runtime history observed; frontend visible unverified'
            elif readback.get('ambiguous'):
                readback_match_reason = str(readback.get('reason') or 'multiple_candidates_in_time_window')
                delivery_state = 'readback_ambiguous'
                evidence_level = 'accepted_by_runtime_api'
                result_reason = 'runtime readback ambiguous; manual verification required'
            else:
                readback_match_reason = str(readback.get('reason') or 'readback_not_found_after_retries')
                delivery_state = 'readback_missing'
                evidence_level = 'accepted_by_runtime_api'
                result_reason = 'api accepted; runtime readback missing'
        elif status == 'failed':
            delivery_state = 'send_failed'
            evidence_level = 'none'
        else:
            delivery_state = 'unknown'
            evidence_level = 'none'

        raw_result = {
            **dict(raw_result or {}),
            'client_send_key': normalized_client_send_key,
            'preflight': preflight,
            'readback': {
                'matched': readback_matched,
                'reason': readback_match_reason,
                'message_id': readback_message_id,
                'text': readback_text,
                'timestamp': readback_timestamp,
                'attempt_count': readback_attempt_count,
            },
        }
        return {
            'deduped': False,
            'client_send_key': normalized_client_send_key,
            'sent': accepted_by_worker,
            'accepted': accepted_by_worker,
            'delivery_verified': False,
            'delivery_state': delivery_state,
            'evidence_level': evidence_level,
            'status': status,
            'result_code': result_code,
            'result_reason': result_reason,
            'raw_result': raw_result,
            'preflight_status': str(preflight.get('status') or 'preflight_ok'),
            'preflight_reason': str(preflight.get('reason') or ''),
            'preflight_details': dict(preflight.get('details') or {}),
            'readback_matched': readback_matched,
            'readback_match_reason': readback_match_reason,
            'readback_message_id': readback_message_id,
            'readback_text': readback_text,
            'readback_timestamp': readback_timestamp,
            'readback_attempt_count': readback_attempt_count,
            'legacy_status': status,
            'legacy_result_code': result_code,
            'legacy_message_id': worker_message_id,
            'migration_note': '',
        }

    def _log_group_atmosphere_event(self, *, config_name: str, account_key: str, target_group: str, direction: str, trigger_type: str, message_text: str, status: str, result_code: str, result_reason: str = '', raw_result: Optional[Dict[str, Any]] = None, delivery_state: str = 'unknown', evidence_level: str = 'none', frontend_verified: bool = False, client_send_key: str = '', legacy_status: str = '', legacy_result_code: str = '', legacy_message_id: str = '', migration_note: str = '', preflight_status: str = '', preflight_reason: str = '', preflight_details: Optional[Dict[str, Any]] = None, readback_matched: bool = False, readback_match_reason: str = '', readback_message_id: str = '', readback_text: str = '', readback_timestamp: str = '', readback_attempt_count: int = 0) -> Dict[str, Any]:
        record = {
            'log_id': create_id('walog'),
            'config_name': config_name,
            'account_key': account_key,
            'target_group': target_group,
            'direction': direction,
            'trigger_type': trigger_type,
            'message_text': message_text,
            'status': status,
            'result_code': result_code,
            'result_reason': result_reason,
            'raw_result': raw_result or {},
            'delivery_state': str(delivery_state or 'unknown').strip() or 'unknown',
            'evidence_level': str(evidence_level or 'none').strip() or 'none',
            'frontend_verified': bool(frontend_verified),
            'client_send_key': str(client_send_key or '').strip(),
            'legacy_status': str(legacy_status or status or '').strip(),
            'legacy_result_code': str(legacy_result_code or result_code or '').strip(),
            'legacy_message_id': str(legacy_message_id or '').strip(),
            'migration_note': str(migration_note or '').strip(),
            'preflight_status': str(preflight_status or '').strip(),
            'preflight_reason': str(preflight_reason or '').strip(),
            'preflight_details': dict(preflight_details or {}),
            'readback_matched': bool(readback_matched),
            'readback_match_reason': str(readback_match_reason or '').strip(),
            'readback_message_id': str(readback_message_id or '').strip(),
            'readback_text': str(readback_text or ''),
            'readback_timestamp': str(readback_timestamp or '').strip(),
            'readback_attempt_count': int(readback_attempt_count or 0),
            'created_at': utc_now(),
        }
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO whatsapp_group_atmosphere_logs (
                    log_id, config_name, account_key, target_group, direction, trigger_type, message_text,
                    status, result_code, result_reason, raw_result, created_at,
                    delivery_state, evidence_level, frontend_verified, client_send_key,
                    legacy_status, legacy_result_code, legacy_message_id, migration_note,
                    preflight_status, preflight_reason, preflight_details,
                    readback_matched, readback_match_reason, readback_message_id, readback_text,
                    readback_timestamp, readback_attempt_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record['log_id'], config_name, account_key, target_group, direction, trigger_type, message_text,
                    status, result_code, result_reason, json.dumps(record['raw_result'], ensure_ascii=False), record['created_at'],
                    record['delivery_state'], record['evidence_level'], 1 if record['frontend_verified'] else 0, record['client_send_key'],
                    record['legacy_status'], record['legacy_result_code'], record['legacy_message_id'], record['migration_note'],
                    record['preflight_status'], record['preflight_reason'], json.dumps(record['preflight_details'], ensure_ascii=False),
                    1 if record['readback_matched'] else 0, record['readback_match_reason'], record['readback_message_id'], record['readback_text'],
                    record['readback_timestamp'], record['readback_attempt_count'],
                ),
            )
            conn.commit()
        return record

    def list_group_atmosphere_logs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM whatsapp_group_atmosphere_logs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(200, int(limit or 50))),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['raw_result'] = json.loads(item.get('raw_result') or '{}')
            item['preflight_details'] = json.loads(item.get('preflight_details') or '{}')
            item['frontend_verified'] = bool(item.get('frontend_verified'))
            item['readback_matched'] = bool(item.get('readback_matched'))
            result.append(item)
        return result

    def group_atmosphere_scheduler_status(self) -> Dict[str, Any]:
        bindings_payload = self.list_group_atmosphere_role_bindings()
        rows = list(bindings_payload.get('rows') or [])
        auto_enabled = sum(1 for row in rows if row.get('auto_speaking_enabled') is True)
        group_send_enabled = sum(1 for row in rows if row.get('group_send_permission_enabled') is not False)
        scheduler_running = bool(
            self.group_atmosphere_scheduler_enabled
            and self._group_atmosphere_scheduler_thread
            and self._group_atmosphere_scheduler_thread.is_alive()
        )
        scheduler_state = dict(getattr(self, '_group_atmosphere_scheduler_state', {}) or {})
        with self.db.connect() as conn:
            last_log = conn.execute(
                """
                SELECT created_at, trigger_type, status, result_code, result_reason
                FROM whatsapp_group_atmosphere_logs
                WHERE trigger_type='scheduled_auto'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        last_run_at = None
        last_skip_reason = ''
        last_skip_reason_text = ''
        if last_log:
            last_run_at = str(last_log['created_at'] or '') or None
            code = str(last_log['result_code'] or '').strip()
            reason = str(last_log['result_reason'] or '').strip()
            if str(last_log['status'] or '').lower() not in {'sent', 'success'}:
                last_skip_reason = code or reason or 'not_sent'
                last_skip_reason_text = reason or code or '未发送'
        if not self.group_atmosphere_scheduler_enabled:
            last_skip_reason = last_skip_reason or 'scheduler_disabled'
            last_skip_reason_text = last_skip_reason_text or '后台调度器未启用'
        elif not rows:
            last_skip_reason = last_skip_reason or 'no_bridge_bindings'
            last_skip_reason_text = last_skip_reason_text or '暂无桥接关系'
        elif auto_enabled <= 0:
            last_skip_reason = last_skip_reason or 'auto_speaking_disabled'
            last_skip_reason_text = last_skip_reason_text or '桥接自动发言均关闭'
        elif group_send_enabled <= 0:
            last_skip_reason = last_skip_reason or 'group_send_disabled'
            last_skip_reason_text = last_skip_reason_text or '群发言均关闭'
        status_label = '调度器未启用' if not self.group_atmosphere_scheduler_enabled else ('调度器运行中' if scheduler_running else '调度器已启用·等待运行')
        return {
            'ok': True,
            'scheduler_enabled': bool(self.group_atmosphere_scheduler_enabled),
            'scheduler_running': scheduler_running,
            'status_label': status_label,
            'poll_interval_seconds': self.group_atmosphere_scheduler_poll_interval_seconds,
            'last_tick_at': scheduler_state.get('last_tick_at') or None,
            'last_success_at': scheduler_state.get('last_success_at') or None,
            'last_error_at': scheduler_state.get('last_error_at') or None,
            'last_error': scheduler_state.get('last_error') or '',
            'last_result': scheduler_state.get('last_result') or {},
            'binding_count': len(rows),
            'auto_enabled_binding_count': auto_enabled,
            'group_send_enabled_count': group_send_enabled,
            'last_run_at': last_run_at,
            'last_skip_reason': last_skip_reason,
            'last_skip_reason_text': last_skip_reason_text,
        }

    def group_atmosphere_summary_snapshot(self) -> Dict[str, Any]:
        """Small admin-dashboard snapshot for group-atmosphere metrics.

        This intentionally avoids account/session list builders so opening `/ops` cannot
        trigger WhatsApp health checks, runtime starts, group probes, or QR refreshes.
        """
        def _row_enabled(value: Any, default: bool = True) -> bool:
            if value is None:
                return default
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            text = str(value).strip().lower()
            if text in {'0', 'false', 'no', 'off', 'disabled'}:
                return False
            if text in {'1', 'true', 'yes', 'on', 'enabled'}:
                return True
            return default

        with self.db.connect() as conn:
            account_rows = [dict(row) for row in conn.execute(
                "SELECT group_links, enabled FROM whatsapp_approval_accounts WHERE responsible_type='group_atmosphere'"
            ).fetchall()]
            enabled_config_count = int(conn.execute(
                "SELECT COUNT(*) FROM whatsapp_group_atmosphere_configs WHERE enabled=1 AND status='enabled'"
            ).fetchone()[0] or 0)
            recent_sent_count = int(conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT 1 FROM whatsapp_group_atmosphere_logs
                    WHERE delivery_state IN ('runtime_observed', 'frontend_verified')
                    ORDER BY created_at DESC
                    LIMIT 50
                )
                """
            ).fetchone()[0] or 0)
        enabled_group_count = 0
        enabled_account_count = 0
        for row in account_rows:
            if not _row_enabled(row.get('enabled'), True):
                continue
            enabled_account_count += 1
            try:
                groups = json.loads(row.get('group_links') or '[]')
            except Exception:
                groups = []
            if not isinstance(groups, list):
                groups = []
            enabled_group_count += sum(1 for group in groups if isinstance(group, dict) and group.get('enabled') is not False)
        return {
            'account_count': len(account_rows),
            'enabled_account_count': enabled_account_count,
            'enabled_group_count': enabled_group_count,
            'enabled_config_count': enabled_config_count,
            'recent_sent_count': recent_sent_count,
            'list_mode': 'snapshot',
        }

    def _group_atmosphere_template_match_keys(self, item: Dict[str, Any]) -> set[str]:
        keys: set[str] = set()
        for key_name in ('candidate_id', 'template_id'):
            value = str((item or {}).get(key_name) or '').strip()
            if value:
                keys.add(f'{key_name}:{value}')
        text = str((item or {}).get('text') or '').strip()
        semantic = self._normalize_group_atmosphere_semantic_phrase_key(text) or text
        role = str((item or {}).get('role_positioning') or (item or {}).get('source_role') or (item or {}).get('category') or '').strip()
        if semantic and role:
            keys.add(f'role_text:{role}:{semantic}')
        return keys

    @staticmethod
    def _group_atmosphere_template_has_media(item: Dict[str, Any]) -> bool:
        return bool((item or {}).get('media_id') or (item or {}).get('media_path') or str((item or {}).get('asset_type') or '').startswith('image'))

    def _group_atmosphere_apply_media_fields(self, item: Dict[str, Any], source: Dict[str, Any]) -> bool:
        changed = False
        media_keys = ('asset_type', 'media_id', 'media_path', 'media_mime_type', 'media_filename', 'media_preview_url')
        source_has_media = self._group_atmosphere_template_has_media(source)
        for key in media_keys:
            next_value = source.get(key) if source_has_media else None
            if source_has_media and key == 'asset_type' and not next_value:
                next_value = 'image_caption'
            if source_has_media:
                if next_value is not None and item.get(key) != next_value:
                    item[key] = next_value
                    changed = True
            else:
                if key in item:
                    item.pop(key, None)
                    changed = True
        if not source_has_media and item.get('asset_type') != 'text':
            item['asset_type'] = 'text'
            changed = True
        return changed

    def _group_atmosphere_apply_candidate_edit_fields(self, item: Dict[str, Any], source: Dict[str, Any]) -> bool:
        changed = False
        copy_keys = (
            'text', 'text_zh', 'text_zh_source', 'text_zh_status', 'text_zh_updated_at',
            'safe_to_send', 'enabled', 'customized', 'customized_at', 'score',
        )
        for key in copy_keys:
            if key in source and item.get(key) != source.get(key):
                item[key] = source.get(key)
                changed = True
        for key in ('candidate_id', 'template_id'):
            value = str(source.get(key) or '').strip()
            if value and not str(item.get(key) or '').strip():
                item[key] = value
                changed = True
        if self._group_atmosphere_apply_media_fields(item, source):
            changed = True
        return changed

    def _latest_group_atmosphere_candidate_media_index(self) -> Dict[str, Dict[str, Any]]:
        latest: Dict[str, Dict[str, Any]] = {}
        for config in self.list_group_atmosphere_configs():
            config_name = str((config or {}).get('config_name') or '').strip()
            if config_name.startswith('binding-') or config_name.startswith('deliver-') or config_name.startswith('role-'):
                continue
            status = str((config or {}).get('status') or '').strip()
            if status == 'enabled':
                continue
            for raw_item in list((config or {}).get('template_pool') or []):
                item = dict(raw_item or {})
                if not item.get('candidate_id') and not item.get('template_id') and not item.get('text'):
                    continue
                for key in self._group_atmosphere_template_match_keys(item):
                    latest[key] = item
        return latest

    def _refresh_group_atmosphere_templates_with_latest_media(self, templates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        latest = self._latest_group_atmosphere_candidate_media_index()
        if not latest:
            return templates
        refreshed: List[Dict[str, Any]] = []
        for raw_item in templates:
            item = dict(raw_item or {})
            source = None
            for key in self._group_atmosphere_template_match_keys(item):
                if key in latest:
                    source = latest[key]
                    break
            if source:
                self._group_atmosphere_apply_media_fields(item, source)
            refreshed.append(item)
        return refreshed

    def _cascade_group_atmosphere_candidate_edit_update(self, source_item: Dict[str, Any], *, source_config_name: str = '', extra_match_keys: Optional[set[str]] = None) -> int:
        match_keys = self._group_atmosphere_template_match_keys(source_item)
        if extra_match_keys:
            match_keys |= {str(key) for key in extra_match_keys if str(key or '').strip()}
        id_values = {
            str((source_item or {}).get(key_name) or '').strip()
            for key_name in ('candidate_id', 'template_id')
            if str((source_item or {}).get(key_name) or '').strip()
        }
        role_match_keys = {key for key in match_keys if str(key).startswith('role_text:')}
        source_role = str(
            (source_item or {}).get('role_positioning')
            or (source_item or {}).get('source_role')
            or (source_item or {}).get('category')
            or ''
        ).strip()
        if not match_keys and not id_values:
            return 0
        now = utc_now()
        changed_configs = 0
        with self.db.connect() as conn:
            rows = conn.execute("SELECT config_name, template_pool FROM whatsapp_group_atmosphere_configs").fetchall()
            for row in rows:
                config_name = str(row['config_name'] or '').strip()
                try:
                    templates = json.loads(row['template_pool'] or '[]')
                except Exception:
                    templates = []
                if not isinstance(templates, list):
                    continue
                changed = False
                next_templates = []
                for raw_item in templates:
                    item = dict(raw_item or {})
                    item_keys = self._group_atmosphere_template_match_keys(item)
                    item_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
                    item_role = str(item.get('role_positioning') or item.get('source_role') or item.get('category') or '').strip()
                    same_role_id_match = bool(item_id and item_id in id_values and (not source_role or not item_role or item_role == source_role))
                    semantic_match = bool(item_keys & role_match_keys)
                    if same_role_id_match or semantic_match:
                        if self._group_atmosphere_apply_candidate_edit_fields(item, source_item):
                            changed = True
                    next_templates.append(item)
                if changed:
                    conn.execute(
                        "UPDATE whatsapp_group_atmosphere_configs SET template_pool=?, updated_at=? WHERE config_name=?",
                        (json.dumps(next_templates, ensure_ascii=False), now, config_name),
                    )
                    changed_configs += 1
            conn.commit()
        return changed_configs

    def _cascade_group_atmosphere_candidate_media_update(self, source_item: Dict[str, Any], *, source_config_name: str = '') -> int:
        match_keys = self._group_atmosphere_template_match_keys(source_item)
        if not match_keys:
            return 0
        now = utc_now()
        changed_configs = 0
        with self.db.connect() as conn:
            rows = conn.execute("SELECT config_name, template_pool FROM whatsapp_group_atmosphere_configs").fetchall()
            for row in rows:
                config_name = str(row['config_name'] or '').strip()
                try:
                    templates = json.loads(row['template_pool'] or '[]')
                except Exception:
                    templates = []
                if not isinstance(templates, list):
                    continue
                changed = False
                next_templates = []
                for raw_item in templates:
                    item = dict(raw_item or {})
                    item_keys = self._group_atmosphere_template_match_keys(item)
                    if item_keys & match_keys:
                        if self._group_atmosphere_apply_media_fields(item, source_item):
                            changed = True
                    next_templates.append(item)
                if changed:
                    conn.execute(
                        "UPDATE whatsapp_group_atmosphere_configs SET template_pool=?, updated_at=? WHERE config_name=?",
                        (json.dumps(next_templates, ensure_ascii=False), now, config_name),
                    )
                    changed_configs += 1
            conn.commit()
        return changed_configs

    @staticmethod
    def _enabled_group_atmosphere_templates(config: Dict[str, Any]) -> List[Dict[str, Any]]:
        templates = [dict(item or {}) for item in list((config or {}).get('template_pool') or [])]
        if not templates:
            return []
        has_review_fields = any(('safe_to_send' in item) or ('enabled' in item) for item in templates)
        if not has_review_fields:
            return templates
        def is_sendable(item: Dict[str, Any]) -> bool:
            if item.get('enabled') is False:
                return False
            source = str(item.get('source_type') or '').strip()
            if source in {'manual', 'manual_upload', 'custom', 'role_save'} or item.get('customized') is True:
                return True
            return item.get('safe_to_send') is True
        return [item for item in templates if is_sendable(item)]

    def dispatch_group_atmosphere_once(self, payload: GroupAtmosphereDispatchRequest) -> Dict[str, Any]:
        config = self._get_group_atmosphere_config(payload.config_name)
        if not config:
            raise HTTPException(status_code=404, detail='group_atmosphere_config_not_found')
        if not config.get('enabled') or config.get('status') != 'enabled':
            return {'sent': False, 'result_code': 'config_disabled', 'result_reason': 'group atmosphere config is disabled'}
        today = _group_atmosphere_business_date()
        sent_count_today = int(config.get('sent_count_today') or 0) if config.get('sent_count_date') == today else 0
        daily_max = 0 if str(config.get('config_name') or '').startswith('binding-') else int(config.get('daily_max_messages') or 0)
        if daily_max and sent_count_today >= daily_max:
            return {'sent': False, 'result_code': 'daily_limit_reached', 'result_reason': 'daily max messages reached'}
        last_sent_at = str(config.get('last_sent_at') or '').strip()
        min_interval = _group_atmosphere_mapping_interval_seconds(config, 'min_interval_seconds', 'min_interval_minutes', 0)
        if min_interval and last_sent_at:
            try:
                elapsed = datetime.now(timezone.utc) - parse_iso_datetime(last_sent_at)
                if elapsed < timedelta(seconds=min_interval):
                    return {'sent': False, 'result_code': 'min_interval_not_reached', 'result_reason': 'minimum send interval has not elapsed'}
            except Exception:
                pass
        message_text = str(payload.message_text or '').strip()
        selected_template: Dict[str, Any] = {}
        if not message_text:
            refreshed_pool = self._refresh_group_atmosphere_templates_with_latest_media([dict(item or {}) for item in list(config.get('template_pool') or [])])
            if refreshed_pool != list(config.get('template_pool') or []):
                config = dict(config)
                config['template_pool'] = refreshed_pool
            templates = self._enabled_group_atmosphere_templates(config)
            if not templates:
                return {'sent': False, 'result_code': 'template_pool_empty', 'result_reason': 'no enabled message template configured'}
            selected_template = dict(random.choice(templates) or {})
            message_text = str(selected_template.get('text') or '').strip()
        message_text = self._format_group_atmosphere_outbound_message_text(message_text)
        if not message_text and not str(selected_template.get('media_path') or '').strip():
            return {'sent': False, 'result_code': 'message_text_empty', 'result_reason': 'selected template has empty text'}
        runtime_resolution = self._resolve_group_atmosphere_send_runtime(
            str(config.get('account_key') or '').strip(),
            configured_worker_base_url=config.get('worker_base_url'),
        )
        worker_base_url = str(runtime_resolution.get('base_url') or '').strip().rstrip('/')
        media_payload = {
            key: selected_template.get(key)
            for key in ['media_id', 'media_path', 'media_mime_type', 'media_filename']
            if selected_template.get(key)
        }
        if not worker_base_url:
            result_code = 'baileys_runtime_not_configured' if runtime_resolution.get('is_baileys') else 'dry_run'
            result_reason = 'Baileys provider base_url not configured; legacy WebJS fallback is disabled' if runtime_resolution.get('is_baileys') else 'worker_base_url not configured; no WhatsApp message was sent'
            return {
                'sent': False,
                'accepted': False,
                'delivery_verified': False,
                'delivery_state': 'unknown',
                'evidence_level': 'none',
                'dry_run': True,
                'message_text': message_text,
                'status': 'skipped',
                'result_code': result_code,
                'result_reason': result_reason,
                'raw_result': {
                    'dry_run': True,
                    'runtime_source': runtime_resolution.get('source'),
                    'provider_name': runtime_resolution.get('provider_name'),
                    'provider_mode': runtime_resolution.get('provider_mode'),
                    'legacy_worker_ignored': bool(runtime_resolution.get('legacy_worker_ignored')),
                },
            }
        delivery = self._execute_group_atmosphere_worker_send(
            base_url=worker_base_url,
            target_group=str(config['target_group']),
            account_key=str(config['account_key']),
            group_index=0,
            group_name=str(config.get('group_name') or config.get('target_group') or ''),
            trigger_type=payload.trigger_type,
            message_text=message_text,
            media_payload=media_payload,
            client_send_key=str(payload.client_send_key or '').strip(),
            scheduled_at=str(payload.scheduled_at or config.get('next_due_at') or ''),
            baileys_account_id=(
                str(runtime_resolution.get('baileys_account_id') or self._group_atmosphere_account_baileys_account_id(str(config.get('account_key') or '').strip()))
                if runtime_resolution.get('is_baileys')
                else ''
            ),
        )
        worker_message_id = str(delivery.get('legacy_message_id') or '').strip()
        accepted_by_worker = bool(delivery.get('accepted'))
        actual_sent = bool(delivery.get('sent'))
        self._log_group_atmosphere_event(
            config_name=config['config_name'], account_key=config['account_key'], target_group=config['target_group'],
            direction='outbound', trigger_type=payload.trigger_type, message_text=message_text,
            status=str(delivery.get('status') or 'unknown'), result_code=str(delivery.get('result_code') or ''), result_reason=str(delivery.get('result_reason') or ''), raw_result=dict(delivery.get('raw_result') or {}),
            delivery_state=str(delivery.get('delivery_state') or 'unknown'), evidence_level=str(delivery.get('evidence_level') or 'none'), frontend_verified=False,
            client_send_key=str(delivery.get('client_send_key') or ''), legacy_status=str(delivery.get('legacy_status') or ''), legacy_result_code=str(delivery.get('legacy_result_code') or ''), legacy_message_id=worker_message_id,
            migration_note=str(delivery.get('migration_note') or ''), preflight_status=str(delivery.get('preflight_status') or ''), preflight_reason=str(delivery.get('preflight_reason') or ''), preflight_details=dict(delivery.get('preflight_details') or {}),
            readback_matched=bool(delivery.get('readback_matched')), readback_match_reason=str(delivery.get('readback_match_reason') or ''), readback_message_id=str(delivery.get('readback_message_id') or ''), readback_text=str(delivery.get('readback_text') or ''), readback_timestamp=str(delivery.get('readback_timestamp') or ''), readback_attempt_count=int(delivery.get('readback_attempt_count') or 0),
        )
        if accepted_by_worker:
            now = utc_now()
            next_due_at = self._next_group_atmosphere_due_at(config)
            conn = self.db.connect()
            self.write_event_ledger(
                event_type='group_message_sent',
                object_type='group_atmosphere_config',
                object_key=str(config['config_name']),
                status='success' if actual_sent else 'accepted',
                evidence_level=str(delivery.get('evidence_level') or 'none'),
                external_id=worker_message_id,
                payload={
                    'config_name': config['config_name'],
                    'account_key': config['account_key'],
                    'target_group': config['target_group'],
                    'group_name': config.get('group_name') or '',
                    'trigger_type': payload.trigger_type,
                    'message_text': message_text,
                    'worker_base_url_configured': bool(worker_base_url),
                    'runtime_source': runtime_resolution.get('source'),
                    'provider_name': runtime_resolution.get('provider_name'),
                    'provider_mode': runtime_resolution.get('provider_mode'),
                    'legacy_worker_ignored': bool(runtime_resolution.get('legacy_worker_ignored')),
                    'accepted_by_worker': accepted_by_worker,
                    'delivery_state': delivery.get('delivery_state'),
                    'readback_matched': bool(delivery.get('readback_matched')),
                    'raw_result': delivery.get('raw_result') or {},
                },
                conn=conn,
            )
            if actual_sent:
                conn.execute(
                    "UPDATE whatsapp_group_atmosphere_configs SET last_sent_at=?, sent_count_today=?, sent_count_date=?, status=?, next_due_at=?, updated_at=? WHERE config_name=?",
                    (now, sent_count_today + 1, today, 'enabled', next_due_at, now, config['config_name']),
                )
            else:
                conn.execute(
                    "UPDATE whatsapp_group_atmosphere_configs SET status=?, next_due_at=?, updated_at=? WHERE config_name=?",
                    ('awaiting_delivery_verification', next_due_at, now, config['config_name']),
                )
            conn.commit()
        return {
            'sent': actual_sent,
            'accepted': accepted_by_worker,
            'delivery_verified': False,
            'delivery_state': str(delivery.get('delivery_state') or 'unknown'),
            'evidence_level': str(delivery.get('evidence_level') or 'none'),
            'dry_run': False,
            'message_text': message_text,
            'status': str(delivery.get('status') or 'unknown'),
            'result_code': str(delivery.get('result_code') or ''),
            'result_reason': str(delivery.get('result_reason') or ''),
            'raw_result': dict(delivery.get('raw_result') or {}),
        }

    def send_group_atmosphere_account_group_message(self, account_key: str, group_index: int, payload: GroupAtmosphereManualSendRequest) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            raise HTTPException(status_code=400, detail='account_key_required')
        message_text = self._format_group_atmosphere_outbound_message_text(payload.message_text)
        media: Optional[Dict[str, Any]] = None
        media_id = str(payload.media_id or '').strip()
        if media_id:
            media = self.get_group_atmosphere_media_asset(media_id)
        if not message_text and not media:
            raise HTTPException(status_code=400, detail='message_text_required')
        row = self._get_whatsapp_approval_account_row(normalized_key)
        if not row or str(row.get('responsible_type') or '').strip() != 'group_atmosphere':
            raise HTTPException(status_code=404, detail='group_atmosphere_account_not_found')
        account = self._serialize_group_atmosphere_account_row(row, runtime_state={}, session_state={})
        if account.get('enabled') is False:
            raise HTTPException(status_code=400, detail='account_disabled')
        groups = list(account.get('groups') or [])
        idx = int(group_index or 0)
        if idx < 0 or idx >= len(groups):
            raise HTTPException(status_code=404, detail='group_not_found')
        group = groups[idx]
        if group.get('enabled') is False:
            raise HTTPException(status_code=400, detail='group_disabled')
        target_group = str(group.get('target_group') or '').strip()
        if not target_group:
            raise HTTPException(status_code=400, detail='target_group_required')
        runtime_state = self._build_whatsapp_approval_runtime_state(normalized_key, allow_shared_fallback=False)
        runtime_resolution = self._resolve_group_atmosphere_send_runtime(
            normalized_key,
            runtime_state=runtime_state,
        )
        base_url = str(runtime_resolution.get('base_url') or '').strip().rstrip('/')
        if runtime_resolution.get('is_baileys'):
            if not base_url:
                raise HTTPException(status_code=400, detail='baileys_runtime_not_configured')
            runtime_state, session_state, _ = self._build_baileys_whatsapp_approval_runtime_and_session(
                row,
                include_qr_ascii=False,
            )
            if not str(runtime_state.get('base_url') or '').strip():
                runtime_state['base_url'] = base_url
            runtime_state['provider_name'] = 'baileys'
            runtime_state['provider_mode'] = runtime_resolution.get('provider_mode')
            cached_baileys_session_state = self._cached_whatsapp_approval_session_snapshot(
                normalized_key,
                max_age_seconds=259200.0,
            )
            if str(cached_baileys_session_state.get('mode') or '').strip() != 'baileys_provider':
                cached_baileys_session_state = {}
            cached_baileys_ready = bool(
                cached_baileys_session_state.get('login_verified')
                or cached_baileys_session_state.get('can_probe')
            )
            if (
                not bool(session_state.get('login_verified'))
                and not str(runtime_state.get('health_error') or '').strip()
                and (
                    self._recent_group_atmosphere_baileys_success(normalized_key, max_age_seconds=259200.0)
                    or cached_baileys_ready
                )
                and self._baileys_session_can_be_marked_operational(session_state, runtime_state)
            ):
                runtime_state, session_state = self._mark_baileys_session_operational(
                    runtime_state,
                    session_state,
                    message='Baileys 最近一次状态已确认登录，允许执行手动发言。',
                )
        else:
            if not runtime_state.get('active') or not base_url:
                raise HTTPException(status_code=400, detail='runtime_not_running')
            worker_health = self._request_whatsapp_approval_worker_health(base_url)
            runtime_state = self._build_whatsapp_approval_runtime_state(normalized_key, worker_health=worker_health, allow_shared_fallback=False)
            session_state = self._build_whatsapp_approval_session_state(normalized_key, worker_health=worker_health, include_qr_ascii=False)
        if not session_state.get('login_verified'):
            raise HTTPException(status_code=400, detail='account_not_logged_in')
        delivery = self._execute_group_atmosphere_worker_send(
            base_url=base_url,
            target_group=target_group,
            account_key=normalized_key,
            group_index=idx,
            group_name=str(group.get('group_name') or target_group),
            trigger_type=payload.trigger_type,
            message_text=message_text,
            media_payload={
                'media_id': media.get('media_id') if media else '',
                'media_path': media.get('media_path') if media else '',
                'media_mime_type': media.get('mime_type') if media else '',
                'media_filename': media.get('filename') if media else '',
            },
            client_send_key=str(payload.client_send_key or '').strip(),
            scheduled_at=str(payload.scheduled_at or ''),
            baileys_account_id=(
                str(runtime_resolution.get('baileys_account_id') or account.get('baileys_account_id') or group.get('baileys_account_id') or '').strip()
                if runtime_resolution.get('is_baileys')
                else ''
            ),
        )
        config_name = f'{normalized_key}-group-{idx + 1}-manual'
        self._log_group_atmosphere_event(
            config_name=config_name,
            account_key=normalized_key,
            target_group=target_group,
            direction='outbound',
            trigger_type=payload.trigger_type,
            message_text=message_text,
            status=str(delivery.get('status') or 'unknown'),
            result_code=str(delivery.get('result_code') or ''),
            result_reason=str(delivery.get('result_reason') or ''),
            raw_result={**dict(delivery.get('raw_result') or {}), 'manual_media_id': media.get('media_id') if media else ''},
            delivery_state=str(delivery.get('delivery_state') or 'unknown'),
            evidence_level=str(delivery.get('evidence_level') or 'none'),
            frontend_verified=False,
            client_send_key=str(delivery.get('client_send_key') or ''),
            legacy_status=str(delivery.get('legacy_status') or ''),
            legacy_result_code=str(delivery.get('legacy_result_code') or ''),
            legacy_message_id=str(delivery.get('legacy_message_id') or ''),
            migration_note=str(delivery.get('migration_note') or ''),
            preflight_status=str(delivery.get('preflight_status') or ''),
            preflight_reason=str(delivery.get('preflight_reason') or ''),
            preflight_details=dict(delivery.get('preflight_details') or {}),
            readback_matched=bool(delivery.get('readback_matched')),
            readback_match_reason=str(delivery.get('readback_match_reason') or ''),
            readback_message_id=str(delivery.get('readback_message_id') or ''),
            readback_text=str(delivery.get('readback_text') or ''),
            readback_timestamp=str(delivery.get('readback_timestamp') or ''),
            readback_attempt_count=int(delivery.get('readback_attempt_count') or 0),
        )
        binding = self._find_group_atmosphere_binding_for_event(normalized_key, target_group)
        self._write_group_atmosphere_binding_send_ledger(
            binding=binding,
            trigger_type=payload.trigger_type,
            message_text=message_text,
            delivery=delivery,
        )
        return {
            'sent': bool(delivery.get('sent')),
            'accepted': bool(delivery.get('accepted')),
            'delivery_verified': False,
            'delivery_state': str(delivery.get('delivery_state') or 'unknown'),
            'evidence_level': str(delivery.get('evidence_level') or 'none'),
            'dry_run': False,
            'account_key': normalized_key,
            'group_index': idx,
            'group_name': group.get('group_name') or target_group,
            'target_group': target_group,
            'message_text': message_text,
            'media_id': media.get('media_id') if media else '',
            'media_filename': media.get('filename') if media else '',
            'status': str(delivery.get('status') or 'unknown'),
            'result_code': str(delivery.get('result_code') or ''),
            'result_reason': str(delivery.get('result_reason') or ''),
            'raw_result': dict(delivery.get('raw_result') or {}),
            'runtime': runtime_state,
            'session': session_state,
        }

    def handle_group_atmosphere_inbound_message(self, payload: GroupAtmosphereInboundMessageRequest) -> Dict[str, Any]:
        trigger_result = self.evaluate_group_atmosphere_trigger_rules_for_inbound(payload)
        if trigger_result.get('should_respond') or trigger_result.get('result_code') in {'trigger_speaking_disabled', 'group_send_permission_disabled', 'system_message_ignored', 'rule_cooldown_active', 'per_user_cooldown_active'}:
            return trigger_result
        rows = self.list_group_atmosphere_configs()
        config = next((row for row in rows if row.get('account_key') == payload.account_key and row.get('target_group') == payload.target_group), None)
        if not config:
            return {'should_respond': False, 'result_code': 'config_not_found'}
        if not payload.mentioned and not payload.quoted_own_message:
            return {'should_respond': False, 'result_code': 'not_mentioned'}
        if not config.get('mention_reply_enabled'):
            return {'should_respond': False, 'result_code': 'mention_reply_disabled'}
        text = str(payload.text or '').lower()
        reply_text = ''
        for rule in list(config.get('faq_rules') or []):
            keyword = str(rule.get('keyword') or '').strip().lower()
            if keyword and keyword in text:
                reply_text = str(rule.get('reply') or '').strip()
                break
        result_code = 'faq_reply_matched'
        safe_to_send = True
        if not reply_text:
            candidate = self.generate_group_atmosphere_ai_candidates(GroupAtmosphereAiCandidateRequest(config_name=config['config_name'], topic=text or 'general', count=1))['candidates'][0]
            reply_text = candidate['text']
            result_code = 'ai_candidate_reply'
            safe_to_send = False
        self._log_group_atmosphere_event(
            config_name=config['config_name'], account_key=config['account_key'], target_group=config['target_group'],
            direction='inbound', trigger_type='mention_reply', message_text=str(payload.text or ''),
            status='matched', result_code=result_code, result_reason=reply_text, raw_result={'sender_id': payload.sender_id, 'safe_to_send': safe_to_send},
        )
        return {'should_respond': True, 'result_code': result_code, 'reply_text': reply_text, 'safe_to_send': safe_to_send}

    def handle_group_atmosphere_trigger_event(self, payload: GroupAtmosphereTriggerEventRequest) -> Dict[str, Any]:
        trigger_type = str(payload.trigger_type or '').strip()
        if trigger_type not in {'member_join', 'group_silence'}:
            return {'should_respond': False, 'result_code': 'unsupported_trigger_type'}
        binding = self._find_group_atmosphere_binding_for_event(payload.account_key, payload.target_group)
        event_payload = dict(payload.event_payload or {})
        if payload.sender_id and not event_payload.get('sender_id'):
            event_payload['sender_id'] = payload.sender_id
        return self.evaluate_group_atmosphere_trigger_rules_for_event(binding or {}, trigger_type=trigger_type, event_payload=event_payload, dry_run=False)

    @staticmethod
    def _group_atmosphere_safe_language_terms(items: List[str], *, allow_short_slang: bool = False) -> List[str]:
        safe_slang = {
            'jgn', 'krm', 'gmn', 'dmn', 'yg', 'ttp', 'sdh', 'blm', 'utk', 'dr', 'dgn', 'aja', 'ga', 'gak', 'nih', 'dong',
            'q', 'vc', 'tbm', 'pq', 'td', 'hj', 'msg', 'pix', 'saq', 'cod', 'amg',
            'xfa', 'xq', 'k', 'dm', 'info', 'wa', 'ok', 'pls', 'thx', 'kak', 'id', 'ya'
        }
        unsafe_words = {'admin', 'sena', 'user', 'media', 'disertakan'}
        output = []
        seen = set()
        for item in items:
            word = re.sub(r"[^A-Za-zÀ-ÿ0-9_']+", '', str(item or '').strip().lower())
            if not word or word in seen:
                continue
            if word in unsafe_words or word.isdigit() or re.search(r'\d{3,}', word):
                continue
            if re.search(r'\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\+?\d{5,})\b', word):
                continue
            if allow_short_slang and word not in safe_slang:
                continue
            if not allow_short_slang and len(word) < 3 and word not in {'id'}:
                continue
            seen.add(word)
            output.append(word)
        return output

    @staticmethod
    def _group_atmosphere_language_profile_from_texts(texts: List[str], language: str = 'en') -> Dict[str, Any]:
        joined = ' '.join(str(text or '') for text in texts)
        words = re.findall(r"[A-Za-zÀ-ÿ0-9_']+", joined.lower())
        stop_words = {'the', 'and', 'for', 'you', 'yang', 'dan', 'atau', 'ini', 'itu', 'dulu', 'kalau', 'sudah', 'para'}
        frequent_terms = Service._group_atmosphere_safe_language_terms([word for word, _ in Counter(word for word in words if len(word) >= 3 and word not in stop_words).most_common(20)])[:12]
        local_abbreviations = Service._group_atmosphere_safe_language_terms([word for word, _ in Counter(words).most_common(30)], allow_short_slang=True)[:10]
        phrase_samples = []
        for text in texts:
            cleaned = re.sub(r'\s+', ' ', str(text or '').strip())
            if cleaned and cleaned not in phrase_samples:
                phrase_samples.append(cleaned[:180])
            if len(phrase_samples) >= 8:
                break
        lower_joined = joined.lower()
        tone_markers = {
            'uses_kak': 'kak' in words,
            'uses_emoji': bool(re.search(r'[\U0001F300-\U0001FAFF]', joined)),
            'question_ratio': round(sum(1 for text in texts if '?' in str(text)) / max(len(texts), 1), 3),
            'avg_message_length': round(sum(len(str(text or '')) for text in texts) / max(len(texts), 1), 1),
            'common_cta': 'admin' if 'admin' in lower_joined else ('dm' if 'dm' in lower_joined else ''),
            'local_abbreviations': local_abbreviations,
        }
        return {
            'language': language or 'en',
            'sample_count': len(texts),
            'frequent_terms': frequent_terms,
            'phrase_samples': phrase_samples,
            'tone_markers': tone_markers,
        }

    @staticmethod
    def _normalize_group_atmosphere_phrase_key(text: str) -> str:
        value = re.sub(r'\s+', ' ', str(text or '').strip().lower())
        value = re.sub(r'^[\W_]+|[\W_]+$', '', value)
        return value

    @classmethod
    def _normalize_group_atmosphere_semantic_phrase_key(cls, text: str) -> str:
        value = cls._normalize_group_atmosphere_phrase_key(text)
        if re.search(r'\bistilah grup yang sering muncul\b', value):
            return re.sub(r'istilah grup yang sering muncul:.*?kalau bingung', 'istilah grup yang sering muncul: <terms>. kalau bingung', value)
        return value

    @staticmethod
    def _clean_group_atmosphere_message_text(text: str) -> str:
        value = str(text or '').strip()
        if not value:
            return ''
        value = re.sub(r'\u200e|\u200f|\ufeff', '', value)
        time_pattern = r'\d{1,2}[.:]\d{2}(?:[.:]\d{2})?'
        date_pattern = r'(?:\d{1,4}[\-/\.]\d{1,2}[\-/\.]\d{1,4}|\d{1,2}[\-/\.]\d{1,2}[\-/\.]\d{2,4})'
        export_prefix = rf'^\[?{date_pattern}[,\s]+{time_pattern}\]?\s*-\s*'
        reverse_export_prefix = rf'^\[?{time_pattern}[,\s]+{date_pattern}\]?\s*-?\s*'
        # Drop pure WhatsApp export metadata lines such as "13/05/26 07.45 - 雪碧-2新中-".
        if re.match(export_prefix, value) and not re.search(r'[:：]\s*\S+', value):
            return ''
        # WhatsApp export prefixes: "2026/05/18 12:31 - Name: ...", "[12/05/26, 09.12.33] Name: ..."
        value = re.sub(export_prefix, '', value).strip()
        value = re.sub(reverse_export_prefix, '', value).strip()
        # Strip a leading sender name only when it looks like export metadata, not normal copy.
        sender_match = re.match(r'^([^:：]{1,40})[:：]\s*(.+)$', value)
        if sender_match:
            possible_sender = sender_match.group(1).strip()
            rest = sender_match.group(2).strip()
            if rest and not re.search(r'[。！？!?]$', possible_sender):
                value = rest
        lower = value.lower().strip()
        invalid_tokens = [
            '<media omitted>', 'media omitted', 'image omitted', 'video omitted', 'audio omitted',
            'sticker omitted', 'gif omitted', 'pesan ini telah dihapus', 'this message was deleted',
            'message deleted', 'missed voice call', 'missed video call', 'deleted message',
            'meminta bergabung', 'menambahkan anda', 'menambahkan anda', 'keluar', 'kode keamanan anda',
            'penunjuk waktu pesan diperbarui', 'pesan baru akan menghilang',
        ]
        if any(token in lower for token in invalid_tokens):
            return ''
        if re.fullmatch(r'[\d\s:.,/\-\[\]()+]+', value):
            return ''
        value = re.sub(r'https?://\S+', '', value).strip()
        value = re.sub(r'@\d{5,}', '', value).strip()
        value = re.sub(r'\+?\d[\d\s().-]{7,}\d', '', value).strip()
        value = re.sub(r'\s+', ' ', value).strip(' -—–:：')
        if len(value) < 4:
            return ''
        return value[:220]

    @classmethod
    def _dedupe_group_atmosphere_records(cls, records: List[GroupAtmosphereChatRecord]) -> List[GroupAtmosphereChatRecord]:
        output: List[GroupAtmosphereChatRecord] = []
        seen = set()
        for record in records:
            cleaned = cls._clean_group_atmosphere_message_text(str(record.text or ''))
            key = cls._normalize_group_atmosphere_phrase_key(cleaned)
            if not key or key in seen:
                continue
            seen.add(key)
            output.append(GroupAtmosphereChatRecord(sender=record.sender, text=cleaned, created_at=record.created_at))
        return output

    @staticmethod
    def _group_atmosphere_candidate_risk_reasons(text: str) -> List[str]:
        lower = str(text or '').lower()
        reasons: List[str] = []
        if any(token in lower for token in ['http://', 'https://', '@g.us']):
            reasons.append('unsafe_link')
        if re.search(r'\b\+?\d[\d\s().-]{7,}\d\b', lower) or re.search(r'\b\d{8,}\b', lower):
            reasons.append('long_number')
        return reasons

    @staticmethod
    def _group_atmosphere_candidate_has_risk(text: str) -> bool:
        return bool(Service._group_atmosphere_candidate_risk_reasons(text))

    @staticmethod
    def _group_atmosphere_candidate_is_manual(item: Dict[str, Any]) -> bool:
        source = str((item or {}).get('source_type') or '').strip()
        label = str((item or {}).get('source_label') or '').strip()
        return source in {'manual', 'manual_upload', 'custom', 'role_save', '人工写入', '自定义'} or label == '人工写入' or (item or {}).get('customized') is True

    @staticmethod
    def _group_atmosphere_truthy_flag(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return value != 0
        normalized = str(value).strip().lower()
        if normalized in {'1', 'true', 'yes', 'y', 'on', 'enabled'}:
            return True
        if normalized in {'0', 'false', 'no', 'n', 'off', 'disabled'}:
            return False
        return default

    @staticmethod
    def _group_atmosphere_candidate_has_media(item: Dict[str, Any]) -> bool:
        return bool((item or {}).get('media_id') or (item or {}).get('media_path') or str((item or {}).get('asset_type') or '').strip() == 'image_caption')

    @staticmethod
    def _group_atmosphere_semantic_role_key(role: str = '') -> str:
        key = str(role or '').strip()
        if not key:
            return ''
        if key in GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS:
            return key
        for legacy_key in sorted(GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS):
            if key.endswith(f'_{legacy_key}') or key.startswith(f'{legacy_key}_') or f'_{legacy_key}_' in key:
                return legacy_key
        return key

    @staticmethod
    def _score_group_atmosphere_phrase(text: str, *, role: str = '') -> int:
        lower = str(text or '').lower()
        role_key = Service._group_atmosphere_semantic_role_key(role)
        score = 0
        useful_tokens = ['kak', 'halo', 'admin', 'kode', 'id', 'data', 'panduan', 'grup', 'mulai', 'kirim', 'krm', 'gmn', 'dmn', 'semangat', 'daftar', 'undangan', 'gabung', 'verifikasi', 'screenshot', 'profil', 'tugas', 'bonus', 'penghasilan', 'ngobrol', 'sapa', 'paham', 'bantu', 'arahkan', 'arahan', 'siap', 'tunggu']
        score += sum(3 for token in useful_tokens if token in lower)
        if 18 <= len(str(text or '')) <= 140:
            score += 10
        if '?' in lower and role_key == 'faq_helper':
            score += 2
        if any(token in lower for token in ['http://', 'https://', '@g.us']):
            score -= 40
        if re.search(r'\b\+?\d[\d\s().-]{7,}\d\b', lower) or re.search(r'\b\d{8,}\b', lower):
            score -= 24
        elif re.search(r'\b\d{5,}\b', lower):
            score -= 10
        role_tokens = {
            'faq_helper': ['kode', 'gmn', 'dmn', 'apa', 'bagaimana', '?'],
            'newcomer_guide': ['id', 'data', 'kirim', 'krm', 'panduan', 'mulai', 'daftar', 'undangan', 'gabung', 'verifikasi', 'screenshot', 'profil'],
            'motivation_admin': ['semangat', 'bonus', 'income', 'penghasilan', 'tugas', 'pelan', 'mulai'],
            'community_seed': ['halo', 'join', 'gabung', 'grup', 'ramai', 'ngobrol', 'sapa'],
        }.get(role_key, [])
        score += sum(4 for token in role_tokens if token in lower)
        return score

    @classmethod
    def _sort_group_atmosphere_candidates(cls, templates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        def sort_key(item: Dict[str, Any]):
            role = str(item.get('role_positioning') or item.get('source_role') or item.get('category') or '').strip()
            text = str(item.get('text') or '')
            score = int(item.get('score') or cls._score_group_atmosphere_phrase(text, role=role))
            is_manual = cls._group_atmosphere_candidate_is_manual(item)
            has_media = cls._group_atmosphere_candidate_has_media(item)
            is_risky = cls._group_atmosphere_candidate_has_risk(text)
            usable = (cls._group_atmosphere_truthy_flag(item.get('enabled')) and cls._group_atmosphere_truthy_flag(item.get('safe_to_send')) and not is_risky)
            customized_priority = 0 if item.get('customized') is True else 1
            customized_at = str(item.get('customized_at') or '').strip()
            try:
                customized_ts = parse_iso_datetime(customized_at).timestamp() if customized_at else 0.0
            except Exception:
                customized_ts = 0.0
            manual_order = item.get('sort_order')
            try:
                has_manual_order = manual_order is not None and str(manual_order).strip() != ''
                manual_order_value = int(manual_order) if has_manual_order else 0
            except Exception:
                has_manual_order = False
                manual_order_value = 0
            if has_manual_order:
                return (0, manual_order_value, str(item.get('candidate_id') or item.get('template_id') or ''))
            return (
                1,
                0 if usable else (2 if is_risky else 1),
                0 if is_manual else 1,
                0 if has_media else 1,
                customized_priority,
                -customized_ts,
                -score,
                -int(item.get('frequency') or 1),
                len(text),
                str(item.get('candidate_id') or item.get('template_id') or ''),
            )
        return sorted([dict(item or {}) for item in templates], key=sort_key)

    @staticmethod
    def _parse_group_atmosphere_chat_export(text: str) -> List[GroupAtmosphereChatRecord]:
        records: List[GroupAtmosphereChatRecord] = []
        for raw_line in str(text or '').splitlines():
            line = str(raw_line or '').strip()
            if not line:
                continue
            sender = 'imported'
            message_text = line
            match = re.match(r'^\[[^\]]+\]\s*([^:：]+)[:：]\s*(.+)$', line)
            if not match:
                match = re.match(r'^(?:\d{1,4}[\-/\.]\d{1,2}[\-/\.]\d{1,4}|\d{1,2}[\-/\.]\d{1,2}[\-/\.]\d{2,4})[,\s]+\d{1,2}[.:]\d{2}(?:[.:]\d{2})?\s*-\s*([^:：]+)[:：]\s*(.+)$', line)
            if match:
                sender = match.group(1).strip() or 'imported'
                message_text = match.group(2).strip()
            cleaned = Service._clean_group_atmosphere_message_text(message_text)
            if cleaned:
                records.append(GroupAtmosphereChatRecord(sender=sender, text=cleaned))
        return Service._dedupe_group_atmosphere_records(records)

    @staticmethod
    def _detect_group_atmosphere_language_and_region(records: List[GroupAtmosphereChatRecord]) -> tuple[str, str]:
        joined = ' '.join(str(record.text or '') for record in records).lower()
        id_hits = sum(1 for token in ['kak', 'halo', 'kode', 'gimana', 'dimana', 'kirim', 'undangan', 'selamat'] if token in joined)
        pt_hits = sum(1 for token in ['você', 'codigo', 'código', 'como', 'ganhar', 'saque', 'obrigada'] if token in joined)
        es_hits = sum(1 for token in ['código', 'como', 'ganar', 'retiro', 'amiga', 'hola', 'gracias'] if token in joined)
        if id_hits >= max(pt_hits, es_hits, 1):
            return 'id', '印尼'
        if pt_hits > es_hits:
            return 'pt', '巴西'
        if es_hits > 0:
            return 'es', '墨西哥'
        return 'en', '未知'

    @staticmethod
    def _group_atmosphere_semantic_intent(text: str) -> str:
        lower = re.sub(r'\s+', ' ', str(text or '').lower()).strip()
        if not lower:
            return ''
        if '?' in lower or re.search(r'\b(?:dimana|dmn|gimana|gmn|apa|bagaimana|como|faq)\b', lower):
            return 'faq_helper'
        if re.search(r'\b(id|data|kirim|krm|submit|screenshot|nomor|phone|profil|profile)\b', lower):
            return 'newcomer_guide'
        if any(token in lower for token in ['kode', 'code', 'código', 'codigo']):
            return 'faq_helper'
        motivation_patterns = [
            r'\bsemangat\b', r'\bkonsisten\b', r'pelan[\s-]*pelan', r'\bsedikit demi sedikit\b',
            r'\btetap jalan\b', r'\blanjut(?:kan)? terus\b', r'\bjangan menyerah\b', r'\btidak apa[\s-]*apa\b',
            r'\bngg?a?k apa[\s-]*apa\b', r'\bsabar\b', r'\byang penting\b.*\b(jalan|lanjut|coba|mulai)\b',
            r'\bbisa kok\b', r'\bpasti bisa\b', r'\bkejar target\b', r'\bincome\b', r'\bearning\b',
            r'\bbonus\b', r'\bmotiva',
        ]
        if any(re.search(pattern, lower) for pattern in motivation_patterns):
            return 'motivation_admin'
        community_patterns = [
            r'\bjangan malu\b', r'\bngobrol\b', r'\bsaling sapa\b', r'\bsuasana (?:hidup|rame|ramai)\b',
            r'\bbiar (?:rame|ramai|hidup)\b', r'\bhalo\b', r'\bselamat\b', r'\bwelcome\b',
            r'\bjoin\b', r'\bgabung\b', r'\bgrup\b', r'\bramai\b', r'\bkenalan\b',
        ]
        if any(re.search(pattern, lower) for pattern in community_patterns):
            return 'community_seed'
        return ''

    @staticmethod
    def _classify_group_atmosphere_record_role(text: str) -> str:
        return Service._group_atmosphere_semantic_intent(text) or 'community_seed'

    @staticmethod
    def _group_atmosphere_sender_kind(sender: str = '') -> str:
        value = re.sub(r'\s+', ' ', str(sender or '').strip().lower())
        if not value:
            return 'unknown'
        system_tokens = ['whatsapp', 'system', '系统', 'message', 'notification']
        if any(token in value for token in system_tokens):
            return 'system'
        operator_tokens = [
            'admin', 'mimin', 'cs', 'customer service', 'support', 'operator', 'ops', 'moderator',
            'mod', 'staff', '客服', '运营', '管理', '管理员', '助手', '助理',
        ]
        if any(token in value for token in operator_tokens):
            return 'operator'
        user_tokens = ['user', 'member', 'anggota', 'customer', 'client', 'lead', '用户', '会员', '客户', '成员']
        if any(token in value for token in user_tokens):
            return 'user'
        if re.fullmatch(r'\+?\d[\d\s().-]{5,}\d', value):
            return 'user'
        return 'unknown'

    @staticmethod
    def _group_atmosphere_message_is_likely_customer_request(text: str) -> bool:
        lower = re.sub(r'\s+', ' ', str(text or '').strip().lower())
        if not lower:
            return False
        if '?' in lower or '？' in lower:
            if re.search(r'\b(?:gimana|gmn|gmna|dimana|dmn|apa|bagaimana|boleh tau|caranya|maksudnya|kenapa|berapa|como|cómo|onde|how|why|what)\b', lower):
                return True
        first_person_request = [
            r'\b(?:aku|saya|gw|gue|me|yo|eu)\b.{0,32}\b(?:bingung|mau|tidak bisa|gak bisa|nggak bisa|belum paham|gmn|gmna|gimana|dimana|dmn|caranya)\b',
            r'\b(?:boleh tau|caranya|nyariin|maksudnya|gimana|gmna|bagaimana)\b',
        ]
        if any(re.search(pattern, lower) for pattern in first_person_request):
            return True
        if re.search(r'^\s*(?:kak|kk|admin|mimin)[,\s]+(?:gimana|gmn|gmna|dimana|dmn|apa|boleh|mau|aku|saya)\b', lower):
            return True
        return False

    @classmethod
    def _extract_group_atmosphere_reusable_reply_candidates(
        cls,
        records: List[GroupAtmosphereChatRecord],
        *,
        role: str = '',
        source_type: str = 'upload_file',
    ) -> Dict[str, Any]:
        candidates: List[Dict[str, Any]] = []
        rejected_items: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()
        for record in records:
            cleaned = cls._clean_group_atmosphere_message_text(str(record.text or ''))
            if not cleaned:
                continue
            sender_kind = cls._group_atmosphere_sender_kind(str(record.sender or ''))
            if sender_kind == 'system':
                rejected_items.append({'text': cleaned, 'role': role, 'reasons': ['system_message'], 'semantic_key': cls._normalize_group_atmosphere_semantic_phrase_key(cleaned)})
                continue
            if sender_kind in {'user', 'unknown'} and cls._group_atmosphere_message_is_likely_customer_request(cleaned):
                rejected_items.append({'text': cleaned, 'role': role, 'reasons': ['user_message'], 'semantic_key': cls._normalize_group_atmosphere_semantic_phrase_key(cleaned)})
                continue
            polished = cls._polish_group_atmosphere_candidate_text(cleaned, role=role)
            key = cls._normalize_group_atmosphere_semantic_phrase_key(polished)
            if not key or key in seen_keys:
                continue
            quality = cls._evaluate_group_atmosphere_candidate_quality(polished, role=role, source_type=source_type)
            if quality.get('decision') == 'reject':
                rejected_items.append({'text': polished, 'role': role, 'reasons': quality.get('reasons') or [], 'semantic_key': quality.get('semantic_key') or key})
                continue
            seen_keys.add(key)
            candidates.append({
                'candidate_id': f'extract-{len(candidates) + 1}',
                'text': polished,
                'topic': role,
                'source': 'extracted_chat_record',
                'source_type': source_type,
                'safe_to_send': False,
                'sender_kind': sender_kind,
                'reason': 'Extracted reusable operator reply from uploaded chat records',
                'quality_status': quality.get('quality_status') or 'pending_review',
                'quality_score': int(quality.get('quality_score') or 0),
                'quality_reasons': list(quality.get('reasons') or []),
            })
        return {'candidates': candidates, 'rejected_items': rejected_items}

    @staticmethod
    def _rewrite_group_atmosphere_semantic_candidate(text: str, *, role: str = '') -> str:
        intent = Service._group_atmosphere_semantic_intent(text)
        target_role = Service._group_atmosphere_semantic_role_key(role or intent)
        if intent == 'motivation_admin':
            if target_role == 'community_seed':
                return 'Kak, tetap semangat ya. Pelan-pelan saja, yang penting terus ikut arahan grup.'
            return 'Kak, pelan-pelan saja ya. Yang penting tetap konsisten dan ikuti arahan dengan benar.'
        if intent == 'community_seed':
            return 'Kak, jangan malu ngobrol di grup ya. Saling sapa biar suasana makin hidup.'
        return ''

    @staticmethod
    def _polish_group_atmosphere_candidate_text(text: str, *, role: str = '') -> str:
        value = re.sub(r'\s+', ' ', str(text or '').strip())
        if not value:
            return ''
        # Keep common Indonesian WhatsApp abbreviations such as jgn/krm/dmn/gmn/kk.
        # They are locally natural and useful for authenticity; polishing only removes noise.
        value = re.sub(r'([!?.,])\1+', r'\1', value)
        value = re.sub(r'([A-Za-zÀ-ÿ])\1{3,}', r'\1\1', value)
        value = re.sub(r'\s+', ' ', value).strip(' -—–:：')
        value = re.sub(r'\badmin admin\b', 'admin', value, flags=re.IGNORECASE)
        if value:
            value = value[0].upper() + value[1:]
        if value and value[-1] not in '.!?。！？':
            value += '?' if '?' in str(text or '') else '.'
        return value[:220]

    @staticmethod
    def _translate_group_atmosphere_candidate_to_zh(text: str) -> str:
        value = str(text or '').strip()
        if not value:
            return ''
        normalized = re.sub(r'[^a-z0-9\s]+', ' ', value.lower())
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        exact = {
            'halo kak selamat datang di grup kalau baru join cek pin grup dulu ya': '你好，欢迎进群。如果刚加入，请先查看群置顶信息。',
            'kak kalau ada yang masih bingung tulis pertanyaannya di grup admin bantu arahkan': '如果还有不清楚的地方，请在群里把问题写出来，管理员会帮忙指引。',
            'yang baru masuk boleh kenalan singkat dulu ya biar grup makin ramai': '新进群的成员可以先简单自我介绍一下，让群里更活跃。',
            'halo kak jangan malu tanya kita bantu pelan pelan sampai paham': '你好，不用不好意思提问，我们会一步一步帮你弄明白。',
            'kak sebelum mulai baca info grup dulu supaya langkahnya tidak salah': '开始前请先阅读群信息，避免操作步骤出错。',
            'kalau sudah siap lanjut ikuti arahan admin ya kak': '如果已经准备好了，请继续按照管理员的指引操作。',
            'grup ini untuk saling bantu kalau ada kendala kabari dengan singkat dan jelas': '这个群是用来互相协助的；如果遇到问题，请简明清楚地说明。',
            'kak tetap pantau info grup ya supaya tidak ketinggalan arahan': '请持续关注群里的信息，避免错过指引。',
            'yang sudah selesai tahap awal boleh update di grup ya kak': '已经完成初始步骤的成员，可以在群里同步一下进度。',
            'selamat bergabung kak semoga prosesnya lancar': '欢迎加入，祝流程顺利。',
            'halo kak kalau bingung soal kode tulis pertanyaannya singkat ya admin bantu cek': '如果对代码有疑问，请简短写出问题，管理员会帮忙查看。',
            'kode pribadi dipakai saat daftar pastikan kodenya benar sebelum kirim data': '个人代码会在注册时使用，提交资料前请确认代码正确。',
            'kalau belum tahu mulai dari mana cek pin grup dulu lalu tanya admin': '如果还不知道从哪里开始，请先看群置顶，再询问管理员。',
            'jika data belum diproses kemungkinan masih antre mohon tunggu arahan admin ya kak': '如果资料还没处理，可能还在排队，请等待管理员指引。',
            'kalau id atau nomor salah kirim koreksi dengan format yang rapi ya kak': '如果 ID 或号码写错了，请按清晰格式重新发送更正信息。',
            'untuk pertanyaan bonus atau tugas sebutkan kendalanya supaya admin bisa bantu arahkan': '关于奖励或任务的问题，请说明具体卡点，方便管理员指引。',
            'kalau tidak bisa screenshot jelaskan bagian yang bermasalah dulu ya kak': '如果无法截图，请先说明具体是哪一步出了问题。',
            'pastikan akun sudah verifikasi sebelum lanjut ke tahap berikutnya': '进入下一步前，请确认账号已经完成验证。',
            'kalau kode tidak terbaca kirim ulang tanpa spasi tambahan ya kak': '如果代码无法识别，请去掉多余空格后重新发送。',
            'jika admin belum balas tunggu antrean sebentar semua akan dicek satu per satu': '如果管理员还没有回复，请稍等排队，所有信息都会逐一检查。',
            'halo kak yang baru join bisa mulai dari panduan dulu lalu kirim data ke admin ya kak': '你好，刚加入的成员可以先看教程，然后把资料发给管理员。',
            'halo kak sebelum lanjut pastikan id nomor dan kode sudah siap ya': '你好，继续之前请确认 ID、号码和代码都已准备好。',
            'halo kak ikuti langkah awal satu per satu kalau mentok kirim pertanyaan ke admin': '你好，请一步一步完成初始流程；如果卡住了，把问题发给管理员。',
            'semangat kak mulai pelan pelan saja yang penting langkahnya benar': '加油，可以慢慢开始，重要的是步骤要正确。',
            'konsisten ikuti panduan dulu hasilnya bisa bertahap': '先持续按照教程操作，结果可以一步步来。',
            'kalau belum paham jangan berhenti tanya admin supaya bisa lanjut': '如果还没明白，不要停在原地，问管理员后继续下一步。',
            'kak proses antrean butuh waktu tenang admin cek satu per satu': '排队处理需要时间，别着急，管理员会逐一检查。',
            'yang sudah selesai tahap awal lanjut jaga aktivitasnya ya kak': '已经完成初始步骤的成员，后续继续保持活跃。',
            'jangan buru buru pastikan data dan tugasnya benar dulu': '不要着急，先确认资料和任务内容是正确的。',
            'semangat kak sedikit sedikit asal rutin akan lebih aman': '加油，少量但持续地做会更稳妥。',
            'kalau ada kendala fokus selesaikan satu langkah dulu': '如果遇到问题，先集中解决当前这一步。',
            'terus pantau arahan grup supaya prosesnya tidak tertinggal': '持续关注群里的指引，避免流程落下。',
            'kak tetap aktif dan ikuti instruksi nanti admin bantu arahkan': '请保持活跃并按照指示操作，之后管理员会继续指引。',
            'halo kk': '你好。',
        }
        if normalized in exact:
            return exact[normalized]
        semantic_fragments = [
            (r'\byang sudah berhasil ambil reward\b', '已经成功领取奖励的人'),
            (r'\bboleh langsung share screenshot bukti penerimaan di grup\b', '可以直接在群里分享收款证明截图'),
            (r'\byang sedang kumpulkan diamond juga\b', '正在积累钻石的人也可以'),
            (r'\bbisa share jumlah diamond kalian sekarang\b', '可以分享你们现在的钻石数量'),
            (r'\bdan kirim screenshot progress di grup\b', '并把进度截图发到群里'),
            (r'\bbiar yang lain juga bisa lihat\b', '让其他人也能看到'),
            (r'\bkalau memang sudah ada yang mulai menghasilkan\b', '确实已经有人开始获得收益'),
            (r'\bkak jangan tunggu lagi ya\b', '不要再等了'),
            (r'\borang lain sudah mulai menghasilkan\b', '别人已经开始获得收益'),
            (r'\bdiamond sudah terkumpul reward sudah jalan\b', '钻石已经积累起来，奖励也已经在正常进行'),
            (r'\bgratis jadi tidak ada salahnya mencoba\b', '这是免费的，试一下没有损失'),
            (r'\bsetiap penghasilan banyak atau sedikit tetap membantu kondisi keluarga dan ekonomi kalian\b', '每一笔收益不管多少，都能帮助你们改善家庭和经济状况'),
            (r'\bjangan pilih pilih yang penting mulai sekarang\b', '不要挑来挑去，重要的是现在就开始'),
            (r'\byang mau maju bertindak duluan\b', '想进步的人要先行动'),
            (r'\bingat peluang tidak menunggu siapa pun\b', '记住，机会不会等任何人'),
            (r'\bburuan kirim id linky kamu dan mulai sekarang\b', '赶紧发送你的 Linky ID，现在就开始'),
        ]
        translated = normalized
        for pattern, replacement in semantic_fragments:
            translated = re.sub(pattern, replacement, translated)
        replacements = [
            (r'\bhalo\b', '你好'), (r'\bselamat datang\b', '欢迎'), (r'\bkak\b|\bkk\b', ''),
            (r'\bjgn\b|\bjangan\b', '不要'), (r'\blupa\b', '忘记'), (r'\bcek\b', '查看'),
            (r'\bpin grup\b', '群置顶'), (r'\binfo grup\b', '群信息'), (r'\bpanduan\b', '教程'),
            (r'\bkrm\b|\bkirim\b', '发送'), (r'\bdata\b', '资料'), (r'\badmin\b|\bmimin\b', '管理员'),
            (r'\bkode\b|\bcode\b', '代码'), (r'\bid\b', 'ID'), (r'\bnomor\b', '号码'),
            (r'\bdmn\b|\bdimana\b', '在哪里'), (r'\bgmn\b|\bgimana\b|\bbagaimana\b', '怎么做'),
            (r'\bke\b', '给'), (r'\bya\b', ''), (r'\bdan\b', '并且'), (r'\batau\b', '或者'),
            (r'\btanya\b', '提问'), (r'\bbingung\b', '不清楚'), (r'\bkendala\b', '问题'),
            (r'\barahan\b', '指引'), (r'\blanjut\b', '继续'), (r'\bmulai\b', '开始'),
            (r'\bsemangat\b', '加油'), (r'\baktif\b', '活跃'), (r'\bverifikasi\b', '验证'),
            (r'\bscreenshot\b|\bss\b', '截图'), (r'\btugas\b', '任务'), (r'\bbonus\b|\breward\b', '奖励'),
            (r'\bdiamond\b', '钻石'), (r'\bgrup\b', '群'), (r'\bshare\b', '分享'),
        ]
        for pattern, replacement in replacements:
            translated = re.sub(pattern, replacement, translated)
        translated = re.sub(r'\s+', ' ', translated).strip(' -—–:：')
        if translated and translated != normalized and not Service._group_atmosphere_translation_has_source_language_residue(translated):
            return translated
        return '暂未生成准确中文翻译，请人工确认原文含义。'

    @classmethod
    def _cap_group_atmosphere_template_pool(cls, templates: List[Dict[str, Any]], *, limit: int = 100) -> List[Dict[str, Any]]:
        items = [dict(item or {}) for item in templates]
        if len(items) <= limit:
            return items
        remove_count = len(items) - limit
        indexed = list(enumerate(items))
        removable = sorted(
            indexed,
            key=lambda pair: (
                1 if (cls._group_atmosphere_truthy_flag(pair[1].get('enabled')) and cls._group_atmosphere_truthy_flag(pair[1].get('safe_to_send'))) else 0,
                1 if pair[1].get('customized') is True else 0,
                int(pair[1].get('frequency') or 1),
                int(pair[1].get('usage_count') or pair[1].get('used_count') or 0),
                int(pair[1].get('score') or 0),
                pair[0],
            ),
        )
        remove_indexes = {idx for idx, _ in removable[:remove_count]}
        return [item for idx, item in indexed if idx not in remove_indexes]

    @classmethod
    def _is_group_atmosphere_useful_candidate(cls, text: str, *, role: str = '') -> bool:
        value = str(text or '').strip()
        if not value or len(value) < 12:
            return False
        lower = value.lower()
        if re.fullmatch(r'[\W_\d\s\U0001F300-\U0001FAFF]+', value):
            return False
        low_value_patterns = [
            r'\bwkwk+\b', r'\bhaha+\b', r'\bhehe+\b', r'\bok(?:ay)?\b', r'\bsiap\b', r'\bmakasih\b',
            r'\bterima kasih\b', r'\bpada kerja mungkin\b', r'\baku tidak mengenali\b', r'\b(?:meler|idung|nongkrong)\b',
            r'\bminta mati\b', r'\bganti nama\b',
            r'\bistilah grup yang sering muncul\b',
            r'\b(?:boleh tau|caranya|nyariin|gimana|gmna)\b.*\?',
        ]
        if any(re.search(pattern, lower) for pattern in low_value_patterns):
            return False
        role_key = cls._group_atmosphere_semantic_role_key(role)
        semantic_intent = cls._group_atmosphere_semantic_intent(value)
        if semantic_intent in {'motivation_admin', 'community_seed'} and (not role_key or semantic_intent == role_key or role_key == 'community_seed'):
            return True
        useful_tokens = ['kak', 'admin', 'kode', 'id', 'data', 'panduan', 'grup', 'kirim', 'krm', 'gimana', 'gmn', 'dimana', 'dmn', 'mulai', 'join', 'gabung', 'tanya', 'semangat', 'bonus', 'tugas', 'screenshot', 'verifikasi', 'profil', 'profile', 'pelan', 'jalan', 'sedikit', 'ngobrol', 'sapa', 'suasana', 'hidup', 'malu', 'paham', 'bantu', 'arahkan', 'arahan', 'siap', 'tunggu']
        if sum(1 for token in useful_tokens if token in lower) < 2:
            return False
        if role_key:
            score = cls._score_group_atmosphere_phrase(value, role=role_key)
            return score >= 18
        return True

    @classmethod
    def _evaluate_group_atmosphere_candidate_quality(cls, text: str, *, role: str = '', source_type: str = '') -> Dict[str, Any]:
        value = str(text or '').strip()
        cleaned = cls._clean_group_atmosphere_message_text(value)
        semantic_key = cls._normalize_group_atmosphere_semantic_phrase_key(cleaned)
        lower = cleaned.lower()
        reasons: List[str] = []
        if not cleaned or len(cleaned) < 12:
            reasons.append('too_short')
        if re.search(r'\bistilah grup yang sering muncul\b|\b(?:frequent terms|common terms|词频|常见词|术语总结)\b', lower):
            reasons.append('meta_summary')
        if re.search(r'\b(?:boleh tau|caranya|nyariin|gimana|gmna|bagaimana|how|why)\b.*\?', lower):
            reasons.append('question_like')
        if re.search(r'\b(?:aku|saya)\b.{0,24}\b(?:bingung|mau|tidak bisa|gak bisa|nggak bisa)\b', lower):
            reasons.append('user_first_person_request')
        if re.search(r'https?://|chat\.whatsapp\.com|@g\.us|\+?\d[\d\s().-]{7,}\d|\b\d{7,}\b', lower):
            reasons.append('dynamic_or_sensitive_token')
        if re.search(r'\b(?:wkwk+|haha+|hehe+|makasih|terima kasih)\b', lower):
            reasons.append('low_value_chat')
        if not reasons and not cls._is_group_atmosphere_useful_candidate(cleaned, role=role):
            reasons.append('low_quality_or_role_mismatch')
        base_score = cls._score_group_atmosphere_phrase(cleaned, role=role) if cleaned else 0
        quality_score = max(0, min(100, 50 + int(base_score) - (35 * len(set(reasons)))))
        decision = 'reject' if reasons else 'accept'
        return {
            'decision': decision,
            'quality_status': 'rejected' if decision == 'reject' else 'pending_review',
            'quality_score': quality_score,
            'safe_to_send': False,
            'enabled': False,
            'reasons': sorted(set(reasons)),
            'normalized_key': cls._normalize_group_atmosphere_phrase_key(cleaned),
            'semantic_key': semantic_key,
            'cleaned_text': cleaned,
            'source_type': str(source_type or '').strip(),
            'role_positioning': str(role or '').strip(),
        }

    def import_group_atmosphere_chat_records_for_account(self, account_key: str, records: List[GroupAtmosphereChatRecord]) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            raise HTTPException(status_code=400, detail='account_key is required')
        account = next((row for row in self.get_group_atmosphere_whatsapp_accounts().get('rows') or [] if row.get('account_key') == normalized_key), None)
        if not account:
            raise HTTPException(status_code=404, detail='group_atmosphere_account_not_found')
        groups = list(account.get('groups') or [])
        first_group = groups[0] if groups else {}
        config_name = normalized_key
        if not self._get_group_atmosphere_config(config_name):
            self.upsert_group_atmosphere_config(GroupAtmosphereConfigRequest(
                config_name=config_name,
                enabled=True,
                account_key=normalized_key,
                target_group=str(first_group.get('target_group') or '').strip() or normalized_key,
                group_name=str(first_group.get('group_name') or '').strip() or str(first_group.get('target_group') or '').strip() or normalized_key,
                language=str(account.get('language') or first_group.get('language') or 'en').strip() or 'en',
                daily_max_messages=int(account.get('daily_max_messages') or 3),
                min_interval_minutes=int(account.get('min_interval_minutes') or 120),
                template_pool=[],
                faq_rules=[],
                worker_base_url='',
            ))
        return self.import_group_atmosphere_chat_records(GroupAtmosphereImportChatRecordsRequest(config_name=config_name, records=records))

    def auto_learn_group_atmosphere_chat_records(self, *, filename: str = '', content: str = '', files: Optional[List[Dict[str, Any]]] = None, role_positioning: str = '') -> Dict[str, Any]:
        max_file_bytes = 30 * 1024 * 1024
        normalized_files = [item for item in list(files or []) if isinstance(item, dict) and str(item.get('content') or '').strip()]
        for item in normalized_files:
            file_content = str(item.get('content') or '')
            if len(file_content.encode('utf-8')) > max_file_bytes:
                raise HTTPException(status_code=413, detail='upload_file_too_large_30mb')
        if not normalized_files and str(content or '').strip() and len(str(content or '').encode('utf-8')) > max_file_bytes:
            raise HTTPException(status_code=413, detail='upload_file_too_large_30mb')
        if normalized_files:
            filename = ', '.join(str(item.get('filename') or '').strip() or f'file-{idx + 1}' for idx, item in enumerate(normalized_files))
            content = '\n'.join(str(item.get('content') or '') for item in normalized_files)
        records = self._parse_group_atmosphere_chat_export(content)
        if not records:
            raise HTTPException(status_code=400, detail='chat_record_content_is_empty')
        language, region = self._detect_group_atmosphere_language_and_region(records)
        target_role = self._resolve_group_atmosphere_phrase_type_key(role_positioning, required=True)
        role_buckets: Dict[str, List[GroupAtmosphereChatRecord]] = {target_role: records}
        default_group_name = f'自动学习素材库-{region if region != "未知" else language}'
        global_candidate_text_keys = set()
        for existing_config in self.list_group_atmosphere_configs():
            for existing_item in list((existing_config or {}).get('template_pool') or []):
                if not isinstance(existing_item, dict):
                    continue
                existing_key = self._normalize_group_atmosphere_semantic_phrase_key(str(existing_item.get('text') or '').strip())
                if existing_key:
                    global_candidate_text_keys.add(existing_key)
        role_assignments = []
        rejected_items: List[Dict[str, Any]] = []
        for role, role_records in role_buckets.items():
            if not role_records:
                continue
            config_name = f'auto-{language}-{role}'
            if not self._get_group_atmosphere_config(config_name):
                self.upsert_group_atmosphere_config(GroupAtmosphereConfigRequest(
                    config_name=config_name,
                    enabled=False,
                    account_key=config_name,
                    target_group=config_name,
                    group_name=default_group_name,
                    language=language,
                    daily_max_messages=0,
                    min_interval_minutes=120,
                    template_pool=[],
                    faq_rules=[],
                    worker_base_url='',
                    status='candidate_pool',
                ))
            imported = self.import_group_atmosphere_chat_records(GroupAtmosphereImportChatRecordsRequest(config_name=config_name, records=role_records))
            extracted = self._extract_group_atmosphere_reusable_reply_candidates(role_records, role=role, source_type='upload_file')
            rejected_items.extend(list(extracted.get('rejected_items') or []))
            candidates = list(extracted.get('candidates') or [])
            if not candidates:
                candidate_limit = min(100, max(10, len(role_records)))
                candidates = self.generate_group_atmosphere_ai_candidates(GroupAtmosphereAiCandidateRequest(config_name=config_name, topic=role, count=candidate_limit))['candidates']
            existing_config = self._get_group_atmosphere_config(config_name) or {}
            candidate_templates = [dict(item or {}) for item in list(existing_config.get('template_pool') or [])]
            existing_by_text = {
                self._normalize_group_atmosphere_semantic_phrase_key(str(item.get('text') or '').strip()): item
                for item in candidate_templates
                if self._normalize_group_atmosphere_semantic_phrase_key(str(item.get('text') or '').strip())
            }
            seen_candidate_texts = set()
            accepted_candidates: List[Dict[str, Any]] = []
            for candidate in candidates:
                cleaned_text = self._clean_group_atmosphere_message_text(str(candidate.get('text') or ''))
                text_key = self._normalize_group_atmosphere_semantic_phrase_key(cleaned_text)
                if not text_key or text_key in seen_candidate_texts:
                    continue
                seen_candidate_texts.add(text_key)
                quality = self._evaluate_group_atmosphere_candidate_quality(cleaned_text, role=role, source_type='upload_file')
                if quality.get('decision') == 'reject':
                    rejected_items.append({'text': cleaned_text, 'role': role, 'reasons': quality.get('reasons') or [], 'semantic_key': quality.get('semantic_key') or text_key})
                    continue
                text_key = str(quality.get('semantic_key') or text_key)
                score = self._score_group_atmosphere_phrase(cleaned_text, role=role)
                existing_item = existing_by_text.get(text_key)
                if text_key in global_candidate_text_keys and existing_item is None:
                    continue
                if existing_item is not None:
                    existing_item['frequency'] = int(existing_item.get('frequency') or 1) + 1
                    existing_item['score'] = max(int(existing_item.get('score') or 0), score)
                    existing_item.setdefault('source_role', role)
                    existing_item.setdefault('category', role)
                    accepted = dict(candidate)
                    accepted.update({
                        'candidate_id': existing_item.get('candidate_id') or existing_item.get('template_id') or candidate.get('candidate_id'),
                        'text': cleaned_text,
                        'topic': role,
                        'source': candidate.get('source') or 'extracted_chat_record',
                        'safe_to_send': False,
                        'duplicate_existing': True,
                    })
                    accepted_candidates.append(accepted)
                    continue
                new_item = {
                    'template_id': create_id('gatpl'),
                    'candidate_id': create_id('gacand'),
                    'category': role,
                    'source_role': role,
                    'text': cleaned_text,
                    'text_zh': '',
                    'text_zh_source': '',
                    'text_zh_status': 'needs_translation',
                    'score': score,
                    'frequency': 1,
                    'safe_to_send': False,
                    'enabled': False,
                    'source_type': 'upload_file',
                    'quality_decision': quality.get('decision') or 'accept',
                    'quality_status': quality.get('quality_status') or 'pending_review',
                    'quality_score': int(quality.get('quality_score') or score),
                    'quality_reasons': list(quality.get('reasons') or []),
                    'normalized_key': quality.get('normalized_key') or self._normalize_group_atmosphere_phrase_key(cleaned_text),
                    'semantic_key': quality.get('semantic_key') or text_key,
                }
                candidate_templates.append(new_item)
                existing_by_text[text_key] = new_item
                global_candidate_text_keys.add(text_key)
                accepted = dict(candidate)
                accepted.update({
                    'candidate_id': new_item['candidate_id'],
                    'text': cleaned_text,
                    'topic': role,
                    'source': candidate.get('source') or 'extracted_chat_record',
                    'safe_to_send': False,
                })
                accepted_candidates.append(accepted)
            candidate_templates = self._sort_group_atmosphere_candidates(self._cap_group_atmosphere_template_pool(candidate_templates, limit=100))
            conn = self.db.connect()
            conn.execute(
                "UPDATE whatsapp_group_atmosphere_configs SET template_pool=?, updated_at=? WHERE config_name=?",
                (json.dumps(candidate_templates, ensure_ascii=False), utc_now(), config_name),
            )
            conn.commit()
            role_assignments.append({
                'role_positioning': role,
                'config_name': config_name,
                'imported_count': imported['imported_count'],
                'profile': imported['language_profile'],
                'candidates': accepted_candidates,
            })
        return {
            'ok': True,
            'filename': str(filename or '').strip(),
            'file_count': len(normalized_files) if normalized_files else (1 if str(filename or '').strip() or str(content or '').strip() else 0),
            'detected_language': language,
            'detected_region': region,
            'imported_count': len(records),
            'role_assignments': role_assignments,
            'rejected_count': len(rejected_items),
            'rejected_reasons': sorted({reason for item in rejected_items for reason in list(item.get('reasons') or [])}),
            'rejected_items': rejected_items[:20],
        }

    @staticmethod
    def _group_atmosphere_region_from_language(language: str) -> str:
        return {'id': '印尼', 'es': '墨西哥', 'pt': '巴西'}.get(str(language or '').strip(), '未知')

    @staticmethod
    def _default_group_atmosphere_plan_display_name(role_positioning: str, region: str) -> str:
        role_name = str(role_positioning or '').strip() or '话术包'
        region_name = str(region or '').strip()
        return f'{region_name} · {role_name}' if region_name and region_name != '未知' else role_name

    @staticmethod
    def _group_atmosphere_language_from_region(region: str) -> str:
        value = str(region or '').strip().lower()
        if value in {'印尼', 'indonesia', 'id', 'indo'}:
            return 'id'
        if value in {'墨西哥', 'mexico', 'mx', '委内瑞拉', 'venezuela', 've', '智利', 'chile', 'cl', '哥伦比亚', 'colombia', 'co', 'es', '西语'}:
            return 'es'
        if value in {'巴西', 'brazil', 'br', 'pt', '葡语'}:
            return 'pt'
        return str(region or '').strip() or 'id'

    @staticmethod
    def _group_atmosphere_role_from_key(role_key: str) -> str:
        key = str(role_key or '').strip()
        if key.startswith('auto-') or key.startswith('role-'):
            parts = key.split('-', 2)
            if len(parts) >= 3 and parts[2]:
                role_part = parts[2]
                if role_part in GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS:
                    return ''
                if any(role_part.startswith(f'{legacy_key}-') for legacy_key in GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS):
                    return ''
                return role_part
        return ''

    @staticmethod
    def _normalize_group_atmosphere_translation_text(text_zh: str) -> str:
        value = str(text_zh or '').strip()
        value = re.sub(r'^\s*(?:大意|含义|中文(?:翻译)?|翻译)\s*[:：]\s*', '', value)
        value = re.sub(r'[ \t]+', ' ', value)
        value = re.sub(r'\s+\n', '\n', value)
        return value.strip()[:1500]

    @staticmethod
    def _group_atmosphere_translation_has_source_language_residue(text_zh: str) -> bool:
        value = str(text_zh or '').strip().lower()
        if not value:
            return True
        allowed_latin_words = {
            'id', 'linky', 'whatsapp', 'wa', 'url', 'http', 'https', 'api', 'ai', 'faq', 'mcn',
        }
        scrubbed = value
        for word in allowed_latin_words:
            scrubbed = re.sub(rf'\b{re.escape(word)}\b', ' ', scrubbed)
        residue_words = {
            # Indonesian WhatsApp operations vocabulary.
            'kak', 'kk', 'jangan', 'malu', 'ngobrol', 'saling', 'sapa', 'suasana', 'hidup', 'tetap',
            'semangat', 'pelan', 'arahan', 'grup', 'user', 'nyariin', 'boleh', 'tau', 'caranya',
            'supaya', 'inget', 'terus', 'sama', 'kita', 'gmna', 'gimana', 'bagaimana', 'yang', 'sudah',
            'berhasil', 'ambil', 'reward', 'langsung', 'share', 'screenshot', 'bukti', 'penerimaan',
            'sedang', 'kumpulkan', 'jumlah', 'kalian', 'sekarang', 'kirim', 'progress', 'biar', 'lain',
            'bisa', 'lihat', 'kalau', 'memang', 'ada', 'mulai', 'menghasilkan', 'orang', 'tunggu',
            'lagi', 'terkumpul', 'jalan', 'gratis', 'jadi', 'tidak', 'salahnya', 'mencoba', 'setiap',
            'penghasilan', 'banyak', 'sedikit', 'membantu', 'kondisi', 'keluarga', 'ekonomi', 'pilih',
            'penting', 'mau', 'maju', 'bertindak', 'duluan', 'ingat', 'peluang', 'menunggu', 'siapa',
            'pun', 'buruan', 'kamu', 'admin', 'kode', 'nomor', 'dimana', 'dmn', 'tanya', 'bingung',
            'kendala', 'lanjut', 'verifikasi', 'tugas', 'bonus', 'diamond', 'data', 'panduan',
            # Spanish / Portuguese common operations vocabulary.
            'hola', 'chicas', 'por', 'favor', 'revisen', 'cuando', 'alcances', 'diamantes', 'contacta',
            'administrador', 'grupo', 'capacitación', 'capacitacion', 'donde', 'aprenderás', 'aprenderas',
            'cómo', 'como', 'realizar', 'retiros', 'conseguir', 'más', 'mas', 'mejorar', 'respuesta',
            'resultados', 'ahora', 'momento', 'siguiente', 'paso', 'éxito', 'exito', 'vamos',
            'olá', 'ola', 'vocês', 'voces', 'grupo', 'print', 'comprovante', 'recompensa', 'ganhos',
            'começar', 'comecar', 'enviar', 'código', 'codigo', 'mensagem', 'administrador',
        }
        latin_words = [w for w in re.findall(r'\b[a-záéíóúüñçãõâêôà]+\b', scrubbed) if w not in allowed_latin_words]
        if any(word in residue_words for word in latin_words):
            return True
        # A real Chinese translation should not contain multiple unexplained Latin words.
        return len([w for w in latin_words if len(w) >= 3]) >= 2

    @classmethod
    def _group_atmosphere_translation_is_usable(cls, text_zh: str, source: str = '') -> bool:
        value = cls._normalize_group_atmosphere_translation_text(text_zh)
        if not value:
            return False
        if str(source or '').strip() == 'manual':
            return True
        return not cls._group_atmosphere_translation_has_source_language_residue(value)

    @classmethod
    def _sanitize_group_atmosphere_translation_payload(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(item or {})
        text_zh = cls._normalize_group_atmosphere_translation_text(str(payload.get('text_zh') or ''))
        source = str(payload.get('text_zh_source') or '').strip()
        if text_zh and cls._group_atmosphere_translation_is_usable(text_zh, source):
            payload['text_zh'] = text_zh
            return payload
        if text_zh and source != 'manual':
            payload['text_zh'] = ''
            payload['text_zh_status'] = 'needs_translation'
            payload['text_zh_failure_reason'] = str(payload.get('text_zh_failure_reason') or '翻译结果仍包含外语原词，已隐藏，请重新翻译或人工修正。')
        return payload

    @staticmethod
    def _group_atmosphere_translation_cache_key(text: str, *, language: str = '', region: str = '') -> str:
        normalized_text = re.sub(r'\s+', ' ', str(text or '').strip())
        raw = json.dumps(
            {
                'text': normalized_text,
                'language': str(language or '').strip().lower(),
                'region': str(region or '').strip().lower(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    @staticmethod
    def _group_atmosphere_translation_result_from_cache(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
        return {
            'text_zh': str(row['text_zh'] or ''),
            'text_zh_source': str(row['text_zh_source'] or ''),
            'text_zh_status': str(row['text_zh_status'] or ''),
            'text_zh_updated_at': str(row['updated_at'] or ''),
            'text_zh_failure_reason': str(row['failure_reason'] or ''),
            'text_zh_retry_count': int(row['retry_count'] or 0),
        }

    def _get_group_atmosphere_translation_cache(self, text: str, *, language: str = '', region: str = '') -> Optional[Dict[str, Any]]:
        value = str(text or '').strip()
        if not value:
            return None
        normalized_language = str(language or '').strip()
        normalized_region = str(region or '').strip()
        cache_key = self._group_atmosphere_translation_cache_key(value, language=language, region=region)
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT cache_key, text, language, region, text_zh, text_zh_source, text_zh_status,
                       failure_reason, retry_count, next_retry_at, created_at, updated_at
                  FROM whatsapp_group_atmosphere_translation_cache
                 WHERE cache_key=?
                """,
                (cache_key,),
            ).fetchone()
            if not row:
                region_variants = sorted({normalized_region, self._group_atmosphere_region_from_language(normalized_language), ''})
                language_variants = sorted({normalized_language, ''})
                row = conn.execute(
                    f"""
                    SELECT cache_key, text, language, region, text_zh, text_zh_source, text_zh_status,
                           failure_reason, retry_count, next_retry_at, created_at, updated_at
                      FROM whatsapp_group_atmosphere_translation_cache
                     WHERE text=? AND language IN ({','.join('?' for _ in language_variants)})
                       AND region IN ({','.join('?' for _ in region_variants)})
                     ORDER BY CASE text_zh_source WHEN 'manual' THEN 0 WHEN 'ai' THEN 1 ELSE 2 END, updated_at DESC
                     LIMIT 1
                    """,
                    (value, *language_variants, *region_variants),
                ).fetchone()
        return dict(row) if row else None

    def _save_group_atmosphere_translation_cache(
        self,
        *,
        text: str,
        language: str = '',
        region: str = '',
        text_zh: str = '',
        source: str = '',
        status: str = '',
        failure_reason: str = '',
        retry_count: Optional[int] = None,
        next_retry_at: str = '',
    ) -> Dict[str, Any]:
        value = str(text or '').strip()
        if not value:
            return {}
        normalized_language = str(language or '').strip()
        normalized_region = str(region or '').strip()
        cache_key = self._group_atmosphere_translation_cache_key(value, language=normalized_language, region=normalized_region)
        now = utc_now()
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT retry_count, created_at FROM whatsapp_group_atmosphere_translation_cache WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            next_retry_count = int(retry_count if retry_count is not None else (existing['retry_count'] if existing else 0) or 0)
            created_at = str(existing['created_at'] if existing else now)
            conn.execute(
                """
                INSERT INTO whatsapp_group_atmosphere_translation_cache (
                    cache_key, text, language, region, text_zh, text_zh_source, text_zh_status,
                    failure_reason, retry_count, next_retry_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    text=excluded.text,
                    language=excluded.language,
                    region=excluded.region,
                    text_zh=excluded.text_zh,
                    text_zh_source=excluded.text_zh_source,
                    text_zh_status=excluded.text_zh_status,
                    failure_reason=excluded.failure_reason,
                    retry_count=excluded.retry_count,
                    next_retry_at=excluded.next_retry_at,
                    updated_at=excluded.updated_at
                """,
                (
                    cache_key,
                    value,
                    normalized_language,
                    normalized_region,
                    str(text_zh or '').strip()[:1500],
                    str(source or '').strip(),
                    str(status or '').strip(),
                    str(failure_reason or '').strip()[:500],
                    next_retry_count,
                    str(next_retry_at or '').strip(),
                    created_at,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT cache_key, text, language, region, text_zh, text_zh_source, text_zh_status,
                       failure_reason, retry_count, next_retry_at, created_at, updated_at
                  FROM whatsapp_group_atmosphere_translation_cache
                 WHERE cache_key=?
                """,
                (cache_key,),
            ).fetchone()
        return dict(row) if row else {}

    def _group_atmosphere_translation_retry_due(self, cache: Dict[str, Any], *, retry_failed: bool = False) -> bool:
        if not cache:
            return True
        if retry_failed:
            return True
        retry_count = int(cache.get('retry_count') or 0)
        if retry_count >= 2:
            return False
        next_retry_at = str(cache.get('next_retry_at') or '').strip()
        if not next_retry_at:
            return True
        try:
            return parse_iso_datetime(next_retry_at) <= datetime.now(timezone.utc)
        except Exception:
            return True

    def _group_atmosphere_translation_failure_result(
        self,
        text: str,
        *,
        language: str = '',
        region: str = '',
        error: Any = '',
    ) -> Dict[str, Any]:
        cache = self._get_group_atmosphere_translation_cache(text, language=language, region=region) or {}
        retry_count = min(2, int(cache.get('retry_count') or 0) + 1)
        retry_delay_seconds = 60 * retry_count
        next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=retry_delay_seconds)).isoformat() if retry_count < 2 else ''
        failure_reason = str(error or 'translator_failed').strip()[:500]
        saved = self._save_group_atmosphere_translation_cache(
            text=text,
            language=language,
            region=region,
            text_zh='',
            source='ai',
            status='failed',
            failure_reason=failure_reason,
            retry_count=retry_count,
            next_retry_at=next_retry_at,
        )
        result = self._group_atmosphere_translation_result_from_cache(saved)
        if not result.get('text_zh_updated_at'):
            result['text_zh_updated_at'] = utc_now()
        return result

    def _build_group_atmosphere_candidate_translation(self, text: str, *, role: str = '', language: str = '', region: str = '', force: bool = False) -> Dict[str, Any]:
        value = str(text or '').strip()
        if not value:
            return {'text_zh': '', 'text_zh_source': '', 'text_zh_status': '', 'text_zh_updated_at': utc_now()}
        cached = self._get_group_atmosphere_translation_cache(value, language=language, region=region)
        if cached and not force:
            cached_result = self._group_atmosphere_translation_result_from_cache(cached)
            cached_status = str(cached_result.get('text_zh_status') or '').strip()
            if cached_result.get('text_zh') and cached_result.get('text_zh_source') in {'ai', 'google', 'libretranslate', 'manual', 'rule'} and self._group_atmosphere_translation_is_usable(str(cached_result.get('text_zh') or ''), str(cached_result.get('text_zh_source') or '')):
                return cached_result
            if cached_status == 'failed' and not self._group_atmosphere_translation_retry_due(cached):
                return cached_result
        translator = self.group_atmosphere_candidate_translator
        if translator is not None and value:
            try:
                translated = translator.translate(value, role=role, language=language, region=region)
            except Exception as exc:
                return self._group_atmosphere_translation_failure_result(value, language=language, region=region, error=exc)
            if translated and str(translated.get('text_zh') or '').strip():
                text_zh = self._normalize_group_atmosphere_translation_text(str(translated.get('text_zh') or ''))
                status = str(translated.get('status') or 'ok').strip()
                source = str(translated.get('source') or 'ai').strip() or 'ai'
                if not self._group_atmosphere_translation_is_usable(text_zh, source):
                    return self._group_atmosphere_translation_failure_result(value, language=language, region=region, error='translation_contains_source_language_residue')
                saved = self._save_group_atmosphere_translation_cache(
                    text=value,
                    language=language,
                    region=region,
                    text_zh=text_zh,
                    source=source,
                    status=status if status in {'ok', 'needs_review'} else 'ok',
                    failure_reason='',
                    retry_count=0,
                    next_retry_at='',
                )
                return {
                    'text_zh': text_zh,
                    'text_zh_source': source,
                    'text_zh_status': status if status in {'ok', 'needs_review'} else 'ok',
                    'text_zh_updated_at': str(saved.get('updated_at') or utc_now()),
                    'text_zh_failure_reason': '',
                    'text_zh_retry_count': 0,
                }
        fallback = self._candidate_translation_fallback(value)
        fallback['text_zh_updated_at'] = utc_now()
        self._save_group_atmosphere_translation_cache(
            text=value,
            language=language,
            region=region,
            text_zh=str(fallback.get('text_zh') or ''),
            source=str(fallback.get('text_zh_source') or ''),
            status=str(fallback.get('text_zh_status') or ''),
            failure_reason='',
            retry_count=0,
            next_retry_at='',
        )
        return fallback

    def translate_group_atmosphere_manual_upload_text(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        text = str((payload or {}).get('text') or '').strip()
        if not text:
            raise HTTPException(status_code=400, detail='candidate_text_required')
        language = str((payload or {}).get('language') or '').strip()
        region = str((payload or {}).get('region') or '').strip()
        if not language:
            language = self._guess_group_atmosphere_manual_upload_language(text, self._group_atmosphere_language_from_region(region) or 'id')
        if not region:
            region = self._group_atmosphere_region_from_language(language)
        translated = self._build_group_atmosphere_candidate_translation(
            text,
            role=self._resolve_group_atmosphere_phrase_type_key(str((payload or {}).get('role_positioning') or (payload or {}).get('role') or '')),
            language=language,
            region=region,
        )
        return {'ok': True, **translated}

    @staticmethod
    def _normalize_group_atmosphere_unique_name(value: str) -> str:
        return re.sub(r'\s+', ' ', str(value or '').strip()).casefold()

    def _find_group_atmosphere_role_duplicate_by_name(
        self,
        conn: sqlite3.Connection,
        role_name: str,
        *,
        language: str = '',
        exclude_key: str = '',
    ) -> Optional[Dict[str, Any]]:
        needle = self._normalize_group_atmosphere_unique_name(role_name)
        if not needle:
            return None
        normalized_language = str(language or '').strip()
        excluded = str(exclude_key or '').strip()
        rows = conn.execute(
            """
            SELECT *
            FROM whatsapp_group_atmosphere_configs
            WHERE COALESCE(group_name, '') <> ''
              AND (? = '' OR config_name <> ?)
              AND (? = '' OR language = ?)
            ORDER BY updated_at DESC, config_name ASC
            """,
            (excluded, excluded, normalized_language, normalized_language),
        ).fetchall()
        for row in rows:
            config = self._row_to_group_atmosphere_config(row)
            if not self._group_atmosphere_should_surface_as_role(config):
                continue
            if self._normalize_group_atmosphere_unique_name(str(config.get('group_name') or '')) == needle:
                return config
        return None

    def upsert_group_atmosphere_manual_phrases(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        role_key = str((payload or {}).get('role_key') or (payload or {}).get('config_name') or '').strip()
        payload_phrases = list((payload or {}).get('phrases') or [])
        payload_text = str((payload or {}).get('text') or '').strip()
        if not role_key and not any(str(item or '').strip() for item in payload_phrases) and not payload_text:
            raise HTTPException(status_code=400, detail='role_key_or_phrases_required')
        region = str((payload or {}).get('region') or self._group_atmosphere_region_from_language(str((payload or {}).get('language') or 'id'))).strip()
        language = str((payload or {}).get('language') or self._group_atmosphere_language_from_region(region)).strip() or 'id'
        source_type = str((payload or {}).get('source_type') or 'manual').strip() or 'manual'
        if role_key.startswith('auto-') and source_type not in {'upload_file', 'auto_learn', 'learning_account', 'learning_bot', 'manual_upload'}:
            role_key = ''
        role_positioning_for_key = self._resolve_group_atmosphere_phrase_type_key(
            str((payload or {}).get('phrase_type') or (payload or {}).get('role_positioning') or self._group_atmosphere_role_from_key(role_key) or ''),
            required=True,
        )
        role_positioning = role_positioning_for_key
        role_name = str((payload or {}).get('role_name') or (payload or {}).get('plan_display_name') or self._default_group_atmosphere_plan_display_name(role_positioning, region)).strip()
        with self.db.connect() as conn:
            duplicate_role = self._find_group_atmosphere_role_duplicate_by_name(
                conn,
                role_name,
                language=language,
                exclude_key=role_key,
            )
            if duplicate_role:
                raise HTTPException(status_code=409, detail='group_atmosphere_role_name_duplicate')
        if not role_key:
            role_name_for_key = role_name
            base_slug = re.sub(r'[^a-z0-9]+', '-', role_name_for_key.lower()).strip('-')[:36] if role_name_for_key else ''
            base_key = f'role-{language}-{role_positioning_for_key}' + (f'-{base_slug}' if base_slug else '')
            with self.db.connect() as conn:
                existing_rows = [
                    {
                        'config_name': str(row['config_name'] or '').strip(),
                        'group_name': str(row['group_name'] or '').strip(),
                    }
                    for row in conn.execute(
                        "SELECT config_name, group_name FROM whatsapp_group_atmosphere_configs WHERE config_name LIKE ? ORDER BY config_name ASC",
                        (f'{base_key}%',),
                    ).fetchall()
                ]
            existing_keys = {row['config_name'] for row in existing_rows}
            exact_name_keys = [row['config_name'] for row in existing_rows if row['group_name'] == role_name_for_key]
            if exact_name_keys:
                raise HTTPException(status_code=409, detail='group_atmosphere_role_name_duplicate')
            elif base_key not in existing_keys:
                role_key = base_key
            else:
                idx = 2
                while f'{base_key}-{idx}' in existing_keys:
                    idx += 1
                role_key = f'{base_key}-{idx}'
        raw_phrases = [str(item or '').strip() for item in list((payload or {}).get('phrases') or []) if str(item or '').strip()]
        if not raw_phrases:
            phrase_text = str((payload or {}).get('text') or '').strip()
            if phrase_text:
                raw_phrases = [line.strip() for line in phrase_text.splitlines() if line.strip()]
        replace_role_phrases = bool((payload or {}).get('replace_role_phrases') or (payload or {}).get('replace_phrases'))
        is_manual_source = self._group_atmosphere_candidate_is_manual({'source_type': source_type, 'customized': (payload or {}).get('customized') is True})
        source_candidates_by_text: Dict[str, Dict[str, Any]] = {}
        for source_item in list((payload or {}).get('source_candidates') or []):
            if not isinstance(source_item, dict):
                continue
            source_text = str(source_item.get('text') or '').strip()
            source_key = self._normalize_group_atmosphere_semantic_phrase_key(source_text) or source_text
            if not source_key:
                continue
            current = source_candidates_by_text.get(source_key) or {}
            source_has_media = bool(source_item.get('media_id') or source_item.get('media_path') or str(source_item.get('asset_type') or '').startswith('image'))
            current_has_media = bool(current.get('media_id') or current.get('media_path') or str(current.get('asset_type') or '').startswith('image'))
            source_has_translation = bool(str(source_item.get('text_zh') or '').strip())
            current_has_translation = bool(str(current.get('text_zh') or '').strip())
            if source_has_media or (source_has_translation and not current_has_translation) or not current_has_media:
                source_candidates_by_text[source_key] = dict(source_item)
        phrases = []
        quality_by_key: Dict[str, Dict[str, Any]] = {}
        rejected_items: List[Dict[str, Any]] = []
        added_items: List[Dict[str, Any]] = []
        for text in raw_phrases:
            if is_manual_source:
                # 人工写入是运营明确确认的文案：不做清洗、过滤、润色或去重，非空即通过。
                manual_text = str(text or '').strip()
                if manual_text:
                    phrases.append(manual_text)
                continue
            cleaned = self._clean_group_atmosphere_message_text(text)
            key = self._normalize_group_atmosphere_semantic_phrase_key(cleaned)
            if not key:
                continue
            if source_type in {'learning_account', 'learning_bot', 'upload_file', 'auto_learn'}:
                quality = self._evaluate_group_atmosphere_candidate_quality(cleaned, role=role_positioning, source_type=source_type)
                if quality.get('decision') == 'reject':
                    rejected_items.append({'text': cleaned, 'reasons': quality.get('reasons') or [], 'semantic_key': quality.get('semantic_key') or key})
                    continue
                cleaned = self._polish_group_atmosphere_candidate_text(cleaned, role=role_positioning)
                key = self._normalize_group_atmosphere_semantic_phrase_key(cleaned)
                quality = self._evaluate_group_atmosphere_candidate_quality(cleaned, role=role_positioning, source_type=source_type)
                if not key or quality.get('decision') == 'reject':
                    rejected_items.append({'text': cleaned, 'reasons': quality.get('reasons') or [], 'semantic_key': quality.get('semantic_key') or key})
                    continue
                quality_by_key[key] = quality
            phrases.append(cleaned)
        existing = self._get_group_atmosphere_config(role_key)
        templates = [dict(item or {}) for item in list((existing or {}).get('template_pool') or [])]
        existing_by_text = {self._normalize_group_atmosphere_semantic_phrase_key(str(item.get('text') or '').strip()): item for item in templates}
        safe_to_send = bool((payload or {}).get('safe_to_send', True))
        enabled = bool((payload or {}).get('enabled', True))
        if replace_role_phrases:
            # 保存话术角色只更新“允许该角色发送哪些话术”，不能把未勾选的话术从话术类型/话术库里物理删除。
            # 未勾选的话术仍保留在 template_pool 中，只把 enabled 置为 False，使调度发送时不选它。
            deduped_phrases: List[str] = []
            selected_replace_keys: set[str] = set()
            for text in phrases:
                key = self._normalize_group_atmosphere_semantic_phrase_key(text) or str(text or '').strip()
                if not key or key in selected_replace_keys:
                    continue
                selected_replace_keys.add(key)
                deduped_phrases.append(text)
            selected_order_by_key = {
                (self._normalize_group_atmosphere_semantic_phrase_key(text) or str(text or '').strip()): idx
                for idx, text in enumerate(deduped_phrases, start=1)
            }
            for source_config in self.list_group_atmosphere_configs():
                for source_item in list((source_config or {}).get('template_pool') or []):
                    if not isinstance(source_item, dict):
                        continue
                    source_text = str(source_item.get('text') or '').strip()
                    source_key = self._normalize_group_atmosphere_semantic_phrase_key(source_text) or source_text
                    source_role = str(source_item.get('source_role') or source_item.get('role_positioning') or source_item.get('category') or self._group_atmosphere_role_from_key(str(source_config.get('config_name') or ''))).strip()
                    if not source_key or source_role != role_positioning:
                        continue
                    current = source_candidates_by_text.get(source_key) or {}
                    source_has_media = bool(source_item.get('media_id') or source_item.get('media_path') or str(source_item.get('asset_type') or '').startswith('image'))
                    current_has_media = bool(current.get('media_id') or current.get('media_path') or str(current.get('asset_type') or '').startswith('image'))
                    if source_has_media or not current_has_media:
                        source_candidates_by_text[source_key] = dict(source_item)
            rebuilt_templates: List[Dict[str, Any]] = []
            existing_seen_keys: set[str] = set()
            for item in templates:
                existing_item = dict(item or {})
                text = str(existing_item.get('text') or '').strip()
                if not text:
                    continue
                text_key = self._normalize_group_atmosphere_semantic_phrase_key(text) or text
                if text_key in existing_seen_keys:
                    continue
                existing_seen_keys.add(text_key)
                selected = text_key in selected_replace_keys
                existing_item['category'] = role_positioning
                existing_item['source_role'] = role_positioning
                existing_item['role_positioning'] = role_positioning
                existing_item['source_type'] = existing_item.get('source_type') or source_type
                source_item = source_candidates_by_text.get(text_key) or {}
                if selected and source_item and not (existing_item.get('media_id') or existing_item.get('media_path')):
                    for media_key in ['asset_type', 'media_id', 'media_path', 'media_mime_type', 'media_filename', 'media_preview_url']:
                        if source_item.get(media_key):
                            existing_item[media_key] = source_item.get(media_key)
                existing_item['role_selected'] = bool(selected)
                existing_item['role_selection_order'] = selected_order_by_key.get(text_key, 0) if selected else 0
                existing_item['safe_to_send'] = safe_to_send if is_manual_source else self._group_atmosphere_truthy_flag(existing_item.get('safe_to_send'))
                existing_item['enabled'] = bool(enabled and selected) if is_manual_source else bool(selected and existing_item.get('enabled') is not False and self._group_atmosphere_truthy_flag(existing_item.get('safe_to_send')))
                existing_item['role_send_enabled'] = bool(selected)
                rebuilt_templates.append(existing_item)
            for text in deduped_phrases:
                text_key = self._normalize_group_atmosphere_semantic_phrase_key(text) or str(text or '').strip()
                if not text_key or text_key in existing_seen_keys:
                    continue
                source_item = dict(source_candidates_by_text.get(text_key) or {})
                translation = dict(source_item) if source_item.get('text_zh') else {'text_zh': '', 'text_zh_source': '', 'text_zh_status': 'needs_translation'}
                new_item = {
                    'template_id': source_item.get('template_id') or create_id('gatpl'),
                    'candidate_id': source_item.get('candidate_id') or create_id('gacand'),
                    'category': role_positioning,
                    'source_role': role_positioning,
                    'source_type': source_item.get('source_type') or source_type,
                    'text': text,
                    'text_zh': translation.get('text_zh') or '',
                    'text_zh_source': translation.get('text_zh_source') or '',
                    'text_zh_status': translation.get('text_zh_status') or '',
                    'text_zh_updated_at': translation.get('text_zh_updated_at') or '',
                    'text_zh_failure_reason': translation.get('text_zh_failure_reason') or '',
                    'text_zh_retry_count': int(translation.get('text_zh_retry_count') or 0),
                    'score': int(source_item.get('score') or self._score_group_atmosphere_phrase(text, role=role_positioning)),
                    'frequency': int(source_item.get('frequency') or 1),
                    'safe_to_send': safe_to_send if is_manual_source else False,
                    'enabled': enabled if is_manual_source else False,
                    'role_send_enabled': True,
                    'role_selected': True,
                    'role_selection_order': selected_order_by_key.get(text_key, 0),
                    'quality_decision': 'accept' if is_manual_source else (quality_by_key.get(text_key, {}).get('decision') or 'accept'),
                    'quality_status': 'manual_approved' if is_manual_source else (quality_by_key.get(text_key, {}).get('quality_status') or 'pending_review'),
                    'quality_score': int(quality_by_key.get(text_key, {}).get('quality_score') or self._score_group_atmosphere_phrase(text, role=role_positioning)),
                    'quality_reasons': list(quality_by_key.get(text_key, {}).get('reasons') or []),
                    'normalized_key': quality_by_key.get(text_key, {}).get('normalized_key') or self._normalize_group_atmosphere_phrase_key(text),
                    'semantic_key': quality_by_key.get(text_key, {}).get('semantic_key') or self._normalize_group_atmosphere_semantic_phrase_key(text),
                    'customized': source_item.get('customized', is_manual_source),
                    'customized_at': source_item.get('customized_at') or (utc_now() if is_manual_source else ''),
                }
                for media_key in ['asset_type', 'media_id', 'media_path', 'media_mime_type', 'media_filename', 'media_preview_url']:
                    if source_item.get(media_key):
                        new_item[media_key] = source_item.get(media_key)
                rebuilt_templates.append(new_item)
                added_items.append(dict(new_item))
                existing_seen_keys.add(text_key)
            templates = rebuilt_templates
            added = len(deduped_phrases)
        else:
            existing_texts = set(existing_by_text.keys())
            added = 0
            for text in phrases:
                text_key = self._normalize_group_atmosphere_semantic_phrase_key(text)
                if not text_key and not is_manual_source:
                    continue
                source_item = dict(source_candidates_by_text.get(text_key) or {}) if text_key else {}
                existing_item = existing_by_text.get(text_key) if text_key else None
                if existing_item is not None:
                    if is_manual_source:
                        existing_item['safe_to_send'] = safe_to_send
                        existing_item['enabled'] = enabled
                        existing_item['quality_status'] = 'manual_approved'
                        existing_item['quality_decision'] = existing_item.get('quality_decision') or 'accept'
                        existing_item['customized'] = True
                        existing_item['customized_at'] = existing_item.get('customized_at') or utc_now()
                        for copy_key in ['text_zh', 'text_zh_source', 'text_zh_status', 'text_zh_updated_at', 'text_zh_failure_reason', 'text_zh_retry_count', 'asset_type', 'media_id', 'media_path', 'media_mime_type', 'media_filename', 'media_preview_url']:
                            if source_item.get(copy_key) and (copy_key not in {'asset_type', 'media_id', 'media_path', 'media_mime_type', 'media_filename', 'media_preview_url'} or not existing_item.get(copy_key)):
                                existing_item[copy_key] = source_item.get(copy_key)
                    else:
                        existing_item['frequency'] = int(existing_item.get('frequency') or 1) + 1
                        existing_item['score'] = max(int(existing_item.get('score') or 0), self._score_group_atmosphere_phrase(text, role=role_positioning))
                    continue
                if text_key:
                    existing_texts.add(text_key)
                translation = dict(source_item) if source_item.get('text_zh') else {'text_zh': '', 'text_zh_source': '', 'text_zh_status': 'needs_translation'}
                new_item = {
                    'template_id': create_id('gatpl'),
                    'candidate_id': create_id('gacand'),
                    'category': role_positioning,
                    'source_role': role_positioning,
                    'source_type': source_type,
                    'text': text,
                    'text_zh': translation.get('text_zh') or '',
                    'text_zh_source': translation.get('text_zh_source') or '',
                    'text_zh_status': translation.get('text_zh_status') or '',
                    'text_zh_updated_at': translation.get('text_zh_updated_at') or '',
                    'text_zh_failure_reason': translation.get('text_zh_failure_reason') or '',
                    'text_zh_retry_count': int(translation.get('text_zh_retry_count') or 0),
                    'score': self._score_group_atmosphere_phrase(text, role=role_positioning),
                    'frequency': 1,
                    'safe_to_send': safe_to_send if is_manual_source else False,
                    'enabled': enabled if is_manual_source else False,
                    'quality_decision': 'accept' if is_manual_source else (quality_by_key.get(text_key, {}).get('decision') or 'accept'),
                    'quality_status': 'manual_approved' if is_manual_source else (quality_by_key.get(text_key, {}).get('quality_status') or 'pending_review'),
                    'quality_score': int(quality_by_key.get(text_key, {}).get('quality_score') or self._score_group_atmosphere_phrase(text, role=role_positioning)),
                    'quality_reasons': list(quality_by_key.get(text_key, {}).get('reasons') or []),
                    'normalized_key': quality_by_key.get(text_key, {}).get('normalized_key') or self._normalize_group_atmosphere_phrase_key(text),
                    'semantic_key': quality_by_key.get(text_key, {}).get('semantic_key') or self._normalize_group_atmosphere_semantic_phrase_key(text),
                    'customized': is_manual_source,
                    'customized_at': utc_now() if is_manual_source else '',
                }
                for media_key in ['asset_type', 'media_id', 'media_path', 'media_mime_type', 'media_filename', 'media_preview_url']:
                    if source_item.get(media_key):
                        new_item[media_key] = source_item.get(media_key)
                templates.append(new_item)
                added_items.append(dict(new_item))
                existing_by_text[text_key] = new_item
                added += 1
        templates = self._sort_group_atmosphere_candidates(self._cap_group_atmosphere_template_pool(templates, limit=100))
        config = self.upsert_group_atmosphere_config(GroupAtmosphereConfigRequest(
            config_name=role_key,
            enabled=False,
            account_key=str((existing or {}).get('account_key') or role_key),
            target_group=str((existing or {}).get('target_group') or role_key),
            group_name=role_name,
            language=language,
            timezone=str((existing or {}).get('timezone') or 'UTC'),
            worker_base_url='',
            daily_max_messages=int((existing or {}).get('daily_max_messages') or 4),
            min_interval_minutes=int((existing or {}).get('min_interval_minutes') or 60),
            max_interval_minutes=int((existing or {}).get('max_interval_minutes') or 240),
            allowed_windows=list((existing or {}).get('allowed_windows') or []),
            template_pool=[GroupAtmosphereTemplate(**item) for item in templates],
            mention_reply_enabled=bool((existing or {}).get('mention_reply_enabled', True)),
            faq_rules=[GroupAtmosphereFaqRule(**item) for item in list((existing or {}).get('faq_rules') or [])],
            status='plan_ready' if any(item.get('enabled') and item.get('safe_to_send') for item in templates) else ('library_only' if templates else 'role_container'),
        ))
        rejected_reasons = sorted({reason for item in rejected_items for reason in list(item.get('reasons') or [])})
        self._schedule_group_atmosphere_translation_preprocess()
        return {'ok': True, 'role': self._group_atmosphere_role_summary(config), 'config': config, 'added_count': added, 'added_items': added_items, 'rejected_count': len(rejected_items), 'rejected_reasons': rejected_reasons, 'rejected_items': rejected_items[:20]}

    def _group_atmosphere_role_summary(self, config: Dict[str, Any]) -> Dict[str, Any]:
        config_name = str((config or {}).get('config_name') or '').strip()
        language = str((config or {}).get('language') or 'id').strip()
        region = self._group_atmosphere_region_from_language(language)
        templates = [dict(item or {}) for item in list((config or {}).get('template_pool') or [])]
        role_templates = [item for item in templates if item.get('role_selected') is True or item.get('role_send_enabled') is True]
        if role_templates:
            role_templates = sorted(
                role_templates,
                key=lambda item: (
                    int(item.get('role_selection_order') or 0),
                    int(item.get('sort_order') or 0),
                    str(item.get('text') or ''),
                ),
            )
        else:
            role_templates = templates
        status = str((config or {}).get('status') or '').strip()
        if status == 'role_type_deleted':
            role_positioning = ''
        else:
            role_positioning = str((role_templates[0].get('source_role') if role_templates else '') or self._group_atmosphere_role_from_key(config_name)).strip()
        role_name = str((config or {}).get('group_name') or '').strip() or self._default_group_atmosphere_plan_display_name(role_positioning, region)
        source_types = sorted({str(item.get('source_type') or 'upload_file').strip() for item in role_templates if item.get('source_type')})
        summary = {
            'role_key': config_name,
            'config_name': config_name,
            'role_name': role_name,
            'plan_display_name': role_name,
            'region': region,
            'language': language,
            'role_positioning': role_positioning,
            'phrase_type': role_positioning,
            'phrase_count': len(role_templates),
            'available_phrase_count': len(templates),
            'enabled_phrase_count': sum(1 for item in role_templates if item.get('enabled') is True and item.get('safe_to_send') is True),
            'source_types': source_types,
            'status': (config or {}).get('status'),
            'updated_at': (config or {}).get('updated_at'),
            'candidates': role_templates,
            'template_pool': role_templates,
        }
        if status == 'role_type_deleted':
            deleted_type = self._group_atmosphere_role_from_key(config_name)
            if deleted_type:
                summary['deleted_phrase_type'] = deleted_type
                summary['phrase_type_deleted'] = True
        return summary

    def _group_atmosphere_available_counts_by_role(self) -> Dict[tuple[str, str], int]:
        counts: Dict[tuple[str, str], int] = {}
        try:
            pool_rows = list(self.list_group_atmosphere_candidate_pool().get('rows') or [])
        except Exception:
            pool_rows = []
        for row in pool_rows:
            language = str(row.get('language') or 'id').strip() or 'id'
            role = str(row.get('role_positioning') or '').strip()
            if not role:
                continue
            counts[(language, role)] = counts.get((language, role), 0) + int(row.get('candidate_count') or len(row.get('candidates') or []) or 0)
        return counts

    def _group_atmosphere_disabled_phrase_type_keys(self) -> set[str]:
        try:
            rows = list(self.list_group_atmosphere_phrase_types(include_disabled=True).get('rows') or [])
        except Exception:
            rows = []
        disabled = {
            str(row.get('type_key') or '').strip()
            for row in rows
            if str(row.get('type_key') or '').strip() and row.get('enabled') is False
        }
        return disabled | set(GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS)

    def _first_enabled_group_atmosphere_phrase_type_key(self) -> str:
        try:
            rows = list(self.list_group_atmosphere_phrase_types().get('rows') or [])
        except Exception:
            rows = []
        for row in rows:
            key = str((row or {}).get('type_key') or '').strip()
            if key and key not in GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS:
                return key
        return ''

    def _resolve_group_atmosphere_phrase_type_key(self, value: str = '', *, required: bool = False) -> str:
        key = str(value or '').strip()
        if key in GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS:
            key = ''
        if key:
            row = self._find_group_atmosphere_phrase_type_by_value(key, include_disabled=True)
            if row:
                key = str(row.get('type_key') or '').strip()
        if not key:
            key = self._first_enabled_group_atmosphere_phrase_type_key()
        if required and not key:
            raise HTTPException(status_code=400, detail='phrase_type_required')
        return key

    def _find_group_atmosphere_phrase_type_by_value(self, value: str, *, include_disabled: bool = False) -> Optional[Dict[str, Any]]:
        key = str(value or '').strip()
        if not key:
            return None
        try:
            rows = list(self.list_group_atmosphere_phrase_types(include_disabled=include_disabled).get('rows') or [])
        except Exception:
            rows = []
        key_lower = key.lower()
        for row in rows:
            row_key = str((row or {}).get('type_key') or '').strip()
            row_name = str((row or {}).get('type_name') or '').strip()
            if row_key and key_lower == row_key.lower():
                return dict(row or {})
            if row_name and key_lower == row_name.lower():
                return dict(row or {})
        return None

    def _ensure_group_atmosphere_phrase_type_key_for_manual_upload(self, value: str = '', *, required: bool = False) -> str:
        raw = str(value or '').strip()
        if raw in GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS:
            raw = ''
        if raw:
            matched = self._find_group_atmosphere_phrase_type_by_value(raw, include_disabled=False)
            if matched and str(matched.get('type_key') or '').strip():
                return str(matched.get('type_key') or '').strip()
            deleted_match = self._find_group_atmosphere_phrase_type_by_value(raw, include_disabled=True)
            if deleted_match and deleted_match.get('enabled') is False:
                raw = str(deleted_match.get('type_name') or raw).strip()
            elif re.fullmatch(r'ptype_[a-z0-9]{8,}', raw.lower()):
                if required:
                    raise HTTPException(status_code=400, detail='phrase_type_not_found')
                raw = ''
            if raw:
                created = self.upsert_group_atmosphere_phrase_type({
                    'type_name': raw,
                    'enabled': True,
                    'created_by': 'manual_upload_template',
                }).get('phrase_type') or {}
                created_key = str(created.get('type_key') or '').strip()
                if created_key:
                    return created_key
        key = self._first_enabled_group_atmosphere_phrase_type_key()
        if required and not key:
            raise HTTPException(status_code=400, detail='phrase_type_required')
        return key

    def _group_atmosphere_should_surface_as_role(self, config: Dict[str, Any]) -> bool:
        name = str((config or {}).get('config_name') or '').strip()
        if not name or name.startswith('deliver-') or name.startswith('binding-'):
            return False
        templates = [dict(item or {}) for item in list((config or {}).get('template_pool') or [])]
        status = str((config or {}).get('status') or '').strip()
        if status == 'candidate_pool' and templates and all(str(item.get('role_deleted_at') or '').strip() for item in templates):
            return False
        has_role_selection = any(item.get('role_selected') is True or item.get('role_send_enabled') is True for item in templates)
        is_explicit_role_config = name.startswith('role-') or (status in {'role_container', 'role_type_deleted'} and not name.startswith('auto-'))
        is_legacy_role_config = status in {'plan_ready', 'library_only'}
        # 兜底：只要模板里还保留“已被角色勾选/装载”的标记，就继续把它当作话术角色展示，
        # 即使 status 被旧逻辑或异常写回成 candidate_pool/disabled，也不能让页面直接消失。
        is_selection_backed_role_config = has_role_selection
        return is_explicit_role_config or is_legacy_role_config or is_selection_backed_role_config

    def list_group_atmosphere_roles(self) -> Dict[str, Any]:
        rows = []
        available_counts = self._group_atmosphere_available_counts_by_role()
        disabled_phrase_type_keys = self._group_atmosphere_disabled_phrase_type_keys()
        for config in self.list_group_atmosphere_configs():
            if not self._group_atmosphere_should_surface_as_role(config):
                continue
            summary = self._group_atmosphere_role_summary(config)
            role_positioning = str(summary.get('role_positioning') or '').strip()
            key = (str(summary.get('language') or 'id').strip() or 'id', role_positioning)
            if role_positioning in disabled_phrase_type_keys:
                summary['deleted_phrase_type'] = role_positioning
                summary['phrase_type_deleted'] = True
                summary['role_positioning'] = ''
                summary['phrase_type'] = ''
            elif available_counts.get(key, 0) > 0:
                summary['available_phrase_count'] = available_counts[key]
            rows.append(summary)
        return {'rows': rows, 'count': len(rows)}

    def _ensure_default_group_atmosphere_phrase_types(self, conn: sqlite3.Connection) -> None:
        if not GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS:
            return
        placeholders = ','.join('?' for _ in GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS)
        conn.execute(
            f"UPDATE whatsapp_group_atmosphere_phrase_types SET enabled=0, is_system=0 WHERE type_key IN ({placeholders})",
            tuple(sorted(GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS)),
        )

    def list_group_atmosphere_phrase_types(self, include_disabled: bool = False) -> Dict[str, Any]:
        with self.db.connect() as conn:
            self._ensure_default_group_atmosphere_phrase_types(conn)
            conn.commit()
            sql = "SELECT * FROM whatsapp_group_atmosphere_phrase_types"
            params: list[Any] = []
            if not include_disabled:
                sql += " WHERE enabled=1"
            sql += " ORDER BY sort_order ASC, type_name ASC, type_key ASC"
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        rows = [row for row in rows if str(row.get('type_key') or '').strip() not in GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS]
        for row in rows:
            try:
                row['region_scope'] = json.loads(row.get('region_scope') or '[]')
            except Exception:
                row['region_scope'] = []
            row['enabled'] = bool(row.get('enabled'))
            row['is_system'] = bool(row.get('is_system'))
        return {'rows': rows, 'count': len(rows)}

    def _find_enabled_group_atmosphere_phrase_type_duplicate_by_name(
        self,
        conn: sqlite3.Connection,
        type_name: str,
        *,
        exclude_key: str = '',
    ) -> Optional[Dict[str, Any]]:
        needle = self._normalize_group_atmosphere_unique_name(type_name)
        if not needle:
            return None
        excluded = str(exclude_key or '').strip()
        rows = conn.execute(
            """
            SELECT *
            FROM whatsapp_group_atmosphere_phrase_types
            WHERE enabled=1
              AND (? = '' OR type_key <> ?)
            ORDER BY sort_order ASC, type_name ASC, type_key ASC
            """,
            (excluded, excluded),
        ).fetchall()
        for row in rows:
            item = dict(row)
            if str(item.get('type_key') or '').strip() in GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS:
                continue
            if self._normalize_group_atmosphere_unique_name(str(item.get('type_name') or '')) == needle:
                return item
        return None

    @staticmethod
    def _group_atmosphere_template_uses_phrase_type(item: Dict[str, Any], type_key: str) -> bool:
        key = str(type_key or '').strip()
        if not key or not isinstance(item, dict):
            return False
        for field in ('phrase_type', 'role_positioning', 'source_role', 'category'):
            if str(item.get(field) or '').strip() == key:
                return True
        return False

    def upsert_group_atmosphere_phrase_type(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_key = str((payload or {}).get('type_key') or '').strip()
        type_name = str((payload or {}).get('type_name') or (payload or {}).get('name') or '').strip()
        if not type_name:
            raise HTTPException(status_code=400, detail='type_name_required')
        if not raw_key:
            raw_key = re.sub(r'[^a-z0-9]+', '_', type_name.lower()).strip('_') or create_id('ptype')
        type_key = re.sub(r'[^a-z0-9_]+', '_', raw_key.lower()).strip('_')
        if not type_key:
            raise HTTPException(status_code=400, detail='type_key_required')
        if type_key in GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS:
            raise HTTPException(status_code=400, detail='legacy_default_phrase_type_disabled')
        now = utc_now()
        region_scope = [str(x or '').strip() for x in list((payload or {}).get('region_scope') or []) if str(x or '').strip()]
        with self.db.connect() as conn:
            self._ensure_default_group_atmosphere_phrase_types(conn)
            existing = conn.execute("SELECT * FROM whatsapp_group_atmosphere_phrase_types WHERE type_key=?", (type_key,)).fetchone()
            if self._find_enabled_group_atmosphere_phrase_type_duplicate_by_name(conn, type_name, exclude_key=type_key):
                raise HTTPException(status_code=409, detail='phrase_type_name_duplicate')
            is_system = bool(existing['is_system']) if existing else bool((payload or {}).get('is_system'))
            if 'sort_order' in (payload or {}) and str((payload or {}).get('sort_order') or '').strip():
                sort_order = int((payload or {}).get('sort_order') or 0)
            elif existing:
                sort_order = int(existing['sort_order'] or 100)
            else:
                max_sort = conn.execute("SELECT COALESCE(MAX(sort_order), 40) FROM whatsapp_group_atmosphere_phrase_types WHERE enabled=1").fetchone()[0]
                sort_order = int(max_sort or 40) + 10
            conn.execute(
                """INSERT INTO whatsapp_group_atmosphere_phrase_types
                (type_key, type_name, description, enabled, is_system, sort_order, region_scope, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(type_key) DO UPDATE SET type_name=excluded.type_name, description=excluded.description, enabled=excluded.enabled, sort_order=excluded.sort_order, region_scope=excluded.region_scope, updated_at=excluded.updated_at""",
                (type_key, type_name, str((payload or {}).get('description') or '').strip(), 0 if (payload or {}).get('enabled') is False else 1, 1 if is_system else 0, sort_order, json.dumps(region_scope, ensure_ascii=False), str((payload or {}).get('created_by') or '').strip(), now, now),
            )
            conn.commit()
        rows = self.list_group_atmosphere_phrase_types(include_disabled=True)['rows']
        row = next((item for item in rows if item.get('type_key') == type_key), None)
        return {'ok': True, 'phrase_type': row}

    def rename_group_atmosphere_phrase_type(self, type_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        key = re.sub(r'[^a-z0-9_]+', '_', str(type_key or '').strip().lower()).strip('_')
        type_name = str((payload or {}).get('type_name') or (payload or {}).get('name') or '').strip()
        if not key:
            raise HTTPException(status_code=400, detail='type_key_required')
        if key in GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS:
            raise HTTPException(status_code=400, detail='legacy_default_phrase_type_disabled')
        if not type_name:
            raise HTTPException(status_code=400, detail='type_name_required')
        with self.db.connect() as conn:
            self._ensure_default_group_atmosphere_phrase_types(conn)
            row = conn.execute("SELECT * FROM whatsapp_group_atmosphere_phrase_types WHERE type_key=?", (key,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='phrase_type_not_found')
            if bool(row['is_system']):
                raise HTTPException(status_code=400, detail='system_phrase_type_cannot_rename')
            if self._find_enabled_group_atmosphere_phrase_type_duplicate_by_name(conn, type_name, exclude_key=key):
                raise HTTPException(status_code=409, detail='phrase_type_name_duplicate')
            now = utc_now()
            conn.execute("UPDATE whatsapp_group_atmosphere_phrase_types SET type_name=?, updated_at=? WHERE type_key=?", (type_name, now, key))
            conn.commit()
        rows = self.list_group_atmosphere_phrase_types(include_disabled=True)['rows']
        renamed = next((item for item in rows if item.get('type_key') == key), None)
        return {'ok': True, 'phrase_type': renamed}

    def group_atmosphere_role_usage(self, role_key: str) -> Dict[str, Any]:
        key = str(role_key or '').strip()
        relationships = []
        binding_count = 0
        try:
            binding_payload = self.list_group_atmosphere_role_bindings()
            for rel in list(binding_payload.get('relationships') or []):
                if str(rel.get('role_key') or '') != key:
                    continue
                groups = list(rel.get('groups') or [])
                binding_count += len(groups)
                relationships.append({
                    'relationship_key': rel.get('relationship_key') or rel.get('role_key') or key,
                    'relationship_label': rel.get('relationship_label') or rel.get('role_name') or key,
                    'role_key': rel.get('role_key') or key,
                    'role_name': rel.get('role_name') or key,
                    'group_count': len(groups),
                    'groups': [{
                        'binding_id': group.get('binding_id'),
                        'group_name': group.get('group_name'),
                        'target_group': group.get('target_group'),
                        'account_key': group.get('account_key'),
                    } for group in groups],
                })
        except Exception:
            relationships = []
            binding_count = 0
        return {
            'ok': True,
            'role_key': key,
            'bridge_relationships': relationships,
            'bridge_relationship_count': len(relationships),
            'binding_count': binding_count,
            'in_use': bool(relationships),
        }

    def group_atmosphere_phrase_type_usage(self, type_key: str) -> Dict[str, Any]:
        key = re.sub(r'[^a-z0-9_]+', '_', str(type_key or '').strip().lower()).strip('_')
        roles = []
        role_keys: set[str] = set()
        try:
            for role in list(self.list_group_atmosphere_roles().get('rows') or []):
                role_type = str(role.get('role_positioning') or role.get('deleted_phrase_type') or role.get('phrase_type') or '').strip()
                if role_type != key:
                    continue
                role_key = str(role.get('role_key') or role.get('config_name') or '').strip()
                if role_key:
                    role_keys.add(role_key)
                roles.append({
                    'role_key': role_key,
                    'role_name': role.get('role_name') or role.get('plan_display_name') or role_key,
                    'region': role.get('region'),
                    'language': role.get('language'),
                    'loaded_phrase_count': int(role.get('enabled_phrase_count') or role.get('phrase_count') or 0),
                })
        except Exception:
            roles = []
            role_keys = set()
        relationships = []
        binding_count = 0
        try:
            binding_payload = self.list_group_atmosphere_role_bindings()
            for rel in list(binding_payload.get('relationships') or []):
                rel_role_key = str(rel.get('role_key') or '').strip()
                rel_type = str(rel.get('role_positioning') or '').strip()
                if rel_role_key not in role_keys and rel_type != key:
                    continue
                groups = list(rel.get('groups') or [])
                binding_count += len(groups)
                relationships.append({
                    'relationship_key': rel.get('relationship_key') or rel_role_key,
                    'relationship_label': rel.get('relationship_label') or rel.get('role_name') or rel_role_key,
                    'role_key': rel_role_key,
                    'role_name': rel.get('role_name') or rel_role_key,
                    'group_count': len(groups),
                    'groups': [{
                        'binding_id': group.get('binding_id'),
                        'group_name': group.get('group_name'),
                        'target_group': group.get('target_group'),
                        'account_key': group.get('account_key'),
                    } for group in groups],
                })
        except Exception:
            relationships = []
            binding_count = 0
        return {
            'ok': True,
            'type_key': key,
            'roles': roles,
            'role_count': len(roles),
            'bridge_relationships': relationships,
            'bridge_relationship_count': len(relationships),
            'binding_count': binding_count,
            'in_use': bool(roles or relationships),
        }

    def delete_group_atmosphere_phrase_type(self, type_key: str) -> Dict[str, Any]:
        key = re.sub(r'[^a-z0-9_]+', '_', str(type_key or '').strip().lower()).strip('_')
        if not key:
            raise HTTPException(status_code=400, detail='type_key_required')
        if key in GROUP_ATMOSPHERE_LEGACY_DEFAULT_PHRASE_TYPE_KEYS:
            raise HTTPException(status_code=400, detail='legacy_default_phrase_type_disabled')
        with self.db.connect() as conn:
            self._ensure_default_group_atmosphere_phrase_types(conn)
            conn.commit()
            row = conn.execute("SELECT * FROM whatsapp_group_atmosphere_phrase_types WHERE type_key=?", (key,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='phrase_type_not_found')
            if bool(row['is_system']):
                raise HTTPException(status_code=400, detail='system_phrase_type_cannot_delete')
            usage = self.group_atmosphere_phrase_type_usage(key)
            deleted_type = dict(row)
            try:
                deleted_type['region_scope'] = json.loads(deleted_type.get('region_scope') or '[]')
            except Exception:
                deleted_type['region_scope'] = []
            deleted_type['enabled'] = False
            deleted_type['is_system'] = bool(deleted_type.get('is_system'))
            deleted_type['hard_deleted'] = True
            now = utc_now()
            removed_template_count = 0
            updated_config_count = 0
            deleted_config_count = 0
            config_rows = conn.execute(
                "SELECT config_name, language, status, template_pool FROM whatsapp_group_atmosphere_configs ORDER BY config_name ASC"
            ).fetchall()
            for config_row in config_rows:
                config_name = str(config_row['config_name'] or '').strip()
                status = str(config_row['status'] or '').strip()
                language = str(config_row['language'] or '').strip()
                try:
                    templates = json.loads(config_row['template_pool'] or '[]')
                except Exception:
                    templates = []
                if not isinstance(templates, list):
                    templates = []
                kept_templates: List[Dict[str, Any]] = []
                removed_count = 0
                for raw_item in templates:
                    if not isinstance(raw_item, dict):
                        continue
                    item = dict(raw_item)
                    if self._group_atmosphere_template_uses_phrase_type(item, key):
                        removed_count += 1
                        continue
                    kept_templates.append(item)
                if not removed_count:
                    continue
                removed_template_count += removed_count
                config_kind = self._group_atmosphere_config_kind(config_name, status)
                if not kept_templates and config_kind == 'candidate_pool':
                    conn.execute("DELETE FROM whatsapp_group_atmosphere_configs WHERE config_name=?", (config_name,))
                    conn.execute("DELETE FROM whatsapp_group_atmosphere_candidates WHERE config_name=?", (config_name,))
                    deleted_config_count += 1
                    continue
                next_status = status
                if not kept_templates and config_kind == 'speech_role':
                    next_status = 'role_type_deleted'
                conn.execute(
                    "UPDATE whatsapp_group_atmosphere_configs SET template_pool=?, status=?, updated_at=? WHERE config_name=?",
                    (json.dumps(kept_templates, ensure_ascii=False), next_status, now, config_name),
                )
                self._sync_group_atmosphere_candidates_from_config(conn, config_name, kept_templates, language=language)
                updated_config_count += 1
            cursor = conn.execute("DELETE FROM whatsapp_group_atmosphere_candidates WHERE role_positioning=?", (key,))
            deleted_candidate_count = max(0, int(cursor.rowcount or 0))
            for candidate_row in conn.execute(
                "SELECT config_name, candidate_id, payload_json FROM whatsapp_group_atmosphere_candidates"
            ).fetchall():
                try:
                    payload = json.loads(candidate_row['payload_json'] or '{}')
                except Exception:
                    payload = {}
                if not isinstance(payload, dict) or not self._group_atmosphere_template_uses_phrase_type(payload, key):
                    continue
                conn.execute(
                    "DELETE FROM whatsapp_group_atmosphere_candidates WHERE config_name=? AND candidate_id=?",
                    (str(candidate_row['config_name'] or '').strip(), str(candidate_row['candidate_id'] or '').strip()),
                )
                deleted_candidate_count += 1
            conn.execute("DELETE FROM whatsapp_group_atmosphere_phrase_types WHERE type_key=?", (key,))
            conn.commit()
        return {
            'ok': True,
            'deleted': True,
            'hard_deleted': True,
            'phrase_type': deleted_type,
            'usage': usage,
            'removed_template_count': removed_template_count,
            'updated_config_count': updated_config_count,
            'deleted_config_count': deleted_config_count,
            'deleted_candidate_count': deleted_candidate_count,
        }

    def _group_atmosphere_media_row_to_dict(self, row: Any) -> Dict[str, Any]:
        item = dict(row)
        item['preview_url'] = f"/api/ops/group-atmosphere/media-assets/{item.get('media_id')}/preview"
        return item

    def list_group_atmosphere_media_assets(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            rows = [self._group_atmosphere_media_row_to_dict(row) for row in conn.execute(
                "SELECT * FROM whatsapp_group_atmosphere_media_assets ORDER BY created_at DESC"
            ).fetchall()]
        return {'ok': True, 'rows': rows}

    def get_group_atmosphere_media_asset(self, media_id: str) -> Dict[str, Any]:
        key = str(media_id or '').strip()
        if not key:
            raise HTTPException(status_code=400, detail='media_id_required')
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM whatsapp_group_atmosphere_media_assets WHERE media_id = ?", (key,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='media_not_found')
        return self._group_atmosphere_media_row_to_dict(row)

    def create_group_atmosphere_media_asset(self, filename: str, content: bytes, mime_type: str = '', created_by: str = '') -> Dict[str, Any]:
        raw = bytes(content or b'')
        if not raw:
            raise HTTPException(status_code=400, detail='media_file_required')
        if len(raw) > GROUP_ATMOSPHERE_MEDIA_UPLOAD_MAX_BYTES:
            raise HTTPException(status_code=400, detail='media_file_too_large')
        original_name = Path(str(filename or 'image')).name or 'image'
        suffix = Path(original_name).suffix.lower()
        guessed_mime = str(mime_type or '').split(';', 1)[0].strip().lower()
        ext_mime = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.webp': 'image/webp',
        }
        if not guessed_mime or guessed_mime == 'application/octet-stream':
            guessed_mime = ext_mime.get(suffix, '')
        if guessed_mime not in {'image/jpeg', 'image/png', 'image/webp'} or suffix not in {'.jpg', '.jpeg', '.png', '.webp'}:
            raise HTTPException(status_code=400, detail='unsupported_media_type')
        digest = hashlib.sha256(raw).hexdigest()
        with self.db.connect() as conn:
            existing = conn.execute("SELECT * FROM whatsapp_group_atmosphere_media_assets WHERE sha256 = ?", (digest,)).fetchone()
            if existing:
                return {'ok': True, 'media': self._group_atmosphere_media_row_to_dict(existing), 'deduped': True}
            media_id = create_id('gamedia')
            stored_name = f'{digest[:24]}{suffix}'
            media_path = str((self.group_atmosphere_media_dir / stored_name).resolve())
            self.group_atmosphere_media_dir.mkdir(parents=True, exist_ok=True)
            Path(media_path).write_bytes(raw)
            now = utc_now()
            conn.execute(
                """
                INSERT INTO whatsapp_group_atmosphere_media_assets
                    (media_id, filename, media_path, mime_type, file_size, sha256, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (media_id, original_name, media_path, guessed_mime, len(raw), digest, str(created_by or ''), now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM whatsapp_group_atmosphere_media_assets WHERE media_id = ?", (media_id,)).fetchone()
        return {'ok': True, 'media': self._group_atmosphere_media_row_to_dict(row), 'deduped': False}

    def manual_upload_group_atmosphere_phrases(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # 人工上传/图片话术只写入“话术类型/候选池”，不能创建或升级为话术角色。
        # 话术角色只能通过“新增话术角色”按钮走 /roles/manual-phrases 产生 role-* 容器。
        requested_key = str((payload or {}).get('config_name') or (payload or {}).get('role_key') or '').strip()
        role_positioning_for_key = self._ensure_group_atmosphere_phrase_type_key_for_manual_upload(str((payload or {}).get('phrase_type') or (payload or {}).get('role_positioning') or self._group_atmosphere_phrase_type_from_config(requested_key) or ''), required=True)
        language_for_key = str((payload or {}).get('language') or self._group_atmosphere_language_from_region(str((payload or {}).get('region') or '印尼'))).strip() or 'id'
        region_for_key = str((payload or {}).get('region') or self._group_atmosphere_region_from_language(language_for_key) or '印尼').strip() or '印尼'
        # 人工上传/图片话术必须落到 auto-* 候选池；即使旧前端/旧测试误传 role-*，也不能写进话术角色容器。
        if requested_key.startswith('auto-'):
            role_key = requested_key
        else:
            role_key = f"auto-{language_for_key}-{role_positioning_for_key}"
        content = str((payload or {}).get('content') or (payload or {}).get('text') or '').strip()
        default_media_id = str((payload or {}).get('media_id') or '').strip()
        media_cache: Dict[str, Dict[str, Any]] = {}
        if default_media_id:
            media_cache[default_media_id] = self.get_group_atmosphere_media_asset(default_media_id)
        raw_phrase_entries: List[Dict[str, str]] = []
        for phrase in list((payload or {}).get('phrases') or []):
            if isinstance(phrase, dict):
                text = str(
                    phrase.get('text')
                    or phrase.get('phrase')
                    or phrase.get('message')
                    or phrase.get('content')
                    or ''
                ).strip()
                phrase_media_id = str(phrase.get('media_id') or default_media_id or '').strip()
                if text or phrase_media_id:
                    raw_phrase_entries.append({'text': text, 'media_id': phrase_media_id})
            else:
                text = str(phrase or '').strip()
                if text or default_media_id:
                    raw_phrase_entries.append({'text': text, 'media_id': default_media_id})
        if content:
            raw_phrase_entries.extend([{'text': line.strip(), 'media_id': default_media_id} for line in content.splitlines() if line.strip()])
        # 人工上传：只做空行/完全重复去重，不做 AI 学习、过滤、润色。
        phrases: List[Dict[str, str]] = []
        seen = set()
        duplicate_count = 0
        for entry in raw_phrase_entries:
            text = str((entry or {}).get('text') or '').strip()
            phrase_media_id = str((entry or {}).get('media_id') or '').strip()
            if not text and not phrase_media_id:
                continue
            dedupe_key = (text, phrase_media_id)
            if dedupe_key in seen:
                duplicate_count += 1
                continue
            seen.add(dedupe_key)
            phrases.append({'text': text, 'media_id': phrase_media_id})
        if not phrases and default_media_id:
            phrases = [{'text': '', 'media_id': default_media_id}]
        if not phrases:
            raise HTTPException(status_code=400, detail='phrases_required')
        enriched = []
        role_positioning = role_positioning_for_key
        for entry in phrases:
            text = str((entry or {}).get('text') or '').strip()
            phrase_media_id = str((entry or {}).get('media_id') or '').strip()
            media: Optional[Dict[str, Any]] = None
            if phrase_media_id:
                if phrase_media_id not in media_cache:
                    media_cache[phrase_media_id] = self.get_group_atmosphere_media_asset(phrase_media_id)
                media = media_cache.get(phrase_media_id)
            enriched.append({
                'template_id': create_id('gatpl'),
                'candidate_id': create_id('gacand'),
                'category': role_positioning,
                'source_role': role_positioning,
                'role_positioning': role_positioning,
                'phrase_type': role_positioning,
                'language': language_for_key,
                'region': region_for_key,
                'source_type': 'manual_upload',
                'text': text,
                'text_zh': '',
                'score': 100,
                'frequency': 1,
                'safe_to_send': True,
                'enabled': True,
                'quality_decision': 'accept',
                'quality_status': str((payload or {}).get('quality_status') or 'approved_manual'),
                'quality_score': 100,
                'quality_reasons': [],
                'normalized_key': self._normalize_group_atmosphere_phrase_key(text),
                'semantic_key': self._normalize_group_atmosphere_semantic_phrase_key(text),
                'customized': True,
                'customized_at': utc_now(),
                'asset_type': 'image_caption' if media else 'text',
                'media_id': media.get('media_id') if media else None,
                'media_path': media.get('media_path') if media else None,
                'media_mime_type': media.get('mime_type') if media else None,
                'media_filename': media.get('filename') if media else None,
            })
        existing = self._get_group_atmosphere_config(role_key)
        templates = [] if (payload or {}).get('replace_phrases') else [dict(item or {}) for item in list((existing or {}).get('template_pool') or [])]
        templates.extend(enriched)
        config = self.upsert_group_atmosphere_config(GroupAtmosphereConfigRequest(
            config_name=role_key,
            enabled=False,
            account_key=str((existing or {}).get('account_key') or role_key),
            target_group=str((existing or {}).get('target_group') or role_key),
            group_name=str((payload or {}).get('role_name') or (existing or {}).get('group_name') or self._default_group_atmosphere_plan_display_name(role_positioning, region_for_key) or role_key),
            language=language_for_key,
            timezone=str((existing or {}).get('timezone') or 'UTC'),
            worker_base_url='',
            daily_max_messages=int((existing or {}).get('daily_max_messages') or 4),
            min_interval_minutes=int((existing or {}).get('min_interval_minutes') or 60),
            max_interval_minutes=int((existing or {}).get('max_interval_minutes') or 240),
            allowed_windows=list((existing or {}).get('allowed_windows') or []),
            template_pool=[GroupAtmosphereTemplate(**item) for item in templates],
            mention_reply_enabled=bool((existing or {}).get('mention_reply_enabled', True)),
            faq_rules=[GroupAtmosphereFaqRule(**item) for item in list((existing or {}).get('faq_rules') or [])],
            status='candidate_pool',
        ))
        return {'ok': True, 'config': config, 'config_name': config.get('config_name'), 'imported_count': len(enriched), 'duplicate_count': duplicate_count, 'review_required': bool((payload or {}).get('review_required', False)), 'source_type': 'manual_upload'}

    @staticmethod
    def _manual_upload_review_dedupe_key(text: str) -> str:
        value = str(text or '').strip().lower()
        value = re.sub(r'[\u200e\u200f\ufeff]', '', value)
        value = re.sub(r'[!！?？。.,，、;；:：~～\-—–_"“”\'‘’`]+$', '', value)
        value = re.sub(r'\s+', ' ', value)
        return value.strip()

    @staticmethod
    def _guess_group_atmosphere_manual_upload_language(text: str, fallback: str = 'id') -> str:
        value = f" {str(text or '').lower()} "
        if not value.strip():
            return str(fallback or 'unknown') or 'unknown'
        es_patterns = [
            r'\b(?:si|el|la|los|las|un|una|como|cuando|donde|para|por|que|quien|quién|pueden|puede|tiene|tienes|estamos|aquí|aqui)\b',
            r'\b(?:c[oó]digo|inv[aá]lido|env[ií]anos|captura|pantalla|error|revisamos|secci[oó]n|correcta|incidencia|sistema)\b',
            r'\b(?:completar|perfil|foto|nombre|descripci[oó]n|llamativa|datos|b[aá]sicos|publicaciones|termines|manda|administradora|falta)\b',
            r'\b(?:hoy|vamos|refrescar|cambien|imagen|actualizado|usuarios|interesen|respondan|mensajes)\b',
            r'\b(?:chicas|hola|amiga|amigo|comparte|experiencia|dudas|preguntar|pena|descarga|agencia|diamantes|retiro|guiarlas|paso a paso|tranquila|r[aá]pido|oportunidad)\b',
            r'[¿¡]|[áéíóúñ]',
        ]
        pt_patterns = [
            r'\b(?:oi|ol[aá]|tudo bem|voc[eê]|obrigad[ao]|cadastro|d[uú]vida|b[oô]nus|saque|ganhar|mensagem|perfil)\b',
            r'\b(?:meninas|perguntar|sem vergonha|passo a passo|captura|tela|erro|sistema|responder|oportunidade)\b',
            r'\b(?:você|vocês|est[aá]|n[aã]o|tamb[eé]m|atenç[aã]o|descriç[aã]o|c[oó]digo)\b',
            r'[ãõç]',
        ]
        id_patterns = [
            r'\b(?:kak|admin|grup|daftar|aktif|tanya|cerita|jangan|kalau|boleh|selamat|akun|pesan|penarikan|berlian)\b',
            r'\b(?:kode|agensi|profil|foto|balas|cepat|gabung|resmi|linky)\b',
        ]
        def score(patterns):
            return sum(len(re.findall(pattern, value)) for pattern in patterns)
        scores = {'es': score(es_patterns), 'pt': score(pt_patterns), 'id': score(id_patterns)}
        if scores['es'] >= 2 and scores['es'] >= scores['pt'] and scores['es'] >= scores['id']:
            return 'es'
        if scores['pt'] >= 2 and scores['pt'] > scores['es'] and scores['pt'] >= scores['id']:
            return 'pt'
        if scores['id'] >= 2 and scores['id'] > scores['es'] and scores['id'] > scores['pt']:
            return 'id'
        if scores['es'] >= 1 and (re.search(r'[¿¡]|[áéíóúñ]', value) or scores['es'] > scores['id']):
            return 'es'
        if scores['pt'] >= 1 and (re.search(r'[ãõç]', value) or scores['pt'] > scores['id']):
            return 'pt'
        if scores['id'] >= 1:
            return 'id'
        return str(fallback or 'unknown') or 'unknown'

    def _existing_group_atmosphere_manual_upload_keys(self) -> set:
        keys = set()
        try:
            rows = self.list_group_atmosphere_candidate_pool().get('rows', [])
        except Exception:
            rows = []
        for row in rows:
            for item in list(row.get('candidates') or []):
                key = self._manual_upload_review_dedupe_key(str(item.get('text') or ''))
                if key:
                    keys.add(key)
        return keys

    def preview_manual_upload_group_atmosphere_phrases(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        content = str((payload or {}).get('content') or (payload or {}).get('text') or '')
        raw_entries: List[Dict[str, Any]] = []
        for phrase in list((payload or {}).get('phrases') or []):
            if isinstance(phrase, dict):
                text = str(
                    phrase.get('text')
                    or phrase.get('phrase')
                    or phrase.get('message')
                    or phrase.get('content')
                    or ''
                ).strip()
                if text:
                    raw_entries.append({**phrase, 'text': text})
            else:
                text = str(phrase or '').strip()
                if text:
                    raw_entries.append({'text': text})
        raw_entries.extend([{'text': line.strip()} for line in content.splitlines() if line.strip()])
        default_region = str((payload or {}).get('region') or '').strip() or '未识别'
        default_language = str((payload or {}).get('language') or (self._group_atmosphere_language_from_region(default_region) if default_region != '未识别' else 'unknown') or 'unknown')
        has_entry_role = any(str(entry.get('role_positioning') or entry.get('phrase_type') or entry.get('type') or entry.get('category') or '').strip() for entry in raw_entries)
        default_role = self._resolve_group_atmosphere_phrase_type_key(str((payload or {}).get('role_positioning') or ''), required=not has_entry_role)
        existing_keys = self._existing_group_atmosphere_manual_upload_keys()
        seen = set()
        items: List[Dict[str, Any]] = []
        duplicates: List[Dict[str, Any]] = []
        invalid_items: List[Dict[str, Any]] = []
        for index, entry in enumerate(raw_entries):
            clean = str((entry or {}).get('text') or '').strip()
            entry_region = str((entry or {}).get('region') or (entry or {}).get('area') or (entry or {}).get('country') or '').strip()
            entry_language = str((entry or {}).get('language') or '').strip()
            entry_role = str((entry or {}).get('role_positioning') or (entry or {}).get('phrase_type') or (entry or {}).get('type') or (entry or {}).get('category') or '').strip()
            item_default_region = entry_region or default_region
            item_default_language = entry_language or (
                self._group_atmosphere_language_from_region(item_default_region)
                if item_default_region != '未识别'
                else default_language
            )
            if entry_role:
                matched_role = self._find_group_atmosphere_phrase_type_by_value(entry_role, include_disabled=False)
                item_role = str((matched_role or {}).get('type_key') or entry_role).strip()
            else:
                item_role = self._resolve_group_atmosphere_phrase_type_key(default_role, required=True)
            key = self._manual_upload_review_dedupe_key(clean)
            if not clean or len(clean) < 4 or re.fullmatch(r'[\d\s:.,/\-+()]+', clean):
                invalid_items.append({'text': clean, 'reason': 'invalid_content', 'row_index': index})
                continue
            if re.search(r'[\u4e00-\u9fff]', clean):
                invalid_items.append({'text': clean, 'reason': 'cjk_non_target_language', 'row_index': index})
                continue
            if key in seen:
                duplicates.append({'text': clean, 'duplicate_status': 'batch_duplicate', 'row_index': index})
                continue
            seen.add(key)
            if key in existing_keys:
                duplicates.append({'text': clean, 'duplicate_status': 'existing', 'row_index': index})
                continue
            explicit_region = bool(entry_region)
            guessed_lang = self._guess_group_atmosphere_manual_upload_language(clean, item_default_language)
            if explicit_region and item_default_region != '未识别':
                region = item_default_region
                lang = entry_language or self._group_atmosphere_language_from_region(region) or guessed_lang
            else:
                lang = guessed_lang
                region = item_default_region if lang == item_default_language and item_default_region != '未识别' else self._group_atmosphere_region_from_language(lang)
                if region == '未知':
                    region = item_default_region if lang == item_default_language else '未识别'
            item = {
                'draft_id': create_id('gaup'),
                'row_index': index,
                'text': clean,
                'language': lang,
                'region': region,
                'role_positioning': item_role,
                'duplicate_status': 'new',
                'duplicate_reason': '',
                'selected': lang != 'unknown',
            }
            media_id = str((entry or {}).get('media_id') or '').strip()
            if media_id:
                item.update({
                    'asset_type': str((entry or {}).get('asset_type') or 'image_caption'),
                    'media_id': media_id,
                    'media_path': str((entry or {}).get('media_path') or ''),
                    'media_mime_type': str((entry or {}).get('media_mime_type') or (entry or {}).get('mime_type') or ''),
                    'media_filename': str((entry or {}).get('media_filename') or (entry or {}).get('filename') or ''),
                    'media_preview_url': str((entry or {}).get('media_preview_url') or (entry or {}).get('preview_url') or f'/api/ops/group-atmosphere/media-assets/{media_id}/preview'),
                })
            items.append(item)
        language_groups = Counter(str(item.get('language') or 'unknown') for item in items)
        role_groups = Counter(str(item.get('role_positioning') or default_role) for item in items)
        return {
            'ok': True,
            'review_required': True,
            'items': items,
            'duplicates': duplicates,
            'invalid_items': invalid_items,
            'summary': {
                'total': len(raw_entries),
                'new_count': len(items),
                'duplicate_count': len(duplicates),
                'existing_duplicate_count': len([d for d in duplicates if d.get('duplicate_status') == 'existing']),
                'invalid_count': len(invalid_items),
                'unknown_language_count': len([item for item in items if item.get('language') == 'unknown']),
                'language_groups': dict(language_groups),
                'role_groups': dict(role_groups),
            },
        }

    def confirm_manual_upload_group_atmosphere_phrases(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        selected_items = [dict(item or {}) for item in list((payload or {}).get('items') or []) if (item or {}).get('selected', True) is not False and str((item or {}).get('text') or '').strip()]
        for item in selected_items:
            text = str(item.get('text') or '').strip()
            original_text = str(item.get('original_text') or '').strip()
            if original_text and re.search(r'[\u4e00-\u9fff]', text) and not re.search(r'[\u4e00-\u9fff]', original_text):
                item['translated_text'] = text
                item['text'] = original_text
            if not str(item.get('language') or '').strip():
                item['language'] = self._guess_group_atmosphere_manual_upload_language(str(item.get('text') or ''), 'id')
            if not str(item.get('region') or '').strip():
                item['region'] = self._group_atmosphere_region_from_language(str(item.get('language') or '')) or '印尼'
        if not selected_items:
            raise HTTPException(status_code=400, detail='review_items_required')
        grouped: Dict[tuple, List[Dict[str, Any]]] = {}
        for item in selected_items:
            language = str(item.get('language') or self._group_atmosphere_language_from_region(str(item.get('region') or '印尼')) or 'id')
            region = str(item.get('region') or self._group_atmosphere_region_from_language(language) or '印尼')
            role = self._ensure_group_atmosphere_phrase_type_key_for_manual_upload(str(item.get('role_positioning') or ''), required=True)
            grouped.setdefault((language, region, role), []).append({
                'text': str(item.get('text') or '').strip(),
                'media_id': str(item.get('media_id') or '').strip(),
            })
        configs = []
        imported = 0
        duplicate = 0
        for (language, region, role), phrases in grouped.items():
            result = self.manual_upload_group_atmosphere_phrases({
                'region': region,
                'language': language,
                'role_positioning': role,
                'role_name': self._default_group_atmosphere_plan_display_name(role, region),
                'phrases': phrases,
                'quality_status': 'manual_approved',
                'review_required': True,
            })
            imported += int(result.get('imported_count') or 0)
            duplicate += int(result.get('duplicate_count') or 0)
            configs.append({'config_name': result.get('config_name'), 'language': language, 'region': region, 'role_positioning': role, 'imported_count': result.get('imported_count')})
        return {'ok': True, 'review_required': True, 'imported_count': imported, 'duplicate_count': duplicate, 'configs': configs, 'source_type': 'manual_upload'}

    def move_group_atmosphere_phrases(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        source_key = str((payload or {}).get('source_role_key') or (payload or {}).get('source_config_name') or '').strip()
        target_key = str((payload or {}).get('target_role_key') or (payload or {}).get('target_config_name') or '').strip()
        mode = str((payload or {}).get('mode') or 'move').strip()
        template_ids = {str(x or '').strip() for x in list((payload or {}).get('template_ids') or []) if str(x or '').strip()}
        if not source_key or not target_key or not template_ids:
            raise HTTPException(status_code=400, detail='source_target_template_ids_required')
        source = self._get_group_atmosphere_config(source_key)
        target = self._get_group_atmosphere_config(target_key)
        if not source or not target:
            raise HTTPException(status_code=404, detail='role_not_found')
        source_templates = [dict(item or {}) for item in list(source.get('template_pool') or [])]
        target_templates = [dict(item or {}) for item in list(target.get('template_pool') or [])]
        moved = []
        now = utc_now()
        for item in source_templates:
            if str(item.get('template_id') or '') not in template_ids:
                continue
            copied = dict(item)
            copied['template_id'] = create_id('gatpl')
            copied['candidate_id'] = create_id('gacand')
            copied['moved_from_role_key'] = source_key
            copied['moved_from_template_id'] = item.get('template_id')
            copied['moved_at'] = now
            copied['enabled'] = True
            copied['safe_to_send'] = True
            target_templates.append(copied)
            moved.append(copied)
            if mode == 'move':
                item['enabled'] = False
                item['safe_to_send'] = False
                item['moved_to_role_key'] = target_key
                item['moved_at'] = now
        if not moved:
            raise HTTPException(status_code=404, detail='phrase_not_found')
        self._replace_group_atmosphere_config_templates(source, source_templates)
        self._replace_group_atmosphere_config_templates(target, target_templates)
        return {'ok': True, 'mode': mode, 'moved_count': len(moved), 'phrases': moved}

    def _replace_group_atmosphere_config_templates(self, config: Dict[str, Any], templates: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.upsert_group_atmosphere_config(GroupAtmosphereConfigRequest(
            config_name=str(config.get('config_name')),
            enabled=bool(config.get('enabled')),
            account_key=str(config.get('account_key')),
            target_group=str(config.get('target_group')),
            group_name=str(config.get('group_name') or ''),
            language=str(config.get('language') or 'id'),
            timezone=str(config.get('timezone') or 'UTC'),
            worker_base_url=str(config.get('worker_base_url') or ''),
            daily_max_messages=int(config.get('daily_max_messages') if config.get('daily_max_messages') is not None else 0),
            min_interval_minutes=int(config.get('min_interval_minutes') or 60),
            max_interval_minutes=int(config.get('max_interval_minutes') or 240),
            allowed_windows=list(config.get('allowed_windows') or []),
            template_pool=[GroupAtmosphereTemplate(**item) for item in templates],
            mention_reply_enabled=bool(config.get('mention_reply_enabled', True)),
            faq_rules=[GroupAtmosphereFaqRule(**item) for item in list(config.get('faq_rules') or [])],
            status=str(config.get('status') or 'plan_ready'),
        ))

    def delete_group_atmosphere_role(self, role_key: str) -> Dict[str, Any]:
        key = str(role_key or '').strip()
        if not key:
            raise HTTPException(status_code=400, detail='role_key_required')
        existing = self._get_group_atmosphere_config(key)
        if not existing:
            raise HTTPException(status_code=404, detail='role_not_found')
        usage = self.group_atmosphere_role_usage(key)
        with self.db.connect() as conn:
            binding_count = int(conn.execute(
                "SELECT COUNT(*) FROM whatsapp_group_atmosphere_role_bindings WHERE role_key=?",
                (key,),
            ).fetchone()[0] or 0)
            templates = [dict(item or {}) for item in list((existing or {}).get('template_pool') or [])]
            for item in templates:
                item['safe_to_send'] = False
                item['enabled'] = False
                item['role_deleted_at'] = utc_now()
            # 话术角色只是容器：删除角色后保留桥接关系，让页面显示“角色被删除/桥接失效”，运营可后续编辑更换角色。
            if templates:
                conn.execute(
                    "UPDATE whatsapp_group_atmosphere_configs SET enabled=0, template_pool=?, status='candidate_pool', updated_at=? WHERE config_name=?",
                    (json.dumps(templates, ensure_ascii=False), utc_now(), key),
                )
                kept_candidate_pool = True
            else:
                conn.execute("DELETE FROM whatsapp_group_atmosphere_configs WHERE config_name=?", (key,))
                kept_candidate_pool = False
            conn.commit()
        return {'ok': True, 'deleted': True, 'role_key': key, 'affected_bindings': binding_count, 'deleted_bindings': 0, 'kept_bindings': True, 'kept_candidate_pool': kept_candidate_pool, 'usage': usage}

    def _get_group_atmosphere_account_group(self, account_key: str, group_index: int) -> tuple[Dict[str, Any], Dict[str, Any]]:
        row = self._get_whatsapp_approval_account_row(str(account_key or '').strip())
        if not row or str(row.get('responsible_type') or '').strip() != 'group_atmosphere':
            raise HTTPException(status_code=404, detail='group_atmosphere_account_not_found')
        account = self._serialize_group_atmosphere_account_row(row, runtime_state={}, session_state={})
        groups = list(account.get('groups') or [])
        idx = int(group_index or 0)
        if idx < 0 or idx >= len(groups):
            raise HTTPException(status_code=404, detail='group_not_found')
        return account, dict(groups[idx] or {})

    @staticmethod
    def _group_atmosphere_account_group_enabled_from_row(account_row: Dict[str, Any], group_index: int, target_group: str = '') -> bool:
        if not account_row:
            return True
        try:
            groups = json.loads(str(account_row.get('group_links') or '[]'))
        except Exception:
            groups = []
        if not isinstance(groups, list):
            return True
        normalized_target = str(target_group or '').strip()
        candidate = None
        idx = int(group_index or 0)
        if 0 <= idx < len(groups) and isinstance(groups[idx], dict):
            candidate = groups[idx]
        if normalized_target and (
            not isinstance(candidate, dict)
            or str(candidate.get('target_group') or candidate.get('group_id') or candidate.get('link') or '').strip() != normalized_target
        ):
            for item in groups:
                if not isinstance(item, dict):
                    continue
                item_target = str(item.get('target_group') or item.get('group_id') or item.get('link') or '').strip()
                if item_target == normalized_target:
                    candidate = item
                    break
        if not isinstance(candidate, dict):
            return True
        return candidate.get('enabled') is not False

    def _set_group_atmosphere_account_group_enabled(
        self,
        conn: sqlite3.Connection,
        *,
        account_key: str,
        group_index: int,
        target_group: str = '',
        enabled: bool,
    ) -> bool:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            return False
        row = conn.execute(
            "SELECT group_links FROM whatsapp_approval_accounts WHERE account_key=? AND responsible_type='group_atmosphere'",
            (normalized_key,),
        ).fetchone()
        if not row:
            return False
        try:
            groups = json.loads(str(row['group_links'] or '[]'))
        except Exception:
            groups = []
        if not isinstance(groups, list):
            return False
        normalized_target = str(target_group or '').strip()
        idx = int(group_index or 0)
        target_index = idx if 0 <= idx < len(groups) and isinstance(groups[idx], dict) else -1
        if normalized_target:
            if target_index < 0 or str(groups[target_index].get('target_group') or groups[target_index].get('group_id') or groups[target_index].get('link') or '').strip() != normalized_target:
                for cursor, item in enumerate(groups):
                    if not isinstance(item, dict):
                        continue
                    item_target = str(item.get('target_group') or item.get('group_id') or item.get('link') or '').strip()
                    if item_target == normalized_target:
                        target_index = cursor
                        break
        if target_index < 0:
            return False
        next_groups = [dict(item or {}) if isinstance(item, dict) else item for item in groups]
        current = dict(next_groups[target_index] or {})
        next_enabled = bool(enabled)
        if current.get('enabled') is next_enabled:
            return False
        current['enabled'] = next_enabled
        next_groups[target_index] = current
        conn.execute(
            "UPDATE whatsapp_approval_accounts SET group_links=?, updated_at=? WHERE account_key=?",
            (json.dumps(next_groups, ensure_ascii=False), utc_now(), normalized_key),
        )
        return True

    def _resolve_group_atmosphere_binding_targets(self, role: Dict[str, Any], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        role_summary = self._group_atmosphere_role_summary(role) if role else {}
        role_region = str((role_summary or {}).get('region') or (role or {}).get('region') or '').strip()
        role_positioning = str((role_summary or {}).get('role_positioning') or (role or {}).get('role_positioning') or '').strip()
        explicit_account_key = str((payload or {}).get('account_key') or '').strip()
        group_targets = [str(x or '').strip() for x in list((payload or {}).get('group_targets') or []) if str(x or '').strip()]
        if explicit_account_key:
            group_indexes = (payload or {}).get('group_indexes')
            if group_indexes is None:
                group_indexes = [(payload or {}).get('group_index', 0)]
            if len(list(group_indexes or [])) > 10:
                raise HTTPException(status_code=400, detail='role_binding_groups_limit_10')
            seen_indexes: List[int] = []
            for idx in [int(i) for i in list(group_indexes or [])]:
                if idx not in seen_indexes:
                    seen_indexes.append(idx)
            if len(seen_indexes) > 10:
                raise HTTPException(status_code=400, detail='role_binding_groups_limit_10')
            return [{'account_key': explicit_account_key, 'group_index': idx} for idx in seen_indexes]
        if not group_targets:
            raise HTTPException(status_code=400, detail='group_targets_required')
        if len(group_targets) > 10:
            raise HTTPException(status_code=400, detail='role_binding_groups_limit_10')
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM whatsapp_approval_accounts WHERE responsible_type = 'group_atmosphere' AND enabled = 1 ORDER BY updated_at DESC, account_key ASC"
            ).fetchall()]
            existing_rows = [self._row_to_group_atmosphere_role_binding(row) for row in conn.execute("SELECT * FROM whatsapp_group_atmosphere_role_bindings").fetchall()]
        accounts = [self._serialize_group_atmosphere_account_row(row, runtime_state={}, session_state={}) for row in rows]
        resolved: List[Dict[str, Any]] = []
        for target in group_targets:
            target_matches: List[Dict[str, Any]] = []
            country_mismatch = False
            conflict = False
            for account in accounts:
                account_region = str(account.get('region') or '').strip()
                for idx, group in enumerate(account.get('groups') or []):
                    if str(group.get('target_group') or '').strip() != target:
                        continue
                    if role_region and account_region and account_region != role_region:
                        country_mismatch = True
                        continue
                    has_conflict = False
                    for binding in existing_rows:
                        if str(binding.get('account_key') or '') == str(account.get('account_key') or '') and str(binding.get('target_group') or '') == target and str(binding.get('role_positioning') or '') and str(binding.get('role_positioning') or '') != role_positioning:
                            has_conflict = True
                            break
                    if has_conflict:
                        conflict = True
                        continue
                    target_matches.append({'account_key': account.get('account_key'), 'group_index': idx})
            if not target_matches:
                if country_mismatch:
                    raise HTTPException(status_code=400, detail=f'国家/地区不一致：{target}')
                if conflict:
                    raise HTTPException(status_code=400, detail=f'该群已有其他话术类型，请增加另一个可用 WhatsApp 账号：{target}')
                raise HTTPException(status_code=400, detail=f'缺少可用 WhatsApp 发言账号：{target}')
            resolved.append(target_matches[0])
        deduped: List[Dict[str, Any]] = []
        seen = set()
        for item in resolved:
            key = (str(item.get('account_key') or ''), int(item.get('group_index') or 0))
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        return deduped

    def upsert_group_atmosphere_role_bindings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        role_key = str((payload or {}).get('role_key') or (payload or {}).get('config_name') or '').strip()
        if not role_key:
            raw_strategies = (payload or {}).get('schedule_strategies')
            if isinstance(raw_strategies, list):
                for item in raw_strategies:
                    if isinstance(item, dict) and item.get('enabled') is not False and str(item.get('role_key') or '').strip():
                        role_key = str(item.get('role_key') or '').strip()
                        break
        if not role_key:
            raise HTTPException(status_code=400, detail='role_key_required')
        role = self._get_group_atmosphere_config(role_key)
        if not role:
            raise HTTPException(status_code=404, detail='role_not_found')
        targets = self._resolve_group_atmosphere_binding_targets(role, payload)
        if not targets:
            raise HTTPException(status_code=400, detail='group_targets_required')
        if len(targets) > 10:
            raise HTTPException(status_code=400, detail='role_binding_groups_limit_10')
        now = utc_now()
        bindings = []
        with self.db.connect() as conn:
            for target in targets:
                account_key = str(target.get('account_key') or '').strip()
                idx = int(target.get('group_index') or 0)
                account, group = self._get_group_atmosphere_account_group(account_key, idx)
                target_group = str(group.get('target_group') or '').strip()
                if not target_group:
                    raise HTTPException(status_code=400, detail='target_group_required')
                requested_binding_id = str((payload or {}).get('binding_id') or '').strip()
                binding_id = requested_binding_id or f"gabind_{hashlib.sha1(f'{role_key}:{account_key}:{idx}'.encode()).hexdigest()[:16]}"
                if not requested_binding_id:
                    existing_binding_id_row = conn.execute(
                        "SELECT role_key, account_key, group_index FROM whatsapp_group_atmosphere_role_bindings WHERE binding_id=?",
                        (binding_id,),
                    ).fetchone()
                    if existing_binding_id_row and (
                        str(existing_binding_id_row['role_key'] or '') != role_key
                        or str(existing_binding_id_row['account_key'] or '') != account_key
                        or int(existing_binding_id_row['group_index'] or 0) != idx
                    ):
                        for seed in [f'v2:{role_key}:{account_key}:{idx}:{target_group}', f'v2:{role_key}:{account_key}:{idx}:{target_group}:{uuid.uuid4().hex}']:
                            candidate = f"gabind_{hashlib.sha1(seed.encode()).hexdigest()[:16]}"
                            if not conn.execute("SELECT 1 FROM whatsapp_group_atmosphere_role_bindings WHERE binding_id=?", (candidate,)).fetchone():
                                binding_id = candidate
                                break
                occupied = conn.execute(
                    "SELECT binding_id, role_key FROM whatsapp_group_atmosphere_role_bindings WHERE account_key=? AND group_index=? AND binding_id<>?",
                    (account_key, idx, requested_binding_id or ''),
                ).fetchone()
                if occupied and str(occupied['role_key'] or '') != role_key:
                    raise HTTPException(status_code=409, detail='role_binding_account_group_already_used')
                daily_max = int((payload or {}).get('daily_max_messages') if (payload or {}).get('daily_max_messages') is not None else (group.get('daily_max_messages') or account.get('daily_max_messages') or role.get('daily_max_messages') or 0))
                min_interval = _group_atmosphere_mapping_interval_seconds(
                    payload or {},
                    'min_interval_seconds',
                    'min_interval_minutes',
                    _group_atmosphere_mapping_interval_seconds(
                        group,
                        'min_interval_seconds',
                        'min_interval_minutes',
                        _group_atmosphere_mapping_interval_seconds(account, 'min_interval_seconds', 'min_interval_minutes', _group_atmosphere_mapping_interval_seconds(role, 'min_interval_seconds', 'min_interval_minutes', 60)),
                    ),
                )
                max_interval = max(
                    min_interval,
                    _group_atmosphere_mapping_interval_seconds(
                        payload or {},
                        'max_interval_seconds',
                        'max_interval_minutes',
                        _group_atmosphere_mapping_interval_seconds(
                            group,
                            'max_interval_seconds',
                            'max_interval_minutes',
                            _group_atmosphere_mapping_interval_seconds(account, 'max_interval_seconds', 'max_interval_minutes', _group_atmosphere_mapping_interval_seconds(role, 'max_interval_seconds', 'max_interval_minutes', max(min_interval, 240))),
                        ),
                    ),
                )
                schedule_strategies = self._normalize_group_atmosphere_binding_schedule_strategies(
                    (payload or {}).get('schedule_strategies'),
                    fallback={
                        'role_key': role_key,
                        'min_interval_seconds': min_interval,
                        'max_interval_seconds': max_interval,
                        'randomness_level': str((payload or {}).get('randomness_level') or group.get('randomness_level') or account.get('randomness_level') or 'medium').strip() or 'medium',
                        'phrase_send_order': str((payload or {}).get('phrase_send_order') or 'random').strip() or 'random',
                        'allowed_windows': (payload or {}).get('allowed_windows') or group.get('allowed_windows') or role.get('allowed_windows') or [],
                    },
                    require_enabled_strategy=True,
                )
                conn.execute(
                    """
                    INSERT INTO whatsapp_group_atmosphere_role_bindings (
                        binding_id, role_key, account_key, group_index, target_group, group_name, enabled,
                        auto_speaking_enabled, trigger_speaking_enabled, group_send_permission_enabled, worker_base_url,
                        daily_max_messages, min_interval_minutes, max_interval_minutes, randomness_level, phrase_send_order, allowed_windows, schedule_strategies,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'enabled', ?, ?)
                    ON CONFLICT(role_key, account_key, group_index) DO UPDATE SET
                        target_group=excluded.target_group, group_name=excluded.group_name,
                        enabled=excluded.enabled, auto_speaking_enabled=excluded.auto_speaking_enabled,
                        trigger_speaking_enabled=excluded.trigger_speaking_enabled,
                        group_send_permission_enabled=excluded.group_send_permission_enabled,
                        worker_base_url=excluded.worker_base_url,
                        daily_max_messages=excluded.daily_max_messages, min_interval_minutes=excluded.min_interval_minutes,
                        max_interval_minutes=excluded.max_interval_minutes, randomness_level=excluded.randomness_level,
                        phrase_send_order=excluded.phrase_send_order, allowed_windows=excluded.allowed_windows,
                        schedule_strategies=excluded.schedule_strategies,
                        status=excluded.status, updated_at=excluded.updated_at
                    """,
                    (
                        binding_id, role_key, account_key, idx, target_group, str(group.get('group_name') or '').strip() or target_group,
                        0 if (payload or {}).get('enabled') is False else 1,
                        0 if (payload or {}).get('auto_speaking_enabled') is False else 1,
                        0 if (payload or {}).get('trigger_speaking_enabled') is False else 1,
                        0 if (payload or {}).get('group_send_permission_enabled') is False or group.get('enabled') is False else 1,
                        self._validate_group_atmosphere_worker_base_url((payload or {}).get('worker_base_url')),
                        daily_max, min_interval, max(max_interval, min_interval),
                        str((payload or {}).get('randomness_level') or group.get('randomness_level') or account.get('randomness_level') or 'medium').strip() or 'medium',
                        str((payload or {}).get('phrase_send_order') or 'random').strip() or 'random',
                        json.dumps((payload or {}).get('allowed_windows') or group.get('allowed_windows') or role.get('allowed_windows') or [], ensure_ascii=False),
                        json.dumps(schedule_strategies, ensure_ascii=False),
                        now, now,
                    ),
                )
                row = conn.execute("SELECT * FROM whatsapp_group_atmosphere_role_bindings WHERE role_key=? AND account_key=? AND group_index=?", (role_key, account_key, idx)).fetchone()
                bindings.append(self._row_to_group_atmosphere_role_binding(row))
            conn.commit()
        relationships = self._group_atmosphere_role_binding_relationships(bindings)
        relationship = relationships[0] if relationships else None
        return {'ok': True, 'created_count': len(bindings), 'bindings': bindings, 'relationship': relationship, 'relationships': relationships}

    def delete_group_atmosphere_role_binding(self, binding_id: str) -> Dict[str, Any]:
        normalized = str(binding_id or '').strip()
        if not normalized:
            raise HTTPException(status_code=404, detail='role_binding_not_found')
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM whatsapp_group_atmosphere_role_bindings WHERE binding_id=?", (normalized,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='role_binding_not_found')
            snapshot = self._row_to_group_atmosphere_role_binding(row)
            conn.execute("DELETE FROM whatsapp_group_atmosphere_role_bindings WHERE binding_id=?", (normalized,))
            generated_config_name = f'binding-{normalized}'
            conn.execute(
                """
                UPDATE whatsapp_group_atmosphere_configs
                SET enabled=0, status='disabled_deleted_role_binding', next_due_at=NULL, updated_at=?
                WHERE config_name=?
                """,
                (utc_now(), generated_config_name),
            )
            self._record_audit_event(
                conn,
                event_type='group_atmosphere_role_binding_deleted',
                event_source='group_atmosphere',
                payload={'binding_id': normalized, 'generated_config_name': generated_config_name, 'binding': snapshot},
            )
            conn.commit()
        return {'ok': True, 'deleted': True, 'binding_id': normalized}

    def _maybe_migrate_group_atmosphere_trigger_rules_for_role_change(
        self,
        conn: sqlite3.Connection,
        *,
        old_role_key: str,
        new_role_key: str,
        binding_id: str = '',
    ) -> Dict[str, Any]:
        old_key = str(old_role_key or '').strip()
        new_key = str(new_role_key or '').strip()
        if not old_key or not new_key or old_key == new_key:
            return {'migrated_count': 0, 'reason': 'role_key_unchanged'}
        old_rule_count = int((conn.execute(
            "SELECT COUNT(*) FROM whatsapp_group_atmosphere_trigger_rules WHERE relationship_key=?",
            (old_key,),
        ).fetchone() or [0])[0] or 0)
        if old_rule_count <= 0:
            return {'migrated_count': 0, 'reason': 'old_role_has_no_rules'}
        new_rule_count = int((conn.execute(
            "SELECT COUNT(*) FROM whatsapp_group_atmosphere_trigger_rules WHERE relationship_key=?",
            (new_key,),
        ).fetchone() or [0])[0] or 0)
        if new_rule_count > 0:
            return {'migrated_count': 0, 'reason': 'new_role_already_has_rules', 'old_rule_count': old_rule_count, 'new_rule_count': new_rule_count}
        remaining_old_bindings = int((conn.execute(
            "SELECT COUNT(*) FROM whatsapp_group_atmosphere_role_bindings WHERE role_key=?",
            (old_key,),
        ).fetchone() or [0])[0] or 0)
        if remaining_old_bindings > 0:
            return {'migrated_count': 0, 'reason': 'old_role_still_bound', 'old_rule_count': old_rule_count, 'remaining_old_bindings': remaining_old_bindings}
        now = utc_now()
        cur = conn.execute(
            "UPDATE whatsapp_group_atmosphere_trigger_rules SET relationship_key=?, updated_at=? WHERE relationship_key=?",
            (new_key, now, old_key),
        )
        migrated_count = int(cur.rowcount or 0)
        if migrated_count > 0:
            self._record_audit_event(
                conn,
                event_type='group_atmosphere_trigger_rules_migrated',
                event_source='group_atmosphere',
                payload={
                    'binding_id': binding_id,
                    'from_role_key': old_key,
                    'to_role_key': new_key,
                    'migrated_count': migrated_count,
                    'reason': 'binding_role_key_changed',
                },
            )
        return {'migrated_count': migrated_count, 'reason': 'migrated', 'from_role_key': old_key, 'to_role_key': new_key}

    def update_group_atmosphere_role_binding(self, binding_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = str(binding_id or '').strip()
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM whatsapp_group_atmosphere_role_bindings WHERE binding_id=?", (normalized,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='role_binding_not_found')
        current = self._row_to_group_atmosphere_role_binding(row)
        merged = dict(current)
        has_group_permission_update = 'group_send_permission_enabled' in (payload or {})
        has_schedule_strategy_update = 'schedule_strategies' in (payload or {})
        for key in ['role_key', 'enabled', 'auto_speaking_enabled', 'trigger_speaking_enabled', 'group_send_permission_enabled', 'worker_base_url', 'daily_max_messages', 'min_interval_seconds', 'max_interval_seconds', 'min_interval_minutes', 'max_interval_minutes', 'randomness_level', 'phrase_send_order', 'allowed_windows', 'schedule_strategies']:
            if key in (payload or {}):
                merged[key] = (payload or {}).get(key)
        next_role_key = str(merged.get('role_key') or '').strip()
        if not next_role_key or not self._get_group_atmosphere_config(next_role_key):
            raise HTTPException(status_code=404, detail='role_not_found')
        allowed_windows = merged.get('allowed_windows')
        if isinstance(allowed_windows, str):
            try:
                allowed_windows = json.loads(allowed_windows or '[]')
            except Exception:
                allowed_windows = []
        schedule_strategies = self._normalize_group_atmosphere_binding_schedule_strategies(
            merged.get('schedule_strategies'),
            fallback={
                'role_key': next_role_key,
                'min_interval_seconds': _group_atmosphere_mapping_interval_seconds(merged, 'min_interval_seconds', 'min_interval_minutes', 60),
                'max_interval_seconds': _group_atmosphere_mapping_interval_seconds(merged, 'max_interval_seconds', 'max_interval_minutes', 240),
                'randomness_level': str(merged.get('randomness_level') or 'medium'),
                'phrase_send_order': str(merged.get('phrase_send_order') or 'random'),
                'allowed_windows': allowed_windows or [],
            },
            require_enabled_strategy=True,
            validate_roles=has_schedule_strategy_update,
        )
        with self.db.connect() as conn:
            conflict = conn.execute(
                "SELECT binding_id FROM whatsapp_group_atmosphere_role_bindings WHERE role_key=? AND account_key=? AND group_index=? AND binding_id<>?",
                (next_role_key, current.get('account_key'), int(current.get('group_index') or 0), normalized),
            ).fetchone()
            if conflict:
                raise HTTPException(status_code=409, detail='role_binding_target_already_exists')
            conn.execute(
                """UPDATE whatsapp_group_atmosphere_role_bindings SET role_key=?, enabled=?, auto_speaking_enabled=?, trigger_speaking_enabled=?, group_send_permission_enabled=?, worker_base_url=?, daily_max_messages=?, min_interval_minutes=?, max_interval_minutes=?, randomness_level=?, phrase_send_order=?, allowed_windows=?, schedule_strategies=?, updated_at=? WHERE binding_id=?""",
                (
                    next_role_key,
                    1 if merged.get('enabled') else 0,
                    1 if merged.get('auto_speaking_enabled') else 0,
                    1 if merged.get('trigger_speaking_enabled') else 0,
                    1 if merged.get('group_send_permission_enabled') else 0,
                    self._validate_group_atmosphere_worker_base_url(merged.get('worker_base_url')),
                    int(merged.get('daily_max_messages') if merged.get('daily_max_messages') is not None else 0),
                    _group_atmosphere_mapping_interval_seconds(merged, 'min_interval_seconds', 'min_interval_minutes', 60),
                    max(
                        _group_atmosphere_mapping_interval_seconds(merged, 'min_interval_seconds', 'min_interval_minutes', 60),
                        _group_atmosphere_mapping_interval_seconds(merged, 'max_interval_seconds', 'max_interval_minutes', 240),
                    ),
                    str(merged.get('randomness_level') or 'medium'),
                    str(merged.get('phrase_send_order') or 'random'),
                    json.dumps(allowed_windows or [], ensure_ascii=False),
                    json.dumps(schedule_strategies, ensure_ascii=False),
                    utc_now(),
                    normalized,
                ),
            )
            trigger_rule_migration = self._maybe_migrate_group_atmosphere_trigger_rules_for_role_change(
                conn,
                old_role_key=str(current.get('role_key') or '').strip(),
                new_role_key=next_role_key,
                binding_id=normalized,
            )
            if has_group_permission_update:
                self._set_group_atmosphere_account_group_enabled(
                    conn,
                    account_key=str(current.get('account_key') or '').strip(),
                    group_index=int(current.get('group_index') or 0),
                    target_group=str(current.get('target_group') or '').strip(),
                    enabled=bool(merged.get('group_send_permission_enabled')),
                )
            conn.commit()
        return {'ok': True, 'binding': self.get_group_atmosphere_role_binding(normalized), 'trigger_rule_migration': trigger_rule_migration}

    def _trusted_group_atmosphere_sent_count_today(self, *, binding_id: str = '', account_key: str = '', target_group: str = '') -> int:
        """Count only WhatsApp sends with worker evidence, not dry-run/cache increments."""
        today = _group_atmosphere_business_date()
        day_start_utc, day_end_utc = _group_atmosphere_business_day_bounds_utc()
        conn = self.db.connect()
        try:
            if str(binding_id or '').strip():
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM mcn_event_ledger
                    WHERE event_type='group_message_sent'
                      AND object_type='group_atmosphere_binding'
                      AND object_key=?
                      AND evidence_level IN ('observed_in_runtime_history', 'frontend_visible_verified')
                      AND external_id<>''
                      AND created_at>=?
                      AND created_at<?
                    """,
                    (str(binding_id or '').strip(), day_start_utc, day_end_utc),
                ).fetchone()
                ledger_count = int((row[0] if row else 0) or 0)
                if ledger_count:
                    return ledger_count
            elif account_key or target_group:
                clauses = ["event_type='group_message_sent'", "evidence_level IN ('observed_in_runtime_history', 'frontend_visible_verified')", "external_id<>''", "created_at>=?", "created_at<?"]
                params: List[Any] = [day_start_utc, day_end_utc]
                if account_key:
                    clauses.append("payload_json LIKE ?")
                    params.append(f'%"account_key": "{str(account_key or "").strip()}"%')
                if target_group:
                    clauses.append("payload_json LIKE ?")
                    params.append(f'%"target_group": "{str(target_group or "").strip()}"%')
                row = conn.execute(f"SELECT COUNT(*) FROM mcn_event_ledger WHERE {' AND '.join(clauses)}", tuple(params)).fetchone()
                ledger_count = int((row[0] if row else 0) or 0)
                if ledger_count:
                    return ledger_count
        except Exception:
            pass
        config_name = f"binding-{str(binding_id or '').strip()}" if str(binding_id or '').strip() else ''
        clauses = ["created_at>=?", "created_at<?", "direction='outbound'", "delivery_state IN ('runtime_observed', 'frontend_verified')"]
        params: List[Any] = [day_start_utc, day_end_utc]
        if config_name:
            clauses.append("config_name=?")
            params.append(config_name)
        else:
            if account_key:
                clauses.append("account_key=?")
                params.append(str(account_key or '').strip())
            if target_group:
                clauses.append("target_group=?")
                params.append(str(target_group or '').strip())
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    f"SELECT raw_result FROM whatsapp_group_atmosphere_logs WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT 2000",
                    tuple(params),
                ).fetchall()
        except Exception:
            return 0
        count = 0
        for row in rows:
            try:
                raw = json.loads((row['raw_result'] if hasattr(row, 'keys') else row[0]) or '{}')
            except Exception:
                raw = {}
            message_id = str((raw or {}).get('message_id') or ((raw or {}).get('raw_result') or {}).get('message_id') or '').strip()
            if message_id and not bool((raw or {}).get('dry_run')):
                count += 1
        return count

    def _accepted_group_atmosphere_binding_send_count_today(self, *, binding_id: str = '', account_key: str = '', target_group: str = '', include_cached_counter: bool = True) -> int:
        """Count sends accepted by the WhatsApp worker, even when runtime readback lags."""
        today = _group_atmosphere_business_date()
        day_start_utc, day_end_utc = _group_atmosphere_business_day_bounds_utc()
        normalized_binding_id = str(binding_id or '').strip()
        ledger_count = 0
        ledger_external_ids: set[str] = set()
        try:
            with self.db.connect() as conn:
                if normalized_binding_id:
                    rows = conn.execute(
                        """
                        SELECT external_id FROM mcn_event_ledger
                        WHERE event_type='group_message_sent'
                          AND object_type='group_atmosphere_binding'
                          AND object_key=?
                          AND status IN ('success', 'accepted')
                          AND external_id<>''
                          AND created_at>=?
                          AND created_at<?
                        """,
                        (normalized_binding_id, day_start_utc, day_end_utc),
                    ).fetchall()
                    ledger_external_ids = {
                        str((row['external_id'] if hasattr(row, 'keys') else row[0]) or '').strip()
                        for row in rows
                        if str((row['external_id'] if hasattr(row, 'keys') else row[0]) or '').strip()
                    }
                    ledger_count = len(ledger_external_ids)
                elif account_key or target_group:
                    clauses = [
                        "event_type='group_message_sent'",
                        "status IN ('success', 'accepted')",
                        "external_id<>''",
                        "created_at>=?",
                        "created_at<?",
                    ]
                    params: List[Any] = [day_start_utc, day_end_utc]
                    if account_key:
                        clauses.append("payload_json LIKE ?")
                        params.append(f'%"account_key": "{str(account_key or "").strip()}"%')
                    if target_group:
                        clauses.append("payload_json LIKE ?")
                        params.append(f'%"target_group": "{str(target_group or "").strip()}"%')
                    rows = conn.execute(
                        f"SELECT external_id FROM mcn_event_ledger WHERE {' AND '.join(clauses)}",
                        tuple(params),
                    ).fetchall()
                    ledger_external_ids = {
                        str((row['external_id'] if hasattr(row, 'keys') else row[0]) or '').strip()
                        for row in rows
                        if str((row['external_id'] if hasattr(row, 'keys') else row[0]) or '').strip()
                    }
                    ledger_count = len(ledger_external_ids)
        except Exception:
            pass

        config_name = f"binding-{normalized_binding_id}" if normalized_binding_id else ''
        clauses = [
            "created_at>=?",
            "created_at<?",
            "direction='outbound'",
            "delivery_state IN ('api_accepted', 'runtime_observed', 'readback_missing', 'readback_ambiguous', 'frontend_verified')",
        ]
        params: List[Any] = [day_start_utc, day_end_utc]
        target_clauses: List[str] = []
        target_params: List[Any] = []
        if config_name:
            target_clauses.append("config_name=?")
            target_params.append(config_name)
        if account_key or target_group:
            account_group_clauses: List[str] = []
            if account_key:
                account_group_clauses.append("account_key=?")
                target_params.append(str(account_key or '').strip())
            if target_group:
                account_group_clauses.append("target_group=?")
                target_params.append(str(target_group or '').strip())
            if account_group_clauses:
                target_clauses.append(f"({' AND '.join(account_group_clauses)})")
        if target_clauses:
            clauses.append(f"({' OR '.join(target_clauses)})")
            params.extend(target_params)
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    f"SELECT raw_result, legacy_message_id FROM whatsapp_group_atmosphere_logs WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT 2000",
                    tuple(params),
                ).fetchall()
        except Exception:
            rows = []
        unledgered_log_count = 0
        seen_log_message_ids: set[str] = set()
        for row in rows:
            try:
                raw = json.loads((row['raw_result'] if hasattr(row, 'keys') else row[0]) or '{}')
            except Exception:
                raw = {}
            legacy_message_id = str((row['legacy_message_id'] if hasattr(row, 'keys') else (row[1] if len(row) > 1 else '')) or '').strip()
            message_id = str((raw or {}).get('message_id') or ((raw or {}).get('raw_result') or {}).get('message_id') or legacy_message_id or '').strip()
            if not message_id or bool((raw or {}).get('dry_run')):
                continue
            if message_id in seen_log_message_ids:
                continue
            seen_log_message_ids.add(message_id)
            if message_id not in ledger_external_ids:
                unledgered_log_count += 1

        cached_count = 0
        if include_cached_counter and normalized_binding_id:
            try:
                with self.db.connect() as conn:
                    row = conn.execute(
                        "SELECT sent_count_today, sent_count_date FROM whatsapp_group_atmosphere_role_bindings WHERE binding_id=?",
                        (normalized_binding_id,),
                    ).fetchone()
                if row and str(row['sent_count_date'] or '') == today:
                    cached_count = max(0, int(row['sent_count_today'] or 0))
            except Exception:
                pass
        return max(0, ledger_count + unledgered_log_count, cached_count)

    def _write_group_atmosphere_binding_send_ledger(
        self,
        *,
        binding: Optional[Dict[str, Any]],
        trigger_type: str,
        message_text: str,
        delivery: Dict[str, Any],
    ) -> bool:
        if not binding or not delivery.get('accepted'):
            return False
        binding_id = str(binding.get('binding_id') or '').strip()
        if not binding_id:
            return False
        raw_result = dict(delivery.get('raw_result') or {})
        nested_raw_result = raw_result.get('raw_result')
        nested_raw_result = nested_raw_result if isinstance(nested_raw_result, dict) else {}
        worker_message_id = str(
            raw_result.get('message_id')
            or nested_raw_result.get('message_id')
            or delivery.get('legacy_message_id')
            or ''
        ).strip()
        if not worker_message_id:
            return False
        with self.db.connect() as conn:
            existing = conn.execute(
                """
                SELECT 1 FROM mcn_event_ledger
                WHERE event_type='group_message_sent'
                  AND object_type='group_atmosphere_binding'
                  AND object_key=?
                  AND external_id=?
                LIMIT 1
                """,
                (binding_id, worker_message_id),
            ).fetchone()
            if existing:
                return False
            self.write_event_ledger(
                event_type='group_message_sent',
                object_type='group_atmosphere_binding',
                object_key=binding_id,
                status='success' if delivery.get('sent') else 'accepted',
                evidence_level=str(delivery.get('evidence_level') or 'none'),
                external_id=worker_message_id,
                payload={
                    'binding_id': binding_id,
                    'role_key': binding.get('role_key'),
                    'account_key': binding.get('account_key'),
                    'target_group': binding.get('target_group'),
                    'group_name': binding.get('group_name'),
                    'trigger_type': trigger_type,
                    'message_text': message_text,
                    'accepted_by_worker': bool(delivery.get('accepted')),
                    'delivery_state': str(delivery.get('delivery_state') or 'unknown'),
                    'readback_matched': bool(delivery.get('readback_matched')),
                    'source': 'manual_group_send',
                },
                conn=conn,
            )
            conn.commit()
        return True

    def _row_to_group_atmosphere_role_binding(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        role = self._get_group_atmosphere_config(str(item.get('role_key') or '').strip()) or {}
        role_templates = [dict(x or {}) for x in list((role or {}).get('template_pool') or [])]
        role_deleted = (not role) or any(x.get('role_deleted_at') for x in role_templates)
        role_summary = self._group_atmosphere_role_summary(role) if role else {'role_name': item.get('role_key'), 'role_positioning': ''}
        group_name = str(item.get('group_name') or '').strip() or str(item.get('target_group') or '').strip()
        account_row = self._get_whatsapp_approval_account_row(str(item.get('account_key') or '').strip()) or {}
        assigned_account_label = str((account_row or {}).get('account_name') or item.get('account_key') or '').strip()
        binding_group_send_permission_enabled = bool(item.get('group_send_permission_enabled'))
        account_group_enabled = self._group_atmosphere_account_group_enabled_from_row(
            dict(account_row or {}),
            int(item.get('group_index') or 0),
            str(item.get('target_group') or '').strip(),
        )
        effective_group_send_permission_enabled = bool(binding_group_send_permission_enabled and account_group_enabled)
        distribution_status = '可发送'
        if role_deleted:
            distribution_status = '角色被删除'
        elif not item.get('enabled'):
            distribution_status = '桥接已停用'
        elif not account_group_enabled:
            distribution_status = '账号群发言关闭'
        elif not binding_group_send_permission_enabled:
            distribution_status = '群权限关闭'
        elif not item.get('auto_speaking_enabled'):
            distribution_status = '自动发言已暂停'
        trusted_sent_count_today = self._trusted_group_atmosphere_sent_count_today(
            binding_id=str(item.get('binding_id') or '').strip(),
            account_key=str(item.get('account_key') or '').strip(),
            target_group=str(item.get('target_group') or '').strip(),
        )
        accepted_sent_count_today = self._accepted_group_atmosphere_binding_send_count_today(
            binding_id=str(item.get('binding_id') or '').strip(),
            account_key=str(item.get('account_key') or '').strip(),
            target_group=str(item.get('target_group') or '').strip(),
            include_cached_counter=False,
        )
        display_sent_count_today = max(trusted_sent_count_today, accepted_sent_count_today)
        min_interval_seconds = _group_atmosphere_mapping_interval_seconds(item, 'min_interval_seconds', 'min_interval_minutes', 0)
        max_interval_seconds = max(
            min_interval_seconds,
            _group_atmosphere_mapping_interval_seconds(item, 'max_interval_seconds', 'max_interval_minutes', min_interval_seconds),
        )
        try:
            allowed_windows = json.loads(item.get('allowed_windows') or '[]')
        except Exception:
            allowed_windows = []
        schedule_strategies = self._normalize_group_atmosphere_binding_schedule_strategies(
            item.get('schedule_strategies'),
            fallback={
                'role_key': item.get('role_key'),
                'min_interval_seconds': min_interval_seconds,
                'max_interval_seconds': max_interval_seconds,
                'randomness_level': str(item.get('randomness_level') or 'medium'),
                'phrase_send_order': str(item.get('phrase_send_order') or 'random'),
                'allowed_windows': allowed_windows,
            },
            validate_roles=False,
        )
        return {
            'binding_id': item.get('binding_id'),
            'role_key': item.get('role_key'),
            'role_name': role_summary.get('role_name') or role_summary.get('plan_display_name'),
            'role_positioning': role_summary.get('role_positioning'),
            'role_deleted': role_deleted,
            'language': role_summary.get('language'),
            'region': role_summary.get('region'),
            'account_key': item.get('account_key'),
            'assigned_account_label': assigned_account_label,
            'group_index': int(item.get('group_index') or 0),
            'target_group': item.get('target_group'),
            'group_name': group_name,
            'enabled': bool(item.get('enabled')),
            'auto_speaking_enabled': bool(item.get('auto_speaking_enabled')),
            'trigger_speaking_enabled': bool(item.get('trigger_speaking_enabled', 1)),
            'group_send_permission_enabled': effective_group_send_permission_enabled,
            'binding_group_send_permission_enabled': binding_group_send_permission_enabled,
            'account_group_enabled': account_group_enabled,
            'worker_base_url': item.get('worker_base_url') or '',
            'daily_max_messages': int(item.get('daily_max_messages') or 0),
            'min_interval_seconds': min_interval_seconds,
            'max_interval_seconds': max_interval_seconds,
            'min_interval_minutes': min_interval_seconds,
            'max_interval_minutes': max_interval_seconds,
            'randomness_level': str(item.get('randomness_level') or 'medium'),
            'phrase_send_order': str(item.get('phrase_send_order') or 'random'),
            'allowed_windows': allowed_windows,
            'schedule_strategies': schedule_strategies,
            'last_sent_at': item.get('last_sent_at'),
            'sent_count_today': display_sent_count_today,
            'trusted_sent_count_today': trusted_sent_count_today,
            'accepted_sent_count_today': accepted_sent_count_today,
            'sent_count_date': _group_atmosphere_business_date(),
            'next_due_at': item.get('next_due_at'),
            'status': item.get('status'),
            'distribution_status': distribution_status,
            'updated_at': item.get('updated_at'),
        }

    def _group_atmosphere_role_binding_relationships(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        grouped: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for row in rows:
            key = f"{row.get('role_key') or ''}"
            if key not in grouped:
                grouped[key] = {
                    'relationship_key': key,
                    'relationship_label': f"桥接关系{len(order) + 1}",
                    'role_key': row.get('role_key'),
                    'role_name': row.get('role_name'),
                    'role_positioning': row.get('role_positioning'),
                    'role_deleted': bool(row.get('role_deleted')),
                    'distribution_status': row.get('distribution_status'),
                    'language': row.get('language'),
                    'region': row.get('region'),
                    'account_key': row.get('account_key'),
                    'enabled': bool(row.get('enabled')),
                    'auto_speaking_enabled': bool(row.get('auto_speaking_enabled')),
                    'trigger_speaking_enabled': bool(row.get('trigger_speaking_enabled', True)),
                    'daily_max_messages': int(row.get('daily_max_messages') or 0),
                    'min_interval_seconds': int(row.get('min_interval_seconds') or row.get('min_interval_minutes') or 0),
                    'max_interval_seconds': int(row.get('max_interval_seconds') or row.get('max_interval_minutes') or 0),
                    'min_interval_minutes': int(row.get('min_interval_seconds') or row.get('min_interval_minutes') or 0),
                    'max_interval_minutes': int(row.get('max_interval_seconds') or row.get('max_interval_minutes') or 0),
                    'randomness_level': row.get('randomness_level') or 'medium',
                    'phrase_send_order': row.get('phrase_send_order') or 'random',
                    'allowed_windows': row.get('allowed_windows') or [],
                    'schedule_strategies': row.get('schedule_strategies') or [],
                    'groups': [],
                    'updated_at': row.get('updated_at'),
                }
                order.append(key)
            grouped[key]['groups'].append({
                'binding_id': row.get('binding_id'),
                'account_key': row.get('account_key'),
                'group_index': row.get('group_index'),
                'target_group': row.get('target_group'),
                'group_name': row.get('group_name'),
                'assigned_account_label': row.get('assigned_account_label'),
                'role_deleted': bool(row.get('role_deleted')),
                'group_send_permission_enabled': bool(row.get('group_send_permission_enabled')),
                'trigger_speaking_enabled': bool(row.get('trigger_speaking_enabled', True)),
                'enabled': bool(row.get('enabled')),
                'distribution_status': row.get('distribution_status'),
                'last_sent_at': row.get('last_sent_at'),
                'sent_count_today': row.get('sent_count_today'),
                'next_due_at': row.get('next_due_at'),
                'randomness_level': row.get('randomness_level') or 'medium',
                'phrase_send_order': row.get('phrase_send_order') or 'random',
                'schedule_strategies': row.get('schedule_strategies') or [],
            })
            if not row.get('enabled'):
                grouped[key]['enabled'] = False
            if not row.get('auto_speaking_enabled'):
                grouped[key]['auto_speaking_enabled'] = False
            if not row.get('trigger_speaking_enabled', True):
                grouped[key]['trigger_speaking_enabled'] = False
        rule_counts = self._group_atmosphere_trigger_rule_counts_by_relationship()
        for key in order:
            grouped[key]['groups'].sort(key=lambda item: int(item.get('group_index') or 0))
            counts = rule_counts.get(key, {'total': 0, 'enabled': 0, 'types': []})
            grouped[key]['trigger_rule_count'] = int(counts.get('total') or 0)
            grouped[key]['trigger_rule_enabled_count'] = int(counts.get('enabled') or 0)
            grouped[key]['trigger_rule_types'] = list(counts.get('types') or [])
        return [grouped[key] for key in order]

    def list_group_atmosphere_role_bindings(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM whatsapp_group_atmosphere_role_bindings ORDER BY created_at ASC, role_key ASC, account_key ASC, group_index ASC").fetchall()
        output = [self._row_to_group_atmosphere_role_binding(row) for row in rows]
        relationships = self._group_atmosphere_role_binding_relationships(output)
        return {'rows': output, 'count': len(output), 'relationships': relationships, 'relationship_count': len(relationships)}

    def get_group_atmosphere_role_binding(self, binding_id: str) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM whatsapp_group_atmosphere_role_bindings WHERE binding_id=?", (str(binding_id or '').strip(),)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='role_binding_not_found')
        return self._row_to_group_atmosphere_role_binding(row)

    @staticmethod
    def is_group_atmosphere_regular_group_message(message: Dict[str, Any]) -> bool:
        kind = str((message or {}).get('message_type') or (message or {}).get('type') or 'text').strip().lower()
        event_type = str((message or {}).get('event_type') or '').strip().lower()
        system_kinds = {'system', 'group_update', 'notification', 'status', 'participant_update'}
        system_events = {'member_join', 'member_leave', 'group_name_changed', 'group_announcement_changed', 'permission_changed'}
        return kind not in system_kinds and event_type not in system_events

    def _row_to_group_atmosphere_trigger_rule(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        try:
            conditions = json.loads(item.get('conditions_json') or '{}')
        except Exception:
            conditions = {}
        try:
            message_sequence = json.loads(item.get('message_sequence_json') or '[]')
        except Exception:
            message_sequence = []
        if isinstance(message_sequence, list):
            message_sequence = self._hydrate_group_atmosphere_trigger_sequence(message_sequence)
        return {
            'rule_id': item.get('rule_id'),
            'relationship_key': item.get('relationship_key'),
            'rule_name': item.get('rule_name'),
            'trigger_type': item.get('trigger_type'),
            'enabled': bool(item.get('enabled')),
            'priority': int(item.get('priority') or 0),
            'send_mode': self._normalize_group_atmosphere_trigger_send_mode(item.get('send_mode')),
            'conditions': conditions if isinstance(conditions, dict) else {},
            'message_sequence': message_sequence if isinstance(message_sequence, list) else [],
            'delay_min_seconds': int(item.get('delay_min_seconds') or 2),
            'delay_max_seconds': int(item.get('delay_max_seconds') or 5),
            'cooldown_seconds': int(item.get('cooldown_seconds') if item.get('cooldown_seconds') is not None else 60),
            'per_user_cooldown_seconds': int(item.get('per_user_cooldown_seconds') if item.get('per_user_cooldown_seconds') is not None else 10),
            'daily_max_triggers': int(item.get('daily_max_triggers') or 0),
            'last_triggered_at': item.get('last_triggered_at'),
            'created_at': item.get('created_at'),
            'updated_at': item.get('updated_at'),
        }

    def _hydrate_group_atmosphere_trigger_sequence(self, sequence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        hydrated: List[Dict[str, Any]] = []
        for raw_segment in list(sequence or []):
            if not isinstance(raw_segment, dict):
                continue
            segment = dict(raw_segment or {})
            media_id = str(segment.get('media_id') or '').strip()
            if media_id:
                try:
                    media = self.get_group_atmosphere_media_asset(media_id)
                except HTTPException:
                    media = {}
                except Exception:
                    media = {}
                if media:
                    segment['media_id'] = media.get('media_id') or media_id
                    segment['media_path'] = segment.get('media_path') or media.get('media_path') or ''
                    segment['media_mime_type'] = segment.get('media_mime_type') or media.get('mime_type') or ''
                    segment['media_filename'] = segment.get('media_filename') or media.get('filename') or ''
                    segment['media_preview_url'] = segment.get('media_preview_url') or media.get('preview_url') or ''
                segment['type'] = 'image_text'
            elif str(segment.get('type') or '').strip() == 'image_text':
                segment['type'] = 'text'
            hydrated.append(segment)
        return hydrated

    @staticmethod
    def _normalize_group_atmosphere_trigger_send_mode(value: Any) -> str:
        mode = str(value or '').strip().lower()
        return 'random_one' if mode == 'random_one' else 'sequence'

    def _resolve_group_atmosphere_trigger_sequence(self, rule: Dict[str, Any]) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        sequence = [dict(item or {}) for item in list(rule.get('message_sequence') or []) if isinstance(item, dict)]
        send_mode = self._normalize_group_atmosphere_trigger_send_mode(rule.get('send_mode'))
        if send_mode == 'random_one' and sequence:
            selected_index = random.randrange(len(sequence))
            return [dict(sequence[selected_index] or {})], {
                'send_mode': send_mode,
                'selected_message_index': selected_index,
                'selected_message_position': selected_index + 1,
                'candidate_message_count': len(sequence),
            }
        return sequence, {
            'send_mode': send_mode,
            'selected_message_index': None,
            'selected_message_position': None,
            'candidate_message_count': len(sequence),
        }

    def _group_atmosphere_trigger_rule_counts_by_relationship(self) -> Dict[str, Dict[str, Any]]:
        try:
            with self.db.connect() as conn:
                rows = conn.execute("SELECT relationship_key, trigger_type, enabled FROM whatsapp_group_atmosphere_trigger_rules").fetchall()
        except Exception:
            return {}
        output: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = str(row['relationship_key'] or '').strip()
            if not key:
                continue
            item = output.setdefault(key, {'total': 0, 'enabled': 0, 'types': []})
            item['total'] += 1
            if bool(row['enabled']):
                item['enabled'] += 1
            trigger_type = str(row['trigger_type'] or '').strip()
            if trigger_type and trigger_type not in item['types']:
                item['types'].append(trigger_type)
        return output

    def list_group_atmosphere_trigger_rules(self, relationship_key: str = '') -> Dict[str, Any]:
        normalized = str(relationship_key or '').strip()
        with self.db.connect() as conn:
            if normalized:
                rows = conn.execute("SELECT * FROM whatsapp_group_atmosphere_trigger_rules WHERE relationship_key=? ORDER BY priority ASC, updated_at DESC", (normalized,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM whatsapp_group_atmosphere_trigger_rules ORDER BY priority ASC, updated_at DESC").fetchall()
        rules = [self._row_to_group_atmosphere_trigger_rule(row) for row in rows]
        return {'rows': rules, 'count': len(rules)}

    def upsert_group_atmosphere_trigger_rule(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = payload or {}
        requested_rule_id = str(payload.get('rule_id') or '').strip()
        existing_rule = None
        if requested_rule_id:
            with self.db.connect() as conn:
                existing_rule = conn.execute(
                    "SELECT relationship_key, trigger_type FROM whatsapp_group_atmosphere_trigger_rules WHERE rule_id=?",
                    (requested_rule_id,),
                ).fetchone()
            if existing_rule is None:
                raise HTTPException(status_code=404, detail='trigger_rule_not_found')
        relationship_key = str((payload or {}).get('relationship_key') or '').strip()
        if existing_rule and not relationship_key:
            relationship_key = str(existing_rule['relationship_key'] or '').strip()
        if not relationship_key:
            raise HTTPException(status_code=400, detail='relationship_key_required')
        with self.db.connect() as conn:
            relationship_exists = conn.execute("SELECT 1 FROM whatsapp_group_atmosphere_role_bindings WHERE role_key=? LIMIT 1", (relationship_key,)).fetchone()
        if not relationship_exists:
            raise HTTPException(status_code=404, detail='relationship_not_found')
        trigger_type = str(existing_rule['trigger_type'] or '').strip() if existing_rule else str((payload or {}).get('trigger_type') or '').strip()
        if trigger_type not in {'keyword_match', 'member_join', 'group_silence'}:
            raise HTTPException(status_code=400, detail='unsupported_trigger_type')
        send_mode = self._normalize_group_atmosphere_trigger_send_mode((payload or {}).get('send_mode'))
        max_trigger_message_sequence = 10
        message_sequence = [dict(item or {}) for item in list((payload or {}).get('message_sequence') or []) if isinstance(item, dict) and (str(item.get('text') or '').strip() or str(item.get('media_id') or '').strip())]
        if len(message_sequence) > max_trigger_message_sequence:
            raise HTTPException(status_code=400, detail='trigger_message_sequence_max_10')
        for idx, segment in enumerate(message_sequence):
            segment['delay_seconds'] = max(0, int(segment.get('delay_seconds') if segment.get('delay_seconds') is not None else (idx * 3)))
            if str(segment.get('media_id') or '').strip():
                segment['type'] = 'image_text'
            else:
                segment['type'] = 'text'
        message_sequence = self._hydrate_group_atmosphere_trigger_sequence(message_sequence)
        if not message_sequence:
            raise HTTPException(status_code=400, detail='message_sequence_required')
        conditions = (payload or {}).get('conditions') if isinstance((payload or {}).get('conditions'), dict) else {}
        if trigger_type == 'keyword_match':
            keywords = [str(item or '').strip() for item in list(conditions.get('keywords') or []) if str(item or '').strip()]
            single_keyword = str(conditions.get('keyword') or '').strip()
            if single_keyword and single_keyword not in keywords:
                keywords.append(single_keyword)
            if not keywords:
                raise HTTPException(status_code=400, detail='keywords_required')
            conditions['keywords'] = keywords
        if trigger_type == 'group_silence':
            silence_seconds = int(conditions.get('silence_seconds') or conditions.get('threshold_seconds') or 60)
            conditions['silence_seconds'] = max(10, silence_seconds)
        rule_id = requested_rule_id or f"gatr_{uuid.uuid4().hex[:16]}"
        now = utc_now()
        delay_min = max(0, int((payload or {}).get('delay_min_seconds') if (payload or {}).get('delay_min_seconds') is not None else 2))
        delay_max = max(delay_min, int((payload or {}).get('delay_max_seconds') if (payload or {}).get('delay_max_seconds') is not None else 5))
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO whatsapp_group_atmosphere_trigger_rules (
                    rule_id, relationship_key, rule_name, trigger_type, enabled, priority, conditions_json, message_sequence_json,
                    send_mode, delay_min_seconds, delay_max_seconds, cooldown_seconds, per_user_cooldown_seconds, daily_max_triggers,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    relationship_key=excluded.relationship_key, rule_name=excluded.rule_name,
                    enabled=excluded.enabled, priority=excluded.priority, conditions_json=excluded.conditions_json,
                    message_sequence_json=excluded.message_sequence_json, send_mode=excluded.send_mode, delay_min_seconds=excluded.delay_min_seconds,
                    delay_max_seconds=excluded.delay_max_seconds, cooldown_seconds=excluded.cooldown_seconds,
                    per_user_cooldown_seconds=excluded.per_user_cooldown_seconds, daily_max_triggers=excluded.daily_max_triggers,
                    updated_at=excluded.updated_at
                """,
                (
                    rule_id, relationship_key, str((payload or {}).get('rule_name') or trigger_type).strip(), trigger_type,
                    0 if (payload or {}).get('enabled') is False else 1, int((payload or {}).get('priority') or 0),
                    json.dumps(conditions, ensure_ascii=False), json.dumps(message_sequence[:max_trigger_message_sequence], ensure_ascii=False), send_mode,
                    delay_min, delay_max, int((payload or {}).get('cooldown_seconds') or 0), int((payload or {}).get('per_user_cooldown_seconds') or 10),
                    int((payload or {}).get('daily_max_triggers') or 0), now, now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM whatsapp_group_atmosphere_trigger_rules WHERE rule_id=?", (rule_id,)).fetchone()
        return {'ok': True, 'rule': self._row_to_group_atmosphere_trigger_rule(row)}

    def delete_group_atmosphere_trigger_rule(self, rule_id: str) -> Dict[str, Any]:
        normalized = str(rule_id or '').strip()
        if not normalized:
            raise HTTPException(status_code=400, detail='rule_id_required')
        with self.db.connect() as conn:
            cur = conn.execute("DELETE FROM whatsapp_group_atmosphere_trigger_rules WHERE rule_id=?", (normalized,))
            conn.commit()
        if cur.rowcount <= 0:
            raise HTTPException(status_code=404, detail='trigger_rule_not_found')
        return {'ok': True, 'deleted': True, 'rule_id': normalized}

    def test_group_atmosphere_trigger_rule(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        relationship_key = str((payload or {}).get('relationship_key') or '').strip()
        trigger_type = str((payload or {}).get('trigger_type') or 'keyword_match').strip()
        if not relationship_key:
            raise HTTPException(status_code=400, detail='relationship_key_required')
        with self.db.connect() as conn:
            binding_row = conn.execute(
                "SELECT * FROM whatsapp_group_atmosphere_role_bindings WHERE role_key=? AND enabled=1 ORDER BY created_at ASC LIMIT 1",
                (relationship_key,),
            ).fetchone()
        if not binding_row:
            raise HTTPException(status_code=404, detail='relationship_not_found')
        binding = self._row_to_group_atmosphere_role_binding(binding_row)
        if trigger_type == 'keyword_match':
            req = GroupAtmosphereInboundMessageRequest(
                account_key=str(binding.get('account_key') or ''),
                target_group=str(binding.get('target_group') or ''),
                sender_id=str((payload or {}).get('sender_id') or 'preview-user'),
                text=str((payload or {}).get('text') or ''),
                message_type='text',
            )
            result = self.evaluate_group_atmosphere_trigger_rules_for_inbound(req)
        else:
            result = self.evaluate_group_atmosphere_trigger_rules_for_event(
                binding,
                trigger_type=trigger_type,
                event_payload={k: v for k, v in dict(payload or {}).items() if k not in {'relationship_key'}},
                dry_run=True,
            )
        result['dry_run'] = True
        result['would_send'] = bool(result.get('should_respond'))
        return result

    def _find_group_atmosphere_binding_for_event(self, account_key: str, target_group: str) -> Optional[Dict[str, Any]]:
        normalized_account_key = str(account_key or '').strip()
        normalized_target = str(target_group or '').strip()
        if not normalized_account_key or not normalized_target:
            return None
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM whatsapp_group_atmosphere_role_bindings WHERE account_key=? AND target_group=? AND enabled=1 ORDER BY created_at ASC LIMIT 1",
                (normalized_account_key, normalized_target),
            ).fetchone()
            if row:
                return self._row_to_group_atmosphere_role_binding(row)
            account_row = self._get_whatsapp_approval_account_row(normalized_account_key) or {}
            try:
                raw_groups = json.loads(account_row.get('group_links') or '[]')
            except Exception:
                raw_groups = []
            if not isinstance(raw_groups, list):
                raw_groups = []
            matching_targets: set[str] = set()
            matching_indexes: set[int] = set()
            for idx, group in enumerate(raw_groups):
                if not isinstance(group, dict):
                    continue
                group_target = str(group.get('target_group') or group.get('link') or group.get('group_id') or group.get('group_name') or '').strip()
                cached_identity = self._group_atmosphere_cached_group_identity(normalized_account_key, group_target)
                candidates = {
                    group_target,
                    str(group.get('target_group') or '').strip(),
                    str(group.get('link') or '').strip(),
                    str(group.get('group_id') or '').strip(),
                    str(group.get('group_name') or '').strip(),
                    str(cached_identity.get('group_id') or '').strip(),
                    str(cached_identity.get('group_name') or '').strip(),
                }
                if normalized_target in candidates:
                    matching_indexes.add(idx)
                    for candidate in candidates:
                        if candidate:
                            matching_targets.add(candidate)
            if matching_indexes:
                placeholders = ','.join('?' for _ in matching_indexes)
                params = [normalized_account_key, *sorted(matching_indexes)]
                row = conn.execute(
                    f"SELECT * FROM whatsapp_group_atmosphere_role_bindings WHERE account_key=? AND enabled=1 AND group_index IN ({placeholders}) ORDER BY created_at ASC LIMIT 1",
                    params,
                ).fetchone()
                if row:
                    return self._row_to_group_atmosphere_role_binding(row)
            if matching_targets:
                placeholders = ','.join('?' for _ in matching_targets)
                params = [normalized_account_key, *sorted(matching_targets)]
                row = conn.execute(
                    f"SELECT * FROM whatsapp_group_atmosphere_role_bindings WHERE account_key=? AND enabled=1 AND target_group IN ({placeholders}) ORDER BY created_at ASC LIMIT 1",
                    params,
                ).fetchone()
                if row:
                    return self._row_to_group_atmosphere_role_binding(row)
            return None
        finally:
            if self.db.db_path != ':memory:':
                conn.close()

    @staticmethod
    def _keyword_trigger_rule_matches(rule: Dict[str, Any], text: str) -> tuple[bool, str]:
        conditions = rule.get('conditions') or {}
        keywords = [str(item or '').strip() for item in list(conditions.get('keywords') or []) if str(item or '').strip()]
        if not keywords:
            single = str(conditions.get('keyword') or '').strip()
            keywords = [single] if single else []
        case_sensitive = bool(conditions.get('case_sensitive'))
        match_type = str(conditions.get('match_type') or 'contains').strip()
        haystack = str(text or '') if case_sensitive else str(text or '').lower()
        for keyword in sorted(keywords, key=len, reverse=True):
            needle = keyword if case_sensitive else keyword.lower()
            if match_type == 'exact' and haystack.strip() == needle:
                return True, keyword
            if match_type == 'word_boundary' and re.search(rf'(?<!\w){re.escape(needle)}(?!\w)', haystack):
                return True, keyword
            if match_type not in {'exact', 'word_boundary'} and needle in haystack:
                return True, keyword
        return False, ''

    def _group_atmosphere_trigger_cooldown_state(self, binding: Dict[str, Any], rule: Dict[str, Any], *, sender_id: str = '') -> Dict[str, Any]:
        rule_id = str(rule.get('rule_id') or '').strip()
        binding_id = str(binding.get('binding_id') or '').strip()
        target_group = str(binding.get('target_group') or '').strip()
        account_key = str(binding.get('account_key') or '').strip()
        if not rule_id or not target_group:
            return {}
        now_dt = datetime.now(timezone.utc)
        cooldown_seconds = max(0, int(rule.get('cooldown_seconds') or 0))
        per_user_seconds = max(0, int(rule.get('per_user_cooldown_seconds') or 0))
        with self.db.connect() as conn:
            def latest_created(extra_sql: str = '', params: tuple = ()) -> str:
                row = conn.execute(
                    "SELECT created_at FROM whatsapp_group_atmosphere_trigger_events WHERE rule_id=? AND status='matched' AND target_group=? " + extra_sql + " ORDER BY created_at DESC LIMIT 1",
                    (rule_id, target_group, *params),
                ).fetchone()
                if row:
                    return str(row['created_at'] if hasattr(row, 'keys') else row[0] or '')
                if binding_id:
                    row2 = conn.execute(
                        "SELECT created_at FROM whatsapp_group_atmosphere_trigger_events WHERE rule_id=? AND status='matched' AND binding_id=? " + extra_sql + " ORDER BY created_at DESC LIMIT 1",
                        (rule_id, binding_id, *params),
                    ).fetchone()
                    if row2:
                        return str(row2['created_at'] if hasattr(row2, 'keys') else row2[0] or '')
                if account_key:
                    row3 = conn.execute(
                        "SELECT created_at FROM whatsapp_group_atmosphere_trigger_events WHERE rule_id=? AND status='matched' AND account_key=? AND target_group=? " + extra_sql + " ORDER BY created_at DESC LIMIT 1",
                        (rule_id, account_key, target_group, *params),
                    ).fetchone()
                    if row3:
                        return str(row3['created_at'] if hasattr(row3, 'keys') else row3[0] or '')
                return ''
            if cooldown_seconds > 0:
                last = latest_created()
                if last:
                    try:
                        elapsed = (now_dt - parse_iso_datetime(last)).total_seconds()
                    except Exception:
                        elapsed = cooldown_seconds + 1
                    if elapsed < cooldown_seconds:
                        return {'should_respond': False, 'result_code': 'rule_cooldown_active', 'cooldown_scope': 'group', 'remaining_seconds': int(max(1, cooldown_seconds - elapsed))}
            normalized_sender = str(sender_id or '').strip()
            if per_user_seconds > 0 and normalized_sender:
                last_user = latest_created(" AND sender_id=?", (normalized_sender,))
                if last_user:
                    try:
                        elapsed_user = (now_dt - parse_iso_datetime(last_user)).total_seconds()
                    except Exception:
                        elapsed_user = per_user_seconds + 1
                    if elapsed_user < per_user_seconds:
                        return {'should_respond': False, 'result_code': 'per_user_cooldown_active', 'cooldown_scope': 'group_user', 'remaining_seconds': int(max(1, per_user_seconds - elapsed_user))}
        return {}

    def _matched_group_atmosphere_trigger_result(self, binding: Dict[str, Any], rule: Dict[str, Any], event_payload: Dict[str, Any], *, sender_id: str = '', matched_keyword: str = '', dry_run: bool = False) -> Dict[str, Any]:
        sequence, sequence_meta = self._resolve_group_atmosphere_trigger_sequence(rule)
        event_id = f"gate_{uuid.uuid4().hex[:16]}"
        if not dry_run:
            now = utc_now()
            with self.db.connect() as conn:
                conn.execute(
                    "INSERT INTO whatsapp_group_atmosphere_trigger_events (event_id, rule_id, relationship_key, binding_id, account_key, target_group, sender_id, trigger_type, status, result_code, trigger_payload_json, message_sequence_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'matched', 'trigger_rule_matched', ?, ?, ?)",
                    (event_id, rule.get('rule_id'), rule.get('relationship_key'), binding.get('binding_id'), binding.get('account_key'), binding.get('target_group'), sender_id or '', rule.get('trigger_type'), json.dumps(event_payload or {}, ensure_ascii=False), json.dumps(sequence, ensure_ascii=False), now),
                )
                conn.execute("UPDATE whatsapp_group_atmosphere_trigger_rules SET last_triggered_at=?, updated_at=? WHERE rule_id=?", (now, now, rule.get('rule_id')))
                conn.commit()
        result = {
            'should_respond': True,
            'result_code': 'trigger_rule_matched',
            'trigger_event_id': '' if dry_run else event_id,
            'matched_rule': rule,
            'reply_sequence': sequence,
            'delay_min_seconds': rule.get('delay_min_seconds'),
            'delay_max_seconds': rule.get('delay_max_seconds'),
            'binding_id': binding.get('binding_id'),
            'relationship_key': rule.get('relationship_key'),
            'trigger_type': rule.get('trigger_type'),
            'send_mode': sequence_meta.get('send_mode') or 'sequence',
            'selected_message_index': sequence_meta.get('selected_message_index'),
            'selected_message_position': sequence_meta.get('selected_message_position'),
            'candidate_message_count': sequence_meta.get('candidate_message_count'),
        }
        if matched_keyword:
            result['matched_keyword'] = matched_keyword
        return result

    @staticmethod
    def _group_atmosphere_record_datetime(record: Dict[str, Any]) -> Optional[datetime]:
        for key in ('created_at', 'timestamp', 'timestamp_ms', 'time'):
            raw = (record or {}).get(key)
            if raw is None or raw == '':
                continue
            try:
                if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().replace('.', '', 1).isdigit()):
                    value = float(raw)
                    seconds = value / 1000.0 if value > 100000000000 else value
                    return datetime.fromtimestamp(seconds, tz=timezone.utc)
                parsed = parse_iso_datetime(str(raw))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except Exception:
                continue
        return None

    def _active_group_atmosphere_silence_rules_for_binding(self, binding: Dict[str, Any]) -> List[Dict[str, Any]]:
        role_key = str((binding or {}).get('role_key') or '').strip()
        if not role_key:
            return []
        rules = [
            rule for rule in self.list_group_atmosphere_trigger_rules(role_key).get('rows') or []
            if rule.get('enabled') and rule.get('trigger_type') == 'group_silence'
        ]
        rules.sort(key=lambda rule: int(rule.get('priority') or 999))
        return rules

    def _latest_group_atmosphere_outbound_activity(self, *, account_key: str, target_group: str) -> Dict[str, Any]:
        accepted_states = ('api_accepted', 'runtime_observed', 'readback_missing', 'readback_ambiguous', 'frontend_verified')
        with self.db.connect() as conn:
            row = conn.execute(
                f"""
                SELECT log_id, created_at, trigger_type
                FROM whatsapp_group_atmosphere_logs
                WHERE account_key=? AND target_group=? AND direction='outbound'
                  AND result_code<>'dry_run'
                  AND delivery_state IN ({','.join('?' for _ in accepted_states)})
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(account_key or '').strip(), str(target_group or '').strip(), *accepted_states),
            ).fetchone()
        if not row:
            return {}
        created = self._group_atmosphere_record_datetime({'created_at': row['created_at']})
        if not created:
            return {}
        return {'created_at': created, 'message_id': str(row['log_id'] or ''), 'trigger_type': str(row['trigger_type'] or ''), 'from_me': True, 'source': 'outbound_log'}

    def _group_atmosphere_silence_observe_targets(self, binding: Dict[str, Any]) -> List[str]:
        target_group = str((binding or {}).get('target_group') or '').strip()
        candidates: List[str] = []

        def add(value: Any) -> None:
            text = str(value or '').strip()
            if text and text not in candidates:
                candidates.append(text)

        account_key = str((binding or {}).get('account_key') or '').strip()
        group_index = int((binding or {}).get('group_index') or 0)
        account = self._get_whatsapp_approval_account_row(account_key) if account_key else None
        if account:
            try:
                groups = json.loads(account.get('group_links') or '[]')
            except Exception:
                groups = []
            matched_groups: List[Dict[str, Any]] = []
            if 0 <= group_index < len(groups) and isinstance(groups[group_index], dict):
                matched_groups.append(groups[group_index])
            for group in groups:
                if not isinstance(group, dict):
                    continue
                identifiers = {
                    str(group.get('target_group') or '').strip(),
                    str(group.get('link') or '').strip(),
                    str(group.get('group_id') or '').strip(),
                    str(group.get('runtime_probe_group_id') or '').strip(),
                }
                if target_group and target_group in identifiers and group not in matched_groups:
                    matched_groups.append(group)
            for group in matched_groups:
                add(group.get('group_id'))
                add(group.get('runtime_probe_group_id'))
                add(group.get('target_group'))
                add(group.get('link'))
        add(target_group)
        return candidates

    def _observe_group_atmosphere_binding_silence(self, binding: Dict[str, Any]) -> Dict[str, Any]:
        account_key = str((binding or {}).get('account_key') or '').strip()
        target_group = str((binding or {}).get('target_group') or '').strip()
        if not account_key or not target_group:
            return {'ok': False, 'result_code': 'missing_account_or_group'}
        runtime_resolution = self._resolve_group_atmosphere_send_runtime(
            account_key,
            configured_worker_base_url=(binding or {}).get('worker_base_url'),
        )
        base_url = str(runtime_resolution.get('base_url') or '').strip().rstrip('/')
        if not base_url:
            return {'ok': False, 'result_code': 'group_atmosphere_runtime_not_configured', 'runtime_source': runtime_resolution.get('source')}
        baileys_account_id = (
            str(runtime_resolution.get('baileys_account_id') or self._group_atmosphere_account_baileys_account_id(account_key) or '').strip()
            if runtime_resolution.get('is_baileys')
            else ''
        )
        body: Dict[str, Any] = {}
        last_error: Dict[str, Any] = {}
        observed_target = ''
        observe_targets = self._group_atmosphere_silence_observe_targets(binding)
        for observe_target in observe_targets:
            payload = {
                'target_group': observe_target,
                'limit': 30,
                'metadata': {
                    'account_key': account_key,
                    'group_index': int((binding or {}).get('group_index') or 0),
                    'group_name': str((binding or {}).get('group_name') or target_group),
                    'operation': 'group_silence_observe',
                    'canonical_target_group': target_group,
                },
            }
            if baileys_account_id:
                payload['accountId'] = baileys_account_id
                payload['baileys_account_id'] = baileys_account_id
                payload['metadata']['baileys_account_id'] = baileys_account_id
            try:
                resp = requests.post(f'{base_url}/fetch-group-messages', json=payload, timeout=20)
                try:
                    candidate_body = resp.json()
                except Exception:
                    candidate_body = {'text': getattr(resp, 'text', '')}
            except Exception as exc:
                last_error = {'result_code': 'silence_observe_request_exception', 'result_reason': str(exc)}
                continue
            if int(getattr(resp, 'status_code', 500)) >= 400:
                last_error = {
                    'result_code': str((candidate_body or {}).get('result_code') or (candidate_body or {}).get('result_reason') or 'silence_observe_request_failed'),
                    'result_reason': str((candidate_body or {}).get('result_reason') or ''),
                    'details': dict(candidate_body or {}),
                }
                reason = f"{last_error.get('result_code')} {last_error.get('result_reason') or ''}".lower()
                if 'group not found' in reason or 'group_not_found' in reason:
                    continue
                return {**last_error, 'ok': False, 'runtime_source': runtime_resolution.get('source')}
            body = dict(candidate_body or {})
            observed_target = observe_target
            if list(body.get('records') or []):
                break
        if not body and last_error:
            return {**last_error, 'ok': False, 'runtime_source': runtime_resolution.get('source')}
        latest_record: Dict[str, Any] = {}
        latest_at: Optional[datetime] = None
        regular_record_count = 0
        member_record_count = 0
        for raw_record in list((body or {}).get('records') or []):
            if not isinstance(raw_record, dict):
                continue
            if not self.is_group_atmosphere_regular_group_message(raw_record):
                continue
            regular_record_count += 1
            if bool(raw_record.get('from_me')):
                continue
            member_record_count += 1
            created = self._group_atmosphere_record_datetime(raw_record)
            if not created:
                continue
            if latest_at is None or created > latest_at:
                latest_at = created
                latest_record = dict(raw_record)
        outbound_activity = self._latest_group_atmosphere_outbound_activity(account_key=account_key, target_group=target_group)
        outbound_at = outbound_activity.get('created_at')
        now_dt = datetime.now(timezone.utc)
        if isinstance(outbound_at, datetime) and latest_at is not None and outbound_at >= latest_at:
            return {
                'ok': False,
                'result_code': 'silence_already_addressed_by_bot',
                'result_reason': 'latest bot message is newer than latest member message',
                'details': {
                    'record_count': len(list((body or {}).get('records') or [])),
                    'regular_record_count': regular_record_count,
                    'member_record_count': member_record_count,
                    'latest_member_message_at': latest_at.isoformat() if latest_at else '',
                    'latest_outbound_at': outbound_at.isoformat(),
                    'latest_outbound_message_id': outbound_activity.get('message_id') or '',
                    'observe_targets': observe_targets,
                    'observed_target': observed_target,
                },
                'runtime_source': runtime_resolution.get('source'),
            }
        if latest_at is None:
            return {
                'ok': False,
                'result_code': 'silence_activity_unavailable',
                'result_reason': 'runtime returned no ordinary group messages',
                'details': {'record_count': len(list((body or {}).get('records') or [])), 'regular_record_count': regular_record_count, 'member_record_count': member_record_count, 'observe_targets': observe_targets, 'observed_target': observed_target},
                'runtime_source': runtime_resolution.get('source'),
            }
        silence_seconds = max(0, int((now_dt - latest_at).total_seconds()))
        return {
            'ok': True,
            'silence_seconds': silence_seconds,
            'last_message_at': latest_at.isoformat(),
            'last_message_id': str(latest_record.get('message_id') or latest_record.get('id') or ''),
            'last_message_from_me': bool(latest_record.get('from_me')),
            'runtime_source': runtime_resolution.get('source'),
            'provider_name': runtime_resolution.get('provider_name'),
            'provider_mode': runtime_resolution.get('provider_mode'),
            'baileys_account_id': baileys_account_id,
            'observed_target': observed_target,
        }

    def _dispatch_group_atmosphere_trigger_sequence_for_binding(self, binding: Dict[str, Any], trigger_result: Dict[str, Any], *, event_payload: Dict[str, Any]) -> Dict[str, Any]:
        sequence = [dict(item or {}) for item in list((trigger_result or {}).get('reply_sequence') or []) if isinstance(item, dict)]
        if not sequence:
            return {'sent': False, 'accepted': False, 'binding_id': binding.get('binding_id'), 'role_key': binding.get('role_key'), 'target_group': binding.get('target_group'), 'trigger_type': trigger_result.get('trigger_type'), 'result_code': 'trigger_sequence_empty', 'result_reason': 'trigger rule has no message sequence'}
        account_key = str(binding.get('account_key') or '').strip()
        target_group = str(binding.get('target_group') or '').strip()
        runtime_resolution = self._resolve_group_atmosphere_send_runtime(account_key, configured_worker_base_url=binding.get('worker_base_url'))
        base_url = str(runtime_resolution.get('base_url') or '').strip().rstrip('/')
        if not base_url:
            result_code = 'baileys_runtime_not_configured' if runtime_resolution.get('is_baileys') else 'worker_base_url_not_configured'
            return {'sent': False, 'accepted': False, 'binding_id': binding.get('binding_id'), 'role_key': binding.get('role_key'), 'target_group': target_group, 'trigger_type': trigger_result.get('trigger_type'), 'result_code': result_code, 'result_reason': 'group atmosphere runtime is not configured'}
        baileys_account_id = (
            str(runtime_resolution.get('baileys_account_id') or self._group_atmosphere_account_baileys_account_id(account_key) or '').strip()
            if runtime_resolution.get('is_baileys')
            else ''
        )
        trigger_type = str(trigger_result.get('trigger_type') or 'group_silence')
        trigger_started_at = utc_now()
        results: List[Dict[str, Any]] = []
        for idx, segment in enumerate(sequence):
            delay_seconds = max(0, int(float(segment.get('delay_seconds') or 0)))
            if delay_seconds > 0:
                time.sleep(min(delay_seconds, 30))
            message_text = self._format_group_atmosphere_outbound_message_text(segment.get('text') or '')
            media_payload = {
                key: segment.get(key)
                for key in ['media_id', 'media_path', 'media_mime_type', 'media_filename']
                if segment.get(key)
            }
            delivery = self._execute_group_atmosphere_worker_send(
                base_url=base_url,
                target_group=target_group,
                account_key=account_key,
                group_index=int(binding.get('group_index') or 0),
                group_name=str(binding.get('group_name') or target_group),
                trigger_type=trigger_type,
                message_text=message_text,
                media_payload=media_payload,
                scheduled_at=f"{trigger_started_at}:{trigger_result.get('trigger_event_id') or ''}:{idx}",
                baileys_account_id=baileys_account_id,
            )
            self._log_group_atmosphere_event(
                config_name=f"binding-{binding.get('binding_id')}",
                account_key=account_key,
                target_group=target_group,
                direction='outbound',
                trigger_type=trigger_type,
                message_text=message_text,
                status=str(delivery.get('status') or 'unknown'),
                result_code=str(delivery.get('result_code') or ''),
                result_reason=str(delivery.get('result_reason') or ''),
                raw_result={
                    **dict(delivery.get('raw_result') or {}),
                    'trigger_event_id': trigger_result.get('trigger_event_id') or '',
                    'trigger_rule_id': (trigger_result.get('matched_rule') or {}).get('rule_id') or '',
                    'trigger_payload': dict(event_payload or {}),
                    'segment_index': idx,
                    'segment_count': len(sequence),
                },
                delivery_state=str(delivery.get('delivery_state') or 'unknown'),
                evidence_level=str(delivery.get('evidence_level') or 'none'),
                frontend_verified=False,
                client_send_key=str(delivery.get('client_send_key') or ''),
                legacy_status=str(delivery.get('legacy_status') or ''),
                legacy_result_code=str(delivery.get('legacy_result_code') or ''),
                legacy_message_id=str(delivery.get('legacy_message_id') or ''),
                migration_note=str(delivery.get('migration_note') or ''),
                preflight_status=str(delivery.get('preflight_status') or ''),
                preflight_reason=str(delivery.get('preflight_reason') or ''),
                preflight_details=dict(delivery.get('preflight_details') or {}),
                readback_matched=bool(delivery.get('readback_matched')),
                readback_match_reason=str(delivery.get('readback_match_reason') or ''),
                readback_message_id=str(delivery.get('readback_message_id') or ''),
                readback_text=str(delivery.get('readback_text') or ''),
                readback_timestamp=str(delivery.get('readback_timestamp') or ''),
                readback_attempt_count=int(delivery.get('readback_attempt_count') or 0),
            )
            results.append({
                'segment_index': idx,
                'sent': bool(delivery.get('sent')),
                'accepted': bool(delivery.get('accepted')),
                'delivery_state': str(delivery.get('delivery_state') or 'unknown'),
                'evidence_level': str(delivery.get('evidence_level') or 'none'),
                'result_code': str(delivery.get('result_code') or ''),
                'result_reason': str(delivery.get('result_reason') or ''),
                'message_text': message_text,
                'raw_result': dict(delivery.get('raw_result') or {}),
            })
        accepted_count = sum(1 for item in results if item.get('accepted'))
        sent_count = sum(1 for item in results if item.get('sent'))
        if accepted_count:
            now = utc_now()
            today = _group_atmosphere_business_date()
            next_due_at = self._next_group_atmosphere_due_at(binding)
            trusted_sent_count_today = int(binding.get('sent_count_today') or 0) if binding.get('sent_count_date') == today else 0
            accepted_sent_count_today = self._accepted_group_atmosphere_binding_send_count_today(
                binding_id=str(binding.get('binding_id') or ''),
                account_key=account_key,
                target_group=target_group,
            )
            with self.db.connect() as conn:
                for item in results:
                    worker_message_id = str((item.get('raw_result') or {}).get('message_id') or ((item.get('raw_result') or {}).get('raw_result') or {}).get('message_id') or '').strip()
                    if not worker_message_id:
                        continue
                    self.write_event_ledger(
                        event_type='group_message_sent',
                        object_type='group_atmosphere_binding',
                        object_key=str(binding.get('binding_id') or ''),
                        status='success' if item.get('sent') else 'accepted',
                        evidence_level=str(item.get('evidence_level') or 'none'),
                        external_id=worker_message_id,
                        payload={
                            'binding_id': binding.get('binding_id'),
                            'role_key': binding.get('role_key'),
                            'account_key': account_key,
                            'target_group': target_group,
                            'group_name': binding.get('group_name'),
                            'trigger_type': trigger_type,
                            'trigger_event_id': trigger_result.get('trigger_event_id') or '',
                            'message_text': item.get('message_text') or '',
                            'accepted_by_worker': bool(item.get('accepted')),
                            'delivery_state': str(item.get('delivery_state') or 'unknown'),
                        },
                        conn=conn,
                    )
                conn.execute(
                    "UPDATE whatsapp_group_atmosphere_role_bindings SET last_sent_at=?, sent_count_today=?, sent_count_date=?, next_due_at=?, updated_at=? WHERE binding_id=?",
                    (now, max(trusted_sent_count_today + accepted_count, accepted_sent_count_today), today, next_due_at, now, binding.get('binding_id')),
                )
                conn.commit()
        return {
            'sent': sent_count > 0,
            'accepted': accepted_count > 0,
            'binding_id': binding.get('binding_id'),
            'role_key': binding.get('role_key'),
            'target_group': target_group,
            'group_name': binding.get('group_name'),
            'trigger_type': trigger_type,
            'trigger_event_id': trigger_result.get('trigger_event_id') or '',
            'matched_rule': trigger_result.get('matched_rule') or {},
            'result_code': 'trigger_sequence_sent' if accepted_count else (results[0].get('result_code') if results else 'trigger_sequence_failed'),
            'result_reason': '' if accepted_count else (results[0].get('result_reason') if results else 'trigger sequence failed'),
            'segment_count': len(sequence),
            'sent_count': sent_count,
            'accepted_count': accepted_count,
            'results': results,
        }

    def _dispatch_due_group_atmosphere_silence_trigger_for_binding(self, binding: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self._active_group_atmosphere_silence_rules_for_binding(binding):
            return None
        observed = self._observe_group_atmosphere_binding_silence(binding)
        if not observed.get('ok'):
            return None
        event_payload = {
            'silence_seconds': int(observed.get('silence_seconds') or 0),
            'last_message_at': observed.get('last_message_at') or '',
            'last_message_id': observed.get('last_message_id') or '',
            'last_message_from_me': bool(observed.get('last_message_from_me')),
            'source': 'backend_scheduler_runtime_history',
            'runtime_source': observed.get('runtime_source') or '',
            'provider_name': observed.get('provider_name') or '',
            'provider_mode': observed.get('provider_mode') or '',
        }
        trigger_result = self.evaluate_group_atmosphere_trigger_rules_for_event(
            binding,
            trigger_type='group_silence',
            event_payload=event_payload,
            dry_run=False,
        )
        if not trigger_result.get('should_respond'):
            return None
        dispatched = self._dispatch_group_atmosphere_trigger_sequence_for_binding(
            binding,
            trigger_result,
            event_payload=event_payload,
        )
        dispatched['silence_seconds'] = int(event_payload['silence_seconds'])
        dispatched['last_message_at'] = event_payload['last_message_at']
        return dispatched

    def evaluate_group_atmosphere_trigger_rules_for_event(self, binding: Dict[str, Any], *, trigger_type: str, event_payload: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
        if not binding:
            return {'should_respond': False, 'result_code': 'role_binding_not_found'}
        if not binding.get('group_send_permission_enabled'):
            return {'should_respond': False, 'result_code': 'group_send_permission_disabled'}
        if not binding.get('trigger_speaking_enabled'):
            return {'should_respond': False, 'result_code': 'trigger_speaking_disabled'}
        if trigger_type == 'member_join':
            action = str((event_payload or {}).get('action') or '').strip().lower()
            if action and action not in {'add', 'join', 'joined', 'member_join', 'participant_add', 'participants_add', 'invite'}:
                return {'should_respond': False, 'result_code': 'member_join_action_ignored', 'event_action': action}
        rules = [rule for rule in self.list_group_atmosphere_trigger_rules(str(binding.get('role_key') or '')).get('rows') or [] if rule.get('enabled') and rule.get('trigger_type') == trigger_type]
        if not rules:
            return {'should_respond': False, 'result_code': 'trigger_rule_not_matched'}
        if trigger_type == 'keyword_match':
            text = str((event_payload or {}).get('text') or '')
            matched: List[tuple[Dict[str, Any], str]] = []
            for rule in rules:
                ok, keyword = self._keyword_trigger_rule_matches(rule, text)
                if ok:
                    matched.append((rule, keyword))
            if not matched:
                return {'should_respond': False, 'result_code': 'trigger_rule_not_matched'}
            matched.sort(key=lambda pair: (int(pair[0].get('priority') or 999), -len(pair[1])))
            rule, keyword = matched[0]
            sender_id = str((event_payload or {}).get('sender_id') or '')
            if not dry_run:
                cooldown = self._group_atmosphere_trigger_cooldown_state(binding, rule, sender_id=sender_id)
                if cooldown:
                    cooldown.update({'matched_rule': rule, 'binding_id': binding.get('binding_id'), 'relationship_key': rule.get('relationship_key'), 'trigger_type': rule.get('trigger_type'), 'matched_keyword': keyword})
                    return cooldown
            return self._matched_group_atmosphere_trigger_result(binding, rule, {'text': text, 'matched_keyword': keyword}, sender_id=sender_id, matched_keyword=keyword, dry_run=dry_run)
        rules.sort(key=lambda rule: int(rule.get('priority') or 999))
        rule = rules[0]
        if trigger_type == 'group_silence':
            conditions = rule.get('conditions') or {}
            observed_raw = (event_payload or {}).get('silence_seconds')
            if observed_raw is None or observed_raw == '':
                observed_raw = conditions.get('silence_seconds') or 0
            observed = int(observed_raw or 0)
            required = int(conditions.get('silence_seconds') or 60)
            if observed < required:
                return {'should_respond': False, 'result_code': 'silence_threshold_not_reached', 'required_silence_seconds': required, 'observed_silence_seconds': observed}
        sender_id = str((event_payload or {}).get('sender_id') or '')
        if not dry_run:
            cooldown = self._group_atmosphere_trigger_cooldown_state(binding, rule, sender_id=sender_id)
            if cooldown:
                cooldown.update({'matched_rule': rule, 'binding_id': binding.get('binding_id'), 'relationship_key': rule.get('relationship_key'), 'trigger_type': rule.get('trigger_type')})
                return cooldown
        return self._matched_group_atmosphere_trigger_result(binding, rule, event_payload or {}, sender_id=sender_id, dry_run=dry_run)

    def evaluate_group_atmosphere_trigger_rules_for_inbound(self, payload: GroupAtmosphereInboundMessageRequest) -> Dict[str, Any]:
        if not self.is_group_atmosphere_regular_group_message(payload.dict()):
            return {'should_respond': False, 'result_code': 'system_message_ignored'}
        binding = self._find_group_atmosphere_binding_for_event(payload.account_key, payload.target_group)
        return self.evaluate_group_atmosphere_trigger_rules_for_event(binding or {}, trigger_type='keyword_match', event_payload={'text': payload.text, 'sender_id': payload.sender_id}, dry_run=False)

    def _dispatch_group_atmosphere_binding_once(self, binding: Dict[str, Any], *, trigger_type: str = 'scheduled_auto', require_auto_enabled: bool = True) -> Dict[str, Any]:
        if not binding.get('enabled'):
            return {'sent': False, 'binding_id': binding.get('binding_id'), 'role_key': binding.get('role_key'), 'target_group': binding.get('target_group'), 'result_code': 'binding_disabled', 'result_reason': '桥接已停用'}
        if require_auto_enabled and not binding.get('auto_speaking_enabled'):
            return {'sent': False, 'binding_id': binding.get('binding_id'), 'role_key': binding.get('role_key'), 'target_group': binding.get('target_group'), 'result_code': 'auto_speaking_disabled', 'result_reason': '自动发言已暂停'}
        if not binding.get('group_send_permission_enabled'):
            return {'sent': False, 'binding_id': binding.get('binding_id'), 'role_key': binding.get('role_key'), 'target_group': binding.get('target_group'), 'result_code': 'group_send_permission_disabled', 'result_reason': '群发送权限关闭'}
        base_role_key = str(binding.get('role_key') or '').strip()
        active_strategy, schedule_reason = self._active_group_atmosphere_binding_schedule_strategy(binding)
        if not active_strategy:
            if require_auto_enabled:
                return {'sent': False, 'binding_id': binding.get('binding_id'), 'role_key': binding.get('role_key'), 'target_group': binding.get('target_group'), 'result_code': 'schedule_strategy_inactive', 'result_reason': schedule_reason}
            strategies = self._normalize_group_atmosphere_binding_schedule_strategies(
                binding.get('schedule_strategies'),
                fallback=binding,
                validate_roles=False,
            )
            active_strategy = next((item for item in strategies if item.get('enabled') is not False and item.get('role_key')), None)
        binding = self._apply_group_atmosphere_binding_schedule_strategy(binding, active_strategy)
        role = self._get_group_atmosphere_config(str(binding.get('role_key') or '').strip())
        if not role:
            return {'sent': False, 'binding_id': binding.get('binding_id'), 'role_key': binding.get('role_key'), 'target_group': binding.get('target_group'), 'result_code': 'role_not_found', 'result_reason': '话术角色不存在'}
        if role and any(dict(x or {}).get('role_deleted_at') for x in list(role.get('template_pool') or [])):
            return {'sent': False, 'binding_id': binding.get('binding_id'), 'role_key': binding.get('role_key'), 'target_group': binding.get('target_group'), 'result_code': 'role_deleted', 'result_reason': '话术角色已删除，请编辑桥接更换角色'}
        templates = self._enabled_group_atmosphere_templates(role)
        if not templates:
            return {'sent': False, 'binding_id': binding.get('binding_id'), 'role_key': binding.get('role_key'), 'target_group': binding.get('target_group'), 'result_code': 'template_pool_empty', 'result_reason': '话术角色没有已启用话术'}
        today = _group_atmosphere_business_date()
        trusted_sent_count_today = int(binding.get('sent_count_today') or 0) if binding.get('sent_count_date') == today else 0
        accepted_sent_count_today = self._accepted_group_atmosphere_binding_send_count_today(
            binding_id=str(binding.get('binding_id') or '').strip(),
            account_key=str(binding.get('account_key') or '').strip(),
            target_group=str(binding.get('target_group') or '').strip(),
        )
        sent_count_today = max(trusted_sent_count_today, accepted_sent_count_today)
        due, due_reason = self._group_atmosphere_config_due_now(binding)
        if require_auto_enabled and not due:
            return {'sent': False, 'binding_id': binding.get('binding_id'), 'role_key': binding.get('role_key'), 'target_group': binding.get('target_group'), 'result_code': 'not_due_yet', 'result_reason': due_reason}
        binding_runtime_resolution = self._resolve_group_atmosphere_send_runtime(
            str(binding.get('account_key') or '').strip(),
            configured_worker_base_url=binding.get('worker_base_url'),
        )
        runtime_base = '' if binding_runtime_resolution.get('is_baileys') else str(binding.get('worker_base_url') or '').strip().rstrip('/')
        template_pool_for_dispatch = self._refresh_group_atmosphere_templates_with_latest_media([dict(item or {}) for item in list(role.get('template_pool') or [])])
        sorted_message_text = ''
        if str(binding.get('phrase_send_order') or 'random') == 'sorted':
            enabled_templates_for_order = self._enabled_group_atmosphere_templates({'template_pool': template_pool_for_dispatch})
            has_role_order = any(int(item.get('role_selection_order') or 0) > 0 for item in enabled_templates_for_order)
            if has_role_order:
                def sorted_template_key(pair):
                    index, item = pair
                    role_order = int(item.get('role_selection_order') or 0)
                    sort_order = int(item.get('sort_order') or 0)
                    return (0 if role_order > 0 else 1, role_order if role_order > 0 else sort_order, index)
                enabled_templates_for_order = [item for _, item in sorted(enumerate(enabled_templates_for_order), key=sorted_template_key)]
            if enabled_templates_for_order:
                sorted_index = sent_count_today % len(enabled_templates_for_order)
                sorted_message_text = str(enabled_templates_for_order[sorted_index].get('text') or '').strip()
        config = dict(role)
        config.update({
            'config_name': f"binding-{binding.get('binding_id')}",
            'enabled': True,
            'status': 'enabled',
            'account_key': binding.get('account_key'),
            'target_group': binding.get('target_group'),
            'group_name': binding.get('group_name'),
            'worker_base_url': runtime_base,
            'daily_max_messages': binding.get('daily_max_messages'),
            'min_interval_seconds': binding.get('min_interval_seconds'),
            'max_interval_seconds': binding.get('max_interval_seconds'),
            'min_interval_minutes': binding.get('min_interval_minutes'),
            'max_interval_minutes': binding.get('max_interval_minutes'),
            'allowed_windows': binding.get('allowed_windows') or [],
            'timezone': 'Asia/Shanghai',
            'last_sent_at': binding.get('last_sent_at'),
            'sent_count_today': sent_count_today,
            'sent_count_date': today,
            'template_pool': template_pool_for_dispatch,
        })
        self.upsert_group_atmosphere_config(GroupAtmosphereConfigRequest(
            config_name=config['config_name'], enabled=True, account_key=str(config['account_key']), target_group=str(config['target_group']),
            group_name=str(config.get('group_name') or ''), language=str(config.get('language') or 'id'), timezone=str(config.get('timezone') or 'UTC'),
            worker_base_url=runtime_base,
            daily_max_messages=int(config.get('daily_max_messages') if config.get('daily_max_messages') is not None else 0),
            min_interval_seconds=_group_atmosphere_mapping_interval_seconds(config, 'min_interval_seconds', 'min_interval_minutes', 0),
            max_interval_seconds=_group_atmosphere_mapping_interval_seconds(config, 'max_interval_seconds', 'max_interval_minutes', 240),
            allowed_windows=list(config.get('allowed_windows') or []),
            template_pool=[GroupAtmosphereTemplate(**item) for item in list(config.get('template_pool') or [])],
            mention_reply_enabled=bool(config.get('mention_reply_enabled')), faq_rules=[GroupAtmosphereFaqRule(**item) for item in list(config.get('faq_rules') or [])], status='enabled'
        ))
        result = self.dispatch_group_atmosphere_once(GroupAtmosphereDispatchRequest(config_name=config['config_name'], trigger_type=trigger_type, message_text=sorted_message_text or None))
        result.update({'binding_id': binding.get('binding_id'), 'role_key': binding.get('role_key'), 'base_role_key': base_role_key, 'target_group': binding.get('target_group'), 'group_name': binding.get('group_name'), 'schedule_strategy': active_strategy or {}})
        if result.get('accepted') and not result.get('dry_run'):
            now = utc_now()
            next_due_at = self._next_group_atmosphere_due_at(binding)
            worker_message_id = str((result.get('raw_result') or {}).get('message_id') or ((result.get('raw_result') or {}).get('raw_result') or {}).get('message_id') or '').strip()
            with self.db.connect() as conn:
                if worker_message_id:
                    self.write_event_ledger(
                        event_type='group_message_sent',
                        object_type='group_atmosphere_binding',
                        object_key=str(binding.get('binding_id') or ''),
                        status='success' if result.get('sent') else 'accepted',
                        evidence_level=str(result.get('evidence_level') or 'none'),
                        external_id=worker_message_id,
                        payload={
                            'binding_id': binding.get('binding_id'),
                            'role_key': binding.get('role_key'),
                            'base_role_key': base_role_key,
                            'schedule_strategy': active_strategy or {},
                            'account_key': binding.get('account_key'),
                            'target_group': binding.get('target_group'),
                            'group_name': binding.get('group_name'),
                            'trigger_type': trigger_type,
                            'message_text': result.get('message_text') or '',
                            'accepted_by_worker': bool(result.get('accepted')),
                            'delivery_state': str(result.get('delivery_state') or 'unknown'),
                            'readback_matched': bool(result.get('readback_matched')),
                        },
                        conn=conn,
                    )
                if result.get('sent'):
                    conn.execute("UPDATE whatsapp_group_atmosphere_role_bindings SET last_sent_at=?, sent_count_today=?, sent_count_date=?, next_due_at=?, updated_at=? WHERE binding_id=?", (now, sent_count_today + 1, today, next_due_at, now, binding.get('binding_id')))
                else:
                    conn.execute("UPDATE whatsapp_group_atmosphere_role_bindings SET next_due_at=?, updated_at=? WHERE binding_id=?", (next_due_at, now, binding.get('binding_id')))
                conn.commit()
        return result

    def trigger_group_atmosphere_role_binding(self, binding_id: str) -> Dict[str, Any]:
        binding = self.get_group_atmosphere_role_binding(binding_id)
        return self._dispatch_group_atmosphere_binding_once(binding, trigger_type='manual_role_bridge', require_auto_enabled=False)

    def upsert_group_atmosphere_learning_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        region = str((payload or {}).get('region') or '印尼').strip()
        language_hint = str((payload or {}).get('language') or self._group_atmosphere_language_from_region(region)).strip()
        key = str((payload or {}).get('learning_account_key') or (payload or {}).get('account_key') or '').strip()
        if not key:
            key = self._next_group_atmosphere_learning_account_key(region=region, language=language_hint)
        language = str((payload or {}).get('language') or self._group_atmosphere_language_from_region(region)).strip()
        groups = list((payload or {}).get('group_links') or (payload or {}).get('groups') or [])
        if len(groups) > 10:
            raise HTTPException(status_code=400, detail='each group atmosphere learning account can manage at most 10 groups')
        roles = [str(item or '').strip() for item in list((payload or {}).get('target_role_keys') or []) if str(item or '').strip()]
        if not roles:
            fallback_role = self._resolve_group_atmosphere_phrase_type_key('', required=True)
            roles = [f'auto-{language_hint or "id"}-{fallback_role}']
        now = utc_now()
        account_name = str((payload or {}).get('account_name') or key).strip()
        runtime_config = _whatsapp_approval_runtime_config_from_dict(payload or {})
        explicit_runtime_mode = str(
            runtime_config.get('provider_mode')
            or runtime_config.get('group_assistant_runtime')
            or ''
        ).strip().lower()
        configured_worker_base_url = str((payload or {}).get('worker_base_url') or '').strip().rstrip('/')
        if (
            not explicit_runtime_mode
            and configured_worker_base_url
            and not _is_legacy_shared_webjs_8787_url(configured_worker_base_url)
        ):
            explicit_runtime_mode = 'legacy_only'
            runtime_config['provider_mode'] = 'legacy_only'
            runtime_config['group_assistant_runtime'] = 'legacy_only'
        baileys_account_id = self._resolve_baileys_runtime_value(
            runtime_config,
            payload or {},
            keys=['baileys_account_id', 'provider_account_id', 'account_id'],
        ) or _default_baileys_account_id_for_whatsapp_account(key)
        runtime_defaults = dict(runtime_config)
        if explicit_runtime_mode != 'legacy_only':
            runtime_defaults = _apply_baileys_runtime_assignment_defaults(
                runtime_defaults,
                responsible_type='group_atmosphere_learning',
                baileys_account_id=baileys_account_id,
            )
        normalized_groups = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            target = str(group.get('target_group') or group.get('group_id') or group.get('link') or group.get('group_name') or '').strip()
            if not target:
                continue
            normalized_group = {
                'target_group': target,
                'group_id': str(group.get('group_id') or '').strip(),
                'link': str(group.get('link') or '').strip(),
                'group_name': str(group.get('group_name') or target).strip(),
                'enabled': False if group.get('enabled') is False else True,
                'language': language,
                'area': region,
            }
            if explicit_runtime_mode != 'legacy_only':
                normalized_group = _apply_baileys_runtime_assignment_defaults(
                    normalized_group,
                    responsible_type='group_atmosphere_learning',
                    baileys_account_id=baileys_account_id,
                )
            elif runtime_defaults:
                normalized_group.update({
                    key: value
                    for key, value in runtime_defaults.items()
                    if key in WHATSAPP_APPROVAL_RUNTIME_CONFIG_KEYS and str(value or '').strip()
                })
            for cursor_key in ('last_learned_message_id', 'last_learned_message_at', 'last_learned_cursor_at'):
                cursor_value = str(group.get(cursor_key) or '').strip()
                if cursor_value:
                    normalized_group[cursor_key] = cursor_value
            normalized_groups.append(normalized_group)
        metadata = {
            'feature': 'group_atmosphere_learning',
            'region': region,
            'language': language,
            'silent_learning_only': True,
            'max_messages_per_run': int((payload or {}).get('max_messages_per_run') or 300),
        }
        metadata.update({
            key: value
            for key, value in runtime_defaults.items()
            if key in WHATSAPP_APPROVAL_RUNTIME_CONFIG_KEYS and (value is not None) and str(value).strip()
        })
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO whatsapp_group_atmosphere_learning_accounts (learning_account_key, account_name, region, language, enabled, group_links, target_role_keys, daily_learning_time, read_recent_hours, max_messages_per_run, worker_base_url, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'silent_learning', ?, ?) ON CONFLICT(learning_account_key) DO UPDATE SET account_name=excluded.account_name, region=excluded.region, language=excluded.language, enabled=excluded.enabled, group_links=excluded.group_links, target_role_keys=excluded.target_role_keys, daily_learning_time=excluded.daily_learning_time, read_recent_hours=excluded.read_recent_hours, max_messages_per_run=excluded.max_messages_per_run, worker_base_url=excluded.worker_base_url, status=excluded.status, updated_at=excluded.updated_at""",
                (key, account_name, region, language, 0 if (payload or {}).get('enabled') is False else 1, json.dumps(normalized_groups, ensure_ascii=False), json.dumps(roles, ensure_ascii=False), str((payload or {}).get('daily_learning_time') or '03:00'), int((payload or {}).get('read_recent_hours') or 24), int((payload or {}).get('max_messages_per_run') or 300), self._validate_group_atmosphere_worker_base_url((payload or {}).get('worker_base_url')), now, now),
            )
            conn.execute(
                """
                INSERT INTO whatsapp_approval_accounts (
                    account_key, account_name, responsible_type, group_links, area, notify_profile_name,
                    approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker,
                    schedule_windows, enabled, verification_status, notes, created_at, updated_at
                ) VALUES (?, ?, 'group_atmosphere_learning', ?, ?, '', 'silent_learning', ?, ?, 1, ?, ?, 'pending_login', ?, ?, ?)
                ON CONFLICT(account_key) DO UPDATE SET
                    account_name=excluded.account_name,
                    responsible_type='group_atmosphere_learning',
                    group_links=excluded.group_links,
                    area=excluded.area,
                    enabled=excluded.enabled,
                    verification_status='pending_login',
                    notes=excluded.notes,
                    created_at=COALESCE(NULLIF(whatsapp_approval_accounts.created_at, ''), excluded.created_at),
                    updated_at=excluded.updated_at
                """,
                (
                    key,
                    account_name,
                    json.dumps(normalized_groups, ensure_ascii=False),
                    region,
                    WHATSAPP_APPROVAL_DEFAULT_COUNT_THRESHOLD,
                    WHATSAPP_APPROVAL_DEFAULT_TIMEOUT_MINUTES,
                    json.dumps([], ensure_ascii=False),
                    1 if (payload or {}).get('enabled') is not False else 0,
                    json.dumps(metadata, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
        return {'ok': True, 'account': self.get_group_atmosphere_learning_account(key)}

    def _row_to_group_atmosphere_learning_account(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        return {
            'learning_account_key': item.get('learning_account_key'),
            'account_key': item.get('learning_account_key'),
            'account_name': item.get('account_name'),
            'responsible_type': 'group_atmosphere_learning',
            'region': item.get('region'),
            'language': item.get('language'),
            'enabled': bool(item.get('enabled')),
            'group_links': json.loads(item.get('group_links') or '[]'),
            'target_role_keys': json.loads(item.get('target_role_keys') or '[]'),
            'daily_learning_time': item.get('daily_learning_time'),
            'read_recent_hours': int(item.get('read_recent_hours') or 24),
            'max_messages_per_run': int(item.get('max_messages_per_run') or 300),
            'worker_base_url': item.get('worker_base_url') or '',
            'status': item.get('status'),
            'silent_learning_only': True,
            'last_learned_at': item.get('last_learned_at'),
            'last_result_summary': json.loads(item.get('last_result_summary') or '{}'),
            'updated_at': item.get('updated_at'),
        }

    def get_group_atmosphere_learning_account(self, key: str) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM whatsapp_group_atmosphere_learning_accounts WHERE learning_account_key=?", (str(key or '').strip(),)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='learning_account_not_found')
        return self._row_to_group_atmosphere_learning_account(row)

    def list_group_atmosphere_learning_accounts(self) -> Dict[str, Any]:
        """List learning bots from cached runtime/session snapshots only.

        Opening the group-atmosphere page must not amplify WhatsApp worker health checks
        or group probes. Live refresh is available through explicit session/learn actions.
        """
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM whatsapp_group_atmosphere_learning_accounts
                ORDER BY COALESCE(NULLIF(created_at, ''), updated_at) ASC, learning_account_key ASC
                """
            ).fetchall()
        output = []
        for row in rows:
            item = self._row_to_group_atmosphere_learning_account(row)
            account_key = str(item.get('learning_account_key') or item.get('account_key') or '').strip()
            runtime_state, session_state = self._build_group_atmosphere_account_runtime_display_state(
                account_key,
                account_enabled=bool(item.get('enabled')),
                skip_health_check=not self._group_atmosphere_allow_test_worker_urls,
                allow_live_test_health=self._group_atmosphere_allow_test_worker_urls,
            ) if account_key else ({}, {})
            item['runtime'] = runtime_state
            base_url = str(runtime_state.get('base_url') or '').strip()
            if self._group_atmosphere_allow_test_worker_urls and account_key and bool(session_state.get('login_verified')) and base_url:
                item['groups'] = self._probe_group_atmosphere_actual_group_names(
                    account_key=account_key,
                    row={'group_links': json.dumps(item.get('group_links') or item.get('groups') or [], ensure_ascii=False)},
                    base_url=base_url,
                    session_state=session_state,
                    responsible_type='group_atmosphere_learning',
                )
                item['group_links'] = item['groups']
            item['session'] = session_state
            item['session_state'] = session_state
            item['login_verified'] = bool(session_state.get('login_verified'))
            item['login_check_status'] = session_state.get('login_check_status') or ''
            item['login_check_message'] = session_state.get('login_check_message') or ''
            item['list_mode'] = 'snapshot'
            output.append(item)
        return {'rows': output, 'count': len(output), 'list_mode': 'snapshot'}

    def delete_group_atmosphere_learning_account(self, key: str) -> Dict[str, Any]:
        key = str(key or '').strip()
        if not key:
            raise HTTPException(status_code=400, detail='missing_learning_account_key')
        with self.db.connect() as conn:
            existing = conn.execute("SELECT learning_account_key FROM whatsapp_group_atmosphere_learning_accounts WHERE learning_account_key=?", (key,)).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail='learning_account_not_found')
            conn.execute("DELETE FROM whatsapp_group_atmosphere_learning_accounts WHERE learning_account_key=?", (key,))
            conn.execute("DELETE FROM whatsapp_approval_accounts WHERE account_key=? AND responsible_type='group_atmosphere_learning'", (key,))
            conn.commit()
        return {'ok': True, 'learning_account_key': key, 'deleted': True}

    @staticmethod
    def _parse_group_atmosphere_learning_time(value: str) -> tuple[int, int]:
        raw = str(value or '').strip()
        match = re.match(r'^(\d{1,2}):(\d{2})$', raw)
        if match:
            hour = min(23, max(0, int(match.group(1))))
            minute = min(59, max(0, int(match.group(2))))
            return hour, minute
        return 3, 0

    def _group_atmosphere_learning_due_now(self, account: Dict[str, Any]) -> tuple[bool, str]:
        if not account.get('enabled'):
            return False, 'learning disabled'
        now = datetime.now(timezone.utc)
        last = str(account.get('last_learned_at') or '').strip()
        if last:
            try:
                last_at = parse_iso_datetime(last)
                elapsed = now - last_at
                if elapsed < timedelta(hours=6):
                    minutes = max(1, int((timedelta(hours=6) - elapsed).total_seconds() // 60) + 1)
                    return False, f'next learning in about {minutes} minutes'
            except Exception:
                pass
        return True, 'six-hour learning due'

    def run_due_group_atmosphere_learning_scheduler(self, limit: int = 20) -> Dict[str, Any]:
        rows = [row for row in self.list_group_atmosphere_learning_accounts().get('rows') or [] if row.get('enabled') is True]
        results = []
        for account in rows:
            if len(results) >= int(limit or 20):
                break
            key = str(account.get('learning_account_key') or account.get('account_key') or '').strip()
            due, reason = self._group_atmosphere_learning_due_now(account)
            if not due:
                results.append({'learning_account_key': key, 'learned': False, 'result_code': 'not_due', 'result_reason': reason})
                continue
            try:
                learned = self.learn_once_group_atmosphere_learning_account(key, {})
                results.append({'learning_account_key': key, 'learned': True, 'result_code': 'learned', 'result': learned})
            except Exception as exc:
                results.append({'learning_account_key': key, 'learned': False, 'result_code': 'learn_failed', 'result_reason': str(exc)})
        return {'ok': True, 'attempted_count': len(results), 'learned_count': sum(1 for item in results if item.get('learned') is True), 'results': results}

    def learn_once_group_atmosphere_learning_account(self, key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        account = self.get_group_atmosphere_learning_account(key)
        records = [GroupAtmosphereChatRecord(**item) for item in list((payload or {}).get('records') or []) if isinstance(item, dict) and str(item.get('text') or '').strip()]
        if not records:
            content = str((payload or {}).get('content') or '').strip()
            records = self._parse_group_atmosphere_chat_export(content) if content else []
        attempted_worker_fetch = False
        if not records:
            runtime_resolution = self._resolve_group_atmosphere_send_runtime(
                str(account.get('learning_account_key') or key),
                configured_worker_base_url=(payload or {}).get('worker_base_url') or account.get('worker_base_url'),
            )
            worker_base_url = str(runtime_resolution.get('base_url') or '').strip().rstrip('/')
            runtime_is_baileys = bool(runtime_resolution.get('is_baileys'))
            baileys_account_id = str(runtime_resolution.get('baileys_account_id') or '').strip() if runtime_is_baileys else ''
            if not worker_base_url and runtime_is_baileys:
                raise HTTPException(status_code=400, detail='baileys_runtime_not_configured')
            if not worker_base_url and not runtime_is_baileys:
                runtime_state = self._build_whatsapp_approval_runtime_state(str(account.get('learning_account_key') or key), allow_shared_fallback=False, skip_health_check=True)
                worker_base_url = self._validate_group_atmosphere_worker_base_url(runtime_state.get('base_url')) if runtime_state.get('active') else ''
            if not worker_base_url and not runtime_is_baileys:
                try:
                    recovered = self.start_whatsapp_approval_account_session(str(account.get('learning_account_key') or key), reset=False)
                except Exception:
                    recovered = {}
                recovered_runtime = recovered.get('runtime') if isinstance(recovered, dict) else {}
                worker_base_url = self._validate_group_atmosphere_worker_base_url((recovered_runtime or {}).get('base_url')) if (recovered_runtime or {}).get('active') else ''
            if worker_base_url:
                attempted_worker_fetch = True
                fetched_records: List[GroupAtmosphereChatRecord] = []
                group_links = [dict(item or {}) for item in list(account.get('group_links') or []) if isinstance(item, dict)]
                group_cursor_updates: Dict[str, Dict[str, str]] = {}
                for group in group_links:
                    if group.get('enabled') is False:
                        continue
                    target_candidates: List[str] = []
                    for candidate in [group.get('group_id'), group.get('target_group'), group.get('link'), group.get('group_name')]:
                        normalized_candidate = str(candidate or '').strip()
                        if normalized_candidate and normalized_candidate not in target_candidates:
                            target_candidates.append(normalized_candidate)
                    if not target_candidates:
                        continue
                    target_group = target_candidates[0]
                    fetch_payload_base = {'limit': int(account.get('max_messages_per_run') or 300)}
                    after_message_id = str(group.get('last_learned_message_id') or '').strip()
                    after_timestamp = str(group.get('last_learned_message_at') or '').strip()
                    if after_message_id:
                        fetch_payload_base['after_message_id'] = after_message_id
                    if after_timestamp:
                        fetch_payload_base['after_timestamp'] = after_timestamp
                    if baileys_account_id:
                        fetch_payload_base['accountId'] = baileys_account_id
                        fetch_payload_base['baileys_account_id'] = baileys_account_id
                    body: Dict[str, Any] = {}
                    response_status = 500
                    last_fetch_error = 'fetch_group_messages_failed'
                    for candidate in target_candidates:
                        fetch_payload = {'target_group': candidate, **fetch_payload_base}
                        response = requests.post(
                            f'{worker_base_url}/fetch-group-messages',
                            json=fetch_payload,
                            timeout=30,
                        )
                        body = response.json()
                        response_status = int(getattr(response, 'status_code', 500))
                        if response_status < 400:
                            target_group = candidate
                            break
                        last_fetch_error = str(body.get('result_reason') or body.get('detail') or 'fetch_group_messages_failed')
                        if 'group not found' not in last_fetch_error.lower() and 'group_not_found' not in last_fetch_error.lower():
                            break
                    if response_status >= 400:
                        raise HTTPException(status_code=502, detail=last_fetch_error)
                    raw_records = [item for item in list(body.get('records') or []) if isinstance(item, dict) and str(item.get('text') or '').strip()]
                    if not raw_records and (after_message_id or after_timestamp):
                        fallback_payload = {'target_group': target_group, 'limit': int(account.get('max_messages_per_run') or 300)}
                        if baileys_account_id:
                            fallback_payload['accountId'] = baileys_account_id
                            fallback_payload['baileys_account_id'] = baileys_account_id
                        fallback_response = requests.post(
                            f'{worker_base_url}/fetch-group-messages',
                            json=fallback_payload,
                            timeout=30,
                        )
                        fallback_body = fallback_response.json()
                        if int(getattr(fallback_response, 'status_code', 500)) >= 400:
                            raise HTTPException(status_code=502, detail=str(fallback_body.get('result_reason') or fallback_body.get('detail') or 'fetch_group_messages_failed'))
                        fallback_records = [item for item in list(fallback_body.get('records') or []) if isinstance(item, dict) and str(item.get('text') or '').strip()]
                        if after_timestamp:
                            try:
                                cursor_dt = parse_iso_datetime(after_timestamp)
                            except Exception:
                                cursor_dt = None
                            filtered_fallback_records = []
                            for item in fallback_records:
                                item_created_at = str(item.get('created_at') or item.get('timestamp') or '').strip()
                                if not item_created_at:
                                    continue
                                try:
                                    if parse_iso_datetime(item_created_at) > cursor_dt:
                                        filtered_fallback_records.append(item)
                                except Exception:
                                    continue
                            fallback_records = filtered_fallback_records
                        elif after_message_id:
                            seen_cursor = False
                            filtered_fallback_records = []
                            for item in fallback_records:
                                item_message_id = str(item.get('message_id') or item.get('id') or '').strip()
                                if item_message_id == after_message_id:
                                    seen_cursor = True
                                    continue
                                if seen_cursor:
                                    filtered_fallback_records.append(item)
                            fallback_records = filtered_fallback_records if seen_cursor else []
                        if fallback_records:
                            raw_records = fallback_records
                            body = fallback_body
                    for item in raw_records:
                        fetched_records.append(GroupAtmosphereChatRecord(
                            sender=str(item.get('sender') or '').strip() or None,
                            text=str(item.get('text') or '').strip(),
                            created_at=str(item.get('created_at') or '').strip() or None,
                            message_id=str(item.get('message_id') or item.get('id') or '').strip() or None,
                        ))
                    next_cursor = body.get('next_cursor') if isinstance(body.get('next_cursor'), dict) else (body.get('cursor') if isinstance(body.get('cursor'), dict) else {})
                    cursor_message_id = str(next_cursor.get('last_message_id') or next_cursor.get('message_id') or '').strip()
                    cursor_message_at = str(next_cursor.get('last_message_at') or next_cursor.get('created_at') or next_cursor.get('timestamp') or '').strip()
                    if raw_records and (not cursor_message_id or not cursor_message_at):
                        last_item = raw_records[-1]
                        cursor_message_id = cursor_message_id or str(last_item.get('message_id') or last_item.get('id') or '').strip()
                        cursor_message_at = cursor_message_at or str(last_item.get('created_at') or last_item.get('timestamp') or '').strip()
                    if cursor_message_id or cursor_message_at:
                        cursor_update = {
                            'last_learned_message_id': cursor_message_id,
                            'last_learned_message_at': cursor_message_at,
                            'last_learned_cursor_at': utc_now(),
                            'group_id': str(body.get('group_id') or '').strip(),
                            'group_name': str(body.get('group_name') or group.get('group_name') or '').strip(),
                        }
                        for candidate in target_candidates:
                            group_cursor_updates[candidate] = cursor_update
                if group_cursor_updates:
                    updated_group_links = []
                    for group in group_links:
                        target_group = str(group.get('group_id') or group.get('target_group') or group.get('link') or '').strip()
                        updated = dict(group)
                        if target_group in group_cursor_updates:
                            updated.update({k: v for k, v in group_cursor_updates[target_group].items() if v})
                        updated_group_links.append(updated)
                    account['group_links'] = updated_group_links
                if not runtime_is_baileys:
                    try:
                        worker_health = self._request_whatsapp_approval_worker_health(worker_base_url)
                        session_state = self._build_whatsapp_approval_session_state(
                            str(account.get('learning_account_key') or key),
                            worker_health=worker_health,
                            include_qr_ascii=False,
                        )
                        self._cache_whatsapp_approval_session_snapshot(str(account.get('learning_account_key') or key), session_state, worker_health)
                    except Exception:
                        pass
                records = fetched_records
        records = self._dedupe_group_atmosphere_records(records)
        if not records:
            if attempted_worker_fetch:
                summary = {
                    'result_code': 'no_new_records',
                    'result_reason': '当前没有新的可学习群消息。',
                    'read_count': 0,
                    'cleaned_count': 0,
                    'useful_count': 0,
                    'semantic_candidate_count': 0,
                    'filtered_count': 0,
                    'imported_count': 0,
                    'candidate_count': 0,
                    'role_keys': list(account.get('target_role_keys') or []),
                    'items': [],
                }
                with self.db.connect() as conn:
                    serialized_group_links = json.dumps(account.get('group_links') or [], ensure_ascii=False)
                    now = utc_now()
                    conn.execute("UPDATE whatsapp_group_atmosphere_learning_accounts SET group_links=?, last_learned_at=?, last_result_summary=?, updated_at=? WHERE learning_account_key=?", (serialized_group_links, now, json.dumps(summary, ensure_ascii=False), now, account.get('learning_account_key')))
                    conn.execute("UPDATE whatsapp_approval_accounts SET group_links=?, updated_at=? WHERE account_key=? AND responsible_type='group_atmosphere_learning'", (serialized_group_links, now, account.get('learning_account_key')))
                    conn.commit()
                return {
                    'ok': True,
                    'result_code': 'no_new_records',
                    'result_reason': '当前没有新的可学习群消息。',
                    'silent_learning_only': True,
                    'imported_count': 0,
                    'read_count': 0,
                    'cleaned_count': 0,
                    'useful_count': 0,
                    'semantic_candidate_count': 0,
                    'filtered_count': 0,
                    'candidate_count': 0,
                    'role_keys': summary['role_keys'],
                    'last_result_summary': summary,
                }
            raise HTTPException(status_code=400, detail='records_required')
        role_keys = list(account.get('target_role_keys') or [])
        imported_count = 0
        candidate_count = 0
        learned_items: List[Dict[str, Any]] = []
        role_keys_by_role: Dict[str, List[str]] = {}
        cleaned_count = 0
        useful_count = 0
        semantic_candidate_count = 0
        filtered_count = 0
        for role_key in role_keys:
            role = self._group_atmosphere_role_from_key(role_key)
            role_keys_by_role.setdefault(role, []).append(role_key)
            semantic_role = self._group_atmosphere_semantic_role_key(role)
            if semantic_role and semantic_role != role:
                role_keys_by_role.setdefault(semantic_role, []).append(role_key)
        single_target_role_key = role_keys[0] if len(role_keys) == 1 else ''
        routed_records: Dict[str, List[GroupAtmosphereChatRecord]] = {role_key: [] for role_key in role_keys}
        for record in records:
            cleaned = self._clean_group_atmosphere_message_text(record.text)
            if not cleaned:
                filtered_count += 1
                continue
            cleaned_count += 1
            semantic_intent = self._group_atmosphere_semantic_intent(cleaned)
            if not self._is_group_atmosphere_useful_candidate(cleaned):
                filtered_count += 1
                continue
            useful_count += 1
            if semantic_intent:
                semantic_candidate_count += 1
            detected_role = self._group_atmosphere_semantic_role_key(self._classify_group_atmosphere_record_role(cleaned))
            target_keys = role_keys_by_role.get(detected_role) or ([single_target_role_key] if single_target_role_key else [])
            for role_key in target_keys:
                role = self._group_atmosphere_role_from_key(role_key)
                polished = self._rewrite_group_atmosphere_semantic_candidate(cleaned, role=role) or self._polish_group_atmosphere_candidate_text(cleaned, role=role)
                if polished and self._is_group_atmosphere_useful_candidate(polished, role=role):
                    quality = self._evaluate_group_atmosphere_candidate_quality(polished, role=role, source_type='learning_account')
                    if quality.get('decision') == 'reject':
                        filtered_count += 1
                        continue
                    routed_records.setdefault(role_key, []).append(GroupAtmosphereChatRecord(sender=record.sender, text=polished, created_at=record.created_at))
        for role_key, role_records in routed_records.items():
            if not role_records:
                continue
            role = self._get_group_atmosphere_config(role_key)
            phrases = [record.text for record in self._dedupe_group_atmosphere_records(role_records)]
            upsert_result = self.upsert_group_atmosphere_manual_phrases({
                'role_key': role_key,
                'role_name': (role or {}).get('group_name') or self._default_group_atmosphere_plan_display_name(self._group_atmosphere_role_from_key(role_key), account.get('region')),
                'region': account.get('region'),
                'language': account.get('language'),
                'role_positioning': self._group_atmosphere_role_from_key(role_key),
                'phrases': phrases,
                'source_type': 'learning_account',
                'safe_to_send': False,
                'enabled': False,
            })
            added_count = int(upsert_result.get('added_count') or 0)
            imported_count += len(role_records)
            candidate_count += added_count
            for added_item in list(upsert_result.get('added_items') or [])[:added_count]:
                if not isinstance(added_item, dict):
                    continue
                phrase = str(added_item.get('text') or '').strip()
                if not phrase:
                    continue
                translation = {
                    'text_zh': added_item.get('text_zh') or '',
                    'text_zh_source': added_item.get('text_zh_source') or '',
                    'text_zh_status': added_item.get('text_zh_status') or '',
                }
                learned_items.append({
                    'role_key': role_key,
                    'role_positioning': self._group_atmosphere_role_from_key(role_key),
                    'candidate_id': added_item.get('candidate_id') or '',
                    'text': phrase,
                    'text_zh': translation.get('text_zh') or '',
                    'text_zh_source': translation.get('text_zh_source') or '',
                    'text_zh_status': translation.get('text_zh_status') or '',
                    'source_type': 'learning_account',
                    'safe_to_send': False,
                    'enabled': False,
                })
        summary = {
            'read_count': len(records),
            'cleaned_count': cleaned_count,
            'useful_count': useful_count,
            'semantic_candidate_count': semantic_candidate_count,
            'filtered_count': filtered_count,
            'imported_count': imported_count,
            'candidate_count': candidate_count,
            'role_keys': role_keys,
            'items': learned_items,
        }
        with self.db.connect() as conn:
            serialized_group_links = json.dumps(account.get('group_links') or [], ensure_ascii=False)
            conn.execute("UPDATE whatsapp_group_atmosphere_learning_accounts SET group_links=?, last_learned_at=?, last_result_summary=?, updated_at=? WHERE learning_account_key=?", (serialized_group_links, utc_now(), json.dumps(summary, ensure_ascii=False), utc_now(), account.get('learning_account_key')))
            conn.execute("UPDATE whatsapp_approval_accounts SET group_links=?, updated_at=? WHERE account_key=? AND responsible_type='group_atmosphere_learning'", (serialized_group_links, utc_now(), account.get('learning_account_key')))
            conn.commit()
        return {
            'ok': True,
            'silent_learning_only': True,
            'imported_count': len(records),
            'read_count': len(records),
            'cleaned_count': cleaned_count,
            'useful_count': useful_count,
            'semantic_candidate_count': semantic_candidate_count,
            'filtered_count': filtered_count,
            'candidate_count': candidate_count,
            'role_keys': role_keys,
            'last_result_summary': summary,
        }

    def _group_atmosphere_plan_usage_map(self) -> Dict[str, List[Dict[str, Any]]]:
        usage: Dict[str, List[Dict[str, Any]]] = {}
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT account_key, account_name, group_links FROM whatsapp_approval_accounts WHERE responsible_type='group_atmosphere'"
            ).fetchall()
        for row in rows:
            account_key = str(row['account_key'] or '').strip()
            account_name = str(row['account_name'] or '').strip() or account_key
            try:
                groups = json.loads(row['group_links'] or '[]')
            except Exception:
                groups = []
            if not isinstance(groups, list):
                continue
            for idx, group in enumerate(groups, start=1):
                if not isinstance(group, dict):
                    continue
                plan_name = str(group.get('speech_plan_config_name') or '').strip()
                if not plan_name:
                    continue
                usage.setdefault(plan_name, []).append({
                    'account_key': account_key,
                    'account_name': account_name,
                    'group_index': idx,
                    'group_name': str(group.get('group_name') or '').strip() or str(group.get('target_group') or '').strip(),
                    'target_group': str(group.get('target_group') or '').strip(),
                })
        return usage

    @staticmethod
    def _group_atmosphere_candidate_source_label(source_type: str) -> str:
        source = str(source_type or '').strip()
        if source in {'manual', 'manual_upload', 'custom', 'role_save', '自定义'}:
            return '人工写入'
        if source in {'learning_account', 'learning_bot', '学习bot'}:
            return '学习bot'
        if source in {'upload_file', 'auto_learn', 'local_language_profile', 'upload', '上传生成'}:
            return '上传生成'
        return source or '上传生成'

    def list_group_atmosphere_candidate_pool(self) -> Dict[str, Any]:
        """Return one candidate-list row per language + role_positioning.

        The candidate review area is an ammo pool, not a config/runtime list. Multiple
        DB configs can contain phrases for the same type because uploads, learning bots,
        roles, and old runtime bindings historically all wrote template_pool rows. The UI
        must not render those storage fragments as multiple lists for the same type.
        """
        self._sync_group_atmosphere_candidate_table_from_configs()
        self._schedule_group_atmosphere_translation_preprocess()
        candidates_by_config = self._group_atmosphere_candidate_payloads_by_config()
        usage_by_plan = self._group_atmosphere_plan_usage_map()
        disabled_phrase_type_keys = self._group_atmosphere_disabled_phrase_type_keys()
        grouped: Dict[tuple[str, str, str], Dict[str, Any]] = {}
        for config in self.list_group_atmosphere_configs():
            config_name = str(config.get('config_name') or '').strip()
            config_status = str(config.get('status') or '').strip()
            if config_name.startswith('deliver-') or config_name.startswith('binding-'):
                continue
            if config_status == 'enabled' and (str(config.get('account_key') or '').strip() or str(config.get('target_group') or '').strip()):
                continue
            table_templates = candidates_by_config.get(config_name)
            templates_source = table_templates if table_templates is not None else list(config.get('template_pool') or [])
            templates = self._sort_group_atmosphere_candidates([dict(item or {}) for item in list(templates_source or [])])
            config_language = str(config.get('language') or '').strip() or 'id'
            config_region = self._group_atmosphere_region_from_language(config_language)
            config_role = self._group_atmosphere_role_from_key(config_name)
            source_types = set()
            for item in templates:
                if not item.get('candidate_id'):
                    continue
                role = str(item.get('role_positioning') or item.get('source_role') or item.get('category') or config_role).strip()
                if not role or role in disabled_phrase_type_keys:
                    continue
                language = str(item.get('language') or config_language or '').strip() or 'id'
                region = str(item.get('region') or config_region or self._group_atmosphere_region_from_language(language)).strip()
                if not region or region == '未知':
                    region = self._group_atmosphere_region_from_language(language)
                source_type = str(item.get('source_type') or 'upload_file').strip()
                is_manual_source = self._group_atmosphere_candidate_is_manual(item)
                risk_reasons = self._group_atmosphere_candidate_risk_reasons(str(item.get('text') or ''))
                has_risk = bool(risk_reasons)
                stored_safe_to_send = self._group_atmosphere_truthy_flag(item.get('safe_to_send'))
                stored_enabled = self._group_atmosphere_truthy_flag(item.get('enabled'))
                quality_ready = source_type != 'learning_account' or str(item.get('quality_status') or '').strip() == 'manual_approved'
                source_types.add(source_type)
                key = (region, language, role)
                preferred_config = f'auto-{language}-{role}'
                if key not in grouped:
                    plan_display_name = self._default_group_atmosphere_plan_display_name(role, region)
                    stored_group_name = str(config.get('group_name') or '').strip()
                    if stored_group_name and (stored_group_name.startswith(region) or region == config_region):
                        plan_display_name = stored_group_name
                    grouped[key] = {
                        'config_name': config_name,
                        'storage_config_names': [],
                        'plan_display_name': plan_display_name,
                        'language': language,
                        'region': region,
                        'role_positioning': role,
                        'phrase_type': role,
                        'status': 'candidate_pool',
                        'enabled': False,
                        'bound_account_key': '',
                        'bound_target_group': '',
                        'bound_group_name': '',
                        'source_types': set(),
                        'usage': [],
                        'candidates': [],
                    }
                row = grouped[key]
                if config_name not in row['storage_config_names']:
                    row['storage_config_names'].append(config_name)
                if config_name == preferred_config:
                    row['config_name'] = preferred_config
                row['source_types'].add(source_type)
                candidate = {
                    'candidate_id': item.get('candidate_id') or item.get('template_id'),
                    'config_name': config_name,
                    'source_config_name': config_name,
                    'text': item.get('text') or '',
                    'language': language,
                    'region': region,
                    'role_positioning': role,
                    'phrase_type': role,
                    'source_role': item.get('source_role') or role,
                    'category': item.get('category') or role,
                    'source_type': source_type,
                    'source_label': self._group_atmosphere_candidate_source_label(source_type),
                    'sort_order': int(item.get('sort_order')) if item.get('sort_order') is not None and str(item.get('sort_order')).strip() != '' else None,
                    'text_zh': item.get('text_zh') or '',
                    'text_zh_source': item.get('text_zh_source') or '',
                    'text_zh_status': item.get('text_zh_status') or '',
                    'text_zh_updated_at': item.get('text_zh_updated_at') or '',
                    'text_zh_failure_reason': item.get('text_zh_failure_reason') or item.get('failure_reason') or '',
                    'text_zh_retry_count': int(item.get('text_zh_retry_count') or item.get('retry_count') or 0),
                    'asset_type': item.get('asset_type') or ('image_caption' if item.get('media_id') or item.get('media_path') else 'text'),
                    'media_id': item.get('media_id') or '',
                    'media_path': item.get('media_path') or '',
                    'media_mime_type': item.get('media_mime_type') or '',
                    'media_filename': item.get('media_filename') or '',
                    'media_preview_url': f"/api/ops/group-atmosphere/media-assets/{item.get('media_id')}/preview" if item.get('media_id') else '',
                    'customized': item.get('customized') is True,
                    'customized_at': item.get('customized_at') or '',
                    'score': int(item.get('score') or self._score_group_atmosphere_phrase(str(item.get('text') or ''), role=role)),
                    'frequency': int(item.get('frequency') or 1),
                    'safe_to_send': False if has_risk else (True if is_manual_source else (stored_safe_to_send and quality_ready)),
                    'enabled': False if has_risk else (True if is_manual_source else (stored_enabled and quality_ready)),
                    'quality_decision': 'quarantine' if has_risk else (item.get('quality_decision') or ('accept' if is_manual_source else '')),
                    'quality_status': 'risk_review' if has_risk else (item.get('quality_status') or ('manual_approved' if is_manual_source else ('pending_review' if source_type == 'learning_account' else ''))),
                    'quality_score': int(item.get('quality_score') or 0),
                    'quality_reasons': (list(item.get('quality_reasons') or []) + [reason for reason in risk_reasons if reason not in list(item.get('quality_reasons') or [])]),
                    'normalized_key': item.get('normalized_key') or self._normalize_group_atmosphere_phrase_key(str(item.get('text') or '')),
                    'semantic_key': item.get('semantic_key') or self._normalize_group_atmosphere_semantic_phrase_key(str(item.get('text') or '')),
                    'bound_account_key': '',
                    'bound_target_group': '',
                    'bound_group_name': '',
                }
                candidate = self._sanitize_group_atmosphere_translation_payload(candidate)
                row['candidates'].append(candidate)
                row['usage'].extend(usage_by_plan.get(config_name, []))
        rows = []
        global_seen_phrase_keys = set()
        for row in grouped.values():
            seen_candidate_keys = set()
            deduped_candidates = []
            for candidate in self._sort_group_atmosphere_candidates(row['candidates']):
                phrase_key = self._normalize_group_atmosphere_semantic_phrase_key(str(candidate.get('text') or '')) or str(candidate.get('candidate_id') or '')
                is_manual_candidate = self._group_atmosphere_candidate_is_manual(candidate)
                source_key = (str(candidate.get('role_positioning') or ''), phrase_key)
                if source_key in seen_candidate_keys:
                    continue
                if not is_manual_candidate and phrase_key in global_seen_phrase_keys:
                    continue
                seen_candidate_keys.add(source_key)
                if not is_manual_candidate:
                    global_seen_phrase_keys.add(phrase_key)
                deduped_candidates.append(candidate)
            source_types = sorted(row.pop('source_types', set()))
            usage = row.get('usage') or []
            row['usage'] = usage
            row['usage_count'] = len(usage)
            row['source_types'] = source_types
            row['candidates'] = deduped_candidates
            row['candidate_count'] = len(deduped_candidates)
            row['enabled_candidate_count'] = sum(1 for item in deduped_candidates if item.get('enabled') and item.get('safe_to_send'))
            rows.append(row)
        rows.sort(key=lambda r: (str(r.get('region') or ''), str(r.get('language') or ''), str(r.get('role_positioning') or ''), str(r.get('config_name') or '')))
        return {'rows': rows, 'storage': 'group_atmosphere_candidates'}

    def _group_atmosphere_delivery_config_name(self, source_config_name: str, account_key: str, target_group: str, index: int = 0) -> str:
        raw = f"{source_config_name}-{account_key}-{target_group}-{index}"
        suffix = re.sub(r'[^a-zA-Z0-9_-]+', '-', raw).strip('-').lower()
        if len(suffix) > 96:
            suffix = suffix[:96].rstrip('-')
        return f'deliver-{suffix}' or f'deliver-{create_id("gacfg")}'

    def _group_atmosphere_enabled_account_groups(self, account_key: str) -> List[Dict[str, Any]]:
        row = self._get_whatsapp_approval_account_row(str(account_key or '').strip())
        if not row or str(row.get('responsible_type') or '').strip() != 'group_atmosphere':
            raise HTTPException(status_code=404, detail='group_atmosphere_account_not_found')
        account = self._serialize_group_atmosphere_account_row(row, runtime_state={}, session_state={})
        if account.get('enabled') is False:
            return []
        return [
            dict(group or {})
            for group in list(account.get('groups') or [])
            if (group or {}).get('enabled') is not False and str((group or {}).get('target_group') or '').strip()
        ]

    def _candidate_translation_fallback(self, text: str) -> Dict[str, Any]:
        try:
            text_zh = self._translate_group_atmosphere_candidate_to_zh(text)
        except Exception:
            return {'text_zh': '暂未生成准确中文翻译，请人工确认原文含义。', 'text_zh_source': 'unavailable', 'text_zh_status': 'needs_translation'}
        if self._group_atmosphere_translation_has_source_language_residue(text_zh):
            return {'text_zh': '暂未生成准确中文翻译，请人工确认原文含义。', 'text_zh_source': 'unavailable', 'text_zh_status': 'needs_translation'}
        status = 'needs_review' if '暂未生成准确中文翻译' in text_zh else 'ok'
        return {'text_zh': text_zh, 'text_zh_source': 'rule', 'text_zh_status': status}

    def translate_group_atmosphere_candidate(self, payload: GroupAtmosphereCandidateTranslateRequest) -> Dict[str, Any]:
        config = self._get_group_atmosphere_config(payload.config_name)
        if not config:
            candidate_key_probe = str(payload.candidate_id or '').strip()
            for row in self.list_group_atmosphere_configs():
                templates_probe = [dict(item or {}) for item in list((row or {}).get('template_pool') or [])]
                if any(str(item.get('candidate_id') or item.get('template_id') or '').strip() == candidate_key_probe for item in templates_probe):
                    config = row
                    break
            if not config:
                raise HTTPException(status_code=404, detail='group_atmosphere_config_not_found')
        candidate_key = str(payload.candidate_id or '').strip()
        templates = [dict(item or {}) for item in list(config.get('template_pool') or [])]
        target = None
        for item in templates:
            item_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
            if item_id == candidate_key:
                target = item
                break
        if target is None:
            raise HTTPException(status_code=404, detail='candidate_not_found')
        manual_text = str(payload.text_zh or '').strip()
        now = utc_now()
        role = str(target.get('source_role') or target.get('category') or self._group_atmosphere_role_from_key(str(config.get('config_name') or ''))).strip()
        language = str(target.get('language') or config.get('language') or '').strip()
        region = str(target.get('region') or self._group_atmosphere_region_from_language(language)).strip()
        if manual_text:
            target['text_zh'] = manual_text[:1500]
            target['text_zh_source'] = 'manual'
            target['text_zh_status'] = 'ok'
            target['text_zh_updated_at'] = now
            target['text_zh_failure_reason'] = ''
            target['text_zh_retry_count'] = 0
            self._save_group_atmosphere_translation_cache(
                text=str(target.get('text') or ''),
                language=language,
                region=region,
                text_zh=manual_text[:1500],
                source='manual',
                status='ok',
                failure_reason='',
                retry_count=0,
                next_retry_at='',
            )
        elif target.get('text_zh_source') == 'manual':
            pass
        elif target.get('text_zh') and target.get('text_zh_source') in {'ai', 'google', 'libretranslate'} and not payload.force and self._group_atmosphere_translation_is_usable(str(target.get('text_zh') or ''), str(target.get('text_zh_source') or '')):
            target['text_zh'] = self._normalize_group_atmosphere_translation_text(str(target.get('text_zh') or ''))
            pass
        else:
            translated = self._build_group_atmosphere_candidate_translation(str(target.get('text') or ''), role=role, language=language, region=region, force=payload.force)
            target.update(translated)
            target['text_zh_updated_at'] = now
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE whatsapp_group_atmosphere_configs SET template_pool=?, updated_at=? WHERE config_name=?",
                (json.dumps(templates, ensure_ascii=False), now, config['config_name']),
            )
            conn.commit()
        return {'ok': True, 'config_name': config['config_name'], 'candidate': target}

    def preprocess_group_atmosphere_candidate_translations(self, *, limit: int = 50, retry_failed: bool = False) -> Dict[str, Any]:
        limit = max(1, min(200, int(limit or 50)))
        scanned = 0
        translated = 0
        cache_hits = 0
        failed = 0
        updated_configs = 0
        now = utc_now()
        for config in self.list_group_atmosphere_configs():
            if scanned >= limit:
                break
            config_name = str(config.get('config_name') or '').strip()
            if not config_name or config_name.startswith('deliver-') or config_name.startswith('binding-'):
                continue
            templates = [dict(item or {}) for item in list(config.get('template_pool') or [])]
            if not templates:
                continue
            config_changed = False
            config_language = str(config.get('language') or '').strip() or 'id'
            config_region = self._group_atmosphere_region_from_language(config_language)
            config_role = self._group_atmosphere_role_from_key(config_name)
            for item in templates:
                if scanned >= limit:
                    break
                text = str(item.get('text') or '').strip()
                if not text:
                    continue
                if str(item.get('text_zh_source') or '').strip() == 'manual':
                    continue
                role = str(item.get('source_role') or item.get('role_positioning') or item.get('category') or config_role).strip()
                language = str(item.get('language') or config_language or '').strip() or 'id'
                region = str(item.get('region') or config_region or self._group_atmosphere_region_from_language(language)).strip()
                cache = self._get_group_atmosphere_translation_cache(text, language=language, region=region)
                current_source = str(item.get('text_zh_source') or '').strip()
                current_status = str(item.get('text_zh_status') or '').strip()
                if cache:
                    cached_source = str(cache.get('text_zh_source') or '').strip()
                    cached_text = str(cache.get('text_zh') or '').strip()
                    cached_status = str(cache.get('text_zh_status') or '').strip()
                    if cached_text and cached_source in {'ai', 'google', 'libretranslate', 'manual', 'rule'} and self._group_atmosphere_translation_is_usable(cached_text, cached_source):
                        if (
                            item.get('text_zh') != cached_text
                            or current_source != cached_source
                            or current_status != cached_status
                            or str(item.get('text_zh_failure_reason') or '') != str(cache.get('failure_reason') or '')
                        ):
                            item.update(self._group_atmosphere_translation_result_from_cache(cache))
                            config_changed = True
                        cache_hits += 1
                        scanned += 1
                        continue
                    if current_status == 'failed' or cached_status == 'failed':
                        if not self._group_atmosphere_translation_retry_due(cache, retry_failed=retry_failed):
                            item.update(self._group_atmosphere_translation_result_from_cache(cache))
                            config_changed = True
                            scanned += 1
                            continue
                has_useful_translation = bool(str(item.get('text_zh') or '').strip()) and current_source in {'ai', 'google', 'libretranslate', 'manual'} and current_status in {'ok', 'needs_review'} and self._group_atmosphere_translation_is_usable(str(item.get('text_zh') or ''), current_source)
                should_translate = retry_failed or not has_useful_translation or current_source in {'', 'rule', 'unavailable', 'error'} or current_status in {'', 'needs_translation', 'failed'}
                if not should_translate:
                    scanned += 1
                    continue
                item['text_zh_status'] = 'translating'
                item['text_zh_failure_reason'] = ''
                item['text_zh_updated_at'] = now
                result = self._build_group_atmosphere_candidate_translation(text, role=role, language=language, region=region, force=retry_failed or current_status == 'failed')
                item.update(result)
                config_changed = True
                translated += 1
                if str(result.get('text_zh_status') or '').strip() == 'failed':
                    failed += 1
                scanned += 1
            if config_changed:
                with self.db.connect() as conn:
                    conn.execute(
                        "UPDATE whatsapp_group_atmosphere_configs SET template_pool=?, updated_at=? WHERE config_name=?",
                        (json.dumps(templates, ensure_ascii=False), utc_now(), config_name),
                    )
                    conn.commit()
                updated_configs += 1
        if updated_configs:
            self._sync_group_atmosphere_candidate_table_from_configs()
        return {
            'ok': True,
            'scanned': scanned,
            'translated': translated,
            'cache_hits': cache_hits,
            'failed': failed,
            'updated_configs': updated_configs,
            'retry_failed': bool(retry_failed),
        }

    def retry_failed_group_atmosphere_candidate_translations(self, *, limit: int = 100) -> Dict[str, Any]:
        return self.preprocess_group_atmosphere_candidate_translations(limit=limit, retry_failed=True)

    def _schedule_group_atmosphere_translation_preprocess(self) -> None:
        if not self.group_atmosphere_translation_background_enabled:
            return
        if self.db.db_path == ':memory:':
            return
        if self.group_atmosphere_candidate_translator is None:
            return
        with self._group_atmosphere_translation_preprocess_lock:
            if self._group_atmosphere_translation_preprocess_thread and self._group_atmosphere_translation_preprocess_thread.is_alive():
                return
            thread = threading.Thread(
                target=lambda: self.preprocess_group_atmosphere_candidate_translations(limit=80, retry_failed=False),
                name='group-atmosphere-translation-preprocess',
                daemon=True,
            )
            self._group_atmosphere_translation_preprocess_thread = thread
            thread.start()

    def save_group_atmosphere_custom_candidate(self, payload: GroupAtmosphereCandidateCustomRequest) -> Dict[str, Any]:
        config_key = str(payload.config_name or '').strip()
        requested_config_key = config_key
        candidate_id = str(payload.candidate_id or '').strip()
        config = self._get_group_atmosphere_config(config_key)
        if not config:
            if candidate_id:
                for fallback_config in self.list_group_atmosphere_configs():
                    fallback_templates = [dict(item or {}) for item in list((fallback_config or {}).get('template_pool') or [])]
                    if any(str(item.get('candidate_id') or item.get('template_id') or '').strip() == candidate_id for item in fallback_templates):
                        config = fallback_config
                        break
                if not config:
                    raise HTTPException(status_code=404, detail='group_atmosphere_config_not_found')
            if not config:
                role_for_new = self._resolve_group_atmosphere_phrase_type_key(str(payload.role_positioning or self._group_atmosphere_role_from_key(config_key) or ''), required=True)
                language_for_new = str(payload.language or '').strip() or 'id'
                parts = config_key.split('-', 2)
                if not str(payload.language or '').strip() and len(parts) >= 3 and parts[0] in {'auto', 'role'} and parts[1]:
                    language_for_new = parts[1]
                region_for_new = str(payload.region or self._group_atmosphere_region_from_language(language_for_new)).strip()
                config = self.upsert_group_atmosphere_config(GroupAtmosphereConfigRequest(
                    config_name=config_key or f'auto-{language_for_new}-{role_for_new}',
                    enabled=False,
                    account_key=config_key or f'auto-{language_for_new}-{role_for_new}',
                    target_group=config_key or f'auto-{language_for_new}-{role_for_new}',
                    group_name=self._default_group_atmosphere_plan_display_name(role_for_new, region_for_new),
                    language=language_for_new,
                    worker_base_url='',
                    daily_max_messages=0,
                    min_interval_minutes=120,
                    template_pool=[],
                    faq_rules=[],
                    status='candidate_pool',
                ))
        # 人工写入/编辑的候选文案由运营明确确认：不走上传/学习文案的清洗、过滤、润色、去重机制。
        text = str(payload.text or '').strip()
        if not text:
            raise HTTPException(status_code=400, detail='candidate_text_required')
        templates = [dict(item or {}) for item in list(config.get('template_pool') or [])]
        now = utc_now()
        candidate_id = str(payload.candidate_id or '').strip()
        role = str(payload.role_positioning or '').strip()
        media_asset = None
        if payload.remove_media and str(payload.media_id or '').strip():
            raise HTTPException(status_code=400, detail='media_replace_or_remove_only')
        if str(payload.media_id or '').strip():
            media_asset = self.get_group_atmosphere_media_asset(str(payload.media_id or '').strip())
        saved_item = None
        original_match_keys: set[str] = set()
        def apply_manual_candidate_edit(item: Dict[str, Any], active_config: Dict[str, Any]) -> Dict[str, Any]:
            item['text'] = text
            item_role = str(item.get('source_role') or item.get('category') or role or self._group_atmosphere_role_from_key(str(active_config.get('config_name') or ''))).strip()
            language = str(payload.language or item.get('language') or active_config.get('language') or '').strip()
            region = str(payload.region or item.get('region') or self._group_atmosphere_region_from_language(language)).strip()
            item['language'] = language
            item['region'] = region
            item['role_positioning'] = item_role
            item['phrase_type'] = item_role
            item.update({'text_zh': '', 'text_zh_source': '', 'text_zh_status': 'needs_translation', 'text_zh_updated_at': now, 'text_zh_failure_reason': '', 'text_zh_retry_count': 0})
            item['safe_to_send'] = True
            item['enabled'] = True
            item['customized'] = True
            item['customized_at'] = now
            item['score'] = self._score_group_atmosphere_phrase(text, role=item_role) + 30
            if payload.remove_media:
                for key in ('media_id', 'media_path', 'media_mime_type', 'media_filename', 'media_preview_url'):
                    item.pop(key, None)
                item['asset_type'] = 'text'
            elif media_asset:
                item['asset_type'] = 'image_caption'
                item['media_id'] = media_asset.get('media_id')
                item['media_path'] = media_asset.get('media_path')
                item['media_mime_type'] = media_asset.get('mime_type')
                item['media_filename'] = media_asset.get('filename')
                item['media_preview_url'] = media_asset.get('preview_url')
            return item
        if candidate_id:
            for item in templates:
                item_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
                if item_id != candidate_id:
                    continue
                original_match_keys |= self._group_atmosphere_template_match_keys(item)
                saved_item = apply_manual_candidate_edit(item, config)
                break
            if saved_item is None:
                # The candidate pool page groups candidates by visible role/type. Older
                # browser state can still send the visible config_name instead of the
                # candidate's source_config_name, so resolve the edit by candidate id.
                for fallback_config in self.list_group_atmosphere_configs():
                    fallback_key = str((fallback_config or {}).get('config_name') or '').strip()
                    if fallback_key == str(config.get('config_name') or '').strip():
                        continue
                    fallback_templates = [dict(item or {}) for item in list((fallback_config or {}).get('template_pool') or [])]
                    for item in fallback_templates:
                        item_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
                        if item_id != candidate_id:
                            continue
                        original_match_keys |= self._group_atmosphere_template_match_keys(item)
                        config = fallback_config
                        templates = fallback_templates
                        saved_item = apply_manual_candidate_edit(item, config)
                        break
                    if saved_item is not None:
                        break
                if saved_item is None:
                    raise HTTPException(status_code=404, detail='candidate_not_found')
        else:
            role = role or str(config.get('role_positioning') or '').strip() or str(config.get('config_name') or '').split('-', 2)[-1]
            candidate_id = create_id('gacand')
            language = str(payload.language or config.get('language') or '').strip()
            region = str(payload.region or self._group_atmosphere_region_from_language(language)).strip()
            saved_item = {
                'template_id': candidate_id,
                'candidate_id': candidate_id,
                'category': role,
                'source_role': role,
                'role_positioning': role,
                'phrase_type': role,
                'language': language,
                'region': region,
                'text': text,
                'score': self._score_group_atmosphere_phrase(text, role=role) + 50,
                'frequency': 1,
                'safe_to_send': True,
                'enabled': True,
                'source_type': 'manual',
                'customized': True,
                'customized_at': now,
            }
            saved_item.update({'text_zh': '', 'text_zh_source': '', 'text_zh_status': 'needs_translation', 'text_zh_updated_at': now, 'text_zh_failure_reason': '', 'text_zh_retry_count': 0})
            if media_asset:
                saved_item.update({
                    'asset_type': 'image_caption',
                    'media_id': media_asset.get('media_id'),
                    'media_path': media_asset.get('media_path'),
                    'media_mime_type': media_asset.get('mime_type'),
                    'media_filename': media_asset.get('filename'),
                    'media_preview_url': media_asset.get('preview_url'),
                })
            templates.append(saved_item)
        templates = self._sort_group_atmosphere_candidates(self._cap_group_atmosphere_template_pool(templates, limit=100))
        conn = self.db.connect()
        conn.execute(
            "UPDATE whatsapp_group_atmosphere_configs SET template_pool=?, updated_at=? WHERE config_name=?",
            (json.dumps(templates, ensure_ascii=False), now, config['config_name']),
        )
        conn.commit()
        cascaded = self._cascade_group_atmosphere_candidate_edit_update(
            saved_item,
            source_config_name=str(config.get('config_name') or ''),
            extra_match_keys=original_match_keys,
        )
        self._schedule_group_atmosphere_translation_preprocess()
        return {
            'ok': True,
            'candidate': saved_item,
            'config_name': config['config_name'],
            'requested_config_name': requested_config_key,
            'cascaded_media_configs': cascaded,
            'cascaded_candidate_configs': cascaded,
        }

    def reorder_group_atmosphere_candidates(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        config_key = str((payload or {}).get('config_name') or '').strip()
        ordered_ids = [str(item or '').strip() for item in list((payload or {}).get('candidate_ids') or []) if str(item or '').strip()]
        if not config_key or not ordered_ids:
            raise HTTPException(status_code=400, detail='candidate_reorder_required')
        config = self._get_group_atmosphere_config(config_key)
        if not config:
            raise HTTPException(status_code=404, detail='group_atmosphere_config_not_found')
        templates = [dict(item or {}) for item in list(config.get('template_pool') or [])]
        by_id = {str(item.get('candidate_id') or item.get('template_id') or '').strip(): item for item in templates}
        missing = [item_id for item_id in ordered_ids if item_id not in by_id]
        if missing:
            raise HTTPException(status_code=404, detail='candidate_not_found')
        raw_orders = (payload or {}).get('candidate_orders') or {}
        candidate_orders = {str(k or '').strip(): int(v) for k, v in dict(raw_orders).items() if str(k or '').strip() and str(v).strip().lstrip('-').isdigit()}
        order_map = {item_id: int(candidate_orders.get(item_id, idx)) for idx, item_id in enumerate(ordered_ids)}
        previous_order = [str(item.get('candidate_id') or item.get('template_id') or '').strip() for item in templates if str(item.get('candidate_id') or item.get('template_id') or '').strip()]
        next_order = (max(order_map.values()) + 1) if order_map else len(ordered_ids)
        for item in templates:
            item_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
            if item_id in order_map:
                item['sort_order'] = order_map[item_id]
            elif item_id:
                item['sort_order'] = next_order
                next_order += 1
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE whatsapp_group_atmosphere_configs SET template_pool=?, updated_at=? WHERE config_name=?",
                (json.dumps(templates, ensure_ascii=False), utc_now(), config_key),
            )
            self._record_audit_event(
                conn,
                event_type='group_atmosphere_candidate_reordered',
                event_source='group_atmosphere',
                payload={'config_name': config_key, 'previous_order': previous_order, 'new_order': ordered_ids},
            )
            conn.commit()
        return {'ok': True, 'config_name': config_key, 'ordered_count': len(ordered_ids), 'candidate_ids': ordered_ids}

    def delete_group_atmosphere_candidate(self, config_name: str, candidate_id: str) -> Dict[str, Any]:
        config_key = str(config_name or '').strip()
        candidate_key = str(candidate_id or '').strip()
        if not config_key or not candidate_key:
            raise HTTPException(status_code=400, detail='candidate_required')
        config = self._get_group_atmosphere_config(config_key)
        if not config:
            raise HTTPException(status_code=404, detail='group_atmosphere_config_not_found')
        requested_config_key = config_key
        templates = [dict(item or {}) for item in list(config.get('template_pool') or [])]
        kept = []
        deleted = None
        for item in templates:
            item_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
            if item_id == candidate_key:
                deleted = item
                continue
            kept.append(item)
        if deleted is None:
            # The candidate pool UI aggregates multiple storage configs into one visible
            # role list. Older page builds may send the visible row config_name instead
            # of the candidate's real source_config_name; fall back to a global exact-id
            # lookup so delete remains idempotent from the operator's perspective.
            for fallback_config in self.list_group_atmosphere_configs():
                fallback_key = str(fallback_config.get('config_name') or '').strip()
                if not fallback_key or fallback_key == requested_config_key:
                    continue
                fallback_templates = [dict(item or {}) for item in list(fallback_config.get('template_pool') or [])]
                fallback_kept = []
                fallback_deleted = None
                for item in fallback_templates:
                    item_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
                    if item_id == candidate_key:
                        fallback_deleted = item
                        continue
                    fallback_kept.append(item)
                if fallback_deleted is not None:
                    config_key = fallback_key
                    config = fallback_config
                    templates = fallback_templates
                    kept = fallback_kept
                    deleted = fallback_deleted
                    break
        if deleted is None:
            raise HTTPException(status_code=404, detail='candidate_not_found')
        current_status = str(config.get('status') or '').strip()
        source_types = {str(item.get('source_type') or '').strip() for item in templates}
        was_visible_role = current_status in {'library_only', 'plan_ready', 'role_container'} or bool(source_types - {'', 'upload_file', 'learning_account', 'learning_bot', 'auto_learn', 'local_language_profile'})
        next_status = current_status
        if not kept and was_visible_role:
            next_status = 'role_container'
        elif not kept:
            next_status = 'candidate_pool'
        elif current_status == 'role_container':
            next_status = 'library_only'
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE whatsapp_group_atmosphere_configs SET template_pool=?, status=?, updated_at=? WHERE config_name=?",
                (json.dumps(kept, ensure_ascii=False), next_status, utc_now(), config_key),
            )
            self._record_audit_event(
                conn,
                event_type='group_atmosphere_candidate_deleted',
                event_source='group_atmosphere',
                payload={'config_name': config_key, 'requested_config_name': requested_config_key, 'candidate_id': candidate_key, 'candidate': deleted, 'status': next_status},
            )
            conn.commit()
        return {'ok': True, 'deleted': True, 'config_name': config_key, 'candidate_id': candidate_key, 'status': next_status}

    def move_group_atmosphere_candidates_to_type(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        source_config_name = str((payload or {}).get('source_config_name') or (payload or {}).get('config_name') or '').strip()
        target_role = str((payload or {}).get('target_role_positioning') or (payload or {}).get('target_role') or '').strip()
        selected_ids = {str(item or '').strip() for item in list((payload or {}).get('candidate_ids') or []) if str(item or '').strip()}
        if not source_config_name:
            raise HTTPException(status_code=400, detail='source_config_required')
        if not target_role:
            raise HTTPException(status_code=400, detail='target_role_required')
        if not selected_ids:
            raise HTTPException(status_code=400, detail='candidate_required')
        source = self._get_group_atmosphere_config(source_config_name)
        if not source:
            raise HTTPException(status_code=404, detail='source_config_not_found')
        source_templates = [dict(item or {}) for item in list(source.get('template_pool') or [])]
        source_role = str((source.get('role_positioning') or (source_templates[0].get('source_role') if source_templates else '') or (source_templates[0].get('role_positioning') if source_templates else '') or (source_templates[0].get('category') if source_templates else '') or self._group_atmosphere_role_from_key(source_config_name)) or '').strip()
        moved: List[Dict[str, Any]] = []
        kept: List[Dict[str, Any]] = []
        for item in source_templates:
            item_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
            if item_id in selected_ids:
                moved.append(dict(item))
            else:
                kept.append(item)
        if not moved:
            raise HTTPException(status_code=404, detail='candidate_not_found')
        language = str(source.get('language') or '').strip() or 'id'
        target_config_name = str((payload or {}).get('target_config_name') or '').strip() or f"auto-{language}-{target_role}"
        if target_role == source_role or target_config_name == source_config_name:
            raise HTTPException(status_code=400, detail='target_type_same_as_source')
        target = self._get_group_atmosphere_config(target_config_name)
        target_templates = [dict(item or {}) for item in list((target or {}).get('template_pool') or [])]
        existing_ids = {str(item.get('candidate_id') or item.get('template_id') or '').strip(): idx for idx, item in enumerate(target_templates)}
        existing_texts = {
            (self._normalize_group_atmosphere_semantic_phrase_key(str(item.get('text') or '').strip()) or str(item.get('text') or '').strip()): idx
            for idx, item in enumerate(target_templates)
            if str(item.get('text') or '').strip()
        }
        now = utc_now()
        media_fields = ['asset_type', 'media_id', 'media_path', 'media_mime_type', 'media_filename', 'media_preview_url']
        for item in moved:
            item['role_positioning'] = target_role
            item['source_role'] = target_role
            item['category'] = target_role
            item['moved_from_config_name'] = source_config_name
            item['moved_from_role_positioning'] = str(source.get('role_positioning') or self._group_atmosphere_role_from_key(source_config_name))
            item['moved_at'] = now
            item_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
            text_key = self._normalize_group_atmosphere_semantic_phrase_key(str(item.get('text') or '').strip()) or str(item.get('text') or '').strip()
            if item_id in existing_ids:
                target_templates[existing_ids[item_id]] = item
            elif text_key and text_key in existing_texts:
                existing_item = target_templates[existing_texts[text_key]]
                existing_item['role_positioning'] = target_role
                existing_item['source_role'] = target_role
                existing_item['category'] = target_role
                existing_item['moved_from_config_name'] = source_config_name
                existing_item['moved_from_role_positioning'] = item.get('moved_from_role_positioning')
                existing_item['moved_at'] = now
                for field in media_fields:
                    if item.get(field) and (field != 'asset_type' or str(existing_item.get('asset_type') or 'text') in {'', 'text'}):
                        existing_item[field] = item.get(field)
                if item.get('media_id') and str(existing_item.get('asset_type') or 'text') == 'text':
                    existing_item['asset_type'] = item.get('asset_type') or 'image_caption'
                existing_item['safe_to_send'] = bool(existing_item.get('safe_to_send', item.get('safe_to_send', True)))
                existing_item['enabled'] = bool(existing_item.get('enabled', item.get('enabled', True)))
            else:
                target_templates.append(item)
                if text_key:
                    existing_texts[text_key] = len(target_templates) - 1
        source_status = str(source.get('status') or '').strip() or 'candidate_pool'
        next_source_status = 'candidate_pool' if not kept else source_status
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE whatsapp_group_atmosphere_configs SET template_pool=?, status=?, updated_at=? WHERE config_name=?",
                (json.dumps(kept, ensure_ascii=False), next_source_status, now, source_config_name),
            )
            if target:
                conn.execute(
                    "UPDATE whatsapp_group_atmosphere_configs SET template_pool=?, status=?, updated_at=? WHERE config_name=?",
                    (json.dumps(self._sort_group_atmosphere_candidates(target_templates), ensure_ascii=False), str(target.get('status') or 'candidate_pool'), now, target_config_name),
                )
            else:
                region = self._group_atmosphere_region_from_language(language)
                conn.execute(
                    """
                    INSERT INTO whatsapp_group_atmosphere_configs (
                        config_name, enabled, account_key, target_group, group_name, language, timezone,
                        worker_base_url, daily_max_messages, min_interval_minutes, max_interval_minutes, allowed_windows,
                        template_pool, mention_reply_enabled, faq_rules, status, updated_at
                    ) VALUES (?, 0, ?, ?, ?, ?, 'UTC', '', 0, 120, 120, '[]', ?, 0, '[]', 'candidate_pool', ?)
                    """,
                    (
                        target_config_name,
                        target_config_name,
                        target_config_name,
                        f"自动学习素材库-{region}",
                        language,
                        json.dumps(self._sort_group_atmosphere_candidates(target_templates), ensure_ascii=False),
                        now,
                    ),
                )
            self._record_audit_event(
                conn,
                event_type='group_atmosphere_candidate_moved_type',
                event_source='group_atmosphere',
                payload={'source_config_name': source_config_name, 'target_config_name': target_config_name, 'target_role_positioning': target_role, 'candidate_ids': sorted(selected_ids)},
            )
            conn.commit()
        return {'ok': True, 'moved_count': len(moved), 'source_config_name': source_config_name, 'target_config_name': target_config_name, 'target_role_positioning': target_role, 'candidate_ids': [str(item.get('candidate_id') or item.get('template_id') or '').strip() for item in moved]}

    def add_group_atmosphere_candidates_to_role(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        role_key = str((payload or {}).get('role_key') or '').strip()
        source_config_name = str((payload or {}).get('source_config_name') or (payload or {}).get('config_name') or '').strip()
        selected_ids = {str(item or '').strip() for item in list((payload or {}).get('candidate_ids') or []) if str(item or '').strip()}
        if not role_key:
            raise HTTPException(status_code=400, detail='role_required')
        if not source_config_name:
            raise HTTPException(status_code=400, detail='source_config_required')
        if not selected_ids:
            raise HTTPException(status_code=400, detail='candidate_required')
        role = self._get_group_atmosphere_config(role_key)
        source = self._get_group_atmosphere_config(source_config_name)
        if not role:
            raise HTTPException(status_code=404, detail='role_not_found')
        if not source:
            raise HTTPException(status_code=404, detail='source_config_not_found')
        role_summary = self._group_atmosphere_role_summary(role)
        role_type = str(role_summary.get('role_positioning') or self._group_atmosphere_role_from_key(role_key)).strip()
        role_language = str(role_summary.get('language') or role.get('language') or '').strip() or 'id'
        source_language = str(source.get('language') or '').strip() or 'id'
        if role_language != source_language:
            raise HTTPException(status_code=400, detail='role_language_mismatch')
        source_templates = [dict(item or {}) for item in list(source.get('template_pool') or [])]
        selected = []
        selected_items = []
        for item in source_templates:
            item_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
            if item_id in selected_ids:
                item_type = str(item.get('source_role') or item.get('category') or self._group_atmosphere_role_from_key(source_config_name)).strip()
                if item_type != role_type:
                    raise HTTPException(status_code=400, detail='role_type_mismatch')
                text = str(item.get('text') or '').strip()
                if text:
                    selected.append(text)
                    selected_items.append(dict(item))
        if not selected:
            raise HTTPException(status_code=400, detail='candidate_not_found')
        if role_key == source_config_name:
            now = utc_now()
            changed = 0
            for item in source_templates:
                item_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
                if item_id in selected_ids:
                    item['safe_to_send'] = True
                    item['enabled'] = True
                    changed += 1
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE whatsapp_group_atmosphere_configs SET template_pool=?, status='plan_ready', updated_at=? WHERE config_name=?",
                    (json.dumps(source_templates, ensure_ascii=False), now, source_config_name),
                )
                conn.commit()
            return {'ok': True, 'role_key': role_key, 'added_count': changed, 'reused_candidate_pool': True}
        result = self.upsert_group_atmosphere_manual_phrases({
            'role_key': role_key,
            'role_name': role_summary.get('role_name') or role_summary.get('plan_display_name') or role_key,
            'region': role_summary.get('region'),
            'language': role_summary.get('language'),
            'role_positioning': role_type,
            'phrases': selected,
            'source_candidates': selected_items,
            'source_type': 'role_save',
            'safe_to_send': True,
            'enabled': True,
        })
        return {'ok': True, 'role_key': role_key, 'added_count': len(selected), 'role': result.get('role')}

    def enable_group_atmosphere_candidates(self, payload: GroupAtmosphereCandidateEnableRequest) -> Dict[str, Any]:
        config = self._get_group_atmosphere_config(payload.config_name)
        if not config:
            selected_ids = {str(item).strip() for item in list(payload.candidate_ids or []) if str(item).strip()}
            for row in self.list_group_atmosphere_configs():
                templates_probe = [dict(item or {}) for item in list((row or {}).get('template_pool') or [])]
                if any(str(item.get('candidate_id') or item.get('template_id') or '').strip() in selected_ids for item in templates_probe):
                    config = row
                    break
            if not config:
                raise HTTPException(status_code=404, detail='group_atmosphere_config_not_found')
        selected_ids = {str(item).strip() for item in list(payload.candidate_ids or []) if str(item).strip()}
        templates = [dict(item or {}) for item in list(config.get('template_pool') or [])]
        enabled_count = 0
        for item in templates:
            candidate_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
            if selected_ids and candidate_id not in selected_ids:
                continue
            item['safe_to_send'] = True
            item['enabled'] = True
            item['quality_status'] = 'manual_approved'
            item['quality_decision'] = item.get('quality_decision') or 'accept'
            enabled_count += 1
        if enabled_count <= 0:
            for row in self.list_group_atmosphere_configs():
                templates_probe = [dict(item or {}) for item in list((row or {}).get('template_pool') or [])]
                if any(str(item.get('candidate_id') or item.get('template_id') or '').strip() in selected_ids for item in templates_probe):
                    config = row
                    templates = templates_probe
                    enabled_count = 0
                    for item in templates:
                        candidate_id = str(item.get('candidate_id') or item.get('template_id') or '').strip()
                        if selected_ids and candidate_id not in selected_ids:
                            continue
                        item['safe_to_send'] = True
                        item['enabled'] = True
                        item['quality_status'] = 'manual_approved'
                        item['quality_decision'] = item.get('quality_decision') or 'accept'
                        enabled_count += 1
                    if enabled_count > 0:
                        break
            if enabled_count <= 0:
                raise HTTPException(status_code=400, detail='candidate_not_found')
        now = utc_now()
        if not str(payload.account_key or '').strip() and not str(payload.target_group or '').strip():
            conn = self.db.connect()
            conn.execute(
                """
                UPDATE whatsapp_group_atmosphere_configs
                SET enabled=0, template_pool=?, status=?, updated_at=?
                WHERE config_name=?
                """,
                (json.dumps(templates, ensure_ascii=False), 'candidate_pool' if str(config.get('config_name') or '').startswith('auto-') else 'plan_ready', now, config['config_name']),
            )
            conn.commit()
            updated = self._get_group_atmosphere_config(config['config_name'])
            return {'ok': True, 'enabled_count': enabled_count, 'config': updated, 'plan_only': True}
        account_key = str(payload.account_key or config.get('account_key') or '').strip()
        target_group = str(payload.target_group or config.get('target_group') or '').strip()
        group_name = str(payload.group_name or config.get('group_name') or '').strip() or None
        worker_base_url = self._validate_group_atmosphere_worker_base_url(payload.worker_base_url if payload.worker_base_url is not None else config.get('worker_base_url') or '')
        if not worker_base_url and self._group_atmosphere_allow_test_worker_urls:
            worker_base_url = 'http://worker.local'
        daily_max = int(payload.daily_max_messages if payload.daily_max_messages is not None else config.get('daily_max_messages') or 4)
        min_interval = _group_atmosphere_interval_seconds(
            payload.min_interval_seconds,
            payload.min_interval_minutes,
            _group_atmosphere_mapping_interval_seconds(config, 'min_interval_seconds', 'min_interval_minutes', 60),
        )
        max_interval = _group_atmosphere_interval_seconds(
            payload.max_interval_seconds,
            payload.max_interval_minutes,
            _group_atmosphere_mapping_interval_seconds(config, 'max_interval_seconds', 'max_interval_minutes', max(min_interval, 240)),
        )
        if max_interval < min_interval:
            max_interval = min_interval
        if target_group == '__all_enabled_groups__':
            if not account_key:
                raise HTTPException(status_code=400, detail='account_key_required')
            with self.db.connect() as conn:
                conn.execute(
                    """
                    UPDATE whatsapp_group_atmosphere_configs
                    SET enabled=0, template_pool=?, status='plan_ready', updated_at=?
                    WHERE config_name=?
                    """,
                    (json.dumps(templates, ensure_ascii=False), now, config['config_name']),
                )
                conn.commit()
            groups = self._group_atmosphere_enabled_account_groups(account_key)
            if not groups:
                raise HTTPException(status_code=400, detail='no_enabled_target_groups')
            delivery_configs = []
            for idx, group in enumerate(groups, start=1):
                group_target = str(group.get('target_group') or '').strip()
                group_display_name = str(group.get('group_name') or '').strip() or group_target
                delivery_name = self._group_atmosphere_delivery_config_name(config['config_name'], account_key, group_target, idx)
                delivery = self.upsert_group_atmosphere_config(GroupAtmosphereConfigRequest(
                    config_name=delivery_name,
                    enabled=True,
                    account_key=account_key,
                    target_group=group_target,
                    group_name=group_display_name,
                    language=str(config.get('language') or 'en'),
                    timezone=str(config.get('timezone') or 'UTC'),
                    worker_base_url=worker_base_url,
                    daily_max_messages=int(group.get('daily_max_messages') or daily_max),
                    min_interval_seconds=_group_atmosphere_mapping_interval_seconds(group, 'min_interval_seconds', 'min_interval_minutes', min_interval),
                    max_interval_seconds=_group_atmosphere_mapping_interval_seconds(group, 'max_interval_seconds', 'max_interval_minutes', max_interval),
                    allowed_windows=list(group.get('allowed_windows') or config.get('allowed_windows') or []),
                    template_pool=[GroupAtmosphereTemplate(**item) for item in templates],
                    mention_reply_enabled=bool(config.get('mention_reply_enabled')),
                    faq_rules=[GroupAtmosphereFaqRule(**item) for item in list(config.get('faq_rules') or [])],
                    status='enabled',
                ))
                delivery['source_config_name'] = config['config_name']
                delivery_configs.append(delivery)
            account_row = self._get_whatsapp_approval_account_row(account_key)
            if account_row:
                try:
                    all_groups = json.loads(account_row.get('group_links') or '[]')
                except Exception:
                    all_groups = []
                enabled_targets = {str(group.get('target_group') or '').strip() for group in groups if isinstance(group, dict)}
                if isinstance(all_groups, list):
                    changed = False
                    for group in all_groups:
                        if not isinstance(group, dict):
                            continue
                        if str(group.get('target_group') or '').strip() in enabled_targets:
                            group['speech_plan_config_name'] = config['config_name']
                            changed = True
                    if changed:
                        with self.db.connect() as conn:
                            conn.execute(
                                "UPDATE whatsapp_approval_accounts SET group_links=?, updated_at=? WHERE account_key=?",
                                (json.dumps(all_groups, ensure_ascii=False), utc_now(), account_key),
                            )
                            conn.commit()
            return {
                'ok': True,
                'enabled_count': enabled_count,
                'target_group_count': len(delivery_configs),
                'configs': delivery_configs,
                'config': delivery_configs[0] if delivery_configs else None,
            }
        conn = self.db.connect()
        conn.execute(
            """
            UPDATE whatsapp_group_atmosphere_configs
            SET enabled=1, account_key=?, target_group=?, group_name=?, worker_base_url=?,
                daily_max_messages=?, min_interval_minutes=?, max_interval_minutes=?, template_pool=?, status='enabled', updated_at=?
            WHERE config_name=?
            """,
            (
                account_key,
                target_group,
                group_name,
                worker_base_url,
                daily_max,
                min_interval,
                max_interval,
                json.dumps(templates, ensure_ascii=False),
                now,
                config['config_name'],
            ),
        )
        conn.commit()
        updated = self._get_group_atmosphere_config(config['config_name'])
        return {'ok': True, 'enabled_count': enabled_count, 'config': updated}

    def rename_group_atmosphere_speech_plan(self, config_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = str(config_name or '').strip()
        if not normalized or normalized.startswith('deliver-'):
            raise HTTPException(status_code=400, detail='invalid_speech_plan')
        display_name = str((payload or {}).get('plan_display_name') or (payload or {}).get('display_name') or (payload or {}).get('name') or '').strip()
        if not display_name:
            raise HTTPException(status_code=400, detail='plan_display_name_required')
        config = self._get_group_atmosphere_config(normalized)
        if not config:
            raise HTTPException(status_code=404, detail='speech_plan_not_found')
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE whatsapp_group_atmosphere_configs SET group_name=?, updated_at=? WHERE config_name=?",
                (display_name, utc_now(), normalized),
            )
            conn.commit()
        return {'ok': True, 'config': self._get_group_atmosphere_config(normalized)}

    def delete_group_atmosphere_speech_plan(self, config_name: str) -> Dict[str, Any]:
        normalized = str(config_name or '').strip()
        if not normalized or normalized.startswith('deliver-'):
            raise HTTPException(status_code=400, detail='invalid_speech_plan')
        config = self._get_group_atmosphere_config(normalized)
        if not config:
            raise HTTPException(status_code=404, detail='speech_plan_not_found')
        cleared_refs = 0
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT account_key, group_links FROM whatsapp_approval_accounts WHERE responsible_type='group_atmosphere'"
            ).fetchall()
            for row in rows:
                try:
                    groups = json.loads(row['group_links'] or '[]')
                except Exception:
                    groups = []
                if not isinstance(groups, list):
                    continue
                changed = False
                for group in groups:
                    if isinstance(group, dict) and str(group.get('speech_plan_config_name') or '').strip() == normalized:
                        group['speech_plan_config_name'] = ''
                        cleared_refs += 1
                        changed = True
                if changed:
                    conn.execute(
                        "UPDATE whatsapp_approval_accounts SET group_links=?, updated_at=? WHERE account_key=?",
                        (json.dumps(groups, ensure_ascii=False), utc_now(), row['account_key']),
                    )
            cursor = conn.execute(
                "DELETE FROM whatsapp_group_atmosphere_configs WHERE config_name=? OR config_name LIKE ?",
                (normalized, f'deliver-{normalized}-%'),
            )
            conn.commit()
        return {'ok': True, 'deleted': True, 'deleted_count': int(cursor.rowcount or 0), 'cleared_reference_count': cleared_refs}

    def sync_group_atmosphere_delivery_configs_from_account_plans(self) -> Dict[str, Any]:
        synced = []
        expected_delivery_names: set[str] = set()
        managed_targets: set[tuple[str, str]] = set()
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM whatsapp_approval_accounts WHERE responsible_type='group_atmosphere' AND enabled=1"
            ).fetchall()
        for row in rows:
            account = self._serialize_group_atmosphere_account_row(dict(row), runtime_state={}, session_state={})
            account_key = str(account.get('account_key') or '').strip()
            for idx, group in enumerate(list(account.get('groups') or []), start=1):
                target_group = str(group.get('target_group') or '').strip()
                if not target_group:
                    continue
                managed_targets.add((account_key, target_group))
                if group.get('enabled') is False:
                    continue
                plan_name = str(group.get('speech_plan_config_name') or '').strip()
                if not plan_name:
                    continue
                plan = self._get_group_atmosphere_config(plan_name)
                if not plan:
                    continue
                templates = [dict(item or {}) for item in list(plan.get('template_pool') or [])]
                enabled_templates = [item for item in templates if self._group_atmosphere_truthy_flag(item.get('enabled')) and self._group_atmosphere_truthy_flag(item.get('safe_to_send'))]
                if not enabled_templates:
                    continue
                delivery_name = self._group_atmosphere_delivery_config_name(plan_name, account_key, target_group, idx)
                expected_delivery_names.add(delivery_name)
                delivery = self.upsert_group_atmosphere_config(GroupAtmosphereConfigRequest(
                    config_name=delivery_name,
                    enabled=True,
                    account_key=account_key,
                    target_group=target_group,
                    group_name=str(group.get('group_name') or '').strip() or target_group,
                    language=str(plan.get('language') or account.get('language') or 'en'),
                    timezone=str(plan.get('timezone') or 'UTC'),
                    worker_base_url='http://worker.local' if self._group_atmosphere_allow_test_worker_urls else '',
                    daily_max_messages=int(group.get('daily_max_messages') or account.get('daily_max_messages') or plan.get('daily_max_messages') or 4),
                    min_interval_seconds=_group_atmosphere_mapping_interval_seconds(
                        group,
                        'min_interval_seconds',
                        'min_interval_minutes',
                        _group_atmosphere_mapping_interval_seconds(account, 'min_interval_seconds', 'min_interval_minutes', _group_atmosphere_mapping_interval_seconds(plan, 'min_interval_seconds', 'min_interval_minutes', 60)),
                    ),
                    max_interval_seconds=_group_atmosphere_mapping_interval_seconds(
                        group,
                        'max_interval_seconds',
                        'max_interval_minutes',
                        _group_atmosphere_mapping_interval_seconds(account, 'max_interval_seconds', 'max_interval_minutes', _group_atmosphere_mapping_interval_seconds(plan, 'max_interval_seconds', 'max_interval_minutes', 240)),
                    ),
                    allowed_windows=list(group.get('allowed_windows') or plan.get('allowed_windows') or []),
                    template_pool=[GroupAtmosphereTemplate(**item) for item in templates],
                    mention_reply_enabled=bool(plan.get('mention_reply_enabled')),
                    faq_rules=[GroupAtmosphereFaqRule(**item) for item in list(plan.get('faq_rules') or [])],
                    status='enabled',
                ))
                delivery['source_plan_config_name'] = plan_name
                synced.append(delivery)
        stale_disabled_count = 0
        if managed_targets:
            with self.db.connect() as conn:
                delivery_rows = conn.execute(
                    "SELECT config_name, account_key, target_group FROM whatsapp_group_atmosphere_configs WHERE config_name LIKE 'deliver-%' AND enabled=1"
                ).fetchall()
                for delivery_row in delivery_rows:
                    config_name = str(delivery_row['config_name'] or '').strip()
                    target_key = (str(delivery_row['account_key'] or '').strip(), str(delivery_row['target_group'] or '').strip())
                    if target_key in managed_targets and config_name not in expected_delivery_names:
                        conn.execute(
                            "UPDATE whatsapp_group_atmosphere_configs SET enabled=0, status='disabled_stale_plan', updated_at=? WHERE config_name=?",
                            (utc_now(), config_name),
                        )
                        stale_disabled_count += 1
                conn.commit()
        return {'ok': True, 'synced_count': len(synced), 'stale_disabled_count': stale_disabled_count, 'configs': synced}

    def _next_group_atmosphere_due_at(self, config: Dict[str, Any]) -> str:
        min_interval = _group_atmosphere_mapping_interval_seconds(config or {}, 'min_interval_seconds', 'min_interval_minutes', 0)
        max_interval = max(
            min_interval,
            _group_atmosphere_mapping_interval_seconds(config or {}, 'max_interval_seconds', 'max_interval_minutes', min_interval),
        )
        delay_seconds = random.randint(min_interval, max_interval) if max_interval > min_interval else min_interval
        return (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()

    def _group_atmosphere_schedule_timezone(self, config: Dict[str, Any]):
        tz_name = str((config or {}).get('timezone') or '').strip() or 'Asia/Shanghai'
        if tz_name.upper() == 'UTC':
            tz_name = 'Asia/Shanghai'
        try:
            return ZoneInfo(tz_name), tz_name
        except Exception:
            return ZoneInfo('Asia/Shanghai'), 'Asia/Shanghai'

    @staticmethod
    def _group_atmosphere_parse_window_minutes(value: Any) -> Optional[int]:
        text = str(value or '').strip()
        if not text or ':' not in text:
            return None
        try:
            hour_text, minute_text = text.split(':', 1)
            hour = int(hour_text)
            minute = int(minute_text)
        except Exception:
            return None
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None
        return hour * 60 + minute

    def _normalize_group_atmosphere_binding_schedule_strategies(
        self,
        raw: Any,
        *,
        fallback: Optional[Dict[str, Any]] = None,
        require_enabled_strategy: bool = False,
        validate_roles: bool = True,
    ) -> List[Dict[str, Any]]:
        fallback = dict(fallback or {})
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or '[]')
            except Exception:
                raw = []
        if not isinstance(raw, list) or not raw:
            role_key = str(fallback.get('role_key') or '').strip()
            if not role_key:
                return []
            windows = fallback.get('allowed_windows') if isinstance(fallback.get('allowed_windows'), list) else []
            first_window = dict(windows[0] or {}) if windows else {'start': '00:00', 'end': '23:59'}
            raw = [{
                'strategy_key': 'default',
                'strategy_name': '全天策略',
                'enabled': True,
                'role_key': role_key,
                'start': first_window.get('start') or '00:00',
                'end': first_window.get('end') or '23:59',
                'min_interval_seconds': fallback.get('min_interval_seconds') or fallback.get('min_interval_minutes') or 0,
                'max_interval_seconds': fallback.get('max_interval_seconds') or fallback.get('max_interval_minutes') or 0,
                'randomness_level': fallback.get('randomness_level') or 'medium',
                'phrase_send_order': fallback.get('phrase_send_order') or 'random',
            }]
        normalized: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()
        enabled_intervals: List[tuple[str, int, int]] = []
        for idx, item in enumerate(raw[:6]):
            if not isinstance(item, dict):
                continue
            strategy_key = str(item.get('strategy_key') or item.get('key') or f'strategy_{idx + 1}').strip()
            strategy_key = re.sub(r'[^a-zA-Z0-9_-]+', '_', strategy_key).strip('_') or f'strategy_{idx + 1}'
            if strategy_key in seen_keys:
                strategy_key = f'{strategy_key}_{idx + 1}'
            seen_keys.add(strategy_key)
            strategy_name = str(item.get('strategy_name') or item.get('name') or ('人工时段' if strategy_key == 'human' else '非人工时段' if strategy_key == 'off_hours' else f'策略{idx + 1}')).strip()
            enabled = item.get('enabled') is not False
            role_key = str(item.get('role_key') or item.get('config_name') or '').strip()
            windows = item.get('allowed_windows') if isinstance(item.get('allowed_windows'), list) else []
            first_window = dict(windows[0] or {}) if windows else {}
            start = str(item.get('start') or first_window.get('start') or '').strip()
            end = str(item.get('end') or first_window.get('end') or '').strip()
            start_minutes = self._group_atmosphere_parse_window_minutes(start)
            end_minutes = self._group_atmosphere_parse_window_minutes(end)
            if enabled:
                if not role_key:
                    raise HTTPException(status_code=400, detail=f'{strategy_name} 请选择话术角色')
                if validate_roles and not self._get_group_atmosphere_config(role_key):
                    raise HTTPException(status_code=404, detail=f'{strategy_name} 的话术角色不存在')
                if start_minutes is None or end_minutes is None:
                    raise HTTPException(status_code=400, detail=f'{strategy_name} 请选择完整的生效时间')
                if start_minutes == end_minutes:
                    raise HTTPException(status_code=400, detail=f'{strategy_name} 的开始和结束时间不能相同')
            elif start_minutes is None or end_minutes is None:
                start, end, start_minutes, end_minutes = '00:00', '23:59', 0, 1439
            min_interval = _coerce_positive_int(
                item.get('min_interval_seconds') if item.get('min_interval_seconds') is not None else item.get('min_interval_minutes'),
                _coerce_positive_int(fallback.get('min_interval_seconds') or fallback.get('min_interval_minutes'), 0),
            )
            max_interval = _coerce_positive_int(
                item.get('max_interval_seconds') if item.get('max_interval_seconds') is not None else item.get('max_interval_minutes'),
                _coerce_positive_int(fallback.get('max_interval_seconds') or fallback.get('max_interval_minutes'), max(min_interval, 0)),
            )
            max_interval = max(min_interval, max_interval)
            normalized_item = {
                'strategy_key': strategy_key,
                'strategy_name': strategy_name,
                'enabled': bool(enabled),
                'role_key': role_key,
                'start': start,
                'end': end,
                'timezone': 'Asia/Shanghai',
                'allowed_windows': [{'start': start, 'end': end}],
                'min_interval_seconds': min_interval,
                'max_interval_seconds': max_interval,
                'min_interval_minutes': min_interval,
                'max_interval_minutes': max_interval,
                'randomness_level': str(item.get('randomness_level') or fallback.get('randomness_level') or 'medium').strip() or 'medium',
                'phrase_send_order': str(item.get('phrase_send_order') or fallback.get('phrase_send_order') or 'random').strip() or 'random',
            }
            normalized.append(normalized_item)
            if enabled and start_minutes is not None and end_minutes is not None:
                intervals = [(start_minutes, end_minutes)] if start_minutes < end_minutes else [(start_minutes, 1440), (0, end_minutes)]
                for start_i, end_i in intervals:
                    enabled_intervals.append((strategy_name, start_i, end_i))
        if require_enabled_strategy and not any(item.get('enabled') and item.get('role_key') for item in normalized):
            raise HTTPException(status_code=400, detail='至少配置一个启用的发言策略')
        for idx, (name, start_i, end_i) in enumerate(enabled_intervals):
            for other_name, other_start, other_end in enabled_intervals[idx + 1:]:
                if max(start_i, other_start) < min(end_i, other_end):
                    raise HTTPException(status_code=400, detail=f'发言策略时间重叠：{name} 与 {other_name}')
        return normalized

    def _active_group_atmosphere_binding_schedule_strategy(
        self,
        binding: Dict[str, Any],
        *,
        now_utc: Optional[datetime] = None,
    ) -> tuple[Optional[Dict[str, Any]], str]:
        strategies = self._normalize_group_atmosphere_binding_schedule_strategies(
            binding.get('schedule_strategies'),
            fallback=binding,
            require_enabled_strategy=False,
            validate_roles=False,
        )
        if not strategies:
            return None, 'no schedule strategy configured'
        tz = ZoneInfo('Asia/Shanghai')
        now_local = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
        current_minutes = now_local.hour * 60 + now_local.minute
        for strategy in strategies:
            if strategy.get('enabled') is False:
                continue
            start_minutes = self._group_atmosphere_parse_window_minutes(strategy.get('start'))
            end_minutes = self._group_atmosphere_parse_window_minutes(strategy.get('end'))
            if start_minutes is None or end_minutes is None or start_minutes == end_minutes:
                continue
            if start_minutes < end_minutes:
                active = start_minutes <= current_minutes < end_minutes
            else:
                active = current_minutes >= start_minutes or current_minutes < end_minutes
            if active:
                return strategy, f"within {strategy.get('strategy_name') or 'strategy'} ({strategy.get('start')}-{strategy.get('end')} Asia/Shanghai)"
        return None, f"outside configured schedule strategies at {now_local.strftime('%H:%M')} (Asia/Shanghai)"

    @staticmethod
    def _apply_group_atmosphere_binding_schedule_strategy(binding: Dict[str, Any], strategy: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        effective = dict(binding or {})
        if not strategy:
            return effective
        effective.update({
            'active_schedule_strategy': dict(strategy),
            'role_key': strategy.get('role_key') or effective.get('role_key'),
            'daily_max_messages': 0,
            'min_interval_seconds': int(strategy.get('min_interval_seconds') or strategy.get('min_interval_minutes') or 0),
            'max_interval_seconds': int(strategy.get('max_interval_seconds') or strategy.get('max_interval_minutes') or 0),
            'min_interval_minutes': int(strategy.get('min_interval_seconds') or strategy.get('min_interval_minutes') or 0),
            'max_interval_minutes': int(strategy.get('max_interval_seconds') or strategy.get('max_interval_minutes') or 0),
            'randomness_level': strategy.get('randomness_level') or effective.get('randomness_level') or 'medium',
            'phrase_send_order': strategy.get('phrase_send_order') or effective.get('phrase_send_order') or 'random',
            'allowed_windows': list(strategy.get('allowed_windows') or [{'start': strategy.get('start'), 'end': strategy.get('end')}]),
            'timezone': 'Asia/Shanghai',
        })
        return effective

    def _group_atmosphere_window_due_status(self, config: Dict[str, Any], now_utc: Optional[datetime] = None) -> tuple[bool, str]:
        windows = list((config or {}).get('allowed_windows') or [])
        if not windows:
            return True, 'within allowed window'
        tz, tz_name = self._group_atmosphere_schedule_timezone(config)
        now_utc = now_utc or datetime.now(timezone.utc)
        now_local = now_utc.astimezone(tz)
        current_minutes = now_local.hour * 60 + now_local.minute
        next_start_local: Optional[datetime] = None
        for raw in windows:
            if not isinstance(raw, dict):
                continue
            start_minutes = self._group_atmosphere_parse_window_minutes(raw.get('start'))
            end_minutes = self._group_atmosphere_parse_window_minutes(raw.get('end'))
            if start_minutes is None or end_minutes is None:
                continue
            if start_minutes <= end_minutes:
                if start_minutes <= current_minutes <= end_minutes:
                    return True, 'within allowed window'
                candidate_date = now_local.date()
                if current_minutes > start_minutes:
                    candidate_date += timedelta(days=1)
                candidate = datetime.combine(candidate_date, datetime.min.time(), tzinfo=tz) + timedelta(minutes=start_minutes)
            else:
                if current_minutes >= start_minutes or current_minutes <= end_minutes:
                    return True, 'within allowed window'
                candidate = datetime.combine(now_local.date(), datetime.min.time(), tzinfo=tz) + timedelta(minutes=start_minutes)
            if next_start_local is None or candidate < next_start_local:
                next_start_local = candidate
        if next_start_local is not None:
            return False, f"outside allowed window; next window starts at {next_start_local.strftime('%Y-%m-%d %H:%M')} ({tz_name})"
        return True, 'allowed window config invalid; skip window gate'

    def _group_atmosphere_config_due_now(self, config: Dict[str, Any]) -> tuple[bool, str]:
        now = datetime.now(timezone.utc)
        within_window, window_reason = self._group_atmosphere_window_due_status(config, now)
        if not within_window:
            return False, window_reason
        next_due_at = str((config or {}).get('next_due_at') or '').strip()
        if next_due_at:
            try:
                due_at = parse_iso_datetime(next_due_at)
                if now < due_at:
                    seconds = max(1, int((due_at - now).total_seconds()) + 1)
                    return False, f'next scheduled send in about {seconds} seconds'
                return True, 'next scheduled send is due'
            except Exception:
                pass
        last_sent_at = str((config or {}).get('last_sent_at') or '').strip()
        min_interval = _group_atmosphere_mapping_interval_seconds(config or {}, 'min_interval_seconds', 'min_interval_minutes', 0)
        if last_sent_at and min_interval:
            try:
                elapsed = now - parse_iso_datetime(last_sent_at)
                if elapsed < timedelta(seconds=min_interval):
                    seconds = max(1, int((timedelta(seconds=min_interval) - elapsed).total_seconds()) + 1)
                    return False, f'minimum interval not reached; about {seconds} seconds remaining'
            except Exception:
                pass
        return True, 'due now'

    def _claim_group_atmosphere_scheduler_lease(self, *, table: str, key_column: str, key: str) -> bool:
        normalized_table = str(table or '').strip()
        normalized_key_column = str(key_column or '').strip()
        normalized_key = str(key or '').strip()
        if normalized_table not in {'whatsapp_group_atmosphere_role_bindings', 'whatsapp_group_atmosphere_configs'}:
            raise ValueError('unsupported_group_atmosphere_scheduler_lease_table')
        if normalized_key_column not in {'binding_id', 'config_name'}:
            raise ValueError('unsupported_group_atmosphere_scheduler_lease_key')
        if not normalized_key:
            return False
        now = utc_now()
        lease_until = (parse_iso_datetime(now) + timedelta(seconds=self._group_atmosphere_scheduler_lease_seconds)).isoformat()
        with self.db.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE {normalized_table}
                SET scheduler_lease_owner=?, scheduler_lease_until=?, updated_at=?
                WHERE {normalized_key_column}=?
                  AND (
                    COALESCE(scheduler_lease_until, '') = ''
                    OR scheduler_lease_until <= ?
                    OR scheduler_lease_owner = ?
                  )
                """,
                (self._worker_id, lease_until, now, normalized_key, now, self._worker_id),
            )
            conn.commit()
        return int(cursor.rowcount or 0) > 0

    def _release_group_atmosphere_scheduler_lease(self, *, table: str, key_column: str, key: str) -> None:
        normalized_table = str(table or '').strip()
        normalized_key_column = str(key_column or '').strip()
        normalized_key = str(key or '').strip()
        if normalized_table not in {'whatsapp_group_atmosphere_role_bindings', 'whatsapp_group_atmosphere_configs'}:
            return
        if normalized_key_column not in {'binding_id', 'config_name'} or not normalized_key:
            return
        with self.db.connect() as conn:
            conn.execute(
                f"""
                UPDATE {normalized_table}
                SET scheduler_lease_owner='', scheduler_lease_until='', updated_at=?
                WHERE {normalized_key_column}=? AND scheduler_lease_owner=?
                """,
                (utc_now(), normalized_key, self._worker_id),
            )
            conn.commit()

    def _start_group_atmosphere_scheduler_worker(self) -> None:
        if self._group_atmosphere_scheduler_thread and self._group_atmosphere_scheduler_thread.is_alive():
            return
        thread = threading.Thread(target=self._group_atmosphere_scheduler_loop, name='group-atmosphere-scheduler', daemon=True)
        thread.start()
        self._group_atmosphere_scheduler_thread = thread

    def _group_atmosphere_scheduler_loop(self) -> None:
        while not self._worker_stop.is_set():
            self._group_atmosphere_scheduler_state.update({'last_tick_at': utc_now()})
            try:
                self.run_due_group_atmosphere_learning_scheduler(limit=20)
                result = self.run_due_group_atmosphere_scheduler(GroupAtmosphereSchedulerRunRequest(limit=100))
                self._group_atmosphere_scheduler_state.update({
                    'last_success_at': utc_now(),
                    'last_error': '',
                    'last_result': {
                        'mode': result.get('mode'),
                        'attempted_count': result.get('attempted_count'),
                        'sent_count': result.get('sent_count'),
                    },
                })
            except Exception as exc:
                self._group_atmosphere_scheduler_state.update({
                    'last_error_at': utc_now(),
                    'last_error': str(exc),
                })
                print(f'Group atmosphere scheduler degraded: {exc}')
            self._worker_stop.wait(self.group_atmosphere_scheduler_poll_interval_seconds)

    def run_due_group_atmosphere_scheduler(self, payload: GroupAtmosphereSchedulerRunRequest) -> Dict[str, Any]:
        results = []
        bindings_payload = self.list_group_atmosphere_role_bindings()
        role_bindings = list(bindings_payload.get('rows') or [])
        if role_bindings:
            for binding in role_bindings:
                if len(results) >= int(payload.limit):
                    break
                binding_id = str(binding.get('binding_id') or '').strip()
                if not self._claim_group_atmosphere_scheduler_lease(
                    table='whatsapp_group_atmosphere_role_bindings',
                    key_column='binding_id',
                    key=binding_id,
                ):
                    results.append({
                        'sent': False,
                        'binding_id': binding_id,
                        'role_key': binding.get('role_key'),
                        'target_group': binding.get('target_group'),
                        'result_code': 'scheduler_lease_held',
                        'result_reason': 'another scheduler worker is handling this binding',
                    })
                    continue
                try:
                    result = self._dispatch_due_group_atmosphere_silence_trigger_for_binding(binding)
                    if result is None:
                        result = self._dispatch_group_atmosphere_binding_once(binding, trigger_type='scheduled_auto', require_auto_enabled=True)
                    results.append(result)
                finally:
                    self._release_group_atmosphere_scheduler_lease(
                        table='whatsapp_group_atmosphere_role_bindings',
                        key_column='binding_id',
                        key=binding_id,
                    )
            return {
                'ok': True,
                'mode': 'role_bindings',
                'attempted_count': len(results),
                'sent_count': sum(1 for item in results if item.get('sent') is True),
                'results': results,
            }
        self.sync_group_atmosphere_delivery_configs_from_account_plans()
        for config in self.list_group_atmosphere_configs():
            if len(results) >= int(payload.limit):
                break
            config_name = str(config.get('config_name') or '').strip()
            if (
                config_name.startswith('binding-')
                or str(config.get('status') or '') == 'plan_ready'
                or not str(config.get('account_key') or '').strip()
                or not str(config.get('target_group') or '').strip()
            ):
                continue
            if not config.get('enabled') or str(config.get('status') or '') != 'enabled':
                continue
            if not self._enabled_group_atmosphere_templates(config):
                continue
            if not self._claim_group_atmosphere_scheduler_lease(
                table='whatsapp_group_atmosphere_configs',
                key_column='config_name',
                key=config_name,
            ):
                results.append({'sent': False, 'config_name': config_name, 'result_code': 'scheduler_lease_held', 'result_reason': 'another scheduler worker is handling this config'})
                continue
            try:
                today = _group_atmosphere_business_date()
                sent_count_today = int(config.get('sent_count_today') or 0) if config.get('sent_count_date') == today else 0
                daily_max = int(config.get('daily_max_messages') or 0)
                if daily_max and sent_count_today >= daily_max:
                    results.append({'sent': False, 'config_name': config_name, 'result_code': 'daily_limit_reached', 'result_reason': 'daily max messages reached'})
                    continue
                due, due_reason = self._group_atmosphere_config_due_now(config)
                if not due:
                    results.append({'sent': False, 'config_name': config_name, 'result_code': 'not_due_yet', 'result_reason': due_reason})
                    continue
                result = self.dispatch_group_atmosphere_once(GroupAtmosphereDispatchRequest(config_name=config_name, trigger_type='scheduled_auto'))
                result['config_name'] = config_name
                results.append(result)
            finally:
                self._release_group_atmosphere_scheduler_lease(
                    table='whatsapp_group_atmosphere_configs',
                    key_column='config_name',
                    key=config_name,
                )
        return {
            'ok': True,
            'attempted_count': len(results),
            'sent_count': sum(1 for item in results if item.get('sent') is True),
            'results': results,
        }

    def import_group_atmosphere_chat_records(self, payload: GroupAtmosphereImportChatRecordsRequest) -> Dict[str, Any]:
        config = self._get_group_atmosphere_config(payload.config_name)
        if not config:
            raise HTTPException(status_code=404, detail='group_atmosphere_config_not_found')
        now = utc_now()
        records = self._dedupe_group_atmosphere_records([record for record in payload.records if str(record.text or '').strip()])
        conn = self.db.connect()
        existing_message_keys = {
            self._normalize_group_atmosphere_phrase_key(str(row['message_text'] or ''))
            for row in conn.execute(
                "SELECT message_text FROM whatsapp_group_atmosphere_chat_records WHERE config_name=?",
                (config['config_name'],),
            ).fetchall()
        }
        inserted_count = 0
        for record in records:
            message_text = self._clean_group_atmosphere_message_text(str(record.text or ''))
            message_key = self._normalize_group_atmosphere_phrase_key(message_text)
            if not message_key or message_key in existing_message_keys:
                continue
            existing_message_keys.add(message_key)
            inserted_count += 1
            conn.execute(
                "INSERT INTO whatsapp_group_atmosphere_chat_records (record_id, config_name, sender, message_text, created_at) VALUES (?, ?, ?, ?, ?)",
                (create_id('wach'), config['config_name'], str(record.sender or '').strip() or None, message_text, str(record.created_at or '').strip() or now),
            )
        rows = conn.execute(
            "SELECT message_text FROM whatsapp_group_atmosphere_chat_records WHERE config_name=? ORDER BY created_at DESC LIMIT 200",
            (config['config_name'],),
        ).fetchall()
        texts = [str(row['message_text'] or '') for row in rows]
        profile = self._group_atmosphere_language_profile_from_texts(texts, str(config.get('language') or 'en'))
        conn.execute(
            """
            INSERT INTO whatsapp_group_atmosphere_language_profiles (config_name, language, sample_count, frequent_terms, phrase_samples, tone_markers, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_name) DO UPDATE SET
                language=excluded.language, sample_count=excluded.sample_count, frequent_terms=excluded.frequent_terms,
                phrase_samples=excluded.phrase_samples, tone_markers=excluded.tone_markers, updated_at=excluded.updated_at
            """,
            (
                config['config_name'], profile['language'], profile['sample_count'],
                json.dumps(profile['frequent_terms'], ensure_ascii=False), json.dumps(profile['phrase_samples'], ensure_ascii=False),
                json.dumps(profile['tone_markers'], ensure_ascii=False), now,
            ),
        )
        conn.commit()
        self._log_group_atmosphere_event(
            config_name=config['config_name'], account_key=config['account_key'], target_group=config['target_group'],
            direction='inbound', trigger_type='chat_record_import', message_text=f'imported {len(records)} records',
            status='success', result_code='language_profile_updated', raw_result=profile,
        )
        return {'ok': True, 'imported_count': inserted_count, 'received_count': len(records), 'language_profile': profile}

    def _get_group_atmosphere_language_profile(self, config_name: str) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM whatsapp_group_atmosphere_language_profiles WHERE config_name=?",
                (str(config_name or '').strip(),),
            ).fetchone()
        if not row:
            return {'language': 'en', 'sample_count': 0, 'frequent_terms': [], 'phrase_samples': [], 'tone_markers': {}}
        return {
            'language': row['language'],
            'sample_count': int(row['sample_count'] or 0),
            'frequent_terms': json.loads(row['frequent_terms'] or '[]'),
            'phrase_samples': json.loads(row['phrase_samples'] or '[]'),
            'tone_markers': json.loads(row['tone_markers'] or '{}'),
            'updated_at': row['updated_at'],
        }

    def generate_group_atmosphere_ai_candidates(self, payload: GroupAtmosphereAiCandidateRequest) -> Dict[str, Any]:
        config = self._get_group_atmosphere_config(payload.config_name)
        if not config:
            raise HTTPException(status_code=404, detail='group_atmosphere_config_not_found')
        profile = self._get_group_atmosphere_language_profile(config['config_name'])
        terms = self._group_atmosphere_safe_language_terms([str(term) for term in profile.get('frequent_terms') or []])
        tone = dict(profile.get('tone_markers') or {})
        local_abbreviations = self._group_atmosphere_safe_language_terms([str(item) for item in tone.get('local_abbreviations') or [] if str(item).strip()], allow_short_slang=True)
        greeting = 'Halo kak' if tone.get('uses_kak') or str(config.get('language')) == 'id' else 'Hi'
        cta = 'krm data ke admin ya kak' if 'krm' in local_abbreviations else ('kirim data ke admin ya kak' if tone.get('uses_kak') else 'contact admin for help')
        role = str(payload.topic or 'general').strip() or 'general'
        base_terms = ', '.join(terms[:3]) if terms else 'panduan, ID, kode'
        short_hint = ', '.join(local_abbreviations[:3])
        common_patterns = [
            f"{greeting}, jgn bingung ya. Cek panduan dulu, trus {cta}.",
            f"{greeting}, yg baru join bisa mulai pelan-pelan. Kalau belum paham, tanya admin ya.",
            f"{greeting}, kalau kode atau cara mulai belum jelas, cek pin grup lalu {cta}.",
            f"{greeting}, reminder singkat: siapkan ID dan kode undangan sebelum lanjut ya.",
        ]
        role_patterns = {
            'faq_helper': [
                f"{greeting}, kalau ada yang bingung soal kode, tanya di grup ya. Admin bantu cek.",
                f"{greeting}, kode pribadi biasanya dipakai saat daftar. Pastikan kodenya benar sebelum kirim data.",
                f"{greeting}, kalau pertanyaanmu belum terjawab, tulis singkat di grup biar admin bantu arahkan.",
            ],
            'newcomer_guide': [
                f"{greeting}, yang baru join bisa mulai dari panduan dulu, lalu {cta}.",
                f"{greeting}, sebelum lanjut, pastikan ID, nomor, dan kode sudah siap ya.",
                f"{greeting}, ikuti langkah awal satu per satu. Kalau mentok, kirim pertanyaan ke admin.",
            ],
            'motivation_admin': [
                f"{greeting}, mulai pelan-pelan dulu tidak apa-apa. Yang penting ikuti panduan dengan benar.",
                f"{greeting}, semangat ya. Kalau sudah siap, lanjutkan sesuai arahan admin.",
                f"{greeting}, konsisten dulu dari langkah kecil. Kalau ada kendala, admin bantu arahkan.",
            ],
            'community_seed': [
                f"{greeting}, selamat datang. Kalau ada yang belum jelas, boleh tanya di grup ya.",
                f"{greeting}, biar rapi, cek panduan dulu sebelum kirim data ke admin ya.",
                f"{greeting}, yang baru masuk bisa baca pin grup dulu supaya tidak bingung.",
            ],
        }.get(role)
        patterns = list(role_patterns or common_patterns)
        if False and short_hint:
            patterns.append(f"{greeting}, istilah grup yang sering muncul: {short_hint}. Kalau bingung, tanya admin ya.")
        elif base_terms:
            patterns.append(f"{greeting}, fokus hari ini: {base_terms}. Ikuti panduan grup dulu ya.")
        candidates = []
        used_texts = set()
        for raw_text in patterns:
            if len(candidates) >= min(int(payload.count), 100):
                break
            text = self._clean_group_atmosphere_message_text(raw_text)
            key = self._normalize_group_atmosphere_phrase_key(text)
            if not key or key in used_texts:
                continue
            used_texts.add(key)
            candidates.append({
                'candidate_id': f"cand-{len(candidates) + 1}",
                'text': text,
                'topic': role,
                'source': 'local_language_profile',
                'safe_to_send': False,
                'reason': 'AI-assisted candidate requires operator review before production sending',
            })
        return {'config_name': config['config_name'], 'source': 'local_language_profile', 'language_profile': profile, 'candidates': candidates}

    def simulate_group_atmosphere(self, payload: GroupAtmosphereSimulationRequest) -> Dict[str, Any]:
        config = self._get_group_atmosphere_config(payload.config_name)
        if not config:
            raise HTTPException(status_code=404, detail='group_atmosphere_config_not_found')
        templates = list(config.get('template_pool') or [])
        scheduled_messages = []
        if templates:
            template = templates[0]
            scheduled_messages.append({
                'trigger_type': 'schedule',
                'would_send': True,
                'message_text': str(template.get('text') or ''),
                'dry_run': True,
                'reason': 'simulation_only',
            })
        inbound_replies = []
        for message in payload.inbound_messages:
            simulated = self.handle_group_atmosphere_inbound_message(GroupAtmosphereInboundMessageRequest(
                account_key=config['account_key'], target_group=config['target_group'], sender_id=message.sender_id,
                text=message.text, mentioned=message.mentioned, quoted_own_message=message.quoted_own_message,
            ))
            inbound_replies.append(simulated)
        return {
            'config_name': config['config_name'],
            'scenario': payload.scenario,
            'dry_run': True,
            'real_send_performed': False,
            'scheduled_messages': scheduled_messages,
            'inbound_replies': inbound_replies,
            'ai_candidates': self.generate_group_atmosphere_ai_candidates(GroupAtmosphereAiCandidateRequest(config_name=config['config_name'], topic=payload.scenario, count=3))['candidates'],
        }

    def _start_ingress_worker(self) -> None:
        self._worker_threads = [thread for thread in self._worker_threads if thread.is_alive()]
        if len(self._worker_threads) >= self.ingress_worker_count:
            return
        self._worker_stop.clear()
        start_index = len(self._worker_threads)
        for idx in range(start_index, self.ingress_worker_count):
            thread = threading.Thread(target=self._worker_loop, name=f'ingress-worker-{idx + 1}', daemon=True)
            thread.start()
            self._worker_threads.append(thread)

    def _notify_worker_new_work(self) -> None:
        if not self.ingress_worker_enabled:
            return
        self._start_ingress_worker()
        self._worker_wakeup.set()

    def process_next_worker_tick(self) -> Optional[Dict[str, Any]]:
        self.reconcile_task_residue()
        return self.process_next_ingress_job() or self.process_next_automation_task()

    def _record_worker_loop_error(self, exc: Exception) -> None:
        try:
            with self.db.connect() as conn:
                self._record_audit_event(
                    conn,
                    event_type='ingress_worker_loop_error',
                    event_source='ingress_worker',
                    payload={
                        'worker_id': self._worker_id,
                        'error': str(exc),
                    },
                )
                conn.commit()
        except Exception:
            pass

    def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            try:
                processed = self.process_next_worker_tick()
                if not processed:
                    self._worker_wakeup.wait(self.ingress_worker_poll_interval)
                    self._worker_wakeup.clear()
            except Exception as exc:
                self._record_worker_loop_error(exc)
                self._worker_wakeup.wait(self.ingress_worker_poll_interval)
                self._worker_wakeup.clear()

    @staticmethod
    def _task_status_rank(status: str) -> int:
        normalized = str(status or '').strip().lower()
        if normalized == 'success':
            return 0
        if normalized == 'failed':
            return 1
        if normalized == 'processing':
            return 2
        if normalized == 'pending':
            return 3
        return 4

    @staticmethod
    def _parse_task_payload_dict(raw_payload: Any) -> Dict[str, Any]:
        try:
            parsed = json.loads(raw_payload or '{}')
        except Exception:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _parse_task_raw_result_dict(raw_result: Any) -> Dict[str, Any]:
        try:
            parsed = json.loads(raw_result or '{}')
        except Exception:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _finalize_bind_task_residue(
        self,
        conn: sqlite3.Connection,
        *,
        row: Dict[str, Any],
        resolved_status: str,
        result_code: str,
        result_reason: str,
        now_iso: str,
    ) -> None:
        raw_result = self._parse_task_raw_result_dict(row.get('raw_result'))
        raw_result.update({
            'execution_disposition': 'auto_reconciled',
            'auto_reconciled': True,
            'auto_reconcile_reason': result_code,
            'auto_reconciled_at': now_iso,
        })
        conn.execute(
            """
            UPDATE automation_tasks
            SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ?, lease_until = '', heartbeat_at = ''
            WHERE task_id = ?
            """,
            (
                resolved_status,
                result_code,
                result_reason,
                now_iso,
                json.dumps(raw_result, ensure_ascii=False),
                row['task_id'],
            ),
        )
        self._record_audit_event(
            conn,
            event_type='automation_task_residue_reconciled',
            event_source='task_residue_reconciler',
            payload={
                'task_type': 'bind_check',
                'task_id': row['task_id'],
                'lead_id': row['lead_id'],
                'resolved_status': resolved_status,
                'result_code': result_code,
                'result_reason': result_reason,
            },
            lead_id=str(row.get('lead_id') or '').strip() or None,
        )

    def _finalize_group_join_task_residue(
        self,
        conn: sqlite3.Connection,
        *,
        row: Dict[str, Any],
        resolved_status: str,
        result_code: str,
        result_reason: str,
        now_iso: str,
        update_lead_status: Optional[str] = None,
    ) -> None:
        raw_result = self._parse_task_raw_result_dict(row.get('raw_result'))
        payload_dict = self._parse_task_payload_dict(row.get('payload'))
        target_group = str(raw_result.get('target_group') or payload_dict.get('target_group') or row.get('resolved_target_group') or '').strip() or None
        raw_result.update({
            'execution_disposition': 'auto_reconciled',
            'auto_reconciled': True,
            'auto_reconcile_reason': result_code,
            'auto_reconciled_at': now_iso,
        })
        if target_group:
            raw_result.setdefault('target_group', target_group)
        conn.execute(
            """
            UPDATE automation_tasks
            SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ?, lease_until = '', heartbeat_at = ''
            WHERE task_id = ?
            """,
            (
                resolved_status,
                result_code,
                result_reason,
                now_iso,
                json.dumps(raw_result, ensure_ascii=False),
                row['task_id'],
            ),
        )
        if update_lead_status:
            current_status = str(row.get('current_status') or '').strip()
            conn.execute(
                "UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?",
                (update_lead_status, now_iso, row['lead_id']),
            )
            if current_status and current_status != update_lead_status:
                self._record_status_history(
                    conn,
                    lead_id=row['lead_id'],
                    from_status=current_status,
                    to_status=update_lead_status,
                    trigger_type='group_join_auto_reconciled',
                    trigger_source='task_residue_reconciler',
                    trigger_task_id=row['task_id'],
                    remark=result_code,
                )
        self._record_audit_event(
            conn,
            event_type='automation_task_residue_reconciled',
            event_source='task_residue_reconciler',
            payload={
                'task_type': 'group_join',
                'task_id': row['task_id'],
                'lead_id': row['lead_id'],
                'resolved_status': resolved_status,
                'result_code': result_code,
                'result_reason': result_reason,
                'target_group': target_group,
                'update_lead_status': update_lead_status,
            },
            lead_id=str(row.get('lead_id') or '').strip() or None,
        )

    def _finalize_crm_task_residue(
        self,
        conn: sqlite3.Connection,
        *,
        row: Dict[str, Any],
        resolved_status: str,
        result_code: str,
        result_reason: str,
        now_iso: str,
    ) -> None:
        raw_result = self._parse_task_raw_result_dict(row.get('raw_result'))
        raw_result.update({
            'execution_disposition': 'auto_reconciled',
            'auto_reconciled': True,
            'auto_reconcile_reason': result_code,
            'auto_reconciled_at': now_iso,
        })
        conn.execute(
            """
            UPDATE automation_tasks
            SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ?, lease_until = '', heartbeat_at = ''
            WHERE task_id = ?
            """,
            (
                resolved_status,
                result_code,
                result_reason,
                now_iso,
                json.dumps(raw_result, ensure_ascii=False),
                row['task_id'],
            ),
        )
        self._record_audit_event(
            conn,
            event_type='automation_task_residue_reconciled',
            event_source='task_residue_reconciler',
            payload={
                'task_type': str(row.get('task_type') or ''),
                'task_id': row['task_id'],
                'lead_id': row.get('lead_id'),
                'resolved_status': resolved_status,
                'result_code': result_code,
                'result_reason': result_reason,
            },
            lead_id=str(row.get('lead_id') or '').strip() or None,
        )

    def reconcile_task_residue(self, *, force: bool = False) -> Dict[str, Any]:
        now_monotonic = time.monotonic()
        if not force and (now_monotonic - self._task_residue_last_reconciled_monotonic) < self._task_residue_reconcile_interval_seconds:
            return {'attempted': False, 'skipped': True}
        now_iso = utc_now()
        now_dt = parse_iso_datetime(now_iso)
        bind_reconciled = 0
        bind_requeued = 0
        group_reconciled = 0
        crm_reconciled = 0
        intake_projection_reconciled = 0
        with self.db.connect() as conn:
            bind_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, t.status, t.created_at, t.started_at, t.raw_result, t.retry_count,
                       COALESCE(t.lease_until, '') AS lease_until,
                       COALESCE(t.worker_id, '') AS worker_id,
                       COALESCE(t.heartbeat_at, '') AS heartbeat_at,
                       COALESCE(l.current_status, '') AS current_status
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'processing'
                ORDER BY datetime(COALESCE(t.started_at, t.created_at)) ASC, t.task_id ASC
                """
            ).fetchall()]
            for row in bind_rows:
                anchor = str(row.get('started_at') or row.get('created_at') or '').strip()
                if not anchor:
                    continue
                try:
                    age_seconds = max(0.0, (now_dt - parse_iso_datetime(anchor)).total_seconds())
                except Exception:
                    continue
                expired_lease = False
                lease_until = str(row.get('lease_until') or '').strip()
                if lease_until:
                    try:
                        expired_lease = parse_iso_datetime(lease_until) <= now_dt
                    except Exception:
                        expired_lease = False
                if age_seconds < self._bind_processing_stale_seconds and not expired_lease:
                    continue
                lead_status = str(row.get('current_status') or '').strip().lower()
                if lead_status in {'bind_success', 'group_join_pending', 'group_join_failed', 'group_join_success', 'synced'}:
                    self._finalize_bind_task_residue(
                        conn,
                        row=row,
                        resolved_status='success',
                        result_code='bind_auto_reconciled_success',
                        result_reason='bind processing residue auto-closed from downstream terminal state',
                        now_iso=now_iso,
                    )
                    bind_reconciled += 1
                elif lead_status in {'bind_failed', 'manual_review_pending'}:
                    self._finalize_bind_task_residue(
                        conn,
                        row=row,
                        resolved_status='failed',
                        result_code='bind_auto_reconciled_failed',
                        result_reason='bind processing residue auto-closed from lead failed state',
                        now_iso=now_iso,
                    )
                    bind_reconciled += 1
                elif lead_status in {'account_submitted', 'bind_check_pending', 'new', ''}:
                    reason_code = 'bind_auto_requeued_expired_lease' if expired_lease else 'bind_auto_requeued_stale_processing'
                    reason_text = 'expired bind task lease requeued for normal worker retry' if expired_lease else 'stale bind processing task requeued after worker interruption'
                    raw_result = self._parse_task_raw_result_dict(row.get('raw_result'))
                    retry_count = int(row.get('retry_count') or 0)
                    raw_result.update({
                        'execution_disposition': 'auto_requeued',
                        'auto_requeued': True,
                        'auto_requeue_reason': reason_code,
                        'auto_requeued_at': now_iso,
                    })
                    conn.execute(
                        """
                        UPDATE automation_tasks
                        SET status = 'pending', started_at = NULL, retry_count = ?,
                            result_code = ?,
                            result_reason = ?,
                            raw_result = ?, worker_id = '', lease_until = '', heartbeat_at = ''
                        WHERE task_id = ? AND status = 'processing'
                        """,
                        (retry_count + 1, reason_code, reason_text, json.dumps(raw_result, ensure_ascii=False), row['task_id']),
                    )
                    self._record_audit_event(
                        conn,
                        event_type='automation_task_residue_requeued',
                        event_source='task_residue_reconciler',
                        payload={
                            'task_type': 'bind_check',
                            'task_id': row['task_id'],
                            'lead_id': row['lead_id'],
                            'result_code': reason_code,
                            'result_reason': reason_text,
                        },
                        lead_id=str(row.get('lead_id') or '').strip() or None,
                    )
                    bind_requeued += 1

            pending_group_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, t.status, t.created_at, t.started_at, t.payload, t.raw_result,
                       COALESCE(l.current_status, '') AS current_status,
                       COALESCE(l.mobile, '') AS mobile,
                       COALESCE(l.area_code, 0) AS area_code,
                       COALESCE(l.country, '') AS country,
                       COALESCE(l.crm_verified_official_group, '') AS crm_verified_official_group,
                       COALESCE(l.updated_at, '') AS lead_updated_at
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'group_join' AND t.status = 'pending'
                ORDER BY t.lead_id ASC, datetime(t.created_at) ASC, t.task_id ASC
                """
            ).fetchall()]
            grouped_pending: Dict[str, List[Dict[str, Any]]] = {}
            for row in pending_group_rows:
                grouped_pending.setdefault(str(row.get('lead_id') or '').strip(), []).append(row)
            for lead_id, rows in grouped_pending.items():
                active_rows = [row for row in rows if row.get('task_id')]
                if not active_rows:
                    continue
                newest = sorted(
                    active_rows,
                    key=lambda item: (str(item.get('created_at') or ''), str(item.get('task_id') or '')),
                    reverse=True,
                )[0]
                lead_status = str(newest.get('current_status') or '').strip().lower()
                payload_dict = self._parse_task_payload_dict(newest.get('payload'))
                resolved_target_group = str(
                    payload_dict.get('target_group') or newest.get('crm_verified_official_group') or ''
                ).strip()
                if not resolved_target_group:
                    lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
                    if lead_row:
                        resolved_target_group = str(self._resolve_official_group_target_group(lead=dict(lead_row)) or '').strip()
                newest['resolved_target_group'] = resolved_target_group
                for older in active_rows:
                    if older['task_id'] == newest['task_id']:
                        continue
                    older['resolved_target_group'] = resolved_target_group
                    self._finalize_group_join_task_residue(
                        conn,
                        row=older,
                        resolved_status='cancelled',
                        result_code='group_join_auto_superseded_duplicate',
                        result_reason='older duplicate pending group_join task auto-cancelled',
                        now_iso=now_iso,
                    )
                    group_reconciled += 1
                anchor = str(newest.get('started_at') or newest.get('created_at') or '').strip()
                try:
                    newest_age_seconds = max(0.0, (now_dt - parse_iso_datetime(anchor)).total_seconds()) if anchor else 0.0
                except Exception:
                    newest_age_seconds = 0.0
                if lead_status in {'group_join_success', 'synced'}:
                    self._finalize_group_join_task_residue(
                        conn,
                        row=newest,
                        resolved_status='success',
                        result_code='group_join_auto_reconciled_success',
                        result_reason='pending group_join residue auto-closed from lead success state',
                        now_iso=now_iso,
                    )
                    group_reconciled += 1
                    continue
                if lead_status in {'group_join_failed', 'bind_failed'}:
                    self._finalize_group_join_task_residue(
                        conn,
                        row=newest,
                        resolved_status='failed',
                        result_code='group_join_auto_reconciled_failed',
                        result_reason='pending group_join residue auto-closed from lead failed state',
                        now_iso=now_iso,
                    )
                    group_reconciled += 1
                    continue
                if lead_status not in {'bind_success', 'group_join_pending'}:
                    continue
                if newest_age_seconds < self._group_join_pending_stale_seconds:
                    continue
                verified_official_group = str(newest.get('crm_verified_official_group') or '').strip()
                if verified_official_group and (not resolved_target_group or verified_official_group == resolved_target_group):
                    self._finalize_group_join_task_residue(
                        conn,
                        row=newest,
                        resolved_status='success',
                        result_code='group_join_auto_reconciled_success_from_verified_official_group',
                        result_reason='pending group_join residue auto-closed from verified official-group evidence',
                        now_iso=now_iso,
                        update_lead_status='group_join_success',
                    )
                    group_reconciled += 1
                    continue
                if not resolved_target_group:
                    continue
                requester_still_pending = self._official_group_requester_pending_in_runtime(
                    target_group=resolved_target_group,
                    target_phone_hint=str(newest.get('mobile') or '').strip() or None,
                    target_requester_id=None,
                )
                if requester_still_pending:
                    continue
                self._finalize_group_join_task_residue(
                    conn,
                    row=newest,
                    resolved_status='failed',
                    result_code='group_join_auto_closed_missing_runtime_requester',
                    result_reason='pending group_join residue auto-closed because runtime no longer has matching requester',
                    now_iso=now_iso,
                    update_lead_status='group_join_failed',
                )
                group_reconciled += 1

            crm_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, t.task_type, t.status, t.created_at, t.started_at, t.raw_result,
                       COALESCE(l.current_status, '') AS current_status,
                       COALESCE(l.crm_verified_at, '') AS crm_verified_at
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type IN ('crm_sync', 'crm_sync_retry')
                  AND t.status IN ('pending', 'processing')
                ORDER BY datetime(COALESCE(t.started_at, t.created_at)) ASC, t.task_id ASC
                """
            ).fetchall()]
            for row in crm_rows:
                anchor = str(row.get('started_at') or row.get('created_at') or '').strip()
                if not anchor:
                    continue
                try:
                    age_seconds = max(0.0, (now_dt - parse_iso_datetime(anchor)).total_seconds())
                except Exception:
                    continue
                if age_seconds < self._crm_task_stale_seconds:
                    continue
                lead_id = str(row.get('lead_id') or '').strip()
                latest_success = None
                latest_failure = None
                if lead_id:
                    latest_success = conn.execute(
                        "SELECT response_snapshot FROM sync_logs WHERE lead_id = ? AND sync_type = 'customer_upsert' AND target_system = 'crm' AND status = 'success' ORDER BY created_at DESC LIMIT 1",
                        (lead_id,),
                    ).fetchone()
                    latest_failure = conn.execute(
                        "SELECT response_snapshot FROM sync_logs WHERE lead_id = ? AND sync_type = 'customer_upsert' AND target_system = 'crm' AND status = 'failed' ORDER BY created_at DESC LIMIT 1",
                        (lead_id,),
                    ).fetchone()
                if str(row.get('current_status') or '').strip() == 'synced' or str(row.get('crm_verified_at') or '').strip() or latest_success:
                    self._finalize_crm_task_residue(
                        conn,
                        row=row,
                        resolved_status='success',
                        result_code='crm_auto_reconciled_success',
                        result_reason='stale crm task auto-closed from verified CRM evidence',
                        now_iso=now_iso,
                    )
                    crm_reconciled += 1
                    continue
                current_status = str(row.get('current_status') or '').strip().lower()
                if current_status in {'group_join_failed', 'group_join_success', 'synced'} and latest_failure:
                    self._finalize_crm_task_residue(
                        conn,
                        row=row,
                        resolved_status='failed',
                        result_code='crm_auto_reconciled_failed',
                        result_reason='stale crm task auto-closed from downstream terminal state and failed crm evidence',
                        now_iso=now_iso,
                    )
                    crm_reconciled += 1
            intake_projection_reconciled = len(self._reconcile_ops_intake_terminal_projections(
                conn,
                now_iso=now_iso,
                limit=100,
            ))
            conn.commit()
        self._task_residue_last_reconciled_monotonic = now_monotonic
        return {
            'attempted': True,
            'bind_reconciled_count': bind_reconciled,
            'bind_requeued_count': bind_requeued,
            'group_join_reconciled_count': group_reconciled,
            'crm_reconciled_count': crm_reconciled,
            'intake_projection_reconciled_count': intake_projection_reconciled,
        }

    def _record_audit_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_type: str,
        event_source: str,
        payload: Dict[str, Any],
        lead_id: Optional[str] = None,
        ingress_event_id: Optional[str] = None,
    ) -> None:
        safe_payload = _redact_sensitive_payload(payload)
        conn.execute(
            "INSERT INTO operator_audit_log (audit_id, lead_id, ingress_event_id, event_type, event_source, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                create_id('audit'),
                lead_id,
                ingress_event_id,
                event_type,
                event_source,
                json.dumps(safe_payload, ensure_ascii=False),
                utc_now(),
            ),
        )

    def _enqueue_ingress_event(self, *, ingress_type: str, source_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        idempotency_key = fingerprint_payload(ingress_type=ingress_type, payload=payload)
        now = utc_now()
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT event_id, status, result_snapshot FROM ingress_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                result_snapshot = existing['result_snapshot'] or '{}'
                self._record_audit_event(
                    conn,
                    event_type='ingress_event_reused',
                    event_source=ingress_type,
                    payload={'idempotency_key': idempotency_key, 'status': existing['status']},
                    ingress_event_id=existing['event_id'],
                )
                conn.commit()
                self._notify_worker_new_work()
                return {
                    'event_id': existing['event_id'],
                    'queued': str(existing['status']) in {'queued', 'processing'},
                    'duplicate': True,
                    'status': existing['status'],
                    'result_snapshot': json.loads(result_snapshot),
                }
            event_id = create_id('ingress')
            job_id = create_id('job')
            conn.execute(
                "INSERT INTO ingress_events (event_id, ingress_type, source_key, idempotency_key, payload, status, result_snapshot, created_at, updated_at, processed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, ingress_type, source_key, idempotency_key, json.dumps(payload, ensure_ascii=False), 'queued', '{}', now, now, None),
            )
            conn.execute(
                """
                INSERT INTO ingress_jobs (
                    job_id, event_id, status, attempt_count, available_at,
                    worker_id, lease_until, heartbeat_at, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, event_id, 'queued', 0, now, '', '', '', None, now, now),
            )
            self._record_audit_event(
                conn,
                event_type='ingress_event_enqueued',
                event_source=ingress_type,
                payload={'idempotency_key': idempotency_key, 'source_key': source_key},
                ingress_event_id=event_id,
            )
            conn.commit()
            self._notify_worker_new_work()
            return {'event_id': event_id, 'queued': True, 'duplicate': False, 'status': 'queued'}

    def _persist_ingress_job_result(self, *, row: Dict[str, Any], event: sqlite3.Row, status: str, error_text: Optional[str], result: Dict[str, Any]) -> None:
        last_exc: Optional[Exception] = None
        for attempt in range(5):
            try:
                with self.db.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    now = utc_now()
                    attempt_count = int(row.get('attempt_count') or 0)
                    job_status = status
                    event_status = status
                    available_at = now
                    processed_at: Optional[str] = now
                    result_snapshot = dict(result or {})
                    if status == 'failed' and attempt_count < self._ingress_job_max_attempts:
                        job_status = 'queued'
                        event_status = 'queued'
                        processed_at = None
                        delay_seconds = min(60, max(1, attempt_count) * 5)
                        available_at = (parse_iso_datetime(now) + timedelta(seconds=delay_seconds)).isoformat()
                        result_snapshot.update({
                            'retry_scheduled': True,
                            'attempt_count': attempt_count,
                            'max_attempts': self._ingress_job_max_attempts,
                            'next_available_at': available_at,
                        })
                    conn.execute(
                        """
                        UPDATE ingress_jobs
                        SET status = ?, available_at = ?, worker_id = '', lease_until = '', heartbeat_at = '',
                            last_error = ?, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (job_status, available_at, error_text, now, row['job_id']),
                    )
                    conn.execute(
                        "UPDATE ingress_events SET status = ?, result_snapshot = ?, updated_at = ?, processed_at = ? WHERE event_id = ?",
                        (event_status, json.dumps(result_snapshot, ensure_ascii=False), now, processed_at, row['event_id']),
                    )
                    self._record_audit_event(
                        conn,
                        event_type='ingress_event_processed',
                        event_source=event['ingress_type'],
                        payload={
                            'status': job_status,
                            'event_status': event_status,
                            'attempt_count': attempt_count,
                            'max_attempts': self._ingress_job_max_attempts,
                            'error': error_text,
                            'result': result_snapshot,
                        },
                        ingress_event_id=row['event_id'],
                    )
                    conn.commit()
                    return
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower():
                    raise
                last_exc = exc
                time.sleep(0.2 * (attempt + 1))
        if last_exc is not None:
            raise last_exc

    def recover_stale_ingress_jobs(self) -> Dict[str, Any]:
        now = utc_now()
        stale_before = (parse_iso_datetime(now) - timedelta(seconds=self._ingress_job_lease_seconds)).isoformat()
        recovered = 0
        failed = 0
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """
                SELECT j.job_id, j.event_id, j.attempt_count, COALESCE(j.worker_id, '') AS worker_id,
                       COALESCE(j.lease_until, '') AS lease_until, COALESCE(j.updated_at, '') AS updated_at,
                       e.ingress_type
                FROM ingress_jobs j
                JOIN ingress_events e ON e.event_id = j.event_id
                WHERE j.status = 'processing'
                  AND (
                    (COALESCE(j.lease_until, '') <> '' AND j.lease_until <= ?)
                    OR (COALESCE(j.lease_until, '') = '' AND j.updated_at <= ?)
                  )
                ORDER BY j.updated_at ASC, j.job_id ASC
                LIMIT 100
                """,
                (now, stale_before),
            ).fetchall()]
            for row in rows:
                attempt_count = int(row.get('attempt_count') or 0)
                if attempt_count >= self._ingress_job_max_attempts:
                    result = {
                        'accepted': False,
                        'reason': 'ingress_job_lease_expired_max_attempts',
                        'attempt_count': attempt_count,
                        'max_attempts': self._ingress_job_max_attempts,
                        'worker_id': row.get('worker_id') or '',
                        'lease_until': row.get('lease_until') or '',
                    }
                    conn.execute(
                        """
                        UPDATE ingress_jobs
                        SET status = 'failed', worker_id = '', lease_until = '', heartbeat_at = '',
                            last_error = ?, updated_at = ?
                        WHERE job_id = ? AND status = 'processing'
                        """,
                        ('ingress job lease expired after max attempts', now, row['job_id']),
                    )
                    conn.execute(
                        "UPDATE ingress_events SET status = 'failed', result_snapshot = ?, updated_at = ?, processed_at = ? WHERE event_id = ?",
                        (json.dumps(result, ensure_ascii=False), now, now, row['event_id']),
                    )
                    self._record_audit_event(
                        conn,
                        event_type='ingress_job_lease_failed',
                        event_source='ingress_lease_recovery',
                        payload=result,
                        ingress_event_id=row['event_id'],
                    )
                    failed += 1
                    continue
                result = {
                    'reason': 'ingress_job_lease_expired_requeued',
                    'attempt_count': attempt_count,
                    'max_attempts': self._ingress_job_max_attempts,
                    'worker_id': row.get('worker_id') or '',
                    'lease_until': row.get('lease_until') or '',
                }
                conn.execute(
                    """
                    UPDATE ingress_jobs
                    SET status = 'queued', available_at = ?, worker_id = '', lease_until = '', heartbeat_at = '',
                        last_error = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'processing'
                    """,
                    (now, 'ingress job lease expired; requeued', now, row['job_id']),
                )
                conn.execute(
                    "UPDATE ingress_events SET status = 'queued', result_snapshot = ?, updated_at = ?, processed_at = NULL WHERE event_id = ?",
                    (json.dumps(result, ensure_ascii=False), now, row['event_id']),
                )
                self._record_audit_event(
                    conn,
                    event_type='ingress_job_lease_requeued',
                    event_source='ingress_lease_recovery',
                    payload=result,
                    ingress_event_id=row['event_id'],
                )
                recovered += 1
            conn.commit()
        return {'attempted': True, 'requeued_count': recovered, 'failed_count': failed}

    def process_next_ingress_job(self) -> Optional[Dict[str, Any]]:
        self.recover_stale_ingress_jobs()
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            now = utc_now()
            row = conn.execute(
                """
                SELECT job_id, event_id, attempt_count
                FROM ingress_jobs
                WHERE status = 'queued' AND available_at <= ?
                ORDER BY available_at ASC, created_at ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                conn.commit()
                return None
            lease_until = (parse_iso_datetime(now) + timedelta(seconds=self._ingress_job_lease_seconds)).isoformat()
            cursor = conn.execute(
                """
                UPDATE ingress_jobs
                SET status = 'processing', attempt_count = attempt_count + 1,
                    worker_id = ?, lease_until = ?, heartbeat_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (self._worker_id, lease_until, now, now, row['job_id']),
            )
            if cursor.rowcount <= 0:
                conn.commit()
                return None
            conn.execute("UPDATE ingress_events SET status = 'processing', updated_at = ? WHERE event_id = ?", (now, row['event_id']))
            event = conn.execute("SELECT ingress_type, payload FROM ingress_events WHERE event_id = ?", (row['event_id'],)).fetchone()
            conn.commit()
        claimed_row = dict(row)
        claimed_row['attempt_count'] = int(claimed_row.get('attempt_count') or 0) + 1
        claimed_row['worker_id'] = self._worker_id
        claimed_row['lease_until'] = lease_until
        if not event:
            return None
        payload = json.loads(event['payload'] or '{}')
        try:
            if event['ingress_type'] == 'lark_event':
                result = self._handle_lark_event_sync(payload)
            elif event['ingress_type'] == 'manual_cs_submission':
                result = self._submit_manual_cs_sync(ManualCsSubmissionRequest(**payload))
            elif event['ingress_type'] == 'registration_group_approval_decision':
                result = self._registration_group_approval_decision_sync(
                    RegistrationGroupApprovalDecisionRequest(**{k: v for k, v in payload.items() if k != 'approval_run_id'}),
                    approval_run_id=str(payload.get('approval_run_id') or '').strip() or None,
                )
            else:
                raise RuntimeError(f'unsupported ingress_type: {event["ingress_type"]}')
            status = 'done'
            error_text = None
        except Exception as exc:
            result = {'accepted': False, 'reason': 'ingress_processing_failed', 'error': str(exc)}
            status = 'failed'
            error_text = str(exc)
        self._persist_ingress_job_result(row=claimed_row, event=event, status=status, error_text=error_text, result=result)
        return {'event_id': row['event_id'], 'status': status, 'result': result}

    def _resolve_executor_proxy_url(self, executor: Optional[Dict[str, Any]]) -> str:
        if not executor:
            return ''
        explicit = str(executor.get('proxy_url') or '').strip()
        if explicit:
            return explicit
        region = str(executor.get('proxy_region') or '').strip()
        if not region:
            return ''
        return str(self.guild_executor_proxy_region_urls.get(region) or '').strip()

    def _build_bind_execution_result(self, *, task_id: str) -> BindCheckResultRequest:
        with self.db.connect() as conn:
            task = conn.execute("SELECT lead_id, payload FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            task_payload = json.loads(task['payload'] or '{}')
            lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (task['lead_id'],)).fetchone()
        if not lead_row:
            raise HTTPException(status_code=404, detail="lead not found")
        lead = dict(lead_row)
        invite_code = str(lead.get('inviter_id') or '').strip().upper()
        account_id = str(task_payload.get('account_id') or '').strip()
        expected_guild = self._resolve_expected_bind_guild(task_payload=task_payload, lead_row=lead_row) or str(lead.get('dept_name') or '').strip()
        route_snapshot = task_payload.get('route_snapshot') if isinstance(task_payload.get('route_snapshot'), dict) else {}
        snapshot_bind_route = str(route_snapshot.get('bind_route') or '').strip()
        context = {
            'task_id': task_id,
            'lead_id': task['lead_id'],
            'submission_id': str(task_payload.get('submission_id') or ''),
            'account_id': account_id,
            'mobile': str(lead.get('mobile') or ''),
            'country': str(lead.get('country') or ''),
            'area_code': int(lead.get('area_code') or 0),
            'app_name': str(lead.get('app_name') or ''),
            'dept_name': expected_guild,
            'registration_group': str(lead.get('pendaftaran_group') or ''),
            'invite_code': invite_code,
            'source_bot_app_id': str(task_payload.get('source_bot_app_id') or ''),
        }
        executor = self.resolve_guild_executor(expected_guild)
        country_guard = self._guild_executor_country_guard(expected_guild, lead.get('country'))
        if not country_guard.get('allowed', True):
            return BindCheckResultRequest(
                status='failed',
                result_code='country_guild_mismatch',
                result_reason=(
                    f"User country {country_guard.get('user_country') or '-'} does not match "
                    f"guild country {country_guard.get('guild_country') or '-'}"
                ),
                finished_at=utc_now(),
                raw_result={
                    'guild_code': expected_guild,
                    'user_country': country_guard.get('user_country') or '',
                    'guild_country': country_guard.get('guild_country') or '',
                    'blocked_before_bind': True,
                },
            )
        if executor:
            has_platform_cms = bool(str(executor.get('platform_backend_url') or '').strip() and str(executor.get('platform_authorization') or '').strip())
            context.update({
                'bind_route': snapshot_bind_route or ('cms_id' if has_platform_cms else 'guild_invite_code'),
                'executor_slot_key': str(task_payload.get('executor_slot_key') or ''),
                'executor_slot_index': int(task_payload.get('executor_slot_index') or 1),
                'executor_slot_count': int(task_payload.get('executor_slot_count') or executor.get('bind_concurrency') or 1),
                'executor_slot_hidden': bool(task_payload.get('executor_slot_hidden', False)),
                'executor_backend_url': str(executor.get('backend_url') or ''),
                'executor_oauth_token': str(executor.get('oauth_token') or ''),
                'executor_oauth_token_secret': str(executor.get('oauth_token_secret') or ''),
                'executor_guild_backend_token': str(executor.get('guild_backend_token') or ''),
                'executor_login_username': str(executor.get('login_username') or ''),
                'executor_password_secret_ref': str(executor.get('password_secret_ref') or ''),
                'executor_platform_backend_url': str(executor.get('platform_backend_url') or ''),
                'executor_platform_authorization': str(executor.get('platform_authorization') or ''),
                'executor_cms_refresh_token': str(executor.get('cms_refresh_token') or ''),
                'executor_cms_refresh_token_deadtime': executor.get('cms_refresh_token_deadtime'),
                'executor_refresh_persist_callback': lambda refresh_result, guild_name=expected_guild: self.persist_cms_executor_refresh_result(guild_name, refresh_result),
                'executor_cms_guild_id': str(executor.get('cms_guild_id') or ''),
                'executor_cms_guild_sid': str(executor.get('cms_guild_sid') or ''),
                'executor_proxy_url': self._resolve_executor_proxy_url(executor),
                'executor_proxy_region': str(executor.get('proxy_region') or ''),
                'executor_proxy_type': str(executor.get('proxy_type') or ''),
                'executor_browser_profile_key': str(executor.get('browser_profile_key') or ''),
                'executor_bind_concurrency': int(executor.get('bind_concurrency') or 1),
                'executor_request_timeout_seconds': int(executor.get('request_timeout_seconds') or 30),
            })
        if callable(self.bind_simulator):
            simulated = self.bind_simulator(context)
            if not isinstance(simulated, dict):
                raise RuntimeError('bind simulator must return a dict')
            return BindCheckResultRequest(
                status=str(simulated.get('status') or 'failed'),
                result_code=simulated.get('result_code'),
                result_reason=simulated.get('result_reason'),
                finished_at=utc_now(),
                raw_result=simulated.get('raw_result') or {},
            )
        if callable(self.real_bind_executor):
            executed = self.real_bind_executor(context)
            if not isinstance(executed, dict):
                raise RuntimeError('real bind executor must return a dict')
            return BindCheckResultRequest(
                status=str(executed.get('status') or 'failed'),
                result_code=executed.get('result_code'),
                result_reason=executed.get('result_reason'),
                finished_at=utc_now(),
                raw_result=executed.get('raw_result') or {},
            )
        return BindCheckResultRequest(
            status='failed',
            result_code='bind_executor_unavailable',
            result_reason='Bind executor unavailable. Check backend runtime.',
            finished_at=utc_now(),
            raw_result={
                'guild_code': lead.get('dept_name') or '',
                'invite_code': invite_code,
                'bind_route': context.get('bind_route') or '',
                'executor_expected': bool(executor),
                'executor_available': False,
            },
        )

    @staticmethod
    def _executor_slot_count(executor: Optional[Dict[str, Any]]) -> int:
        try:
            return max(1, int((executor or {}).get('bind_concurrency') or 1))
        except Exception:
            return 1

    @staticmethod
    def _executor_slot_key(guild_name: str, slot_index: int) -> str:
        normalized_guild = str(guild_name or '').strip() or 'unknown'
        return f'{normalized_guild}#slot-{max(1, int(slot_index or 1))}'

    def _used_executor_slot_indexes(self, rows: list[Dict[str, Any]]) -> set[int]:
        used: set[int] = set()
        legacy_processing_without_slot = 0
        for row in rows:
            payload = self._parse_task_payload_dict(row.get('payload'))
            slot_index = payload.get('executor_slot_index')
            try:
                slot_number = int(slot_index)
            except Exception:
                slot_number = 0
            if slot_number > 0:
                used.add(slot_number)
            else:
                legacy_processing_without_slot += 1
        for idx in range(1, legacy_processing_without_slot + 1):
            used.add(idx)
        return used

    def _next_hidden_executor_slot_payload(
        self,
        *,
        guild_name: str,
        executor: Optional[Dict[str, Any]],
        processing_rows: list[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        slot_count = self._executor_slot_count(executor)
        used_slots = self._used_executor_slot_indexes(processing_rows)
        for slot_index in range(1, slot_count + 1):
            if slot_index not in used_slots:
                return {
                    'executor_slot_key': self._executor_slot_key(guild_name, slot_index),
                    'executor_slot_index': slot_index,
                    'executor_slot_count': slot_count,
                    'executor_slot_hidden': True,
                }
        return None

    def _select_next_bind_task(self) -> Optional[Dict[str, Any]]:
        now = utc_now()
        lease_until = (parse_iso_datetime(now) + timedelta(seconds=self._bind_task_lease_seconds)).isoformat()
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.payload, t.lead_id, t.created_at, t.priority,
                       COALESCE(l.dept_name, '') AS dept_name, COALESCE(l.current_status, '') AS lead_current_status
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'pending'
                  AND COALESCE(l.current_status, '') NOT IN ('archived_test_residue', 'console_cleared_test_data')
                ORDER BY
                  CASE WHEN datetime(t.created_at) >= datetime('now', '-10 minutes') THEN 0 ELSE 1 END ASC,
                  CASE COALESCE(t.priority, '') WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END ASC,
                  datetime(t.created_at) ASC,
                  t.task_id ASC
                LIMIT 500
                """
            ).fetchall()]
            if not rows:
                conn.commit()
                return None
            processing_rows = [dict(r) for r in conn.execute(
                """
                SELECT COALESCE(l.dept_name, '') AS guild_name, t.payload
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'processing'
                """
            ).fetchall()]
            processing_by_guild: Dict[str, list[Dict[str, Any]]] = {}
            processing_bind_keys: set[str] = set()
            for processing_row in processing_rows:
                guild_key = str(processing_row.get('guild_name') or '').strip()
                processing_by_guild.setdefault(guild_key, []).append(processing_row)
                processing_payload = self._parse_task_payload_dict(processing_row.get('payload'))
                processing_sid = str(processing_payload.get('account_id') or '').strip()
                if guild_key and processing_sid:
                    processing_bind_keys.add(f'{guild_key.lower()}\u001f{processing_sid}')
            executor_cache: Dict[str, Optional[Dict[str, Any]]] = {}
            for row in rows:
                guild_name = str(row.get('dept_name') or '').strip()
                if guild_name not in executor_cache:
                    executor_cache[guild_name] = self.resolve_guild_executor(guild_name) if guild_name else None
                executor = executor_cache[guild_name]
                guild_processing_rows = processing_by_guild.get(guild_name, [])
                slot_payload = self._next_hidden_executor_slot_payload(
                    guild_name=guild_name,
                    executor=executor,
                    processing_rows=guild_processing_rows,
                )
                if not slot_payload:
                    continue
                task_payload = self._parse_task_payload_dict(row.get('payload'))
                task_sid = str(task_payload.get('account_id') or '').strip()
                bind_key = f'{guild_name.lower()}\u001f{task_sid}' if guild_name and task_sid else ''
                if bind_key and bind_key in processing_bind_keys:
                    continue
                task_payload.update(slot_payload)
                new_payload = json.dumps(task_payload, ensure_ascii=False)
                cursor = conn.execute(
                    """
                    UPDATE automation_tasks
                    SET status = 'processing', started_at = COALESCE(started_at, ?), payload = ?,
                        worker_id = ?, lease_until = ?, heartbeat_at = ?
                    WHERE task_id = ? AND status = 'pending'
                    """,
                    (now, new_payload, self._worker_id, lease_until, now, row['task_id']),
                )
                if cursor.rowcount:
                    conn.commit()
                    row['started_at'] = row.get('started_at') or now
                    row['task_type'] = 'bind_check'
                    row['payload'] = new_payload
                    processing_by_guild.setdefault(guild_name, []).append({'guild_name': guild_name, 'payload': new_payload})
                    if bind_key:
                        processing_bind_keys.add(bind_key)
                    return row
            conn.commit()
            return None

    def _select_next_crm_retry_task(self) -> Optional[Dict[str, Any]]:
        now = utc_now()
        now_dt = parse_iso_datetime(now)
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.payload, t.lead_id, t.created_at, t.retry_count, t.result_code, t.result_reason
                FROM automation_tasks t
                WHERE t.task_type = 'crm_sync_retry' AND t.status = 'pending'
                ORDER BY t.created_at ASC
                LIMIT 200
                """
            ).fetchall()]
            for row in rows:
                try:
                    payload = json.loads(row.get('payload') or '{}')
                except Exception:
                    payload = {}
                next_retry_at = str(payload.get('next_retry_at') or '').strip()
                if next_retry_at:
                    try:
                        if parse_iso_datetime(next_retry_at) > now_dt:
                            continue
                    except Exception:
                        pass
                cursor = conn.execute(
                    "UPDATE automation_tasks SET status = 'processing', started_at = COALESCE(started_at, ?) WHERE task_id = ? AND status = 'pending'",
                    (now, row['task_id']),
                )
                if cursor.rowcount:
                    conn.commit()
                    row['started_at'] = row.get('started_at') or now
                    row['task_type'] = 'crm_sync_retry'
                    row['payload_dict'] = payload
                    return row
            return None

    def _calculate_bind_metrics(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            pending_rows = [dict(r) for r in conn.execute(
                """
                SELECT COALESCE(l.dept_name, '') AS guild_name, MIN(t.created_at) AS oldest_created_at, COUNT(*) AS pending_count,
                       COALESCE(l.current_status, '') AS lead_current_status
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'pending'
                GROUP BY COALESCE(l.dept_name, ''), COALESCE(l.current_status, '')
                """
            ).fetchall()]
            processing_rows = [dict(r) for r in conn.execute(
                """
                SELECT COALESCE(l.dept_name, '') AS guild_name, COUNT(*) AS processing_count,
                       COALESCE(l.current_status, '') AS lead_current_status
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'processing'
                GROUP BY COALESCE(l.dept_name, ''), COALESCE(l.current_status, '')
                """
            ).fetchall()]
            completed_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.created_at, t.started_at, t.finished_at, t.result_code, t.result_reason,
                       COALESCE(l.current_status, '') AS lead_current_status
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check'
                  AND t.started_at IS NOT NULL
                  AND t.finished_at IS NOT NULL
                  AND t.status IN ('success', 'failed')
                ORDER BY t.finished_at DESC
                LIMIT 100
                """
            ).fetchall()]
        pending_rows = [
            row for row in pending_rows
            if str(row.get('lead_current_status') or '').strip().lower() not in IGNORED_HISTORY_LEAD_STATUSES
        ]
        processing_rows = [
            row for row in processing_rows
            if str(row.get('lead_current_status') or '').strip().lower() not in IGNORED_HISTORY_LEAD_STATUSES
        ]
        completed_rows = [
            row for row in completed_rows
            if str(row.get('lead_current_status') or '').strip().lower() not in IGNORED_HISTORY_LEAD_STATUSES
        ]
        now_dt = datetime.now(timezone.utc)
        pending_by_guild = {str(r.get('guild_name') or '').strip(): r for r in pending_rows}
        processing_by_guild = {str(r.get('guild_name') or '').strip(): int(r.get('processing_count') or 0) for r in processing_rows}
        guild_names = sorted(set(pending_by_guild.keys()) | set(processing_by_guild.keys()))
        per_guild = []
        oldest_pending_age_seconds = 0.0
        for guild_name in guild_names:
            executor = self.resolve_guild_executor(guild_name) if guild_name else None
            bind_limit = int((executor or {}).get('bind_concurrency') or 1)
            pending_count = int((pending_by_guild.get(guild_name) or {}).get('pending_count') or 0)
            processing_count = int(processing_by_guild.get(guild_name) or 0)
            oldest_created_at = (pending_by_guild.get(guild_name) or {}).get('oldest_created_at')
            oldest_age = 0.0
            if oldest_created_at:
                oldest_age = max(0.0, round((now_dt - parse_iso_datetime(str(oldest_created_at))).total_seconds(), 3))
                oldest_pending_age_seconds = max(oldest_pending_age_seconds, oldest_age)
            per_guild.append({
                'guild_name': guild_name,
                'pending_count': pending_count,
                'processing_count': processing_count,
                'bind_concurrency': max(1, bind_limit),
                'available_slots': max(0, max(1, bind_limit) - processing_count),
                'oldest_pending_age_seconds': oldest_age,
            })
        queue_waits = []
        execution_times = []
        end_to_end_times = []
        clean_queue_waits = []
        clean_execution_times = []
        clean_end_to_end_times = []
        excluded_reasons: Dict[str, int] = {}
        clean_long_tail_threshold_seconds = 300.0
        for row in completed_rows:
            try:
                created_at = parse_iso_datetime(str(row.get('created_at') or ''))
                started_at = parse_iso_datetime(str(row.get('started_at') or ''))
                finished_at = parse_iso_datetime(str(row.get('finished_at') or ''))
            except Exception:
                excluded_reasons['invalid_timestamp'] = excluded_reasons.get('invalid_timestamp', 0) + 1
                continue
            queue_wait = max(0.0, (started_at - created_at).total_seconds())
            execution_time = max(0.0, (finished_at - started_at).total_seconds())
            end_to_end_time = max(0.0, (finished_at - created_at).total_seconds())
            queue_waits.append(queue_wait)
            execution_times.append(execution_time)
            end_to_end_times.append(end_to_end_time)
            result_code = str(row.get('result_code') or '').strip().lower()
            result_reason = str(row.get('result_reason') or '').strip().lower()
            exclude_reason = ''
            if 'auto_reconciled' in result_code or 'auto_requeued' in result_code:
                exclude_reason = 'auto_reconciled_or_requeued'
            elif 'duplicate' in result_code or 'data duplication' in result_reason or 'duplicate_sid' in result_reason:
                exclude_reason = 'duplicate'
            elif end_to_end_time > clean_long_tail_threshold_seconds or queue_wait > clean_long_tail_threshold_seconds or execution_time > clean_long_tail_threshold_seconds:
                exclude_reason = 'long_tail'
            if exclude_reason:
                excluded_reasons[exclude_reason] = excluded_reasons.get(exclude_reason, 0) + 1
                continue
            clean_queue_waits.append(queue_wait)
            clean_execution_times.append(execution_time)
            clean_end_to_end_times.append(end_to_end_time)
        def _avg(values: List[float]) -> float:
            if not values:
                return 0.0
            return round(sum(values) / len(values), 3)
        def _median(values: List[float]) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            mid = len(ordered) // 2
            if len(ordered) % 2:
                return round(ordered[mid], 3)
            return round((ordered[mid - 1] + ordered[mid]) / 2.0, 3)
        def _p90(values: List[float]) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.9) - 1))
            return round(ordered[index], 3)
        clean_metrics = {
            'sample_count': len(clean_end_to_end_times),
            'excluded_outlier_count': sum(excluded_reasons.values()),
            'queue_wait_p50_seconds': _median(clean_queue_waits),
            'queue_wait_p90_seconds': _p90(clean_queue_waits),
            'execution_p50_seconds': _median(clean_execution_times),
            'execution_p90_seconds': _p90(clean_execution_times),
            'end_to_end_p50_seconds': _median(clean_end_to_end_times),
            'end_to_end_p90_seconds': _p90(clean_end_to_end_times),
        }
        return {
            'recent_completed_count': len(end_to_end_times),
            'oldest_pending_age_seconds': round(oldest_pending_age_seconds, 3),
            'avg_queue_wait_seconds': _avg(queue_waits),
            'avg_execution_seconds': _avg(execution_times),
            'avg_end_to_end_seconds': _avg(end_to_end_times),
            'fresh_clean_metrics': clean_metrics,
            'outlier_metrics': {
                'excluded_count': sum(excluded_reasons.values()),
                'reasons': excluded_reasons,
                'long_tail_threshold_seconds': clean_long_tail_threshold_seconds,
            },
            'per_guild': per_guild,
        }

    def _recent_runtime_traces(self, *, bind_limit: int = 10, crm_limit: int = 10) -> Dict[str, Any]:
        with self.db.connect() as conn:
            bind_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, t.status, t.result_code, t.result_reason, t.created_at, t.started_at, t.finished_at,
                       COALESCE(l.dept_name, '') AS guild_name, COALESCE(l.mobile, '') AS mobile, COALESCE(l.yw_id, '') AS account_id,
                       COALESCE(l.pendaftaran_group, '') AS registration_group, COALESCE(l.current_status, '') AS lead_current_status
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check'
                ORDER BY COALESCE(t.finished_at, t.started_at, t.created_at) DESC
                LIMIT ?
                """,
                (max(1, min(int(bind_limit or 10), 50)),),
            ).fetchall()]
            crm_rows = [dict(r) for r in conn.execute(
                """
                SELECT sl.sync_log_id, sl.lead_id, sl.task_id, sl.status, sl.sync_type, sl.target_system, sl.created_at,
                       COALESCE(l.dept_name, '') AS guild_name, COALESCE(l.mobile, '') AS mobile, COALESCE(l.yw_id, '') AS account_id,
                       COALESCE(l.pendaftaran_group, '') AS registration_group,
                       COALESCE(l.current_status, '') AS lead_current_status,
                       sl.request_snapshot, sl.response_snapshot
                FROM sync_logs sl
                LEFT JOIN leads l ON l.lead_id = sl.lead_id
                WHERE sl.target_system = 'crm'
                ORDER BY sl.created_at DESC
                LIMIT ?
                """,
                (max(1, min(int(crm_limit or 10), 50)),),
            ).fetchall()]
        recent_bind_traces = []
        for row in bind_rows:
            if str(row.get('lead_current_status') or '').strip().lower() in IGNORED_HISTORY_LEAD_STATUSES:
                continue
            queue_wait = None
            execution = None
            end_to_end = None
            try:
                created_at = parse_iso_datetime(str(row.get('created_at') or '')) if row.get('created_at') else None
                started_at = parse_iso_datetime(str(row.get('started_at') or '')) if row.get('started_at') else None
                finished_at = parse_iso_datetime(str(row.get('finished_at') or '')) if row.get('finished_at') else None
                if created_at and started_at:
                    queue_wait = round(max(0.0, (started_at - created_at).total_seconds()), 3)
                if started_at and finished_at:
                    execution = round(max(0.0, (finished_at - started_at).total_seconds()), 3)
                if created_at and finished_at:
                    end_to_end = round(max(0.0, (finished_at - created_at).total_seconds()), 3)
            except Exception:
                pass
            recent_bind_traces.append({
                'task_id': row.get('task_id'),
                'lead_id': row.get('lead_id'),
                'guild_name': row.get('guild_name') or '',
                'mobile': row.get('mobile') or '',
                'account_id': row.get('account_id') or '',
                'registration_group': row.get('registration_group') or '',
                'status': row.get('status'),
                'result_code': row.get('result_code'),
                'result_reason': row.get('result_reason'),
                'created_at': row.get('created_at'),
                'started_at': row.get('started_at'),
                'finished_at': row.get('finished_at'),
                'queue_wait_seconds': queue_wait,
                'execution_seconds': execution,
                'end_to_end_seconds': end_to_end,
            })
        recent_crm_traces = []
        for row in crm_rows:
            if str(row.get('lead_current_status') or '').strip().lower() in IGNORED_HISTORY_LEAD_STATUSES:
                continue
            request_snapshot = json.loads(row.get('request_snapshot') or '{}') if row.get('request_snapshot') else {}
            response_snapshot = json.loads(row.get('response_snapshot') or '{}') if row.get('response_snapshot') else {}
            recent_crm_traces.append({
                'sync_log_id': row.get('sync_log_id'),
                'lead_id': row.get('lead_id'),
                'task_id': row.get('task_id'),
                'status': row.get('status'),
                'sync_type': row.get('sync_type'),
                'target_system': row.get('target_system'),
                'created_at': row.get('created_at'),
                'guild_name': row.get('guild_name') or '',
                'mobile': row.get('mobile') or '',
                'account_id': row.get('account_id') or '',
                'registration_group': row.get('registration_group') or '',
                'request_app_name': request_snapshot.get('appName') if isinstance(request_snapshot, dict) else None,
                'request_dept_name': request_snapshot.get('deptName') if isinstance(request_snapshot, dict) else None,
                'request_group': request_snapshot.get('pendaftaranGroup') if isinstance(request_snapshot, dict) else None,
                'verified_after_write': bool((response_snapshot.get('verified_after_write') if isinstance(response_snapshot, dict) else False)),
                'action': response_snapshot.get('action') if isinstance(response_snapshot, dict) else None,
                'crm_response_code': ((response_snapshot.get('crm_response') or {}).get('code') if isinstance(response_snapshot, dict) and isinstance(response_snapshot.get('crm_response'), dict) else None),
                'crm_write_elapsed_seconds': response_snapshot.get('crm_write_elapsed_seconds') if isinstance(response_snapshot, dict) else None,
                'crm_verify_elapsed_seconds': response_snapshot.get('crm_verify_elapsed_seconds') if isinstance(response_snapshot, dict) else None,
                'crm_total_elapsed_seconds': response_snapshot.get('crm_total_elapsed_seconds') if isinstance(response_snapshot, dict) else None,
            })
        return {
            'recent_bind_traces': recent_bind_traces,
            'recent_crm_traces': recent_crm_traces,
        }

    def _run_crm_failure_compensation_patrol_if_due(self, *, limit: int = 20) -> Dict[str, Any]:
        now = time.monotonic()
        with self._crm_compensation_patrol_lock:
            elapsed = now - self._crm_compensation_patrol_last_monotonic
            if self._crm_compensation_patrol_last_monotonic > 0 and elapsed < self._crm_compensation_patrol_interval_seconds:
                return {
                    'attempted': False,
                    'reason': 'patrol_interval_not_elapsed',
                    'retry_after_seconds': round(self._crm_compensation_patrol_interval_seconds - elapsed, 3),
                }
            self._crm_compensation_patrol_last_monotonic = now
        return self.run_crm_failure_compensation_patrol(limit=limit)

    def process_next_automation_task(self) -> Optional[Dict[str, Any]]:
        try:
            self.reconcile_task_residue(force=False)
        except Exception as exc:
            print(f'Task residue reconcile degraded before worker tick: {exc}')
        try:
            self._run_crm_failure_compensation_patrol_if_due(limit=20)
        except Exception as exc:
            print(f'CRM compensation patrol degraded before worker tick: {exc}')
        row = self._select_next_bind_task()
        if row:
            try:
                payload = self._build_bind_execution_result(task_id=row['task_id'])
                result = self.bind_check_result(row['task_id'], payload)
                if payload.result_code and not result.get('result_code'):
                    result['result_code'] = payload.result_code
                if payload.result_reason and not result.get('result_reason'):
                    result['result_reason'] = payload.result_reason
            except Exception as exc:
                payload = BindCheckResultRequest(
                    status='failed',
                    result_code='bind_execution_error',
                    result_reason=str(exc),
                    finished_at=utc_now(),
                    raw_result={},
                )
                result = self.bind_check_result(row['task_id'], payload)
                if payload.result_code and not result.get('result_code'):
                    result['result_code'] = payload.result_code
                if payload.result_reason and not result.get('result_reason'):
                    result['result_reason'] = payload.result_reason
            executor = None
            with self.db.connect() as conn:
                lead_row = conn.execute("SELECT dept_name FROM leads WHERE lead_id = ?", (row['lead_id'],)).fetchone()
            if lead_row:
                executor = self.get_guild_executor(str(lead_row['dept_name'] or '').strip()) if self.resolve_guild_executor(str(lead_row['dept_name'] or '').strip()) else None
            task_payload = json.loads(row['payload'] or '{}')
            source_bot_app_id = str(task_payload.get('source_bot_app_id') or '').strip()
            message_id = str(task_payload.get('source_message_id') or '').strip()
            chat_id = str(task_payload.get('source_chat_id') or '').strip()
            source_channel = str(task_payload.get('source_channel') or '').strip()
            is_ops_intake_synthetic_message = (
                source_channel == 'ops_intake_workbench'
                or message_id.startswith('ops_msg_')
                or chat_id == 'ops_intake_submit'
            )
            if message_id or chat_id:
                reply_adapter = self._resolve_lark_reply_adapter(app_id=source_bot_app_id or None)
                with self.db.connect() as conn:
                    lead_row = conn.execute("SELECT mobile, area_code, pendaftaran_group, inviter_id FROM leads WHERE lead_id = ?", (row['lead_id'],)).fetchone()
                reply_envelope = {
                    'accepted': bool(result.get('lead_status') == 'bind_success' and result.get('reason') != 'crm_sync_failed'),
                    'reason': result.get('reason'),
                    'result_code': result.get('result_code'),
                    'result_reason': result.get('result_reason'),
                    'bind_precheck': result.get('bind_precheck'),
                    'bind_failure_category': result.get('bind_failure_category'),
                    'lead_status': result.get('lead_status'),
                    'next_action': result.get('next_action'),
                    'crm_verified': result.get('crm_verified'),
                    'current_submission_crm_verified': result.get('current_submission_crm_verified'),
                    'requires_human_action': result.get('requires_human_action'),
                    'human_action_type': result.get('human_action_type'),
                    'reply_phone': str((lead_row['mobile'] if lead_row else '') or '-'),
                    'reply_area_code': int((lead_row['area_code'] if lead_row and lead_row['area_code'] is not None else 0) or 0),
                    'reply_id': str(task_payload.get('account_id') or '-'),
                    'reply_group': str((lead_row['pendaftaran_group'] if lead_row else '') or '-'),
                    'reply_code': str((lead_row['inviter_id'] if lead_row else '') or '-'),
                }
                emit_lark_reply = self._should_emit_lark_reply(reply_envelope)
                if is_ops_intake_synthetic_message or emit_lark_reply:
                    reply_text = self._format_lark_reply_text(reply_envelope)
                    result['reply_text'] = reply_text
                    updated_items: List[Dict[str, Any]] = []
                    with self.db.connect() as conn:
                        updated_items = self._update_ops_intake_items_after_bind_result(
                            conn,
                            task_id=str(row['task_id'] or ''),
                            lead_id=str(row['lead_id'] or ''),
                            submission_id=str(task_payload.get('submission_id') or ''),
                            result=result,
                            reply_envelope=reply_envelope,
                            reply_text=reply_text,
                        )
                        conn.commit()
                    try:
                        for updated_item in updated_items:
                            try:
                                snapshot = json.loads(updated_item.get('result_snapshot') or '{}')
                            except Exception:
                                snapshot = {}
                            self._upsert_binding_current_truth_snapshot(updated_item, snapshot)
                    except Exception:
                        pass
                    if (not is_ops_intake_synthetic_message) and emit_lark_reply:
                        self._reply_lark_message(message_id=message_id, chat_id=chat_id, text=reply_text, adapter=reply_adapter)
            if executor is not None:
                result['executor'] = executor
            result['task_type'] = 'bind_check'
            return result

        retry_row = self._select_next_crm_retry_task()
        if not retry_row:
            return None
        result = self._process_crm_retry_task(retry_row)
        result['task_type'] = 'crm_sync_retry'
        return result

    def list_ingress_queue(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """
                SELECT j.job_id, j.event_id, j.status, j.attempt_count, j.available_at,
                       COALESCE(j.worker_id, '') AS worker_id,
                       COALESCE(j.lease_until, '') AS lease_until,
                       COALESCE(j.heartbeat_at, '') AS heartbeat_at,
                       j.last_error, e.ingress_type, e.source_key, e.created_at, e.updated_at
                FROM ingress_jobs j
                JOIN ingress_events e ON e.event_id = j.event_id
                ORDER BY e.created_at DESC
                LIMIT 200
                """
            ).fetchall()]
            return {'rows': rows}

    def operator_audit_log(self, *, limit: int = 200) -> Dict[str, Any]:
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT audit_id, lead_id, ingress_event_id, event_type, event_source, payload, created_at FROM operator_audit_log ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit or 200), 1000)),),
            ).fetchall()]
        for row in rows:
            try:
                parsed_payload = json.loads(row.get('payload') or '{}')
            except Exception:
                parsed_payload = {'raw_payload': row.get('payload') or ''}
            row['payload'] = json.dumps(_redact_sensitive_payload(parsed_payload), ensure_ascii=False)
        return {'rows': rows}

    def _pending_bind_human_actions(self, *, limit: int = 20) -> list[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, t.status, t.result_code, t.result_reason, t.created_at, t.started_at, t.finished_at, t.raw_result,
                       COALESCE(l.dept_name, '') AS guild_name,
                       COALESCE(l.mobile, '') AS mobile,
                       COALESCE(l.yw_id, '') AS account_id,
                       COALESCE(l.pendaftaran_group, '') AS registration_group,
                       COALESCE(l.current_status, '') AS lead_current_status
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'failed'
                ORDER BY COALESCE(t.finished_at, t.created_at) DESC
                LIMIT ?
                """,
                (max(1, min(int(limit or 20), 100)),),
            ).fetchall()]
        pending = []
        for row in rows:
            if str(row.get('lead_current_status') or '').strip().lower() in IGNORED_HISTORY_LEAD_STATUSES:
                continue
            try:
                raw_result = json.loads(row.get('raw_result') or '{}')
            except Exception:
                raw_result = {}
            human = self._classify_bind_human_action(
                result_code=row.get('result_code'),
                result_reason=row.get('result_reason'),
                raw_result=raw_result,
            )
            if not human.get('requires_human_action'):
                continue
            pending.append({
                'task_id': row.get('task_id'),
                'lead_id': row.get('lead_id'),
                'guild_name': row.get('guild_name') or '',
                'mobile': row.get('mobile') or '',
                'account_id': row.get('account_id') or '',
                'registration_group': row.get('registration_group') or '',
                'status': row.get('status'),
                'result_code': row.get('result_code'),
                'result_reason': row.get('result_reason'),
                'human_action_type': human.get('human_action_type'),
                'created_at': row.get('created_at'),
                'started_at': row.get('started_at'),
                'finished_at': row.get('finished_at'),
            })
        return pending

    def runtime_health(self) -> Dict[str, Any]:
        crm_adapter_health = self.crm_adapter.health_snapshot() if self.crm_adapter is not None and hasattr(self.crm_adapter, 'health_snapshot') else {}
        bind_metrics = self._calculate_bind_metrics()
        runtime_traces = self._recent_runtime_traces()
        pending_bind_human_actions = self._pending_bind_human_actions()
        with self.db.connect() as conn:
            ingress_queued = conn.execute("SELECT COUNT(*) FROM ingress_jobs WHERE status = 'queued'").fetchone()[0]
            ingress_processing = conn.execute("SELECT COUNT(*) FROM ingress_jobs WHERE status = 'processing'").fetchone()[0]
            now = utc_now()
            stale_before = (parse_iso_datetime(now) - timedelta(seconds=self._ingress_job_lease_seconds)).isoformat()
            ingress_stale_processing = conn.execute(
                """
                SELECT COUNT(*) FROM ingress_jobs
                WHERE status = 'processing'
                  AND (
                    (COALESCE(lease_until, '') <> '' AND lease_until <= ?)
                    OR (COALESCE(lease_until, '') = '' AND updated_at <= ?)
                  )
                """,
                (now, stale_before),
            ).fetchone()[0]
            pending_bind_tasks = conn.execute("SELECT COUNT(*) FROM automation_tasks WHERE task_type = 'bind_check' AND status = 'pending'").fetchone()[0]
            processing_bind_tasks = conn.execute("SELECT COUNT(*) FROM automation_tasks WHERE task_type = 'bind_check' AND status = 'processing'").fetchone()[0]
        registration_group_approval_health = self.registration_group_approval_executor_health()
        official_group_approval_health = self.official_group_approval_executor_health()
        bind_executor_configured = callable(self.bind_simulator) or callable(self.real_bind_executor)
        bind_executor_mode = (
            'simulated'
            if callable(self.bind_simulator)
            else ('live' if callable(self.real_bind_executor) else 'unavailable')
        )
        return {
            'crm': {
                'enabled': self.crm_adapter is not None,
                'base_url': self.crm_base_url or getattr(self.crm_adapter, 'base_url', None),
                'username': self.crm_username or getattr(self.crm_adapter, 'username', None),
                'login_error': self.crm_login_error or crm_adapter_health.get('login_error'),
                'status': crm_adapter_health.get('status') or ('degraded' if (self.crm_login_error and self.crm_adapter is not None) else ('healthy' if self.crm_adapter is not None else 'disabled')),
                'token_ready': crm_adapter_health.get('token_ready'),
                'last_login_attempt_at': crm_adapter_health.get('last_login_attempt_at'),
                'last_login_ok_at': crm_adapter_health.get('last_login_ok_at'),
                'login_retry_cooldown_seconds': crm_adapter_health.get('login_retry_cooldown_seconds'),
            },
            'lark': {
                'default_app': self.lark_default_app_name,
                'default_guild': self.lark_default_dept_name,
                'current_app_id': self.current_lark_app_id,
            },
            'simulation': {
                'auto_bind_simulation': self.auto_bind_simulation,
                'success_rate': self.auto_bind_simulation_success_rate,
                'mode': 'simulated' if self.auto_bind_simulation else 'live',
            },
            'bind_executor': {
                'configured': bind_executor_configured,
                'status': 'healthy' if bind_executor_configured else 'unavailable',
                'mode': bind_executor_mode,
                'executor_type': type(self.real_bind_executor).__name__ if callable(self.real_bind_executor) else None,
                'simulator_configured': callable(self.bind_simulator),
            },
            'registration_group_approval': registration_group_approval_health,
            'official_group_approval': official_group_approval_health,
            'baileys_qr_recovery': {
                **dict(getattr(self, '_baileys_qr_recovery_state', {}) or {}),
                'worker_alive': bool(
                    getattr(self, '_baileys_qr_recovery_thread', None)
                    and self._baileys_qr_recovery_thread.is_alive()
                ),
                'poll_interval_seconds': getattr(self, '_baileys_qr_recovery_poll_interval_seconds', None),
            },
            'ingress': {
                'async_default': self.ingress_async_default,
                'worker_enabled': self.ingress_worker_enabled,
                'worker_count': self.ingress_worker_count,
                'worker_alive': any(thread.is_alive() for thread in self._worker_threads),
                'active_worker_threads': sum(1 for thread in self._worker_threads if thread.is_alive()),
                'queued_jobs': ingress_queued,
                'processing_jobs': ingress_processing,
                'stale_processing_jobs': ingress_stale_processing,
                'job_lease_seconds': self._ingress_job_lease_seconds,
                'job_max_attempts': self._ingress_job_max_attempts,
                'pending_bind_tasks': pending_bind_tasks,
                'processing_bind_tasks': processing_bind_tasks,
                'require_invite_code': self.require_invite_code,
                'bind_metrics': bind_metrics,
                'pending_bind_human_actions': pending_bind_human_actions,
                'pending_bind_human_action_count': len(pending_bind_human_actions),
                'recent_bind_traces': runtime_traces['recent_bind_traces'],
                'recent_crm_traces': runtime_traces['recent_crm_traces'],
            },
        }

    def _classify_manual_cs_submission(
        self,
        *,
        payload: ManualCsSubmissionRequest,
        parsed_payload: Dict[str, Any],
        final_account_id: Optional[str],
        final_mobile: str,
        final_registration_group: Optional[str],
        final_app_name: Optional[str],
        final_dept_name: Optional[str],
        final_invite_code: Optional[str],
        invite_code_required: bool = True,
    ) -> Dict[str, Any]:
        review_reason_codes = list(parsed_payload.get('conflicts', []) or [])
        confidence = float(parsed_payload.get('confidence') or 0.0)
        critical_missing = [
            name for name, value in {
                'mobile': final_mobile,
                'app_name': final_app_name,
                'dept_name': final_dept_name,
            }.items() if not value
        ]
        if critical_missing:
            review_reason_codes.extend(f'missing_{name}' for name in critical_missing)
        has_required_identity = bool(final_mobile and final_account_id and final_registration_group and final_app_name and final_dept_name)
        has_required_code = bool(final_invite_code) or not bool(invite_code_required)
        if has_required_identity and has_required_code:
            confidence = max(confidence, 0.75)
        if confidence < 0.75:
            review_reason_codes.append('low_confidence')
        review_reason_codes = list(dict.fromkeys(review_reason_codes))

        if 'account_id_conflict' in review_reason_codes:
            return {
                'parser_version': 'manual_cs_parser_v2',
                'parser_status': 'conflict',
                'routing_decision': 'manual_review',
                'recommended_next_action': 'review_account_conflict',
                'review_reason_codes': review_reason_codes,
                'review_status': 'pending',
            }
        if critical_missing:
            return {
                'parser_version': 'manual_cs_parser_v2',
                'parser_status': 'missing_fields',
                'routing_decision': 'manual_review',
                'recommended_next_action': 'fill_missing_fields',
                'review_reason_codes': review_reason_codes,
                'review_status': 'pending',
            }
        if payload.submission_type == 'screenshot' and not final_account_id:
            return {
                'parser_version': 'manual_cs_parser_v2',
                'parser_status': 'needs_recognition',
                'routing_decision': 'queue_account_recognition',
                'recommended_next_action': 'queue_account_recognition',
                'review_reason_codes': review_reason_codes,
                'review_status': 'not_needed',
            }
        if confidence < 0.75:
            return {
                'parser_version': 'manual_cs_parser_v2',
                'parser_status': 'low_confidence',
                'routing_decision': 'manual_review',
                'recommended_next_action': 'review_low_confidence',
                'review_reason_codes': review_reason_codes,
                'review_status': 'pending',
            }
        return {
            'parser_version': 'manual_cs_parser_v2',
            'parser_status': 'ready',
            'routing_decision': 'queue_bind_check' if final_account_id else 'queue_account_recognition',
            'recommended_next_action': 'queue_bind_check' if final_account_id else 'queue_account_recognition',
            'review_reason_codes': review_reason_codes,
            'review_status': 'not_needed',
        }

    def _record_status_history(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        from_status: Optional[str],
        to_status: str,
        trigger_type: str,
        trigger_source: str,
        trigger_event_id: Optional[str] = None,
        trigger_task_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        operator_name: Optional[str] = None,
        remark: Optional[str] = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO lead_status_history (
                history_id, lead_id, from_status, to_status, trigger_type, trigger_source,
                trigger_event_id, trigger_task_id, operator_id, operator_name, remark, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                create_id("hist"),
                lead_id,
                from_status,
                to_status,
                trigger_type,
                trigger_source,
                trigger_event_id,
                trigger_task_id,
                operator_id,
                operator_name,
                remark,
                utc_now(),
            ),
        )

    def _record_sync_log(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: Optional[str],
        task_id: Optional[str],
        sync_type: str,
        target_system: str,
        status: str,
        request_snapshot: Any,
        response_snapshot: Any,
    ) -> None:
        conn.execute(
            "INSERT INTO sync_logs (sync_log_id, lead_id, task_id, sync_type, target_system, status, request_snapshot, response_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                create_id("sync"),
                lead_id,
                task_id,
                sync_type,
                target_system,
                status,
                json.dumps(request_snapshot, ensure_ascii=False),
                json.dumps(response_snapshot, ensure_ascii=False),
                utc_now(),
            ),
        )

    def _load_persisted_crm_option_cache(self) -> None:
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    "SELECT option_type, display_name, row_json FROM crm_option_cache"
                ).fetchall()
        except Exception:
            return
        for row in rows:
            option_type = str(row['option_type'] or '').strip()
            display_name = str(row['display_name'] or '').strip().lower()
            if not option_type or not display_name:
                continue
            try:
                payload = json.loads(row['row_json'] or '{}')
            except Exception:
                payload = {}
            if isinstance(payload, dict) and payload:
                self._crm_option_cache.setdefault(option_type, {})[display_name] = payload

    def _persist_crm_option_row(self, *, option_type: str, display_name: str, row: Dict[str, Any]) -> None:
        normalized_name = str(display_name or '').strip().lower()
        if not option_type or not normalized_name or not isinstance(row, dict) or not row:
            return
        try:
            with self.db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO crm_option_cache (option_type, display_name, row_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(option_type, display_name)
                    DO UPDATE SET row_json = excluded.row_json, updated_at = excluded.updated_at
                    """,
                    (
                        option_type,
                        normalized_name,
                        json.dumps(row, ensure_ascii=False),
                        utc_now(),
                    ),
                )
                conn.commit()
        except Exception:
            return

    def _resolve_lead_notification_context(self, conn: sqlite3.Connection, lead_id: str) -> tuple[str, Optional[str]]:
        lead = conn.execute(
            'SELECT mobile, yw_id FROM leads WHERE lead_id = ?',
            (lead_id,),
        ).fetchone()
        mobile = str((lead['mobile'] if lead else '') or '')
        yw_id = str((lead['yw_id'] if lead else '') or '').strip() or None
        return mobile, yw_id

    def _auto_resolve_prior_failed_notifications(self, conn: sqlite3.Connection, *, lead_id: str) -> None:
        conn.execute(
            """
            UPDATE operator_notifications
            SET is_read = 1, read_at = ?, read_by = ?
            WHERE lead_id = ?
              AND notification_type = 'crm_record_failed'
              AND is_read = 0
            """,
            (utc_now(), 'system:auto_resolved', lead_id),
        )

    def _notification_recent_duplicate_exists(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        notification_type: str,
        write_result: str,
        reason: Optional[str],
        window_seconds: int = 900,
    ) -> bool:
        row = conn.execute(
            """
            SELECT created_at FROM operator_notifications
            WHERE lead_id = ?
              AND notification_type = ?
              AND write_result = ?
              AND COALESCE(reason, '') = COALESCE(?, '')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (lead_id, notification_type, write_result, reason),
        ).fetchone()
        if not row:
            return False
        try:
            return abs((parse_iso_datetime(utc_now()) - parse_iso_datetime(str(row['created_at'] or ''))).total_seconds()) <= window_seconds
        except Exception:
            return False

    def _queue_operator_notification(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        notification_type: str,
        mobile: str,
        yw_id: Optional[str],
        write_result: str,
        reason: Optional[str] = None,
    ) -> None:
        if notification_type == 'crm_record_success' or write_result == 'success':
            self._auto_resolve_prior_failed_notifications(conn, lead_id=lead_id)
        elif self._notification_recent_duplicate_exists(
            conn,
            lead_id=lead_id,
            notification_type=notification_type,
            write_result=write_result,
            reason=reason,
        ):
            return
        conn.execute(
            """
            INSERT INTO operator_notifications (
                notification_id, lead_id, notification_type, mobile, yw_id, write_result, reason, is_read, read_at, read_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                create_id("notify"),
                lead_id,
                notification_type,
                mobile,
                yw_id,
                write_result,
                reason,
                0,
                None,
                None,
                utc_now(),
            ),
        )

    def _record_verified_crm_state(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        crm_payload: Dict[str, Any],
        official_group: Optional[str] = None,
    ) -> None:
        conn.execute(
            """
            UPDATE leads
            SET crm_verified_payload = ?,
                crm_verified_app_name = ?,
                crm_verified_dept_name = ?,
                crm_verified_registration_group = ?,
                crm_verified_official_group = COALESCE(?, crm_verified_official_group),
                crm_verified_at = ?,
                updated_at = ?
            WHERE lead_id = ?
            """,
            (
                json.dumps(crm_payload, ensure_ascii=False),
                str(crm_payload.get('appName') or '').strip() or None,
                str(crm_payload.get('deptName') or '').strip() or None,
                str(crm_payload.get('pendaftaranGroup') or '').strip() or None,
                str(official_group or '').strip() or None,
                utc_now(),
                utc_now(),
                lead_id,
            ),
        )

    @staticmethod
    def _sync_log_crm_success_semantics(response_snapshot: Dict[str, Any]) -> Optional[str]:
        if not isinstance(response_snapshot, dict):
            return None
        verified_after_write = response_snapshot.get('verified_after_write')
        if verified_after_write is True:
            return 'verified_success'
        if verified_after_write not in (None, False):
            return 'verified_success' if bool(verified_after_write) else None
        crm_response = response_snapshot.get('crm_response') or {}
        crm_code = crm_response.get('code') if isinstance(crm_response, dict) else None
        if crm_code != 0:
            return None
        action = str(response_snapshot.get('action') or '').strip().lower()
        if action == 'verify_before_retry':
            return 'verified_success'
        if action == 'create':
            return 'legacy_success_unverified'
        return None

    @staticmethod
    def _sync_log_indicates_verified_crm_success(response_snapshot: Dict[str, Any]) -> bool:
        return Service._sync_log_crm_success_semantics(response_snapshot) == 'verified_success'

    def _restore_verified_crm_state_from_sync_logs(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
    ) -> bool:
        rows = conn.execute(
            """
            SELECT request_snapshot, response_snapshot
            FROM sync_logs
            WHERE lead_id = ?
              AND sync_type = 'customer_upsert'
              AND status = 'success'
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (lead_id,),
        ).fetchall()
        for sync_row in rows:
            try:
                response_snapshot = json.loads(sync_row['response_snapshot'] or '{}')
            except Exception:
                response_snapshot = {}
            if not self._sync_log_crm_success_semantics(response_snapshot):
                continue
            try:
                request_snapshot = json.loads(sync_row['request_snapshot'] or '{}')
            except Exception:
                request_snapshot = {}
            if not isinstance(request_snapshot, dict):
                continue
            app_name = str(request_snapshot.get('appName') or '').strip()
            registration_group = str(request_snapshot.get('pendaftaranGroup') or '').strip()
            if not app_name or not registration_group:
                continue
            self._record_verified_crm_state(
                conn,
                lead_id=lead_id,
                crm_payload=request_snapshot,
                official_group=str(request_snapshot.get('wa') or '').strip() or None,
            )
            return True
        return False

    def _find_recent_verified_duplicate_lead(
        self,
        conn: sqlite3.Connection,
        *,
        mobile: str,
        area_code: int,
        account_id: Optional[str],
        app_name: Optional[str],
        dept_name: Optional[str],
        registration_group: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        normalized_mobile = str(mobile or '').strip()
        normalized_account_id = str(account_id or '').strip()
        normalized_app_name = str(app_name or '').strip().lower()
        normalized_dept_name = str(dept_name or '').strip().lower()
        normalized_registration_group = str(registration_group or '').strip().lower()
        if not normalized_mobile or not normalized_account_id or not normalized_app_name:
            return None
        row = conn.execute(
            """
            SELECT lead_id, mobile, yw_id, app_name, dept_name, pendaftaran_group,
                   crm_verified_app_name, crm_verified_dept_name, crm_verified_registration_group
            FROM leads
            WHERE area_code = ? AND mobile = ? AND COALESCE(yw_id, '') = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (area_code, normalized_mobile, normalized_account_id),
        ).fetchone()
        if not row:
            return None
        restored_from_blank_verified_columns = False
        if not row['crm_verified_app_name'] and not row['crm_verified_registration_group']:
            if self._restore_verified_crm_state_from_sync_logs(conn, lead_id=str(row['lead_id'] or '').strip()):
                restored_from_blank_verified_columns = True
                row = conn.execute(
                    """
                    SELECT lead_id, mobile, yw_id, app_name, dept_name, pendaftaran_group,
                           crm_verified_app_name, crm_verified_dept_name, crm_verified_registration_group
                    FROM leads
                    WHERE lead_id = ?
                    LIMIT 1
                    """,
                    (row['lead_id'],),
                ).fetchone() or row
        effective_app_name = str(row['crm_verified_app_name'] or row['app_name'] or '').strip().lower()
        effective_dept_name = str(row['crm_verified_dept_name'] or row['dept_name'] or '').strip().lower()
        effective_group = str(row['crm_verified_registration_group'] or row['pendaftaran_group'] or '').strip().lower()
        if effective_app_name != normalized_app_name:
            return None
        if normalized_dept_name and effective_dept_name and effective_dept_name != normalized_dept_name:
            return None
        if normalized_registration_group and effective_group and effective_group != normalized_registration_group:
            return None
        sync_rows = conn.execute(
            "SELECT status, response_snapshot FROM sync_logs WHERE lead_id = ? ORDER BY created_at DESC LIMIT 50",
            (row['lead_id'],),
        ).fetchall()
        for latest_sync in sync_rows:
            if str(latest_sync['status'] or '').strip().lower() != 'success':
                continue
            try:
                snapshot = json.loads(latest_sync['response_snapshot'] or '{}')
            except Exception:
                snapshot = {}
            semantics = self._sync_log_crm_success_semantics(snapshot)
            if semantics == 'verified_success':
                payload = dict(row)
                payload['duplicate_semantics'] = 'already_in_target_guild'
                return payload
            if semantics == 'legacy_success_unverified' and restored_from_blank_verified_columns:
                payload = dict(row)
                payload['duplicate_semantics'] = 'legacy_success_unverified'
                return payload
        return None

    def _find_recent_cross_channel_duplicate_submission(
        self,
        conn: sqlite3.Connection,
        *,
        mobile: str,
        area_code: int,
        account_id: Optional[str],
        app_name: Optional[str],
        dept_name: Optional[str],
        registration_group: Optional[str],
        source_channel: str,
        submitted_at: str,
        window_seconds: int = 120,
    ) -> Optional[Dict[str, Any]]:
        normalized_mobile = str(mobile or '').strip()
        normalized_account_id = str(account_id or '').strip()
        normalized_app_name = str(app_name or '').strip().lower()
        normalized_dept_name = str(dept_name or '').strip().lower()
        normalized_registration_group = str(registration_group or '').strip().lower()
        if not normalized_mobile or not normalized_account_id:
            return None
        submitted_dt = parse_iso_datetime(submitted_at)
        rows = conn.execute(
            """
            SELECT s.submission_id, s.lead_id, s.source_channel, s.submitted_by, s.submitted_at, s.created_at,
                   l.area_code, l.mobile, l.yw_id, l.app_name, l.dept_name, l.pendaftaran_group
            FROM account_submissions s
            JOIN leads l ON l.lead_id = s.lead_id
            WHERE l.area_code = ? AND l.mobile = ? AND COALESCE(l.yw_id, '') = ?
            ORDER BY s.created_at DESC
            LIMIT 10
            """,
            (area_code, normalized_mobile, normalized_account_id),
        ).fetchall()
        for row in rows:
            existing_source = str(row['source_channel'] or '').strip()
            if not existing_source or existing_source == str(source_channel or '').strip():
                continue
            existing_app_name = str(row['app_name'] or '').strip().lower()
            existing_dept_name = str(row['dept_name'] or '').strip().lower()
            existing_registration_group = str(row['pendaftaran_group'] or '').strip().lower()
            if (
                existing_app_name != normalized_app_name
                or existing_dept_name != normalized_dept_name
                or existing_registration_group != normalized_registration_group
            ):
                continue
            try:
                existing_dt = parse_iso_datetime(str(row['submitted_at'] or row['created_at'] or ''))
            except Exception:
                existing_dt = parse_iso_datetime(str(row['created_at'] or submitted_at))
            if abs((submitted_dt - existing_dt).total_seconds()) <= window_seconds:
                return dict(row)
        return None

    def _build_duplicate_submission_response(
        self,
        conn: sqlite3.Connection,
        *,
        duplicate_submission: Dict[str, Any],
        parsed_result: Dict[str, Any],
        accepted_override: Optional[bool] = None,
        reason_override: Optional[str] = None,
        result_code_override: Optional[str] = None,
        result_reason_override: Optional[str] = None,
        bind_precheck_override: Optional[str] = None,
        next_action_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        lead_row = conn.execute("SELECT lead_id, matched_customer_id, current_status FROM leads WHERE lead_id = ?", (duplicate_submission['lead_id'],)).fetchone()
        latest_sync = conn.execute(
            "SELECT status, response_snapshot FROM sync_logs WHERE lead_id = ? ORDER BY created_at DESC LIMIT 1",
            (duplicate_submission['lead_id'],),
        ).fetchone()
        response = {
            'deduped': True,
            'duplicate_submission_id': duplicate_submission.get('submission_id'),
            'duplicate_source_channel': duplicate_submission.get('source_channel'),
            'lead_id': duplicate_submission.get('lead_id'),
            'matched_customer_id': lead_row['matched_customer_id'] if lead_row else None,
            'parsed_payload': parsed_result,
            'reply_phone': f"+{parsed_result.get('area_code')} {parsed_result.get('mobile')}" if parsed_result.get('area_code') and parsed_result.get('mobile') else (parsed_result.get('mobile') or '-'),
            'reply_id': parsed_result.get('account_id') or '-',
            'reply_group': parsed_result.get('registration_group') or '-',
        }
        if latest_sync and str(latest_sync['status'] or '').strip().lower() == 'success':
            verified_after_write = False
            if latest_sync['response_snapshot']:
                try:
                    snapshot = json.loads(latest_sync['response_snapshot'])
                except Exception:
                    snapshot = {}
                verified_after_write = bool(snapshot.get('verified_after_write'))
            accepted_success = True if accepted_override is None else bool(accepted_override)
            if accepted_success:
                response.update({
                    'accepted': True,
                    'next_action': 'queue_group_join',
                    'lead_status': lead_row['current_status'] if lead_row else 'bind_success',
                    'crm_verified': verified_after_write,
                    'current_submission_crm_verified': verified_after_write,
                })
            else:
                response.update({
                    'accepted': False,
                    'reason': reason_override or 'crm_sync_failed',
                    'result_code': result_code_override or '',
                    'result_reason': result_reason_override or 'Data duplication.',
                    'bind_precheck': bind_precheck_override or '',
                    'next_action': next_action_override or 'retry_crm_sync',
                    'lead_status': lead_row['current_status'] if lead_row else 'bind_success',
                })
        else:
            result_reason = result_reason_override or 'Duplicate intake ignored after previous failed attempt.'
            if latest_sync and latest_sync['response_snapshot']:
                try:
                    snapshot = json.loads(latest_sync['response_snapshot'])
                except Exception:
                    snapshot = {}
                mapping_failure = str(snapshot.get('mapping_failure') or '').strip()
                if mapping_failure:
                    result_reason = mapping_failure
                else:
                    crm_response = snapshot.get('crm_response') or {}
                    if crm_response:
                        result_reason = self._normalize_crm_failure_reason(crm_response, fallback_found=False)
            response.update({
                'accepted': False,
                'reason': reason_override or 'crm_sync_failed',
                'result_code': result_code_override or '',
                'result_reason': result_reason,
                'bind_precheck': bind_precheck_override or '',
                'next_action': next_action_override or 'retry_crm_sync',
                'lead_status': lead_row['current_status'] if lead_row else 'bind_failed',
            })
        response['reply_text'] = self._format_lark_reply_text(response)
        return response

    def _build_simulated_bind_result(self, *, lead_id: str, task_id: str, lead: Dict[str, Any], submission_id: str, account_id: Optional[str], source_channel: str) -> Dict[str, Any]:
        context = {
            'lead_id': lead_id,
            'task_id': task_id,
            'submission_id': submission_id,
            'account_id': str(account_id or ''),
            'mobile': str(lead.get('mobile') or ''),
            'app_name': str(lead.get('app_name') or ''),
            'dept_name': str(lead.get('dept_name') or ''),
            'registration_group': str(lead.get('pendaftaran_group') or ''),
            'source_channel': str(source_channel or ''),
        }
        if callable(self.bind_simulator):
            simulated = self.bind_simulator(context)
            if not isinstance(simulated, dict):
                raise RuntimeError('bind simulator must return a dict')
            return simulated

        resolved_dept = self._resolve_crm_dept_mapping(context['dept_name'])
        if self._bind_random.random() < self.auto_bind_simulation_success_rate:
            return {
                'status': 'success',
                'result_code': 'bind_ok_simulated',
                'result_reason': 'simulated bind success',
                'raw_result': {
                    'guild_code': context['dept_name'],
                    'deptName': resolved_dept['deptName'],
                    'deptId': resolved_dept['deptId'],
                    'simulated': True,
                },
            }
        failure_reason = self._bind_random.choice([
            'already joined another guild',
            'device account limit reached',
            'account id not eligible for binding',
        ])
        return {
            'status': 'failed',
            'result_code': 'bind_failed_simulated',
            'result_reason': failure_reason,
            'raw_result': {
                'guild_code': context['dept_name'],
                'simulated': True,
            },
        }

    def _maybe_auto_simulate_bind_after_intake(self, *, lead: Dict[str, Any], payload: ManualCsSubmissionRequest, parsed_result: Dict[str, Any], account_submission: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.auto_bind_simulation:
            return None
        if account_submission.get('next_action') != 'queue_bind_check':
            return None
        task_id = str(account_submission.get('task_id') or '')
        if not task_id:
            return None
        simulated = self._build_simulated_bind_result(
            lead_id=lead['lead_id'],
            task_id=task_id,
            lead=lead,
            submission_id=str(account_submission.get('submission_id') or ''),
            account_id=parsed_result.get('account_id'),
            source_channel=payload.source_channel,
        )
        bind_result = self.bind_check_result(
            task_id,
            BindCheckResultRequest(
                status=str(simulated.get('status') or 'failed'),
                result_code=simulated.get('result_code'),
                result_reason=simulated.get('result_reason'),
                finished_at=payload.submitted_at,
                raw_result=simulated.get('raw_result') or {},
            ),
        )
        accepted = bind_result.get('next_action') == 'queue_group_join'
        response = {
            'accepted': accepted,
            'simulation_applied': True,
            'simulated_bind_status': str(simulated.get('status') or ''),
            'task_id': task_id,
            'submission_id': account_submission.get('submission_id'),
            'lead_id': lead['lead_id'],
            'matched_customer_id': lead.get('matched_customer_id'),
            'next_action': bind_result.get('next_action'),
            'lead_status': bind_result.get('lead_status'),
            'routing_decision': 'queue_bind_check',
            'review_reason_codes': [],
            'parsed_payload': parsed_result,
            'reply_phone': parsed_result.get('mobile') or '-',
            'reply_id': parsed_result.get('account_id') or '-',
            'reply_group': parsed_result.get('registration_group') or '-',
            'result_reason': simulated.get('result_reason') or bind_result.get('result_reason') or '',
            'result_code': simulated.get('result_code') or bind_result.get('result_code') or '',
        }
        if bind_result.get('reason') == 'crm_sync_failed':
            response['reason'] = 'crm_sync_failed'
            response['result_reason'] = bind_result.get('result_reason') or response['result_reason']
        elif bind_result.get('reason') == 'crm_sync_retry_pending':
            response['reason'] = 'crm_sync_retry_pending'
            response['result_reason'] = bind_result.get('result_reason') or response['result_reason']
        elif not accepted:
            bind_reason = str(bind_result.get('reason') or '').strip()
            response['reason'] = bind_reason if bind_reason == 'bind_backend_guild_mismatch' else 'simulated_bind_failed'
            response['result_reason'] = bind_result.get('result_reason') or response['result_reason']
        return response

    def operator_notifications(self, *, status: Optional[str] = None, query: Optional[str] = None) -> Dict[str, Any]:
        sql = """
            SELECT notification_id, lead_id, notification_type, mobile, yw_id, write_result, reason,
                   is_read, read_at, read_by, created_at
            FROM operator_notifications
        """
        conditions = []
        params: list[Any] = []
        if status == 'unread':
            conditions.append('is_read = 0')
        elif status == 'read':
            conditions.append('is_read = 1')
        if query:
            conditions.append('(mobile LIKE ? OR COALESCE(yw_id, \'\') LIKE ?)')
            like = f"%{query}%"
            params.extend([like, like])
        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)
        sql += ' ORDER BY created_at DESC'
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
            for row in rows:
                row['is_read'] = bool(row['is_read'])
                row['message_title'] = 'Lark收口通知'
                message_lines = [
                    f"用户手机: {row.get('mobile') or ''}",
                    f"用户ID: {row.get('yw_id') or ''}",
                    f"写入结果: {row.get('write_result') or ''}",
                ]
                if row.get('reason'):
                    message_lines.append(f"失败原因: {row['reason']}")
                row['message_text'] = "\n".join(message_lines)
            return {"rows": rows}

    def mark_operator_notification_read(self, notification_id: str, *, read_by: Optional[str] = None) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute('SELECT notification_id FROM operator_notifications WHERE notification_id = ?', (notification_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='notification not found')
            conn.execute(
                'UPDATE operator_notifications SET is_read = 1, read_at = ?, read_by = ? WHERE notification_id = ?',
                (utc_now(), read_by, notification_id),
            )
            return {'notification_id': notification_id, 'updated': True}

    def evaluate_approval_batch(self, payload: ApprovalBatchEvaluateRequest) -> Dict[str, Any]:
        rules = {
            'registration_group': {'batch_size': 30, 'timeout_minutes': 30},
            'official_group': {'batch_size': 10, 'timeout_minutes': 30},
        }
        if payload.approval_type not in rules:
            raise HTTPException(status_code=400, detail='unsupported approval_type')
        default_rule = rules[payload.approval_type]
        rule = {
            'batch_size': _coerce_positive_int(payload.batch_size, default_rule['batch_size']),
            'timeout_minutes': _coerce_positive_int(payload.timeout_minutes, default_rule['timeout_minutes']),
        }
        pending_count = max(int(payload.pending_count), 0)
        now = parse_iso_datetime(payload.now)
        cycle_anchor_at = str(payload.cycle_anchor_at or '').strip() or None
        if cycle_anchor_at:
            empty_cycle = self._approval_cycle_window(now=now, timeout_minutes=rule['timeout_minutes'], cycle_anchor_at=cycle_anchor_at)
            cycle_start = empty_cycle['cycle_started_at']
            cycle_end = empty_cycle['cycle_ends_at']
        else:
            cycle_end = self._approval_cycle_next_boundary(now=now, timeout_minutes=rule['timeout_minutes'])
            cycle_start = cycle_end - timedelta(minutes=rule['timeout_minutes'])
        remaining_seconds = max(int((cycle_end - now).total_seconds()), 0)
        remaining_minutes = max((remaining_seconds + 59) // 60, 0)

        if pending_count <= 0:
            return {
                'approval_type': payload.approval_type,
                'approval_scope': payload.approval_type,
                'registration_group': payload.registration_group,
                'target_group_label': payload.registration_group,
                'pending_count': pending_count,
                'oldest_pending_at': payload.oldest_pending_at,
                'ready': False,
                'release_count': 0,
                'reason_code': 'waiting_next_cycle',
                'batch_size': rule['batch_size'],
                'timeout_minutes': rule['timeout_minutes'],
                'elapsed_minutes': max(0, int((now - cycle_start).total_seconds() // 60)),
                'remaining_minutes': remaining_minutes,
                'remaining_seconds': remaining_seconds,
                'cycle_started_at': cycle_start.isoformat(),
                'cycle_ends_at': cycle_end.isoformat(),
            }

        if cycle_anchor_at:
            pending_cycle = self._approval_cycle_anchor_deadline(now=now, timeout_minutes=rule['timeout_minutes'], cycle_anchor_at=cycle_anchor_at)
            active_cycle_start = pending_cycle['cycle_started_at']
            next_boundary_after_oldest = pending_cycle['cycle_ends_at']
        else:
            oldest = parse_iso_datetime(payload.oldest_pending_at)
            next_boundary_after_oldest = self._approval_cycle_next_boundary(now=oldest, timeout_minutes=rule['timeout_minutes'])
            active_cycle_start = next_boundary_after_oldest - timedelta(minutes=rule['timeout_minutes'])
        elapsed_minutes = max(0, int((now - active_cycle_start).total_seconds() // 60))
        if pending_count >= rule['batch_size']:
            return {
                'approval_type': payload.approval_type,
                'approval_scope': payload.approval_type,
                'registration_group': payload.registration_group,
                'target_group_label': payload.registration_group,
                'pending_count': pending_count,
                'oldest_pending_at': payload.oldest_pending_at,
                'ready': True,
                'release_count': pending_count,
                'reason_code': 'batch_size_reached',
                'batch_size': rule['batch_size'],
                'timeout_minutes': rule['timeout_minutes'],
                'elapsed_minutes': elapsed_minutes,
                'remaining_minutes': 0,
                'remaining_seconds': 0,
                'cycle_started_at': active_cycle_start.isoformat(),
                'cycle_ends_at': next_boundary_after_oldest.isoformat(),
            }
        if now >= next_boundary_after_oldest:
            return {
                'approval_type': payload.approval_type,
                'approval_scope': payload.approval_type,
                'registration_group': payload.registration_group,
                'target_group_label': payload.registration_group,
                'pending_count': pending_count,
                'oldest_pending_at': payload.oldest_pending_at,
                'ready': True,
                'release_count': pending_count,
                'reason_code': 'timeout_flush',
                'batch_size': rule['batch_size'],
                'timeout_minutes': rule['timeout_minutes'],
                'elapsed_minutes': elapsed_minutes,
                'remaining_minutes': 0,
                'remaining_seconds': 0,
                'cycle_started_at': active_cycle_start.isoformat(),
                'cycle_ends_at': next_boundary_after_oldest.isoformat(),
            }
        remaining_seconds = max(int((next_boundary_after_oldest - now).total_seconds()), 0)
        remaining_minutes = max((remaining_seconds + 59) // 60, 0)
        return {
            'approval_type': payload.approval_type,
            'approval_scope': payload.approval_type,
            'registration_group': payload.registration_group,
            'target_group_label': payload.registration_group,
            'pending_count': pending_count,
            'oldest_pending_at': payload.oldest_pending_at,
            'ready': False,
            'release_count': 0,
            'reason_code': 'waiting_for_batch',
            'batch_size': rule['batch_size'],
            'timeout_minutes': rule['timeout_minutes'],
            'elapsed_minutes': elapsed_minutes,
            'remaining_minutes': remaining_minutes,
            'remaining_seconds': remaining_seconds,
            'cycle_started_at': active_cycle_start.isoformat(),
            'cycle_ends_at': next_boundary_after_oldest.isoformat(),
        }

    @staticmethod
    def _approval_cycle_next_boundary(*, now: datetime, timeout_minutes: int) -> datetime:
        interval_seconds = max(int(timeout_minutes or 0), 1) * 60
        local_tz = timezone(timedelta(hours=8))
        localized_now = now.astimezone(local_tz)
        local_day_start = localized_now.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed_seconds = int((localized_now - local_day_start).total_seconds())
        next_boundary_seconds = ((elapsed_seconds // interval_seconds) + 1) * interval_seconds
        next_boundary_local = local_day_start + timedelta(seconds=next_boundary_seconds)
        return next_boundary_local.astimezone(now.tzinfo or timezone.utc)

    @staticmethod
    def _approval_cycle_window(*, now: datetime, timeout_minutes: int, cycle_anchor_at: Optional[str] = None) -> Dict[str, datetime]:
        interval_seconds = max(int(timeout_minutes or 0), 1) * 60
        anchor = now
        if cycle_anchor_at:
            try:
                parsed_anchor = parse_iso_datetime(cycle_anchor_at)
                if parsed_anchor.tzinfo is None:
                    parsed_anchor = parsed_anchor.replace(tzinfo=timezone.utc)
                anchor = parsed_anchor
            except Exception:
                anchor = now
        if anchor > now:
            anchor = now
        elapsed_seconds = max(int((now - anchor).total_seconds()), 0)
        completed_cycles = elapsed_seconds // interval_seconds
        cycle_start = anchor + timedelta(seconds=completed_cycles * interval_seconds)
        cycle_end = cycle_start + timedelta(seconds=interval_seconds)
        return {
            'anchor_at': anchor,
            'cycle_started_at': cycle_start,
            'cycle_ends_at': cycle_end,
        }

    @staticmethod
    def _approval_cycle_anchor_deadline(*, now: datetime, timeout_minutes: int, cycle_anchor_at: Optional[str] = None) -> Dict[str, datetime]:
        interval_seconds = max(int(timeout_minutes or 0), 1) * 60
        anchor = now
        if cycle_anchor_at:
            try:
                parsed_anchor = parse_iso_datetime(cycle_anchor_at)
                if parsed_anchor.tzinfo is None:
                    parsed_anchor = parsed_anchor.replace(tzinfo=timezone.utc)
                anchor = parsed_anchor
            except Exception:
                anchor = now
        if anchor > now:
            anchor = now
        return {
            'anchor_at': anchor,
            'cycle_started_at': anchor,
            'cycle_ends_at': anchor + timedelta(seconds=interval_seconds),
        }

    @staticmethod
    def _binding_oldest_pending_at(probe: Dict[str, Any]) -> Optional[str]:
        requesters = list(probe.get('requesters') or []) if isinstance(probe.get('requesters'), list) else []
        oldest_candidates: List[str] = []
        for requester in requesters:
            if not isinstance(requester, dict):
                continue
            requested_at_iso = str(requester.get('requestedAtIso') or '').strip()
            if requested_at_iso:
                oldest_candidates.append(requested_at_iso)
                continue
            requested_at_unix = requester.get('requestedAtUnix')
            if requested_at_unix not in (None, ''):
                try:
                    oldest_candidates.append(datetime.fromtimestamp(float(requested_at_unix), tz=timezone.utc).isoformat())
                except Exception:
                    pass
        return min(oldest_candidates) if oldest_candidates else None

    @staticmethod
    def _binding_next_approval_eta_text(*, pending_count: int, batch_size: int, timeout_minutes: int, elapsed_minutes: int, ready: bool, reason_code: str, remaining_minutes: int) -> str:
        if pending_count <= 0:
            return '下一轮'
        if ready:
            return '可审批'
        return f'{remaining_minutes}分后'

    def _build_binding_next_approval_runtime(self, *, responsible_type: str, binding: Dict[str, Any], probe: Dict[str, Any]) -> Dict[str, Any]:
        normalized_type = str(responsible_type or '').strip()
        if normalized_type not in {'registration_group', 'official_group'}:
            return {}
        zero_pending_unverified = bool((probe or {}).get('zero_pending_unverified'))
        approval_state_status = str((probe or {}).get('approval_state_status') or (probe or {}).get('truth_state', {}).get('status') or '').strip()
        unverified_pending = approval_state_status in {'unverified_pending', 'pending_unverified'}
        pending_count_raw = normalize_int_or_none((probe or {}).get('pending_count'))
        pending_count_unknown = pending_count_raw is None
        pending_count = 0 if pending_count_unknown else max(int(pending_count_raw), 0)
        member_count = normalize_int_or_none((probe or {}).get('member_count'))
        if member_count is None:
            member_count = normalize_int_or_none((probe or {}).get('participants_count_raw'))
        if member_count is None:
            member_count = normalize_int_or_none((probe or {}).get('participants_count'))
        if member_count is None:
            member_count = normalize_int_or_none(binding.get('last_probe_member_count') or binding.get('member_count'))
        if member_count is None and isinstance(binding.get('approval_queue_truth'), dict):
            truth = dict(binding.get('approval_queue_truth') or {})
            current_truth = dict(truth.get('current_truth') or truth.get('current_truth_raw') or {}) if isinstance(truth.get('current_truth') or truth.get('current_truth_raw'), dict) else {}
            member_count = normalize_int_or_none(current_truth.get('member_count') or current_truth.get('memberCount') or truth.get('member_count') or truth.get('memberCount'))
        oldest_pending_at = self._binding_oldest_pending_at(probe)
        batch_size = max(int(binding.get('approval_count_threshold') or 0), 1)
        timeout_minutes = max(int(binding.get('approval_timeout_minutes') or 0), 1)
        now = parse_iso_datetime(utc_now())
        cycle_anchor_at = str(binding.get('cycle_anchor_at') or '').strip() or None
        probe_quality_fields = {
            'zero_pending_unverified': zero_pending_unverified,
            'zero_pending_unverified_reason': (probe or {}).get('zero_pending_unverified_reason'),
            'zero_pending_verified_by': (probe or {}).get('zero_pending_verified_by'),
            'pending_zero_confidence': (probe or {}).get('pending_zero_confidence'),
            'probe_data_quality': (probe or {}).get('probe_data_quality') or (probe or {}).get('data_quality'),
            'approval_state_status': approval_state_status or None,
            'unverified_pending_reason': (probe or {}).get('unverified_pending_reason'),
            'empty_queue_visible': bool((probe or {}).get('empty_queue_visible')),
            'has_pending_section': bool((probe or {}).get('has_pending_section')),
            'has_pending_request_row': bool((probe or {}).get('has_pending_request_row')),
        }
        if member_count is not None:
            probe_quality_fields['member_count'] = member_count
            probe_quality_fields['last_probe_member_count'] = member_count
        if pending_count_unknown:
            return {
                **probe_quality_fields,
                'next_approval_ready': False,
                'next_approval_reason_code': 'pending_count_unknown',
                'next_approval_eta_text': '等待群状态读数',
                'pending_count': None,
                'trusted_pending_count': None,
                'ui_pending_count': None,
                'api_pending_count': None,
                'requester_ids': list((probe or {}).get('requester_ids') or []) if isinstance((probe or {}).get('requester_ids'), list) else [],
                'requesters': list((probe or {}).get('requesters') or []) if isinstance((probe or {}).get('requesters'), list) else [],
                'next_approval_pending_count': None,
                'next_approval_batch_size': batch_size,
                'next_approval_timeout_minutes': timeout_minutes,
                'next_approval_elapsed_minutes': 0,
                'next_approval_remaining_minutes': None,
                'next_approval_remaining_seconds': None,
                'next_approval_oldest_pending_at': oldest_pending_at,
            }
        if cycle_anchor_at:
            try:
                parsed_cycle_anchor = parse_iso_datetime(cycle_anchor_at)
                if parsed_cycle_anchor.tzinfo is None:
                    parsed_cycle_anchor = parsed_cycle_anchor.replace(tzinfo=timezone.utc)
                if parsed_cycle_anchor > now:
                    cycle_anchor_at = None
            except Exception:
                cycle_anchor_at = None
        if pending_count <= 0:
            if cycle_anchor_at:
                cycle_window = self._approval_cycle_window(now=now, timeout_minutes=timeout_minutes, cycle_anchor_at=cycle_anchor_at)
                cycle_end = cycle_window['cycle_ends_at']
                cycle_start = cycle_window['cycle_started_at']
                elapsed_minutes = max(0, int((now - cycle_start).total_seconds() // 60))
            else:
                cycle_end = self._approval_cycle_next_boundary(now=now, timeout_minutes=timeout_minutes)
                cycle_start = cycle_end - timedelta(minutes=timeout_minutes)
                elapsed_minutes = max(0, timeout_minutes - max((max(int((cycle_end - now).total_seconds()), 0) + 59) // 60, 0))
            remaining_seconds = max(int((cycle_end - now).total_seconds()), 0)
            remaining_minutes = max((remaining_seconds + 59) // 60, 0)
            return {
                **probe_quality_fields,
                'next_approval_ready': False,
                'next_approval_reason_code': 'zero_pending_unverified' if zero_pending_unverified else 'waiting_next_cycle',
                'next_approval_eta_text': '待核验，暂不按 0 人展示' if zero_pending_unverified else self._binding_next_approval_eta_text(
                    pending_count=pending_count,
                    batch_size=batch_size,
                    timeout_minutes=timeout_minutes,
                    elapsed_minutes=elapsed_minutes,
                    ready=False,
                    reason_code='waiting_next_cycle',
                    remaining_minutes=remaining_minutes,
                ),
                'next_approval_pending_count': None if zero_pending_unverified else pending_count,
                'next_approval_batch_size': batch_size,
                'next_approval_timeout_minutes': timeout_minutes,
                'next_approval_elapsed_minutes': elapsed_minutes,
                'next_approval_remaining_minutes': remaining_minutes,
                'next_approval_remaining_seconds': remaining_seconds,
                'next_approval_oldest_pending_at': oldest_pending_at,
            }
        if unverified_pending:
            cycle_window = self._approval_cycle_anchor_deadline(now=now, timeout_minutes=timeout_minutes, cycle_anchor_at=cycle_anchor_at)
            remaining_seconds = max(int((cycle_window['cycle_ends_at'] - now).total_seconds()), 0)
            remaining_minutes = max((remaining_seconds + 59) // 60, 0)
            return {
                **probe_quality_fields,
                'next_approval_ready': False,
                'next_approval_reason_code': 'pending_unverified',
                'next_approval_eta_text': '待核验，暂不自动审批',
                'next_approval_pending_count': None,
                'next_approval_batch_size': batch_size,
                'next_approval_timeout_minutes': timeout_minutes,
                'next_approval_elapsed_minutes': 0,
                'next_approval_remaining_minutes': remaining_minutes,
                'next_approval_remaining_seconds': remaining_seconds,
                'next_approval_oldest_pending_at': oldest_pending_at,
            }
        if cycle_anchor_at:
            cycle_window = self._approval_cycle_anchor_deadline(now=now, timeout_minutes=timeout_minutes, cycle_anchor_at=cycle_anchor_at)
            cycle_end = cycle_window['cycle_ends_at']
            cycle_start = cycle_window['cycle_started_at']
        else:
            if not oldest_pending_at:
                return {
                    **probe_quality_fields,
                    'next_approval_ready': False,
                    'next_approval_reason_code': 'oldest_pending_unknown',
                    'next_approval_eta_text': '已有待审批，等待更多实时数据后再计算',
                    'next_approval_pending_count': pending_count,
                    'next_approval_batch_size': batch_size,
                    'next_approval_timeout_minutes': timeout_minutes,
                    'next_approval_elapsed_minutes': 0,
                    'next_approval_remaining_minutes': timeout_minutes,
                    'next_approval_remaining_seconds': timeout_minutes * 60,
                    'next_approval_oldest_pending_at': oldest_pending_at,
                }
            oldest = parse_iso_datetime(oldest_pending_at)
            cycle_end = self._approval_cycle_next_boundary(now=oldest, timeout_minutes=timeout_minutes)
            cycle_start = cycle_end - timedelta(minutes=timeout_minutes)
        elapsed_minutes = max(0, int((now - cycle_start).total_seconds() // 60))
        ready = pending_count >= batch_size or now >= cycle_end
        reason_code = 'batch_size_reached' if pending_count >= batch_size else ('timeout_flush' if now >= cycle_end else 'waiting_for_batch')
        remaining_seconds = 0 if ready else max(int((cycle_end - now).total_seconds()), 0)
        remaining_minutes = 0 if ready else max((remaining_seconds + 59) // 60, 0)
        return {
            **probe_quality_fields,
            'next_approval_ready': ready,
            'next_approval_reason_code': reason_code,
            'next_approval_eta_text': self._binding_next_approval_eta_text(
                pending_count=pending_count,
                batch_size=batch_size,
                timeout_minutes=timeout_minutes,
                elapsed_minutes=elapsed_minutes,
                ready=ready,
                reason_code=reason_code,
                remaining_minutes=remaining_minutes,
            ),
            'next_approval_pending_count': pending_count,
            'next_approval_batch_size': batch_size,
            'next_approval_timeout_minutes': timeout_minutes,
            'next_approval_elapsed_minutes': elapsed_minutes,
            'next_approval_remaining_minutes': remaining_minutes,
            'next_approval_remaining_seconds': remaining_seconds,
            'next_approval_oldest_pending_at': oldest_pending_at,
        }

    @staticmethod
    def _paused_binding_next_approval_runtime(*, pending_count: int, batch_size: int, timeout_minutes: int, reason_code: str, eta_text: str) -> Dict[str, Any]:
        return {
            'next_approval_ready': False,
            'next_approval_reason_code': reason_code,
            'next_approval_eta_text': eta_text,
            'next_approval_pending_count': max(int(pending_count or 0), 0),
            'next_approval_batch_size': max(int(batch_size or 0), 1),
            'next_approval_timeout_minutes': max(int(timeout_minutes or 0), 1),
            'next_approval_elapsed_minutes': 0,
            'next_approval_remaining_minutes': None,
            'next_approval_remaining_seconds': None,
            'next_approval_oldest_pending_at': None,
            'next_approval_paused': True,
        }

    def _request_whatsapp_approval_group_state(self, base_url: str, registration_group: str, *, timeout_seconds: float = 30.0, expected_runtime_phone: str = '', account_key: str = '') -> Dict[str, Any]:
        normalized_base_url = str(base_url or '').strip().rstrip('/')
        normalized_group = str(registration_group or '').strip()
        normalized_expected_phone = ''.join(ch for ch in str(expected_runtime_phone or account_key or '') if ch.isdigit())
        if not normalized_base_url:
            raise RuntimeError('whatsapp approval runtime base_url is required')
        if not normalized_group:
            raise RuntimeError('registration_group is required')
        payload = {
            'registration_group': normalized_group,
            'timeoutMs': 2500,
            'maxAgeMs': 20000,
        }
        if normalized_expected_phone:
            payload['expected_runtime_phone'] = normalized_expected_phone
        if account_key:
            payload['account_key'] = str(account_key or '').strip()
        return fetch_json(
            f"{normalized_base_url}/group-state",
            method='POST',
            payload=payload,
            timeout=min(max(float(timeout_seconds or 0.0), 0.1), 4.0),
        )

    def _request_whatsapp_approval_group_state_with_retry(
        self,
        base_url: str,
        registration_group: str,
        *,
        attempts: int = 3,
        retry_delay_seconds: float = 0.0,
        timeout_seconds: float = 30.0,
        expected_runtime_phone: str = '',
        account_key: str = '',
    ) -> Dict[str, Any]:
        normalized_attempts = max(1, int(attempts or 1))
        last_error: Optional[Exception] = None
        for index in range(normalized_attempts):
            try:
                try:
                    return self._request_whatsapp_approval_group_state(
                        base_url,
                        registration_group,
                        timeout_seconds=timeout_seconds,
                        expected_runtime_phone=expected_runtime_phone,
                        account_key=account_key,
                    )
                except TypeError as exc:
                    message = str(exc)
                    if 'timeout_seconds' not in message:
                        raise
                    try:
                        return self._request_whatsapp_approval_group_state(
                            base_url,
                            registration_group,
                            expected_runtime_phone=expected_runtime_phone,
                            account_key=account_key,
                        )
                    except TypeError as inner_exc:
                        inner_message = str(inner_exc)
                        if 'expected_runtime_phone' not in inner_message and 'account_key' not in inner_message:
                            raise
                        return self._request_whatsapp_approval_group_state(base_url, registration_group)
            except Exception as exc:
                last_error = exc
                if index >= normalized_attempts - 1:
                    break
                if retry_delay_seconds > 0:
                    time.sleep(retry_delay_seconds)
        if last_error is not None:
            raise last_error
        raise RuntimeError('group state probe failed without error')

    @staticmethod
    def _whatsapp_binding_probe_target(binding: Dict[str, Any]) -> str:
        if Service._whatsapp_binding_should_resolve_from_invite_link(binding):
            return ''
        runtime_group_id = Service._whatsapp_binding_runtime_group_id(binding)
        if runtime_group_id:
            return runtime_group_id
        registration_group = str(binding.get('registration_group') or '').strip()
        if registration_group and not _looks_like_whatsapp_invite_link(registration_group):
            return registration_group
        return ''

    @staticmethod
    def _whatsapp_binding_invite_link_target(binding: Dict[str, Any]) -> str:
        for value in (
            str(binding.get('registration_group') or '').strip(),
            str(binding.get('link') or '').strip(),
            str(binding.get('group_id') or '').strip(),
        ):
            if value and _looks_like_whatsapp_invite_link(value):
                return value
        return ''

    @staticmethod
    def _whatsapp_binding_should_resolve_from_invite_link(binding: Dict[str, Any]) -> bool:
        item = dict(binding or {})
        if not Service._whatsapp_binding_invite_link_target(item):
            return False
        identity_status = str(item.get('identity_status') or '').strip().lower()
        identity_reason = str(item.get('identity_rebuild_reason') or '').strip().lower()
        last_probe_reason = str(item.get('last_probe_reason') or '').strip().lower()
        if identity_status == 'resolved' and last_probe_reason == 'resolved':
            return False
        return bool(
            identity_status in {'unresolved', 'stale', 'needs_rebuild', 'permission_pending'}
            or identity_reason in {'group_link_config_changed', 'stale_identity', 'manual_rebuild'}
            or last_probe_reason in {
                'group_link_config_changed',
                'stale_identity_rebuild',
                'manual_identity_rebuild',
                'identity_unresolved',
                'probe_group_id_mismatch',
            }
        )

    @staticmethod
    def _whatsapp_binding_probe_candidates(binding: Dict[str, Any], *, allow_non_jid_fallback: bool = False) -> List[str]:
        runtime_group_id = Service._whatsapp_binding_runtime_group_id(binding)
        invite_link = Service._whatsapp_binding_invite_link_target(binding)
        prefer_invite_link = bool(allow_non_jid_fallback and invite_link and Service._whatsapp_binding_should_resolve_from_invite_link(binding))
        candidates: List[str] = []
        if prefer_invite_link:
            candidates.append(invite_link)
        if runtime_group_id and not prefer_invite_link:
            candidates.append(runtime_group_id)
        if allow_non_jid_fallback:
            for candidate in (
                str(binding.get('registration_group') or '').strip(),
                str(binding.get('link') or '').strip(),
            ):
                if candidate and candidate not in candidates:
                    candidates.append(candidate)
        return candidates

    @staticmethod
    def _whatsapp_binding_probe_label(binding: Dict[str, Any], target: str = '') -> str:
        return (
            str(binding.get('group_name') or '').strip()
            or str(binding.get('target_group_label') or '').strip()
            or str(binding.get('link') or '').strip()
            or str(target or '').strip()
        )

    def _probe_whatsapp_binding_group_state(
        self,
        *,
        responsible_type: str,
        binding: Dict[str, Any],
        runtime_state: Optional[Dict[str, Any]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        allow_shared_fallback: bool = True,
        allow_non_jid_fallback: bool = False,
        attempts: int = 3,
        retry_delay_seconds: float = 0.0,
        timeout_seconds: float = 30.0,
    ) -> Dict[str, Any]:
        normalized_type = str(responsible_type or '').strip()
        runtime_state = dict(runtime_state or {})
        session_state = dict(session_state or {})
        target = self._whatsapp_binding_probe_target(binding)
        target_label = self._whatsapp_binding_probe_label(binding, target)
        target_candidates = self._whatsapp_binding_probe_candidates(
            binding,
            allow_non_jid_fallback=allow_non_jid_fallback,
        )
        expected_account_key = str(binding.get('account_key') or runtime_state.get('account_key') or '').strip()
        expected_runtime_phone = ''.join(ch for ch in expected_account_key if ch.isdigit())
        if not normalized_type or not target_candidates:
            return {}
        candidate_base_urls: List[str] = []
        runtime_base_url = str(runtime_state.get('base_url') or '').strip().rstrip('/')
        if runtime_state.get('active') and runtime_base_url:
            # 账号未完成登录校验时，不能拿这个 runtime 去读群状态。
            # 否则列表自动刷新会把“待扫码/待登录”的账号误探成其它已登录 runtime 当前打开的群，
            # 造成审批账号真实状态在不同账号/群之间抖动。
            if bool(session_state.get('login_verified')):
                candidate_base_urls.append(runtime_base_url)
        if normalized_type != 'registration_group' and allow_shared_fallback and str(runtime_state.get('source') or '').strip() != 'dedicated':
            config = self.get_production_ops_daemon_config().get('config') or {}
            shared_base_url = str(config.get('worker_base_url') or '').strip().rstrip('/')
            if shared_base_url and shared_base_url not in candidate_base_urls:
                candidate_base_urls.append(shared_base_url)
        last_error = None
        for base_url in candidate_base_urls:
            for probe_target in target_candidates:
                try:
                    candidate_timeout = min(float(timeout_seconds or 10.0), 4.0) if 'chat.whatsapp.com' in probe_target else timeout_seconds
                    payload = self._request_whatsapp_approval_group_state_with_retry(
                        base_url,
                        probe_target,
                        attempts=1 if 'chat.whatsapp.com' in probe_target else attempts,
                        retry_delay_seconds=retry_delay_seconds,
                        timeout_seconds=candidate_timeout,
                        expected_runtime_phone=expected_runtime_phone,
                        account_key=expected_account_key,
                    )
                except Exception as exc:
                    last_error = exc
                    continue
                if isinstance(payload, dict):
                    normalized = dict(payload)
                    normalized['source_base_url'] = base_url
                    normalized['probe_target'] = target_label
                    normalized['probe_request_target'] = probe_target
                    return normalized
        if last_error:
            return {'error': str(last_error), 'probe_target': target_label, 'probe_request_target': target}
        return {}

    def _apply_live_group_identity_to_binding(
        self,
        binding: Dict[str, Any],
        *,
        responsible_type: str,
        runtime_state: Optional[Dict[str, Any]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        allow_shared_fallback: bool = True,
        allow_non_jid_fallback: bool = False,
        overwrite_existing_name: bool = False,
        attempts: int = 3,
        retry_delay_seconds: float = 0.0,
        timeout_seconds: float = 30.0,
    ) -> Dict[str, Any]:
        probe = self._probe_whatsapp_binding_group_state(
            responsible_type=responsible_type,
            binding=binding,
            runtime_state=runtime_state,
            session_state=session_state,
            allow_shared_fallback=allow_shared_fallback,
            allow_non_jid_fallback=allow_non_jid_fallback,
            attempts=attempts,
            retry_delay_seconds=retry_delay_seconds,
            timeout_seconds=timeout_seconds,
        )
        live_group_name = str(probe.get('group_name') or '').strip()
        live_group_id = str(probe.get('group_id') or '').strip()
        current_group_id = str(binding.get('group_id') or '').strip()
        group_identity_safe = bool(live_group_id and (not current_group_id or live_group_id == current_group_id))
        if live_group_name and group_identity_safe and (overwrite_existing_name or not str(binding.get('group_name') or '').strip()):
            binding['group_name'] = live_group_name
        if live_group_id and not current_group_id:
            binding['group_id'] = live_group_id
        return probe

    def _persist_registration_group_binding_live_names(
        self,
        account_key: str,
        bindings: list[dict[str, Any]],
        runtime_rows: list[dict[str, Any]],
        binding_verifiers: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key or not bindings:
            return bindings
        changed = False
        updated_bindings: list[dict[str, Any]] = []
        live_ready_statuses = {'mapped_live_probe_ready', 'inferred_live_probe_ready', 'live_probe_ready'}
        for binding, runtime_row, verifier in zip(bindings, runtime_rows, binding_verifiers):
            current = dict(binding or {})
            status = str((verifier or {}).get('status') or '').strip()
            live_group_name = str((runtime_row or {}).get('runtime_probe_group_name') or '').strip()
            live_group_id = str((runtime_row or {}).get('runtime_probe_group_id') or '').strip()
            current_group_id = str(current.get('group_id') or '').strip()
            current_registration_group = str(current.get('registration_group') or '').strip()
            group_identity_safe = bool(live_group_id and (not current_group_id or live_group_id == current_group_id))
            if (status in live_ready_statuses) and group_identity_safe:
                current_group_name = str(current.get('group_name') or '').strip()
                if live_group_name and (live_group_id == current_group_id or not current_group_name):
                    current['group_name'] = live_group_name
                    changed = True
                if live_group_id and (
                    not current_group_id
                    or live_group_id != current_registration_group
                ):
                    current['group_id'] = live_group_id
                    current['registration_group'] = live_group_id
                    changed = True
            updated_bindings.append(current)
        if not changed:
            return updated_bindings
        now_iso = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                'UPDATE whatsapp_approval_accounts SET group_links = ?, updated_at = ? WHERE account_key = ?',
                (json.dumps(updated_bindings, ensure_ascii=False), now_iso, normalized_key),
            )
            conn.commit()
        return updated_bindings

    def _registration_group_runtime_queue_rows(
        self,
        *,
        now_iso: str,
        accounts_payload: Optional[Dict[str, Any]] = None,
        production_ops: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        seen_groups: set[str] = set()
        cycle_anchor_map = self._production_ops_cycle_anchor_maps(production_ops or self._production_ops_daemon_snapshot()).get('registration_group') or {}
        try:
            if accounts_payload is None:
                try:
                    accounts_payload = self.list_whatsapp_approval_accounts(lightweight=True) or {}
                except TypeError:
                    accounts_payload = self.list_whatsapp_approval_accounts() or {}
            accounts = list(accounts_payload.get('rows') or accounts_payload.get('accounts') or [])
        except Exception:
            accounts = []
        for account in accounts:
            if str(account.get('responsible_type') or '').strip() != 'registration_group':
                continue
            if not bool(account.get('enabled')):
                continue
            runtime_state = account.get('runtime_state') or {}
            account_key = str(account.get('account_key') or '').strip()
            worker_base_url = str(runtime_state.get('base_url') or account.get('worker_base_url') or '').strip()
            if not account_key or not bool(runtime_state.get('active')) or not worker_base_url:
                continue
            bindings = list(account.get('group_binding_runtimes') or account.get('group_link_bindings') or [])
            for binding_index, binding in enumerate(bindings):
                if not isinstance(binding, dict):
                    continue
                if binding.get('enabled') is False:
                    continue
                queue_group = self._whatsapp_binding_probe_target(binding)
                binding_target = queue_group
                if not queue_group or not binding_target or queue_group in seen_groups:
                    continue
                truth_view = dict(binding.get('approval_queue_truth') or {}) if isinstance(binding.get('approval_queue_truth'), dict) else {}
                truth_current = dict(truth_view.get('current_truth') or truth_view.get('current_truth_raw') or {}) if isinstance(truth_view.get('current_truth') or truth_view.get('current_truth_raw'), dict) else {}
                truth_current_facts = dict(truth_current.get('facts') or {}) if isinstance(truth_current.get('facts'), dict) else {}
                pending_count = truth_view.get('pending_count')
                group_name = str(binding.get('group_name') or '').strip()
                group_id = str(binding.get('group_id') or '').strip()
                if pending_count is None:
                    continue
                try:
                    pending_count = max(int(pending_count), 0)
                except Exception:
                    continue
                requesters = []
                for requester_source in (
                    truth_current.get('requesters'),
                    truth_current_facts.get('requesters'),
                    truth_view.get('requesters'),
                ):
                    if isinstance(requester_source, list):
                        requesters = [dict(item) for item in requester_source if isinstance(item, dict)]
                        if requesters:
                            break
                oldest_pending_at = str(
                    truth_current.get('oldest_pending_at')
                    or truth_current_facts.get('oldest_pending_at')
                    or truth_view.get('oldest_pending_at')
                    or truth_current.get('source_ts')
                    or truth_current.get('verified_at')
                    or truth_view.get('verified_at')
                    or ''
                ).strip() or None
                oldest_candidates: List[str] = []
                for requester in requesters:
                    if not isinstance(requester, dict):
                        continue
                    requested_at_iso = str(requester.get('requestedAtIso') or '').strip()
                    if requested_at_iso:
                        oldest_candidates.append(requested_at_iso)
                        continue
                    requested_at_unix = requester.get('requestedAtUnix')
                    if requested_at_unix not in (None, ''):
                        try:
                            oldest_candidates.append(datetime.fromtimestamp(float(requested_at_unix), tz=timezone.utc).isoformat())
                        except Exception:
                            pass
                if pending_count > 0 and oldest_candidates:
                    oldest_pending_at = min(oldest_candidates)
                if pending_count > 0 and not oldest_pending_at:
                    oldest_pending_at = now_iso
                cycle_anchor_at = next((
                    cycle_anchor_map.get(candidate)
                    for candidate in (
                        str(binding.get('registration_group') or '').strip(),
                        str(binding.get('group_id') or '').strip(),
                        str(binding.get('link') or '').strip(),
                    )
                    if candidate and cycle_anchor_map.get(candidate)
                ), None)
                evaluated = self.evaluate_approval_batch(
                    ApprovalBatchEvaluateRequest(
                        approval_type='registration_group',
                        registration_group=queue_group,
                        pending_count=pending_count,
                        oldest_pending_at=oldest_pending_at,
                        now=now_iso,
                        batch_size=int(binding.get('approval_count_threshold') or account.get('approval_count_threshold') or 0),
                        timeout_minutes=int(binding.get('approval_timeout_minutes') or account.get('approval_timeout_minutes') or 0),
                        cycle_anchor_at=cycle_anchor_at,
                    )
                )
                evaluated.update({
                    'source': 'approval_queue_current_truth',
                    'binding_link': str(binding.get('link') or '').strip() or None,
                    'group_name': group_name or queue_group,
                    'group_id': group_id or None,
                    'account_key': account_key,
                    'account_name': str(account.get('account_name') or '').strip() or None,
                    'worker_base_url': worker_base_url or None,
                    'requesters': requesters,
                })
                rows.append(evaluated)
                seen_groups.add(queue_group)
        if rows:
            return rows
        # Approval queue summaries are snapshot-only. Do not fall back to live WhatsApp
        # group-state here; the production daemon is the single scheduled probe owner.
        return rows

    def _official_group_runtime_queue_rows(
        self,
        *,
        now_iso: str,
        accounts_payload: Optional[Dict[str, Any]] = None,
        production_ops: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        seen_targets: set[str] = set()
        cycle_anchor_map = self._production_ops_cycle_anchor_maps(production_ops or self._production_ops_daemon_snapshot()).get('official_group') or {}
        try:
            if accounts_payload is None:
                try:
                    accounts_payload = self.list_whatsapp_approval_accounts(lightweight=True) or {}
                except TypeError:
                    accounts_payload = self.list_whatsapp_approval_accounts() or {}
            accounts = list(accounts_payload.get('rows') or accounts_payload.get('accounts') or [])
        except Exception:
            accounts = []
        for account in accounts:
            if str(account.get('responsible_type') or '').strip() != 'official_group':
                continue
            if not bool(account.get('enabled')):
                continue
            runtime_state = account.get('runtime_state') or {}
            account_key = str(account.get('account_key') or '').strip()
            worker_base_url = str(runtime_state.get('base_url') or account.get('worker_base_url') or '').strip()
            if not account_key or not bool(runtime_state.get('active')) or not worker_base_url:
                continue
            bindings = list(account.get('group_binding_runtimes') or account.get('group_link_bindings') or [])
            for binding_index, binding in enumerate(bindings):
                if not isinstance(binding, dict):
                    continue
                if binding.get('enabled') is False:
                    continue
                binding_id = str(binding.get('binding_id') or '').strip()
                binding_target = (
                    str(binding.get('group_id') or '').strip()
                    or str(binding.get('link') or '').strip()
                    or str(binding.get('registration_group') or '').strip()
                    or binding_id
                    or str(binding.get('group_name') or '').strip()
                )
                if not binding_target or binding_target in seen_targets:
                    continue
                truth_view = dict(binding.get('approval_queue_truth') or {}) if isinstance(binding.get('approval_queue_truth'), dict) else {}
                truth_current = dict(truth_view.get('current_truth') or truth_view.get('current_truth_raw') or {}) if isinstance(truth_view.get('current_truth') or truth_view.get('current_truth_raw'), dict) else {}
                truth_current_payload = dict(truth_current.get('payload') or {}) if isinstance(truth_current.get('payload'), dict) else {}
                truth_current_facts = dict(truth_current.get('facts') or {}) if isinstance(truth_current.get('facts'), dict) else {}
                if not truth_current or bool(truth_view.get('stale')) or bool(truth_current.get('stale')):
                    continue
                pending_count = normalize_int_or_none(truth_view.get('pending_count'))
                if pending_count is None:
                    continue
                pending_count = max(int(pending_count), 0)
                requesters: List[Dict[str, Any]] = []
                for requester_source in (
                    truth_current.get('requesters'),
                    truth_current_payload.get('requesters'),
                    truth_current_facts.get('requesters'),
                    truth_view.get('requesters'),
                ):
                    if isinstance(requester_source, list):
                        requesters = [dict(item) for item in requester_source if isinstance(item, dict)]
                        if requesters:
                            break
                if not requesters:
                    requester_ids_source = next((
                        source for source in (
                            truth_current.get('requester_ids'),
                            truth_current.get('requesterIds'),
                            truth_current_payload.get('requester_ids'),
                            truth_view.get('requester_ids'),
                        )
                        if isinstance(source, list) and source
                    ), [])
                    requesters = [
                        {'requesterId': str(item).strip()}
                        for item in requester_ids_source
                        if str(item).strip()
                    ]
                oldest_pending_at = str(
                    truth_current.get('oldest_pending_at')
                    or truth_current_facts.get('oldest_pending_at')
                    or truth_view.get('oldest_pending_at')
                    or truth_current.get('source_ts')
                    or truth_current.get('verified_at')
                    or truth_view.get('verified_at')
                    or ''
                ).strip() or None
                oldest_candidates: List[str] = []
                for requester in requesters:
                    if not isinstance(requester, dict):
                        continue
                    requested_at_iso = str(requester.get('requestedAtIso') or '').strip()
                    if requested_at_iso:
                        oldest_candidates.append(requested_at_iso)
                        continue
                    requested_at_unix = requester.get('requestedAtUnix')
                    if requested_at_unix not in (None, ''):
                        try:
                            oldest_candidates.append(datetime.fromtimestamp(float(requested_at_unix), tz=timezone.utc).isoformat())
                        except Exception:
                            pass
                if pending_count > 0 and oldest_candidates:
                    oldest_pending_at = min(oldest_candidates)
                if pending_count > 0 and not oldest_pending_at:
                    oldest_pending_at = now_iso
                display_group = str(binding.get('group_name') or binding_target).strip() or binding_target
                routing_target = (
                    str(binding.get('group_id') or '').strip()
                    or str(binding.get('registration_group') or '').strip()
                    or str(binding.get('link') or '').strip()
                    or binding_id
                    or display_group
                )
                cycle_anchor_at = next((
                    cycle_anchor_map.get(candidate)
                    for candidate in (
                        routing_target,
                        str(binding.get('group_id') or '').strip(),
                        str(binding.get('registration_group') or '').strip(),
                        str(binding.get('link') or '').strip(),
                    )
                    if candidate and cycle_anchor_map.get(candidate)
                ), None)
                evaluated = self.evaluate_approval_batch(
                    ApprovalBatchEvaluateRequest(
                        approval_type='official_group',
                        registration_group=display_group,
                        pending_count=pending_count,
                        oldest_pending_at=oldest_pending_at,
                        now=now_iso,
                        batch_size=int(binding.get('approval_count_threshold') or account.get('approval_count_threshold') or 0),
                        timeout_minutes=int(binding.get('approval_timeout_minutes') or account.get('approval_timeout_minutes') or 0),
                        cycle_anchor_at=cycle_anchor_at,
                    )
                )
                evaluated.update({
                    'source': 'approval_queue_current_truth',
                    'target_group': routing_target,
                    'binding_link': str(binding.get('link') or '').strip() or None,
                    'binding_registration_group': str(binding.get('registration_group') or '').strip() or None,
                    'notify_profile_name': str(binding.get('notify_profile_name') or '').strip() or None,
                    'notify_robot_name': str(binding.get('notify_robot_name') or '').strip() or self._notify_robot_name(binding.get('notify_profile_name')) or None,
                    'group_name': display_group,
                    'group_id': str(binding.get('group_id') or '').strip() or None,
                    'binding_index': int(normalize_int_or_none(binding.get('binding_index')) if normalize_int_or_none(binding.get('binding_index')) is not None else binding_index),
                    'account_key': account_key,
                    'account_name': str(account.get('account_name') or '').strip() or None,
                    'worker_base_url': worker_base_url or None,
                    'requesters': requesters,
                    'approval_queue_truth': {
                        **truth_view,
                        'flow_type': str(truth_view.get('flow_type') or 'official_group'),
                    },
                })
                rows.append(evaluated)
                seen_targets.add(binding_target)
        return rows

    def _approval_batch_queue_accounts_payload(self, *, production_ops: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self.db.connect() as conn:
            raw_rows = conn.execute(
                """
                SELECT account_key, account_name, responsible_type, group_links, area, notify_profile_name,
                       approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker,
                       schedule_windows, enabled, verification_status, assigned_customer_service_user_id,
                       assigned_customer_service_username, assigned_customer_service_display_name, notes,
                       created_at, updated_at
                FROM whatsapp_approval_accounts
                WHERE responsible_type IN ('registration_group', 'official_group')
                ORDER BY CASE responsible_type WHEN 'registration_group' THEN 1 WHEN 'official_group' THEN 2 ELSE 99 END ASC,
                         CASE WHEN NULLIF(created_at, '') IS NULL THEN 1 ELSE 0 END ASC,
                         COALESCE(NULLIF(created_at, ''), '') ASC,
                         account_key ASC
                """
            ).fetchall()
        raw_dict_rows = [dict(row) for row in raw_rows]
        had_snapshot_cache = hasattr(self, '_approval_queue_snapshot_cache')
        previous_snapshot_cache = getattr(self, '_approval_queue_snapshot_cache', None)
        self._approval_queue_snapshot_cache = self._build_approval_queue_snapshot_cache_for_account_rows(raw_dict_rows)
        rows: List[Dict[str, Any]] = []
        try:
            for row in raw_dict_rows:
                account = dict(row or {})
                responsible_type = str(account.get('responsible_type') or '').strip()
                if responsible_type not in {'registration_group', 'official_group'}:
                    continue
                account_key = str(account.get('account_key') or '').strip()
                if not account_key:
                    continue
                raw_group_links: Any = []
                try:
                    raw_group_links = json.loads(account.get('group_links') or '[]')
                except Exception:
                    raw_group_links = []
                if not isinstance(raw_group_links, list):
                    raw_group_links = []
                bindings = _normalize_group_link_bindings(
                    [dict(item) if isinstance(item, dict) else {'link': str(item or '').strip()} for item in raw_group_links],
                    responsible_type=responsible_type,
                )
                default_notify_profile_name = str(account.get('notify_profile_name') or '').strip()
                default_area = str(account.get('area') or '').strip()
                legacy_count_threshold, legacy_timeout_minutes = _legacy_approval_thresholds(account.get('approval_rule'))
                default_approval_count_threshold = _coerce_positive_int(account.get('approval_count_threshold'), legacy_count_threshold)
                default_approval_timeout_minutes = _coerce_positive_int(account.get('approval_timeout_minutes'), legacy_timeout_minutes)
                account_schedule_windows = _normalize_schedule_windows_payload(
                    json.loads(account.get('schedule_windows') or '[]')
                    if isinstance(account.get('schedule_windows'), str) and str(account.get('schedule_windows') or '').strip()
                    else account.get('schedule_windows') or []
                )
                provider_decision = self._resolve_wa_provider_decision(account=account, responsible_type=responsible_type)
                runtime_state = {
                    'active': bool(account.get('enabled')),
                    'configured': bool(bindings),
                    'source': 'approval_batch_queue_snapshot',
                    'base_url': self._resolve_baileys_runtime_base_url(account=account, binding=_preferred_group_binding(bindings)) or None,
                    'provider_name': provider_decision.get('provider_name'),
                    'provider_mode': provider_decision.get('provider_mode'),
                }
                group_link_bindings: List[Dict[str, Any]] = []
                for idx, binding in enumerate(bindings):
                    item = dict(binding or {})
                    item['binding_index'] = normalize_int_or_none(item.get('binding_index'))
                    if item['binding_index'] is None:
                        item['binding_index'] = idx
                    item['index'] = normalize_int_or_none(item.get('index'))
                    if item['index'] is None:
                        item['index'] = item['binding_index']
                    item['area'] = str(item.get('area') or default_area).strip()
                    item['notify_profile_name'] = str(item.get('notify_profile_name') or default_notify_profile_name).strip()
                    item = self._apply_account_notify_profile_to_official_binding(
                        item,
                        account=account,
                        responsible_type=responsible_type,
                    )
                    item['approval_count_threshold'] = _coerce_positive_int(item.get('approval_count_threshold'), default_approval_count_threshold)
                    item['approval_timeout_minutes'] = _coerce_positive_int(item.get('approval_timeout_minutes'), default_approval_timeout_minutes)
                    item['auto_recover_worker'] = bool(item.get('auto_recover_worker')) if item.get('auto_recover_worker') is not None else bool(account.get('auto_recover_worker'))
                    item['schedule_windows'] = _normalize_schedule_windows_payload(item.get('schedule_windows') or account_schedule_windows)
                    item['schedule_runtime'] = self._schedule_runtime(item.get('schedule_windows') or [])
                    item['notify_robot_name'] = item.get('notify_robot_name') or self._notify_robot_name(item.get('notify_profile_name'))
                    item['approval_rule_text'] = _approval_condition_text(item.get('approval_count_threshold'), item.get('approval_timeout_minutes'))
                    item['config_fingerprint'] = item.get('config_fingerprint') or _whatsapp_approval_binding_config_fingerprint(item)
                    item['approval_scope'] = responsible_type
                    item['responsible_type'] = responsible_type
                    item['provider_name'] = provider_decision.get('provider_name')
                    item['provider_mode'] = provider_decision.get('provider_mode')
                    item['provider_capabilities'] = provider_decision.get('provider_capabilities') or {}
                    item['provider_decision'] = provider_decision
                    item['target_group_label'] = str(
                        item.get('group_name')
                        or item.get('group_id')
                        or item.get('registration_group')
                        or item.get('link')
                        or ''
                    ).strip()
                    snapshots = self._load_approval_binding_queue_snapshots(account_key, item)
                    truth_view = self._approval_queue_truth_view(snapshots.get('current_truth'), snapshots.get('latest_probe'))
                    truth_view['flow_type'] = responsible_type
                    item['approval_queue_truth'] = truth_view
                    if normalize_int_or_none(truth_view.get('member_count')) is not None:
                        item['member_count'] = normalize_int_or_none(truth_view.get('member_count'))
                        item['last_probe_member_count'] = item['member_count']
                    item['syncing'] = truth_view.get('syncing')
                    item['can_manual_approve'] = truth_view.get('can_manual_approve')
                    item['manual_approve_allowed'] = truth_view.get('manual_approve_allowed')
                    item['monitoring_effective'] = bool(account.get('enabled')) and item.get('enabled') is not False
                    item['monitoring_status_text'] = '监控中' if item['monitoring_effective'] else ('不监控' if item.get('enabled') is False else '账号已关闭')
                    group_link_bindings.append(item)
                rows.append({
                    'account_key': account_key,
                    'account_name': account.get('account_name'),
                    'responsible_type': responsible_type,
                    'approval_scope': responsible_type,
                    'enabled': bool(account.get('enabled')),
                    'area': default_area,
                    'notify_profile_name': default_notify_profile_name,
                    'approval_count_threshold': default_approval_count_threshold,
                    'approval_timeout_minutes': default_approval_timeout_minutes,
                    'verification_status': str(account.get('verification_status') or '').strip(),
                    'assigned_customer_service_user_ids': self._whatsapp_approval_assigned_customer_service_ids_from_row(account),
                    'provider_name': provider_decision.get('provider_name'),
                    'provider_mode': provider_decision.get('provider_mode'),
                    'provider_capabilities': provider_decision.get('provider_capabilities') or {},
                    'provider_decision': provider_decision,
                    'runtime_state': runtime_state,
                    'session_state': {},
                    'runtime_status': 'active' if runtime_state.get('active') else 'inactive',
                    'monitor_runtime_active': bool(runtime_state.get('active')),
                    'service_scope': {'code': 'approval_batch_queue_snapshot', 'ready': bool(runtime_state.get('active'))},
                    'group_link_bindings': group_link_bindings,
                    'group_binding_runtimes': group_link_bindings,
                    'group_links': [str(item.get('link') or '').strip() for item in group_link_bindings if str(item.get('link') or '').strip()],
                    'group_count': len(group_link_bindings),
                    'list_mode': 'approval_batch_queue_snapshot',
                })
        finally:
            if had_snapshot_cache:
                self._approval_queue_snapshot_cache = previous_snapshot_cache
            else:
                try:
                    delattr(self, '_approval_queue_snapshot_cache')
                except AttributeError:
                    pass
        return {'rows': rows, 'list_mode': 'approval_batch_queue_snapshot'}

    def approval_batch_queue(self) -> Dict[str, Any]:
        self.reconcile_task_residue()
        now = utc_now()
        production_ops = self._production_ops_daemon_snapshot_light()
        accounts_payload = self._approval_batch_queue_accounts_payload(production_ops=production_ops)
        registration_statuses = ('new', 'engaged', 'manual_review_pending', 'recognition_pending', 'account_submitted', 'bind_check_pending', 'bind_failed')
        with self.db.connect() as conn:
            registration_rows = [dict(r) for r in conn.execute(
                f"""
                SELECT pendaftaran_group AS registration_group, COUNT(*) AS pending_count,
                       MIN(updated_at) AS oldest_pending_at
                FROM leads
                WHERE pendaftaran_group IS NOT NULL
                  AND current_status IN ({','.join(['?'] * len(registration_statuses))})
                GROUP BY pendaftaran_group
                ORDER BY pending_count DESC, pendaftaran_group ASC
                """,
                registration_statuses,
            ).fetchall()]
        official_runtime_rows = self._official_group_runtime_queue_rows(
            now_iso=now,
            accounts_payload=accounts_payload,
            production_ops=production_ops,
        )
        registration_runtime_rows = self._registration_group_runtime_queue_rows(
            now_iso=now,
            accounts_payload=accounts_payload,
            production_ops=production_ops,
        )
        with self.db.connect() as conn:
            official_runtime_scope_configured = bool(conn.execute(
                "SELECT 1 FROM whatsapp_approval_accounts WHERE responsible_type = 'official_group' LIMIT 1"
            ).fetchone())
            registration_runtime_scope_configured = bool(conn.execute(
                "SELECT 1 FROM whatsapp_approval_accounts WHERE responsible_type = 'registration_group' LIMIT 1"
            ).fetchone())
        # Official-group approval is current-truth only. Historical lead states are
        # useful for CRM diagnostics, but they are not WhatsApp membership-request
        # facts and must never become an executable approval queue.
        evaluated_official_rows = official_runtime_rows if official_runtime_scope_configured else []
        if registration_runtime_scope_configured:
            evaluated_registration_rows = registration_runtime_rows
        else:
            evaluated_registration_rows = [
                self.evaluate_approval_batch(
                    ApprovalBatchEvaluateRequest(
                        approval_type='registration_group',
                        registration_group=row['registration_group'],
                        pending_count=row['pending_count'],
                        oldest_pending_at=row['oldest_pending_at'] or now,
                        now=now,
                    )
                ) for row in registration_rows
            ]
        return {
            'registration_groups': evaluated_registration_rows,
            'official_groups': evaluated_official_rows,
        }


__all__ = ['GroupAtmosphereServiceMixin']
