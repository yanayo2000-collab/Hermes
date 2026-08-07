from __future__ import annotations

from app.main_shared import *
from app.timo_guild_identity import timo_guild_storage_name


class ApprovalServiceMixin:
    def _build_registration_group_approval_batch_existing_response(
        self,
        existing: Dict[str, Any],
        *,
        request_snapshot: Dict[str, Any],
        fallback_crm_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        existing_request = dict(existing.get('request_snapshot_dict') or {})
        existing_response = dict(existing.get('response_snapshot_dict') or {})
        existing_status = str(existing.get('status') or 'failed').strip() or 'failed'
        return {
            'accepted': True,
            'crm_sync_status': 'processing' if existing_status == 'processing' else ('success' if existing_status == 'success' else 'failed'),
            'crm_payload': dict(existing_request.get('crm_payload') or fallback_crm_payload),
            'crm_response': existing_response,
            'approval_run_id': str(existing.get('approval_run_id') or existing_request.get('approval_run_id') or '').strip() or None,
            'request_snapshot': {k: existing_request.get(k) for k in request_snapshot.keys()},
            'elapsed_seconds': 0.0,
            'duplicate': True,
        }

    def _claim_registration_group_approval_batch_run(self, approval_run_id: str, request_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        normalized_run_id = str(approval_run_id or '').strip()
        if not normalized_run_id:
            return {'claimed': True}
        now = utc_now()
        serialized_request = json.dumps(request_snapshot, ensure_ascii=False)
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                "SELECT approval_run_id, sync_log_id, status, request_snapshot, response_snapshot, created_at, updated_at FROM registration_group_approval_batch_runs WHERE approval_run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO registration_group_approval_batch_runs (approval_run_id, sync_log_id, status, request_snapshot, response_snapshot, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (normalized_run_id, None, 'processing', serialized_request, json.dumps({}, ensure_ascii=False), now, now),
                )
                conn.commit()
                return {'claimed': True}
            row_dict = dict(row)
            status = str(row_dict.get('status') or '').strip()
            if status == 'failed':
                cursor = conn.execute(
                    "UPDATE registration_group_approval_batch_runs SET sync_log_id = NULL, status = 'processing', request_snapshot = ?, response_snapshot = ?, updated_at = ? WHERE approval_run_id = ? AND status = 'failed'",
                    (serialized_request, json.dumps({}, ensure_ascii=False), now, normalized_run_id),
                )
                if cursor.rowcount > 0:
                    conn.commit()
                    return {'claimed': True}
                row = conn.execute(
                    "SELECT approval_run_id, sync_log_id, status, request_snapshot, response_snapshot, created_at, updated_at FROM registration_group_approval_batch_runs WHERE approval_run_id = ?",
                    (normalized_run_id,),
                ).fetchone()
                row_dict = dict(row) if row else row_dict
            conn.commit()
        return {'claimed': False, 'row': self._deserialize_registration_group_approval_batch_run_row(row_dict)}

    def _wait_for_registration_group_approval_batch_run(self, approval_run_id: str, *, timeout_seconds: float = 5.0, poll_interval_seconds: float = 0.05) -> Optional[Dict[str, Any]]:
        deadline = time.perf_counter() + max(0.1, float(timeout_seconds or 0.0))
        while time.perf_counter() < deadline:
            row = self._find_registration_group_approval_batch_sync_log(approval_run_id)
            if row is None:
                time.sleep(poll_interval_seconds)
                continue
            if str(row.get('status') or '').strip() != 'processing':
                return row
            time.sleep(poll_interval_seconds)
        return self._find_registration_group_approval_batch_sync_log(approval_run_id)

    def _deserialize_registration_group_approval_batch_run_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(row or {})
        try:
            normalized['request_snapshot_dict'] = json.loads(normalized.get('request_snapshot') or '{}')
        except Exception:
            normalized['request_snapshot_dict'] = {}
        try:
            normalized['response_snapshot_dict'] = json.loads(normalized.get('response_snapshot') or '{}')
        except Exception:
            normalized['response_snapshot_dict'] = {}
        return normalized

    def _find_registration_group_approval_batch_sync_log(self, approval_run_id: str) -> Optional[Dict[str, Any]]:
        normalized_run_id = str(approval_run_id or '').strip()
        if not normalized_run_id:
            return None
        with self.db.connect() as conn:
            batch_row = conn.execute(
                "SELECT approval_run_id, sync_log_id, status, request_snapshot, response_snapshot, created_at, updated_at FROM registration_group_approval_batch_runs WHERE approval_run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if batch_row:
                row = dict(batch_row)
                try:
                    row['request_snapshot_dict'] = json.loads(row.get('request_snapshot') or '{}')
                except Exception:
                    row['request_snapshot_dict'] = {}
                try:
                    row['response_snapshot_dict'] = json.loads(row.get('response_snapshot') or '{}')
                except Exception:
                    row['response_snapshot_dict'] = {}
                return row
            rows = [dict(r) for r in conn.execute(
                "SELECT sync_log_id, status, request_snapshot, response_snapshot, created_at FROM sync_logs WHERE sync_type = 'registration_group_approval_batch' ORDER BY created_at DESC LIMIT 500"
            ).fetchall()]
        for row in rows:
            try:
                request_snapshot = json.loads(row.get('request_snapshot') or '{}')
            except Exception:
                request_snapshot = {}
            if str(request_snapshot.get('approval_run_id') or '').strip() != normalized_run_id:
                continue
            try:
                response_snapshot = json.loads(row.get('response_snapshot') or '{}')
            except Exception:
                response_snapshot = {}
            row['request_snapshot_dict'] = request_snapshot
            row['response_snapshot_dict'] = response_snapshot
            return row
        return None

    def _find_registration_group_approval_ingress_event(self, approval_run_id: str) -> Optional[Dict[str, Any]]:
        normalized_run_id = str(approval_run_id or '').strip()
        if not normalized_run_id:
            return None
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT event_id, ingress_type, status, payload, result_snapshot, created_at, updated_at, processed_at FROM ingress_events WHERE ingress_type = 'registration_group_approval_decision' ORDER BY created_at DESC LIMIT 500"
            ).fetchall()]
        for row in rows:
            try:
                payload = json.loads(row.get('payload') or '{}')
            except Exception:
                payload = {}
            try:
                result_snapshot = json.loads(row.get('result_snapshot') or '{}')
            except Exception:
                result_snapshot = {}
            if str(payload.get('approval_run_id') or result_snapshot.get('approval_run_id') or '').strip() != normalized_run_id:
                continue
            row['payload_dict'] = payload
            row['result_snapshot_dict'] = result_snapshot
            return row
        return None

    @staticmethod
    def _registration_group_batch_member_snapshot_json(value: Any) -> str:
        if value in (None, ''):
            return ''
        if isinstance(value, str):
            return value.strip()
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            return ''

    @classmethod
    def _registration_group_batch_member_eligibility_fields(
        cls,
        *,
        lead: Optional[Dict[str, Any]] = None,
        eligibility: Optional[Dict[str, Any]] = None,
        source: str = '',
    ) -> Dict[str, Any]:
        lead_dict = dict(lead or {}) if isinstance(lead, dict) else {}
        eligibility_dict = dict(eligibility or {}) if isinstance(eligibility, dict) else {}
        lead_id = str(lead_dict.get('lead_id') or eligibility_dict.get('lead_id') or '').strip()
        crm_snapshot = eligibility_dict.get('crm_snapshot') if isinstance(eligibility_dict.get('crm_snapshot'), dict) else {}
        matched_customer_id = str(
            lead_dict.get('matched_customer_id')
            or eligibility_dict.get('matched_customer_id')
            or crm_snapshot.get('id')
            or ''
        ).strip()
        lead_current_status = str(lead_dict.get('current_status') or eligibility_dict.get('current_status') or '').strip()
        eligible = bool(eligibility_dict.get('eligible'))
        registration_status = ''
        registration_status_label = ''
        if matched_customer_id or eligible:
            registration_status = 'registered'
            registration_status_label = '已注册'
        elif lead_id:
            registered_statuses = {'bind_success', 'group_join_pending', 'group_join_success', 'synced'}
            if str(lead_dict.get('crm_verified_at') or '').strip() and lead_current_status.lower() in registered_statuses:
                registration_status = 'registered'
                registration_status_label = '已注册'
            else:
                registration_status = 'in_progress'
                registration_status_label = '引导注册中'
        snapshot = {
            'lead_id': lead_id or None,
            'lead_current_status': lead_current_status or None,
            'matched_customer_id': matched_customer_id or None,
            'eligible': eligible if eligibility_dict else None,
            'reason_code': eligibility_dict.get('reason_code'),
            'crm_identity_match': eligibility_dict.get('crm_identity_match'),
            'crm_customer_found': eligibility_dict.get('crm_customer_found'),
            'source': source or eligibility_dict.get('source') or None,
        }
        return {
            'lead_id': lead_id,
            'matched_customer_id': matched_customer_id,
            'registration_status_snapshot': registration_status,
            'registration_status_label_snapshot': registration_status_label,
            'eligibility_source': str(source or eligibility_dict.get('source') or '').strip(),
            'eligibility_snapshot': cls._registration_group_batch_member_snapshot_json({k: v for k, v in snapshot.items() if v not in (None, '')}),
        }

    def _replace_registration_group_approval_batch_members(
        self,
        *,
        approval_run_id: str,
        registration_group: str,
        registration_group_name: str,
        approved_at: str,
        selected_candidates: List[Dict[str, Any]],
        group_type: str = 'registration_group',
    ) -> None:
        normalized_run_id = str(approval_run_id or '').strip()
        if not normalized_run_id:
            return
        normalized_group_type = self._normalize_approval_batch_member_group_type(group_type)
        now = utc_now()
        rows: List[tuple[Any, ...]] = []
        for index, item in enumerate(selected_candidates or []):
            if not isinstance(item, dict):
                continue
            phone_raw = str(item.get('phoneRaw') or item.get('phone_raw') or '').strip()
            phone_normalized = str(item.get('phoneNormalized') or item.get('phone_normalized') or '').strip()
            display_name = self._registration_group_batch_member_candidate_display_name(item)
            display_name_source = self._registration_group_batch_member_candidate_name_source(item, display_name)
            display_name_enhanced_at = now if display_name_source else None
            requester_id = str(item.get('requesterId') or item.get('requester_id') or '').strip()
            lead_id = str(item.get('lead_id') or item.get('leadId') or '').strip()
            matched_customer_id = str(item.get('matched_customer_id') or item.get('matchedCustomerId') or '').strip()
            registration_status_snapshot = str(item.get('registration_status_snapshot') or item.get('registration_status') or '').strip()
            registration_status_label_snapshot = str(item.get('registration_status_label_snapshot') or item.get('registration_status_label') or '').strip()
            eligibility_source = str(item.get('eligibility_source') or '').strip()
            eligibility_snapshot = self._registration_group_batch_member_snapshot_json(item.get('eligibility_snapshot'))
            requested_at = str(item.get('requestedAtIso') or item.get('requested_at') or '').strip() or None
            if not any([phone_raw, phone_normalized, display_name, requester_id]):
                continue
            rows.append((
                create_id('rgm'),
                normalized_run_id,
                normalized_group_type,
                str(registration_group or '').strip(),
                str(registration_group_name or '').strip(),
                lead_id,
                matched_customer_id,
                registration_status_snapshot,
                registration_status_label_snapshot,
                eligibility_source,
                eligibility_snapshot,
                requester_id,
                display_name,
                display_name_source,
                display_name_enhanced_at,
                phone_raw,
                phone_normalized,
                requested_at,
                str(approved_at or '').strip(),
                index,
                None,
                '',
                None,
                now,
                now,
            ))
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM registration_group_approval_batch_members WHERE approval_run_id = ?', (normalized_run_id,))
            if rows:
                conn.executemany(
                    "INSERT INTO registration_group_approval_batch_members (member_id, approval_run_id, group_type, registration_group, registration_group_name, lead_id, matched_customer_id, registration_status_snapshot, registration_status_label_snapshot, eligibility_source, eligibility_snapshot, requester_id, display_name, display_name_source, display_name_enhanced_at, wa_phone_raw, wa_phone_normalized, requested_at, approved_at, batch_index, repair_last_attempt_at, repair_last_result, repair_next_attempt_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            conn.commit()

    @staticmethod
    def _registration_group_batch_member_candidate_phone_keys(item: Optional[Dict[str, Any]]) -> set[str]:
        if not isinstance(item, dict):
            return set()
        keys: set[str] = set()
        for key in (
            'phoneRaw',
            'phone_raw',
            'phoneNormalized',
            'phone_normalized',
            'debugLidPhoneRaw',
            'debugContactNumberRaw',
            'phoneNumber',
            'phoneJid',
            'phone',
            'wa_id',
            'requesterId',
            'requester_id',
            'jid',
            'id',
        ):
            digits = re.sub(r'\D+', '', str(item.get(key) or '').strip())
            if not digits:
                continue
            keys.add(digits)
            keys.update(localized_phone_match_keys(phone=digits))
            if len(digits) > 8:
                keys.add(digits[-8:])
            if len(digits) > 10:
                keys.add(digits[-10:])
            try:
                normalized_mobile, normalized_area_code, _ = normalize_phone_identity(
                    mobile=digits,
                    area_code=0,
                    country='',
                )
            except Exception:
                normalized_mobile, normalized_area_code = '', 0
            if normalized_mobile:
                keys.add(str(normalized_mobile))
                if normalized_area_code:
                    keys.add(f'{int(normalized_area_code)}{normalized_mobile}')
                    keys.add(f'+{int(normalized_area_code)}{normalized_mobile}')
            for prefix in sorted(PHONE_PREFIX_COUNTRY_MAP.keys(), key=len, reverse=True):
                if digits.startswith(prefix) and len(digits) > len(prefix):
                    keys.add(digits[len(prefix):])
                    break
        return {item for item in keys if item}

    @classmethod
    def _registration_group_batch_member_candidate_display_name(cls, item: Dict[str, Any]) -> str:
        if not isinstance(item, dict):
            return ''
        for key in ('displayName', 'display_name'):
            value = str(item.get(key) or '').strip()
            if value:
                return value
        for key in (
            'name',
            'fullName',
            'full_name',
            'pushName',
            'push_name',
            'pushname',
            'notify',
            'verifiedName',
            'verified_name',
            'profileName',
            'profile_name',
        ):
            usable = cls._registration_group_batch_member_usable_display_name(item.get(key))
            if usable:
                return usable
        return ''

    @classmethod
    def _merge_registration_group_candidate_metadata(
        cls,
        *,
        selected_candidates: List[Dict[str, Any]],
        expected_requesters: Optional[List[Dict[str, Any]]] = None,
        approval_results: Optional[List[Dict[str, Any]]] = None,
        target_member: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        expected_by_requester: Dict[str, Dict[str, Any]] = {}
        expected_by_phone_key: Dict[str, Optional[Dict[str, Any]]] = {}

        def remember_expected_phone_keys(row: Dict[str, Any]) -> None:
            for phone_key in cls._registration_group_batch_member_candidate_phone_keys(row):
                existing = expected_by_phone_key.get(phone_key)
                if existing is None and phone_key in expected_by_phone_key:
                    continue
                if existing and existing is not row:
                    expected_by_phone_key[phone_key] = None
                    continue
                expected_by_phone_key[phone_key] = row

        for item in expected_requesters or []:
            if not isinstance(item, dict):
                continue
            requester_id = str(item.get('requesterId') or item.get('requester_id') or '').strip()
            expected_row = dict(item)
            if requester_id:
                expected_by_requester[requester_id] = expected_row
            remember_expected_phone_keys(expected_row)
        base_candidates: List[Dict[str, Any]] = [dict(item) for item in (selected_candidates or []) if isinstance(item, dict)]
        if not base_candidates and approval_results:
            for item in approval_results or []:
                if not isinstance(item, dict):
                    continue
                error_value = item.get('error')
                is_success = error_value in (None, '', 0, '0')
                if not is_success:
                    try:
                        is_success = int(error_value) == 409
                    except Exception:
                        is_success = False
                if not is_success:
                    continue
                requester_id = str(item.get('requesterId') or item.get('requester_id') or '').strip()
                if not requester_id:
                    continue
                expected = expected_by_requester.get(requester_id) or {}
                base_candidates.append({
                    'requesterId': requester_id,
                    'displayName': str(expected.get('displayName') or expected.get('display_name') or '').strip(),
                    'phoneRaw': str(expected.get('phoneRaw') or expected.get('phone_raw') or expected.get('debugLidPhoneRaw') or '').strip(),
                    'phoneNormalized': str(expected.get('phoneNormalized') or expected.get('phone_normalized') or expected.get('debugLidPhoneRaw') or '').strip(),
                    'requestedAtIso': str(expected.get('requestedAtIso') or expected.get('requested_at') or '').strip(),
                    'requestedAtUnix': expected.get('requestedAtUnix'),
                    'debugLidPhoneRaw': str(expected.get('debugLidPhoneRaw') or '').strip(),
                })
        merged: List[Dict[str, Any]] = []
        for item in base_candidates:
            row = dict(item)
            if cls._registration_group_batch_member_name_needs_repair(cls._registration_group_batch_member_candidate_display_name(row)):
                for alias in ('name', 'fullName', 'full_name', 'pushName', 'push_name', 'pushname', 'notify', 'verifiedName', 'verified_name', 'profileName', 'profile_name'):
                    usable_alias = cls._registration_group_batch_member_usable_display_name(row.get(alias))
                    if usable_alias:
                        row['displayName'] = usable_alias
                        break
            requester_id = str(row.get('requesterId') or row.get('requester_id') or '').strip()
            expected = expected_by_requester.get(requester_id) or {}
            if not expected:
                for phone_key in cls._registration_group_batch_member_candidate_phone_keys(row):
                    candidate = expected_by_phone_key.get(phone_key)
                    if candidate:
                        expected = candidate
                        break
            for source_key, target_keys in (
                ('displayName', ('displayName', 'display_name')),
                ('display_name', ('displayName', 'display_name')),
                ('name', ('displayName', 'display_name')),
                ('fullName', ('displayName', 'display_name')),
                ('full_name', ('displayName', 'display_name')),
                ('pushName', ('displayName', 'display_name')),
                ('push_name', ('displayName', 'display_name')),
                ('pushname', ('displayName', 'display_name')),
                ('notify', ('displayName', 'display_name')),
                ('verifiedName', ('displayName', 'display_name')),
                ('verified_name', ('displayName', 'display_name')),
                ('profileName', ('displayName', 'display_name')),
                ('profile_name', ('displayName', 'display_name')),
                ('phoneRaw', ('phoneRaw', 'phone_raw')),
                ('phone_raw', ('phoneRaw', 'phone_raw')),
                ('phoneNormalized', ('phoneNormalized', 'phone_normalized')),
                ('phone_normalized', ('phoneNormalized', 'phone_normalized')),
                ('requestedAtIso', ('requestedAtIso', 'requested_at')),
                ('requested_at', ('requestedAtIso', 'requested_at')),
                ('debugLidPhoneRaw', ('debugLidPhoneRaw',)),
            ):
                source_value = str(expected.get(source_key) or '').strip()
                if not source_value:
                    continue
                for target_key in target_keys:
                    if not str(row.get(target_key) or '').strip():
                        row[target_key] = source_value
            if len(base_candidates) == 1 and target_member:
                if not str(row.get('displayName') or row.get('display_name') or '').strip():
                    fallback_name = str((target_member or {}).get('name') or '').strip()
                    if fallback_name:
                        row['displayName'] = fallback_name
                if not str(row.get('phoneRaw') or row.get('phone_raw') or '').strip():
                    fallback_phone_raw = str((target_member or {}).get('phone_raw') or '').strip()
                    if fallback_phone_raw:
                        row['phoneRaw'] = fallback_phone_raw
                if not str(row.get('phoneNormalized') or row.get('phone_normalized') or '').strip():
                    fallback_phone_normalized = str((target_member or {}).get('phone_normalized') or '').strip()
                    if fallback_phone_normalized:
                        row['phoneNormalized'] = fallback_phone_normalized
            merged.append(row)
        return merged

    @staticmethod
    def _registration_group_batch_member_name_needs_repair(display_name: Any) -> bool:
        name = str(display_name or '').strip()
        if not name:
            return True
        return re.fullmatch(r'[.。·•~～—_\-\s]+', name) is not None

    @classmethod
    def _registration_group_batch_member_normalize_name_source(cls, source: Any) -> str:
        value = str(source or '').strip().lower().replace('-', '_').replace(' ', '_')
        aliases = {
            'baileys': 'baileys_runtime',
            'baileys_poc': 'baileys_runtime',
            'webjs': 'webjs_runtime',
            'web_js': 'webjs_runtime',
            'history': 'historical_batch_member',
            'historical_batch': 'historical_batch_member',
            'historical_batch_members': 'historical_batch_member',
            'batch_history': 'historical_batch_member',
            'crm': 'crm_projection',
            'customer_projection': 'crm_projection',
            'lead': 'lead_history',
            'lead_snapshot': 'lead_history',
            'selected_candidate': 'approval_snapshot',
            'expected_requester': 'approval_snapshot',
        }
        normalized = aliases.get(value, value)
        allowed = {
            'approval_snapshot',
            'baileys_runtime',
            'webjs_runtime',
            'historical_batch_member',
            'crm_projection',
            'live_crm',
            'lead_history',
            'ingress_event',
        }
        return normalized if normalized in allowed else ''

    @classmethod
    def _registration_group_batch_member_name_source_label(cls, source: Any) -> str:
        normalized = cls._registration_group_batch_member_normalize_name_source(source)
        labels = {
            'approval_snapshot': '审批快照',
            'baileys_runtime': 'Baileys',
            'webjs_runtime': 'WebJS',
            'historical_batch_member': '历史留存',
            'crm_projection': 'CRM',
            'live_crm': 'CRM',
            'lead_history': '历史线索',
            'ingress_event': '审批请求',
        }
        return labels.get(normalized, '')

    @classmethod
    def _registration_group_batch_member_usable_display_name(cls, value: Any) -> str:
        text = re.sub(r'\s+', ' ', str(value or '').strip())
        if cls._registration_group_batch_member_name_needs_repair(text):
            return ''
        if len(text) > 80:
            return ''
        lowered = text.lower()
        if '@g.us' in lowered or '@lid' in lowered or '@c.us' in lowered:
            return ''
        digits = re.sub(r'\D+', '', text)
        if digits and len(digits) >= 6 and len(digits) >= len(re.sub(r'\s+', '', text)) - 2:
            return ''
        return text

    @classmethod
    def _registration_group_batch_member_candidate_name_source(cls, item: Dict[str, Any], display_name: str) -> str:
        usable_name = cls._registration_group_batch_member_usable_display_name(display_name)
        if not usable_name:
            return ''
        for key in ('displayNameSource', 'display_name_source', 'nameSource', 'name_source', 'source'):
            normalized = cls._registration_group_batch_member_normalize_name_source((item or {}).get(key))
            if normalized:
                return normalized
        return 'approval_snapshot'

    @classmethod
    def _registration_group_batch_member_extract_crm_display_name(cls, row: Optional[Dict[str, Any]]) -> str:
        if not isinstance(row, dict):
            return ''
        for key in (
            'whatsappName',
            'whatsapp_name',
            'waName',
            'wa_name',
            'displayName',
            'display_name',
            'nickname',
            'nick_name',
            'name',
            'fullName',
            'full_name',
            'customerName',
            'customer_name',
            'contactName',
            'contact_name',
            'profileName',
            'profile_name',
        ):
            usable = cls._registration_group_batch_member_usable_display_name(row.get(key))
            if usable:
                return usable
        return ''

    @staticmethod
    def _registration_group_batch_member_repair_cooldown_seconds() -> int:
        raw_value = str(os.getenv('REGISTRATION_GROUP_BATCH_MEMBER_REPAIR_COOLDOWN_SECONDS') or '').strip()
        try:
            parsed = int(raw_value or 21600)
        except Exception:
            parsed = 21600
        return max(parsed, 0)

    @classmethod
    def _registration_group_batch_member_repair_next_attempt_at(cls, attempted_at: str) -> Optional[str]:
        cooldown_seconds = cls._registration_group_batch_member_repair_cooldown_seconds()
        if cooldown_seconds <= 0:
            return None
        try:
            return (parse_iso_datetime(attempted_at) + timedelta(seconds=cooldown_seconds)).isoformat()
        except Exception:
            return None

    @classmethod
    def _registration_group_batch_member_is_in_repair_cooldown(cls, row: Dict[str, Any], *, now: Optional[datetime] = None) -> bool:
        if not isinstance(row, dict):
            return False
        next_attempt_at = str(row.get('repair_next_attempt_at') or '').strip()
        if not next_attempt_at:
            return False
        try:
            next_attempt_dt = parse_iso_datetime(next_attempt_at)
        except Exception:
            return False
        current_dt = now or datetime.now(timezone.utc)
        if current_dt.tzinfo is None:
            current_dt = current_dt.replace(tzinfo=timezone.utc)
        else:
            current_dt = current_dt.astimezone(timezone.utc)
        return next_attempt_dt > current_dt

    @classmethod
    def _registration_group_batch_member_should_attempt_repair(cls, row: Dict[str, Any], *, now: Optional[datetime] = None, force: bool = False) -> bool:
        if not isinstance(row, dict):
            return False
        requester_id = str(row.get('requester_id') or '').strip()
        display_name = str(row.get('display_name') or '').strip()
        phone_raw = str(row.get('wa_phone_raw') or '').strip()
        phone_normalized = str(row.get('wa_phone_normalized') or '').strip()
        has_phone_hint = bool(phone_raw or phone_normalized)
        if not requester_id and not has_phone_hint:
            return False
        name_needs_repair = cls._registration_group_batch_member_name_needs_repair(display_name)
        phone_needs_repair = (not phone_raw or '*' in phone_raw or not phone_normalized or '*' in phone_normalized)
        if not name_needs_repair and not phone_needs_repair:
            return False
        if force:
            return True
        return not cls._registration_group_batch_member_is_in_repair_cooldown(row, now=now)

    @staticmethod
    def _registration_group_batch_member_normalize_phone_from_resolver(phone_value: Any) -> str:
        text = str(phone_value or '').strip()
        if not text:
            return ''
        if text.endswith('@c.us'):
            text = text[:-5]
        digits = re.sub(r'\D+', '', text)
        if not digits:
            return ''
        return f'+{digits}'

    def _list_registration_group_batch_member_runtime_candidates(self, *, registration_group: str, registration_group_name: str) -> List[Dict[str, Any]]:
        normalized_group = str(registration_group or '').strip()
        normalized_group_name = str(registration_group_name or '').strip()
        normalized_targets = {
            item.strip().lower()
            for item in (normalized_group, normalized_group_name)
            if item and item.strip()
        }
        candidates: List[Dict[str, Any]] = []
        seen: set[str] = set()
        account_state = self.list_whatsapp_approval_accounts()
        for row in account_state.get('rows') or []:
            if str(row.get('responsible_type') or '').strip() != 'registration_group':
                continue
            bindings = row.get('group_binding_runtimes') or row.get('group_link_bindings') or []
            if not isinstance(bindings, list):
                continue
            matched_binding: Dict[str, Any] = {}
            for binding in bindings:
                if not isinstance(binding, dict):
                    continue
                runtime_group_id = self._whatsapp_binding_runtime_group_id(binding)
                binding_targets = {
                    str(binding.get(key) or '').strip().lower()
                    for key in (
                        'registration_group',
                        'group_id',
                        'link',
                        'target_group',
                        'group_name',
                        'runtime_probe_group_id',
                        'runtime_probe_group_name',
                        'previous_verified_group_id',
                        'previous_verified_group_name',
                    )
                    if str(binding.get(key) or '').strip()
                }
                if runtime_group_id:
                    binding_targets.add(runtime_group_id.strip().lower())
                if normalized_targets.intersection(binding_targets):
                    matched_binding = dict(binding)
                    break
            if not matched_binding:
                continue
            runtime_state = row.get('runtime_state') or {}
            provider_decision = self._resolve_wa_provider_decision(
                account=row,
                binding=matched_binding,
                runtime_state=runtime_state if isinstance(runtime_state, dict) else {},
                responsible_type='registration_group',
            )
            account_key = str(row.get('account_key') or '').strip()
            if str(provider_decision.get('provider_name') or '').strip().lower() == 'baileys' and account_key:
                baileys_account_id = self._resolve_baileys_runtime_value(
                    matched_binding,
                    row,
                    runtime_state if isinstance(runtime_state, dict) else {},
                    keys=['baileys_account_id', 'provider_account_id', 'account_id'],
                ) or _default_baileys_account_id_for_whatsapp_account(account_key)
                base_url = self._resolve_baileys_runtime_base_url(
                    account=row,
                    binding=matched_binding,
                    runtime_state=runtime_state if isinstance(runtime_state, dict) else {},
                )
                dedupe_key = f'baileys::{account_key}::{baileys_account_id}::{base_url}'
                if base_url and dedupe_key not in seen:
                    seen.add(dedupe_key)
                    candidates.append({
                        'provider': 'baileys',
                        'account_key': account_key,
                        'account_name': str(row.get('account_name') or '').strip() or account_key,
                        'baileys_account_id': baileys_account_id,
                        'base_url': base_url,
                        'token': self._resolve_baileys_runtime_token(
                            account=row,
                            binding=matched_binding,
                            runtime_state=runtime_state if isinstance(runtime_state, dict) else {},
                        ),
                        'binding': matched_binding,
                        'runtime_state': dict(runtime_state if isinstance(runtime_state, dict) else {}),
                    })
                continue
            auth_path = str(runtime_state.get('auth_path') or '').strip()
            client_id = str(runtime_state.get('client_id') or '').strip()
            if not auth_path or not client_id or not account_key:
                continue
            dedupe_key = f'{account_key}::{client_id}::{auth_path}'
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            candidates.append({
                'account_key': account_key,
                'account_name': str(row.get('account_name') or '').strip() or account_key,
                'auth_path': auth_path,
                'client_id': client_id,
            })
        return candidates

    @staticmethod
    def _registration_group_batch_member_first_text(payload: Dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = str((payload or {}).get(key) or '').strip()
            if value:
                return value
        return ''

    def _resolve_registration_group_batch_member_contacts_via_baileys_runtime(
        self,
        *,
        runtime: Dict[str, Any],
        registration_group: str,
        requester_ids: List[str],
    ) -> List[Dict[str, Any]]:
        normalized_requester_ids = [str(item or '').strip() for item in requester_ids if str(item or '').strip()]
        normalized_group = str(registration_group or '').strip()
        if not normalized_group or not normalized_requester_ids:
            return []
        account_key = str((runtime or {}).get('account_key') or '').strip()
        baileys_account_id = str((runtime or {}).get('baileys_account_id') or '').strip() or _default_baileys_account_id_for_whatsapp_account(account_key)
        binding = dict((runtime or {}).get('binding') or {})
        runtime_state = dict((runtime or {}).get('runtime_state') or {})
        if baileys_account_id:
            binding.setdefault('baileys_account_id', baileys_account_id)
            binding.setdefault('provider_account_id', baileys_account_id)
            binding.setdefault('account_id', baileys_account_id)
        try:
            executor = self._build_runtime_baileys_registration_group_executor(
                account={
                    'account_key': account_key,
                    'baileys_account_id': baileys_account_id,
                    'provider_account_id': baileys_account_id,
                    'account_id': baileys_account_id,
                    'baileys_base_url': str((runtime or {}).get('base_url') or '').strip(),
                    'provider_base_url': str((runtime or {}).get('base_url') or '').strip(),
                    'baileys_token': str((runtime or {}).get('token') or '').strip(),
                    'provider_token': str((runtime or {}).get('token') or '').strip(),
                },
                binding=binding,
                runtime_state=runtime_state,
            )
            if not getattr(executor, 'base_url', '') or not hasattr(executor, 'group_member_lookup'):
                return []
            lookup_group_id = self._whatsapp_binding_runtime_group_id(binding) or normalized_group
            group_link = str(binding.get('link') or '').strip()
            if not group_link and _looks_like_whatsapp_invite_link(normalized_group):
                group_link = normalized_group
            group_name = str(binding.get('group_name') or binding.get('target_group_label') or '').strip()
            payload = {
                'accountId': baileys_account_id or account_key,
                'account_id': baileys_account_id or account_key,
                'baileys_account_id': baileys_account_id or account_key,
                'groupId': lookup_group_id,
                'group_id': lookup_group_id,
                'requesterIds': normalized_requester_ids,
                'requester_ids': normalized_requester_ids,
            }
            if group_link:
                payload['groupLink'] = group_link
                payload['group_link'] = group_link
            if group_name:
                payload['groupName'] = group_name
                payload['group_name'] = group_name
            lookup = executor.group_member_lookup(payload)
        except Exception:
            return []
        if not isinstance(lookup, dict):
            return []
        raw_members: List[Dict[str, Any]] = []
        for value in (lookup.get('members'), lookup.get('requesters')):
            if isinstance(value, list):
                raw_members.extend([dict(item) for item in value if isinstance(item, dict)])
        for match in lookup.get('matches') or []:
            if not isinstance(match, dict):
                continue
            requester = match.get('requester') if isinstance(match.get('requester'), dict) else {}
            if requester:
                raw_members.append(dict(requester))
        wanted = set(normalized_requester_ids)
        resolved: List[Dict[str, Any]] = []
        seen_requesters: set[str] = set()
        for member in raw_members:
            requester_id = self._registration_group_batch_member_first_text(
                member,
                'requesterId',
                'requester_id',
                'jid',
                'id',
                'participant',
                'lid',
            )
            if not requester_id or requester_id not in wanted or requester_id in seen_requesters:
                continue
            seen_requesters.add(requester_id)
            display_name = self._registration_group_batch_member_first_text(
                member,
                'displayName',
                'display_name',
                'notify',
                'name',
                'pushName',
                'pushname',
                'verifiedName',
            )
            phone_value = self._registration_group_batch_member_first_text(
                member,
                'phoneRaw',
                'phone_raw',
                'phoneNormalized',
                'phone_normalized',
                'debugLidPhoneRaw',
                'debugContactNumberRaw',
                'phoneNumber',
                'phoneJid',
                'phone',
                'wa_id',
            )
            resolved.append({
                'requester_id': requester_id,
                'display_name': display_name,
                'display_name_source': 'baileys_runtime',
                'phone_from_lid': phone_value,
                'phone_from_contact_id': phone_value,
            })
        return resolved

    def _resolve_registration_group_batch_member_contacts_via_runtime(self, *, auth_path: str, client_id: str, requester_ids: List[str]) -> List[Dict[str, Any]]:
        normalized_auth_path = Path(str(auth_path or '').strip()).expanduser().resolve()
        normalized_client_id = str(client_id or '').strip()
        normalized_requester_ids = [str(item or '').strip() for item in requester_ids if str(item or '').strip()]
        if not normalized_auth_path.exists() or not normalized_client_id or not normalized_requester_ids:
            return []
        resolver_script = WHATSAPP_APPROVAL_WORKER_ROOT / 'tmp' / 'resolve_batch_requester_names.js'
        if not resolver_script.exists():
            return []
        with tempfile.TemporaryDirectory(prefix='wa-name-audit-', dir=str(WHATSAPP_APPROVAL_WORKER_ROOT / 'tmp')) as tmp_dir:
            tmp_auth_path = Path(tmp_dir) / normalized_auth_path.name
            shutil.copytree(
                normalized_auth_path,
                tmp_auth_path,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('Singleton*', 'RunningChromeVersion'),
            )
            completed = subprocess.run(
                ['node', str(resolver_script), str(tmp_auth_path), normalized_client_id, *normalized_requester_ids],
                cwd=str(WHATSAPP_APPROVAL_WORKER_ROOT),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        if completed.returncode != 0:
            return []
        try:
            payload = json.loads(str(completed.stdout or '{}'))
        except Exception:
            return []
        if not isinstance(payload, dict) or not payload.get('ok'):
            return []
        results = payload.get('results') or []
        normalized_results: List[Dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            if str(row.get('display_name') or row.get('displayName') or '').strip():
                row.setdefault('display_name_source', 'webjs_runtime')
            normalized_results.append(row)
        return normalized_results

    def _resolve_registration_group_batch_member_names_from_history(
        self,
        conn: sqlite3.Connection,
        *,
        rows: List[Dict[str, Any]],
        requester_ids: set[str],
    ) -> Dict[str, Dict[str, Any]]:
        targets: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            requester_id = str(row.get('requester_id') or '').strip()
            if not requester_id or requester_id not in requester_ids:
                continue
            if not self._registration_group_batch_member_name_needs_repair(row.get('display_name')):
                continue
            phone_keys = set()
            phone_keys.update(self._registration_membership_phone_keys(mobile=row.get('wa_phone_raw')))
            phone_keys.update(self._registration_membership_phone_keys(mobile=row.get('wa_phone_normalized')))
            targets[requester_id] = {
                'member_id': str(row.get('member_id') or '').strip(),
                'phone_keys': phone_keys,
            }
        if not targets:
            return {}
        history_rows = [
            dict(item)
            for item in conn.execute(
                """
                SELECT member_id, requester_id, display_name, display_name_source,
                       wa_phone_raw, wa_phone_normalized, approved_at, created_at
                FROM registration_group_approval_batch_members
                WHERE COALESCE(display_name, '') != ''
                ORDER BY approved_at DESC, created_at DESC
                LIMIT 5000
                """
            ).fetchall()
        ]
        resolved: Dict[str, Dict[str, Any]] = {}
        for history in history_rows:
            display_name = self._registration_group_batch_member_usable_display_name(history.get('display_name'))
            if not display_name:
                continue
            history_requester_id = str(history.get('requester_id') or '').strip()
            history_member_id = str(history.get('member_id') or '').strip()
            history_phone_keys = set()
            history_phone_keys.update(self._registration_membership_phone_keys(mobile=history.get('wa_phone_raw')))
            history_phone_keys.update(self._registration_membership_phone_keys(mobile=history.get('wa_phone_normalized')))
            for requester_id, target in targets.items():
                if requester_id in resolved:
                    continue
                if history_member_id and history_member_id == target.get('member_id'):
                    continue
                same_requester = bool(history_requester_id and history_requester_id == requester_id)
                same_phone = bool(history_phone_keys and target.get('phone_keys') and history_phone_keys.intersection(target.get('phone_keys') or set()))
                if same_requester or same_phone:
                    resolved[requester_id] = {
                        'display_name': display_name,
                        'source': 'historical_batch_member',
                    }
            if len(resolved) >= len(targets):
                break
        return resolved

    @classmethod
    def _registration_group_batch_member_ingress_named_candidates(
        cls,
        *sources: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()

        def candidate_name(row: Dict[str, Any]) -> str:
            for key in ('displayName', 'display_name', 'name', 'pushName', 'notify', 'verifiedName'):
                usable = cls._registration_group_batch_member_usable_display_name(row.get(key))
                if usable:
                    return usable
            return ''

        def add_candidate(row: Any) -> None:
            if not isinstance(row, dict):
                return
            name = candidate_name(row)
            if not name:
                return
            requester_id = str(row.get('requesterId') or row.get('requester_id') or row.get('id') or row.get('jid') or '').strip()
            phone_keys = cls._registration_group_batch_member_candidate_phone_keys(row)
            if not requester_id and not phone_keys:
                return
            dedupe_key = (requester_id, ','.join(sorted(phone_keys)), name)
            if dedupe_key in seen:
                return
            seen.add(dedupe_key)
            candidate = dict(row)
            candidate.setdefault('displayName', name)
            candidate.setdefault('display_name_source', 'ingress_event')
            candidates.append(candidate)

        def add_list(value: Any) -> None:
            if not isinstance(value, list):
                return
            for item in value:
                add_candidate(item)

        def visit(container: Any, depth: int = 0) -> None:
            if not isinstance(container, dict) or depth > 4:
                return
            for key in (
                'expected_requesters',
                'expectedRequesters',
                'requesters',
                'pending_requesters',
                'pendingRequesters',
                'selected_candidates',
                'selectedCandidates',
                'approved_requesters',
                'approvedRequesters',
                'approval_results',
                'approvalResults',
                'members',
                'participants',
            ):
                add_list(container.get(key))
            for key in (
                'payload',
                'raw_result',
                'rawResult',
                'result',
                'result_snapshot',
                'current_group_state',
                'currentGroupState',
                'expected_group_state',
                'expectedGroupState',
                'latest_group_state_before_approve',
                'latestGroupStateBeforeApprove',
                'group_state',
                'groupState',
                'latest_probe',
                'latestProbe',
                'current_truth',
                'currentTruth',
                'poc_snapshot',
                'pocSnapshot',
                'snapshot',
                'probe',
            ):
                visit(container.get(key), depth + 1)

        for source in sources:
            visit(source)
        return candidates

    def _resolve_registration_group_batch_member_names_from_ingress_events(
        self,
        *,
        rows: List[Dict[str, Any]],
        requester_ids: set[str],
    ) -> Dict[str, Dict[str, Any]]:
        targets_by_run: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            requester_id = str(row.get('requester_id') or '').strip()
            approval_run_id = str(row.get('approval_run_id') or '').strip()
            if not requester_id or requester_id not in requester_ids or not approval_run_id:
                continue
            if not self._registration_group_batch_member_name_needs_repair(row.get('display_name')):
                continue
            target = dict(row)
            target['requesterId'] = requester_id
            target['phoneRaw'] = str(row.get('wa_phone_raw') or '').strip()
            target['phoneNormalized'] = str(row.get('wa_phone_normalized') or '').strip()
            target['phone_keys'] = self._registration_group_batch_member_candidate_phone_keys(target)
            targets_by_run.setdefault(approval_run_id, []).append(target)
        if not targets_by_run:
            return {}

        resolved: Dict[str, Dict[str, Any]] = {}
        for approval_run_id, target_rows in targets_by_run.items():
            ingress_event = self._find_registration_group_approval_ingress_event(approval_run_id)
            if not ingress_event:
                continue
            candidates = self._registration_group_batch_member_ingress_named_candidates(
                ingress_event.get('payload_dict') if isinstance(ingress_event, dict) else None,
                ingress_event.get('result_snapshot_dict') if isinstance(ingress_event, dict) else None,
            )
            if not candidates:
                continue
            candidates_by_requester: Dict[str, Dict[str, Any]] = {}
            candidates_by_phone_key: Dict[str, Optional[Dict[str, Any]]] = {}
            for candidate in candidates:
                requester_id = str(
                    candidate.get('requesterId')
                    or candidate.get('requester_id')
                    or candidate.get('id')
                    or candidate.get('jid')
                    or ''
                ).strip()
                if requester_id:
                    candidates_by_requester.setdefault(requester_id, candidate)
                for phone_key in self._registration_group_batch_member_candidate_phone_keys(candidate):
                    existing = candidates_by_phone_key.get(phone_key)
                    if existing is None and phone_key in candidates_by_phone_key:
                        continue
                    if existing and existing is not candidate:
                        candidates_by_phone_key[phone_key] = None
                        continue
                    candidates_by_phone_key[phone_key] = candidate
            for target in target_rows:
                requester_id = str(target.get('requester_id') or '').strip()
                if not requester_id or requester_id in resolved:
                    continue
                candidate = candidates_by_requester.get(requester_id)
                if candidate is None:
                    for phone_key in target.get('phone_keys') or set():
                        matched = candidates_by_phone_key.get(phone_key)
                        if matched:
                            candidate = matched
                            break
                if not candidate:
                    continue
                display_name = self._registration_group_batch_member_usable_display_name(
                    candidate.get('displayName') or candidate.get('display_name') or candidate.get('name')
                )
                if not display_name:
                    continue
                resolved_phone = self._registration_group_batch_member_normalize_phone_from_resolver(
                    candidate.get('phoneNormalized')
                    or candidate.get('phone_normalized')
                    or candidate.get('phoneRaw')
                    or candidate.get('phone_raw')
                    or candidate.get('debugLidPhoneRaw')
                    or candidate.get('debugContactNumberRaw')
                    or ''
                )
                resolved[requester_id] = {
                    'display_name': display_name,
                    'phone': resolved_phone,
                    'source': 'ingress_event',
                }
        return resolved

    def _resolve_registration_group_batch_member_names_from_crm(
        self,
        conn: sqlite3.Connection,
        *,
        rows: List[Dict[str, Any]],
        requester_ids: set[str],
        allow_live_crm: bool = True,
        allow_member_id_keys: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        resolved: Dict[str, Dict[str, Any]] = {}

        def lookup_local_name(table_name: str, *, mobile: str, area_code: int) -> Tuple[str, str, Optional[Dict[str, Any]]]:
            if not mobile:
                return '', '', None
            queries: List[Tuple[str, tuple[Any, ...]]] = []
            if area_code:
                queries.append((f'SELECT * FROM {table_name} WHERE mobile = ? AND area_code = ? ORDER BY updated_at DESC LIMIT 1', (mobile, int(area_code))))
            queries.append((f'SELECT * FROM {table_name} WHERE mobile = ? ORDER BY updated_at DESC LIMIT 1', (mobile,)))
            for sql, params in queries:
                try:
                    row = conn.execute(sql, params).fetchone()
                except sqlite3.OperationalError:
                    row = None
                if row is None:
                    continue
                row_dict = dict(row)
                name = self._registration_group_batch_member_extract_crm_display_name(row_dict)
                if name:
                    source = 'crm_projection' if table_name == 'customer_projection' else 'lead_history'
                    return name, source, row_dict
                return '', '', row_dict
            return '', '', None

        for row in rows:
            requester_id = str(row.get('requester_id') or '').strip()
            member_id = str(row.get('member_id') or '').strip()
            if requester_id:
                if requester_id not in requester_ids or requester_id in resolved:
                    continue
                resolution_key = requester_id
            elif allow_member_id_keys and member_id:
                resolution_key = f'member:{member_id}'
                if resolution_key in resolved:
                    continue
            else:
                continue
            if not self._registration_group_batch_member_name_needs_repair(row.get('display_name')):
                continue
            phone_value = str(row.get('wa_phone_normalized') or row.get('wa_phone_raw') or '').strip()
            if not phone_value or '*' in phone_value:
                continue
            try:
                mobile_body, area_code, _ = normalize_phone_identity(mobile=phone_value, area_code=0, country='')
            except Exception:
                mobile_body, area_code = '', 0
            digits_only = re.sub(r'\D+', '', phone_value)
            mobile_candidates = []
            for value in (mobile_body, digits_only, phone_value):
                normalized = str(value or '').strip()
                if normalized and normalized not in mobile_candidates:
                    mobile_candidates.append(normalized)

            crm_context_rows: List[Dict[str, Any]] = []
            for mobile_candidate in mobile_candidates:
                for table_name in ('customer_projection', 'leads'):
                    name, source, context_row = lookup_local_name(table_name, mobile=mobile_candidate, area_code=int(area_code or 0))
                    if context_row:
                        crm_context_rows.append(context_row)
                    if name and source:
                        resolved[resolution_key] = {'display_name': name, 'source': source}
                        break
                if resolution_key in resolved:
                    break
            if resolution_key in resolved or not allow_live_crm or self.crm_adapter is None:
                continue
            live_candidates: List[Tuple[Optional[str], Optional[str]]] = []
            seen_live_keys: set[Tuple[str, str]] = set()

            def add_live_candidate(yw_id_value: Any, mobile_value: Any) -> None:
                yw_key = str(yw_id_value or '').strip()
                mobile_key = str(mobile_value or '').strip()
                if not yw_key and not mobile_key:
                    return
                key = (yw_key, mobile_key)
                if key in seen_live_keys:
                    return
                seen_live_keys.add(key)
                live_candidates.append((yw_key or None, mobile_key or None))

            for context_row in crm_context_rows:
                add_live_candidate(context_row.get('yw_id'), context_row.get('mobile'))
            for mobile_candidate in mobile_candidates:
                add_live_candidate(None, mobile_candidate)
            for yw_id_candidate, mobile_candidate in live_candidates[:6]:
                try:
                    crm_row = self.crm_adapter.find_customer(yw_id=yw_id_candidate, mobile=mobile_candidate)
                except Exception:
                    crm_row = None
                name = self._registration_group_batch_member_extract_crm_display_name(dict(crm_row or {}))
                if name:
                    resolved[resolution_key] = {'display_name': name, 'source': 'live_crm'}
                    break
        return resolved

    def _repair_registration_group_batch_member_rows(
        self,
        *,
        rows: List[Dict[str, Any]],
        registration_group: str,
        registration_group_name: str,
        force: bool = False,
    ) -> Dict[str, Any]:
        candidate_rows = [dict(item) for item in (rows or []) if isinstance(item, dict)]
        repair_now = datetime.now(timezone.utc)
        repair_rows = [item for item in candidate_rows if self._registration_group_batch_member_should_attempt_repair(item, now=repair_now, force=force)]
        skipped_cooldown = sum(
            1
            for item in candidate_rows
            if self._registration_group_batch_member_name_needs_repair(item.get('display_name'))
            and (str(item.get('requester_id') or '').strip() or str(item.get('wa_phone_raw') or item.get('wa_phone_normalized') or '').strip())
            and not self._registration_group_batch_member_should_attempt_repair(item, now=repair_now, force=force)
        )
        if not repair_rows:
            return {'checked': len(candidate_rows), 'candidates': 0, 'updated': 0, 'unresolved': 0, 'skipped_cooldown': skipped_cooldown, 'applied': []}
        runtime_candidates = self._list_registration_group_batch_member_runtime_candidates(
            registration_group=registration_group,
            registration_group_name=registration_group_name,
        )
        ingress_phone_by_run: Dict[str, str] = {}
        for item in repair_rows:
            approval_run_id = str(item.get('approval_run_id') or '').strip()
            if not approval_run_id or approval_run_id in ingress_phone_by_run:
                continue
            ingress_event = self._find_registration_group_approval_ingress_event(approval_run_id)
            result_snapshot = (ingress_event or {}).get('result_snapshot_dict') or {}
            target_member = result_snapshot.get('target_member') if isinstance(result_snapshot, dict) else {}
            normalized_phone = self._registration_group_batch_member_normalize_phone_from_resolver(
                (target_member or {}).get('phone_normalized') or (target_member or {}).get('phone_raw') or ''
            )
            if normalized_phone:
                ingress_phone_by_run[approval_run_id] = normalized_phone
        requester_ids = [str(item.get('requester_id') or '').strip() for item in repair_rows if str(item.get('requester_id') or '').strip()]
        resolved_by_requester: Dict[str, Dict[str, Any]] = {}
        resolved_by_member_id: Dict[str, Dict[str, Any]] = {}
        unresolved_requesters = set(requester_ids)

        def remember_resolution(requester_id: str, *, display_name: str = '', phone: str = '', source: str = '') -> None:
            normalized_requester = str(requester_id or '').strip()
            if not normalized_requester:
                return
            current = dict(resolved_by_requester.get(normalized_requester) or {})
            normalized_phone = str(phone or '').strip()
            if normalized_phone and not str(current.get('phone') or '').strip():
                current['phone'] = normalized_phone
            usable_name = self._registration_group_batch_member_usable_display_name(display_name)
            if usable_name and not self._registration_group_batch_member_usable_display_name(current.get('display_name')):
                current['display_name'] = usable_name
                current['source'] = self._registration_group_batch_member_normalize_name_source(source)
                unresolved_requesters.discard(normalized_requester)
            resolved_by_requester[normalized_requester] = current

        def remember_member_resolution(member_id: str, *, display_name: str = '', phone: str = '', source: str = '') -> None:
            normalized_member_id = str(member_id or '').strip()
            if not normalized_member_id:
                return
            current = dict(resolved_by_member_id.get(normalized_member_id) or {})
            normalized_phone = str(phone or '').strip()
            if normalized_phone and not str(current.get('phone') or '').strip():
                current['phone'] = normalized_phone
            usable_name = self._registration_group_batch_member_usable_display_name(display_name)
            if usable_name and not self._registration_group_batch_member_usable_display_name(current.get('display_name')):
                current['display_name'] = usable_name
                current['source'] = self._registration_group_batch_member_normalize_name_source(source)
            resolved_by_member_id[normalized_member_id] = current

        if unresolved_requesters:
            ingress_resolutions = self._resolve_registration_group_batch_member_names_from_ingress_events(
                rows=repair_rows,
                requester_ids=set(unresolved_requesters),
            )
            for requester_id, resolved in ingress_resolutions.items():
                remember_resolution(
                    requester_id,
                    display_name=str(resolved.get('display_name') or '').strip(),
                    phone=str(resolved.get('phone') or '').strip(),
                    source=str(resolved.get('source') or 'ingress_event'),
                )

        for runtime in runtime_candidates:
            if not unresolved_requesters:
                break
            runtime_source = 'baileys_runtime' if str(runtime.get('provider') or '').strip().lower() == 'baileys' else 'webjs_runtime'
            if str(runtime.get('provider') or '').strip().lower() == 'baileys':
                resolved_rows = self._resolve_registration_group_batch_member_contacts_via_baileys_runtime(
                    runtime=runtime,
                    registration_group=registration_group,
                    requester_ids=sorted(unresolved_requesters),
                )
            else:
                resolved_rows = self._resolve_registration_group_batch_member_contacts_via_runtime(
                    auth_path=str(runtime.get('auth_path') or '').strip(),
                    client_id=str(runtime.get('client_id') or '').strip(),
                    requester_ids=sorted(unresolved_requesters),
                )
            for resolved in resolved_rows:
                requester_id = str(resolved.get('requester_id') or '').strip()
                if not requester_id:
                    continue
                resolved_name = str(resolved.get('display_name') or '').strip()
                resolved_phone = self._registration_group_batch_member_normalize_phone_from_resolver(
                    resolved.get('phone_from_contact_id') or resolved.get('phone_from_lid') or ''
                )
                resolved_source = (
                    resolved.get('display_name_source')
                    or resolved.get('displayNameSource')
                    or resolved.get('name_source')
                    or resolved.get('source')
                    or runtime_source
                )
                remember_resolution(requester_id, display_name=resolved_name, phone=resolved_phone, source=str(resolved_source or ''))
        if unresolved_requesters:
            with self.db.connect() as conn:
                history_resolutions = self._resolve_registration_group_batch_member_names_from_history(
                    conn,
                    rows=repair_rows,
                    requester_ids=set(unresolved_requesters),
                )
                for requester_id, resolved in history_resolutions.items():
                    remember_resolution(
                        requester_id,
                        display_name=str(resolved.get('display_name') or '').strip(),
                        source=str(resolved.get('source') or 'historical_batch_member'),
                    )
                if unresolved_requesters:
                    crm_resolutions = self._resolve_registration_group_batch_member_names_from_crm(
                        conn,
                        rows=repair_rows,
                        requester_ids=set(unresolved_requesters),
                        allow_live_crm=True,
                    )
                    for requester_id, resolved in crm_resolutions.items():
                        remember_resolution(
                            requester_id,
                            display_name=str(resolved.get('display_name') or '').strip(),
                            source=str(resolved.get('source') or 'crm_projection'),
                        )
        phone_only_rows = [
            item
            for item in repair_rows
            if not str(item.get('requester_id') or '').strip()
            and str(item.get('member_id') or '').strip()
            and self._registration_group_batch_member_name_needs_repair(item.get('display_name'))
            and str(item.get('wa_phone_raw') or item.get('wa_phone_normalized') or '').strip()
        ]
        if phone_only_rows:
            with self.db.connect() as conn:
                member_resolutions = self._resolve_registration_group_batch_member_names_from_crm(
                    conn,
                    rows=phone_only_rows,
                    requester_ids=set(),
                    allow_live_crm=True,
                    allow_member_id_keys=True,
                )
            for resolution_key, resolved in member_resolutions.items():
                if not str(resolution_key or '').startswith('member:'):
                    continue
                remember_member_resolution(
                    str(resolution_key).split(':', 1)[1],
                    display_name=str(resolved.get('display_name') or '').strip(),
                    source=str(resolved.get('source') or 'crm_projection'),
                )
        updates: List[tuple[str, str, Optional[str], str, str, str, Optional[str], str]] = []
        applied: List[Dict[str, Any]] = []
        unresolved = 0
        now = utc_now()
        for row in repair_rows:
            requester_id = str(row.get('requester_id') or '').strip()
            member_id = str(row.get('member_id') or '').strip()
            approval_run_id = str(row.get('approval_run_id') or '').strip()
            resolved = resolved_by_requester.get(requester_id) or resolved_by_member_id.get(member_id) or {}
            resolved_name = str(resolved.get('display_name') or '').strip()
            resolved_phone = str(resolved.get('phone') or '').strip()
            if not resolved_phone and approval_run_id:
                resolved_phone = str(ingress_phone_by_run.get(approval_run_id) or '').strip()
            next_name = str(row.get('display_name') or '').strip()
            next_source = self._registration_group_batch_member_normalize_name_source(row.get('display_name_source'))
            next_enhanced_at = str(row.get('display_name_enhanced_at') or '').strip() or None
            next_phone_raw = str(row.get('wa_phone_raw') or '').strip()
            next_phone_normalized = str(row.get('wa_phone_normalized') or '').strip()
            changed = False
            usable_resolved_name = self._registration_group_batch_member_usable_display_name(resolved_name)
            if usable_resolved_name:
                resolved_source = self._registration_group_batch_member_normalize_name_source(resolved.get('source'))
                if next_name != usable_resolved_name:
                    next_name = usable_resolved_name
                    changed = True
                if resolved_source and next_source != resolved_source:
                    next_source = resolved_source
                    changed = True
                if resolved_source and not next_enhanced_at:
                    next_enhanced_at = now
                    changed = True
            if resolved_phone:
                if not next_phone_raw or '*' in next_phone_raw:
                    next_phone_raw = resolved_phone
                    changed = True
                if not next_phone_normalized or '*' in next_phone_normalized:
                    next_phone_normalized = resolved_phone
                    changed = True
            if not member_id:
                unresolved += 1
                continue
            repair_result = 'updated' if changed else 'unresolved'
            repair_next_attempt_at = None if changed else self._registration_group_batch_member_repair_next_attempt_at(now)
            updates.append((next_name, next_source, next_enhanced_at, next_phone_raw, next_phone_normalized, repair_result, repair_next_attempt_at, member_id))
            if changed:
                applied.append({
                    'member_id': member_id,
                    'requester_id': requester_id,
                    'display_name': next_name,
                    'display_name_source': next_source,
                    'display_name_source_label': self._registration_group_batch_member_name_source_label(next_source),
                    'wa_phone_raw': next_phone_raw,
                    'wa_phone_normalized': next_phone_normalized,
                })
            else:
                unresolved += 1
        if updates:
            with self.db.connect() as conn:
                conn.executemany(
                    'UPDATE registration_group_approval_batch_members SET display_name = ?, display_name_source = ?, display_name_enhanced_at = ?, wa_phone_raw = ?, wa_phone_normalized = ?, repair_last_attempt_at = ?, repair_last_result = ?, repair_next_attempt_at = ?, updated_at = ? WHERE member_id = ?',
                    [(name, source, enhanced_at, phone_raw, phone_normalized, now, result, next_attempt, now, member_id) for (name, source, enhanced_at, phone_raw, phone_normalized, result, next_attempt, member_id) in updates],
                )
                conn.commit()
        return {
            'checked': len(candidate_rows),
            'candidates': len(repair_rows),
            'updated': len(applied),
            'unresolved': unresolved,
            'skipped_cooldown': skipped_cooldown,
            'applied': applied,
        }

    @staticmethod
    def _registration_group_batch_member_crm_row_indicates_joined_official_group(crm_row: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(crm_row, dict) or not crm_row:
            return False
        official_group_value = str(
            crm_row.get('officialGroup')
            or crm_row.get('official_group')
            or crm_row.get('crm_verified_official_group')
            or crm_row.get('joinGroupName')
            or crm_row.get('join_group_name')
            or ''
        ).strip()
        if official_group_value:
            return True
        join_group_value = crm_row.get('joinGroup', crm_row.get('join_group'))
        if isinstance(join_group_value, bool):
            return join_group_value
        return str(join_group_value or '').strip().lower() in {'1', 'true', 'yes', 'y', 'joined', 'done', 'success'}

    @staticmethod
    def _registration_group_batch_member_crm_row_has_abnormal_marker(crm_row: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(crm_row, dict) or not crm_row:
            return False
        abnormal_text = ' '.join(
            str(crm_row.get(key) or '').strip().lower()
            for key in ('userQuality', 'user_quality', 'remark', 'remarks', 'statusRemark', 'status_remark')
            if str(crm_row.get(key) or '').strip()
        )
        if not abnormal_text:
            return False
        return any(
            marker in abnormal_text
            for marker in ('异常', 'abnormal', 'blacklist', '黑名单', '封禁', '封号', '违规', '拉黑')
        )

    def _registration_group_batch_member_historical_registered_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        phone_value: str,
        mobile_body: str,
        area_code: int,
        digits_only: str,
        country: str,
        lead_dict: Optional[Dict[str, Any]] = None,
        allow_live_crm: bool = True,
    ) -> Optional[Dict[str, Any]]:
        def build_snapshot(source_row: Dict[str, Any], *, source: str) -> Dict[str, Any]:
            lead_id = str((lead_dict or {}).get('lead_id') or source_row.get('lead_id') or '').strip() or None
            submission_count = 0
            if lead_id:
                submission_count = int(conn.execute('SELECT COUNT(1) FROM account_submissions WHERE lead_id = ?', (lead_id,)).fetchone()[0] or 0)
            return {
                'registration_status': 'registered',
                'registration_status_label': '已注册',
                'lead_id': lead_id,
                'lead_current_status': str((lead_dict or {}).get('current_status') or 'historical_official_group_registered').strip() or 'historical_official_group_registered',
                'submission_count': submission_count,
                'country': source_row.get('country') or (lead_dict or {}).get('country') or country or None,
                'area_code': source_row.get('area_code') or (lead_dict or {}).get('area_code') or int(area_code or 0) or None,
                'historical_registered_source': source,
            }

        matched_customer_id = str((lead_dict or {}).get('matched_customer_id') or '').strip()
        projection_row = None
        if matched_customer_id:
            projection_row = conn.execute(
                'SELECT * FROM customer_projection WHERE customer_id = ? LIMIT 1',
                (matched_customer_id,),
            ).fetchone()
        if projection_row is None and mobile_body and area_code:
            projection_row = conn.execute(
                'SELECT * FROM customer_projection WHERE mobile = ? AND area_code = ? ORDER BY updated_at DESC LIMIT 1',
                (mobile_body, int(area_code)),
            ).fetchone()
        if projection_row is None and digits_only and digits_only != mobile_body and area_code:
            projection_row = conn.execute(
                'SELECT * FROM customer_projection WHERE mobile = ? AND area_code = ? ORDER BY updated_at DESC LIMIT 1',
                (digits_only, int(area_code)),
            ).fetchone()
        if projection_row is not None:
            projection_dict = dict(projection_row)
            if (
                self._registration_group_batch_member_crm_row_indicates_joined_official_group(projection_dict)
                and not self._registration_group_batch_member_crm_row_has_abnormal_marker(projection_dict)
            ):
                return build_snapshot(projection_dict, source='customer_projection')

        crm_candidates: List[Tuple[Optional[str], Optional[str]]] = []
        seen_crm_keys: set[Tuple[str, str]] = set()

        def add_crm_candidate(yw_id_value: Optional[str], mobile_value: Optional[str]) -> None:
            yw_key = str(yw_id_value or '').strip()
            mobile_key = str(mobile_value or '').strip()
            if not yw_key and not mobile_key:
                return
            dedupe_key = (yw_key, mobile_key)
            if dedupe_key in seen_crm_keys:
                return
            seen_crm_keys.add(dedupe_key)
            crm_candidates.append((yw_key or None, mobile_key or None))

        add_crm_candidate((lead_dict or {}).get('yw_id'), (lead_dict or {}).get('mobile'))
        add_crm_candidate((lead_dict or {}).get('yw_id'), mobile_body)
        add_crm_candidate((lead_dict or {}).get('yw_id'), digits_only)
        add_crm_candidate((lead_dict or {}).get('yw_id'), phone_value)
        add_crm_candidate(None, mobile_body)
        add_crm_candidate(None, digits_only)
        add_crm_candidate(None, phone_value)
        if projection_row is not None:
            projection_dict = dict(projection_row)
            add_crm_candidate(projection_dict.get('yw_id'), projection_dict.get('mobile'))

        if allow_live_crm and self.crm_adapter is not None:
            for yw_id_candidate, mobile_candidate in crm_candidates:
                try:
                    crm_row = self.crm_adapter.find_customer(yw_id=yw_id_candidate, mobile=mobile_candidate)
                except Exception:
                    crm_row = None
                if not crm_row:
                    continue
                crm_dict = dict(crm_row)
                crm_customer_id = str(crm_dict.get('id') or '').strip()
                if matched_customer_id and crm_customer_id and crm_customer_id != matched_customer_id:
                    continue
                if (
                    self._registration_group_batch_member_crm_row_indicates_joined_official_group(crm_dict)
                    and not self._registration_group_batch_member_crm_row_has_abnormal_marker(crm_dict)
                ):
                    return build_snapshot(crm_dict, source='live_crm')
        return None

    def _registration_group_batch_member_historical_registered_snapshot_from_masked_phone(
        self,
        conn: sqlite3.Connection,
        *,
        phone_value: str,
        allow_live_crm: bool = True,
    ) -> Optional[Dict[str, Any]]:
        compact = re.sub(r'\s+', '', str(phone_value or '').strip())
        if not compact or '*' not in compact:
            return None
        match = re.fullmatch(r'\+?(\d{1,4})\*+(\d{3,})', compact)
        if not match:
            return None
        area_code = int(match.group(1))
        suffix = match.group(2)
        candidate_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT lead_id, current_status, area_code, country, crm_verified_at, matched_customer_id, crm_verified_official_group, yw_id, mobile FROM leads WHERE area_code = ? AND mobile LIKE ? ORDER BY updated_at DESC LIMIT 5",
                (area_code, f'%{suffix}'),
            ).fetchall()
        ]
        if len(candidate_rows) != 1:
            return None
        candidate = candidate_rows[0]
        return self._registration_group_batch_member_historical_registered_snapshot(
            conn,
            phone_value=phone_value,
            mobile_body=str(candidate.get('mobile') or '').strip(),
            area_code=int(candidate.get('area_code') or area_code),
            digits_only=str(candidate.get('mobile') or '').strip(),
            country=str(candidate.get('country') or '').strip(),
            lead_dict=candidate,
            allow_live_crm=allow_live_crm,
        )

    @staticmethod
    def _registration_group_batch_member_phone_lookup_candidates(
        *,
        phone_value: str,
        mobile_body: str,
        area_code: int,
        digits_only: str,
    ) -> List[str]:
        candidates: List[str] = []

        def remember(value: Any) -> None:
            normalized = re.sub(r'\D+', '', str(value or '').strip())
            if not normalized:
                return
            expanded = [normalized]
            expanded.extend(sorted(localized_phone_match_keys(phone=normalized, area_code=area_code)))
            expanded.extend(sorted(Service._brazil_phone_ninth_digit_variants(normalized)))
            for item in expanded:
                if item and item not in candidates:
                    candidates.append(item)

        prefixes: List[str] = []
        if area_code:
            prefixes.append(str(int(area_code)))
        for prefix in ('62', '55', '56', '57', '58', '63', '852'):
            if prefix not in prefixes:
                prefixes.append(prefix)

        for value in (mobile_body, digits_only, phone_value):
            normalized = re.sub(r'\D+', '', str(value or '').strip())
            remember(normalized)
            for prefix in prefixes:
                if not normalized.startswith(prefix):
                    continue
                without_prefix = normalized[len(prefix):]
                remember(without_prefix)
                if without_prefix.startswith(prefix):
                    remember(without_prefix[len(prefix):])

        for value in list(candidates):
            if value.startswith('0'):
                remember(value[1:])
            else:
                remember(f'0{value}')
        return candidates

    def _registration_group_batch_member_registration_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        wa_phone_raw: str,
        wa_phone_normalized: str,
        allow_live_crm: bool = True,
    ) -> Dict[str, Any]:
        phone_value = str(wa_phone_raw or wa_phone_normalized or '').strip()
        if not phone_value:
            return {
                'registration_status': 'not_found',
                'registration_status_label': '未注册',
                'lead_id': None,
                'lead_current_status': None,
                'submission_count': 0,
            }
        if '*' in phone_value:
            historical_snapshot = self._registration_group_batch_member_historical_registered_snapshot_from_masked_phone(
                conn,
                phone_value=phone_value,
                allow_live_crm=allow_live_crm,
            )
            if historical_snapshot is not None:
                return historical_snapshot
            return {
                'registration_status': 'not_found',
                'registration_status_label': '未注册',
                'lead_id': None,
                'lead_current_status': None,
                'submission_count': 0,
            }
        try:
            mobile_body, area_code, country = normalize_phone_identity(mobile=phone_value, area_code=0, country='')
        except Exception:
            mobile_body, area_code, country = ('', 0, '')
        digits_only = re.sub(r'\D+', '', phone_value)
        lead_row = None
        ignored_lead_row = None
        projection_row = None
        phone_candidates = self._registration_group_batch_member_phone_lookup_candidates(
            phone_value=phone_value,
            mobile_body=mobile_body,
            area_code=int(area_code or 0),
            digits_only=digits_only,
        )
        for candidate_mobile in phone_candidates:
            if projection_row is None and area_code:
                projection_row = conn.execute(
                    "SELECT customer_id, lead_id, area_code, mobile, yw_id FROM customer_projection WHERE mobile = ? AND area_code = ? ORDER BY updated_at DESC LIMIT 1",
                    (candidate_mobile, int(area_code)),
                ).fetchone()
            if projection_row is None:
                projection_row = conn.execute(
                    "SELECT customer_id, lead_id, area_code, mobile, yw_id FROM customer_projection WHERE mobile = ? ORDER BY updated_at DESC LIMIT 1",
                    (candidate_mobile,),
                ).fetchone()
            if lead_row is None and area_code:
                candidate_lead_row = conn.execute(
                    "SELECT lead_id, current_status, area_code, country, crm_verified_at, matched_customer_id, crm_verified_official_group, yw_id, mobile FROM leads WHERE mobile = ? AND area_code = ? ORDER BY updated_at DESC LIMIT 1",
                    (candidate_mobile, int(area_code)),
                ).fetchone()
                if candidate_lead_row is not None:
                    candidate_status = str(dict(candidate_lead_row).get('current_status') or '').strip()
                    if candidate_status in IGNORED_HISTORY_LEAD_STATUSES:
                        ignored_lead_row = ignored_lead_row or candidate_lead_row
                    else:
                        lead_row = candidate_lead_row
            if lead_row is None:
                candidate_lead_row = conn.execute(
                    "SELECT lead_id, current_status, area_code, country, crm_verified_at, matched_customer_id, crm_verified_official_group, yw_id, mobile FROM leads WHERE mobile = ? ORDER BY updated_at DESC LIMIT 1",
                    (candidate_mobile,),
                ).fetchone()
                if candidate_lead_row is not None:
                    candidate_status = str(dict(candidate_lead_row).get('current_status') or '').strip()
                    if candidate_status in IGNORED_HISTORY_LEAD_STATUSES:
                        ignored_lead_row = ignored_lead_row or candidate_lead_row
                    else:
                        lead_row = candidate_lead_row
            if projection_row is not None and lead_row is not None:
                break
        if lead_row is None and ignored_lead_row is not None:
            lead_row = ignored_lead_row
        if lead_row is None:
            if projection_row is not None:
                projection_dict = dict(projection_row)
                if str(projection_dict.get('customer_id') or '').strip() and str(projection_dict.get('mobile') or '').strip():
                    return {
                        'registration_status': 'registered',
                        'registration_status_label': '已注册',
                        'lead_id': str(projection_dict.get('lead_id') or '').strip() or None,
                        'lead_current_status': 'crm_projected',
                        'submission_count': 0,
                        'country': country or None,
                        'area_code': projection_dict.get('area_code') or int(area_code or 0) or None,
                        'historical_registered_source': 'customer_projection',
                    }
            return {
                'registration_status': 'not_found',
                'registration_status_label': '未注册',
                'lead_id': None,
                'lead_current_status': None,
                'submission_count': 0,
                'country': country or None,
                'area_code': int(area_code or 0) or None,
            }
        lead_dict = dict(lead_row)
        lead_current_status = str(lead_dict.get('current_status') or '').strip()
        if lead_current_status in IGNORED_HISTORY_LEAD_STATUSES:
            historical_snapshot = self._registration_group_batch_member_historical_registered_snapshot(
                conn,
                phone_value=phone_value,
                mobile_body=mobile_body,
                area_code=int(area_code or 0),
                digits_only=digits_only,
                country=country,
                lead_dict=lead_dict,
                allow_live_crm=allow_live_crm,
            )
            if historical_snapshot is not None:
                return historical_snapshot
            return {
                'registration_status': 'not_found',
                'registration_status_label': '未注册',
                'lead_id': None,
                'lead_current_status': None,
                'submission_count': 0,
                'country': lead_dict.get('country') or country or None,
                'area_code': lead_dict.get('area_code') or int(area_code or 0) or None,
            }
        submission_count = int(conn.execute('SELECT COUNT(1) FROM account_submissions WHERE lead_id = ?', (lead_dict['lead_id'],)).fetchone()[0] or 0)
        crm_verified_at = str(lead_dict.get('crm_verified_at') or '').strip()
        projection_dict = dict(projection_row) if projection_row is not None else {}
        projection_customer_id = str(projection_dict.get('customer_id') or '').strip()
        projection_mobile = str(projection_dict.get('mobile') or '').strip()
        if projection_customer_id and projection_mobile:
            return {
                'registration_status': 'registered',
                'registration_status_label': '已注册',
                'lead_id': lead_dict.get('lead_id') or projection_dict.get('lead_id'),
                'lead_current_status': lead_current_status or 'crm_projected',
                'submission_count': submission_count,
                'country': lead_dict.get('country') or country or None,
                'area_code': lead_dict.get('area_code') or projection_dict.get('area_code') or int(area_code or 0) or None,
                'historical_registered_source': 'customer_projection',
            }
        if str(lead_dict.get('matched_customer_id') or '').strip() and str(lead_dict.get('mobile') or '').strip():
            return {
                'registration_status': 'registered',
                'registration_status_label': '已注册',
                'lead_id': lead_dict.get('lead_id'),
                'lead_current_status': lead_current_status or 'crm_matched',
                'submission_count': submission_count,
                'country': lead_dict.get('country') or country or None,
                'area_code': lead_dict.get('area_code') or int(area_code or 0) or None,
                'historical_registered_source': 'lead_matched_customer',
            }
        registered_statuses = {'bind_success', 'group_join_pending', 'group_join_success', 'synced'}
        if crm_verified_at and lead_current_status.lower() in registered_statuses:
            registration_status = 'registered'
            registration_status_label = '已注册'
        else:
            registration_status = 'in_progress'
            registration_status_label = '引导注册中'
        return {
            'registration_status': registration_status,
            'registration_status_label': registration_status_label,
            'lead_id': lead_dict.get('lead_id'),
            'lead_current_status': lead_current_status or None,
            'submission_count': submission_count,
            'country': lead_dict.get('country') or country or None,
            'area_code': lead_dict.get('area_code') or int(area_code or 0) or None,
        }

    @staticmethod
    def _registration_group_batch_member_stored_registration_snapshot(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(row, dict):
            return None
        status = str(row.get('registration_status_snapshot') or '').strip()
        if status not in {'registered', 'in_progress', 'not_found'}:
            return None
        label = str(row.get('registration_status_label_snapshot') or '').strip()
        if not label:
            label = '已注册' if status == 'registered' else ('引导注册中' if status == 'in_progress' else '未注册')
        return {
            'registration_status': status,
            'registration_status_label': label,
            'lead_id': str(row.get('lead_id') or '').strip() or None,
            'lead_current_status': None,
            'submission_count': 0,
            'matched_customer_id': str(row.get('matched_customer_id') or '').strip() or None,
            'eligibility_source': str(row.get('eligibility_source') or '').strip() or None,
            'eligibility_snapshot': str(row.get('eligibility_snapshot') or '').strip() or None,
            'registration_status_source': 'approval_retention_snapshot',
        }

    @staticmethod
    def _registration_group_batch_members_beijing_tz() -> timezone:
        return timezone(timedelta(hours=8))

    @staticmethod
    def _normalize_approval_batch_member_group_type(value: Any) -> str:
        normalized = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
        if normalized in {'official', 'official_group', 'officialgroup'}:
            return 'official_group'
        return 'registration_group'

    @classmethod
    def _approval_batch_member_group_type_label(cls, value: Any) -> str:
        return '官方群' if cls._normalize_approval_batch_member_group_type(value) == 'official_group' else '注册群'

    @classmethod
    def _default_registration_group_batch_members_date(cls) -> str:
        return datetime.now(timezone.utc).astimezone(cls._registration_group_batch_members_beijing_tz()).date().isoformat()

    @classmethod
    def _normalize_registration_group_batch_members_date(cls, approved_date: Optional[str]) -> str:
        text = str(approved_date or '').strip()
        if not text:
            return cls._default_registration_group_batch_members_date()
        try:
            return datetime.strptime(text, '%Y-%m-%d').date().isoformat()
        except Exception:
            return cls._default_registration_group_batch_members_date()

    @classmethod
    def _parse_registration_group_batch_members_date(cls, approved_date: Optional[str]) -> Optional[str]:
        text = str(approved_date or '').strip()
        if not text:
            return None
        try:
            return datetime.strptime(text, '%Y-%m-%d').date().isoformat()
        except Exception:
            return None

    @classmethod
    def _resolve_registration_group_batch_members_date_range(
        cls,
        *,
        approval_run_id: Optional[str] = None,
        approved_date: Optional[str] = None,
        approved_date_start: Optional[str] = None,
        approved_date_end: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        derived_date = cls._registration_group_batch_members_external_date_from_batch_id(approval_run_id)
        if derived_date:
            return derived_date, derived_date
        single_date = cls._parse_registration_group_batch_members_date(approved_date)
        start_date = cls._parse_registration_group_batch_members_date(approved_date_start)
        end_date = cls._parse_registration_group_batch_members_date(approved_date_end)
        if single_date and not start_date and not end_date:
            return single_date, single_date
        if start_date and not end_date:
            end_date = start_date
        elif end_date and not start_date:
            start_date = end_date
        if start_date and end_date and start_date > end_date:
            start_date, end_date = end_date, start_date
        return start_date, end_date

    @classmethod
    def _registration_group_batch_members_external_date_from_batch_id(cls, approval_run_id: Optional[str]) -> Optional[str]:
        text = str(approval_run_id or '').strip()
        match = re.fullmatch(r'(\d{8})(\d{2,})', text)
        if not match:
            return None
        try:
            parsed = datetime.strptime(match.group(1), '%Y%m%d').date()
        except Exception:
            return None
        return parsed.isoformat()

    @classmethod
    def _registration_group_batch_member_beijing_parts(cls, approved_at: str) -> Dict[str, Any]:
        dt = parse_iso_datetime(str(approved_at or '').strip())
        localized = dt.astimezone(cls._registration_group_batch_members_beijing_tz())
        return {
            'approved_at_beijing': localized,
            'approved_date_beijing': localized.date().isoformat(),
            'approved_time_beijing': localized.strftime('%H:%M:%S'),
        }

    @classmethod
    def _registration_group_batch_members_utc_bounds(
        cls,
        approved_date_start: Optional[str],
        approved_date_end: Optional[str],
    ) -> Tuple[Optional[str], Optional[str]]:
        if not approved_date_start and not approved_date_end:
            return None, None
        tz = cls._registration_group_batch_members_beijing_tz()
        start_date = cls._parse_registration_group_batch_members_date(approved_date_start)
        end_date = cls._parse_registration_group_batch_members_date(approved_date_end) or start_date
        if start_date and end_date and start_date > end_date:
            start_date, end_date = end_date, start_date
        start_bound = None
        end_bound = None
        if start_date:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=tz)
            start_bound = start_dt.astimezone(timezone.utc).isoformat()
        if end_date:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d').replace(tzinfo=tz) + timedelta(days=1)
            end_bound = end_dt.astimezone(timezone.utc).isoformat()
        return start_bound, end_bound

    @classmethod
    def _registration_group_batch_member_date_in_range(
        cls,
        approved_date_value: str,
        approved_date_start: Optional[str],
        approved_date_end: Optional[str],
    ) -> bool:
        normalized_value = str(approved_date_value or '').strip()
        if not normalized_value:
            return False
        if approved_date_start and normalized_value < approved_date_start:
            return False
        if approved_date_end and normalized_value > approved_date_end:
            return False
        return True

    @classmethod
    def _build_registration_group_batch_display_map(cls, rows: List[Dict[str, Any]], approved_date_start: Optional[str] = None, approved_date_end: Optional[str] = None) -> Dict[str, str]:
        per_date_sequence_source: Dict[str, Dict[str, datetime]] = {}
        for row in rows:
            approved_date = str(row.get('approved_date_beijing') or '')
            if not approved_date:
                continue
            if not cls._registration_group_batch_member_date_in_range(approved_date, approved_date_start, approved_date_end):
                continue
            approval_run_id = str(row.get('approval_run_id') or '').strip()
            if not approval_run_id:
                continue
            approved_at_beijing = row.get('approved_at_beijing')
            if not isinstance(approved_at_beijing, datetime):
                continue
            date_map = per_date_sequence_source.setdefault(approved_date, {})
            previous = date_map.get(approval_run_id)
            if previous is None or approved_at_beijing < previous:
                date_map[approval_run_id] = approved_at_beijing
        label_map: Dict[str, str] = {}
        for approved_date, sequence_source in per_date_sequence_source.items():
            compact_date = approved_date.replace('-', '')
            for index, (approval_run_id, _) in enumerate(sorted(sequence_source.items(), key=lambda item: (item[1], item[0])), start=1):
                label_map[approval_run_id] = f'{compact_date}{index:02d}'
        return label_map

    def _approval_batch_filter_metadata_from_account_bindings(
        self,
        conn: sqlite3.Connection,
        *,
        group_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_filter = self._normalize_approval_batch_member_group_type(group_type) if str(group_type or '').strip() else ''
        group_options: Dict[str, str] = {}
        area_options: Dict[str, str] = {}
        group_area_by_key: Dict[str, str] = {}
        rows = conn.execute(
            """
            SELECT responsible_type, group_links
            FROM whatsapp_approval_accounts
            WHERE responsible_type IN ('registration_group', 'official_group')
            ORDER BY updated_at DESC, account_key ASC
            """
        ).fetchall()
        for raw_row in rows:
            row = dict(raw_row)
            row_type = self._normalize_approval_batch_member_group_type(row.get('responsible_type'))
            if normalized_filter and row_type != normalized_filter:
                continue
            try:
                raw_bindings = json.loads(row.get('group_links') or '[]')
            except Exception:
                raw_bindings = []
            bindings = _normalize_group_link_bindings(
                [dict(item or {}) for item in raw_bindings if isinstance(item, dict)],
                responsible_type=row_type,
            )
            for binding in bindings:
                if binding.get('enabled') is False:
                    continue
                group_id = self._whatsapp_binding_runtime_group_id(binding)
                group_name = str(binding.get('group_name') or binding.get('runtime_probe_group_name') or '').strip()
                registration_group = str(binding.get('registration_group') or '').strip()
                link = str(binding.get('link') or '').strip()
                value = group_id or registration_group or group_name or link
                label = group_name or registration_group or link or group_id
                if not value or not label:
                    continue
                group_options.setdefault(value, f'{self._approval_batch_member_group_type_label(row_type)} · {label}')
                area = _canonical_mcn_region_value(binding.get('area'))
                if area:
                    area_options.setdefault(area, str(_enrich_mcn_region_option(area).get('label') or area))
                    for lookup_key in (value, group_id, registration_group, group_name, link):
                        normalized_key = str(lookup_key or '').strip()
                        if normalized_key:
                            group_area_by_key.setdefault(normalized_key, area)
        return {
            'group_options': group_options,
            'area_options': area_options,
            'group_area_by_key': group_area_by_key,
        }

    def _approval_batch_configured_area_options(self) -> Dict[str, str]:
        options: Dict[str, str] = {}
        try:
            payload = self.list_whatsapp_approval_area_options()
        except Exception:
            payload = {}
        for item in list((payload or {}).get('options') or []):
            if not isinstance(item, dict):
                continue
            value = _canonical_mcn_region_value(item.get('value') or item.get('label') or item.get('code'))
            if not value:
                continue
            options.setdefault(value, str(item.get('label') or value).strip() or value)
        return options

    def _approval_batch_member_today_counts(self, *, approved_date: Optional[str] = None) -> Dict[str, Any]:
        effective_date = self._parse_registration_group_batch_members_date(approved_date) or self._default_registration_group_batch_members_date()
        start_bound, end_bound = self._registration_group_batch_members_utc_bounds(effective_date, effective_date)
        counts: Dict[str, Any] = {
            'approved_date': effective_date,
            'registration_group': 0,
            'official_group': 0,
            'total': 0,
        }
        if not start_bound or not end_bound:
            return counts
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT COALESCE(NULLIF(group_type, ''), 'registration_group') AS group_type, COUNT(1) AS approved_count
                FROM registration_group_approval_batch_members
                WHERE approved_at >= ? AND approved_at < ?
                GROUP BY COALESCE(NULLIF(group_type, ''), 'registration_group')
                """,
                (start_bound, end_bound),
            ).fetchall()
        for raw_row in rows:
            row = dict(raw_row)
            group_type = self._normalize_approval_batch_member_group_type(row.get('group_type'))
            approved_count = int(row.get('approved_count') or 0)
            counts[group_type] = approved_count
            counts['total'] += approved_count
        return counts

    def registration_group_approval_batch_members_summary(
        self,
        *,
        group_type: Optional[str] = None,
        approved_date: Optional[str] = None,
        approved_date_start: Optional[str] = None,
        approved_date_end: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_group_type = self._normalize_approval_batch_member_group_type(group_type) if str(group_type or '').strip() else ''
        effective_start, effective_end = self._resolve_registration_group_batch_members_date_range(
            approval_run_id='',
            approved_date=approved_date,
            approved_date_start=approved_date_start,
            approved_date_end=approved_date_end,
        )
        if not effective_start and not effective_end:
            effective_start = effective_end = self._default_registration_group_batch_members_date()
        start_bound, end_bound = self._registration_group_batch_members_utc_bounds(effective_start, effective_end)
        summary = {
            'total_members': 0,
            'registration_group_members': 0,
            'official_group_members': 0,
            'registered_members': 0,
            'in_progress_members': 0,
            'not_registered_members': 0,
            'registration_rate': 0.0,
            'registration_rate_source': 'stored_batch_member_snapshot',
        }
        where_clauses: List[str] = []
        query_params: List[Any] = []
        if start_bound:
            where_clauses.append('approved_at >= ?')
            query_params.append(start_bound)
        if end_bound:
            where_clauses.append('approved_at < ?')
            query_params.append(end_bound)
        if normalized_group_type:
            where_clauses.append("COALESCE(NULLIF(group_type, ''), 'registration_group') = ?")
            query_params.append(normalized_group_type)
        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ''
        with self.db.connect() as conn:
            row = conn.execute(
                f"""
                WITH filtered AS (
                  SELECT
                    COALESCE(NULLIF(group_type, ''), 'registration_group') AS group_type,
                    registration_status_snapshot,
                    REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', '') AS phone_full,
                    CASE
                      WHEN REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', '') LIKE '852%' THEN substr(REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', ''), 4)
                      WHEN REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', '') LIKE '55%' THEN substr(REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', ''), 3)
                      WHEN REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', '') LIKE '62%' THEN substr(REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', ''), 3)
                      WHEN REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', '') LIKE '63%' THEN substr(REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', ''), 3)
                      WHEN REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', '') LIKE '56%' THEN substr(REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', ''), 3)
                      WHEN REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', '') LIKE '57%' THEN substr(REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', ''), 3)
                      WHEN REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', '') LIKE '58%' THEN substr(REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', ''), 3)
                      ELSE REPLACE(REPLACE(COALESCE(wa_phone_normalized, wa_phone_raw, ''), '+', ''), ' ', '')
                    END AS phone_local
                  FROM registration_group_approval_batch_members
                  {where_sql}
                ),
                classified AS (
                  SELECT
                    filtered.*,
                    CASE
                      WHEN registration_status_snapshot = 'registered' THEN 1
                      WHEN EXISTS (
                        SELECT 1 FROM customer_projection cp
                        WHERE COALESCE(cp.customer_id, '') <> ''
                          AND cp.mobile IN (filtered.phone_full, filtered.phone_local)
                      ) THEN 1
                      WHEN EXISTS (
                        SELECT 1 FROM leads l
                        WHERE l.mobile IN (filtered.phone_full, filtered.phone_local)
                          AND (
                            COALESCE(l.matched_customer_id, '') <> ''
                            OR (
                              COALESCE(l.crm_verified_at, '') <> ''
                              AND l.current_status IN ('bind_success', 'group_join_pending', 'group_join_success', 'synced')
                            )
                          )
                      ) THEN 1
                      ELSE 0
                    END AS registered_flag,
                    CASE
                      WHEN registration_status_snapshot = 'in_progress' THEN 1
                      WHEN EXISTS (
                        SELECT 1 FROM leads l
                        WHERE l.mobile IN (filtered.phone_full, filtered.phone_local)
                      ) THEN 1
                      ELSE 0
                    END AS known_lead_flag
                  FROM filtered
                )
                SELECT
                  COUNT(1) AS total_members,
                  SUM(CASE WHEN group_type = 'official_group' THEN 1 ELSE 0 END) AS official_group_members,
                  SUM(CASE WHEN group_type = 'official_group' THEN 0 ELSE 1 END) AS registration_group_members,
                  SUM(CASE WHEN registered_flag = 1 THEN 1 ELSE 0 END) AS registered_members,
                  SUM(CASE WHEN registered_flag = 0 AND known_lead_flag = 1 THEN 1 ELSE 0 END) AS in_progress_members
                FROM classified
                """,
                tuple(query_params),
            ).fetchone()
        if row is not None:
            data = dict(row)
            summary['total_members'] = int(data.get('total_members') or 0)
            summary['registration_group_members'] = int(data.get('registration_group_members') or 0)
            summary['official_group_members'] = int(data.get('official_group_members') or 0)
            summary['registered_members'] = int(data.get('registered_members') or 0)
            summary['in_progress_members'] = int(data.get('in_progress_members') or 0)
            summary['not_registered_members'] = max(
                summary['total_members'] - summary['registered_members'] - summary['in_progress_members'],
                0,
            )
            summary['registration_rate'] = round(summary['registered_members'] / summary['total_members'], 4) if summary['total_members'] else 0.0
        return {
            'filters': {
                'group_type': normalized_group_type or None,
                'approved_date': effective_start if effective_start == effective_end else None,
                'approved_date_start': effective_start,
                'approved_date_end': effective_end,
            },
            'summary': summary,
        }

    def list_registration_group_approval_batch_members(
        self,
        *,
        approval_run_id: Optional[str] = None,
        registration_group: Optional[str] = None,
        group_type: Optional[str] = None,
        area: Optional[str] = None,
        keyword: Optional[str] = None,
        registration_status: Optional[str] = None,
        approved_date: Optional[str] = None,
        approved_date_start: Optional[str] = None,
        approved_date_end: Optional[str] = None,
        member_ids: Optional[str] = None,
        limit: int = 30,
        page: int = 1,
    ) -> Dict[str, Any]:
        normalized_run_id = str(approval_run_id or '').strip()
        normalized_group = str(registration_group or '').strip()
        normalized_group_type = self._normalize_approval_batch_member_group_type(group_type) if str(group_type or '').strip() else ''
        normalized_area = str(area or '').strip()
        normalized_keyword = str(keyword or '').strip()
        normalized_status = str(registration_status or '').strip().lower()
        configured_area_options_map = self._approval_batch_configured_area_options()
        selected_member_ids = {
            item.strip()
            for item in re.split(r'[,\s]+', str(member_ids or '').strip())
            if item.strip()
        }
        effective_approved_date_start, effective_approved_date_end = self._resolve_registration_group_batch_members_date_range(
            approval_run_id=normalized_run_id,
            approved_date=approved_date,
            approved_date_start=approved_date_start,
            approved_date_end=approved_date_end,
        )
        normalized_limit = max(1, min(int(limit or 30), 200))
        normalized_page = max(1, int(page or 1))
        where_clauses: List[str] = []
        query_params: List[Any] = []
        start_bound, end_bound = self._registration_group_batch_members_utc_bounds(
            effective_approved_date_start,
            effective_approved_date_end,
        )
        if start_bound:
            where_clauses.append('approved_at >= ?')
            query_params.append(start_bound)
        if end_bound:
            where_clauses.append('approved_at < ?')
            query_params.append(end_bound)
        if normalized_group_type:
            where_clauses.append("COALESCE(NULLIF(group_type, ''), 'registration_group') = ?")
            query_params.append(normalized_group_type)
        if normalized_group:
            where_clauses.append('(registration_group = ? OR registration_group_name = ?)')
            query_params.extend([normalized_group, normalized_group])
        if selected_member_ids:
            placeholders = ','.join('?' for _ in selected_member_ids)
            where_clauses.append(f'member_id IN ({placeholders})')
            query_params.extend(sorted(selected_member_ids))
        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ''
        query = f"SELECT member_id, approval_run_id, COALESCE(NULLIF(group_type, ''), 'registration_group') AS group_type, registration_group, registration_group_name, lead_id, matched_customer_id, registration_status_snapshot, registration_status_label_snapshot, eligibility_source, eligibility_snapshot, requester_id, display_name, display_name_source, display_name_enhanced_at, wa_phone_raw, wa_phone_normalized, requested_at, approved_at, batch_index, created_at, updated_at FROM registration_group_approval_batch_members{where_sql} ORDER BY approved_at DESC, approval_run_id DESC, batch_index ASC"
        rows: List[Dict[str, Any]] = []
        summary = {
            'total_members': 0,
            'registration_group_members': 0,
            'official_group_members': 0,
            'registered_members': 0,
            'in_progress_members': 0,
            'not_registered_members': 0,
            'registration_rate': 0.0,
        }
        with self.db.connect() as conn:
            raw_rows = [dict(r) for r in conn.execute(query, tuple(query_params)).fetchall()]
            for row in raw_rows:
                row['group_type'] = self._normalize_approval_batch_member_group_type(row.get('group_type'))
                row['group_type_label'] = self._approval_batch_member_group_type_label(row.get('group_type'))
                row.update(self._registration_group_batch_member_beijing_parts(str(row.get('approved_at') or '')))
            display_map = self._build_registration_group_batch_display_map(raw_rows, effective_approved_date_start, effective_approved_date_end)
            binding_filter_metadata = self._approval_batch_filter_metadata_from_account_bindings(conn, group_type=normalized_group_type)
            group_options_map: Dict[str, str] = dict(binding_filter_metadata.get('group_options') or {})
            area_options_map: Dict[str, str] = dict(configured_area_options_map)
            for area_value, area_label in dict(binding_filter_metadata.get('area_options') or {}).items():
                area_options_map.setdefault(area_value, area_label)
            group_area_by_key: Dict[str, str] = dict(binding_filter_metadata.get('group_area_by_key') or {})
            registration_meta_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
            date_filtered_rows: List[Dict[str, Any]] = []
            for row in raw_rows:
                row_group_type = self._normalize_approval_batch_member_group_type(row.get('group_type'))
                if normalized_group_type and row_group_type != normalized_group_type:
                    continue
                approved_date_value = str(row.get('approved_date_beijing') or '')
                if not self._registration_group_batch_member_date_in_range(
                    approved_date_value,
                    effective_approved_date_start,
                    effective_approved_date_end,
                ):
                    continue
                group_value = str(row.get('registration_group') or '').strip() or str(row.get('registration_group_name') or '').strip()
                group_label = str(row.get('registration_group_name') or row.get('registration_group') or '').strip()
                if group_value and group_value not in group_options_map:
                    group_options_map[group_value] = f'{self._approval_batch_member_group_type_label(row_group_type)} · {group_label or group_value}'
                date_filtered_rows.append(row)
            candidate_rows: List[Dict[str, Any]] = []
            for row in date_filtered_rows:
                display_batch_id = display_map.get(str(row.get('approval_run_id') or '').strip(), '')
                if normalized_run_id and normalized_run_id not in {str(row.get('approval_run_id') or '').strip(), display_batch_id}:
                    continue
                if normalized_group and normalized_group not in {
                    str(row.get('registration_group') or '').strip(),
                    str(row.get('registration_group_name') or '').strip(),
                }:
                    continue
                candidate_rows.append(row)
            enriched_rows: List[Dict[str, Any]] = []
            for row in candidate_rows:
                cache_key = (
                    str(row.get('wa_phone_raw') or '').strip(),
                    str(row.get('wa_phone_normalized') or '').strip(),
                    str(row.get('lead_id') or '').strip(),
                    str(row.get('matched_customer_id') or '').strip(),
                    str(row.get('registration_status_snapshot') or '').strip(),
                )
                registration_meta = registration_meta_cache.get(cache_key)
                if registration_meta is None:
                    stored_registration_meta = self._registration_group_batch_member_stored_registration_snapshot(row)
                    registration_meta = stored_registration_meta or self._registration_group_batch_member_registration_snapshot(
                        conn,
                        wa_phone_raw=cache_key[0],
                        wa_phone_normalized=cache_key[1],
                        allow_live_crm=False,
                    )
                    registration_meta_cache[cache_key] = registration_meta
                display_name_source = self._registration_group_batch_member_normalize_name_source(row.get('display_name_source'))
                if not display_name_source and self._registration_group_batch_member_usable_display_name(row.get('display_name')):
                    display_name_source = 'approval_snapshot'
                enriched_row = {
                    **row,
                    **registration_meta,
                    'display_name_source': display_name_source,
                    'display_name_source_label': self._registration_group_batch_member_name_source_label(display_name_source),
                    'approval_batch_display_id': display_map.get(str(row.get('approval_run_id') or '').strip(), ''),
                    'approved_time_display': str(row.get('approved_time_beijing') or ''),
                    'approved_date_display': str(row.get('approved_date_beijing') or ''),
                    'area': (
                        str(registration_meta.get('country') or '').strip()
                        or group_area_by_key.get(str(row.get('registration_group') or '').strip())
                        or group_area_by_key.get(str(row.get('registration_group_name') or '').strip())
                        or None
                    ),
                }
                enriched_rows.append(enriched_row)
                area_value = str(enriched_row.get('area') or '').strip()
                if area_value and area_value not in area_options_map:
                    area_options_map[area_value] = area_value
            matched_rows: List[Dict[str, Any]] = []
            for normalized_row in enriched_rows:
                if selected_member_ids and str(normalized_row.get('member_id') or '').strip() not in selected_member_ids:
                    continue
                if normalized_area and normalized_area != str(normalized_row.get('area') or '').strip():
                    continue
                if normalized_keyword:
                    display_batch_id = str(normalized_row.get('approval_batch_display_id') or '').strip()
                    like_candidates = [
                        str(normalized_row.get('approval_run_id') or '').strip(),
                        display_batch_id,
                        str(normalized_row.get('registration_group') or '').strip(),
                        str(normalized_row.get('registration_group_name') or '').strip(),
                        str(normalized_row.get('group_type_label') or '').strip(),
                        str(normalized_row.get('group_type') or '').strip(),
                        str(normalized_row.get('display_name') or '').strip(),
                        str(normalized_row.get('wa_phone_raw') or '').strip(),
                        str(normalized_row.get('wa_phone_normalized') or '').strip(),
                        str(normalized_row.get('area') or '').strip(),
                    ]
                    if not any(normalized_keyword in candidate for candidate in like_candidates if candidate):
                        continue
                if normalized_status and normalized_row.get('registration_status') != normalized_status:
                    continue
                matched_rows.append(normalized_row)
            summary['total_members'] = len(matched_rows)
            for normalized_row in matched_rows:
                if self._normalize_approval_batch_member_group_type(normalized_row.get('group_type')) == 'official_group':
                    summary['official_group_members'] += 1
                else:
                    summary['registration_group_members'] += 1
                if normalized_row.get('registration_status') == 'registered':
                    summary['registered_members'] += 1
                elif normalized_row.get('registration_status') == 'in_progress':
                    summary['in_progress_members'] += 1
                else:
                    summary['not_registered_members'] += 1
            summary['registration_rate'] = round(summary['registered_members'] / summary['total_members'], 4) if summary['total_members'] else 0.0
            total_pages = max(1, (len(matched_rows) + normalized_limit - 1) // normalized_limit)
            normalized_page = min(normalized_page, total_pages)
            start_index = (normalized_page - 1) * normalized_limit
            end_index = start_index + normalized_limit
            rows = matched_rows[start_index:end_index]
        registration_group_options = [
            {'value': value, 'label': label}
            for value, label in sorted(group_options_map.items(), key=lambda item: item[1])
        ]
        area_options = [
            {'value': value, 'label': label}
            for value, label in sorted(area_options_map.items(), key=lambda item: item[1])
        ]
        return {
            'filters': {
                'approval_run_id': normalized_run_id or None,
                'registration_group': normalized_group or None,
                'group_type': normalized_group_type or None,
                'area': normalized_area or None,
                'keyword': normalized_keyword or None,
                'registration_status': normalized_status or None,
                'approved_date': effective_approved_date_start if effective_approved_date_start == effective_approved_date_end else None,
                'approved_date_start': effective_approved_date_start,
                'approved_date_end': effective_approved_date_end,
                'member_ids': ','.join(sorted(selected_member_ids)) if selected_member_ids else None,
                'limit': normalized_limit,
                'page': normalized_page,
            },
            'summary': summary,
            'pagination': {
                'page': normalized_page,
                'page_size': normalized_limit,
                'total_rows': summary['total_members'],
                'total_pages': total_pages,
                'has_prev': normalized_page > 1,
                'has_next': normalized_page < total_pages,
            },
            'registration_group_options': registration_group_options,
            'group_options': registration_group_options,
            'group_type_options': [
                {'value': 'registration_group', 'label': '注册群'},
                {'value': 'official_group', 'label': '官方群'},
            ],
            'area_options': area_options,
            'rows': rows,
        }

    @staticmethod
    def _registration_group_batch_member_export_columns() -> List[str]:
        return [
            '审批时间',
            '批次ID',
            '群类型',
            '群组',
            '地区',
            '昵称',
            'WA号码',
            '注册状态',
            'Lead ID',
            '提交次数',
        ]

    def _registration_group_batch_member_export_rows(self, *, approval_run_id: Optional[str] = None, registration_group: Optional[str] = None, group_type: Optional[str] = None, area: Optional[str] = None, keyword: Optional[str] = None, registration_status: Optional[str] = None, approved_date: Optional[str] = None, approved_date_start: Optional[str] = None, approved_date_end: Optional[str] = None, member_ids: Optional[str] = None, limit: int = 5000) -> List[Dict[str, Any]]:
        result = self.list_registration_group_approval_batch_members(
            approval_run_id=approval_run_id,
            registration_group=registration_group,
            group_type=group_type,
            area=area,
            keyword=keyword,
            registration_status=registration_status,
            approved_date=approved_date,
            approved_date_start=approved_date_start,
            approved_date_end=approved_date_end,
            member_ids=member_ids,
            limit=limit,
        )
        export_rows: List[Dict[str, Any]] = []
        for row in list(result.get('rows') or []):
            export_rows.append({
                '审批时间': str(row.get('approved_time_display') or ''),
                '批次ID': str(row.get('approval_batch_display_id') or row.get('approval_run_id') or ''),
                '群类型': str(row.get('group_type_label') or ''),
                '群组': str(row.get('registration_group_name') or row.get('registration_group') or ''),
                '地区': str(row.get('area') or ''),
                '昵称': str(row.get('display_name') or ''),
                'WA号码': str(row.get('wa_phone_raw') or row.get('wa_phone_normalized') or ''),
                '注册状态': str(row.get('registration_status_label') or row.get('registration_status') or ''),
                'Lead ID': str(row.get('lead_id') or ''),
                '提交次数': int(row.get('submission_count') or 0),
            })
        return export_rows

    def export_registration_group_approval_batch_members_csv(self, *, approval_run_id: Optional[str] = None, registration_group: Optional[str] = None, group_type: Optional[str] = None, area: Optional[str] = None, keyword: Optional[str] = None, registration_status: Optional[str] = None, approved_date: Optional[str] = None, approved_date_start: Optional[str] = None, approved_date_end: Optional[str] = None, member_ids: Optional[str] = None, limit: int = 5000) -> bytes:
        columns = self._registration_group_batch_member_export_columns()
        rows = self._registration_group_batch_member_export_rows(
            approval_run_id=approval_run_id,
            registration_group=registration_group,
            group_type=group_type,
            area=area,
            keyword=keyword,
            registration_status=registration_status,
            approved_date=approved_date,
            approved_date_start=approved_date_start,
            approved_date_end=approved_date_end,
            member_ids=member_ids,
            limit=limit,
        )
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return buffer.getvalue().encode('utf-8-sig')

    def export_registration_group_approval_batch_members_xlsx(self, *, approval_run_id: Optional[str] = None, registration_group: Optional[str] = None, group_type: Optional[str] = None, area: Optional[str] = None, keyword: Optional[str] = None, registration_status: Optional[str] = None, approved_date: Optional[str] = None, approved_date_start: Optional[str] = None, approved_date_end: Optional[str] = None, member_ids: Optional[str] = None, limit: int = 5000) -> bytes:
        columns = self._registration_group_batch_member_export_columns()
        rows = self._registration_group_batch_member_export_rows(
            approval_run_id=approval_run_id,
            registration_group=registration_group,
            group_type=group_type,
            area=area,
            keyword=keyword,
            registration_status=registration_status,
            approved_date=approved_date,
            approved_date_start=approved_date_start,
            approved_date_end=approved_date_end,
            member_ids=member_ids,
            limit=limit,
        )
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = '群审批留存页'
        sheet.append(columns)
        header_fill = PatternFill(fill_type='solid', fgColor='DBEAFE')
        header_font = Font(bold=True, color='1E3A8A')
        status_fills = {
            '已注册': PatternFill(fill_type='solid', fgColor='DCFCE7'),
            '引导注册中': PatternFill(fill_type='solid', fgColor='FEF3C7'),
            '未注册': PatternFill(fill_type='solid', fgColor='FEE2E2'),
        }
        for row in rows:
            sheet.append([row.get(column, '') for column in columns])
        sheet.freeze_panes = 'A2'
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center', vertical='center')
        for row_index in range(2, sheet.max_row + 1):
            sheet.cell(row=row_index, column=1).alignment = Alignment(horizontal='center')
            sheet.cell(row=row_index, column=8).alignment = Alignment(horizontal='center')
            sheet.cell(row=row_index, column=10).alignment = Alignment(horizontal='center')
            status_value = str(sheet.cell(row=row_index, column=8).value or '').strip()
            fill = status_fills.get(status_value)
            if fill is not None:
                sheet.cell(row=row_index, column=8).fill = fill
        widths = {
            'A': 22, 'B': 26, 'C': 14, 'D': 28, 'E': 18,
            'F': 22, 'G': 20, 'H': 20, 'I': 20, 'J': 12,
        }
        for column_letter, width in widths.items():
            sheet.column_dimensions[column_letter].width = width
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def registration_group_approval_batch_members_export_filename(
        self,
        *,
        extension: str,
        approval_run_id: Optional[str] = None,
        registration_group: Optional[str] = None,
        group_type: Optional[str] = None,
        area: Optional[str] = None,
        keyword: Optional[str] = None,
        registration_status: Optional[str] = None,
        approved_date: Optional[str] = None,
        approved_date_start: Optional[str] = None,
        approved_date_end: Optional[str] = None,
        member_ids: Optional[str] = None,
        limit: int = 5000,
    ) -> str:
        normalized_extension = str(extension or 'xlsx').strip().lower().lstrip('.') or 'xlsx'
        exported_at = datetime.now(timezone.utc).astimezone(self._registration_group_batch_members_beijing_tz()).strftime('%Y%m%d-%H%M%S')
        return f'reg-approvals-{exported_at}.{normalized_extension}'

    def registration_group_approval_decision_status(self, approval_run_id: str) -> Dict[str, Any]:
        row = self._find_registration_group_approval_ingress_event(approval_run_id)
        if row is None:
            raise HTTPException(status_code=404, detail='registration group approval run not found')
        result = dict(row.get('result_snapshot_dict') or {})
        batch_sync = self._find_registration_group_approval_batch_sync_log(approval_run_id)
        if result and str(result.get('crm_recorded')).strip().lower() != 'true' and batch_sync:
            batch_status = str(batch_sync.get('status') or '').strip().lower()
            batch_response = dict(batch_sync.get('response_snapshot_dict') or {})
            batch_request = dict(batch_sync.get('request_snapshot_dict') or {})
            if batch_status == 'success':
                result['crm_recorded'] = True
                raw_result = dict(result.get('raw_result') or {})
                crm_batch = dict(raw_result.get('crm_batch') or {})
                crm_batch['accepted'] = True
                crm_batch['crm_sync_status'] = 'success'
                crm_batch['crm_payload'] = dict(batch_request.get('crm_payload') or crm_batch.get('crm_payload') or {})
                crm_batch['crm_response'] = batch_response
                crm_batch['approval_run_id'] = str(batch_request.get('approval_run_id') or approval_run_id).strip() or approval_run_id
                crm_batch['request_snapshot'] = {
                    'registration_group': batch_request.get('registration_group'),
                    'registration_group_name': batch_request.get('registration_group_name'),
                    'approved_count': batch_request.get('approved_count'),
                    'approved_by': batch_request.get('approved_by'),
                    'approved_by_name': batch_request.get('approved_by_name'),
                    'source_platform': batch_request.get('source_platform'),
                    'source_campaign': batch_request.get('source_campaign'),
                    'source_adset': batch_request.get('source_adset'),
                    'source_ad': batch_request.get('source_ad'),
                    'approved_at': batch_request.get('approved_at'),
                    'area': batch_request.get('area'),
                    'remark': batch_request.get('remark'),
                    'approval_run_id': batch_request.get('approval_run_id'),
                }
                raw_result['crm_batch'] = crm_batch
                result['raw_result'] = raw_result
                result['crm_batch'] = crm_batch
        return {
            'approval_run_id': approval_run_id,
            'ingress_event_id': row['event_id'],
            'status': row['status'],
            'created_at': row.get('created_at'),
            'updated_at': row.get('updated_at'),
            'processed_at': row.get('processed_at'),
            'result': result,
        }

    def _registration_group_active_monitor_target_health(self) -> Optional[Dict[str, Any]]:
        try:
            production_ops = self.get_production_ops_daemon_config() or {}
        except Exception:
            return None
        runtime = dict(production_ops.get('runtime') or {})
        status = dict(runtime.get('status') or {})
        monitor_target = dict(status.get('monitor_target') or {})
        if str(monitor_target.get('source') or '').strip() != 'account_binding':
            return None
        base_url = str(monitor_target.get('worker_base_url') or '').strip().rstrip('/')
        if not base_url:
            return None
        if _is_legacy_shared_webjs_8787_url(base_url) and not _legacy_shared_webjs_8787_allowed():
            return None
        try:
            health = self._request_whatsapp_approval_worker_health(base_url)
        except Exception:
            return None
        if not isinstance(health, dict) or not health:
            return None
        normalized = dict(health)
        normalized.setdefault('configured', True)
        normalized.setdefault('provider', 'whatsapp_webjs_bridge')
        normalized.setdefault('base_url', base_url)
        normalized.setdefault('supports', normalized.get('supports') or ['approve', 'strict_queue_and_member_verify', 'crm_batch_writeback_ready'])
        normalized['routed_via'] = 'production_ops_monitor_target'
        normalized['monitor_target'] = {
            'account_key': str(monitor_target.get('account_key') or '').strip() or None,
            'registration_group': str(monitor_target.get('registration_group') or '').strip() or None,
            'worker_base_url': base_url,
            'source': 'account_binding',
        }
        return normalized

    def _registration_group_dedicated_runtime_health(self) -> Optional[Dict[str, Any]]:
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM whatsapp_approval_accounts
                    WHERE responsible_type = 'registration_group' AND enabled = 1
                    ORDER BY updated_at DESC, account_key ASC
                    """
                ).fetchall()
        except Exception:
            return None
        candidates: list[Dict[str, Any]] = []
        for raw_row in rows:
            row = dict(raw_row)
            account_key = str(row.get('account_key') or '').strip()
            if not account_key:
                continue
            try:
                runtime_state, session_state, _ = self._build_whatsapp_approval_lightweight_runtime_snapshot(row)
            except Exception:
                runtime_state, session_state = {}, {}
            provider_decision = self._resolve_wa_provider_decision(
                account=row,
                runtime_state=runtime_state,
                responsible_type='registration_group',
            )
            runtime_provider = str(runtime_state.get('provider_name') or provider_decision.get('provider_name') or '').strip().lower()
            if runtime_provider == 'baileys':
                base_url = str(runtime_state.get('base_url') or '').strip().rstrip('/')
                normalized = {
                    'configured': bool(runtime_state.get('configured') or base_url),
                    'status': str(runtime_state.get('status') or ('running' if session_state.get('can_probe') else 'configured')).strip(),
                    'provider': 'baileys',
                    'provider_name': 'baileys',
                    'provider_mode': str(runtime_state.get('provider_mode') or provider_decision.get('provider_mode') or '').strip(),
                    'base_url': base_url or None,
                    'ready': bool(runtime_state.get('ready') or session_state.get('can_probe')),
                    'authenticated': bool(runtime_state.get('authenticated') or session_state.get('login_verified')),
                    'supports': ['approve', 'strict_queue_and_member_verify', 'crm_batch_writeback_ready', 'baileys_provider_runtime'],
                    'account_key': account_key,
                    'account_name': row.get('account_name'),
                    'runtime': runtime_state,
                    'session': session_state,
                    'source': 'baileys_approval_account_runtime',
                    'routed_via': 'baileys_approval_account_runtime',
                    'legacy_shared_worker_ignored': True,
                    'monitor_target': {
                        'account_key': account_key,
                        'worker_base_url': '',
                        'provider_base_url': base_url or None,
                        'source': 'account_binding',
                        'provider_name': 'baileys',
                        'provider_mode': str(runtime_state.get('provider_mode') or provider_decision.get('provider_mode') or '').strip(),
                    },
                }
                if session_state.get('login_verified') or session_state.get('can_probe'):
                    normalized['status'] = 'running'
                candidates.append(normalized)
                continue
            try:
                runtime_state = self._build_whatsapp_approval_runtime_state(
                    account_key,
                    allow_shared_fallback=False,
                    skip_health_check=True,
                )
            except Exception:
                continue
            base_url = str(runtime_state.get('base_url') or '').strip().rstrip('/')
            if not base_url or not bool(runtime_state.get('active')):
                continue
            try:
                health = self._request_whatsapp_approval_worker_health(base_url)
            except Exception as exc:
                candidates.append({
                    'account_key': account_key,
                    'account_name': row.get('account_name'),
                    'base_url': base_url,
                    'status': 'error',
                    'error': str(exc),
                    'runtime': runtime_state,
                })
                continue
            normalized = dict(health or {})
            normalized.setdefault('status', 'warm' if normalized.get('ready') else 'running')
            normalized.setdefault('configured', True)
            normalized.setdefault('provider', 'whatsapp_webjs_bridge')
            normalized.setdefault('supports', normalized.get('supports') or ['approve', 'strict_queue_and_member_verify', 'crm_batch_writeback_ready'])
            normalized['account_key'] = account_key
            normalized['account_name'] = row.get('account_name')
            normalized['base_url'] = base_url
            normalized['runtime'] = runtime_state
            candidates.append(normalized)
        ready_candidates = [item for item in candidates if str(item.get('status') or '').strip() in {'warm', 'ready', 'running'} and not item.get('error')]
        selected = ready_candidates[0] if ready_candidates else (candidates[0] if candidates else None)
        if not selected:
            return None
        result = dict(selected)
        result['configured'] = True
        result['source'] = 'dedicated_approval_account_runtime'
        result['routed_via'] = 'dedicated_approval_account_runtime'
        result['legacy_shared_worker_ignored'] = True
        result['dedicated_runtime_count'] = len(candidates)
        result['dedicated_ready_runtime_count'] = len(ready_candidates)
        result['monitor_target'] = {
            'account_key': result.get('account_key'),
            'worker_base_url': result.get('base_url'),
            'source': 'account_binding',
        }
        return result

    def registration_group_approval_executor_health(self) -> Dict[str, Any]:
        executor = self.registration_group_approval_executor
        executor_base_url = str(getattr(executor, 'base_url', '') or '').strip().rstrip('/') if executor is not None else ''
        executor_is_webjs_bridge = type(executor).__name__ == 'WebjsBridgeRegistrationGroupApprovalExecutor' if executor is not None else False
        prefers_active_runtime = (
            executor is None
            or (executor_is_webjs_bridge and not executor_base_url)
            or _is_legacy_shared_webjs_8787_url(executor_base_url)
        )
        if prefers_active_runtime:
            routed_health = self._registration_group_active_monitor_target_health()
            if routed_health:
                supports = routed_health.get('supports')
                if supports is None:
                    routed_health['supports'] = []
                return routed_health
            dedicated_health = self._registration_group_dedicated_runtime_health()
            if dedicated_health:
                supports = dedicated_health.get('supports')
                if supports is None:
                    dedicated_health['supports'] = []
                return dedicated_health
            if _is_legacy_shared_webjs_8787_url(executor_base_url):
                return {
                    'configured': True,
                    'status': 'idle',
                    'provider': 'whatsapp_webjs_bridge',
                    'base_url': None,
                    'supports': ['approve', 'strict_queue_and_member_verify', 'crm_batch_writeback_ready'],
                    'source': 'dedicated_approval_account_runtime',
                    'routed_via': 'dedicated_approval_account_runtime',
                    'legacy_shared_worker_ignored': True,
                    'detail': 'legacy shared worker 8787 ignored; waiting for account-bound dedicated runtime',
                }
        if executor is None:
            return {
                'configured': False,
                'status': 'unconfigured',
                'provider': None,
                'supports': [],
            }
        if hasattr(executor, 'health') and callable(getattr(executor, 'health')):
            try:
                health = executor.health() or {}
                if isinstance(health, dict):
                    supports = health.get('supports')
                    if supports is None:
                        health['supports'] = []
                    return health
            except Exception as exc:
                return {
                    'configured': True,
                    'status': 'error',
                    'provider': type(executor).__name__,
                    'supports': [],
                    'error': str(exc),
                }
        return {
            'configured': True,
            'status': 'configured',
            'provider': type(executor).__name__,
            'supports': [],
        }

    def registration_group_approval_executor_warmup(self) -> Dict[str, Any]:
        executor = self.registration_group_approval_executor
        if executor is None:
            return {
                'configured': False,
                'status': 'unconfigured',
                'provider': None,
                'supports': [],
                'warmed': False,
            }
        if hasattr(executor, 'warmup') and callable(getattr(executor, 'warmup')):
            try:
                result = executor.warmup() or {}
                if isinstance(result, dict):
                    result.setdefault('warmed', bool(result.get('status') == 'warm'))
                    result.setdefault('supports', result.get('supports') or [])
                    return result
            except Exception as exc:
                return {
                    'configured': True,
                    'status': 'error',
                    'provider': type(executor).__name__,
                    'supports': [],
                    'warmed': False,
                    'error': str(exc),
                }
        health = self.registration_group_approval_executor_health()
        health['warmed'] = False
        health['warmup_supported'] = False
        return health

    def group_approval_executor_health(self, approval_scope: str) -> Dict[str, Any]:
        normalized_scope = str(approval_scope or '').strip()
        if normalized_scope == 'registration_group':
            return _with_shared_group_approval_executor_result(
                self.registration_group_approval_executor_health(),
                approval_scope='registration_group',
            )
        if normalized_scope == 'official_group':
            return _with_shared_group_approval_executor_result(
                self.official_group_approval_executor_health(),
                approval_scope='official_group',
            )
        raise HTTPException(status_code=400, detail='unsupported approval_scope')

    def group_approval_executor_warmup(self, approval_scope: str) -> Dict[str, Any]:
        normalized_scope = str(approval_scope or '').strip()
        if normalized_scope == 'registration_group':
            return _with_shared_group_approval_executor_result(
                self.registration_group_approval_executor_warmup(),
                approval_scope='registration_group',
            )
        if normalized_scope == 'official_group':
            return _with_shared_group_approval_executor_result(
                self.official_group_approval_executor_warmup(),
                approval_scope='official_group',
            )
        raise HTTPException(status_code=400, detail='unsupported approval_scope')

    def group_approval_executor_target_state(self, approval_scope: str, target_group: str) -> Dict[str, Any]:
        normalized_scope = str(approval_scope or '').strip()
        normalized_target = str(target_group or '').strip()
        if normalized_scope == 'registration_group':
            result = self.registration_group_approval_executor_group_state(normalized_target)
        elif normalized_scope == 'official_group':
            result = self.official_group_approval_executor_group_state(normalized_target)
        else:
            raise HTTPException(status_code=400, detail='unsupported approval_scope')
        return _with_shared_group_approval_executor_result(
            result,
            approval_scope=normalized_scope,
            target_group=normalized_target,
        )

    def group_approval_executor_group_metadata(self, approval_scope: str, target_group: str) -> Dict[str, Any]:
        state = self.group_approval_executor_target_state(approval_scope, target_group)
        requesters = [dict(item) for item in (state.get('requesters') or []) if isinstance(item, dict)]
        participants = [dict(item) for item in (state.get('participants') or []) if isinstance(item, dict)]
        return {
            'approval_scope': str(state.get('approval_scope') or approval_scope or '').strip(),
            'target_group_label': str(state.get('target_group_label') or target_group or '').strip(),
            'configured': bool(state.get('configured')),
            'status': state.get('status'),
            'provider': state.get('provider'),
            'group_name': state.get('group_name'),
            'group_id': state.get('group_id'),
            'pending_count': state.get('pending_count'),
            'member_count': state.get('member_count'),
            'requester_count': len(requesters),
            'participant_count': len(participants),
            'requester_ids': list(state.get('requester_ids') or []),
            'requesters': requesters,
            'participants': participants,
            'routed_runtime': dict(state.get('routed_runtime') or {}),
        }

    def group_approval_executor_member_lookup(
        self,
        approval_scope: str,
        target_group: str,
        *,
        requester_id: Optional[str] = None,
        phone_hint: Optional[str] = None,
        name_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_requester_id = str(requester_id or '').strip()
        normalized_phone_hint = str(phone_hint or '').strip()
        normalized_name_hint = str(name_hint or '').strip()
        if not (normalized_requester_id or normalized_phone_hint or normalized_name_hint):
            raise HTTPException(status_code=400, detail='requester_id, phone_hint, or name_hint is required')
        state = self.group_approval_executor_target_state(approval_scope, target_group)
        requesters = [dict(item) for item in (state.get('requesters') or []) if isinstance(item, dict)]
        matches = []
        for requester in requesters:
            matched_by = self._group_approval_requester_match_reasons(
                approval_scope=str(state.get('approval_scope') or approval_scope or '').strip(),
                requester=requester,
                requester_id=normalized_requester_id,
                phone_hint=normalized_phone_hint,
                name_hint=normalized_name_hint,
            )
            if matched_by:
                matches.append({
                    'matched_by': matched_by,
                    'requester': requester,
                })
        return {
            'approval_scope': str(state.get('approval_scope') or approval_scope or '').strip(),
            'target_group_label': str(state.get('target_group_label') or target_group or '').strip(),
            'group_name': state.get('group_name'),
            'group_id': state.get('group_id'),
            'pending_count': state.get('pending_count'),
            'member_count': state.get('member_count'),
            'lookup': {
                'requester_id': normalized_requester_id or None,
                'phone_hint': normalized_phone_hint or None,
                'name_hint': normalized_name_hint or None,
            },
            'match_count': len(matches),
            'matches': matches,
            'requester_ids': list(state.get('requester_ids') or []),
        }

    def _group_approval_executor_lookup_snapshot(
        self,
        *,
        approval_scope: str,
        target_group: str,
        requester_id: Optional[str] = None,
        phone_hint: Optional[str] = None,
        name_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        metadata = self.group_approval_executor_group_metadata(approval_scope, target_group)
        lookup = self.group_approval_executor_member_lookup(
            approval_scope,
            target_group,
            requester_id=requester_id,
            phone_hint=phone_hint,
            name_hint=name_hint,
        )
        return {
            'group_metadata': {
                'approval_scope': metadata.get('approval_scope'),
                'target_group_label': metadata.get('target_group_label'),
                'group_name': metadata.get('group_name'),
                'group_id': metadata.get('group_id'),
                'pending_count': metadata.get('pending_count'),
                'member_count': metadata.get('member_count'),
                'requester_count': metadata.get('requester_count'),
                'routed_runtime': metadata.get('routed_runtime') or {},
            },
            'runtime_member_lookup': {
                'lookup': lookup.get('lookup') or {},
                'match_count': lookup.get('match_count'),
                'matches': lookup.get('matches') or [],
                'requester_ids': lookup.get('requester_ids') or [],
            },
        }

    def _group_approval_requester_match_reasons(
        self,
        *,
        approval_scope: str,
        requester: Dict[str, Any],
        requester_id: str,
        phone_hint: str,
        name_hint: str,
    ) -> List[str]:
        reasons: List[str] = []
        normalized_requester_id = str(requester_id or '').strip()
        normalized_phone_hint = str(phone_hint or '').strip()
        normalized_name_hint = str(name_hint or '').strip().lower()
        if normalized_requester_id:
            candidate_ids = {
                str(requester.get('requesterId') or '').strip(),
                str(requester.get('requester_id') or '').strip(),
                str(requester.get('id') or '').strip(),
            }
            candidate_ids.discard('')
            if normalized_requester_id in candidate_ids:
                reasons.append('requester_id')
        if normalized_phone_hint:
            target_phone_keys = self._group_approval_phone_match_keys(
                approval_scope=approval_scope,
                phone=normalized_phone_hint,
            )
            requester_phone_keys = set()
            for value in (
                requester.get('phoneNormalized'),
                requester.get('phone_normalized'),
                requester.get('phoneRaw'),
                requester.get('phone_raw'),
                requester.get('debugLidPhoneRaw'),
                requester.get('debugContactNumberRaw'),
                requester.get('contactNumberRaw'),
                requester.get('contact_number_raw'),
                requester.get('waId'),
                requester.get('wa_id'),
                requester.get('requesterId'),
                requester.get('requester_id'),
            ):
                requester_phone_keys.update(self._group_approval_phone_match_keys(
                    approval_scope=approval_scope,
                    phone=value,
                ))
            if target_phone_keys and requester_phone_keys.intersection(target_phone_keys):
                reasons.append('phone_hint')
        if normalized_name_hint:
            for value in (
                requester.get('name'),
                requester.get('displayName'),
                requester.get('display_name'),
                requester.get('fullName'),
                requester.get('full_name'),
                requester.get('pushName'),
                requester.get('push_name'),
            ):
                normalized_candidate = str(value or '').strip().lower()
                if normalized_candidate and normalized_name_hint in normalized_candidate:
                    reasons.append('name_hint')
                    break
        return reasons

    def _group_approval_phone_match_keys(self, *, approval_scope: str, phone: Any) -> set[str]:
        normalized_phone = str(phone or '').strip()
        if not normalized_phone:
            return set()
        if str(approval_scope or '').strip() == 'official_group':
            keys = set(self._official_group_phone_match_keys(phone=normalized_phone))
            digits = ''.join(ch for ch in normalized_phone if ch.isdigit())
            if digits:
                keys.add(digits)
                if digits.startswith('0') and len(digits) > 1:
                    keys.add(digits[1:])
                    keys.add(f'62{digits[1:]}')
                if digits.startswith('62') and len(digits) > 2:
                    keys.add(digits[2:])
                    keys.add(f'0{digits[2:]}')
            return {item for item in keys if item}
        digits = ''.join(ch for ch in normalized_phone if ch.isdigit())
        keys = {normalized_phone.lower()}
        if digits:
            keys.add(digits)
            if not digits.startswith('0'):
                keys.add(f'0{digits}')
        return {item for item in keys if item}

    def registration_group_approval_executor_group_state(self, registration_group: str, *, allow_legacy_target: bool = False) -> Dict[str, Any]:
        normalized_group = str(registration_group or '').strip()
        if not normalized_group:
            raise HTTPException(status_code=400, detail='registration_group is required')
        canonical_group = self._resolve_whatsapp_runtime_target_group(
            responsible_type='registration_group',
            target_group=normalized_group,
        )
        if not canonical_group:
            if not allow_legacy_target:
                raise HTTPException(status_code=400, detail='registration_group must resolve to a WhatsApp group id (@g.us)')
            canonical_group = normalized_group
        routed_runtime = self._resolve_whatsapp_approval_runtime_executor(target_group=canonical_group, responsible_type='registration_group')
        executor = (routed_runtime or {}).get('executor') or self.registration_group_approval_executor
        if executor is None:
            return {
                'configured': False,
                'status': 'unconfigured',
                'provider': None,
                'group_name': canonical_group,
                'group_id': None,
                'pending_count': None,
                'member_count': None,
                'requester_ids': [],
            }
        if hasattr(executor, 'group_state') and callable(getattr(executor, 'group_state')):
            result = executor.group_state(canonical_group) or {}
            if not isinstance(result, dict):
                raise HTTPException(status_code=500, detail='registration group approval executor group_state must return dict result')
            normalized = dict(result)
            normalized.setdefault('configured', True)
            normalized.setdefault('group_name', canonical_group)
            normalized.setdefault('group_id', canonical_group if _looks_like_whatsapp_group_jid(canonical_group) else None)
            normalized.setdefault('pending_count', None)
            normalized.setdefault('member_count', None)
            normalized.setdefault('requester_ids', [])
            if routed_runtime:
                normalized['routed_runtime'] = {
                    'account_key': routed_runtime.get('account_key'),
                    'account_name': routed_runtime.get('account_name'),
                    'base_url': (routed_runtime.get('runtime_state') or {}).get('base_url'),
                }
            return normalized
        raise HTTPException(status_code=400, detail='registration group approval executor group_state not supported')

    def official_group_approval_executor_group_state(self, target_group: str) -> Dict[str, Any]:
        normalized_group = str(target_group or '').strip()
        if not normalized_group:
            raise HTTPException(status_code=400, detail='target_group is required')
        routed_runtime = self._resolve_whatsapp_approval_runtime_executor(target_group=normalized_group, responsible_type='official_group')
        executor = (routed_runtime or {}).get('executor') or self.official_group_approval_executor
        runtime_target = str(
            (routed_runtime or {}).get('resolved_target_group')
            or normalized_group
            or ''
        ).strip()
        if executor is None:
            return {
                'configured': False,
                'status': 'unconfigured',
                'provider': None,
                'group_name': normalized_group,
                'group_id': None,
                'pending_count': None,
                'member_count': None,
                'requester_ids': [],
                'requesters': [],
            }
        if hasattr(executor, 'group_state') and callable(getattr(executor, 'group_state')):
            result = executor.group_state(runtime_target or normalized_group) or {}
            if not isinstance(result, dict):
                raise HTTPException(status_code=500, detail='official group approval executor group_state must return dict result')
            normalized = dict(result)
            normalized.setdefault('configured', True)
            normalized.setdefault('group_name', normalized_group)
            normalized.setdefault('group_id', runtime_target if _looks_like_whatsapp_group_jid(runtime_target) else None)
            normalized.setdefault('pending_count', None)
            normalized.setdefault('member_count', None)
            normalized.setdefault('requester_ids', [])
            normalized.setdefault('requesters', [])
            if routed_runtime:
                normalized['routed_runtime'] = {
                    'account_key': routed_runtime.get('account_key'),
                    'account_name': routed_runtime.get('account_name'),
                    'base_url': (routed_runtime.get('runtime_state') or {}).get('base_url'),
                    'resolved_target_group': runtime_target or None,
                }
            return normalized
        raise HTTPException(status_code=400, detail='official group approval executor group_state not supported')

    def _registration_group_approval_evidence_summary(self, result: Dict[str, Any]) -> Dict[str, Any]:
        raw_result = dict((result or {}).get('raw_result') or {})
        pending_before = raw_result.get('pending_before')
        pending_after = raw_result.get('pending_after')
        member_count_before = raw_result.get('member_count_before')
        member_count_after = raw_result.get('member_count_after')
        queue_delta = bool(result.get('queue_delta'))
        if not queue_delta and pending_before is not None and pending_after is not None:
            try:
                queue_delta = int(pending_after) < int(pending_before)
            except Exception:
                queue_delta = False
        member_count_delta = None
        if member_count_before is not None and member_count_after is not None:
            try:
                member_count_delta = int(member_count_after) - int(member_count_before)
            except Exception:
                member_count_delta = None
        member_confirmed = bool(result.get('member_confirmed'))
        target_member = dict((result or {}).get('target_member') or {})
        approval_may_have_executed = bool(
            queue_delta
            or member_confirmed
            or (member_count_delta is not None and member_count_delta > 0)
        )
        return {
            'pending_before': pending_before,
            'pending_after': pending_after,
            'member_count_before': member_count_before,
            'member_count_after': member_count_after,
            'queue_delta': queue_delta,
            'member_count_delta': member_count_delta,
            'member_confirmed': member_confirmed,
            'approval_may_have_executed': approval_may_have_executed,
            'target_member_name': target_member.get('name'),
            'target_member_phone_raw': target_member.get('phone_raw'),
            'target_member_phone_normalized': target_member.get('phone_normalized'),
        }

    def registration_group_approval_decision(self, payload: RegistrationGroupApprovalDecisionRequest) -> Dict[str, Any]:
        approval_run_id = f"registration_group_approval_{uuid.uuid4().hex[:12]}"
        try:
            payload.approved_count = _coerce_registration_group_single_approval_count(payload.approved_count)
        except Exception:
            pass
        if self.ingress_async_default:
            source_key = str(payload.registration_group or 'registration_group_approval').strip() or 'registration_group_approval'
            queued_payload = payload.model_dump() if hasattr(payload, 'model_dump') else payload.dict()
            queued_payload['approval_run_id'] = approval_run_id
            queued = self._enqueue_ingress_event(
                ingress_type='registration_group_approval_decision',
                source_key=source_key,
                payload=queued_payload,
            )
            result_snapshot = dict(queued.get('result_snapshot') or {})
            if not approval_run_id:
                approval_run_id = str(result_snapshot.get('approval_run_id') or approval_run_id)
            if queued.get('duplicate'):
                existing = self._find_registration_group_approval_ingress_event(approval_run_id)
                if existing is not None:
                    approval_run_id = str((existing.get('payload_dict') or {}).get('approval_run_id') or (existing.get('result_snapshot_dict') or {}).get('approval_run_id') or approval_run_id)
            return {
                'accepted': True,
                'queued': True,
                'duplicate': bool(queued.get('duplicate')),
                'status': queued.get('status') or 'queued',
                'next_action': 'queued_for_processing',
                'approval_run_id': approval_run_id,
                'ingress_event_id': queued.get('event_id'),
                'executed': False,
                'verified': False,
                'verification_pending': False,
                'crm_recorded': False,
            }
        return self._registration_group_approval_decision_sync(payload, approval_run_id=approval_run_id)

    @staticmethod
    def _registration_group_requester_fingerprint(group_state: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        fingerprint: List[Dict[str, Any]] = []
        for item in (group_state or {}).get('requesters') or []:
            if not isinstance(item, dict):
                continue
            requester_id = str(item.get('requesterId') or '').strip()
            if not requester_id:
                continue
            fingerprint.append({
                'requesterId': requester_id,
                'requestedAtUnix': item.get('requestedAtUnix'),
            })
        fingerprint.sort(key=lambda item: (item['requesterId'], '' if item.get('requestedAtUnix') is None else str(item.get('requestedAtUnix'))))
        return fingerprint

    @staticmethod
    def _registration_group_expected_group_state(payload: RegistrationGroupApprovalDecisionRequest) -> Optional[Dict[str, Any]]:
        expected_requesters: List[Dict[str, Any]] = []
        for item in payload.expected_requesters or []:
            if not isinstance(item, dict):
                continue
            requester_id = str(item.get('requesterId') or '').strip()
            if not requester_id:
                continue
            expected_requesters.append({
                'requesterId': requester_id,
                'requestedAtUnix': item.get('requestedAtUnix'),
                'requestedAtIso': str(item.get('requestedAtIso') or item.get('requested_at') or '').strip() or None,
                'displayName': str(item.get('displayName') or item.get('display_name') or '').strip(),
                'phoneRaw': str(
                    item.get('phoneRaw')
                    or item.get('phone_raw')
                    or item.get('debugLidPhoneRaw')
                    or item.get('debugContactNumberRaw')
                    or ''
                ).strip(),
                'phoneNormalized': str(
                    item.get('phoneNormalized')
                    or item.get('phone_normalized')
                    or item.get('debugLidPhoneRaw')
                    or item.get('debugContactNumberRaw')
                    or ''
                ).strip(),
                'debugLidPhoneRaw': str(item.get('debugLidPhoneRaw') or '').strip(),
            })
        expected_requester_ids = [
            str(item).strip() for item in (payload.expected_requester_ids or []) if str(item).strip()
        ]
        if not expected_requesters and not expected_requester_ids and payload.expected_pending_count is None and payload.expected_member_count is None:
            return None
        return {
            'pending_count': payload.expected_pending_count,
            'member_count': payload.expected_member_count,
            'requester_ids': expected_requester_ids,
            'requesters': expected_requesters,
        }

    def _registration_group_queue_changed_before_execute_response(
        self,
        *,
        payload: RegistrationGroupApprovalDecisionRequest,
        decision: str,
        approval_run_id: str,
        started: float,
        expected_group_state: Dict[str, Any],
        current_group_state: Dict[str, Any],
    ) -> Dict[str, Any]:
        total_elapsed_seconds = round(time.perf_counter() - started, 3)
        expected_fingerprint = self._registration_group_requester_fingerprint(expected_group_state)
        current_fingerprint = self._registration_group_requester_fingerprint(current_group_state)
        evidence_summary = {
            'pending_before': expected_group_state.get('pending_count'),
            'pending_after': current_group_state.get('pending_count'),
            'member_count_before': expected_group_state.get('member_count'),
            'member_count_after': current_group_state.get('member_count'),
            'queue_delta': True,
            'member_count_delta': None,
            'member_confirmed': False,
            'approval_may_have_executed': False,
            'target_member_name': None,
            'target_member_phone_raw': None,
            'target_member_phone_normalized': None,
        }
        try:
            if expected_group_state.get('member_count') is not None and current_group_state.get('member_count') is not None:
                evidence_summary['member_count_delta'] = int(current_group_state.get('member_count')) - int(expected_group_state.get('member_count'))
        except Exception:
            evidence_summary['member_count_delta'] = None
        return {
            'registration_group': payload.registration_group,
            'decision': decision,
            'approval_run_id': approval_run_id,
            'executed': False,
            'verified': False,
            'verification_pending': False,
            'crm_recorded': False,
            'status': 'failed',
            'result_code': 'requester_fingerprint_changed_before_approval',
            'result_reason': 'registration group queue changed before approval execution; retry with a fresh snapshot',
            'approved_count': _coerce_registration_group_single_approval_count(payload.approved_count),
            'approved_at': payload.decided_at,
            'elapsed_seconds': total_elapsed_seconds,
            'crm_elapsed_seconds': 0.0,
            'total_elapsed_seconds': total_elapsed_seconds,
            'force_immediate': payload.force_immediate,
            'target_member': {},
            'evidence_summary': evidence_summary,
            'raw_result': {
                'approval_run_id': approval_run_id,
                'execution_disposition': 'blocked_before_execution',
                'expected_group_state': expected_group_state,
                'current_group_state': current_group_state,
                'expected_requester_fingerprint': expected_fingerprint,
                'current_requester_fingerprint': current_fingerprint,
            },
            'crm_batch': None,
        }

    def _registration_group_approval_decision_sync(
        self,
        payload: RegistrationGroupApprovalDecisionRequest,
        *,
        approval_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        decision = str(payload.decision or 'approve').strip().lower() or 'approve'
        if decision != 'approve':
            raise HTTPException(status_code=400, detail='unsupported decision')
        request_approval_count = _coerce_registration_group_single_approval_count(payload.approved_count)
        try:
            payload.approved_count = request_approval_count
        except Exception:
            pass
        target_phone_hint = str(payload.target_phone_hint or '').strip()
        if target_phone_hint:
            existing_membership = self._find_registration_group_memberships_for_phone(mobile=target_phone_hint)
            if existing_membership.get('status') == 'unique':
                active_group = str((existing_membership.get('match') or {}).get('resolved_registration_group') or '').strip()
                requested_group = str(payload.registration_group or '').strip()
                if active_group and requested_group and active_group.lower() != requested_group.lower():
                    total_elapsed_seconds = round(time.perf_counter() - started, 3)
                    return {
                        'registration_group': payload.registration_group,
                        'active_registration_group': active_group,
                        'decision': decision,
                        'approval_run_id': str(approval_run_id or '').strip() or f"registration_group_approval_{uuid.uuid4().hex[:12]}",
                        'executed': False,
                        'verified': False,
                        'verification_pending': False,
                        'crm_recorded': False,
                        'status': 'skipped',
                        'result_code': 'duplicate_registration_group_request',
                        'result_reason': 'phone already has an active registration group; skip approving another registration group',
                        'approved_count': 0,
                        'approved_at': payload.decided_at,
                        'elapsed_seconds': total_elapsed_seconds,
                        'crm_elapsed_seconds': 0.0,
                        'total_elapsed_seconds': total_elapsed_seconds,
                        'force_immediate': payload.force_immediate,
                        'target_member': {},
                        'evidence_summary': {'approval_may_have_executed': False},
                        'raw_result': {'existing_membership': existing_membership.get('match') or {}},
                        'crm_batch': None,
                    }
        payload_runtime = getattr(payload, '__dict__', {}) if payload is not None else {}
        explicit_provider_mode = ''
        if isinstance(payload_runtime, dict):
            for key in RUNTIME_MODE_KEYS:
                candidate = str(payload_runtime.get(key) or '').strip().lower()
                if candidate:
                    explicit_provider_mode = candidate
                    break
        requires_baileys_executor = explicit_provider_mode in BAILEYS_PROVIDER_MODES or explicit_provider_mode.startswith('baileys')
        canonical_registration_group = self._resolve_whatsapp_runtime_target_group(
            responsible_type='registration_group',
            target_group=str(payload.registration_group or '').strip(),
        ) or str(payload.registration_group or '').strip()
        routed_runtime = self._resolve_whatsapp_approval_runtime_executor(target_group=canonical_registration_group, responsible_type='registration_group')
        if requires_baileys_executor:
            routed_provider = str(((routed_runtime or {}).get('provider_decision') or {}).get('provider_name') or '').strip().lower()
            if routed_provider != 'baileys':
                approval_run_id = str(approval_run_id or '').strip() or f"registration_group_approval_{uuid.uuid4().hex[:12]}"
                total_elapsed_seconds = round(time.perf_counter() - started, 3)
                requested_approved_count = _coerce_registration_group_single_approval_count(payload.approved_count)
                return {
                    'registration_group': payload.registration_group,
                    'decision': decision,
                    'approval_run_id': approval_run_id,
                    'executed': False,
                    'verified': False,
                    'verification_pending': False,
                    'crm_recorded': False,
                    'status': 'failed',
                    'result_code': 'registration_group_baileys_runtime_not_routed',
                    'result_reason': 'Baileys manual approval requires an account-bound Baileys runtime; legacy WebJS fallback is disabled',
                    'approved_count': requested_approved_count,
                    'approved_at': payload.decided_at,
                    'elapsed_seconds': total_elapsed_seconds,
                    'crm_elapsed_seconds': 0.0,
                    'total_elapsed_seconds': total_elapsed_seconds,
                    'force_immediate': payload.force_immediate,
                    'target_member': {},
                    'evidence_summary': {'approval_may_have_executed': False},
                    'raw_result': {
                        'approval_run_id': approval_run_id,
                        'provider_mode': explicit_provider_mode,
                        'execution_disposition': 'blocked_before_execution',
                        'routed_runtime': {
                            key: value
                            for key, value in dict(routed_runtime or {}).items()
                            if key not in {'executor'}
                        } if isinstance(routed_runtime, dict) else None,
                    },
                    'crm_batch': None,
                }
        executor = (routed_runtime or {}).get('executor') or self.registration_group_approval_executor
        if executor is None:
            raise HTTPException(status_code=400, detail='registration group approval executor not configured')
        approval_run_id = str(approval_run_id or '').strip() or f"registration_group_approval_{uuid.uuid4().hex[:12]}"
        expected_group_state = self._registration_group_expected_group_state(payload)
        current_group_state = self.whatsapp_approval_runtime_adapter.registration_group_executor_state(
            service=self,
            registration_group=canonical_registration_group,
            allow_legacy_target=True,
        )
        if requires_baileys_executor and isinstance(expected_group_state, dict):
            expected_pending_count = normalize_int_or_none(expected_group_state.get('pending_count'))
            expected_requester_ids = [
                str(item).strip()
                for item in (expected_group_state.get('requester_ids') or [])
                if str(item).strip()
            ] if isinstance(expected_group_state.get('requester_ids'), list) else []
            current_state_source = str((current_group_state or {}).get('source') or '').strip().lower()
            current_state_error = str((current_group_state or {}).get('error') or (current_group_state or {}).get('probe_error') or '').strip().lower()
            current_state_reason = str((current_group_state or {}).get('reason_code') or '').strip().lower()
            current_state_pending = normalize_int_or_none((current_group_state or {}).get('pending_count'))
            current_state_group_id = str(
                (current_group_state or {}).get('group_id')
                or (current_group_state or {}).get('groupId')
                or ''
            ).strip()
            current_state_permission = str((current_group_state or {}).get('permission_status') or '').strip().lower()
            current_state_explicitly_denied = bool(
                current_state_permission in {'not_group_member', 'not_group_admin'}
                or current_state_reason in {'not_group_member', 'not_group_admin'}
                or (current_group_state or {}).get('self_participant_found') is False
                or (current_group_state or {}).get('self_is_admin') is False
                or (current_group_state or {}).get('can_manage_membership_requests') is False
            )
            expected_truth_is_complete = bool(
                expected_pending_count is not None
                and expected_pending_count > 0
                and len(expected_requester_ids) >= expected_pending_count
            )
            current_group_identity_matches = bool(
                not current_state_group_id
                or not canonical_registration_group
                or current_state_group_id.lower() == canonical_registration_group.lower()
            )
            current_state_is_missing_probe = bool(
                expected_truth_is_complete
                and current_group_identity_matches
                and not current_state_explicitly_denied
                and current_state_pending is None
                and current_state_reason in {
                    'poc_baileys_probe_pending_count_missing',
                    'executor_group_state_pending_count_missing',
                }
            )
            current_state_is_link_probe_miss = bool(
                _looks_like_whatsapp_invite_link(canonical_registration_group)
                and expected_truth_is_complete
                and (current_state_pending is None or current_state_pending <= 0)
                and not current_state_explicitly_denied
                and (
                    current_state_source in {'', 'baileys_probe_timeout_snapshot', 'baileys_probe_snapshot_after_error'}
                    or 'timeout' in current_state_error
                    or 'group_not_found' in current_state_error
                    or 'group not found' in current_state_error
                    or bool((current_group_state or {}).get('servedFromSnapshot'))
                    or bool((current_group_state or {}).get('refreshInBackground'))
                )
            )
            if current_state_is_link_probe_miss or current_state_is_missing_probe:
                current_group_state = {
                    **expected_group_state,
                    'group_id': canonical_registration_group,
                    'groupId': canonical_registration_group,
                    'group_name': str((current_group_state or {}).get('group_name') or payload.registration_group or canonical_registration_group),
                    'source': 'manual_approve_preflight_truth_reuse',
                    'preflight_truth_reused_for_execution': True,
                    'preflight_truth_reuse_reason': (
                        'weak_group_state_missing_pending_count'
                        if current_state_is_missing_probe
                        else 'invite_link_group_state_miss'
                    ),
                    'preflight_group_state_miss': current_group_state,
                }
        current_pending_count = max(0, int(current_group_state.get('pending_count') or 0))
        if current_pending_count <= 0:
            total_elapsed_seconds = round(time.perf_counter() - started, 3)
            return {
                'registration_group': payload.registration_group,
                'decision': decision,
                'approval_run_id': approval_run_id,
                'executed': False,
                'verified': False,
                'verification_pending': False,
                'crm_recorded': False,
                'status': 'failed',
                'result_code': 'no_pending_request',
                'result_reason': 'registration group has no pending requests at execution time',
                'approved_count': 0,
                'approved_at': payload.decided_at,
                'elapsed_seconds': total_elapsed_seconds,
                'crm_elapsed_seconds': 0.0,
                'total_elapsed_seconds': total_elapsed_seconds,
                'force_immediate': payload.force_immediate,
                'target_member': {},
                'evidence_summary': {
                    'pending_before': current_pending_count,
                    'pending_after': current_pending_count,
                    'member_count_before': current_group_state.get('member_count'),
                    'member_count_after': current_group_state.get('member_count'),
                    'queue_delta': False,
                    'member_count_delta': 0,
                    'member_confirmed': False,
                    'approval_may_have_executed': False,
                    'target_member_name': None,
                    'target_member_phone_raw': None,
                    'target_member_phone_normalized': None,
                },
                'raw_result': {
                    'approval_run_id': approval_run_id,
                    'current_group_state': current_group_state,
                    'execution_disposition': 'blocked_before_execution',
                },
                'crm_batch': None,
            }
        if expected_group_state is not None:
            expected_fingerprint = self._registration_group_requester_fingerprint(expected_group_state)
            current_fingerprint = self._registration_group_requester_fingerprint(current_group_state)
            fingerprint_changed = bool(expected_fingerprint) and expected_fingerprint != current_fingerprint
            pending_changed = (
                payload.expected_pending_count is not None
                and int(payload.expected_pending_count) != current_pending_count
            )
            if fingerprint_changed or pending_changed:
                return self._registration_group_queue_changed_before_execute_response(
                    payload=payload,
                    decision=decision,
                    approval_run_id=approval_run_id,
                    started=started,
                    expected_group_state=expected_group_state,
                    current_group_state=current_group_state,
                )
        execution_approval_count = _coerce_registration_group_single_approval_count(
            payload.approved_count,
            pending_count=current_pending_count,
        )
        execution_context = {
            'registration_group': canonical_registration_group,
            'group_id': canonical_registration_group,
            'groupId': canonical_registration_group,
            'decision': decision,
            'decided_at': payload.decided_at,
            'decided_by': payload.decided_by,
            'decided_by_name': payload.decided_by_name,
            'source_platform': payload.source_platform,
            'source_campaign': payload.source_campaign,
            'source_adset': payload.source_adset,
            'source_ad': payload.source_ad,
            'target_name_hint': payload.target_name_hint,
            'target_phone_hint': payload.target_phone_hint,
            'approved_count': execution_approval_count,
            'area': payload.area,
            'remark': payload.remark,
            'force_immediate': payload.force_immediate,
            'approval_run_id': approval_run_id,
            'expected_requester_ids': list(payload.expected_requester_ids or []) if isinstance(payload.expected_requester_ids, list) else [],
            'expected_requesters': [dict(item) for item in (payload.expected_requesters or []) if isinstance(item, dict)],
            'latest_group_state_before_approve': current_group_state,
            'expected_group_state': expected_group_state,
            'approval_runtime_route': {
                'account_key': (routed_runtime or {}).get('account_key'),
                'account_name': (routed_runtime or {}).get('account_name'),
                'base_url': ((routed_runtime or {}).get('runtime_state') or {}).get('base_url'),
                'binding': (routed_runtime or {}).get('binding') or {},
                'responsible_type': 'registration_group',
            } if routed_runtime else None,
        }
        if routed_runtime:
            routed_binding = dict((routed_runtime or {}).get('binding') or {})
            routed_runtime_state = dict((routed_runtime or {}).get('runtime_state') or {})
            baileys_account_id = str(
                routed_binding.get('baileys_account_id')
                or routed_runtime_state.get('baileys_account_id')
                or os.getenv('REGISTRATION_GROUP_BAILEYS_ACCOUNT_ID', '')
                or ''
            ).strip()
            if baileys_account_id:
                execution_context['accountId'] = baileys_account_id
                execution_context['baileys_account_id'] = baileys_account_id
            if routed_binding.get('link'):
                execution_context['groupLink'] = routed_binding.get('link')
                execution_context['link'] = routed_binding.get('link')
        requested_approval_count = execution_approval_count
        base_executor_timeout_seconds = max(10.0, float(os.getenv('REGISTRATION_GROUP_APPROVAL_EXECUTOR_TIMEOUT_SECONDS', '45') or 45))
        per_requester_timeout_seconds = max(
            1.0,
            float(os.getenv('REGISTRATION_GROUP_APPROVAL_WEBJS_PER_REQUESTER_TIMEOUT_MS', '5000') or 5000) / 1000.0,
        )
        retry_reset_budget_seconds = max(
            15.0,
            float(os.getenv('REGISTRATION_GROUP_APPROVAL_EXECUTOR_RETRY_RESET_BUDGET_SECONDS', '30') or 30),
        )
        estimated_executor_timeout_seconds = (requested_approval_count * per_requester_timeout_seconds) + retry_reset_budget_seconds
        executor_timeout_seconds = max(base_executor_timeout_seconds, estimated_executor_timeout_seconds)
        if hasattr(executor, 'approve') and callable(getattr(executor, 'approve')):
            call_target = lambda: executor.approve(execution_context)
        elif callable(executor):
            call_target = lambda: executor(execution_context)
        else:
            raise HTTPException(status_code=500, detail='registration group approval executor is not callable')
        result_holder: Dict[str, Any] = {}
        error_holder: Dict[str, BaseException] = {}

        def _run_executor_call() -> None:
            try:
                result_holder['result'] = call_target()
            except BaseException as exc:
                error_holder['error'] = exc

        executor_thread = threading.Thread(
            target=_run_executor_call,
            name=f'registration-group-approval-{approval_run_id}',
            daemon=True,
        )
        executor_thread.start()
        executor_thread.join(timeout=executor_timeout_seconds)
        if executor_thread.is_alive():
            timeout_elapsed = round(time.perf_counter() - started, 3)
            return {
                'registration_group': payload.registration_group,
                'decision': decision,
                'approval_run_id': approval_run_id,
                'executed': False,
                'verified': False,
                'verification_pending': False,
                'crm_recorded': False,
                'status': 'failed',
                'result_code': 'executor_timeout',
                'result_reason': f'registration group approval executor exceeded {executor_timeout_seconds:.0f}s timeout',
                'approved_count': requested_approval_count,
                'approved_at': payload.decided_at,
                'elapsed_seconds': timeout_elapsed,
                'crm_elapsed_seconds': 0.0,
                'total_elapsed_seconds': timeout_elapsed,
                'force_immediate': payload.force_immediate,
                'target_member': {},
                'evidence_summary': {
                    'pending_before': None,
                    'pending_after': None,
                    'member_count_before': None,
                    'member_count_after': None,
                    'queue_delta': False,
                    'member_count_delta': None,
                    'member_confirmed': False,
                    'approval_may_have_executed': False,
                    'target_member_name': None,
                    'target_member_phone_raw': None,
                    'target_member_phone_normalized': None,
                },
                'raw_result': {
                    'approval_run_id': approval_run_id,
                    'base_executor_timeout_seconds': base_executor_timeout_seconds,
                    'executor_timeout_seconds': executor_timeout_seconds,
                    'estimated_executor_timeout_seconds': estimated_executor_timeout_seconds,
                    'per_requester_timeout_seconds': per_requester_timeout_seconds,
                    'retry_reset_budget_seconds': retry_reset_budget_seconds,
                },
                'crm_batch': None,
            }
        if 'error' in error_holder:
            raise error_holder['error']
        result = result_holder.get('result')
        if not isinstance(result, dict):
            raise HTTPException(status_code=500, detail='registration group approval executor must return dict result')
        raw_result = dict(result.get('raw_result') or {})
        raw_result.setdefault('approval_run_id', approval_run_id)
        crm_registration_group_name = self._resolve_registration_group_display_name(
            registration_group=payload.registration_group,
            raw_result=raw_result,
            expected_group_state=expected_group_state if isinstance(expected_group_state, dict) else None,
            current_group_state=current_group_state if isinstance(current_group_state, dict) else None,
        )
        verified = bool(result.get('verified'))
        evidence_summary = self._registration_group_approval_evidence_summary({**result, 'raw_result': raw_result})
        raw_result.setdefault('evidence_summary', evidence_summary)
        executed = True
        requested_approved_count = execution_approval_count
        approval_results = raw_result.get('approval_results')
        approved_success_count = None
        if isinstance(approval_results, list):
            approved_success_count = 0
            for item in approval_results:
                if not isinstance(item, dict):
                    continue
                error_value = item.get('error')
                if error_value in (None, '', 0, '0'):
                    approved_success_count += 1
                    continue
                try:
                    if int(error_value) == 409:
                        approved_success_count += 1
                except Exception:
                    continue
        observed_queue_consumed_count = None
        pending_before = evidence_summary.get('pending_before')
        pending_after = evidence_summary.get('pending_after')
        if pending_before is not None and pending_after is not None:
            try:
                observed_queue_consumed_count = max(0, int(pending_before) - int(pending_after))
            except Exception:
                observed_queue_consumed_count = None
        approved_count = requested_approved_count
        resolved_approved_count_from_consistency = False
        observed_member_count_delta = evidence_summary.get('member_count_delta')
        try:
            observed_member_count_delta = int(observed_member_count_delta)
        except Exception:
            observed_member_count_delta = None
        if (
            verified
            and requested_approved_count > 1
            and approved_success_count is not None
            and approved_success_count < requested_approved_count
        ):
            raw_result['verification_consistency_error'] = 'batch_success_count_mismatch'
            raw_result['verification_consistency_detail'] = {
                'requested_approved_count': requested_approved_count,
                'approved_success_count': approved_success_count,
            }
            if observed_queue_consumed_count and observed_queue_consumed_count > 0 and evidence_summary.get('approval_may_have_executed'):
                approved_count = observed_queue_consumed_count
                resolved_approved_count_from_consistency = True
                raw_result['verification_consistency_detail'].update({
                    'resolved_approved_count': approved_count,
                    'resolution': 'queue_consumed',
                })
            elif (
                evidence_summary.get('approval_may_have_executed')
                and evidence_summary.get('member_confirmed')
                and observed_member_count_delta is not None
                and observed_member_count_delta >= requested_approved_count
            ):
                approved_count = requested_approved_count
                resolved_approved_count_from_consistency = True
                raw_result['verification_consistency_detail'].update({
                    'resolved_approved_count': approved_count,
                    'resolution': 'member_count_delta',
                })
            else:
                verified = False
        if verified:
            if resolved_approved_count_from_consistency:
                pass
            elif approved_success_count is not None and approved_success_count > 0:
                approved_count = approved_success_count
            elif observed_queue_consumed_count and observed_queue_consumed_count > 0:
                approved_count = observed_queue_consumed_count
            elif observed_member_count_delta and observed_member_count_delta > 0:
                approved_count = observed_member_count_delta
        approved_count = min(max(0, int(approved_count or 0)), requested_approved_count)
        verification_pending = bool(not verified and evidence_summary.get('approval_may_have_executed'))
        approved_at = str(result.get('approved_at') or result.get('finished_at') or payload.decided_at)
        target_member = result.get('target_member') or {}
        selected_candidates = self._merge_registration_group_candidate_metadata(
            selected_candidates=[item for item in (raw_result.get('selected_candidates') or []) if isinstance(item, dict)],
            expected_requesters=[item for item in (payload.expected_requesters or []) if isinstance(item, dict)],
            approval_results=[item for item in (raw_result.get('approval_results') or []) if isinstance(item, dict)],
            target_member=target_member if isinstance(target_member, dict) else None,
        )
        if selected_candidates:
            selected_candidates = selected_candidates[:approved_count]
        if selected_candidates:
            self._replace_registration_group_approval_batch_members(
                approval_run_id=approval_run_id,
                registration_group=payload.registration_group,
                registration_group_name=crm_registration_group_name,
                approved_at=approved_at,
                selected_candidates=selected_candidates,
            )
            with self.db.connect() as conn:
                persisted_member_rows = [
                    dict(item) for item in conn.execute(
                        'SELECT member_id, requester_id, display_name, display_name_source, display_name_enhanced_at, wa_phone_raw, wa_phone_normalized FROM registration_group_approval_batch_members WHERE approval_run_id = ? ORDER BY batch_index ASC',
                        (approval_run_id,),
                    ).fetchall()
                ]
            self._repair_registration_group_batch_member_rows(
                rows=persisted_member_rows,
                registration_group=payload.registration_group,
                registration_group_name=crm_registration_group_name,
            )
        resolved_source_ad = payload.source_ad or ' '.join(
            part for part in [
                str(target_member.get('name') or '').strip(),
                str(target_member.get('phone_raw') or '').strip(),
            ] if part
        ) or None
        response_status = result.get('status')
        response_code = result.get('result_code')
        response_reason = result.get('result_reason')
        if verification_pending:
            response_status = 'pending_verification'
            response_code = 'approval_consumed_waiting_verification'
            response_reason = 'approval likely executed but strict verification is still pending'
        crm_batch = None
        crm_recorded = False
        crm_elapsed_seconds = 0.0
        if verified:
            crm_batch = self.create_registration_group_approval_batch(
                RegistrationGroupApprovalBatchRequest(
                    registration_group=payload.registration_group,
                    registration_group_name=crm_registration_group_name,
                    approved_count=approved_count,
                    approved_by=payload.decided_by,
                    approved_by_name=payload.decided_by_name,
                    source_platform=payload.source_platform,
                    source_campaign=payload.source_campaign,
                    source_adset=payload.source_adset,
                    source_ad=resolved_source_ad,
                    approved_at=approved_at,
                    area=payload.area,
                    remark=payload.remark,
                    approval_run_id=approval_run_id,
                )
            )
            crm_elapsed_seconds = round(float(crm_batch.get('elapsed_seconds') or 0.0), 3)
            crm_recorded = crm_batch.get('crm_sync_status') == 'success'
        total_elapsed_seconds = round(time.perf_counter() - started, 3)
        return {
            'registration_group': payload.registration_group,
            'decision': decision,
            'approval_run_id': approval_run_id,
            'executed': executed,
            'verified': verified,
            'verification_pending': verification_pending,
            'crm_recorded': crm_recorded,
            'status': response_status,
            'result_code': response_code,
            'result_reason': response_reason,
            'approved_count': approved_count,
            'approved_at': approved_at,
            'elapsed_seconds': result.get('elapsed_seconds'),
            'crm_elapsed_seconds': crm_elapsed_seconds,
            'total_elapsed_seconds': total_elapsed_seconds,
            'force_immediate': payload.force_immediate,
            'target_member': target_member,
            'evidence_summary': evidence_summary,
            'raw_result': raw_result,
            'crm_batch': crm_batch,
        }

    def _latest_group_join_task(self, conn: sqlite3.Connection, *, lead_id: str) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            """
            SELECT task_id, lead_id, task_type, status, payload, result_code, result_reason, created_at, finished_at
            FROM automation_tasks
            WHERE lead_id = ? AND task_type = 'group_join'
            ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'failed' THEN 1 WHEN 'running' THEN 2 ELSE 3 END,
                     created_at DESC
            LIMIT 1
            """,
            (lead_id,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _extract_closing_record_yw_id_from_snapshot(snapshot: Any) -> str:
        if not isinstance(snapshot, dict):
            return ''
        direct_keys = (
            'account_id',
            'recognized_account_id',
            'yw_id',
            'ywId',
            'linky_account_id',
            'parsed_account_id',
            'normalized_account_id',
        )
        for key in direct_keys:
            value = str(snapshot.get(key) or '').strip()
            if value:
                return value
        for key in ('fields', 'parsed_payload', 'normalized', 'payload', 'raw_result'):
            nested = snapshot.get(key)
            if isinstance(nested, str):
                try:
                    nested = json.loads(nested or '{}')
                except Exception:
                    nested = None
            value = Service._extract_closing_record_yw_id_from_snapshot(nested)
            if value:
                return value
        return ''

    def _resolve_official_group_closing_record_yw_id(
        self,
        conn: sqlite3.Connection,
        *,
        lead: Dict[str, Any],
        lead_id: str,
    ) -> str:
        direct = str((lead or {}).get('yw_id') or '').strip()
        if direct:
            return direct
        submission = conn.execute(
            """
            SELECT account_id, recognized_account_id, recognition_raw
            FROM account_submissions
            WHERE lead_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (lead_id,),
        ).fetchone()
        if submission:
            submission_dict = dict(submission)
            for key in ('recognized_account_id', 'account_id'):
                value = str(submission_dict.get(key) or '').strip()
                if value:
                    return value
            try:
                recognition_raw = json.loads(submission_dict.get('recognition_raw') or '{}')
            except Exception:
                recognition_raw = {}
            value = self._extract_closing_record_yw_id_from_snapshot(recognition_raw)
            if value:
                return value
        task_rows = conn.execute(
            """
            SELECT payload, raw_result
            FROM automation_tasks
            WHERE lead_id = ?
              AND task_type IN ('group_join', 'bind_check', 'account_recognition', 'manual_review', 'crm_sync')
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (lead_id,),
        ).fetchall()
        for task_row in task_rows:
            for column in ('payload', 'raw_result'):
                try:
                    snapshot = json.loads(task_row[column] or '{}')
                except Exception:
                    snapshot = {}
                value = self._extract_closing_record_yw_id_from_snapshot(snapshot)
                if value:
                    return value
        return ''

    @staticmethod
    def _official_group_approval_executor_result_succeeded(executor_result: Dict[str, Any]) -> bool:
        if not isinstance(executor_result, dict):
            return False
        status = str(executor_result.get('status') or '').strip().lower()
        result_code = str(executor_result.get('result_code') or '').strip().lower()
        if status in {'failed', 'failure', 'error'}:
            return False
        if status in {'success', 'succeeded', 'ok'}:
            return True
        return result_code in {'approval_ok', 'approved', 'approved_by_operator', 'success'}

    def _record_official_group_approval_batch_member(
        self,
        *,
        approval_run_id: str,
        target_group: str,
        target_group_name: str,
        approved_at: str,
        lead: Dict[str, Any],
        payload: OfficialGroupApprovalDecisionRequest,
        executor_result: Dict[str, Any],
        eligibility: Optional[Dict[str, Any]] = None,
    ) -> None:
        normalized_run_id = str(approval_run_id or '').strip()
        if not normalized_run_id:
            return
        raw_result = dict((executor_result or {}).get('raw_result') or {})
        target_member = dict((executor_result or {}).get('target_member') or raw_result.get('target_member') or {})
        selected_candidates = [dict(item) for item in (raw_result.get('selected_candidates') or []) if isinstance(item, dict)]
        fallback_display_name = self._registration_group_batch_member_usable_display_name(
            payload.target_name_hint
            or target_member.get('displayName')
            or target_member.get('display_name')
            or target_member.get('name')
            or target_member.get('pushName')
            or target_member.get('notify')
            or lead.get('name')
            or lead.get('full_name')
            or ''
        )
        fallback_phone_raw = str(
            payload.target_phone_hint
            or target_member.get('phoneRaw')
            or target_member.get('phone_raw')
            or target_member.get('phone')
            or lead.get('mobile')
            or ''
        ).strip()
        if fallback_phone_raw and not fallback_phone_raw.startswith('+'):
            area_code = str(lead.get('area_code') or '').strip()
            if area_code:
                fallback_phone_raw = f'+{area_code} {fallback_phone_raw}'
        fallback_requester_id = str(
            payload.target_requester_id
            or target_member.get('requesterId')
            or target_member.get('requester_id')
            or raw_result.get('requesterId')
            or raw_result.get('requester_id')
            or ''
        ).strip()
        fallback_phone_normalized = str(
            target_member.get('phoneNormalized')
            or target_member.get('phone_normalized')
            or raw_result.get('phoneNormalized')
            or raw_result.get('phone_normalized')
            or fallback_phone_raw
            or ''
        ).strip()
        if selected_candidates:
            enriched_candidates: List[Dict[str, Any]] = []
            for candidate in selected_candidates:
                row = dict(candidate)
                if fallback_requester_id and not str(row.get('requesterId') or row.get('requester_id') or '').strip():
                    row['requesterId'] = fallback_requester_id
                if fallback_display_name and self._registration_group_batch_member_name_needs_repair(self._registration_group_batch_member_candidate_display_name(row)):
                    row['displayName'] = fallback_display_name
                    row.setdefault('source', 'lead_history')
                if fallback_phone_raw and not str(row.get('phoneRaw') or row.get('phone_raw') or '').strip():
                    row['phoneRaw'] = fallback_phone_raw
                if fallback_phone_normalized and not str(row.get('phoneNormalized') or row.get('phone_normalized') or '').strip():
                    row['phoneNormalized'] = fallback_phone_normalized
                enriched_candidates.append(row)
            selected_candidates = enriched_candidates
        if not selected_candidates:
            selected_candidates = [{
                'requesterId': fallback_requester_id,
                'displayName': fallback_display_name,
                'phoneRaw': fallback_phone_raw,
                'phoneNormalized': fallback_phone_normalized,
                'requestedAtIso': str(
                    target_member.get('requestedAtIso')
                    or target_member.get('requested_at')
                    or raw_result.get('requestedAtIso')
                    or raw_result.get('requested_at')
                    or ''
                ).strip(),
                'source': 'lead_history',
            }]
        merged_candidates = self._merge_registration_group_candidate_metadata(
            selected_candidates=selected_candidates,
            target_member=target_member if target_member else None,
        )
        eligibility_fields = self._registration_group_batch_member_eligibility_fields(
            lead=lead,
            eligibility=eligibility,
            source='official_group_approval_check',
        )
        merged_candidates = [{**dict(candidate), **eligibility_fields} for candidate in merged_candidates]
        self._replace_registration_group_approval_batch_members(
            approval_run_id=normalized_run_id,
            registration_group=str(target_group or '').strip(),
            registration_group_name=str(target_group_name or '').strip(),
            approved_at=str(approved_at or '').strip(),
            selected_candidates=merged_candidates,
            group_type='official_group',
        )
        with self.db.connect() as conn:
            persisted_member_rows = [
                dict(item) for item in conn.execute(
                    'SELECT member_id, requester_id, display_name, display_name_source, display_name_enhanced_at, wa_phone_raw, wa_phone_normalized FROM registration_group_approval_batch_members WHERE approval_run_id = ? ORDER BY batch_index ASC',
                    (normalized_run_id,),
                ).fetchall()
            ]
            self._repair_registration_group_batch_member_rows(
            rows=persisted_member_rows,
            registration_group=str(target_group or '').strip(),
            registration_group_name=str(target_group_name or '').strip(),
        )

    def _official_group_approval_decision_by_phone(self, payload: OfficialGroupApprovalDecisionRequest) -> Dict[str, Any]:
        decision = str(payload.decision or 'approve').strip().lower() or 'approve'
        decided_at = parse_iso_datetime(payload.decided_at).isoformat()
        check_result = self._official_group_phone_approval_check(
            target_group=str(payload.target_group or '').strip(),
            target_phone_hint=payload.target_phone_hint,
            checked_at=decided_at,
            checked_by=payload.decided_by,
            checked_by_name=payload.decided_by_name,
        )
        if not check_result.get('eligible'):
            with self.db.connect() as conn:
                self._record_audit_event(
                    conn,
                    event_type='official_group_approval_decision_skipped',
                    event_source='official_group_phone_approval_decision',
                    payload={
                        **check_result,
                        'decision': decision,
                        'target_requester_id': payload.target_requester_id,
                        'target_name_hint': payload.target_name_hint,
                        'remark': payload.remark,
                    },
                    lead_id=None,
                )
                conn.commit()
            return {
                'lead_id': None,
                'target_group': payload.target_group,
                'decision': decision,
                'executed': False,
                **check_result,
            }
        approval_run_id = f"official_group_approval_{uuid.uuid4().hex[:12]}"
        routed_runtime = self._resolve_whatsapp_approval_runtime_executor_from_hint(
            account_key=str(payload.approval_runtime_account_key or '').strip(),
            responsible_type='official_group',
            target_group=str(payload.target_group or '').strip(),
            binding_index=payload.approval_runtime_binding_index,
            binding_target=str(payload.approval_runtime_binding_target or '').strip(),
        ) or self._resolve_whatsapp_approval_runtime_executor(target_group=str(payload.target_group or '').strip(), responsible_type='official_group')
        if not routed_runtime and self.official_group_approval_executor is None:
            raise HTTPException(status_code=400, detail='official group approval executor not configured')
        if routed_runtime:
            runtime_executor = routed_runtime['executor']
            runtime_binding = routed_runtime.get('binding') or {}
            routed_runtime_state = dict(routed_runtime.get('runtime_state') or {})
            runtime_group_target = str(
                self._whatsapp_binding_runtime_group_id(runtime_binding)
                or routed_runtime.get('resolved_target_group')
                or payload.target_group
                or ''
            ).strip()
            runtime_context = {
                'registration_group': runtime_group_target,
                'group_id': runtime_group_target,
                'target_group': runtime_group_target,
                'decision': decision,
                'decided_at': payload.decided_at,
                'decided_by': payload.decided_by,
                'decided_by_name': payload.decided_by_name,
                'target_requester_id': str(payload.target_requester_id or '').strip() or None,
                'target_name_hint': str(payload.target_name_hint or '').strip() or None,
                'target_phone_hint': str(payload.target_phone_hint or '').strip() or None,
                'approved_count': 1,
                'area': 'Indonesia',
                'remark': payload.remark,
                'force_immediate': True,
                'approval_run_id': approval_run_id,
                'approval_runtime_route': {
                    'account_key': routed_runtime.get('account_key'),
                    'account_name': routed_runtime.get('account_name'),
                    'base_url': (routed_runtime.get('runtime_state') or {}).get('base_url'),
                    'binding': runtime_binding,
                    'responsible_type': 'official_group',
                    'resolved_group_target': runtime_group_target,
                },
            }
            baileys_account_id = str(
                runtime_binding.get('baileys_account_id')
                or runtime_binding.get('provider_account_id')
                or runtime_binding.get('account_id')
                or routed_runtime_state.get('baileys_account_id')
                or ''
            ).strip()
            if baileys_account_id:
                runtime_context['accountId'] = baileys_account_id
                runtime_context['baileys_account_id'] = baileys_account_id
            if hasattr(runtime_executor, 'official_group_approve'):
                executor_result = runtime_executor.official_group_approve(runtime_context) or {}
            else:
                executor_result = runtime_executor.approve(runtime_context) or {}
            resolved_target = runtime_group_target or str(payload.target_group or '').strip()
        else:
            executor_result = self.official_group_approval_executor.approve(
                target_group=str(payload.target_group or '').strip(),
                lead={},
                crm_snapshot=check_result.get('crm_snapshot') or {},
                task={},
            ) or {}
            resolved_target = str(payload.target_group or '').strip()
        executor_raw_result = dict(executor_result.get('raw_result') or {})
        executor_raw_result.setdefault('approval_run_id', approval_run_id)
        executor_raw_result.setdefault('target_group', resolved_target)
        executor_result = {**executor_result, 'raw_result': executor_raw_result}
        current_truth_write = self._write_official_group_executor_current_truth_from_result(
            routed_runtime=routed_runtime,
            target_group=resolved_target,
            executor_result=executor_result,
            source='official_group_phone_approval_decision',
            approval_run_id=approval_run_id,
        )
        display_name = self._resolve_official_group_display_name(
            target_group=resolved_target,
            raw_result=executor_raw_result,
        ) or str(payload.target_group or '').strip()
        executed = self._official_group_approval_executor_result_succeeded(executor_result)
        if executed:
            self._record_official_group_approval_batch_member(
                approval_run_id=approval_run_id,
                target_group=resolved_target,
                target_group_name=display_name,
                approved_at=decided_at,
                lead={},
                payload=payload,
                executor_result=executor_result,
                eligibility=check_result,
            )
        with self.db.connect() as conn:
            self._record_audit_event(
                conn,
                event_type='official_group_approval_decision_executed' if executed else 'official_group_approval_decision_failed',
                event_source='official_group_phone_approval_decision',
                payload={
                    'target_group': payload.target_group,
                    'decision': decision,
                    'eligibility': check_result,
                    'executor_result': executor_result,
                    'remark': payload.remark,
                },
                lead_id=None,
            )
            conn.commit()
        return {
            'lead_id': None,
            'target_group': payload.target_group,
            'decision': decision,
            'executed': executed,
            'approval_run_id': approval_run_id,
            'eligible': True,
            'reason_code': check_result.get('reason_code'),
            'next_action': 'close_or_education' if executed else 'manual_review_official_group_approval',
            'executor_result': executor_result,
            'eligibility': check_result,
            'current_truth_write': current_truth_write,
        }

    def official_group_approval_decision(self, payload: OfficialGroupApprovalDecisionRequest) -> Dict[str, Any]:
        decision = str(payload.decision or 'approve').strip().lower() or 'approve'
        if decision != 'approve':
            raise HTTPException(status_code=400, detail='unsupported decision')
        if not str(payload.lead_id or '').strip():
            return self._official_group_approval_decision_by_phone(payload)
        check_result = self.official_group_approval_check(
            OfficialGroupApprovalCheckRequest(
                lead_id=payload.lead_id,
                target_group=payload.target_group,
                checked_at=payload.decided_at,
                checked_by=payload.decided_by,
                checked_by_name=payload.decided_by_name,
                source_platform=payload.source_platform,
                source_campaign=payload.source_campaign,
                source_adset=payload.source_adset,
                source_ad=payload.source_ad,
                target_phone_hint=payload.target_phone_hint,
                target_requester_id=payload.target_requester_id,
                target_requester_pending_hint=payload.target_requester_pending_hint,
                remark=payload.remark,
            )
        )
        if not check_result.get('eligible'):
            with self.db.connect() as conn:
                self._record_audit_event(
                    conn,
                    event_type='official_group_approval_decision_skipped',
                    event_source='official_group_approval_decision',
                    payload={
                        **check_result,
                        'decision': decision,
                        'decided_by': payload.decided_by,
                        'decided_by_name': payload.decided_by_name,
                        'remark': payload.remark,
                    },
                    lead_id=str(payload.lead_id or '').strip() or None,
                )
                conn.commit()
            return {
                'lead_id': payload.lead_id,
                'target_group': payload.target_group,
                'decision': decision,
                'executed': False,
                **check_result,
            }
        decided_at = parse_iso_datetime(payload.decided_at).isoformat()
        with self.db.connect() as conn:
            lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (payload.lead_id,)).fetchone()
            if not lead_row:
                raise HTTPException(status_code=404, detail='lead not found')
            lead = dict(lead_row)
            task = self._latest_group_join_task(conn, lead_id=str(payload.lead_id or '').strip())
            if not task:
                self._queue_group_join_after_verified_crm(
                    conn,
                    lead_id=str(payload.lead_id or '').strip(),
                    submission_id=None,
                    account_id=str(check_result.get('closing_record_yw_id') or lead.get('yw_id') or '').strip() or None,
                    created_at=decided_at,
                )
                task = self._latest_group_join_task(conn, lead_id=str(payload.lead_id or '').strip())
            if not task:
                raise HTTPException(status_code=400, detail='group_join task not found for lead')
        approval_run_id = f"official_group_approval_{uuid.uuid4().hex[:12]}"
        resolved_official_group_target = str(payload.target_group or '').strip()
        routed_runtime = self._resolve_whatsapp_approval_runtime_executor_from_hint(
            account_key=str(payload.approval_runtime_account_key or '').strip(),
            responsible_type='official_group',
            target_group=str(payload.target_group or '').strip(),
            binding_index=payload.approval_runtime_binding_index,
            binding_target=str(payload.approval_runtime_binding_target or '').strip(),
        ) or self._resolve_whatsapp_approval_runtime_executor(target_group=str(payload.target_group or '').strip(), responsible_type='official_group')
        if routed_runtime:
            runtime_executor = routed_runtime['executor']
            runtime_binding = routed_runtime.get('binding') or {}
            routed_runtime_state = dict(routed_runtime.get('runtime_state') or {})
            runtime_supports_official_group = hasattr(runtime_executor, 'official_group_approve')
            runtime_group_target = str(
                self._whatsapp_binding_runtime_group_id(runtime_binding)
                or routed_runtime.get('resolved_target_group')
                or payload.target_group
                or ''
            ).strip()
            if (
                not runtime_supports_official_group
                and str(payload.target_group or '').strip().startswith('official-group-')
            ):
                runtime_group_target = str(payload.target_group or '').strip()
            resolved_official_group_target = runtime_group_target or resolved_official_group_target
            runtime_context = {
                'registration_group': runtime_group_target,
                'group_id': runtime_group_target,
                'target_group': runtime_group_target,
                'decision': decision,
                'decided_at': payload.decided_at,
                'decided_by': payload.decided_by,
                'decided_by_name': payload.decided_by_name,
                'source_platform': payload.source_platform,
                'source_campaign': payload.source_campaign,
                'source_adset': payload.source_adset,
                'source_ad': payload.source_ad,
                'target_requester_id': str(payload.target_requester_id or '').strip() or None,
                'target_name_hint': str(payload.target_name_hint or lead.get('name') or lead.get('full_name') or '').strip() or None,
                'target_phone_hint': str(payload.target_phone_hint or lead.get('mobile') or '').strip() or None,
                'approved_count': 1,
                'area': str(lead.get('country') or lead.get('area') or 'Indonesia').strip() or 'Indonesia',
                'remark': payload.remark,
                'force_immediate': True,
                'approval_run_id': approval_run_id,
                'approval_runtime_route': {
                    'account_key': routed_runtime.get('account_key'),
                    'account_name': routed_runtime.get('account_name'),
                    'base_url': (routed_runtime.get('runtime_state') or {}).get('base_url'),
                    'binding': runtime_binding,
                    'responsible_type': 'official_group',
                    'resolved_group_target': runtime_group_target,
                },
            }
            baileys_account_id = str(
                runtime_binding.get('baileys_account_id')
                or runtime_binding.get('provider_account_id')
                or runtime_binding.get('account_id')
                or routed_runtime_state.get('baileys_account_id')
                or ''
            ).strip()
            if baileys_account_id:
                runtime_context['accountId'] = baileys_account_id
                runtime_context['baileys_account_id'] = baileys_account_id
            if runtime_supports_official_group:
                executor_result = runtime_executor.official_group_approve(runtime_context) or {}
            else:
                executor_result = runtime_executor.approve(runtime_context) or {}
        else:
            if self.official_group_approval_executor is None:
                raise HTTPException(status_code=400, detail='official group approval executor not configured')
            executor_result = self.official_group_approval_executor.approve(
                target_group=str(payload.target_group or '').strip(),
                lead=lead,
                crm_snapshot=check_result.get('crm_snapshot') or {},
                task=task,
            ) or {}
        executor_raw_result = dict(executor_result.get('raw_result') or {})
        executor_raw_result.setdefault('approval_run_id', approval_run_id)
        executor_raw_result.setdefault('target_group', resolved_official_group_target or str(payload.target_group or '').strip())
        executor_result = {**executor_result, 'raw_result': executor_raw_result}
        current_truth_write = self._write_official_group_executor_current_truth_from_result(
            routed_runtime=routed_runtime,
            target_group=resolved_official_group_target or str(payload.target_group or '').strip(),
            executor_result=executor_result,
            source='official_group_approval_decision',
            approval_run_id=approval_run_id,
        )
        official_group_display_name_for_result = self._resolve_official_group_display_name(
            target_group=resolved_official_group_target or str(payload.target_group or '').strip(),
            raw_result=executor_raw_result,
        ) or str(payload.target_group or '').strip()
        execution_disposition = str(executor_raw_result.get('execution_disposition') or '').strip().lower()
        retryable = bool(executor_raw_result.get('retryable'))
        requires_human_action = bool(executor_raw_result.get('requires_human_action'))
        human_action_type = None
        if execution_disposition == 'retryable_failed' or retryable:
            follow_up_action = 'retry_official_group_approval'
            retryable = True
            requires_human_action = False
        elif execution_disposition == 'manual_required' or requires_human_action:
            follow_up_action = 'manual_continue_official_group_approval'
            requires_human_action = True
            retryable = False
            lowered_reason = f"{executor_result.get('result_code') or ''} {executor_result.get('result_reason') or ''}".lower()
            if 'captcha' in lowered_reason:
                human_action_type = 'captcha_required'
            elif 'auth' in lowered_reason or 'login' in lowered_reason:
                human_action_type = 'auth_required'
            elif 'session' in lowered_reason or 'expired' in lowered_reason:
                human_action_type = 'session_expired'
            else:
                human_action_type = 'manual_continue_required'
        else:
            follow_up_action = 'close_or_education' if str(executor_result.get('status') or '').strip().lower() == 'success' else 'queue_reengagement'
        group_join_payload = GroupJoinResultRequest(
            status=str(executor_result.get('status') or 'failed'),
            result_code=executor_result.get('result_code'),
            result_reason=executor_result.get('result_reason'),
            finished_at=decided_at,
            raw_result={
                **dict(executor_result.get('raw_result') or {}),
                'target_group': str(payload.target_group or '').strip(),
                'group_name': official_group_display_name_for_result,
                'decision': decision,
                'decided_by': payload.decided_by,
                'decided_by_name': payload.decided_by_name,
            },
        )
        decision_result = self.group_join_result(task['task_id'], group_join_payload)
        if self._official_group_approval_executor_result_succeeded(executor_result):
            self._record_official_group_approval_batch_member(
                approval_run_id=approval_run_id,
                target_group=resolved_official_group_target or str(payload.target_group or '').strip(),
                target_group_name=official_group_display_name_for_result,
                approved_at=decided_at,
                lead=lead,
                payload=payload,
                executor_result=executor_result,
                eligibility=check_result,
            )
        with self.db.connect() as conn:
            self._record_audit_event(
                conn,
                event_type='official_group_approval_decision_executed',
                event_source='official_group_approval_decision',
                payload={
                    'lead_id': payload.lead_id,
                    'target_group': payload.target_group,
                    'decision': decision,
                    'task_id': task['task_id'],
                    'eligibility': check_result,
                    'executor_result': executor_result,
                    'decision_result': decision_result,
                    'follow_up_action': follow_up_action,
                    'retryable': retryable,
                    'requires_human_action': requires_human_action,
                    'human_action_type': human_action_type,
                    'remark': payload.remark,
                },
                lead_id=str(payload.lead_id or '').strip() or None,
            )
            conn.commit()
        return {
            'lead_id': payload.lead_id,
            'target_group': payload.target_group,
            'decision': decision,
            'executed': True,
            'task_id': task['task_id'],
            'approval_run_id': approval_run_id,
            'eligible': True,
            'reason_code': check_result.get('reason_code'),
            'next_action': decision_result.get('next_action'),
            'follow_up_action': follow_up_action,
            'retryable': retryable,
            'requires_human_action': requires_human_action,
            'human_action_type': human_action_type,
            'executor_result': executor_result,
            'decision_result': decision_result,
            'current_truth_write': current_truth_write,
        }

    def retry_official_group_approval(self, lead_id: str, payload: OfficialGroupApprovalRetryRequest) -> Dict[str, Any]:
        normalized_lead_id = str(lead_id or '').strip()
        if not normalized_lead_id:
            raise HTTPException(status_code=400, detail='lead_id is required')
        return self.official_group_approval_decision(
            OfficialGroupApprovalDecisionRequest(
                lead_id=normalized_lead_id,
                target_group=payload.target_group,
                decision='approve',
                decided_at=payload.decided_at,
                decided_by=payload.decided_by,
                decided_by_name=payload.decided_by_name,
                source_platform=payload.source_platform,
                source_campaign=payload.source_campaign,
                source_adset=payload.source_adset,
                source_ad=payload.source_ad,
                remark=payload.remark,
            )
        )

    def official_group_approval_executor_health(self) -> Dict[str, Any]:
        executor = self.official_group_approval_executor
        if executor is None:
            return {
                'configured': False,
                'status': 'unconfigured',
                'provider': None,
                'supports': [],
            }
        health_fn = getattr(executor, 'health', None)
        if callable(health_fn):
            snapshot = health_fn() or {}
            return {
                'configured': True,
                'status': str(snapshot.get('status') or 'unknown'),
                'provider': snapshot.get('provider'),
                'supports': list(snapshot.get('supports') or []),
                'schema_version': snapshot.get('schema_version'),
                'details': snapshot,
            }
        return {
            'configured': True,
            'status': 'unknown',
            'provider': executor.__class__.__name__,
            'supports': ['approve'] if hasattr(executor, 'approve') else [],
        }

    def official_group_approval_executor_warmup(self) -> Dict[str, Any]:
        executor = self.official_group_approval_executor
        if executor is None:
            return {
                'configured': False,
                'status': 'unconfigured',
                'provider': None,
                'supports': [],
                'warmed': False,
            }
        if hasattr(executor, 'warmup') and callable(getattr(executor, 'warmup')):
            try:
                result = executor.warmup() or {}
                if isinstance(result, dict):
                    result.setdefault('warmed', bool(result.get('status') == 'warm'))
                    result.setdefault('supports', result.get('supports') or [])
                    return result
            except Exception as exc:
                return {
                    'configured': True,
                    'status': 'error',
                    'provider': type(executor).__name__,
                    'supports': [],
                    'warmed': False,
                    'error': str(exc),
                }
        health = self.official_group_approval_executor_health()
        health['warmed'] = False
        health['warmup_supported'] = False
        return health

    def _official_group_summary_bucket(self) -> Dict[str, int]:
        return {
            'approved_count': 0,
            'failed_count': 0,
            'skipped_duplicate_count': 0,
            'retryable_failed_count': 0,
            'manual_required_count': 0,
        }

    def _normalize_official_group_summary_target_group(self, value: Any) -> str:
        normalized = str(value or '').strip()
        return normalized if normalized.startswith('official-group-') else ''

    def _resolve_group_join_task_target_group(self, row: Dict[str, Any]) -> str:
        payload: Dict[str, Any] = {}
        raw_result: Dict[str, Any] = {}
        try:
            payload = json.loads(row.get('payload') or '{}') if isinstance(row.get('payload'), str) else (row.get('payload') or {})
        except Exception:
            payload = {}
        try:
            raw_result = json.loads(row.get('raw_result') or '{}') if isinstance(row.get('raw_result'), str) else (row.get('raw_result') or {})
        except Exception:
            raw_result = {}
        return self._normalize_official_group_summary_target_group(
            row.get('target_group')
            or payload.get('target_group')
            or raw_result.get('target_group')
            or ''
        )

    def _fetch_official_group_bridge_pending_counts(self) -> Optional[Dict[str, Any]]:
        executor = self.official_group_approval_executor
        if executor is None:
            return None
        webhook_url = str(getattr(executor, 'webhook_url', '') or '').strip()
        if not webhook_url:
            try:
                health = executor.health() if hasattr(executor, 'health') else {}
            except Exception:
                health = {}
            webhook_url = str((health or {}).get('webhook_url') or '').strip()
        if not webhook_url or '/official-group/approve' not in webhook_url:
            return None
        base_url = webhook_url.split('/official-group/approve', 1)[0].rstrip('/')
        if not base_url:
            return None
        try:
            summary = fetch_json(f'{base_url}/ops/official-group-bridge/summary', timeout=5.0)
        except Exception:
            return None
        by_target_group_raw = summary.get('by_target_group') or {}
        if not isinstance(by_target_group_raw, dict):
            by_target_group_raw = {}
        by_target_group: Dict[str, Dict[str, int]] = {}
        total_pending = 0
        for target_group, bucket in by_target_group_raw.items():
            normalized_target = str(target_group or '').strip()
            if not normalized_target:
                continue
            pending_value = 0
            if isinstance(bucket, dict):
                try:
                    pending_value = max(int(bucket.get('pending_count') or 0), 0)
                except Exception:
                    pending_value = 0
            total_pending += pending_value
            by_target_group[normalized_target] = {'pending_count': pending_value}
        return {
            'pending_count': total_pending,
            'by_target_group': by_target_group,
        }

    def _has_active_official_group_monitor_config(self) -> bool:
        try:
            try:
                payload = self.list_whatsapp_approval_accounts(lightweight=True) or {}
            except TypeError:
                payload = self.list_whatsapp_approval_accounts() or {}
            rows = payload.get('rows') or payload.get('accounts') or [] if isinstance(payload, dict) else []
        except Exception:
            return False
        for account in rows:
            if not isinstance(account, dict):
                continue
            if str(account.get('responsible_type') or '').strip() != 'official_group':
                continue
            if account.get('enabled') is False:
                continue
            bindings = account.get('group_link_bindings') if isinstance(account.get('group_link_bindings'), list) else []
            for binding in bindings:
                if isinstance(binding, dict) and binding.get('enabled') is not False:
                    return True
        return False

    def official_group_approval_summary(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            fallback_pending_count = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE current_status IN ('bind_success', 'group_join_pending', 'group_join_failed') AND current_status NOT IN ('archived_test_residue', 'console_cleared_test_data')"
            ).fetchone()[0]
            latest_task_rows = [dict(row) for row in conn.execute(
                """
                WITH ranked AS (
                    SELECT t.task_id, t.lead_id, t.status, t.payload, t.raw_result, t.created_at,
                           COALESCE(l.current_status, '') AS lead_current_status,
                           ROW_NUMBER() OVER (
                               PARTITION BY t.lead_id
                               ORDER BY datetime(COALESCE(t.finished_at, t.created_at)) DESC, datetime(t.created_at) DESC, t.task_id DESC
                           ) AS rn
                    FROM automation_tasks t
                    LEFT JOIN leads l ON l.lead_id = t.lead_id
                    WHERE t.task_type = 'group_join'
                )
                SELECT task_id, lead_id, status, payload, raw_result, created_at, lead_current_status
                FROM ranked
                WHERE rn = 1
                """
            ).fetchall()]
            skipped_rows = [dict(row) for row in conn.execute(
                """
                SELECT al.payload, COALESCE(l.current_status, '') AS lead_current_status
                FROM operator_audit_log al
                LEFT JOIN leads l ON l.lead_id = al.lead_id
                WHERE al.event_type = 'official_group_approval_decision_skipped'
                ORDER BY al.created_at DESC
                """
            ).fetchall()]

        runtime_rows = self._official_group_runtime_queue_rows(now_iso=utc_now())
        official_monitor_configured = self._has_active_official_group_monitor_config()
        pending_count = int(sum(max(int(row.get('pending_count') or 0), 0) for row in runtime_rows)) if official_monitor_configured else 0
        bridge_pending = self._fetch_official_group_bridge_pending_counts()
        if not official_monitor_configured and bridge_pending is None:
            return {
                'view_scope': 'current_active_scope',
                'pending_count': 0,
                'approved_count': 0,
                'failed_count': 0,
                'skipped_duplicate_count': 0,
                'retryable_failed_count': 0,
                'manual_required_count': 0,
                'by_target_group': {},
            }

        active_target_groups: set[str] = set()
        for row in runtime_rows:
            normalized_target = self._normalize_official_group_summary_target_group(row.get('target_group'))
            if normalized_target:
                active_target_groups.add(normalized_target)
        if bridge_pending is not None:
            for target_group, pending_bucket in (bridge_pending.get('by_target_group') or {}).items():
                normalized_target = self._normalize_official_group_summary_target_group(target_group)
                if not normalized_target:
                    continue
                if max(int((pending_bucket or {}).get('pending_count') or 0), 0) > 0:
                    active_target_groups.add(normalized_target)

        scoped_summary = bool(active_target_groups)
        by_target_group: Dict[str, Dict[str, int]] = {}

        for row in latest_task_rows:
            if str(row.get('lead_current_status') or '').strip().lower() in IGNORED_HISTORY_LEAD_STATUSES:
                continue
            target_group = self._resolve_group_join_task_target_group(row)
            if not target_group:
                continue
            if scoped_summary and target_group not in active_target_groups:
                continue
            by_target_group.setdefault(target_group, self._official_group_summary_bucket())
            status = str(row.get('status') or '').strip().lower()
            try:
                parsed_raw = json.loads(row.get('raw_result') or '{}') if isinstance(row.get('raw_result'), str) else (row.get('raw_result') or {})
            except Exception:
                parsed_raw = {}
            disposition = str(parsed_raw.get('execution_disposition') or '').strip().lower()
            if status == 'success':
                by_target_group[target_group]['approved_count'] += 1
                continue
            if status != 'failed':
                continue
            if disposition == 'retryable_failed':
                by_target_group[target_group]['retryable_failed_count'] += 1
            elif disposition == 'manual_required':
                by_target_group[target_group]['manual_required_count'] += 1
            else:
                by_target_group[target_group]['failed_count'] = int(by_target_group[target_group].get('failed_count') or 0) + 1

        for row in skipped_rows:
            if str(row.get('lead_current_status') or '').strip().lower() in IGNORED_HISTORY_LEAD_STATUSES:
                continue
            try:
                payload = json.loads(row['payload'] or '{}')
            except Exception:
                payload = {}
            if str(payload.get('reason_code') or '') != 'already_in_target_group':
                continue
            target_group = self._normalize_official_group_summary_target_group(payload.get('target_group'))
            if not target_group:
                continue
            if scoped_summary and target_group not in active_target_groups:
                continue
            by_target_group.setdefault(target_group, self._official_group_summary_bucket())
            by_target_group[target_group]['skipped_duplicate_count'] += 1

        if bridge_pending is not None:
            for target_group, bucket in list(by_target_group.items()):
                pending_bucket = (bridge_pending.get('by_target_group') or {}).get(target_group) or {}
                bucket['manual_required_count'] = max(int(pending_bucket.get('pending_count') or 0), 0)
            if not scoped_summary:
                for target_group, pending_bucket in (bridge_pending.get('by_target_group') or {}).items():
                    normalized_target = self._normalize_official_group_summary_target_group(target_group)
                    if not normalized_target:
                        continue
                    by_target_group.setdefault(normalized_target, self._official_group_summary_bucket())
                    by_target_group[normalized_target]['manual_required_count'] = max(int((pending_bucket or {}).get('pending_count') or 0), 0)

        filtered_by_target_group = {}
        for target_group, bucket in by_target_group.items():
            normalized_bucket = {
                'approved_count': int(bucket.get('approved_count') or 0),
                'failed_count': int(bucket.get('failed_count') or 0),
                'skipped_duplicate_count': int(bucket.get('skipped_duplicate_count') or 0),
                'retryable_failed_count': int(bucket.get('retryable_failed_count') or 0),
                'manual_required_count': int(bucket.get('manual_required_count') or 0),
            }
            if any(normalized_bucket.values()):
                filtered_by_target_group[target_group] = normalized_bucket

        approved_count = sum(bucket['approved_count'] for bucket in filtered_by_target_group.values())
        failed_count = sum(bucket['failed_count'] for bucket in filtered_by_target_group.values())
        skipped_duplicate_count = sum(bucket['skipped_duplicate_count'] for bucket in filtered_by_target_group.values())
        retryable_failed_count = sum(bucket['retryable_failed_count'] for bucket in filtered_by_target_group.values())
        manual_required_count = sum(bucket['manual_required_count'] for bucket in filtered_by_target_group.values())
        return {
            'view_scope': 'current_active_scope',
            'pending_count': int(pending_count or 0),
            'approved_count': approved_count,
            'failed_count': failed_count,
            'skipped_duplicate_count': skipped_duplicate_count,
            'retryable_failed_count': retryable_failed_count,
            'manual_required_count': manual_required_count,
            'by_target_group': filtered_by_target_group,
        }

    @staticmethod
    def _official_group_requester_has_matchable_identity(requester: Dict[str, Any]) -> bool:
        if not isinstance(requester, dict):
            return False
        for key in ('phoneNormalized', 'phone_normalized', 'phoneRaw', 'phone_raw', 'debugLidPhoneRaw', 'debugContactNumberRaw', 'waId', 'wa_id'):
            if str(requester.get(key) or '').strip():
                return True
        requester_id = str(requester.get('requesterId') or requester.get('requester_id') or '').strip()
        return bool(requester_id and not requester_id.lower().endswith('@lid'))

    @classmethod
    def _official_group_requesters_precise_enough(cls, requesters: List[Dict[str, Any]], release_count: int) -> bool:
        needed = max(int(release_count or 0), 0)
        if needed <= 0:
            return False
        usable = [dict(item) for item in list(requesters or []) if isinstance(item, dict)]
        if len(usable) < needed:
            return False
        for requester in usable[:needed]:
            requester_id = str(requester.get('requesterId') or requester.get('requester_id') or '').strip()
            if not requester_id:
                return False
            if not cls._official_group_requester_has_matchable_identity(requester):
                return False
        return True

    @staticmethod
    def _official_group_requesters_from_truth_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        truth_view = dict(payload.get('approval_queue_truth') or {}) if isinstance(payload.get('approval_queue_truth'), dict) else {}
        current_truth = dict(truth_view.get('current_truth') or {}) if isinstance(truth_view.get('current_truth'), dict) else {}
        current_truth_raw = dict(truth_view.get('current_truth_raw') or {}) if isinstance(truth_view.get('current_truth_raw'), dict) else {}
        for source in (
            payload.get('requesters'),
            truth_view.get('requesters'),
            current_truth.get('requesters'),
            current_truth_raw.get('requesters'),
        ):
            if isinstance(source, list) and source:
                requesters = [dict(item) for item in source if isinstance(item, dict)]
                if requesters:
                    return requesters
        requester_ids: List[str] = []
        for source in (
            payload.get('requester_ids'),
            payload.get('requesterIds'),
            truth_view.get('requester_ids'),
            current_truth.get('requester_ids'),
            current_truth_raw.get('requester_ids'),
        ):
            if isinstance(source, list) and source:
                requester_ids.extend(str(item).strip() for item in source if str(item).strip())
        return [{'requesterId': item} for item in list(dict.fromkeys(requester_ids))]

    def _ensure_ready_official_group_precise_requesters(self, group: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(group or {})
        try:
            release_count = max(int(row.get('release_count') or row.get('pending_count') or 0), 0)
        except Exception:
            release_count = 0
        requesters = [dict(item) for item in (row.get('requesters') or []) if isinstance(item, dict)] if isinstance(row.get('requesters'), list) else []
        if self._official_group_requesters_precise_enough(requesters, release_count):
            return row
        account_key = str(row.get('account_key') or '').strip()
        binding_index = normalize_int_or_none(row.get('binding_index'))
        if not account_key or binding_index is None:
            row['precise_requester_sync'] = {
                'attempted': False,
                'reason': 'binding_route_missing',
            }
            return row
        try:
            sync_result = self.full_sync_whatsapp_approval_binding(
                account_key,
                int(binding_index),
                source='official_ready_precise_sync',
                timeout_seconds=30.0,
                _skip_operation_lock=True,
            )
        except Exception as exc:
            row['precise_requester_sync'] = {
                'attempted': True,
                'ok': False,
                'reason': 'precise_sync_failed',
                'error': str(exc)[:240],
            }
            return row
        synced_requesters = self._official_group_requesters_from_truth_payload(sync_result)
        if synced_requesters:
            row['requesters'] = synced_requesters
            row['requester_ids'] = [
                str(item.get('requesterId') or item.get('requester_id') or '').strip()
                for item in synced_requesters
                if str(item.get('requesterId') or item.get('requester_id') or '').strip()
            ]
        synced_pending_count = normalize_int_or_none(sync_result.get('pending_count') or sync_result.get('trusted_pending_count') or sync_result.get('ui_pending_count') or sync_result.get('api_pending_count'))
        if synced_pending_count is not None:
            row['pending_count'] = max(int(synced_pending_count), 0)
            if release_count > 0:
                row['release_count'] = min(release_count, row['pending_count'])
        row['precise_requester_sync'] = {
            'attempted': True,
            'ok': self._official_group_requesters_precise_enough(row.get('requesters') or [], int(row.get('release_count') or release_count or 0)),
            'trust_status': sync_result.get('trust_status'),
            'reason_code': sync_result.get('reason_code'),
            'requester_count': len(row.get('requesters') or []),
        }
        return row

    def run_ready_official_group_batches(self, payload: OfficialGroupBatchRunRequest) -> Dict[str, Any]:
        total_started_at = time.perf_counter()
        timings: Dict[str, Any] = {
            'queue_ms': 0.0,
            'ready_group_prepare_ms': 0.0,
            'match_ms': 0.0,
            'approval_ms': 0.0,
            'notification_ms': 0.0,
            'per_group': [],
        }
        now_iso = parse_iso_datetime(payload.decided_at).isoformat()
        queue_started_at = time.perf_counter()
        batch_queue = self.approval_batch_queue()
        timings['queue_ms'] = round((time.perf_counter() - queue_started_at) * 1000, 1)
        prepare_started_at = time.perf_counter()
        ready_groups = []
        filter_account_key = str(getattr(payload, 'account_key', None) or '').strip()
        filter_binding_index = normalize_int_or_none(getattr(payload, 'binding_index', None))
        filter_registration_group = str(getattr(payload, 'registration_group', None) or '').strip()
        filter_target_group = str(getattr(payload, 'target_group', None) or '').strip()
        for row in list(batch_queue.get('official_groups') or []):
            if not isinstance(row, dict):
                continue
            if bool(row.get('ready')):
                if filter_account_key and str(row.get('account_key') or '').strip() != filter_account_key:
                    continue
                if filter_binding_index is not None and normalize_int_or_none(row.get('binding_index')) != filter_binding_index:
                    continue
                if filter_registration_group and filter_registration_group not in {
                    str(row.get('registration_group') or '').strip(),
                    str(row.get('binding_registration_group') or '').strip(),
                    str(row.get('group_id') or '').strip(),
                    str(row.get('target_group') or '').strip(),
                }:
                    continue
                if filter_target_group and filter_target_group not in {
                    str(row.get('target_group') or '').strip(),
                    str(row.get('group_id') or '').strip(),
                    str(row.get('binding_registration_group') or '').strip(),
                    str(row.get('registration_group') or '').strip(),
                }:
                    continue
                ready_groups.append(row)
        ready_groups = [self._ensure_ready_official_group_precise_requesters(row) for row in ready_groups[:max(1, int(payload.limit_groups or 10))]]
        timings['ready_group_prepare_ms'] = round((time.perf_counter() - prepare_started_at) * 1000, 1)
        official_statuses = ('bind_success', 'group_join_pending', 'group_join_failed', 'group_join_success', 'synced')
        results: list[dict[str, Any]] = []
        unresolved_count = 0
        executed_count = 0
        skipped_count = 0
        previous_suppress_success_notifications = bool(getattr(self, 'official_group_success_notifications_suppressed', False))
        self.official_group_success_notifications_suppressed = bool(payload.suppress_success_notifications)
        notification_results: list[dict[str, Any]] = []
        try:
            for group in ready_groups:
                group_started_at = time.perf_counter()
                group_timing: Dict[str, Any] = {
                    'registration_group': str(group.get('registration_group') or '').strip() or None,
                    'target_group': str(group.get('target_group') or '').strip() or None,
                    'requester_count': len(group.get('requesters') or []) if isinstance(group.get('requesters'), list) else 0,
                    'match_ms': 0.0,
                    'approval_ms': 0.0,
                }
                registration_group = str(group.get('registration_group') or '').strip()
                target_group_filter = str(group.get('target_group') or '').strip()
                requesters = list(group.get('requesters') or []) if isinstance(group.get('requesters'), list) else []
                release_count = int(group.get('release_count') or group.get('pending_count') or 0)
                approval_limit = release_count
                if payload.limit_leads_per_group is not None:
                    approval_limit = min(release_count, max(1, int(payload.limit_leads_per_group or 1)))
                match_started_at = time.perf_counter()
                lead_rows, unmatched_requesters = self._match_official_group_requesters_to_phone_records(
                    requesters=requesters,
                    release_count=release_count,
                )
                match_ms = round((time.perf_counter() - match_started_at) * 1000, 1)
                group_timing['match_ms'] = match_ms
                timings['match_ms'] = round(float(timings.get('match_ms') or 0.0) + match_ms, 1)
                if approval_limit >= 0:
                    lead_rows = lead_rows[:approval_limit]
                if target_group_filter:
                    for lead in lead_rows:
                        if isinstance(lead, dict):
                            lead['matched_official_target_group'] = target_group_filter
                allow_live_crm_phone_match = bool(
                    getattr(payload, 'allow_live_crm_phone_match', True)
                    or getattr(payload, 'allow_crm_only_test_match', False)
                )
                for unmatched in unmatched_requesters:
                    crm_phone_match_record = None
                    if allow_live_crm_phone_match and len(lead_rows) < approval_limit:
                        crm_row, _ = self._find_crm_customer_for_official_group_requester(unmatched)
                        if crm_row:
                            crm_phone_match_record = {
                                'lead_id': None,
                                'mobile': str(crm_row.get('mobile') or '').strip(),
                                'area_code': 0,
                                'country': '',
                                'yw_id': str(crm_row.get('ywId') or '').strip(),
                                'app_name': str(crm_row.get('appName') or '').strip(),
                                'dept_name': str(crm_row.get('deptName') or '').strip(),
                                'pendaftaran_group': str(crm_row.get('pendaftaranGroup') or '').strip(),
                                'matched_customer_id': str(crm_row.get('id') or '').strip(),
                                'current_status': 'crm_phone_matched',
                                'crm_phone_match_target_group': target_group_filter,
                                'matched_requester_id': str(unmatched.get('requester_id') or unmatched.get('requesterId') or '').strip() or None,
                                'matched_requester_phone_hint': str(unmatched.get('phone_normalized') or unmatched.get('phone_raw') or unmatched.get('debugLidPhoneRaw') or '').strip() or None,
                                'matched_requester_name_hint': str(unmatched.get('display_name') or unmatched.get('displayName') or '').strip() or None,
                            }
                    if crm_phone_match_record:
                        lead_rows.append(crm_phone_match_record)
                        continue
                    skipped_count += 1
                    detail = {
                        'registration_group': registration_group,
                        'target_group': target_group_filter or None,
                        'reason_code': 'official_group_requester_phone_unmatched',
                        'next_action': 'manual_review_official_group_approval',
                        'requester': unmatched,
                        'mobile': str(
                            (unmatched or {}).get('phoneNormalized')
                            or (unmatched or {}).get('phone_normalized')
                            or (unmatched or {}).get('phoneRaw')
                            or (unmatched or {}).get('phone_raw')
                            or (unmatched or {}).get('debugLidPhoneRaw')
                            or ''
                        ).strip() or None,
                    }
                    if target_group_filter and not bool(payload.suppress_success_notifications):
                        try:
                            detail.update(self._group_approval_executor_lookup_snapshot(
                                approval_scope='official_group',
                                target_group=target_group_filter,
                                requester_id=str((unmatched or {}).get('requester_id') or (unmatched or {}).get('requesterId') or '').strip() or None,
                                phone_hint=detail.get('mobile'),
                                name_hint=str((unmatched or {}).get('display_name') or (unmatched or {}).get('displayName') or '').strip() or None,
                            ))
                        except Exception as exc:
                            detail['runtime_lookup_error'] = str(exc)
                    results.append(detail)
                for lead_row in lead_rows:
                    lead = dict(lead_row)
                    target_group = str(
                        lead.get('crm_phone_match_target_group')
                        or lead.get('matched_official_target_group')
                        or self._resolve_official_group_target_group(lead=lead)
                        or ''
                    ).strip()
                    if not target_group:
                        unresolved_count += 1
                        detail = {
                            'lead_id': lead.get('lead_id'),
                            'registration_group': registration_group,
                            'reason_code': 'official_group_target_unresolved',
                            'next_action': 'configure_official_group_target_mapping',
                        }
                        with self.db.connect() as conn:
                            self._record_audit_event(
                                conn,
                                event_type='official_group_approval_target_unresolved',
                                event_source='official_group_batch_runner',
                                payload=detail,
                                lead_id=str(lead.get('lead_id') or '').strip() or None,
                            )
                            conn.commit()
                        results.append(detail)
                        continue
                    approval_started_at = time.perf_counter()
                    result = self.official_group_approval_decision(
                        OfficialGroupApprovalDecisionRequest(
                            lead_id=None,
                            target_group=target_group,
                            decision='approve',
                            decided_at=now_iso,
                            decided_by=payload.decided_by,
                            decided_by_name=payload.decided_by_name,
                            source_platform=payload.source_platform,
                            source_campaign=payload.source_campaign,
                            source_adset=payload.source_adset,
                            source_ad=payload.source_ad,
                            remark=payload.remark,
                            target_name_hint=str(lead.get('matched_requester_name_hint') or '').strip() or None,
                            target_phone_hint=str(lead.get('matched_requester_phone_hint') or '').strip() or None,
                            target_requester_id=str(lead.get('matched_requester_id') or '').strip() or None,
                            target_requester_pending_hint=True if requesters else None,
                            approval_runtime_account_key=str(group.get('account_key') or '').strip() or None,
                            approval_runtime_binding_index=normalize_int_or_none(group.get('binding_index')),
                            approval_runtime_binding_target=str(
                                group.get('binding_registration_group')
                                or group.get('binding_link')
                                or group.get('registration_group')
                                or target_group_filter
                                or ''
                            ).strip() or None,
                        )
                    )
                    approval_ms = round((time.perf_counter() - approval_started_at) * 1000, 1)
                    group_timing['approval_ms'] = round(float(group_timing.get('approval_ms') or 0.0) + approval_ms, 1)
                    timings['approval_ms'] = round(float(timings.get('approval_ms') or 0.0) + approval_ms, 1)
                    if not result.get('executed') and str(result.get('next_action') or '').strip() == 'manual_review_official_group_approval':
                        result = {
                            **result,
                            'group_name': str(self._resolve_official_group_display_name(target_group=target_group) or target_group).strip() or target_group,
                            'mobile': str(lead.get('mobile') or lead.get('matched_requester_phone_hint') or '').strip() or None,
                        }
                        if bool(payload.suppress_success_notifications):
                            result['runtime_lookup_skipped'] = 'daemon_suppressed_batch'
                        else:
                            try:
                                result.update(self._group_approval_executor_lookup_snapshot(
                                    approval_scope='official_group',
                                    target_group=target_group,
                                    requester_id=str(result.get('target_requester_id') or lead.get('matched_requester_id') or '').strip() or None,
                                    phone_hint=str(result.get('target_phone_hint') or lead.get('mobile') or lead.get('matched_requester_phone_hint') or '').strip() or None,
                                    name_hint=str(result.get('target_name_hint') or lead.get('matched_requester_name_hint') or '').strip() or None,
                                ))
                            except Exception as exc:
                                result['runtime_lookup_error'] = str(exc)
                    results.append(result)
                    if result.get('executed'):
                        executed_count += 1
                    else:
                        skipped_count += 1
                group_timing['lead_match_count'] = len(lead_rows)
                group_timing['unmatched_count'] = len(unmatched_requesters)
                group_timing['total_ms'] = round((time.perf_counter() - group_started_at) * 1000, 1)
                timings['per_group'].append(group_timing)
            notification_started_at = time.perf_counter()
            notification_results = self._send_official_group_success_notifications(
                decided_at=now_iso,
                ready_groups=ready_groups,
                results=results,
            )
            timings['notification_ms'] = round((time.perf_counter() - notification_started_at) * 1000, 1)
        finally:
            self.official_group_success_notifications_suppressed = previous_suppress_success_notifications
        timings['total_ms'] = round((time.perf_counter() - total_started_at) * 1000, 1)
        return {
            'executed': True,
            'decided_at': now_iso,
            'ready_group_count': len(ready_groups),
            'executed_count': executed_count,
            'skipped_count': skipped_count,
            'unresolved_count': unresolved_count,
            'results': results,
            'notification_results': notification_results,
            'timings': timings,
        }

    def _official_group_has_abnormal_marker(self, lead: Dict[str, Any]) -> Tuple[bool, List[str]]:
        if not isinstance(lead, dict):
            return False, []
        reasons: List[str] = []
        current_status = str(lead.get('current_status') or '').strip().lower()
        review_status = str(lead.get('review_status') or '').strip().lower()
        routing_decision = str(lead.get('routing_decision') or '').strip().lower()
        if current_status == 'manual_review_pending':
            reasons.append('manual_review_pending')
        if review_status in {'pending', 'retry_requested', 'rejected'}:
            reasons.append(f'review_status:{review_status}')
        if routing_decision == 'manual_review':
            reasons.append('routing_decision:manual_review')
        return bool(reasons), reasons

    def _official_group_requester_pending_in_runtime(
        self,
        *,
        target_group: str,
        target_phone_hint: Optional[str] = None,
        target_requester_id: Optional[str] = None,
        area_code: Any = 0,
        country: Any = '',
    ) -> bool:
        routed_runtime = self._resolve_whatsapp_approval_runtime_executor(target_group=target_group, responsible_type='official_group')
        if not routed_runtime:
            return False
        runtime_state = dict(routed_runtime.get('runtime_state') or {})
        runtime_base_url = str(runtime_state.get('base_url') or '').strip()
        binding = dict(routed_runtime.get('binding') or {})
        probe_target = (
            str(binding.get('group_id') or '').strip()
            or str(binding.get('link') or '').strip()
            or str(binding.get('registration_group') or '').strip()
            or str(binding.get('group_name') or '').strip()
        )
        if not runtime_base_url or not probe_target:
            return False
        try:
            group_state = self._request_whatsapp_approval_group_state_with_retry(runtime_base_url, probe_target)
        except Exception:
            return False
        target_requester_id_normalized = str(target_requester_id or '').strip()
        target_phone_keys = self._official_group_phone_match_keys(
            phone=target_phone_hint,
            area_code=area_code,
            country=country,
        )
        for requester in list(group_state.get('requesters') or []):
            if not isinstance(requester, dict):
                continue
            requester_id = str(requester.get('requesterId') or '').strip()
            if target_requester_id_normalized and requester_id and requester_id == target_requester_id_normalized:
                return True
            requester_phone_keys = set()
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=requester.get('phoneNormalized')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=requester.get('phoneRaw')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=requester.get('debugLidPhoneRaw')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=requester.get('debugContactNumberRaw')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=self._official_group_requester_id_phone_candidate(requester_id)))
            if target_phone_keys and requester_phone_keys.intersection(target_phone_keys):
                return True
        return False

    def official_group_approval_check(self, payload: OfficialGroupApprovalCheckRequest) -> Dict[str, Any]:
        lead_id = str(payload.lead_id or '').strip()
        target_group = str(payload.target_group or '').strip()
        if not lead_id:
            raise HTTPException(status_code=400, detail='lead_id is required')
        if not target_group:
            raise HTTPException(status_code=400, detail='target_group is required')
        checked_at = parse_iso_datetime(payload.checked_at)
        checked_at_iso = checked_at.isoformat()
        with self.db.connect() as conn:
            lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if not lead_row:
                raise HTTPException(status_code=404, detail='lead not found')
            lead = dict(lead_row)
            current_status = str(lead.get('current_status') or '')
            crm_verified = bool(
                lead.get('crm_verified_at')
                or lead.get('crm_verified_payload')
                or lead.get('crm_verified_app_name')
                or lead.get('crm_verified_registration_group')
            )
            if not crm_verified and self._restore_verified_crm_state_from_sync_logs(conn, lead_id=lead_id):
                lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
                lead = dict(lead_row)
                current_status = str(lead.get('current_status') or '')
                crm_verified = bool(
                    lead.get('crm_verified_at')
                    or lead.get('crm_verified_payload')
                    or lead.get('crm_verified_app_name')
                    or lead.get('crm_verified_registration_group')
                )
            abnormal_flagged, abnormal_reasons = self._official_group_has_abnormal_marker(lead)
            result: Dict[str, Any] = {
                'lead_id': lead_id,
                'target_group': target_group,
                'checked_at': checked_at_iso,
                'checked_by': payload.checked_by,
                'checked_by_name': payload.checked_by_name,
                'current_status': current_status,
                'crm_verified': crm_verified,
                'crm_customer_found': False,
                'crm_snapshot': None,
                'eligible': False,
                'reason_code': 'unknown',
                'reason_detail': None,
                'next_action': 'manual_review_official_group_approval',
                'abnormal_flagged': abnormal_flagged,
                'abnormal_reasons': abnormal_reasons,
            }
            approval_requester_phone = str(payload.target_phone_hint or lead.get('mobile') or '').strip()
            closing_record_yw_id = self._resolve_official_group_closing_record_yw_id(
                conn,
                lead=lead,
                lead_id=lead_id,
            )
            if not closing_record_yw_id and self.crm_adapter is not None and approval_requester_phone:
                try:
                    requester_crm_row = self.crm_adapter.find_customer(mobile=approval_requester_phone)
                except Exception:
                    requester_crm_row = None
                if requester_crm_row:
                    closing_record_yw_id = str(requester_crm_row.get('ywId') or '').strip()
            result['approval_requester_phone'] = approval_requester_phone or None
            result['closing_record_yw_id'] = closing_record_yw_id or None
            if not approval_requester_phone:
                result.update({
                    'reason_code': 'approval_requester_phone_missing',
                    'reason_detail': 'WhatsApp approval requester phone is required for official-group approval.',
                    'next_action': 'manual_review_official_group_approval',
                })
            elif not closing_record_yw_id:
                result.update({
                    'reason_code': 'closing_record_id_missing',
                    'reason_detail': 'Closing record ID is required for official-group approval.',
                    'next_action': 'manual_review_official_group_approval',
                })
            elif self.crm_adapter is None:
                result.update({
                    'reason_code': 'crm_adapter_not_configured',
                    'reason_detail': 'CRM adapter is unavailable.',
                    'next_action': 'manual_review_official_group_approval',
                })
            else:
                if payload.target_requester_pending_hint is not None:
                    target_requester_still_pending = bool(payload.target_requester_pending_hint)
                else:
                    target_requester_still_pending = self._official_group_requester_pending_in_runtime(
                        target_group=target_group,
                        target_phone_hint=payload.target_phone_hint or lead.get('mobile'),
                        target_requester_id=payload.target_requester_id,
                        area_code=lead.get('area_code'),
                        country=lead.get('country'),
                    )
                result['target_requester_still_pending'] = target_requester_still_pending
                crm_row = self._find_existing_customer_with_fallback(
                    yw_id=closing_record_yw_id,
                    mobile=approval_requester_phone,
                    app_name=None,
                    dept_name=None,
                    registration_group=None,
                    official_group=None,
                )
                result['crm_customer_found'] = bool(crm_row)
                cached_verified_payload: Dict[str, Any] = {}
                try:
                    parsed_verified_payload = json.loads(lead.get('crm_verified_payload') or '{}')
                except Exception:
                    parsed_verified_payload = {}
                if isinstance(parsed_verified_payload, dict):
                    cached_verified_payload = parsed_verified_payload
                cached_snapshot = {
                    'id': cached_verified_payload.get('id') or lead.get('matched_customer_id'),
                    'mobile': cached_verified_payload.get('mobile') or lead.get('mobile'),
                    'ywId': cached_verified_payload.get('ywId') or lead.get('yw_id'),
                    'appName': cached_verified_payload.get('appName') or lead.get('crm_verified_app_name') or lead.get('app_name'),
                    'deptName': cached_verified_payload.get('deptName') or lead.get('crm_verified_dept_name') or lead.get('dept_name'),
                    'pendaftaranGroup': cached_verified_payload.get('pendaftaranGroup') or lead.get('crm_verified_registration_group') or lead.get('pendaftaran_group'),
                    'wa': cached_verified_payload.get('wa') or lead.get('crm_verified_official_group') or '',
                    'joinGroup': cached_verified_payload.get('joinGroup'),
                }
                if not crm_row:
                    try:
                        phone_only_row = self.crm_adapter.find_customer(mobile=approval_requester_phone)
                    except Exception:
                        phone_only_row = None
                    if phone_only_row:
                        result['crm_phone_match_snapshot'] = {
                            'id': phone_only_row.get('id'),
                            'mobile': phone_only_row.get('mobile'),
                            'ywId': phone_only_row.get('ywId'),
                        }
                if crm_row:
                    result['crm_snapshot'] = {
                        'id': crm_row.get('id'),
                        'mobile': crm_row.get('mobile'),
                        'ywId': crm_row.get('ywId'),
                        'appName': crm_row.get('appName'),
                        'deptName': crm_row.get('deptName'),
                        'pendaftaranGroup': crm_row.get('pendaftaranGroup'),
                        'wa': crm_row.get('wa'),
                        'joinGroup': crm_row.get('joinGroup'),
                        'source': 'live_crm',
                    }
                    self._record_verified_crm_state(
                        conn,
                        lead_id=lead_id,
                        crm_payload=crm_row,
                        official_group=str(crm_row.get('wa') or '').strip() or None,
                    )
                    lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
                    lead = dict(lead_row)
                elif crm_verified:
                    result['crm_snapshot'] = {
                        **cached_snapshot,
                        'source': 'local_verified_cache',
                    }
                if crm_row and self._official_group_value_matches_target(
                    value=crm_row.get('wa'),
                    target_group=target_group,
                ) and not target_requester_still_pending:
                    result.update({
                        'reason_code': 'already_in_target_group',
                        'reason_detail': 'CRM already points to the requested official group.',
                        'next_action': 'skip_duplicate_group_approval',
                    })
                elif not crm_row and self._official_group_value_matches_target(
                    value=cached_snapshot.get('wa'),
                    target_group=target_group,
                ) and not target_requester_still_pending:
                    result.update({
                        'reason_code': 'already_in_target_group',
                        'reason_detail': 'Local verified CRM snapshot already points to the requested official group.',
                        'next_action': 'skip_duplicate_group_approval',
                    })
                elif not crm_row:
                    phone_match = result.get('crm_phone_match_snapshot') if isinstance(result.get('crm_phone_match_snapshot'), dict) else None
                    cached_yw_id = str(cached_snapshot.get('ywId') or '').strip()
                    cached_phone_keys = set()
                    cached_phone_keys.update(self._official_group_phone_match_keys(phone=cached_snapshot.get('mobile')))
                    cached_phone_keys.update(self._official_group_phone_match_keys(phone=lead.get('mobile'), area_code=lead.get('area_code'), country=lead.get('country')))
                    requester_phone_keys = self._official_group_phone_match_keys(phone=approval_requester_phone, area_code=lead.get('area_code'), country=lead.get('country'))
                    if (
                        crm_verified
                        and cached_yw_id
                        and closing_record_yw_id
                        and cached_yw_id == closing_record_yw_id
                        and (not requester_phone_keys or not cached_phone_keys or bool(requester_phone_keys.intersection(cached_phone_keys)))
                    ):
                        result.update({
                            'crm_identity_match': True,
                            'eligible': True,
                            'reason_code': 'eligible',
                            'reason_detail': 'Local verified CRM snapshot matched the WhatsApp requester and the closing record ID; official-group approval is allowed.',
                            'next_action': 'approve_official_group',
                        })
                    elif phone_match:
                        result.update({
                            'reason_code': 'crm_customer_identity_mismatch',
                            'reason_detail': 'CRM phone matched the WhatsApp requester, but the CRM user ID does not match the closing record ID.',
                            'next_action': 'manual_review_official_group_approval',
                        })
                    else:
                        result.update({
                            'reason_code': 'crm_customer_not_found',
                            'reason_detail': 'No CRM customer matched the WhatsApp requester phone and closing record ID.',
                            'next_action': 'manual_review_official_group_approval',
                        })
                else:
                    result.update({
                        'crm_identity_match': True,
                        'eligible': True,
                        'reason_code': 'eligible',
                        'reason_detail': 'CRM customer matched the WhatsApp requester phone and the closing record ID; official-group approval is allowed.',
                        'next_action': 'approve_official_group',
                    })
            self._record_audit_event(
                conn,
                event_type='official_group_approval_eligibility_checked',
                event_source='official_group_approval_check',
                payload={
                    **result,
                    'source_platform': payload.source_platform,
                    'source_campaign': payload.source_campaign,
                    'source_adset': payload.source_adset,
                    'source_ad': payload.source_ad,
                    'remark': payload.remark,
                },
                lead_id=lead_id,
            )
            conn.commit()
            return result

    def ops_bind_queue(self) -> Dict[str, Any]:
        statuses = ('account_submitted', 'recognition_pending', 'bind_check_pending')
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                f"""
                SELECT l.lead_id, l.mobile, l.area_code, l.yw_id, l.app_name, l.dept_name, l.pendaftaran_group,
                       l.current_status, l.updated_at, l.parser_confidence, l.parser_missing_fields, l.parser_conflicts,
                       COALESCE(
                         (SELECT t.task_id FROM automation_tasks t
                           WHERE t.lead_id = l.lead_id AND t.task_type = 'account_recognition'
                           ORDER BY t.created_at DESC LIMIT 1),
                         (SELECT t.task_id FROM automation_tasks t
                           WHERE t.lead_id = l.lead_id AND t.task_type = 'bind_check'
                           ORDER BY t.created_at DESC LIMIT 1)
                       ) AS task_id
                FROM leads l
                WHERE l.current_status IN ({','.join(['?']*len(statuses))})
                ORDER BY l.updated_at DESC
                """,
                statuses,
            ).fetchall()]
            for row in rows:
                row['parser_missing_fields'] = json.loads(row.get('parser_missing_fields') or '[]')
                row['parser_conflicts'] = json.loads(row.get('parser_conflicts') or '[]')
            return {'rows': rows}

    def ops_group_queue(self) -> Dict[str, Any]:
        statuses = ('bind_success', 'group_join_pending')
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                f"""
                SELECT l.lead_id, l.mobile, l.area_code, l.yw_id, l.app_name, l.dept_name, l.pendaftaran_group,
                       l.current_status, l.updated_at, l.parser_confidence, l.parser_missing_fields, l.parser_conflicts,
                       (SELECT t.task_id FROM automation_tasks t
                         WHERE t.lead_id = l.lead_id AND t.task_type = 'group_join'
                         ORDER BY t.created_at DESC LIMIT 1) AS task_id
                FROM leads l
                WHERE l.current_status IN ({','.join(['?']*len(statuses))})
                ORDER BY l.updated_at DESC
                """,
                statuses,
            ).fetchall()]
            return {'rows': rows}

    def ops_dashboard_summary(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            self._ensure_ops_intake_bind_failed_clears_table(conn)
            bind_queue_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status IN ('account_submitted','recognition_pending','bind_check_pending')").fetchone()[0]
            manual_review_count = conn.execute("""
                SELECT
                    (SELECT COUNT(*) FROM leads WHERE current_status = 'manual_review_pending')
                    +
                    (SELECT COUNT(*) FROM ops_intake_items
                     WHERE system_status = 'manual_required'
                       AND COALESCE(feedback_status, '') NOT IN ('feedback_done', 'cleared'))
            """).fetchone()[0]
            group_queue_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status IN ('bind_success','group_join_pending')").fetchone()[0]
            bind_success_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status = 'bind_success'").fetchone()[0]
            bind_failed_count = conn.execute("""
                SELECT
                    (SELECT COUNT(*) FROM leads WHERE current_status = 'bind_failed'
                       AND NOT EXISTS (SELECT 1 FROM ops_intake_bind_failed_clears c WHERE c.source_type IN ('lead','lead_bind_failed') AND c.source_id = leads.lead_id AND COALESCE(c.action, '') IN ('resolved','ignored','no_followup','duplicate_closed')))
                    +
                    (SELECT COUNT(*) FROM ops_intake_items
                     WHERE system_status IN ('failed', 'crm_failed', 'bind_failed', 'partial_success_crm_failed', 'validation_failed', 'route_mismatch')
                       AND COALESCE(feedback_status, '') NOT IN ('feedback_done', 'cleared', 'resolved', 'ignored', 'no_followup', 'duplicate_closed')
                       AND NOT EXISTS (SELECT 1 FROM ops_intake_bind_failed_clears c WHERE c.source_type='ops_intake_item' AND c.source_id = ops_intake_items.item_id AND COALESCE(c.action, '') IN ('resolved','ignored','no_followup','duplicate_closed')))
            """).fetchone()[0]
            pending_completion_user_count = conn.execute("""
                SELECT COUNT(*) FROM ops_intake_items
                WHERE system_status = 'fully_success'
                  AND feedback_status = 'pending_feedback'
            """).fetchone()[0]
            unread_customer_notification_count = conn.execute("""
                SELECT COUNT(*) FROM (
                    SELECT COALESCE(NULLIF(n.lead_id, ''), NULLIF(n.mobile, '') || ':' || NULLIF(n.yw_id, ''), n.notification_id) AS notification_subject
                    FROM operator_notifications n
                    LEFT JOIN leads l ON l.lead_id = n.lead_id
                    WHERE n.is_read = 0
                      AND (
                          (n.notification_type = 'bind_check_failed' AND COALESCE(l.current_status, '') IN ('bind_failed', 'manual_review_pending'))
                          OR
                          (n.notification_type = 'crm_record_failed' AND COALESCE(l.current_status, '') IN ('bind_success', 'bind_failed', 'manual_review_pending', 'group_join_failed'))
                          OR
                          (COALESCE(l.lead_id, '') = '' AND n.notification_type IN ('bind_check_failed', 'crm_record_failed'))
                      )
                    GROUP BY notification_subject
                )
            """).fetchone()[0]
            group_join_success_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status = 'group_join_success'").fetchone()[0]
            crm_synced_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status = 'synced'").fetchone()[0]
            voucher_uploaded_count = conn.execute("SELECT COUNT(*) FROM customer_projection WHERE pz_status = 1").fetchone()[0]
            parser_conflict_count = conn.execute("SELECT COUNT(*) FROM leads WHERE parser_status = 'conflict'").fetchone()[0]
            correction_count = conn.execute("SELECT COUNT(*) FROM lead_corrections").fetchone()[0]
            return {
                'bind_queue_count': bind_queue_count,
                'manual_review_count': manual_review_count,
                'group_queue_count': group_queue_count,
                'bind_success_count': bind_success_count,
                'bind_failed_count': bind_failed_count,
                'pending_completion_user_count': pending_completion_user_count,
                'unread_customer_notification_count': unread_customer_notification_count,
                'group_join_success_count': group_join_success_count,
                'crm_synced_count': crm_synced_count,
                'voucher_uploaded_count': voucher_uploaded_count,
                'parser_conflict_count': parser_conflict_count,
                'correction_count': correction_count,
                'metric_sources': {
                    'manual_review_count': "leads.current_status='manual_review_pending' + ops_intake_items.system_status='manual_required'",
                    'bind_failed_count': "leads.current_status='bind_failed' + ops_intake_items failure statuses",
                    'pending_completion_user_count': "ops_intake_items.system_status='fully_success' AND feedback_status='pending_feedback'",
                    'unread_customer_notification_count': 'distinct unread actionable failure operator_notifications users',
                },
            }

    def ops_next_bind_task(self) -> Dict[str, Any]:
        queue = self.ops_bind_queue()['rows']
        return {'kind': 'bind', 'row': queue[0]} if queue else {'kind': 'none', 'row': None}

    def ops_next_group_task(self) -> Dict[str, Any]:
        queue = self.ops_group_queue()['rows']
        return {'kind': 'group', 'row': queue[0]} if queue else {'kind': 'none', 'row': None}

    def ops_next_action(self) -> Dict[str, Any]:
        candidates = []
        review_rows = self.ops_manual_review_queue()['rows']
        for row in review_rows:
            candidates.append({'kind': 'manual_review', 'row': row, 'score': 110, 'reason': '存在待人工复核的数据，优先处理脏数据入口'})
        bind_rows = self.ops_bind_queue()['rows']
        for row in bind_rows:
            if row.get('current_status') == 'bind_check_pending':
                candidates.append({'kind': 'bind', 'row': row, 'score': 90, 'reason': '存在待回写的绑定结果'})
            elif row.get('current_status') == 'recognition_pending':
                candidates.append({'kind': 'bind', 'row': row, 'score': 80, 'reason': '截图待识别，需先得到账号ID'})
            else:
                candidates.append({'kind': 'bind', 'row': row, 'score': 70, 'reason': '存在待处理的账号绑定任务'})

        with self.db.connect() as conn:
            failed_bind_rows = [dict(r) for r in conn.execute(
                """
                SELECT l.lead_id, l.mobile, l.area_code, l.yw_id, l.app_name, l.dept_name, l.pendaftaran_group,
                       l.current_status, l.updated_at,
                       (SELECT t.task_id FROM automation_tasks t
                         WHERE t.lead_id = l.lead_id AND t.task_type = 'bind_check'
                         ORDER BY t.created_at DESC LIMIT 1) AS task_id
                FROM leads l
                WHERE l.current_status = 'bind_failed'
                ORDER BY l.updated_at DESC
                """
            ).fetchall()]
        for row in failed_bind_rows:
            candidates.append({'kind': 'bind', 'row': row, 'score': 100, 'reason': '绑定失败优先复核与再次沟通'})

        with self.db.connect() as conn:
            crm_sync_rows = [dict(r) for r in conn.execute(
                """
                SELECT l.lead_id, l.mobile, l.area_code, l.yw_id, l.app_name, l.dept_name, l.pendaftaran_group,
                       l.current_status, l.updated_at
                FROM leads l
                WHERE l.current_status = 'bind_success'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM sync_logs sl
                      WHERE sl.lead_id = l.lead_id
                        AND sl.target_system = 'crm'
                        AND sl.status = 'success'
                  )
                ORDER BY l.updated_at DESC
                """
            ).fetchall()]
        for row in crm_sync_rows:
            candidates.append({'kind': 'crm_sync', 'row': row, 'score': 60, 'reason': '公会绑定已成功，需入库 CRM'})

        batch_queue = self.approval_batch_queue()
        for bucket_name, rows in [('registration_approval_batch', batch_queue['registration_groups']), ('official_approval_batch', batch_queue['official_groups'])]:
            for row in rows:
                if row.get('ready'):
                    candidates.append({'kind': bucket_name, 'row': row, 'score': 55, 'reason': f"审批批次已就绪：{row.get('registration_group')}"})

        group_rows = self.ops_group_queue()['rows']
        for row in group_rows:
            candidates.append({'kind': 'group', 'row': row, 'score': 40, 'reason': '存在待处理的官方群审批/入群任务'})

        with self.db.connect() as conn:
            failed_group_rows = [dict(r) for r in conn.execute(
                """
                SELECT l.lead_id, l.mobile, l.area_code, l.yw_id, l.app_name, l.dept_name, l.pendaftaran_group,
                       l.current_status, l.updated_at,
                       (SELECT t.task_id FROM automation_tasks t
                         WHERE t.lead_id = l.lead_id AND t.task_type = 'group_join'
                         ORDER BY t.created_at DESC LIMIT 1) AS task_id
                FROM leads l
                WHERE l.current_status = 'group_join_failed'
                ORDER BY l.updated_at DESC
                """
            ).fetchall()]
        for row in failed_group_rows:
            candidates.append({'kind': 'group', 'row': row, 'score': 50, 'reason': '官方群处理失败，需优先重试或复核'})

        if not candidates:
            return {'kind': 'none', 'row': None, 'score': 0, 'reason': '当前没有待处理任务'}

        candidates.sort(key=lambda x: (-x['score'], x['row'].get('updated_at') or ''))
        return candidates[0]

    def _fetch_intake_bot_preset_rows(self) -> list[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT profile_name, app_id, robot_name, default_app, default_guild, enabled, updated_at FROM intake_bot_presets ORDER BY profile_name ASC"
            ).fetchall()]
        for row in rows:
            normalized_profile = str(row.get('profile_name') or '').strip()
            robot_name = str(row.get('robot_name') or '').strip()
            row['robot_name'] = robot_name or normalized_profile
        return rows

    def _list_notify_robot_options(self) -> list[Dict[str, Any]]:
        profiles_dir = Path(os.getenv('HERMES_HOME') or (Path.home() / '.hermes')) / 'profiles'
        discovered: list[Dict[str, Any]] = []
        if profiles_dir.exists():
            for profile_dir in sorted(profiles_dir.glob('wa-approval-broadcast*')):
                if not profile_dir.is_dir():
                    continue
                profile_name = profile_dir.name
                env_path = profile_dir / '.env'
                app_id = ''
                if env_path.exists():
                    try:
                        for line in env_path.read_text(errors='ignore').splitlines():
                            if line.startswith('FEISHU_APP_ID='):
                                app_id = line.split('=', 1)[1].strip().strip('"\'')
                                break
                    except Exception:
                        app_id = ''
                suffix_match = re.search(r'-(\d+)$', profile_name)
                bot_number = int(suffix_match.group(1)) if suffix_match else 1
                robot_name = {
                    'wa-approval-broadcast': '审批bot01',
                    'wa-approval-broadcast-02': '审批bot02',
                    'wa-approval-broadcast-03': '审批Bot03',
                }.get(profile_name, f'审批bot{bot_number:02d}')
                if profile_name == 'wa-approval-broadcast':
                    continue
                discovered.append({
                    'profile_name': profile_name,
                    'robot_name': robot_name,
                    'label': robot_name,
                    'app_id': app_id,
                })
        if discovered:
            return discovered
        return [
            {
                'profile_name': 'wa-approval-broadcast-02',
                'robot_name': '审批bot02',
                'label': '审批bot02',
                'app_id': '',
            },
            {
                'profile_name': 'wa-approval-broadcast-03',
                'robot_name': '审批Bot03',
                'label': '审批Bot03',
                'app_id': '',
            },
        ]

    def list_whatsapp_approval_area_options(self) -> Dict[str, Any]:
        options = [_enrich_mcn_region_option(item.get('value')) for item in self.list_mcn_region_options(include_disabled=False).get('enabled_options', [])]
        return {
            'options': options,
            'source_options': options,
            'updated_at': None,
            'source': 'mcn_region_options',
            'editable': False,
        }

    def update_whatsapp_approval_area_options(self, payload: WhatsAppApprovalAreaOptionsUpdateRequest) -> Dict[str, Any]:
        result = self.list_whatsapp_approval_area_options()
        result['saved'] = False
        result['ignored'] = True
        result['detail'] = 'approval area options are managed by mcn_region_options'
        return result

    def _notify_robot_name(self, profile_name: Optional[str]) -> str:
        normalized = str(profile_name or '').strip()
        if not normalized:
            return ''
        for option in self._list_notify_robot_options():
            if str(option.get('profile_name') or '').strip() == normalized:
                return str(option.get('robot_name') or normalized).strip() or normalized
        return normalized

    def _apply_account_notify_profile_to_official_binding(
        self,
        binding: Dict[str, Any],
        *,
        account: Dict[str, Any],
        responsible_type: str,
    ) -> Dict[str, Any]:
        if str(responsible_type or '').strip() != 'official_group':
            return binding
        account_notify_profile = str((account or {}).get('notify_profile_name') or '').strip()
        if not account_notify_profile:
            return binding
        binding['notify_profile_name'] = account_notify_profile
        binding['notify_robot_name'] = self._notify_robot_name(account_notify_profile)
        return binding

    @staticmethod
    def _whatsapp_approval_session_account_key(account_key: str) -> str:
        normalized = re.sub(r'[^a-z0-9]+', '-', str(account_key or '').strip().lower()).strip('-')
        return normalized or 'default'

    def _whatsapp_approval_session_client_id(self, account_key: str) -> str:
        return f"wa-approval-{self._whatsapp_approval_session_account_key(account_key)}"

    def _whatsapp_approval_runtime_port_for_slug(self, slug: str) -> int:
        raw = hashlib.sha1(str(slug or 'default').encode('utf-8')).hexdigest()
        return 56000 + (int(raw[:8], 16) % 8000)

    def _whatsapp_approval_session_auth_path(self, account_key: str) -> Path:
        return WHATSAPP_APPROVAL_WORKER_AUTH_ACCOUNTS_DIR / self._whatsapp_approval_session_account_key(account_key)

    def _whatsapp_approval_runtime_raw_state_path(self, account_key: str) -> Path:
        return WHATSAPP_APPROVAL_WORKER_RUNTIME_DIR / f"{self._whatsapp_approval_session_account_key(account_key)}.json"

    def _whatsapp_approval_runtime_identity(self, account_key: str) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        slug = self._whatsapp_approval_session_account_key(normalized_key)
        state_path = self._whatsapp_approval_runtime_raw_state_path(normalized_key)
        log_path = WHATSAPP_APPROVAL_WORKER_LOG_DIR / f"{slug}.log"
        auth_path = WHATSAPP_APPROVAL_WORKER_AUTH_ACCOUNTS_DIR / slug
        meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
        persisted_port = meta.get('port') if isinstance(meta, dict) else None
        try:
            port = int(persisted_port)
        except (TypeError, ValueError):
            port = self._whatsapp_approval_runtime_port_for_slug(slug)
        persisted_base_url = str((meta or {}).get('base_url') or '').strip() if isinstance(meta, dict) else ''
        base_url = persisted_base_url or f'http://127.0.0.1:{port}'
        return {
            'account_key': normalized_key,
            'slug': slug,
            'systemd_unit': f'mcn-wa-runtime-{slug}.service',
            'auth_path': auth_path,
            'client_id': f'wa-approval-{slug}',
            'state_path': state_path,
            'log_path': log_path,
            'port': port,
            'base_url': base_url,
        }

    def _whatsapp_approval_has_local_auth_session(self, account_key: str) -> bool:
        try:
            auth_path = self._whatsapp_approval_session_auth_path(account_key)
            return auth_path.exists() and auth_path.is_dir() and any(auth_path.iterdir())
        except Exception:
            return False

    def _whatsapp_approval_runtime_in_localauth_recovery_window(self, account_key: str, meta: Optional[Dict[str, Any]] = None) -> bool:
        normalized_key = str(account_key or '').strip()
        if not normalized_key or not self._whatsapp_approval_has_local_auth_session(normalized_key):
            return False
        runtime_meta = dict(meta or self._read_whatsapp_approval_runtime_meta(normalized_key) or {})
        started_at = runtime_meta.get('started_at')
        if not started_at:
            return False
        try:
            started_dt = parse_iso_datetime(str(started_at))
            age_seconds = (datetime.now(timezone.utc) - started_dt).total_seconds()
        except Exception:
            return False
        if age_seconds < 0 or age_seconds > WHATSAPP_APPROVAL_LOCALAUTH_RECOVERY_GRACE_SECONDS:
            return False
        stopped_at = runtime_meta.get('stopped_at')
        if stopped_at:
            try:
                stopped_dt = parse_iso_datetime(str(stopped_at))
                if stopped_dt >= started_dt:
                    return False
            except Exception:
                pass
        return True

    def _whatsapp_approval_runtime_state_path(self, account_key: str) -> Path:
        return self._whatsapp_approval_runtime_identity(account_key)['state_path']

    def _whatsapp_approval_runtime_log_path(self, account_key: str) -> Path:
        return self._whatsapp_approval_runtime_identity(account_key)['log_path']

    def _pick_whatsapp_approval_runtime_port(self, account_key: str = '') -> int:
        if account_key:
            return int(self._whatsapp_approval_runtime_identity(account_key)['port'])
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _pid_running(pid: Any) -> bool:
        try:
            normalized = int(pid)
        except (TypeError, ValueError):
            return False
        if normalized <= 0:
            return False
        try:
            os.kill(normalized, 0)
        except OSError:
            return False
        return True

    def _list_whatsapp_approval_runtime_processes(self, auth_path: str) -> List[int]:
        normalized_auth_path = str(auth_path or '').strip()
        if not normalized_auth_path:
            return []
        try:
            result = subprocess.run(
                ['ps', '-axo', 'pid=,command='],
                capture_output=True,
                text=True,
                check=True,
            )
        except Exception:
            return []
        matched: List[int] = []
        for raw_line in str(result.stdout or '').splitlines():
            line = str(raw_line or '').strip()
            if not line or normalized_auth_path not in line:
                continue
            parts = line.split(None, 1)
            if not parts:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if pid > 0 and pid not in matched:
                matched.append(pid)
        return matched

    def _terminate_whatsapp_approval_runtime_processes(self, pids: List[int]) -> None:
        seen: List[int] = []
        for raw_pid in pids:
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                continue
            if pid <= 0 or pid in seen:
                continue
            seen.append(pid)
            if not self._pid_running(pid):
                continue
            try:
                os.kill(pid, 15)
            except OSError:
                continue
        deadline = time.time() + 2.0
        while time.time() < deadline:
            remaining = [pid for pid in seen if self._pid_running(pid)]
            if not remaining:
                return
            time.sleep(0.2)
        for pid in seen:
            if not self._pid_running(pid):
                continue
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def _active_whatsapp_approval_runtime_entries(self, *, exclude_account_key: str = '') -> List[Dict[str, Any]]:
        excluded = self._whatsapp_approval_session_account_key(exclude_account_key) if exclude_account_key else ''
        entries: List[Dict[str, Any]] = []
        try:
            paths = list(WHATSAPP_APPROVAL_WORKER_RUNTIME_DIR.glob('*.json'))
        except Exception:
            paths = []
        for path in paths:
            account_slug = path.stem
            if excluded and account_slug == excluded:
                continue
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get('stopped_at'):
                continue
            pid = payload.get('pid')
            if not self._pid_running(pid):
                continue
            item = dict(payload)
            item.setdefault('account_key', account_slug)
            item['runtime_state_path'] = str(path)
            entries.append(item)
        return entries

    def _whatsapp_approval_runtime_capacity_bucket(self, account_key: str) -> str:
        normalized = str(account_key or '').strip().lower()
        if normalized.startswith('learn-') or normalized.startswith('group-atmosphere'):
            return 'group_atmosphere'
        try:
            row = self._get_whatsapp_approval_account_row(account_key)
        except Exception:
            row = None
        responsible_type = str((row or {}).get('responsible_type') or '').strip()
        if responsible_type in {'group_atmosphere', 'group_atmosphere_learning'}:
            return 'group_atmosphere'
        return responsible_type or 'registration_group'

    def _whatsapp_approval_runtime_capacity_limit(self) -> int:
        raw = os.environ.get('WHATSAPP_APPROVAL_MAX_ACTIVE_RUNTIMES', '2')
        try:
            limit = int(str(raw).strip())
        except (TypeError, ValueError):
            limit = 2
        return max(1, limit)

    def _ensure_whatsapp_approval_runtime_capacity(self, account_key: str) -> None:
        limit = self._whatsapp_approval_runtime_capacity_limit()
        target_bucket = self._whatsapp_approval_runtime_capacity_bucket(account_key)
        active_entries = [
            item for item in self._active_whatsapp_approval_runtime_entries(exclude_account_key=account_key)
            if self._whatsapp_approval_runtime_capacity_bucket(str(item.get('account_key') or '')) == target_bucket
        ]
        if len(active_entries) < limit:
            return
        raise HTTPException(
            status_code=429,
            detail={
                'code': 'queued_runtime_start',
                'message': '当前 WhatsApp Runtime 已达到并发上限，请先停止一个空闲账号或稍后重试',
                'max_active_runtimes': limit,
                'active_runtime_count': len(active_entries),
                'active_accounts': [str(item.get('account_key') or '').strip() for item in active_entries if str(item.get('account_key') or '').strip()],
            },
        )

    def _read_whatsapp_approval_runtime_meta(self, account_key: str) -> Dict[str, Any]:
        path = self._whatsapp_approval_runtime_raw_state_path(account_key)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _cache_whatsapp_approval_session_snapshot(self, account_key: str, session_state: Dict[str, Any], worker_health: Dict[str, Any]) -> None:
        normalized_key = str(account_key or '').strip()
        if not normalized_key or not isinstance(session_state, dict) or not isinstance(worker_health, dict) or not worker_health:
            return
        try:
            meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
            if not meta:
                return
            cached_session = dict(session_state)
            cached_session['qr_text'] = None
            cached_session['qr_ascii'] = None
            cached_session['qr_image_data_url'] = None
            cached_session['qr_available'] = False if cached_session.get('login_verified') else bool(cached_session.get('qr_available'))
            meta['last_session_state'] = cached_session
            meta['last_worker_health'] = dict(worker_health)
            meta['last_session_checked_at'] = utc_now()
            meta['last_session_checked_ts'] = time.time()
            self._write_whatsapp_approval_runtime_meta(normalized_key, meta)
        except Exception:
            return

    def _cache_baileys_whatsapp_approval_session_snapshot(
        self,
        account_key: str,
        *,
        runtime_state: Dict[str, Any],
        session_state: Dict[str, Any],
        provider_health: Dict[str, Any],
    ) -> None:
        normalized_key = str(account_key or '').strip()
        if not normalized_key or not isinstance(session_state, dict):
            return
        runtime = dict(runtime_state or {})
        session = dict(session_state or {})
        provider_account_id = str(
            runtime.get('baileys_account_id')
            or runtime.get('provider_account_id')
            or session.get('baileys_account_id')
            or session.get('provider_account_id')
            or ''
        ).strip()
        if not provider_account_id or not isinstance(provider_health, dict) or not provider_health:
            return
        try:
            meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
            if not isinstance(meta, dict):
                meta = {}
            cached_session = dict(session)
            cached_session['qr_text'] = None
            cached_session['qr_ascii'] = None
            cached_session['qr_image_data_url'] = None
            if cached_session.get('login_verified') or cached_session.get('can_probe'):
                cached_session['qr_available'] = False
                cached_session['can_show_qr'] = False
            meta.update({
                'provider': 'baileys',
                'provider_name': 'baileys',
                'base_url': runtime.get('base_url') or meta.get('base_url'),
                'baileys_account_id': provider_account_id,
                'provider_account_id': provider_account_id,
                'last_runtime_state': runtime,
                'last_session_state': cached_session,
                'last_session_checked_at': utc_now(),
                'last_session_checked_ts': time.time(),
            })
            if provider_health:
                meta['last_baileys_provider_health'] = self._drop_baileys_pairing_code_secrets(provider_health)
            if not str(meta.get('started_at') or '').strip() and bool(runtime.get('active')):
                meta['started_at'] = utc_now()
            self._write_whatsapp_approval_runtime_meta(normalized_key, meta)
        except Exception:
            return

    @classmethod
    def _drop_baileys_pairing_code_secrets(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cls._drop_baileys_pairing_code_secrets(item)
                for key, item in value.items()
                if str(key) not in {'pairingCode', 'pairing_code'}
            }
        if isinstance(value, list):
            return [cls._drop_baileys_pairing_code_secrets(item) for item in value]
        return copy.deepcopy(value)

    def _cached_whatsapp_approval_session_snapshot(self, account_key: str, *, max_age_seconds: float = 300.0) -> Dict[str, Any]:
        meta = self._read_whatsapp_approval_runtime_meta(account_key)
        session_state = meta.get('last_session_state') if isinstance(meta.get('last_session_state'), dict) else {}
        if not session_state:
            return {}
        try:
            checked_ts = float(meta.get('last_session_checked_ts') or 0.0)
        except (TypeError, ValueError):
            checked_ts = 0.0
        if max_age_seconds > 0 and checked_ts and (time.time() - checked_ts) > max_age_seconds:
            return {}
        cached = dict(session_state)
        cached.setdefault('from_cached_session', True)
        cached['cached_session_age_seconds'] = round(max(0.0, time.time() - checked_ts), 3) if checked_ts else None
        return cached

    def _write_whatsapp_approval_runtime_meta(self, account_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        path = self._whatsapp_approval_runtime_raw_state_path(account_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = dict(payload or {})
        row['account_key'] = str(account_key or '').strip()
        path.write_text(json.dumps(row, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return row

    @staticmethod
    def _normalize_wa_provider_capabilities(value: Any) -> Dict[str, bool]:
        payload = dict(value or {}) if isinstance(value, dict) else {}
        return {
            'shadow_read': bool(payload.get('shadow_read')),
            'advisory_verify': bool(payload.get('advisory_verify')),
            'authoritative_read': bool(payload.get('authoritative_read')),
            'manual_approve': bool(payload.get('manual_approve')),
            'auto_approve': bool(payload.get('auto_approve')),
            'official_group_approval': bool(payload.get('official_group_approval') or payload.get('officialGroupApproval')),
            'group_member_lookup': bool(payload.get('group_member_lookup') or payload.get('groupMemberLookup')),
            'group_metadata': bool(payload.get('group_metadata') or payload.get('groupMetadata')),
            'assistant_group_runtime': bool(payload.get('assistant_group_runtime') or payload.get('assistantGroupRuntime')),
        }

    def _resolve_wa_provider_decision(
        self,
        *,
        account: Optional[Dict[str, Any]] = None,
        binding: Optional[Dict[str, Any]] = None,
        runtime_state: Optional[Dict[str, Any]] = None,
        responsible_type: Any = '',
    ) -> Dict[str, Any]:
        account_input = dict(account or {})
        runtime_row = dict(runtime_state or {})
        account_row = dict(account_input)
        if runtime_row:
            account_row = {**runtime_row, **account_row}
        binding_row = dict(binding or {})
        normalized_responsible_type = str(
            responsible_type
            or binding_row.get('responsible_type')
            or account_row.get('responsible_type')
            or ''
        ).strip().lower()
        if normalized_responsible_type in {'registration_group', 'official_group', 'group_atmosphere', 'group_atmosphere_learning'}:
            has_explicit_runtime_mode = any(
                str(source.get(key) or '').strip()
                for source in (account_input, binding_row)
                for key in RUNTIME_MODE_KEYS
            )
            if not has_explicit_runtime_mode and str(account_row.get('provider_mode') or '').strip().lower() in {'', 'legacy_only'}:
                account_row['provider_mode'] = _baileys_default_provider_mode_for_responsible_type(normalized_responsible_type)
        adapter = getattr(self, 'whatsapp_approval_runtime_adapter', None)
        if adapter is not None and hasattr(adapter, 'provider_decision'):
            decision = adapter.provider_decision(account=account_row, binding=binding_row).to_dict()
            decision['provider_capabilities'] = self._normalize_wa_provider_capabilities(decision.get('provider_capabilities'))
            return decision
        provider_mode = resolve_whatsapp_approval_provider_mode(
            account=account_row,
            binding=binding_row,
            responsible_type=normalized_responsible_type,
        )
        provider_name = 'baileys' if provider_mode.startswith('baileys') else 'legacy_playwright'
        capabilities = {
            'shadow_read': provider_mode in {'baileys_shadow', 'baileys_advisory', 'baileys_authoritative', 'baileys_manual_approve_gray'},
            'advisory_verify': provider_mode == 'baileys_advisory',
            'authoritative_read': provider_mode in {'baileys_authoritative', 'baileys_primary'},
            'manual_approve': provider_mode in {'baileys_manual_approve_gray', 'baileys_primary'},
            'auto_approve': False,
            'official_group_approval': provider_name == 'baileys',
            'group_member_lookup': provider_name == 'baileys',
            'group_metadata': provider_name == 'baileys',
            'assistant_group_runtime': provider_name == 'baileys',
        }
        return {
            'provider_name': provider_name,
            'provider_mode': provider_mode,
            'provider_source': 'fallback_resolver',
            'provider_capabilities': capabilities,
            'shadow_enabled': capabilities['shadow_read'],
            'advisory_enabled': capabilities['advisory_verify'],
            'authoritative_read': capabilities['authoritative_read'],
            'manual_approve_enabled': capabilities['manual_approve'],
        }

    def _sync_wa_account_projection(self, account: Dict[str, Any], *, runtime_state: Optional[Dict[str, Any]] = None) -> None:
        row = dict(account or {})
        account_key = str(row.get('account_key') or '').strip()
        if not account_key:
            return
        runtime = dict(runtime_state or row.get('runtime_state') or {})
        provider_mode = resolve_whatsapp_approval_provider_mode(account=row, binding=runtime)
        provider_name = str(row.get('provider_name') or runtime.get('provider_name') or ('baileys' if provider_mode.startswith('baileys') else 'legacy_playwright')).strip()
        runtime_generation = row.get('runtime_generation', runtime.get('runtime_generation'))
        try:
            runtime_generation = int(runtime_generation) if runtime_generation is not None else 0
        except Exception:
            runtime_generation = 0
        if provider_name.strip().lower().startswith('baileys'):
            runtime_generation = max(runtime_generation, self._approval_queue_current_truth_runtime_generation_floor(account_key))
        payload = {
            'account_name': str(row.get('account_name') or '').strip(),
            'verification_status': str(row.get('verification_status') or '').strip(),
            'runtime': runtime,
        }
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO wa_accounts (
                    account_key, responsible_type, provider_name, provider_mode,
                    health_status, runtime_generation, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_key) DO UPDATE SET
                    responsible_type=excluded.responsible_type,
                    provider_name=excluded.provider_name,
                    provider_mode=excluded.provider_mode,
                    health_status=excluded.health_status,
                    runtime_generation=excluded.runtime_generation,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    account_key,
                    str(row.get('responsible_type') or '').strip(),
                    provider_name,
                    provider_mode,
                    str(runtime.get('status') or '').strip() or 'unknown',
                    runtime_generation,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()

    def _sync_wa_group_binding_projection(self, account_key: str, binding: Dict[str, Any], *, responsible_type: str = '') -> None:
        item = dict(binding or {})
        binding_id = str(item.get('binding_id') or '').strip()
        if not binding_id:
            return
        provider_mode = resolve_whatsapp_approval_provider_mode(binding=item, responsible_type=responsible_type or item.get('responsible_type'))
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO wa_group_bindings (
                    binding_id, account_key, responsible_type, link, group_id, registration_group,
                    group_name, identity_status, config_fingerprint, provider_mode,
                    provider_capabilities_json, binding_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(binding_id) DO UPDATE SET
                    account_key=excluded.account_key,
                    responsible_type=excluded.responsible_type,
                    link=excluded.link,
                    group_id=excluded.group_id,
                    registration_group=excluded.registration_group,
                    group_name=excluded.group_name,
                    identity_status=excluded.identity_status,
                    config_fingerprint=excluded.config_fingerprint,
                    provider_mode=excluded.provider_mode,
                    provider_capabilities_json=excluded.provider_capabilities_json,
                    binding_json=excluded.binding_json,
                    updated_at=excluded.updated_at
                """,
                (
                    binding_id,
                    str(account_key or '').strip(),
                    str(responsible_type or item.get('responsible_type') or '').strip(),
                    str(item.get('link') or '').strip(),
                    str(item.get('group_id') or '').strip(),
                    str(item.get('registration_group') or '').strip(),
                    str(item.get('group_name') or '').strip(),
                    str(item.get('identity_status') or '').strip(),
                    str(item.get('config_fingerprint') or _whatsapp_approval_binding_config_fingerprint(item)).strip(),
                    provider_mode,
                    json.dumps(self._normalize_wa_provider_capabilities(item.get('provider_capabilities')), ensure_ascii=False),
                    json.dumps(item, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()

    def _mirror_wa_truth_snapshot(self, *, account_key: str, binding: Dict[str, Any], snapshot_type: str, facts: Dict[str, Any], observed_at: str, expires_at: Optional[str]) -> None:
        binding_id = str((binding or {}).get('binding_id') or '').strip()
        if not binding_id:
            return
        snapshot_id = f'{snapshot_type}:{binding_id}'
        requester_ids = [str(item).strip() for item in (facts.get('requester_ids') or []) if str(item).strip()]
        trusted_pending_count = facts.get('trusted_pending_count')
        try:
            trusted_pending_count = int(trusted_pending_count) if trusted_pending_count is not None else None
        except Exception:
            trusted_pending_count = None
        source = dict(facts.get('source') or {}) if isinstance(facts.get('source'), dict) else {}
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO wa_truth_snapshots (
                    snapshot_id, binding_id, account_key, snapshot_type, truth_status,
                    trusted_pending_count, requester_ids_json, facts_json, source_json,
                    checked_at, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    account_key=excluded.account_key,
                    snapshot_type=excluded.snapshot_type,
                    truth_status=excluded.truth_status,
                    trusted_pending_count=excluded.trusted_pending_count,
                    requester_ids_json=excluded.requester_ids_json,
                    facts_json=excluded.facts_json,
                    source_json=excluded.source_json,
                    checked_at=excluded.checked_at,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    snapshot_id,
                    binding_id,
                    str(account_key or '').strip(),
                    snapshot_type,
                    str(facts.get('trust_status') or '').strip() or 'UNKNOWN',
                    trusted_pending_count,
                    json.dumps(requester_ids, ensure_ascii=False),
                    json.dumps(facts, ensure_ascii=False),
                    json.dumps(source, ensure_ascii=False),
                    observed_at,
                    expires_at,
                    now,
                ),
            )
            conn.commit()

    def _record_wa_runtime_action(self, *, account_key: str, binding: Dict[str, Any], action_type: str, status: str, request_payload: Optional[Dict[str, Any]] = None, result_payload: Optional[Dict[str, Any]] = None) -> str:
        binding_id = str((binding or {}).get('binding_id') or '').strip()
        action_id = create_id('wa_action')
        provider_mode = resolve_whatsapp_approval_provider_mode(
            binding=binding,
            account=request_payload or {},
            responsible_type=(binding or {}).get('approval_scope') or (request_payload or {}).get('responsible_type'),
        )
        provider_name = 'baileys' if provider_mode.startswith('baileys') else 'legacy_playwright'
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO wa_runtime_actions (
                    action_id, account_key, binding_id, action_type, provider_name,
                    provider_mode, status, request_json, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    str(account_key or '').strip(),
                    binding_id,
                    str(action_type or '').strip(),
                    provider_name,
                    provider_mode,
                    str(status or '').strip(),
                    json.dumps(dict(request_payload or {}), ensure_ascii=False),
                    json.dumps(dict(result_payload or {}), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
        return action_id

    def _upsert_wa_identity_map_from_result(self, *, provider_name: str, result: Dict[str, Any]) -> None:
        requester_lookup: Dict[str, Dict[str, Any]] = {}
        for item in result.get('requesters') or []:
            if not isinstance(item, dict):
                continue
            requester_id = str(
                item.get('requesterId')
                or item.get('requester_id')
                or item.get('id')
                or item.get('jid')
                or ''
            ).strip()
            if requester_id:
                requester_lookup[requester_id] = dict(item)
        requester_ids = [str(item).strip() for item in (result.get('requester_ids') or []) if str(item).strip()]
        for requester_id in requester_lookup.keys():
            if requester_id not in requester_ids:
                requester_ids.append(requester_id)
        if not requester_ids:
            return
        now = utc_now()
        with self.db.connect() as conn:
            for requester_id in requester_ids:
                normalized = requester_id.lower()
                is_lid_requester = normalized.endswith('@lid') or normalized.endswith('@hosted.lid')
                requester = requester_lookup.get(requester_id) or requester_lookup.get(normalized) or {}
                phone_candidate = str(
                    requester.get('phoneNormalized')
                    or requester.get('phone_normalized')
                    or requester.get('phoneRaw')
                    or requester.get('phone_raw')
                    or requester.get('debugLidPhoneRaw')
                    or requester.get('debug_lid_phone_raw')
                    or ''
                ).strip()
                phone_digits = ''.join(ch for ch in phone_candidate if ch.isdigit())
                if not phone_digits and requester_id and requester_id[0].isdigit() and not is_lid_requester:
                    phone_digits = ''.join(ch for ch in requester_id if ch.isdigit())
                lid = requester_id if is_lid_requester else str(requester.get('lid') or '').strip()
                conn.execute(
                    """
                    INSERT INTO wa_identity_map (
                        identity_key, provider_name, provider_requester_id, normalized_requester_id,
                        wa_phone_normalized, lid, metadata_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(identity_key) DO UPDATE SET
                        provider_name=excluded.provider_name,
                        provider_requester_id=excluded.provider_requester_id,
                        normalized_requester_id=excluded.normalized_requester_id,
                        wa_phone_normalized=excluded.wa_phone_normalized,
                        lid=excluded.lid,
                        metadata_json=excluded.metadata_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        f'{provider_name}:{normalized}',
                        provider_name,
                        requester_id,
                        normalized,
                        phone_digits,
                        lid,
                        json.dumps({
                            'source': result.get('source'),
                            'fingerprint': result.get('fingerprint'),
                            'identityResolutionStatus': requester.get('identityResolutionStatus'),
                            'identityResolutionReason': requester.get('identityResolutionReason'),
                        }, ensure_ascii=False),
                        now,
                    ),
                )
            conn.commit()

    def _upsert_truth_acquisition_log(self, *, acquisition_id: str, account_key: str, binding: Dict[str, Any], trigger: str, result: Dict[str, Any], stages: List[Dict[str, Any]]) -> None:
        if not acquisition_id:
            return
        now = utc_now()
        audit_result = dict(result or {})
        audit_result.pop('stages', None)
        approval_queue_truth = audit_result.get('approval_queue_truth')
        if isinstance(approval_queue_truth, dict):
            compact_truth = dict(approval_queue_truth)
            compact_truth.pop('current_truth_raw', None)
            compact_truth.pop('latest_probe_debug', None)
            audit_result['approval_queue_truth'] = compact_truth
        if db_writer_enabled() and self.db.db_path != ':memory:':
            job = {
                'type': 'truth_acquisition_log',
                'acquisition_id': acquisition_id,
                'account_key': str(account_key or '').strip(),
                'binding_id': str((binding or {}).get('binding_id') or '').strip(),
                'trigger': str(trigger or '').strip(),
                'final_state': str(result.get('final_state') or '').strip(),
                'trust_status': str(result.get('trust_status') or '').strip(),
                'current_truth_written': bool(result.get('current_truth_written')),
                'latest_probe_written': bool(result.get('latest_probe_written')),
                'result': audit_result,
                'stages': list(stages or []),
                'now': now,
            }
            try:
                submit_sqlite_write_job(job, timeout=float(os.getenv('MCN_DB_WRITER_TIMEOUT_SECONDS') or '60'))
                return
            except SQLiteWriteQueueError:
                if db_writer_required():
                    raise
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO truth_acquisition_logs (
                    acquisition_id, account_key, binding_id, trigger, final_state, trust_status,
                    current_truth_written, latest_probe_written, result_json, stages_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(acquisition_id) DO UPDATE SET
                    account_key=excluded.account_key,
                    binding_id=excluded.binding_id,
                    trigger=excluded.trigger,
                    final_state=excluded.final_state,
                    trust_status=excluded.trust_status,
                    current_truth_written=excluded.current_truth_written,
                    latest_probe_written=excluded.latest_probe_written,
                    result_json=excluded.result_json,
                    stages_json=excluded.stages_json,
                    updated_at=excluded.updated_at
                """,
                (
                    acquisition_id,
                    str(account_key or '').strip(),
                    str((binding or {}).get('binding_id') or '').strip(),
                    str(trigger or '').strip(),
                    str(result.get('final_state') or '').strip(),
                    str(result.get('trust_status') or '').strip(),
                    1 if result.get('current_truth_written') else 0,
                    1 if result.get('latest_probe_written') else 0,
                    json.dumps(audit_result, ensure_ascii=False),
                    json.dumps(list(stages or []), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()

    def _request_whatsapp_approval_worker_health(self, base_url: str) -> Dict[str, Any]:
        normalized_base_url = str(base_url or '').strip().rstrip('/')
        if not normalized_base_url:
            raise RuntimeError('worker base_url is required')
        response = requests.get(f'{normalized_base_url}/health', timeout=10.0)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError('worker health must be a JSON object')
        return payload

    def _current_whatsapp_approval_worker_health(self) -> Dict[str, Any]:
        config = self.get_production_ops_daemon_config().get('config') or {}
        worker_base_url = str(config.get('worker_base_url') or '').strip().rstrip('/')
        if not worker_base_url:
            raise RuntimeError('shared registration-group worker base_url is not configured')
        return self._request_whatsapp_approval_worker_health(worker_base_url)

    def _build_whatsapp_approval_runtime_state(
        self,
        account_key: str,
        *,
        worker_health: Optional[Dict[str, Any]] = None,
        allow_shared_fallback: bool = True,
        skip_health_check: bool = False,
    ) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        expected_client_id = self._whatsapp_approval_session_client_id(normalized_key)
        expected_approval_client_id = f"{expected_client_id}-approval"
        expected_auth_path = str(self._whatsapp_approval_session_auth_path(normalized_key))
        meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
        base_runtime = {
            'account_key': normalized_key,
            'mode': 'dedicated_runtime',
            'source': 'dedicated' if meta else 'shared',
            'configured': bool(meta),
            'active': False,
            'pid': meta.get('pid'),
            'port': meta.get('port'),
            'base_url': str(meta.get('base_url') or '').strip() or None,
            'auth_path': str(meta.get('auth_path') or expected_auth_path).strip(),
            'client_id': str(meta.get('client_id') or expected_client_id).strip(),
            'log_path': str(meta.get('log_path') or self._whatsapp_approval_runtime_log_path(normalized_key)).strip(),
            'meta_path': str(self._whatsapp_approval_runtime_state_path(normalized_key)),
            'started_at': meta.get('started_at'),
            'stopped_at': meta.get('stopped_at'),
            'last_started_at': meta.get('started_at'),
            'last_action_at': meta.get('last_action_at'),
            'last_error': meta.get('last_error'),
            'status': 'not_started',
            'ready': False,
            'authenticated': False,
            'session_target_match': None,
            'status_text': '尚未启动独立 Runtime',
            'health_error': None,
        }
        if meta:
            systemd_unit = str(meta.get('systemd_unit') or '').strip()
            systemd_pid = self._systemd_whatsapp_runtime_main_pid(systemd_unit) if systemd_unit else None
            if systemd_pid:
                base_runtime['pid'] = systemd_pid
            pid_running = bool(systemd_pid) or self._pid_running(meta.get('pid')) or bool(worker_health)
            if skip_health_check and pid_running and worker_health is None:
                cached_worker_health = meta.get('last_worker_health') if isinstance(meta.get('last_worker_health'), dict) else {}
                approval_payload = cached_worker_health.get('approval_client') if isinstance(cached_worker_health.get('approval_client'), dict) else {}
                current_client_id = str(approval_payload.get('client_id') or cached_worker_health.get('client_id') or '').strip()
                current_auth_path = str(approval_payload.get('auth_path') or cached_worker_health.get('auth_path') or '').strip()
                cached_status = str(approval_payload.get('status') or cached_worker_health.get('status') or '').strip()
                base_runtime.update({
                    'active': True,
                    'status': cached_status or 'running',
                    'ready': bool(approval_payload.get('ready') or cached_worker_health.get('ready')),
                    'authenticated': bool(approval_payload.get('authenticated') or cached_worker_health.get('authenticated')),
                    'last_started_at': approval_payload.get('last_started_at') or cached_worker_health.get('last_started_at') or base_runtime.get('last_started_at'),
                    'last_action_at': approval_payload.get('last_action_at') or cached_worker_health.get('last_action_at') or base_runtime.get('last_action_at'),
                    'last_error': approval_payload.get('last_error') or cached_worker_health.get('last_error') or base_runtime.get('last_error'),
                    'session_target_match': None if (not current_client_id or not current_auth_path) else bool(current_client_id in {expected_client_id, expected_approval_client_id} and current_auth_path == expected_auth_path),
                    'status_text': '独立 Runtime 已启动，使用最近一次服务器健康快照',
                })
                return base_runtime
            if not pid_running and worker_health is None and base_runtime['base_url']:
                try:
                    worker_health = self._request_whatsapp_approval_worker_health(base_runtime['base_url'])
                    pid_running = True
                except Exception as exc:
                    base_runtime['health_error'] = str(exc)
            base_runtime['active'] = pid_running
            if pid_running:
                health_payload = worker_health
                if health_payload is None and base_runtime['base_url']:
                    try:
                        health_payload = self._request_whatsapp_approval_worker_health(base_runtime['base_url'])
                    except Exception as exc:
                        base_runtime['health_error'] = str(exc)
                if isinstance(health_payload, dict) and health_payload:
                    approval_payload = health_payload.get('approval_client') if isinstance(health_payload.get('approval_client'), dict) else {}
                    current_client_id = str(approval_payload.get('client_id') or health_payload.get('client_id') or '').strip()
                    current_auth_path = str(approval_payload.get('auth_path') or health_payload.get('auth_path') or '').strip()
                    base_runtime['status'] = str(approval_payload.get('status') or health_payload.get('status') or '').strip() or 'running'
                    base_runtime['ready'] = bool(approval_payload.get('ready'))
                    base_runtime['authenticated'] = bool(approval_payload.get('authenticated'))
                    base_runtime['last_started_at'] = approval_payload.get('last_started_at') or health_payload.get('last_started_at') or base_runtime.get('last_started_at')
                    base_runtime['last_action_at'] = approval_payload.get('last_action_at') or health_payload.get('last_action_at') or base_runtime.get('last_action_at')
                    base_runtime['last_error'] = approval_payload.get('last_error') or health_payload.get('last_error') or base_runtime.get('last_error')
                    base_runtime['session_target_match'] = None if (not current_client_id or not current_auth_path) else bool(current_client_id in {expected_client_id, expected_approval_client_id} and current_auth_path == expected_auth_path)
                    if (
                        not base_runtime['authenticated']
                        and not base_runtime['ready']
                        and self._whatsapp_approval_runtime_in_localauth_recovery_window(normalized_key, meta)
                    ):
                        base_runtime['status'] = 'recovering'
                        base_runtime['status_text'] = '独立 Runtime 正在恢复服务器登录态'
                        base_runtime['recovering'] = True
                        base_runtime['recovery_grace_seconds'] = WHATSAPP_APPROVAL_LOCALAUTH_RECOVERY_GRACE_SECONDS
                    else:
                        base_runtime['status_text'] = '独立 Runtime 运行中'
                else:
                    base_runtime['status'] = 'running'
                    base_runtime['status_text'] = '独立 Runtime 已启动，健康检查暂未就绪'
            else:
                base_runtime['status'] = 'stopped'
                base_runtime['status_text'] = '独立 Runtime 已停止'
            return base_runtime

        if allow_shared_fallback:
            health_payload = worker_health
            if health_payload is None:
                try:
                    health_payload = self._current_whatsapp_approval_worker_health()
                except Exception as exc:
                    base_runtime['source'] = 'unavailable'
                    base_runtime['health_error'] = str(exc)
                    base_runtime['status'] = 'unavailable'
                    base_runtime['status_text'] = '共享 legacy worker 当前不可达'
                    return base_runtime
            approval_payload = health_payload.get('approval_client') if isinstance(health_payload.get('approval_client'), dict) else {}
            current_client_id = str(approval_payload.get('client_id') or health_payload.get('client_id') or '').strip()
            current_auth_path = str(approval_payload.get('auth_path') or health_payload.get('auth_path') or '').strip()
            config = self.get_production_ops_daemon_config().get('config') or {}
            shared_base_url = str(config.get('worker_base_url') or '').strip().rstrip('/')
            if not shared_base_url:
                return base_runtime
            port_match = re.search(r':(\d+)$', shared_base_url)
            base_runtime.update({
                'active': True,
                'port': int(port_match.group(1)) if port_match else None,
                'base_url': shared_base_url,
                'status': str(approval_payload.get('status') or health_payload.get('status') or '').strip() or 'shared',
                'ready': bool(approval_payload.get('ready')),
                'authenticated': bool(approval_payload.get('authenticated')),
                'last_started_at': approval_payload.get('last_started_at') or health_payload.get('last_started_at') or base_runtime.get('last_started_at'),
                'last_action_at': approval_payload.get('last_action_at') or health_payload.get('last_action_at') or base_runtime.get('last_action_at'),
                'last_error': approval_payload.get('last_error') or health_payload.get('last_error') or base_runtime.get('last_error'),
                'session_target_match': None if (not current_client_id or not current_auth_path) else bool(current_client_id in {expected_client_id, expected_approval_client_id} and current_auth_path == expected_auth_path),
                'status_text': '当前仍在复用共享 legacy worker',
            })
        return base_runtime

    def _build_runtime_registration_group_executor(self, base_url: str):
        from app.registration_group_webjs_executor import WebjsBridgeRegistrationGroupApprovalExecutor

        fallback = self.registration_group_approval_executor
        token = str(getattr(fallback, 'token', '') or os.getenv('REGISTRATION_GROUP_APPROVAL_WEBJS_TOKEN') or '').strip() or None
        timeout_seconds = float(getattr(fallback, 'timeout_seconds', 35.0) or 35.0)
        return WebjsBridgeRegistrationGroupApprovalExecutor(
            base_url=str(base_url or '').strip(),
            token=token,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _resolve_baileys_runtime_value(*sources: Optional[Dict[str, Any]], keys: List[str]) -> str:
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key in keys:
                value = str(source.get(key) or '').strip()
                if value:
                    return value
        return ''

    @staticmethod
    def _source_declares_baileys_provider_runtime(source: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(source, dict):
            return False
        provider_name = str(source.get('provider_name') or source.get('provider') or '').strip().lower()
        if provider_name == 'baileys':
            return True
        runtime_markers = {
            str(source.get(key) or '').strip().lower()
            for key in ('mode', 'source', 'runtime_source', 'runtime_kind', 'provider_runtime')
            if str(source.get(key) or '').strip()
        }
        for marker in runtime_markers:
            if marker in {'baileys_provider_runtime', 'baileys_provider', 'baileys_poc', 'poc_baileys'}:
                return True
            if marker.startswith('baileys_provider') or marker.startswith('poc_baileys'):
                return True
        return False

    def _resolve_baileys_runtime_base_url(
        self,
        *,
        account: Optional[Dict[str, Any]] = None,
        binding: Optional[Dict[str, Any]] = None,
        runtime_state: Optional[Dict[str, Any]] = None,
    ) -> str:
        base_url = self._resolve_baileys_runtime_value(
            runtime_state,
            binding,
            account,
            keys=['baileys_base_url', 'provider_base_url'],
        ).rstrip('/')
        if base_url:
            return base_url
        for source in (runtime_state, binding, account):
            if not self._source_declares_baileys_provider_runtime(source):
                continue
            base_url = self._resolve_baileys_runtime_value(
                source,
                keys=['base_url'],
            ).rstrip('/')
            if base_url:
                return base_url
        return str(os.getenv('REGISTRATION_GROUP_BAILEYS_BASE_URL', '') or os.getenv('MCN_PROBE_BAILEYS_BASE_URL', '') or '').strip().rstrip('/')

    def _resolve_baileys_runtime_token(
        self,
        *,
        account: Optional[Dict[str, Any]] = None,
        binding: Optional[Dict[str, Any]] = None,
        runtime_state: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        token = self._resolve_baileys_runtime_value(
            runtime_state,
            binding,
            account,
            keys=['baileys_token', 'provider_token', 'runtime_token'],
        )
        if token:
            return token
        env_token = str(os.getenv('REGISTRATION_GROUP_BAILEYS_TOKEN', '') or '').strip()
        return env_token or None

    def _preferred_baileys_whatsapp_approval_context(self, row: Dict[str, Any]) -> Dict[str, Any]:
        account = dict(row or {})
        responsible_type = str(account.get('responsible_type') or '').strip()
        try:
            account_metadata = json.loads(str(account.get('notes') or '').strip() or '{}')
        except Exception:
            account_metadata = {}
        if isinstance(account_metadata, dict):
            account.update(account_metadata)
        raw_group_links: Any = []
        try:
            raw_group_links = json.loads(str(account.get('group_links') or '[]'))
        except Exception:
            raw_group_links = []
        raw_bindings = [dict(item or {}) for item in raw_group_links if isinstance(item, dict)] if isinstance(raw_group_links, list) else []
        bindings = _normalize_group_link_bindings(raw_bindings, responsible_type=responsible_type)
        if not bindings and responsible_type == 'group_atmosphere':
            bindings = raw_bindings
        preferred_binding = _preferred_group_binding(bindings)
        explicit_mode = _explicit_whatsapp_runtime_mode(preferred_binding, account)
        if explicit_mode == 'legacy_only':
            return {}
        runtime_config = _whatsapp_approval_runtime_config_from_dict(preferred_binding)
        account_with_runtime = _merge_whatsapp_approval_runtime_configs(runtime_config, account)
        decision = self._resolve_wa_provider_decision(
            account=account_with_runtime,
            binding=preferred_binding,
            responsible_type=responsible_type,
        )
        if str(decision.get('provider_name') or '').strip() != 'baileys':
            return {}
        explicit_baileys_account_id = self._resolve_baileys_runtime_value(
            preferred_binding,
            account_with_runtime,
            keys=['baileys_account_id', 'provider_account_id', 'account_id'],
        )
        base_url = self._resolve_baileys_runtime_base_url(account=account_with_runtime, binding=preferred_binding)
        if not base_url:
            base_url = _default_baileys_provider_base_url()
        token = self._resolve_baileys_runtime_token(account=account_with_runtime, binding=preferred_binding)
        if not base_url and not explicit_baileys_account_id:
            return {}
        baileys_account_id = explicit_baileys_account_id or _default_baileys_account_id_for_whatsapp_account(str(account.get('account_key') or '').strip())
        if baileys_account_id:
            account_with_runtime['baileys_account_id'] = baileys_account_id
            account_with_runtime['provider_account_id'] = baileys_account_id
            account_with_runtime['account_id'] = baileys_account_id
        return {
            'account': account_with_runtime,
            'binding': preferred_binding,
            'baileys_account_id': baileys_account_id,
            'base_url': base_url,
            'token': token,
            'provider_mode': str(decision.get('provider_mode') or '').strip(),
            'provider_decision': decision,
            'expected_login_phone': self._expected_whatsapp_login_phone_identity(account, account_with_runtime, preferred_binding),
        }

    def _request_baileys_provider_health(self, base_url: str, token: Optional[str] = None) -> Dict[str, Any]:
        normalized_base_url = str(base_url or '').strip().rstrip('/')
        if not normalized_base_url:
            raise RuntimeError('Baileys provider base_url is required')
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        response = requests.get(f'{normalized_base_url}/provider/health', headers=headers, timeout=6.0)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError('Baileys provider health must be a JSON object')
        return self._drop_baileys_pairing_code_secrets(payload)

    def _cached_baileys_provider_health(self, base_url: str, token: Optional[str] = None, *, max_age_seconds: float = 30.0) -> Dict[str, Any]:
        normalized_base_url = str(base_url or '').strip().rstrip('/')
        if not normalized_base_url:
            return {}
        cache_key = f'{normalized_base_url}|{str(token or "").strip()}'
        cache = getattr(self, '_baileys_provider_health_cache', None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, '_baileys_provider_health_cache', cache)
        generations = getattr(self, '_baileys_provider_health_cache_generation', None)
        if not isinstance(generations, dict):
            generations = {}
            setattr(self, '_baileys_provider_health_cache_generation', generations)
        request_generation = int(generations.get(cache_key) or 0)
        cached = cache.get(cache_key)
        now_ts = time.time()
        if isinstance(cached, dict):
            try:
                checked_ts = float(cached.get('checked_ts') or 0.0)
            except (TypeError, ValueError):
                checked_ts = 0.0
            if checked_ts and now_ts - checked_ts <= max(float(max_age_seconds), 0.0):
                return dict(cached.get('payload') or {})
        payload = self._drop_baileys_pairing_code_secrets(
            self._request_baileys_provider_health(normalized_base_url, token)
        )
        current_generation = int(generations.get(cache_key) or 0)
        if current_generation == request_generation:
            cache[cache_key] = {'checked_ts': now_ts, 'payload': dict(payload or {})}
            return dict(payload or {})
        latest = cache.get(cache_key)
        return dict(latest.get('payload') or {}) if isinstance(latest, dict) else {}

    def _invalidate_baileys_provider_health_cache(self, base_url: str, token: Optional[str] = None) -> None:
        normalized_base_url = str(base_url or '').strip().rstrip('/')
        if not normalized_base_url:
            return
        cache_key = f'{normalized_base_url}|{str(token or "").strip()}'
        generations = getattr(self, '_baileys_provider_health_cache_generation', None)
        if not isinstance(generations, dict):
            generations = {}
            setattr(self, '_baileys_provider_health_cache_generation', generations)
        generations[cache_key] = int(generations.get(cache_key) or 0) + 1
        cache = getattr(self, '_baileys_provider_health_cache', None)
        if isinstance(cache, dict):
            cache.pop(cache_key, None)

    @staticmethod
    def _baileys_provider_account_from_health(provider_health: Dict[str, Any], baileys_account_id: str) -> Dict[str, Any]:
        normalized_account_id = str(baileys_account_id or '').strip()
        if not normalized_account_id:
            return {}
        accounts = provider_health.get('accounts') if isinstance(provider_health, dict) else []
        if not isinstance(accounts, list):
            return {}
        for item in accounts:
            if not isinstance(item, dict):
                continue
            if str(item.get('accountId') or item.get('account_id') or '').strip() == normalized_account_id:
                return dict(item)
        return {}

    @staticmethod
    def _format_whatsapp_login_phone_candidate(value: Any) -> str:
        raw = str(value or '').strip()
        if not raw:
            return ''
        lowered = raw.lower()
        if '@g.us' in lowered or lowered.endswith('@broadcast'):
            return ''
        local_part = raw.split('@', 1)[0].split(':', 1)[0].strip()
        digits = ''.join(ch for ch in local_part if ch.isdigit())
        if len(digits) < 6:
            return ''
        return format_display_phone(digits)

    @staticmethod
    def _whatsapp_phone_digits(value: Any) -> str:
        return ''.join(ch for ch in str(value or '') if ch.isdigit())

    @classmethod
    def _expected_whatsapp_login_phone_identity(cls, *sources: Optional[Dict[str, Any]]) -> Dict[str, str]:
        direct_keys = (
            'expected_login_phone',
            'expected_phone',
            'account_phone',
            'phone_number',
            'phoneNumber',
            'phone',
            'mobile',
            'account_name',
        )
        fallback_keys = ('account_key', 'baileys_account_id', 'provider_account_id')
        for keys in (direct_keys, fallback_keys):
            for source in sources:
                if not isinstance(source, dict):
                    continue
                for key in keys:
                    if key not in source:
                        continue
                    raw_value = source.get(key)
                    formatted = cls._format_whatsapp_login_phone_candidate(raw_value)
                    digits = cls._whatsapp_phone_digits(formatted or raw_value)
                    if len(digits) >= 6:
                        return {
                            'phone': formatted or format_display_phone(digits),
                            'digits': digits,
                            'source': str(key),
                            'raw': str(raw_value or '').strip(),
                        }
        return {'phone': '', 'digits': '', 'source': '', 'raw': ''}

    @classmethod
    def _extract_whatsapp_login_phone(cls, *sources: Optional[Dict[str, Any]]) -> Dict[str, str]:
        direct_keys = (
            'login_phone',
            'display_phone_number',
            'displayPhoneNumber',
            'phone_number',
            'phoneNumber',
            'phone',
            'mobile',
            'msisdn',
            'jid',
            'id',
            'user_id',
            'userId',
            'wa_id',
            'waId',
        )
        nested_keys = ('user', 'me', 'identity', 'account', 'profile')

        def candidates(payload: Any) -> List[tuple[str, str]]:
            if not isinstance(payload, dict):
                return []
            found: List[tuple[str, str]] = []
            for key in direct_keys:
                if key in payload:
                    value = payload.get(key)
                    if isinstance(value, (str, int)):
                        source_key = str(payload.get('login_phone_source') or '').strip() if key == 'login_phone' else ''
                        found.append((source_key or str(key), str(value)))
            for key in nested_keys:
                nested = payload.get(key)
                if isinstance(nested, dict):
                    for nested_key, nested_value in candidates(nested):
                        found.append((f'{key}.{nested_key}', nested_value))
            return found

        for source in sources:
            for key, raw_value in candidates(source):
                phone = cls._format_whatsapp_login_phone_candidate(raw_value)
                if phone:
                    return {'phone': phone, 'source': key, 'raw': str(raw_value or '').strip()}
        return {'phone': '', 'source': '', 'raw': ''}

    def _baileys_provider_qr_observation(
        self,
        baileys_account_id: str,
        provider_payload: Dict[str, Any],
        *,
        max_age_seconds: float = 45.0,
    ) -> Dict[str, Any]:
        normalized_account_id = str(baileys_account_id or '').strip()
        payload = dict(provider_payload or {})
        qr_payload = str(payload.get('qrImageDataUrl') or payload.get('qrTerminal') or '').strip()
        provider_has_qr = bool(payload.get('hasQr') or qr_payload)
        cache = getattr(self, '_baileys_provider_qr_observation_cache', None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, '_baileys_provider_qr_observation_cache', cache)
        if not normalized_account_id or not provider_has_qr:
            if normalized_account_id:
                cache.pop(normalized_account_id, None)
            return {'has_qr': False, 'qr_payload_present': False, 'qr_stale': False}
        provider_qr_age_ms = payload.get('qrAgeMs')
        provider_qr_max_age_ms = payload.get('qrMaxAgeMs')
        provider_last_qr_at = str(payload.get('lastQrAt') or payload.get('qrUpdatedAt') or '').strip()
        try:
            provider_age_seconds = max(0.0, float(provider_qr_age_ms) / 1000.0)
        except (TypeError, ValueError):
            provider_age_seconds = None
        try:
            provider_max_age_seconds = max(5.0, float(provider_qr_max_age_ms) / 1000.0)
        except (TypeError, ValueError):
            provider_max_age_seconds = max(float(max_age_seconds or 45.0), 5.0)
        if provider_age_seconds is not None:
            stale = bool(payload.get('qrExpired')) or provider_age_seconds > provider_max_age_seconds
            return {
                'has_qr': True,
                'qr_payload_present': bool(qr_payload),
                'qr_fingerprint': hashlib.sha256(qr_payload.encode('utf-8')).hexdigest()[:16] if qr_payload else '',
                'qr_first_seen_at': provider_last_qr_at or None,
                'qr_age_seconds': round(provider_age_seconds, 3),
                'qr_stale': stale,
                'qr_max_age_seconds': provider_max_age_seconds,
            }
        if not qr_payload:
            return {'has_qr': True, 'qr_payload_present': False, 'qr_stale': False}
        fingerprint = hashlib.sha256(qr_payload.encode('utf-8')).hexdigest()
        now_ts = time.time()
        now_at = datetime.now(timezone.utc).isoformat()
        previous = cache.get(normalized_account_id) if isinstance(cache.get(normalized_account_id), dict) else {}
        if previous.get('fingerprint') == fingerprint:
            try:
                first_seen_ts = float(previous.get('first_seen_ts') or now_ts)
            except (TypeError, ValueError):
                first_seen_ts = now_ts
            first_seen_at = str(previous.get('first_seen_at') or now_at)
        else:
            first_seen_ts = now_ts
            first_seen_at = now_at
        age_seconds = max(0.0, now_ts - first_seen_ts)
        stale = bool(age_seconds > max(float(max_age_seconds or 45.0), 5.0))
        observation = {
            'has_qr': True,
            'qr_payload_present': True,
            'qr_fingerprint': fingerprint[:16],
            'qr_first_seen_at': first_seen_at,
            'qr_age_seconds': round(age_seconds, 3),
            'qr_stale': stale,
            'qr_max_age_seconds': max(float(max_age_seconds or 45.0), 5.0),
            'fingerprint': fingerprint,
            'first_seen_ts': first_seen_ts,
            'first_seen_at': first_seen_at,
        }
        cache[normalized_account_id] = observation
        return observation

    def _build_baileys_whatsapp_approval_runtime_and_session(
        self,
        row: Dict[str, Any],
        *,
        include_qr_ascii: bool = False,
        provider_health: Optional[Dict[str, Any]] = None,
        cache_snapshot: bool = True,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
        context = self._preferred_baileys_whatsapp_approval_context(row)
        if not context:
            return {}, {}, False
        account_key = str((row or {}).get('account_key') or '').strip()
        baileys_account_id = str(context.get('baileys_account_id') or '').strip()
        base_url = str(context.get('base_url') or '').strip().rstrip('/')
        token = str(context.get('token') or '').strip() or None
        provider_mode = str(context.get('provider_mode') or '').strip()
        expected_login_phone = dict(context.get('expected_login_phone') or {})
        expected_phone_digits = str(expected_login_phone.get('digits') or '').strip()
        expected_phone_label = str(expected_login_phone.get('phone') or expected_login_phone.get('raw') or '').strip()
        runtime_state: Dict[str, Any] = {
            'account_key': account_key,
            'mode': 'baileys_provider_runtime',
            'source': 'baileys_poc',
            'provider_name': 'baileys',
            'provider_mode': provider_mode,
            'baileys_account_id': baileys_account_id,
            'provider_account_id': baileys_account_id,
            'account_id': baileys_account_id,
            'configured': bool(base_url and baileys_account_id),
            'active': False,
            'base_url': base_url or None,
            'status': 'not_started',
            'ready': False,
            'authenticated': False,
            'session_target_match': True if baileys_account_id else None,
            'status_text': 'Baileys 账号尚未初始化',
            'health_error': None,
            'expected_login_phone': expected_phone_label or None,
            'expected_login_phone_digits': expected_phone_digits or None,
        }
        session_state: Dict[str, Any] = {
            'account_key': account_key,
            'mode': 'baileys_provider',
            'auth_strategy': 'baileys',
            'client_id': baileys_account_id,
            'expected_client_id': baileys_account_id,
            'auth_path': '',
            'expected_auth_path': '',
            'session_target_match': True if baileys_account_id else None,
            'ready': False,
            'authenticated': False,
            'bound': False,
            'login_verified': False,
            'login_check_status': 'pending_runtime' if base_url else 'runtime_unavailable',
            'login_check_message': 'Baileys 账号尚未初始化，点击“二维码”生成登录会话。' if base_url else 'Baileys POC 服务地址未配置。',
            'qr_available': False,
            'qr_text': None,
            'qr_ascii': None,
            'qr_image_data_url': None,
            'can_show_qr': False,
            'can_probe': False,
            'baileys_account_id': baileys_account_id,
            'provider_account_id': baileys_account_id,
            'provider_base_url': base_url or None,
            'expected_login_phone': expected_phone_label or None,
            'expected_login_phone_digits': expected_phone_digits or None,
        }
        if not base_url or not baileys_account_id:
            return runtime_state, session_state, bool(base_url or baileys_account_id)
        try:
            health_payload = dict(
                self._cached_baileys_provider_health(base_url, token)
                if provider_health is None
                else (provider_health or {})
            )
        except Exception as exc:
            runtime_state.update({
                'status': 'unavailable',
                'status_text': 'Baileys POC 当前不可达',
                'health_error': str(exc),
            })
            session_state.update({
                'login_check_status': 'runtime_unavailable',
                'login_check_message': 'Baileys POC 当前不可达，请先恢复 POC 服务。',
                'health_error': str(exc),
            })
            return runtime_state, session_state, True
        account_health = self._baileys_provider_account_from_health(health_payload, baileys_account_id)
        provider_payload = dict(account_health.get('provider') or {}) if isinstance(account_health.get('provider'), dict) else {}
        if not account_health:
            runtime_state.update({
                'status': 'not_started',
                'status_text': 'Baileys 账号尚未初始化',
                'provider_health_ready': bool(health_payload.get('ready')),
            })
            if cache_snapshot:
                self._cache_baileys_whatsapp_approval_session_snapshot(
                    account_key,
                    runtime_state=runtime_state,
                    session_state=session_state,
                    provider_health=health_payload,
                )
            return runtime_state, session_state, True
        provider_ready = bool(provider_payload.get('ready'))
        provider_login_verified = bool(
            provider_payload.get('loginVerified')
            if 'loginVerified' in provider_payload
            else provider_ready
        )
        login_stability_pending = bool(
            provider_payload.get('pairingVerificationPending')
            or provider_payload.get('pairing_verification_pending')
        )
        login_stability_started_at = str(provider_payload.get('lastConnectAt') or '').strip() or None
        login_stability_until = str(
            provider_payload.get('pairingVerificationUntil')
            or provider_payload.get('pairing_verification_until')
            or ''
        ).strip() or None
        try:
            login_stability_remaining_ms = max(
                0,
                int(
                    provider_payload.get('pairingVerificationRemainingMs')
                    or provider_payload.get('pairing_verification_remaining_ms')
                    or 0
                ),
            )
        except (TypeError, ValueError):
            login_stability_remaining_ms = 0
        ready = bool(provider_ready and provider_login_verified and not login_stability_pending)
        initialized = bool(provider_payload.get('initialized'))
        has_pairing_code = bool(provider_payload.get('hasPairingCode'))
        pairing_code_issued_at = str(provider_payload.get('pairingCodeIssuedAt') or '').strip() or None
        pairing_code_expired = bool(
            provider_payload.get('pairingCodeExpired')
            or str(provider_payload.get('pairingFailureCode') or '').strip() == 'pairing_code_expired'
        )
        qr_observation = self._baileys_provider_qr_observation(baileys_account_id, provider_payload)
        raw_has_qr = bool(provider_payload.get('hasQr') or provider_payload.get('qrImageDataUrl') or provider_payload.get('qrTerminal'))
        qr_stale = bool(qr_observation.get('qr_stale'))
        has_qr = bool(raw_has_qr and not qr_stale)
        connection_state = str(provider_payload.get('connectionState') or '').strip()
        actor_health = str(account_health.get('actorHealth') or account_health.get('health') or '').strip()
        auth_dir = str(provider_payload.get('authDir') or '').strip()
        last_disconnect_reason = str(provider_payload.get('lastDisconnectReason') or '').strip()
        reconnect_state = str(provider_payload.get('reconnectState') or '').strip()
        login_phone_identity = self._extract_whatsapp_login_phone(provider_payload, account_health)
        login_phone = str(login_phone_identity.get('phone') or '').strip()
        login_phone_digits = self._whatsapp_phone_digits(login_phone or login_phone_identity.get('raw'))
        session_target_match = True
        if expected_phone_digits and login_phone_digits:
            session_target_match = login_phone_digits == expected_phone_digits
        elif provider_ready and expected_phone_digits and not login_phone_digits:
            session_target_match = False
        if ready and not session_target_match:
            ready = False
        auth_failed = bool(
            reconnect_state == 'stopped'
            or any(marker in last_disconnect_reason for marker in ('401', '403', 'loggedOut', 'forbidden'))
        )
        if not session_target_match:
            runtime_status = 'session_mismatch'
            runtime_text = 'Baileys 登录账号不匹配'
            login_status = 'session_mismatch'
            login_message = '当前 Baileys 会话登录手机号与该审批账号不一致，请重置后重新扫码。'
        elif login_stability_pending:
            runtime_status = 'login_verifying'
            runtime_text = 'Baileys 登录稳定性验证中'
            login_status = 'login_verifying'
            login_message = '扫码已通过，连接已建立；系统正在完成约 30 秒稳定验证。'
        elif ready:
            runtime_status = 'running'
            runtime_text = 'Baileys 已登录'
            login_status = 'passed'
            login_message = 'Baileys 账号已登录，可以正常使用。'
        elif has_pairing_code:
            runtime_status = 'waiting_for_pairing_code'
            runtime_text = 'Baileys 等待输入配对码'
            login_status = 'waiting_for_pairing_code'
            login_message = '配对码已生成，请在手机 WhatsApp 的“已关联设备”中输入。'
        elif pairing_code_expired:
            runtime_status = 'pairing_code_expired'
            runtime_text = 'Baileys 配对码已过期'
            login_status = 'pairing_code_expired'
            login_message = '当前配对码已过期，请手动重新获取。'
        elif has_qr:
            runtime_status = 'waiting_for_scan'
            runtime_text = 'Baileys 等待扫码'
            login_status = 'waiting_for_scan'
            login_message = '已生成 Baileys 登录二维码，等待扫码完成登录。'
        elif qr_stale:
            runtime_status = 'qr_expired'
            runtime_text = 'Baileys 二维码已过期'
            login_status = 'qr_expired'
            login_message = '当前二维码已过期，请重新生成后扫码。'
        elif (
            reconnect_state == 'auth_paused'
            and str(last_disconnect_reason or '').strip() == '408'
            and not bool(provider_payload.get('authRegistered'))
        ):
            runtime_status = 'qr_expired'
            runtime_text = 'Baileys 二维码会话已结束'
            login_status = 'qr_expired'
            login_message = '当前二维码会话已过期，请重新生成后扫码。'
        elif auth_failed:
            runtime_status = 'auth_failed'
            runtime_text = 'Baileys 认证失效'
            login_status = 'auth_failed'
            login_message = 'Baileys 账号认证已失效，请重新生成二维码并扫码登录。'
        elif initialized or connection_state in {'connecting', 'open'}:
            runtime_status = 'initializing'
            runtime_text = 'Baileys 正在连接'
            login_status = 'pending_runtime'
            login_message = 'Baileys 登录会话正在初始化，请稍候刷新。'
        else:
            runtime_status = 'disconnected' if actor_health == 'degraded' else 'not_started'
            runtime_text = 'Baileys 登录态未就绪'
            login_status = 'auth_failed' if actor_health == 'degraded' else 'pending_runtime'
            login_message = 'Baileys 登录态异常或未就绪，需重新生成二维码。' if actor_health == 'degraded' else 'Baileys 账号尚未初始化，点击“二维码”生成登录会话。'
        runtime_state.update({
            'active': True,
            'status': runtime_status,
            'ready': ready,
            'authenticated': ready,
            'session_target_match': session_target_match,
            'status_text': runtime_text,
            'actor_health': actor_health,
            'provider_ready': provider_ready,
            'provider_login_verified': provider_login_verified,
            'provider_initialized': initialized,
            'connection_state': connection_state,
            'auth_path': auth_dir,
            'last_started_at': provider_payload.get('lastConnectAt'),
            'last_action_at': provider_payload.get('lastConnectAt') or provider_payload.get('lastDisconnectAt'),
            'last_error': provider_payload.get('lastError') or provider_payload.get('lastDisconnectReason'),
            'last_disconnect_reason': provider_payload.get('lastDisconnectReason'),
            'reconnect_state': provider_payload.get('reconnectState'),
            'has_qr': raw_has_qr,
            'has_pairing_code': has_pairing_code,
            'pairing_code_issued_at': pairing_code_issued_at,
            'pairing_code_expired': pairing_code_expired,
            'qr_stale': qr_stale,
            'qr_age_seconds': qr_observation.get('qr_age_seconds'),
            'qr_max_age_seconds': qr_observation.get('qr_max_age_seconds'),
            'qr_first_seen_at': qr_observation.get('qr_first_seen_at'),
            'login_phone': login_phone,
            'login_phone_digits': login_phone_digits,
            'login_phone_source': str(login_phone_identity.get('source') or '').strip(),
            'login_phone_raw': str(login_phone_identity.get('raw') or '').strip(),
            'expected_login_phone': expected_phone_label or None,
            'expected_login_phone_digits': expected_phone_digits or None,
            'login_stability_pending': login_stability_pending,
            'login_stability_started_at': login_stability_started_at,
            'login_stability_until': login_stability_until,
            'login_stability_remaining_ms': login_stability_remaining_ms,
        })
        session_state.update({
            'status': connection_state or runtime_status,
            'ready': ready,
            'authenticated': ready,
            'bound': ready,
            'login_verified': ready,
            'login_check_status': login_status,
            'login_check_message': login_message,
            'session_target_match': session_target_match,
            'auth_path': auth_dir,
            'expected_auth_path': auth_dir,
            'qr_available': has_qr,
            'pairing_code_available': has_pairing_code,
            'pairing_code_issued_at': pairing_code_issued_at,
            'pairing_code_expired': pairing_code_expired,
            'qr_image_data_url': (str(provider_payload.get('qrImageDataUrl') or '').strip() or None) if has_qr else None,
            'qr_ascii': None,
            'qr_text': None,
            'can_show_qr': has_qr,
            'can_probe': ready,
            'last_qr_at': qr_observation.get('qr_first_seen_at') or provider_payload.get('lastQrAt') or provider_payload.get('qrUpdatedAt') or provider_payload.get('lastConnectAt'),
            'qr_stale': qr_stale,
            'qr_age_seconds': qr_observation.get('qr_age_seconds'),
            'qr_max_age_seconds': qr_observation.get('qr_max_age_seconds'),
            'last_error': provider_payload.get('lastError'),
            'last_disconnect_reason': provider_payload.get('lastDisconnectReason'),
            'reconnect_state': provider_payload.get('reconnectState'),
            'actor_health': actor_health,
            'provider_initialized': initialized,
            'connection_state': connection_state,
            'login_phone': login_phone,
            'login_phone_digits': login_phone_digits,
            'login_phone_source': str(login_phone_identity.get('source') or '').strip(),
            'login_phone_raw': str(login_phone_identity.get('raw') or '').strip(),
            'expected_login_phone': expected_phone_label or None,
            'expected_login_phone_digits': expected_phone_digits or None,
            'login_stability_pending': login_stability_pending,
            'login_stability_started_at': login_stability_started_at,
            'login_stability_until': login_stability_until,
            'login_stability_remaining_ms': login_stability_remaining_ms,
        })
        if include_qr_ascii and has_qr and not session_state.get('qr_image_data_url'):
            session_state['qr_ascii'] = str(provider_payload.get('qrTerminal') or '').strip() or None
        if cache_snapshot:
            self._cache_baileys_whatsapp_approval_session_snapshot(
                account_key,
                runtime_state=runtime_state,
                session_state=session_state,
                provider_health=health_payload,
            )
        return runtime_state, session_state, True

    def _build_cached_baileys_whatsapp_approval_runtime_and_session(
        self,
        row: Dict[str, Any],
        *,
        include_qr_ascii: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
        """Build the card state from local snapshots only.

        The realtime/list GET paths must never turn a cache miss into a provider
        request or a runtime-meta write. A background account refresh owns those
        mutations and later publishes the refreshed account row to the store.
        """
        account_row = dict(row or {})
        account_key = str(account_row.get('account_key') or '').strip()
        meta = self._read_whatsapp_approval_runtime_meta(account_key)
        raw_bindings = account_row.get('group_link_bindings') or account_row.get('group_links') or []
        if isinstance(raw_bindings, str):
            try:
                raw_bindings = json.loads(raw_bindings)
            except Exception:
                raw_bindings = []
        sources = [account_row, *[item for item in raw_bindings if isinstance(item, dict)]]
        explicit_baileys = any(
            str(source.get('provider_name') or '').strip().lower() == 'baileys'
            or str(source.get('provider_mode') or source.get('registration_group_runtime') or '').strip().lower().startswith('baileys')
            or bool(str(source.get('baileys_account_id') or '').strip())
            for source in sources
        )
        cached_runtime_hint = meta.get('last_runtime_state') if isinstance(meta.get('last_runtime_state'), dict) else {}
        cached_provider_hint = str(
            meta.get('provider_name')
            or meta.get('provider')
            or cached_runtime_hint.get('provider_name')
            or cached_runtime_hint.get('source')
            or ''
        ).strip().lower()
        if not explicit_baileys and cached_provider_hint not in {'baileys', 'baileys_poc'}:
            return {}, {}, False
        context = self._preferred_baileys_whatsapp_approval_context(account_row)
        if not context:
            return {}, {}, False
        try:
            checked_ts = float(meta.get('last_session_checked_ts') or 0.0)
        except (TypeError, ValueError):
            checked_ts = 0.0
        cached_age_seconds = max(0.0, time.time() - checked_ts) if checked_ts else None
        cached_snapshot_fresh = bool(
            checked_ts
            and cached_age_seconds is not None
            and cached_age_seconds <= 90.0
        )
        cached_health = (
            meta.get('last_baileys_provider_health')
            if cached_snapshot_fresh and isinstance(meta.get('last_baileys_provider_health'), dict)
            else {}
        )
        if cached_health:
            runtime_state, session_state, used = self._build_baileys_whatsapp_approval_runtime_and_session(
                account_row,
                include_qr_ascii=include_qr_ascii,
                provider_health=cached_health,
                cache_snapshot=False,
            )
            if used:
                session_state['from_cached_provider_health'] = True
                session_state['cached_session_age_seconds'] = round(cached_age_seconds, 3) if cached_age_seconds is not None else None
                return runtime_state, session_state, True

        runtime_state, session_state, used = self._build_baileys_whatsapp_approval_runtime_and_session(
            account_row,
            include_qr_ascii=include_qr_ascii,
            provider_health={},
            cache_snapshot=False,
        )
        if not used:
            return runtime_state, session_state, False
        cached_runtime = meta.get('last_runtime_state') if isinstance(meta.get('last_runtime_state'), dict) else {}
        cached_session = meta.get('last_session_state') if isinstance(meta.get('last_session_state'), dict) else {}
        cached_provider = str(
            meta.get('provider_name')
            or meta.get('provider')
            or cached_runtime.get('provider_name')
            or cached_runtime.get('source')
            or ''
        ).strip().lower()
        if cached_snapshot_fresh and cached_provider in {'baileys', 'baileys_poc'}:
            runtime_state.update(cached_runtime)
            session_state.update(cached_session)
            session_state['from_cached_session'] = True
            session_state['cached_session_age_seconds'] = round(cached_age_seconds, 3) if cached_age_seconds is not None else None
        return runtime_state, session_state, True

    def _baileys_session_should_reset_for_qr(self, session_state: Dict[str, Any], runtime_state: Optional[Dict[str, Any]] = None) -> bool:
        session = dict(session_state or {})
        runtime = dict(runtime_state or {})
        if bool(session.get('login_verified')) or bool(session.get('authenticated')) or bool(runtime.get('ready')):
            return False
        login_status = str(session.get('login_check_status') or '').strip()
        runtime_status = str(runtime.get('status') or '').strip()
        reconnect_state = str(
            session.get('reconnect_state')
            or session.get('reconnectState')
            or runtime.get('reconnect_state')
            or runtime.get('reconnectState')
            or ''
        ).strip().lower()
        failure_text = ' '.join(
            str(value or '').strip().lower()
            for value in (
                session.get('last_disconnect_reason'),
                session.get('last_error'),
                runtime.get('last_disconnect_reason'),
                runtime.get('last_error'),
            )
        )
        permanent_auth_failure = bool(
            any(marker in failure_text for marker in ('401', '403', 'loggedout', 'forbidden'))
        )
        return bool(
            session.get('qr_stale') is True
            or runtime.get('qr_stale') is True
            or login_status == 'qr_expired'
            or runtime_status == 'qr_expired'
            or permanent_auth_failure
        )

    def _baileys_init_payload_should_reset(self, payload: Dict[str, Any], account_id: str) -> bool:
        account_health = self._baileys_provider_account_from_health(payload or {}, account_id)
        if not account_health:
            return False
        provider_payload = dict(account_health.get('provider') or {}) if isinstance(account_health.get('provider'), dict) else {}
        if bool(provider_payload.get('ready')):
            return False
        actor_health = str(account_health.get('actorHealth') or account_health.get('health') or '').strip()
        last_disconnect_reason = str(provider_payload.get('lastDisconnectReason') or '').strip()
        reconnect_state = str(provider_payload.get('reconnectState') or '').strip()
        connection_state = str(provider_payload.get('connectionState') or '').strip()
        has_qr = bool(provider_payload.get('hasQr') or provider_payload.get('qrImageDataUrl') or provider_payload.get('qrTerminal'))
        permanent_failure_text = ' '.join((last_disconnect_reason, str(provider_payload.get('lastError') or ''))).lower()
        rejected_registered_session = bool(
            last_disconnect_reason == '405'
            and connection_state == 'failed'
            and not has_qr
        )
        unregistered_qrless_timeout = bool(
            provider_payload.get('initialized') is True
            and provider_payload.get('authRegistered') is False
            and last_disconnect_reason == '408'
            and connection_state in {'close', 'closed', 'failed', 'idle'}
            and not has_qr
        )
        if any(marker in permanent_failure_text for marker in ('401', '403', 'loggedout', 'forbidden')):
            return True
        if rejected_registered_session:
            return True
        if unregistered_qrless_timeout:
            return True
        if actor_health == 'degraded' and has_qr and last_disconnect_reason == '408':
            return True
        if has_qr and connection_state == 'connecting' and last_disconnect_reason == '408':
            return True
        return False

    @staticmethod
    def _baileys_reset_action_accepted(payload: Dict[str, Any]) -> bool:
        action = dict(payload or {})
        requested_action = str(action.get('requested_action') or '').strip()
        active_action = str(action.get('action') or '').strip()
        if requested_action:
            return bool(
                requested_action == 'provider/reset'
                and action.get('action_accepted') is True
            )
        if action.get('pending') is True and active_action and active_action != 'provider/reset':
            return False
        return bool(action)

    def _arm_baileys_qr_recovery_intent(self, account_key: str, baileys_account_id: str) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        normalized_account_id = str(baileys_account_id or '').strip()
        if not normalized_key or not normalized_account_id:
            return {}
        now_ts = time.time()
        with self._baileys_qr_recovery_lock:
            meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
            existing = meta.get('baileys_qr_recovery_intent') if isinstance(meta.get('baileys_qr_recovery_intent'), dict) else {}
            try:
                existing_expires_at_ts = float(existing.get('expires_at_ts') or 0.0)
            except (TypeError, ValueError):
                existing_expires_at_ts = 0.0
            try:
                existing_armed_at_ts = float(existing.get('armed_at_ts') or now_ts)
            except (TypeError, ValueError):
                existing_armed_at_ts = now_ts
            try:
                existing_next_attempt_ts = float(existing.get('next_attempt_ts') or now_ts)
            except (TypeError, ValueError):
                existing_next_attempt_ts = now_ts
            try:
                existing_attempts = int(existing.get('attempts') or 0)
            except (TypeError, ValueError):
                existing_attempts = 0
            same_active_intent = bool(
                existing.get('state') == 'armed'
                and str(existing.get('baileys_account_id') or '').strip() == normalized_account_id
                and existing_expires_at_ts > now_ts
            )
            intent = {
                'schema_version': 1,
                'state': 'armed',
                'account_key': normalized_key,
                'baileys_account_id': normalized_account_id,
                'armed_at': str(existing.get('armed_at') or '').strip() if same_active_intent else utc_now(),
                'armed_at_ts': existing_armed_at_ts if same_active_intent else now_ts,
                'expires_at_ts': now_ts + 900.0,
                'attempts': existing_attempts if same_active_intent else 0,
                'next_attempt_ts': min(existing_next_attempt_ts, now_ts) if same_active_intent else now_ts,
                'last_attempt_at': existing.get('last_attempt_at') if same_active_intent else None,
                'last_error': existing.get('last_error') if same_active_intent else None,
            }
            meta['baileys_qr_recovery_intent'] = intent
            self._write_whatsapp_approval_runtime_meta(normalized_key, meta)
            return dict(intent)

    def _complete_baileys_qr_recovery_intent(
        self,
        account_key: str,
        *,
        outcome: str,
        runtime_state: Optional[Dict[str, Any]] = None,
        session_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            return
        with self._baileys_qr_recovery_lock:
            meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
            intent = meta.pop('baileys_qr_recovery_intent', None)
            if not isinstance(intent, dict):
                return
            try:
                attempts = int(intent.get('attempts') or 0)
            except (TypeError, ValueError):
                attempts = 0
            meta['last_baileys_qr_recovery'] = {
                'outcome': str(outcome or '').strip() or 'completed',
                'completed_at': utc_now(),
                'attempts': attempts,
                'qr_available': bool((session_state or {}).get('qr_available')),
                'login_verified': bool((session_state or {}).get('login_verified')),
                'runtime_status': str((runtime_state or {}).get('status') or '').strip(),
            }
            self._write_whatsapp_approval_runtime_meta(normalized_key, meta)

    def _settle_baileys_qr_recovery_intent(
        self,
        account_key: str,
        *,
        runtime_state: Dict[str, Any],
        session_state: Dict[str, Any],
    ) -> None:
        if bool(session_state.get('login_verified')):
            self._complete_baileys_qr_recovery_intent(
                account_key,
                outcome='login_verified',
                runtime_state=runtime_state,
                session_state=session_state,
            )
        elif bool(session_state.get('qr_available') or session_state.get('can_show_qr')):
            with self._baileys_qr_recovery_lock:
                meta = self._read_whatsapp_approval_runtime_meta(account_key)
                intent = meta.get('baileys_qr_recovery_intent') if isinstance(meta.get('baileys_qr_recovery_intent'), dict) else {}
                if intent.get('state') == 'armed':
                    intent['last_qr_observed_at'] = utc_now()
                    intent['next_attempt_ts'] = time.time() + 15.0
                    meta['baileys_qr_recovery_intent'] = intent
                    self._write_whatsapp_approval_runtime_meta(account_key, meta)

    def _run_baileys_qr_recovery_tick(self) -> Dict[str, Any]:
        if not self._baileys_qr_recovery_lock.acquire(blocking=False):
            return {'ok': True, 'skipped': True, 'reason': 'recovery_tick_already_running'}
        checked = 0
        attempted = 0
        completed = 0
        try:
            with self.db.connect() as conn:
                account_keys = [
                    str(row['account_key'] or '').strip()
                    for row in conn.execute(
                        "SELECT account_key FROM whatsapp_approval_accounts WHERE enabled=1 ORDER BY account_key"
                    ).fetchall()
                    if str(row['account_key'] or '').strip()
                ]
            now_ts = time.time()
            for account_key in account_keys:
                meta = self._read_whatsapp_approval_runtime_meta(account_key)
                intent = meta.get('baileys_qr_recovery_intent') if isinstance(meta.get('baileys_qr_recovery_intent'), dict) else {}
                if intent.get('state') != 'armed':
                    continue
                checked += 1
                try:
                    expires_at_ts = float(intent.get('expires_at_ts') or 0.0)
                except (TypeError, ValueError):
                    expires_at_ts = 0.0
                try:
                    attempts = int(intent.get('attempts') or 0)
                except (TypeError, ValueError):
                    attempts = 0
                if not expires_at_ts or expires_at_ts <= now_ts:
                    meta.pop('baileys_qr_recovery_intent', None)
                    meta['last_baileys_qr_recovery'] = {
                        'outcome': 'expired',
                        'completed_at': utc_now(),
                        'attempts': attempts,
                    }
                    self._write_whatsapp_approval_runtime_meta(account_key, meta)
                    completed += 1
                    continue
                try:
                    next_attempt_ts = float(intent.get('next_attempt_ts') or 0.0)
                except (TypeError, ValueError):
                    next_attempt_ts = 0.0
                if next_attempt_ts > now_ts:
                    continue
                if attempts >= 3:
                    intent['state'] = 'manual_required'
                    intent['last_error'] = str(intent.get('last_error') or 'automatic_recovery_attempts_exhausted')
                    meta['baileys_qr_recovery_intent'] = intent
                    self._write_whatsapp_approval_runtime_meta(account_key, meta)
                    completed += 1
                    continue
                account_row = self._get_whatsapp_approval_account_row(account_key)
                context = self._preferred_baileys_whatsapp_approval_context(account_row or {}) if account_row else {}
                base_url = str(context.get('base_url') or '').strip()
                baileys_account_id = str(context.get('baileys_account_id') or '').strip()
                if not base_url or not baileys_account_id or baileys_account_id != str(intent.get('baileys_account_id') or '').strip():
                    intent['state'] = 'manual_required'
                    intent['last_error'] = 'baileys_recovery_context_changed'
                    meta['baileys_qr_recovery_intent'] = intent
                    self._write_whatsapp_approval_runtime_meta(account_key, meta)
                    completed += 1
                    continue
                try:
                    health = self._request_baileys_provider_health(
                        base_url,
                        str(context.get('token') or '').strip() or None,
                    )
                    account_health = self._baileys_provider_account_from_health(health, baileys_account_id)
                    provider = dict(account_health.get('provider') or {}) if isinstance(account_health.get('provider'), dict) else {}
                    if bool(provider.get('ready')):
                        self._complete_baileys_qr_recovery_intent(account_key, outcome='provider_ready')
                        completed += 1
                        continue
                    if bool(provider.get('hasQr') or provider.get('qrImageDataUrl') or provider.get('qrTerminal')):
                        intent['last_qr_observed_at'] = utc_now()
                        intent['next_attempt_ts'] = now_ts + 15.0
                        meta['baileys_qr_recovery_intent'] = intent
                        self._write_whatsapp_approval_runtime_meta(account_key, meta)
                        continue
                    if provider.get('authRegistered') is True:
                        intent['state'] = 'manual_required'
                        intent['last_error'] = 'registered_credentials_preserved'
                        meta['baileys_qr_recovery_intent'] = intent
                        self._write_whatsapp_approval_runtime_meta(account_key, meta)
                        completed += 1
                        continue
                    initialized = provider.get('initialized') is True
                    actionable = self._baileys_init_payload_should_reset(health, baileys_account_id) or not initialized
                    if not actionable:
                        intent['next_attempt_ts'] = now_ts + 15.0
                        meta['baileys_qr_recovery_intent'] = intent
                        self._write_whatsapp_approval_runtime_meta(account_key, meta)
                        continue
                    intent['attempts'] = attempts + 1
                    intent['last_attempt_at'] = utc_now()
                    intent['next_attempt_ts'] = now_ts + 60.0
                    intent['last_error'] = None
                    meta['baileys_qr_recovery_intent'] = intent
                    self._write_whatsapp_approval_runtime_meta(account_key, meta)
                    attempted += 1
                    result = self.start_whatsapp_approval_account_session(
                        account_key,
                        reset=False,
                        arm_recovery=False,
                    )
                    runtime_state = dict(result.get('runtime') or {})
                    session_state = dict(result.get('session') or {})
                    self._settle_baileys_qr_recovery_intent(
                        account_key,
                        runtime_state=runtime_state,
                        session_state=session_state,
                    )
                except Exception as exc:
                    latest_meta = self._read_whatsapp_approval_runtime_meta(account_key)
                    latest_intent = latest_meta.get('baileys_qr_recovery_intent') if isinstance(latest_meta.get('baileys_qr_recovery_intent'), dict) else intent
                    latest_intent['last_error'] = str(exc)[:500]
                    latest_intent['next_attempt_ts'] = time.time() + 60.0
                    latest_meta['baileys_qr_recovery_intent'] = latest_intent
                    self._write_whatsapp_approval_runtime_meta(account_key, latest_meta)
            return {'ok': True, 'checked': checked, 'attempted': attempted, 'completed': completed}
        finally:
            self._baileys_qr_recovery_lock.release()

    def _baileys_qr_recovery_loop(self) -> None:
        self._worker_stop.wait(5.0)
        while not self._worker_stop.is_set():
            self._baileys_qr_recovery_state['last_tick_at'] = utc_now()
            try:
                result = self._run_baileys_qr_recovery_tick()
                self._baileys_qr_recovery_state.update({
                    'last_success_at': utc_now(),
                    'last_error': '',
                    'last_result': dict(result or {}),
                })
            except Exception as exc:
                self._baileys_qr_recovery_state.update({
                    'last_error_at': utc_now(),
                    'last_error': str(exc)[:500],
                })
                print(f'Baileys QR recovery degraded: {exc}')
            self._worker_stop.wait(self._baileys_qr_recovery_poll_interval_seconds)

    def _start_baileys_qr_recovery_worker(self) -> None:
        if self._baileys_qr_recovery_thread and self._baileys_qr_recovery_thread.is_alive():
            return
        thread = threading.Thread(
            target=self._baileys_qr_recovery_loop,
            name='baileys-qr-recovery',
            daemon=True,
        )
        thread.start()
        self._baileys_qr_recovery_thread = thread

    def _initialize_baileys_whatsapp_approval_account(self, row: Dict[str, Any], *, reset: bool = False) -> Dict[str, Any]:
        context = self._preferred_baileys_whatsapp_approval_context(row)
        if not context:
            return {}
        base_url = str(context.get('base_url') or '').strip().rstrip('/')
        account_id = str(context.get('baileys_account_id') or '').strip()
        token = str(context.get('token') or '').strip() or None
        if not base_url or not account_id:
            raise RuntimeError('Baileys account or provider base_url is not configured')
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        endpoint = 'provider/reset' if reset else 'provider/init'
        payload: Dict[str, Any] = {'accountId': account_id}
        if reset:
            payload['clearAuth'] = True
        else:
            payload['allowAuthStart'] = True
        action_key = f'{base_url}|{account_id}'
        wait_seconds = 8.0
        with self._baileys_provider_action_lock:
            existing = self._baileys_provider_actions.get(action_key)
            existing_thread = existing.get('thread') if isinstance(existing, dict) else None
            existing_finished_at = float(existing.get('finished_at') or 0.0) if isinstance(existing, dict) else 0.0
            if existing_thread is not None and existing_thread.is_alive() and not existing_finished_at:
                existing_endpoint = str(existing.get('endpoint') or endpoint)
                reset_queued = bool(reset and existing_endpoint != 'provider/reset')
                if reset_queued:
                    existing['followup_reset'] = True
                return {
                    'ok': True,
                    'status': 'pending',
                    'pending': True,
                    'action': existing_endpoint,
                    'requested_action': endpoint,
                    'accountId': account_id,
                    'deduplicated': not reset_queued,
                    'action_accepted': bool(existing_endpoint == endpoint or reset_queued),
                    'reset_queued': reset_queued,
                    'action_started_at': existing.get('started_at'),
                }
            if isinstance(existing, dict):
                finished_at = float(existing.get('finished_at') or 0.0)
                cached_result = existing.get('result')
                if (
                    str(existing.get('endpoint') or '') == endpoint
                    and cached_result
                    and finished_at
                    and time.time() - finished_at <= 30.0
                ):
                    return {
                        **dict(cached_result),
                        'action': endpoint,
                        'requested_action': endpoint,
                        'action_accepted': True,
                        'cached_action_result': True,
                    }
                self._baileys_provider_actions.pop(action_key, None)

            action_state: Dict[str, Any] = {
                'endpoint': endpoint,
                'started_at': datetime.now(timezone.utc).isoformat(),
                'thread': None,
                'result': None,
                'error': '',
                'finished_at': 0.0,
                'followup_reset': False,
            }

            def run_provider_action() -> None:
                current_endpoint = endpoint
                current_payload = dict(payload)
                while True:
                    try:
                        response = requests.post(
                            f'{base_url}/{current_endpoint}',
                            json=current_payload,
                            headers=headers,
                            timeout=45.0,
                        )
                        try:
                            response_payload = response.json()
                            parsed_json = True
                        except Exception:
                            response_payload = {}
                            parsed_json = False
                        if response.status_code >= 400 and not parsed_json:
                            response.raise_for_status()
                        if not isinstance(response_payload, dict):
                            raise RuntimeError('Baileys provider init must return a JSON object')
                        action_state['result'] = response_payload
                        action_state['error'] = ''
                    except Exception as exc:
                        action_state['error'] = str(exc)
                    with self._baileys_provider_action_lock:
                        run_followup_reset = bool(
                            current_endpoint != 'provider/reset'
                            and action_state.get('followup_reset')
                        )
                        if run_followup_reset:
                            action_state['followup_reset'] = False
                            action_state['endpoint'] = 'provider/reset'
                            action_state['started_at'] = datetime.now(timezone.utc).isoformat()
                            action_state['result'] = None
                            action_state['error'] = ''
                            current_endpoint = 'provider/reset'
                            current_payload = {'accountId': account_id, 'clearAuth': True}
                            continue
                        action_state['finished_at'] = time.time()
                    break

            worker = threading.Thread(
                target=run_provider_action,
                name=f'baileys-provider-{endpoint.replace("/", "-")}-{account_id}',
                daemon=True,
            )
            action_state['thread'] = worker
            self._baileys_provider_actions[action_key] = action_state
            worker.start()

        worker.join(wait_seconds)
        if worker.is_alive():
            return {
                'ok': True,
                'status': 'pending',
                'pending': True,
                'action': endpoint,
                'requested_action': endpoint,
                'action_accepted': True,
                'accountId': account_id,
                'action_started_at': action_state.get('started_at'),
                'message': 'Baileys provider action is continuing in background',
            }
        error_text = str(action_state.get('error') or '').strip()
        if error_text:
            raise RuntimeError(error_text)
        result = action_state.get('result')
        if not isinstance(result, dict):
            raise RuntimeError('Baileys provider init did not return a JSON object')
        return {
            **dict(result),
            'action': str(action_state.get('endpoint') or endpoint),
            'requested_action': endpoint,
            'action_accepted': True,
        }

    def request_whatsapp_approval_account_pairing_code(self, account_key: str, phone_number: str) -> Dict[str, Any]:
        account_row = self._get_whatsapp_approval_account_row(account_key)
        if not account_row:
            raise HTTPException(status_code=404, detail='whatsapp approval account not found')
        context = self._preferred_baileys_whatsapp_approval_context(account_row)
        base_url = str(context.get('base_url') or '').strip().rstrip('/') if context else ''
        account_id = str(context.get('baileys_account_id') or '').strip() if context else ''
        token = str(context.get('token') or '').strip() if context else ''
        if not base_url or not account_id:
            raise HTTPException(
                status_code=409,
                detail={'reason': 'pairing_code_requires_baileys', 'message': '该审批账号未配置 Baileys，不能使用电话号码登录。'},
            )

        digits = self._whatsapp_phone_digits(phone_number)
        if len(digits) < 8 or len(digits) > 15:
            raise HTTPException(
                status_code=400,
                detail={'reason': 'invalid_pairing_phone_number', 'message': '请输入包含国家码的 8–15 位手机号。'},
            )
        expected_identity = dict(context.get('expected_login_phone') or {})
        expected_digits = str(expected_identity.get('digits') or '').strip()
        expected_source = str(expected_identity.get('source') or '').strip()
        trusted_phone_sources = {
            'expected_login_phone', 'expected_phone', 'account_phone', 'phone_number',
            'phoneNumber', 'phone', 'mobile', 'account_name',
        }
        if expected_source not in trusted_phone_sources or len(expected_digits) < 8 or len(expected_digits) > 15:
            raise HTTPException(
                status_code=409,
                detail={
                    'reason': 'pairing_phone_identity_unavailable',
                    'message': '当前审批账号没有可验证的完整手机号，请先补全账号手机号后再使用电话号码登录。',
                },
            )
        if digits != expected_digits:
            raise HTTPException(
                status_code=409,
                detail={'reason': 'pairing_phone_mismatch', 'message': '输入手机号与当前审批账号不一致，请核对后重试。'},
            )

        headers = {'Authorization': f'Bearer {token}'} if token else {}
        try:
            response = requests.post(
                f'{base_url}/provider/pairing-code',
                json={'accountId': account_id, 'phoneNumber': digits},
                headers=headers,
                timeout=30.0,
            )
        except requests.Timeout as exc:
            raise HTTPException(
                status_code=504,
                detail={
                    'reason': 'pairing_code_timeout',
                    'message': '电话号码登录暂时不可用，请切换到二维码登录。系统已安全结束本次请求，不会删除现有登录凭证。',
                },
            ) from exc
        except requests.RequestException as exc:
            raise HTTPException(
                status_code=503,
                detail={'reason': 'pairing_code_provider_unavailable', 'message': 'WhatsApp 登录服务暂不可用，请稍后重试。'},
            ) from exc

        try:
            payload = response.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if response.status_code >= 400 or payload.get('ok') is False:
            messages = {
                'interactive_auth_account_id_required': '登录请求缺少明确审批账号，已拒绝启动。',
                'interactive_auth_state_unavailable': '暂时无法确认其他账号的登录状态，请稍后再试。',
                'invalid_pairing_phone_number': '手机号格式不正确，请输入包含国家码的 8–15 位数字。',
                'pairing_phone_mismatch': '输入手机号与当前审批账号不一致，请核对后重试。',
                'pairing_code_account_already_logged_in': '该账号已经登录，无需再次生成配对码。',
                'pairing_code_existing_credentials_not_ready': '检测到已有登录凭证，但当前尚未在线。系统已保留凭证，请刷新状态后重试；如确认已退出，请改用二维码重新登录。',
                'pairing_code_session_active_for_another_phone': '当前账号已有另一个手机号的配对会话，请稍后再试。',
                'pairing_code_request_in_progress_for_another_phone': '当前账号正在为另一个手机号生成配对码，请稍后再试。',
                'another_account_auth_session_active': '当前有另一个审批账号正在登录，请完成或关闭后再试。',
                'pairing_code_request_cooldown': '请求过于频繁，请等待冷却结束后再试。',
                'pairing_code_rate_limited': 'WhatsApp 暂时限制了配对请求，请稍后再试。',
                'pairing_code_request_timeout': '电话号码登录暂时不可用，请切换到二维码登录。系统已安全结束本次请求，不会删除现有登录凭证。',
                'pairing_code_restart_required': 'WhatsApp 配对连接需要恢复，请稍后再试。',
                'pairing_code_session_changed': 'WhatsApp 登录连接已变化，本次配对码已作废，请稍后重试。',
                'pairing_code_credentials_not_persisted': '配对凭证保存失败，本次登录已安全停止，请稍后重试。',
                'pairing_code_not_supported_for_provider': '当前 WhatsApp 登录服务不支持电话号码登录。',
                'pairing_code_provider_returned_empty_code': 'WhatsApp 未返回有效配对码，请稍后重试。',
                'pairing_code_provider_error': '电话号码登录暂时不可用，请切换到二维码登录。系统已保留现有登录凭证。',
            }
            raw_provider_code = str(payload.get('error') or '').strip()
            provider_code = raw_provider_code if raw_provider_code in messages else 'pairing_code_provider_error'
            try:
                retry_after_ms = max(0, int(payload.get('retryAfterMs') or 0))
            except (TypeError, ValueError):
                retry_after_ms = 0
            safe_status = response.status_code if response.status_code in {400, 409, 429, 503} else 502
            raise HTTPException(
                status_code=safe_status,
                detail={
                    'reason': provider_code,
                    'message': messages.get(provider_code, '生成配对码失败，请稍后重试。'),
                    'retry_after_ms': retry_after_ms,
                },
            )

        pairing_code = re.sub(r'[^0-9A-Za-z]', '', str(payload.get('pairingCode') or '')).upper()
        if len(pairing_code) < 6 or len(pairing_code) > 12:
            raise HTTPException(
                status_code=502,
                detail={'reason': 'invalid_pairing_code_response', 'message': '登录服务返回了无效配对码，请稍后重试。'},
            )
        phone_hint = str(payload.get('phoneMasked') or '').strip()
        if not phone_hint:
            phone_hint = f"{'*' * max(0, len(digits) - 4)}{digits[-4:]}"
        display_code = '-'.join(pairing_code[index:index + 4] for index in range(0, len(pairing_code), 4))
        self._invalidate_baileys_provider_health_cache(base_url, token)
        try:
            retry_after_ms = max(0, int(payload.get('retryAfterMs') or 0))
        except (TypeError, ValueError):
            retry_after_ms = 0
        return {
            'ok': True,
            'account_key': str(account_key or '').strip(),
            'provider': 'baileys',
            'pairing_code': pairing_code,
            'pairing_code_display': display_code,
            'issued_at': str(payload.get('issuedAt') or '').strip() or utc_now(),
            'phone_hint': phone_hint,
            'reused': bool(payload.get('reused')),
            'retry_after_ms': retry_after_ms,
        }

    def _build_runtime_baileys_registration_group_executor(
        self,
        *,
        account: Optional[Dict[str, Any]] = None,
        binding: Optional[Dict[str, Any]] = None,
        runtime_state: Optional[Dict[str, Any]] = None,
    ):
        from app.registration_group_baileys_executor import BaileysRegistrationGroupApprovalExecutor

        fallback = self.registration_group_approval_executor
        timeout_seconds = float(getattr(fallback, 'timeout_seconds', 35.0) or 35.0)
        base_url = self._resolve_baileys_runtime_base_url(
            account=account,
            binding=binding,
            runtime_state=runtime_state,
        )
        token = self._resolve_baileys_runtime_token(
            account=account,
            binding=binding,
            runtime_state=runtime_state,
        )
        return BaileysRegistrationGroupApprovalExecutor(
            base_url=base_url,
            token=token,
            timeout_seconds=timeout_seconds,
        )

    def _find_whatsapp_approval_account_binding(self, *, responsible_type: str, target_group: str) -> Optional[Dict[str, Any]]:
        normalized_target = str(target_group or '').strip().lower()
        normalized_type = str(responsible_type or '').strip().lower()
        if not normalized_target or not normalized_type:
            return None
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT account_key, account_name, responsible_type, group_links, enabled FROM whatsapp_approval_accounts WHERE responsible_type = ? AND enabled = 1 ORDER BY updated_at DESC, account_key ASC",
                (normalized_type,),
            ).fetchall()
        unresolved_match: Optional[Dict[str, Any]] = None
        for row in rows:
            payload = dict(row)
            try:
                bindings = json.loads(payload.get('group_links') or '[]')
            except Exception:
                bindings = []
            normalized_bindings = _normalize_group_link_bindings(bindings if isinstance(bindings, list) else [])
            for binding in normalized_bindings:
                if binding.get('enabled') is False:
                    continue
                runtime_group_id = self._whatsapp_binding_runtime_group_id(binding)
                candidates = {
                    str(binding.get('registration_group') or '').strip().lower(),
                    str(binding.get('group_id') or '').strip().lower(),
                    str(binding.get('link') or '').strip().lower(),
                }
                binding_id = str(binding.get('binding_id') or '').strip().lower()
                if binding_id:
                    candidates.add(binding_id)
                if runtime_group_id:
                    candidates.add(runtime_group_id.lower())
                candidates.discard('')
                if normalized_target in candidates:
                    match = {
                        'account_key': str(payload.get('account_key') or '').strip(),
                        'account_name': str(payload.get('account_name') or '').strip(),
                        'responsible_type': normalized_type,
                        'binding': dict(binding),
                    }
                    if runtime_group_id:
                        return match
                    if unresolved_match is None:
                        unresolved_match = match
        return unresolved_match

    def _resolve_whatsapp_runtime_target_group(self, *, responsible_type: str, target_group: str) -> str:
        normalized_target = str(target_group or '').strip()
        if not normalized_target:
            return ''
        sanitized_target = str(target_group or '').strip()
        if sanitized_target.endswith('@g.us'):
            return sanitized_target
        match = self._find_whatsapp_approval_account_binding(
            responsible_type=responsible_type,
            target_group=normalized_target,
        )
        binding = match.get('binding') if isinstance(match, dict) else {}
        if isinstance(binding, dict):
            return self._whatsapp_binding_runtime_group_id(binding)
        return ''

    def _resolve_whatsapp_approval_runtime_executor(self, *, target_group: str, responsible_type: str) -> Optional[Dict[str, Any]]:
        normalized_type = str(responsible_type or '').strip().lower()
        allow_shared_fallback = False if normalized_type == 'registration_group' else False
        resolved_target = self._resolve_whatsapp_runtime_target_group(
            responsible_type=responsible_type,
            target_group=target_group,
        )
        match = None
        if resolved_target:
            match = self._find_whatsapp_approval_account_binding(responsible_type=responsible_type, target_group=resolved_target)
        if not match:
            match = self._find_whatsapp_approval_account_binding(responsible_type=responsible_type, target_group=target_group)
        if not match:
            return None
        binding = dict(match.get('binding') or {})
        runtime_state = self._build_whatsapp_approval_runtime_state(match['account_key'], allow_shared_fallback=allow_shared_fallback)
        if not resolved_target:
            resolved_target = self._whatsapp_binding_runtime_group_id(binding)
        provider_decision = self.whatsapp_approval_runtime_adapter.provider_decision(
            account={
                'provider_mode': str(runtime_state.get('provider_mode') or '').strip().lower() or str(match.get('provider_mode') or '').strip().lower(),
                'responsible_type': normalized_type,
            },
            binding=binding,
        ).to_dict()
        provider_name = str(provider_decision.get('provider_name') or '').strip()
        legacy_official_alias_target = (
            normalized_type == 'official_group'
            and str(target_group or '').strip().startswith('official-group-')
            and str(binding.get('registration_group') or '').strip().startswith('official-group-')
        )
        if provider_name == 'baileys' and legacy_official_alias_target and not normalize_int_or_none(binding.get('binding_index')):
            provider_name = 'legacy'
        if provider_name == 'baileys':
            baileys_base_url = self._resolve_baileys_runtime_base_url(
                account=match,
                binding=binding,
                runtime_state=runtime_state,
            )
            if not baileys_base_url:
                baileys_base_url = str(runtime_state.get('base_url') or '').strip().rstrip('/')
            if not baileys_base_url:
                return None
            runtime_state['base_url'] = baileys_base_url
            runtime_state.setdefault('provider_name', 'baileys')
            runtime_state.setdefault('provider_mode', str(provider_decision.get('provider_mode') or '').strip())
        elif not runtime_state.get('active') or not runtime_state.get('base_url'):
            return None
        if not resolved_target:
            if provider_name == 'baileys':
                resolved_target = (
                    self._whatsapp_binding_runtime_group_id(binding)
                    or str(binding.get('registration_group') or '').strip()
                    or str(binding.get('link') or '').strip()
                    or str(target_group or '').strip()
                )
                if not resolved_target:
                    return None
            fallback_target = str(binding.get('registration_group') or target_group or '').strip()
            if provider_name != 'baileys' and (not fallback_target or _looks_like_whatsapp_invite_link(fallback_target)):
                return None
            if provider_name != 'baileys':
                resolved_target = fallback_target
        elif legacy_official_alias_target:
            resolved_target = str(binding.get('registration_group') or target_group or '').strip()
        if provider_name == 'baileys':
            executor = self._build_runtime_baileys_registration_group_executor(
                account=match,
                binding=binding,
                runtime_state=runtime_state,
            )
        else:
            executor = self._build_runtime_registration_group_executor(str(runtime_state.get('base_url') or ''))
        return {
            'account_key': match['account_key'],
            'account_name': match.get('account_name'),
            'binding': binding,
            'runtime_state': runtime_state,
            'provider_decision': provider_decision,
            'executor': executor,
            'resolved_target_group': resolved_target,
        }

    def _resolve_whatsapp_approval_runtime_executor_from_hint(
        self,
        *,
        account_key: str,
        responsible_type: str,
        target_group: str = '',
        binding_index: Optional[int] = None,
        binding_target: str = '',
    ) -> Optional[Dict[str, Any]]:
        normalized_account_key = str(account_key or '').strip()
        normalized_type = str(responsible_type or '').strip().lower()
        if not normalized_account_key or not normalized_type:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT account_key, account_name, responsible_type, group_links, area, notify_profile_name,
                       approval_count_threshold, approval_timeout_minutes, auto_recover_worker,
                       schedule_windows, enabled, notes, updated_at
                  FROM whatsapp_approval_accounts
                 WHERE account_key = ?
                   AND responsible_type = ?
                   AND enabled = 1
                """,
                (normalized_account_key, normalized_type),
            ).fetchone()
        if not row:
            return None
        account = dict(row)
        try:
            metadata = json.loads(str(account.get('notes') or '').strip() or '{}')
        except Exception:
            metadata = {}
        if isinstance(metadata, dict):
            account.update(metadata)
        try:
            raw_bindings = json.loads(account.get('group_links') or '[]')
        except Exception:
            raw_bindings = []
        bindings = _normalize_group_link_bindings(raw_bindings if isinstance(raw_bindings, list) else [])
        bindings = [dict(item) for item in bindings if isinstance(item, dict) and item.get('enabled') is not False]
        if not bindings:
            return None
        binding: Optional[Dict[str, Any]] = None
        if binding_index is not None:
            try:
                idx = int(binding_index)
            except Exception:
                idx = -1
            if 0 <= idx < len(bindings):
                binding = dict(bindings[idx])
        wanted = {
            str(target_group or '').strip().lower(),
            str(binding_target or '').strip().lower(),
        }
        wanted.discard('')
        if binding is None and wanted:
            for candidate_binding in bindings:
                runtime_group_id = self._whatsapp_binding_runtime_group_id(candidate_binding)
                candidates = {
                    str(candidate_binding.get('registration_group') or '').strip().lower(),
                    str(candidate_binding.get('group_id') or '').strip().lower(),
                    str(candidate_binding.get('link') or '').strip().lower(),
                    str(candidate_binding.get('binding_id') or '').strip().lower(),
                    str(candidate_binding.get('group_name') or '').strip().lower(),
                }
                if runtime_group_id:
                    candidates.add(runtime_group_id.lower())
                candidates.discard('')
                if candidates.intersection(wanted):
                    binding = dict(candidate_binding)
                    break
        if binding is None and len(bindings) == 1:
            binding = dict(bindings[0])
        if binding is None:
            return None
        runtime_state = self._build_whatsapp_approval_runtime_state(normalized_account_key, allow_shared_fallback=False)
        provider_decision = self.whatsapp_approval_runtime_adapter.provider_decision(
            account={
                'provider_mode': str(runtime_state.get('provider_mode') or account.get('provider_mode') or '').strip().lower(),
                'responsible_type': normalized_type,
            },
            binding=binding,
        ).to_dict()
        provider_name = str(provider_decision.get('provider_name') or '').strip()
        if provider_name == 'baileys':
            runtime_preference = str(
                binding.get('official_group_runtime')
                or binding.get('registration_group_runtime')
                or binding.get('provider_mode')
                or account.get('official_group_runtime')
                or account.get('provider_mode')
                or ''
            ).strip().lower()
            if normalized_type == 'official_group' and runtime_preference in {'legacy_only', 'legacy'}:
                provider_name = 'legacy'
        if provider_name == 'baileys':
            resolved_target = (
                self._whatsapp_binding_runtime_group_id(binding)
                or str(binding.get('registration_group') or '').strip()
                or str(binding.get('link') or '').strip()
                or str(target_group or '').strip()
            )
            baileys_base_url = self._resolve_baileys_runtime_base_url(
                account=account,
                binding=binding,
                runtime_state=runtime_state,
            )
            if not baileys_base_url:
                baileys_base_url = str(runtime_state.get('base_url') or '').strip().rstrip('/')
            if not baileys_base_url:
                return None
            runtime_state['base_url'] = baileys_base_url
            runtime_state.setdefault('provider_name', 'baileys')
            runtime_state.setdefault('provider_mode', str(provider_decision.get('provider_mode') or '').strip())
            executor = self._build_runtime_baileys_registration_group_executor(
                account=account,
                binding=binding,
                runtime_state=runtime_state,
            )
        else:
            resolved_target = (
                str(binding.get('registration_group') or '').strip()
                or str(target_group or '').strip()
                or self._whatsapp_binding_runtime_group_id(binding)
                or str(binding.get('link') or '').strip()
            )
            if not runtime_state.get('active') or not runtime_state.get('base_url'):
                return None
            if not resolved_target or _looks_like_whatsapp_invite_link(resolved_target):
                return None
            executor = self._build_runtime_registration_group_executor(str(runtime_state.get('base_url') or ''))
        return {
            'account_key': normalized_account_key,
            'account_name': account.get('account_name'),
            'binding': binding,
            'runtime_state': runtime_state,
            'provider_decision': provider_decision,
            'executor': executor,
            'resolved_target_group': resolved_target,
        }

    @staticmethod
    def _registration_group_bridge_pending_count(current_truth: Dict[str, Any]) -> Optional[int]:
        value = (current_truth or {}).get('pendingCount')
        if value is None:
            value = (current_truth or {}).get('pending_count')
        try:
            return int(value) if value is not None else None
        except Exception:
            return None

    def _fetch_registration_group_bridge_snapshot(self, *, account: Dict[str, Any], binding: Dict[str, Any]) -> Dict[str, Any]:
        responsible_type = str(binding.get('responsible_type') or account.get('responsible_type') or '').strip().lower()
        if responsible_type not in {'registration_group', 'official_group'}:
            return {}
        if hasattr(self.whatsapp_approval_runtime_adapter, 'provider_decision'):
            provider_decision = self.whatsapp_approval_runtime_adapter.provider_decision(
                account={**dict(account or {}), 'responsible_type': responsible_type},
                binding={**dict(binding or {}), 'responsible_type': responsible_type},
            ).to_dict()
            authoritative_read = bool(provider_decision.get('authoritative_read'))
        else:
            provider_mode = resolve_whatsapp_approval_provider_mode(
                binding=binding,
                account=account,
                responsible_type=responsible_type,
            )
            authoritative_read = provider_mode in {'baileys_authoritative', 'baileys_primary', 'authoritative_read'}
        if not authoritative_read:
            return {}
        runtime_state = dict(account.get('runtime_state') or {})
        executor = self._build_runtime_baileys_registration_group_executor(
            account=account,
            binding=binding,
            runtime_state=runtime_state,
        )
        if not getattr(executor, 'base_url', ''):
            return {}
        registration_group = str(
            self._whatsapp_binding_runtime_group_id(binding)
            or binding.get('group_id')
            or binding.get('registration_group')
            or ''
        ).strip()
        if not registration_group:
            return {}
        if hasattr(executor, 'group_state') and callable(getattr(executor, 'group_state')):
            try:
                snapshot = executor.group_state(
                    registration_group,
                    extra_payload={
                        'group_id': binding.get('group_id'),
                        'group_name': binding.get('group_name'),
                        'link': binding.get('link'),
                        'binding_id': binding.get('binding_id'),
                        'account_key': account.get('account_key') or binding.get('account_key'),
                        'baileys_account_id': binding.get('baileys_account_id') or runtime_state.get('baileys_account_id') or os.getenv('REGISTRATION_GROUP_BAILEYS_ACCOUNT_ID', ''),
                        'provider_mode': binding.get('provider_mode') or runtime_state.get('provider_mode') or account.get('provider_mode'),
                        'priority': 'P0',
                    },
                ) or {}
            except TypeError:
                try:
                    snapshot = executor.group_state(registration_group) or {}
                except Exception:
                    snapshot = {}
            except Exception:
                snapshot = {}
            if isinstance(snapshot, dict) and snapshot:
                return snapshot
        if not hasattr(executor, 'snapshot_state') or not callable(getattr(executor, 'snapshot_state')):
            return {}
        try:
            snapshot = executor.snapshot_state(registration_group) or {}
        except Exception:
            return {}
        return snapshot if isinstance(snapshot, dict) else {}

    def _build_registration_group_bridge_result(
        self,
        *,
        account: Dict[str, Any],
        binding: Dict[str, Any],
        snapshot: Dict[str, Any],
        acquisition_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(snapshot, dict) or not snapshot:
            return {}
        current_truth = dict(snapshot.get('current_truth') or {}) if isinstance(snapshot.get('current_truth'), dict) else {}
        latest_probe = dict(snapshot.get('latest_probe') or {}) if isinstance(snapshot.get('latest_probe'), dict) else {}
        if not current_truth and normalize_int_or_none(snapshot.get('pending_count')) is not None:
            snapshot_source = snapshot.get('source') if isinstance(snapshot.get('source'), dict) else {}
            current_truth = {
                'pendingCount': normalize_int_or_none(snapshot.get('pending_count')),
                'pending_count': normalize_int_or_none(snapshot.get('pending_count')),
                'memberCount': normalize_int_or_none(snapshot.get('member_count')),
                'member_count': normalize_int_or_none(snapshot.get('member_count')),
                'requesterIds': list(snapshot.get('requester_ids') or []) if isinstance(snapshot.get('requester_ids'), list) else [],
                'requester_ids': list(snapshot.get('requester_ids') or []) if isinstance(snapshot.get('requester_ids'), list) else [],
                'requesters': list(snapshot.get('requesters') or []) if isinstance(snapshot.get('requesters'), list) else [],
                'verifiedAt': str(snapshot.get('verified_at') or snapshot.get('source_ts') or snapshot.get('checked_at') or snapshot.get('observed_at') or '').strip() or utc_now(),
                'source': str(snapshot_source.get('mode') or snapshot.get('source') or 'group_state').strip(),
            }
        verification_state = dict(snapshot.get('verification_state') or {}) if isinstance(snapshot.get('verification_state'), dict) else {}
        permission_status = str(snapshot.get('permission_status') or '').strip().lower()
        probe_error = str(snapshot.get('probe_error') or snapshot.get('error') or '').strip()
        self_participant_found = snapshot.get('self_participant_found')
        self_is_admin = snapshot.get('self_is_admin')
        can_manage_membership_requests = snapshot.get('can_manage_membership_requests')
        queue_readable = snapshot.get('queue_readable')
        not_group_member = bool(
            permission_status == 'not_group_member'
            or self_participant_found is False
        )
        not_group_admin = bool(
            not not_group_member
            and (
                permission_status == 'not_group_admin'
                or self_is_admin is False
                or can_manage_membership_requests is False
            )
        )
        pending_count = self._registration_group_bridge_pending_count(current_truth)
        bridge_observed_at = str(
            current_truth.get('verifiedAt')
            or current_truth.get('verified_at')
            or current_truth.get('observedAt')
            or current_truth.get('observed_at')
            or latest_probe.get('observedAt')
            or latest_probe.get('observed_at')
            or ''
        ).strip()
        requester_ids = [str(item).strip() for item in (current_truth.get('requesterIds') or current_truth.get('requester_ids') or []) if str(item).strip()]
        if not requester_ids:
            requester_ids = [str(item).strip() for item in (latest_probe.get('requesterIds') or latest_probe.get('requester_ids') or []) if str(item).strip()]
        if pending_count is not None and pending_count >= 0 and len(requester_ids) > pending_count:
            requester_ids = requester_ids[:pending_count]
        requesters = list(current_truth.get('requesters') or []) if isinstance(current_truth.get('requesters'), list) else []
        if not requesters and isinstance(latest_probe.get('requesters'), list):
            requesters = [dict(item) for item in latest_probe.get('requesters') or [] if isinstance(item, dict)]
        if pending_count is not None and pending_count >= 0 and len(requesters) > pending_count:
            requester_id_set = set(requester_ids)
            ordered_requesters = []
            for requester in requesters:
                requester_id = self._official_group_requester_identity(requester)
                if requester_id_set and requester_id not in requester_id_set:
                    continue
                ordered_requesters.append(requester)
                if len(ordered_requesters) >= pending_count:
                    break
            requesters = ordered_requesters or requesters[:pending_count]
        group_id = str(
            snapshot.get('group_id')
            or snapshot.get('groupId')
            or self._whatsapp_binding_runtime_group_id(binding)
            or binding.get('group_id')
            or ''
        ).strip() or None
        group_name = str(
            snapshot.get('group_name')
            or snapshot.get('groupName')
            or binding.get('group_name')
            or binding.get('registration_group')
            or group_id
            or ''
        ).strip() or None
        result: Dict[str, Any] = {
            'provider': 'baileys',
            'ok': False,
            'display_trusted': False,
            'group_id': group_id,
            'group_name': group_name,
            'pending_count': pending_count,
            'trusted_pending_count': pending_count,
            'api_pending_count': pending_count,
            'ui_pending_count': pending_count,
            'member_count': current_truth.get('memberCount', current_truth.get('member_count')),
            'source_ts': bridge_observed_at or None,
            'verified_at': bridge_observed_at or None,
            'requester_ids': requester_ids,
            'requesters': requesters,
            'stale': bool(current_truth.get('stale')),
            'review_surface_ready': bool(queue_readable) if isinstance(queue_readable, bool) else bool(pending_count is not None),
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': bool(self_participant_found) if isinstance(self_participant_found, bool) else True,
            'self_is_admin': bool(self_is_admin) if isinstance(self_is_admin, bool) else True,
            'can_manage_membership_requests': bool(can_manage_membership_requests) if isinstance(can_manage_membership_requests, bool) else True,
            'empty_queue_visible': bool(pending_count == 0),
            'strong_empty_evidence': bool(pending_count == 0),
            'zero_pending_verified_by': 'registration_group_poc_bridge' if pending_count == 0 else None,
            'fingerprint_quality': 'strong',
            'manual_override_eligible': False,
            'manual_override_mode': None,
            'manual_override_issues': [],
            'bridge_snapshot': snapshot,
            'verification_state': str(verification_state.get('status') or '').strip() or None,
            'source': {
                'provider': 'baileys',
                'mode': 'registration_group_poc_bridge',
                'bridge': 'registration_group_poc',
                'provider_endpoint': snapshot.get('provider_endpoint'),
                'current_truth_source': str(current_truth.get('source') or '').strip() or None,
                'permission_status': permission_status or None,
                'probe_error': probe_error or None,
            },
        }
        responsible_type = str(binding.get('responsible_type') or account.get('responsible_type') or '').strip().lower()
        if responsible_type == 'official_group':
            result['zero_pending_verified_by'] = 'official_group_poc_bridge' if pending_count == 0 else None
            result['source'] = {
                **dict(result.get('source') or {}),
                'mode': 'official_group_poc_bridge',
                'bridge': 'official_group_poc',
            }
        if not_group_member or not_group_admin:
            result['trust_status'] = 'PERMISSION_DENIED'
            result['reason_code'] = 'not_group_member' if not_group_member else 'not_group_admin'
            result['pending_count'] = None
            result['trusted_pending_count'] = None
            result['api_pending_count'] = None
            result['ui_pending_count'] = None
            result['can_manual_approve'] = False
            result['manual_approve_allowed'] = False
            if isinstance(acquisition_result, dict):
                result['acquisition_result'] = acquisition_result
            return result
        if bool(current_truth) and pending_count is not None:
            result['ok'] = True
            result['display_trusted'] = True
            result['trust_status'] = 'TRUSTED_CONFIRMED_PENDING' if pending_count > 0 else 'TRUSTED_CONFIRMED_EMPTY'
            result['reason_code'] = 'official_group_poc_bridge_current_truth_pending_count' if responsible_type == 'official_group' else 'registration_group_poc_bridge_current_truth_pending_count'
            result['can_manual_approve'] = bool(pending_count > 0)
            result['manual_approve_allowed'] = bool(pending_count > 0)
            if isinstance(acquisition_result, dict):
                result['acquisition_result'] = acquisition_result
            return result
        result['trust_status'] = 'TRUTH_UNKNOWN'
        result['reason_code'] = 'official_group_poc_bridge_current_truth_missing' if responsible_type == 'official_group' else 'registration_group_poc_bridge_current_truth_missing'
        result['pending_count'] = None
        result['trusted_pending_count'] = None
        result['api_pending_count'] = None
        result['ui_pending_count'] = None
        result['can_manual_approve'] = False
        result['manual_approve_allowed'] = False
        if isinstance(acquisition_result, dict):
            result['acquisition_result'] = acquisition_result
        return result

    def _render_whatsapp_approval_qr_image_data_url(self, qr_text: str) -> str:
        normalized_qr = str(qr_text or '').strip()
        if not normalized_qr:
            return ''
        script = """
const QRCode = require('qrcode');
QRCode.toDataURL(process.argv[1], { errorCorrectionLevel: 'M', type: 'image/png', margin: 4, scale: 8 })
  .then((url) => { process.stdout.write(url); })
  .catch((err) => { console.error(err && err.stack ? err.stack : String(err)); process.exit(1); });
""".strip()
        completed = subprocess.run(
            ['node', '-e', script, normalized_qr],
            cwd=str(WHATSAPP_APPROVAL_WORKER_ROOT),
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or 'failed to render qr').strip())
        return str(completed.stdout or '').strip()

    def _whatsapp_approval_session_qr_age_seconds(self, session_state: Dict[str, Any]) -> Optional[float]:
        last_qr_at = str((session_state or {}).get('last_qr_at') or '').strip()
        if not last_qr_at:
            return None
        try:
            return max(0.0, (datetime.now(timezone.utc) - parse_iso_datetime(last_qr_at)).total_seconds())
        except Exception:
            return None

    def _whatsapp_approval_session_has_stale_qr(self, session_state: Dict[str, Any], *, max_age_seconds: float = 90.0) -> bool:
        if not (session_state or {}).get('qr_available'):
            return False
        if bool((session_state or {}).get('login_verified')) or bool((session_state or {}).get('authenticated')):
            return False
        age_seconds = self._whatsapp_approval_session_qr_age_seconds(session_state)
        return bool(age_seconds is not None and age_seconds > max_age_seconds)

    def _build_whatsapp_approval_session_state(
        self,
        account_key: str,
        *,
        worker_health: Optional[Dict[str, Any]] = None,
        include_qr_ascii: bool = False,
    ) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        expected_client_id = self._whatsapp_approval_session_client_id(normalized_key)
        expected_approval_client_id = f'{expected_client_id}-approval'
        expected_auth_path = self._whatsapp_approval_session_auth_path(normalized_key)
        payload = dict(worker_health or {})
        approval_payload = payload.get('approval_client') if isinstance(payload.get('approval_client'), dict) else {}
        selected_payload = approval_payload if approval_payload else payload
        current_client_id = str(selected_payload.get('client_id') or payload.get('client_id') or '').strip()
        current_auth_path = str(selected_payload.get('auth_path') or payload.get('auth_path') or '').strip()
        qr_text = str(selected_payload.get('last_qr') or payload.get('last_qr') or '').strip()
        session_target_match = None
        if current_client_id and current_auth_path:
            session_target_match = bool(
                current_client_id in {expected_client_id, expected_approval_client_id}
                and current_auth_path == str(expected_auth_path)
            )
        authenticated = bool(selected_payload.get('authenticated'))
        ready = bool(selected_payload.get('ready'))
        login_verified = bool(authenticated and session_target_match)
        session_status = str(selected_payload.get('status') or payload.get('status') or '').strip()
        last_error = str(selected_payload.get('last_error') or payload.get('last_error') or '').strip()
        last_disconnected_reason = str(selected_payload.get('last_disconnected_reason') or payload.get('last_disconnected_reason') or '').strip()
        combined_failure_text = ' '.join(part for part in [session_status, last_error, last_disconnected_reason] if part).lower()
        restricted_markers = (
            'smb_tos_block',
            'policy violation',
            'account can no longer use whatsapp',
            'this account can no longer use whatsapp',
            'temporarily banned',
            'permanently banned',
            'account restricted',
            'account has been banned',
        )
        meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
        try:
            last_recover_attempt_ts = float((meta or {}).get('last_session_mismatch_recover_attempt_ts') or 0.0)
        except (TypeError, ValueError):
            last_recover_attempt_ts = 0.0
        auto_recovering = bool(session_target_match is False and last_recover_attempt_ts and (time.time() - last_recover_attempt_ts) < 15.0)
        local_auth_recovering = bool(
            not authenticated
            and not ready
            and not qr_text
            and self._whatsapp_approval_runtime_in_localauth_recovery_window(normalized_key, meta)
        )
        if local_auth_recovering:
            qr_text = ''
        if login_verified:
            login_check_status = 'passed'
            login_check_message = '账号已登录，可以正常使用。'
        elif any(marker in combined_failure_text for marker in restricted_markers):
            login_check_status = 'account_restricted'
            login_check_message = '账号疑似受限，需先在手机端核查封禁/限制状态后再处理。'
        elif auto_recovering:
            login_check_status = 'auto_recovering'
            login_check_message = '系统正在自动切回这个账号，请稍候几秒后自动刷新。'
        elif local_auth_recovering:
            login_check_status = 'runtime_recovering'
            login_check_message = '账号已有服务器登录态，运行时正在恢复；请稍候刷新。'
        elif session_target_match is False:
            login_check_status = 'session_mismatch'
            login_check_message = '当前扫码服务还未切到这个账号；点“生成二维码”或“刷新状态”后会按该账号重新校验。'
        elif qr_text:
            login_check_status = 'waiting_for_scan'
            login_check_message = '已生成二维码，等待扫码完成登录。'
        elif session_status.lower() in {'auth_failure', 'failed', 'disconnected'} or last_error or last_disconnected_reason:
            login_check_status = 'auth_failed'
            login_check_message = '登录态异常或已失效，需重新登录后再使用。'
        else:
            login_check_status = 'pending_runtime'
            login_check_message = '正在准备登录会话，请稍候。'
        session = {
            'account_key': normalized_key,
            'auth_strategy': str(selected_payload.get('auth_strategy') or payload.get('auth_strategy') or '').strip(),
            'status': session_status,
            'ready': ready,
            'authenticated': authenticated,
            'client_id': current_client_id,
            'expected_client_id': expected_client_id,
            'expected_approval_client_id': expected_approval_client_id,
            'auth_path': current_auth_path,
            'expected_auth_path': str(expected_auth_path),
            'session_target_match': session_target_match,
            'qr_available': bool(qr_text),
            'qr_text': qr_text if qr_text else None,
            'qr_ascii': None,
            'qr_image_data_url': None,
            'last_qr_at': selected_payload.get('last_qr_at') or payload.get('last_qr_at'),
            'bound': authenticated and session_target_match,
            'mode': 'dedicated_localauth',
            'login_verified': login_verified,
            'login_check_status': login_check_status,
            'login_check_message': login_check_message,
        }
        if include_qr_ascii and qr_text:
            try:
                session['qr_image_data_url'] = self._render_whatsapp_approval_qr_image_data_url(qr_text)
            except Exception as exc:
                session['qr_image_data_url'] = None
                session['qr_render_error'] = str(exc)[:500]
        enriched_session = enrich_whatsapp_login_state(
            session,
            runtime_state=None,
            account_enabled=True,
        )
        self._cache_whatsapp_approval_session_snapshot(normalized_key, enriched_session, payload)
        return enriched_session

    def _get_whatsapp_approval_account_row(self, account_key: str) -> Optional[Dict[str, Any]]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT account_key, account_name, responsible_type, group_links, area, notify_profile_name, approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker, schedule_windows, enabled, verification_status, notes, updated_at FROM whatsapp_approval_accounts WHERE account_key = ?",
                (normalized_key,),
            ).fetchone()
        return dict(row) if row else None

    def _build_baileys_whatsapp_approval_account_status_payload(
        self,
        account_row: Dict[str, Any],
        *,
        runtime_state: Dict[str, Any],
        session_state: Dict[str, Any],
        production_ops: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        serialized = dict(account_row or {})
        account_key = str(serialized.get('account_key') or '').strip()
        responsible_type = str(serialized.get('responsible_type') or '').strip()
        effective_production_ops = production_ops or self._production_ops_daemon_snapshot_light()
        assigned_customer_service_user_ids = self._whatsapp_approval_assigned_customer_service_ids_from_row(serialized)
        serialized['assigned_customer_service_user_ids'] = assigned_customer_service_user_ids
        try:
            raw_group_links = json.loads(serialized.get('group_links') or '[]')
        except Exception:
            raw_group_links = []
        if not isinstance(raw_group_links, list):
            raw_group_links = []
        group_link_bindings: List[Dict[str, Any]] = []
        for idx, item in enumerate(raw_group_links):
            if isinstance(item, dict):
                binding = dict(item)
            else:
                binding = {'link': str(item or '').strip()}
            binding['binding_index'] = normalize_int_or_none(binding.get('binding_index'))
            if binding['binding_index'] is None:
                binding['binding_index'] = idx
            binding['index'] = normalize_int_or_none(binding.get('index'))
            if binding['index'] is None:
                binding['index'] = binding['binding_index']
            binding.setdefault('area', serialized.get('area') or '')
            binding.setdefault('notify_profile_name', serialized.get('notify_profile_name') or '')
            binding = self._apply_account_notify_profile_to_official_binding(
                binding,
                account=serialized,
                responsible_type=responsible_type,
            )
            binding.setdefault('enabled', True)
            group_link_bindings.append(binding)
        group_link_bindings = _normalize_group_link_bindings(group_link_bindings, responsible_type=responsible_type)
        account_provider_decision = self._resolve_wa_provider_decision(
            account=serialized,
            binding=_preferred_group_binding(group_link_bindings),
            runtime_state=runtime_state,
            responsible_type=responsible_type,
        )
        session_state = enrich_whatsapp_login_state(
            session_state,
            runtime_state=runtime_state,
            account_enabled=bool(serialized.get('enabled')),
        )
        ready = bool(session_state.get('login_verified')) or bool(session_state.get('can_probe')) or (
            bool(runtime_state.get('ready')) and bool(runtime_state.get('authenticated'))
        )
        has_qr = bool(session_state.get('qr_available') or session_state.get('can_show_qr') or runtime_state.get('has_qr'))
        login_status = str(session_state.get('login_check_status') or session_state.get('login_state') or '').strip()
        runtime_status_raw = str(runtime_state.get('status') or '').strip()
        health_error = str(runtime_state.get('health_error') or '').strip()
        if ready:
            status_text = '运行中'
            status_color = 'green'
            verification_status = 'ready'
            runtime_status = 'active'
        elif has_qr or login_status in {'waiting_for_scan', 'waiting_for_scan_qr_ready', 'waiting_for_scan_qr_pending'}:
            status_text = '待扫码'
            status_color = 'amber'
            verification_status = 'pending_login'
            runtime_status = 'starting'
        elif health_error or login_status in {'auth_failed', 'login_failed', 'runtime_unavailable', 'account_restricted', 'session_mismatch'}:
            status_text = '登录异常'
            status_color = 'amber'
            verification_status = 'pending_login'
            runtime_status = 'error'
        else:
            status_text = '待登录'
            status_color = 'amber' if runtime_status_raw and runtime_status_raw != 'not_started' else 'gray'
            verification_status = 'pending_login'
            runtime_status = 'inactive'

        binding_runtimes: List[Dict[str, Any]] = []
        for binding in group_link_bindings:
            runtime_row = dict(binding or {})
            runtime_row['approval_scope'] = responsible_type
            runtime_row['provider_name'] = account_provider_decision.get('provider_name')
            runtime_row['provider_mode'] = account_provider_decision.get('provider_mode')
            runtime_row['provider_decision'] = account_provider_decision
            runtime_row['monitoring_effective'] = bool(serialized.get('enabled')) and bool(runtime_row.get('enabled', True)) and ready
            runtime_row['monitoring_status_text'] = '监控中' if runtime_row.get('monitoring_effective') else ('不监控' if runtime_row.get('enabled') is False else '待登录后生效')
            runtime_row['target_group_label'] = str(
                runtime_row.get('group_name')
                or runtime_row.get('group_id')
                or runtime_row.get('registration_group')
                or runtime_row.get('link')
                or ''
            ).strip()
            if responsible_type in {'registration_group', 'official_group'}:
                self._apply_approval_queue_truth_to_binding(
                    account_key,
                    runtime_row,
                    account={**dict(serialized or {}), 'runtime_state': runtime_state, 'responsible_type': responsible_type},
                    production_ops=effective_production_ops,
                )
            runtime_row['membership_verifier'] = serialize_membership_verifier({
                'ready': ready,
                'status': 'baileys_provider_ready' if ready else 'login_unready',
                'detail': 'Baileys 账号已登录，可执行群操作。' if ready else 'Baileys 账号未登录，需扫码或恢复会话。',
                'source': 'baileys_provider_runtime',
            })
            binding_runtimes.append(runtime_row)

        serialized.update({
            'group_link_bindings': group_link_bindings,
            'group_binding_runtimes': binding_runtimes,
            'group_links': [str(item.get('link') or '').strip() for item in group_link_bindings if str(item.get('link') or '').strip()],
            'group_count': len(group_link_bindings),
            'runtime_state': runtime_state,
            'session_state': session_state,
            'provider_name': account_provider_decision.get('provider_name'),
            'provider_mode': account_provider_decision.get('provider_mode'),
            'provider_capabilities': account_provider_decision.get('provider_capabilities') or {},
            'provider_decision': account_provider_decision,
            'approval_scope': responsible_type,
            'runtime_status': runtime_status,
            'verification_status': verification_status,
            'verification_status_label': '可投产' if verification_status == 'ready' else '待补齐',
            'status_text': status_text,
            'status_color': status_color,
            'monitor_runtime_active': ready,
            'provider_monitor_enabled': True,
            'login_phone': str(session_state.get('login_phone') or runtime_state.get('login_phone') or '').strip(),
            'service_scope': {
                'code': 'baileys_provider',
                'label': 'Baileys 账号运行时',
                'ready': ready,
                'detail': 'Baileys provider 已登录，可按账号路由审批/刷新' if ready else 'Baileys provider 已配置，等待账号登录',
                'runtime': {
                    'provider_name': account_provider_decision.get('provider_name'),
                    'provider_mode': account_provider_decision.get('provider_mode'),
                    'baileys_account_id': runtime_state.get('baileys_account_id') or runtime_state.get('provider_account_id'),
                    'base_url': runtime_state.get('base_url'),
                    'status': runtime_state.get('status'),
                    'ready': bool(runtime_state.get('ready')),
                    'authenticated': bool(runtime_state.get('authenticated')),
                    'configured': bool(runtime_state.get('configured')),
                    'health_error': health_error or None,
                },
            },
            'membership_verifier': serialize_membership_verifier({
                'ready': ready,
                'status': 'baileys_provider_ready' if ready else 'login_unready',
                'detail': 'Baileys 账号已登录，可执行群操作。' if ready else 'Baileys 账号未登录，需扫码或恢复会话。',
                'source': 'baileys_provider_runtime',
            }),
            'list_mode': 'baileys_status',
        })
        return serialized

    def get_whatsapp_approval_account_runtime(self, account_key: str) -> Dict[str, Any]:
        account_row = self._get_whatsapp_approval_account_row(account_key)
        if not account_row:
            raise HTTPException(status_code=404, detail='whatsapp approval account not found')
        baileys_runtime_state, baileys_session_state, baileys_used = self._build_baileys_whatsapp_approval_runtime_and_session(
            account_row,
            include_qr_ascii=False,
        )
        if baileys_used:
            baileys_session_state = enrich_whatsapp_login_state(
                baileys_session_state,
                runtime_state=baileys_runtime_state,
                account_enabled=bool(account_row.get('enabled')),
            )
            return {
                'account': self._build_baileys_whatsapp_approval_account_status_payload(
                    account_row,
                    runtime_state=baileys_runtime_state,
                    session_state=baileys_session_state,
                ),
                'runtime': baileys_runtime_state,
                'session': baileys_session_state,
            }
        runtime_state = self._build_whatsapp_approval_runtime_state(account_key)
        return {
            'account': self._build_whatsapp_approval_account_runtime(account_row, runtime_state=runtime_state),
            'runtime': runtime_state,
        }

    def _whatsapp_approval_runtime_systemd_unit(self, account_key: str) -> str:
        return str(self._whatsapp_approval_runtime_identity(account_key)['systemd_unit'])

    def _should_use_systemd_whatsapp_runtime(self) -> bool:
        mode = str(os.getenv('WHATSAPP_APPROVAL_RUNTIME_SUPERVISOR') or '').strip().lower()
        if mode in {'popen', 'process', 'subprocess', 'none', 'disabled'}:
            return False
        if mode in {'systemd', 'systemctl'}:
            return True
        return platform.system().lower() == 'linux' and bool(shutil.which('systemd-run') and shutil.which('systemctl'))

    def _systemd_whatsapp_runtime_main_pid(self, unit: str) -> Optional[int]:
        if not unit or not shutil.which('systemctl'):
            return None
        try:
            completed = subprocess.run(
                ['systemctl', 'show', unit, '--property=MainPID', '--value'],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            pid = int(str(completed.stdout or '').strip() or '0')
            return pid if pid > 0 else None
        except Exception:
            return None

    def _start_whatsapp_approval_runtime_systemd(
        self,
        *,
        account_key: str,
        port: int,
        auth_path: Path,
        log_path: Path,
        reset: bool,
        env: Dict[str, str],
    ) -> Dict[str, Any]:
        if not shutil.which('systemd-run'):
            raise HTTPException(status_code=500, detail='systemd-run not available for whatsapp runtime')
        unit = self._whatsapp_approval_runtime_systemd_unit(account_key)
        subprocess.run(['systemctl', 'stop', unit], capture_output=True, text=True, timeout=15, check=False)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        node_prefix = str(os.getenv('WHATSAPP_APPROVAL_NODE_PATH') or '/root/.nvm/versions/node/v24.12.0/bin').strip()
        runtime_env = dict(env)
        runtime_env['HOME'] = str(os.getenv('WHATSAPP_APPROVAL_RUNTIME_HOME') or '/root')
        runtime_env['PATH'] = f"{node_prefix}:{runtime_env.get('PATH') or os.getenv('PATH') or '/usr/local/bin:/usr/bin:/bin'}"
        command = [
            'systemd-run',
            '--unit', unit,
            '--collect',
            '--property', f'WorkingDirectory={WHATSAPP_APPROVAL_WORKER_ROOT}',
            '--property', 'Restart=on-failure',
            '--property', 'RestartSec=5',
            '--property', 'KillMode=control-group',
        ]
        for key in sorted(runtime_env):
            if key.startswith('REGISTRATION_GROUP_APPROVAL_WEBJS_') or key in {'PATH', 'HOME', 'PUPPETEER_EXECUTABLE_PATH'}:
                command.extend(['--setenv', f'{key}={runtime_env[key]}'])
        shell_cmd = f"exec npm start >> {shlex.quote(str(log_path))} 2>&1"
        command.extend(['/bin/bash', '-lc', shell_cmd])
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode != 0:
            raise HTTPException(status_code=500, detail=(completed.stderr or completed.stdout or 'systemd runtime start failed')[-1000:])
        deadline = time.time() + 10.0
        main_pid: Optional[int] = None
        while time.time() < deadline:
            main_pid = self._systemd_whatsapp_runtime_main_pid(unit)
            if main_pid:
                break
            time.sleep(0.25)
        return {
            'systemd_unit': unit,
            'pid': main_pid,
            'port': port,
            'base_url': f'http://127.0.0.1:{port}',
            'auth_path': str(auth_path),
            'client_id': self._whatsapp_approval_session_client_id(account_key),
            'log_path': str(log_path),
            'started_at': utc_now(),
            'reset': reset,
            'supervisor': 'systemd',
        }

    def start_whatsapp_approval_account_runtime(self, account_key: str, *, reset: bool = False) -> Dict[str, Any]:
        with self._whatsapp_approval_runtime_lock:
            account_row = self._get_whatsapp_approval_account_row(account_key)
            if not account_row:
                raise HTTPException(status_code=404, detail='whatsapp approval account not found')
            normalized_key = str(account_key or '').strip()
            baileys_context = self._preferred_baileys_whatsapp_approval_context(account_row)
            if baileys_context:
                runtime_state, session_state, _ = self._build_baileys_whatsapp_approval_runtime_and_session(
                    account_row,
                    include_qr_ascii=False,
                )
                session_state = enrich_whatsapp_login_state(
                    session_state,
                    runtime_state=runtime_state,
                    account_enabled=bool(account_row.get('enabled')),
                )
                return {
                    'started': False,
                    'skipped_legacy_runtime': True,
                    'provider': 'baileys',
                    'reason': 'baileys_provider_uses_external_runtime',
                    'account': self._build_baileys_whatsapp_approval_account_status_payload(
                        account_row,
                        runtime_state=runtime_state,
                        session_state=session_state,
                    ),
                    'runtime': runtime_state,
                    'session': session_state,
                }
            existing_meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
            if existing_meta and not reset:
                existing_runtime_state = self._build_whatsapp_approval_runtime_state(normalized_key, allow_shared_fallback=False)
                if bool(existing_runtime_state.get('active')) and bool(existing_runtime_state.get('session_target_match')):
                    worker_health: Dict[str, Any] = {}
                    base_url = str(existing_runtime_state.get('base_url') or '').strip()
                    if base_url:
                        try:
                            worker_health = self._request_whatsapp_approval_worker_health(base_url)
                        except Exception:
                            worker_health = {}
                    if worker_health:
                        existing_session_state = self._build_whatsapp_approval_session_state(
                            normalized_key,
                            worker_health=worker_health,
                            include_qr_ascii=False,
                        )
                        if self._whatsapp_approval_session_has_stale_qr(existing_session_state):
                            reset = True
                        else:
                            return {
                                'started': True,
                                'reused_existing_runtime': True,
                                'reset': False,
                                'account': self._build_whatsapp_approval_account_runtime(account_row, runtime_state=existing_runtime_state, worker_health=worker_health),
                                'runtime': existing_runtime_state,
                                'meta': existing_meta,
                            }
            if existing_meta:
                self.stop_whatsapp_approval_account_runtime(normalized_key)
            self._ensure_whatsapp_approval_runtime_capacity(normalized_key)
            auth_path = self._whatsapp_approval_session_auth_path(normalized_key)
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            if reset and auth_path.exists():
                shutil.rmtree(auth_path)
            port = self._pick_whatsapp_approval_runtime_port(normalized_key)
            base_url = f'http://127.0.0.1:{port}'
            runtime_generation = int(existing_meta.get('runtime_generation') or 0) + 1
            log_path = self._whatsapp_approval_runtime_log_path(normalized_key)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update({
                'REGISTRATION_GROUP_APPROVAL_WEBJS_PORT': str(port),
                'REGISTRATION_GROUP_APPROVAL_WEBJS_HOST': '127.0.0.1',
                'REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_MODE': 'dedicated_localauth',
                'REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_DATA_PATH': str(auth_path),
                'REGISTRATION_GROUP_APPROVAL_WEBJS_CLIENT_ID': self._whatsapp_approval_session_client_id(normalized_key),
                'REGISTRATION_GROUP_APPROVAL_WEBJS_EVENT_LOG': str(log_path.with_suffix('.jsonl')),
            })
            system_chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
            if os.path.exists(system_chrome):
                env.setdefault('PUPPETEER_EXECUTABLE_PATH', system_chrome)
                env.setdefault('REGISTRATION_GROUP_APPROVAL_WEBJS_CHROME_EXECUTABLE', system_chrome)
            if self._should_use_systemd_whatsapp_runtime():
                meta = self._start_whatsapp_approval_runtime_systemd(
                    account_key=normalized_key,
                    port=port,
                    auth_path=auth_path,
                    log_path=log_path,
                    reset=reset,
                    env=env,
                )
                meta['runtime_generation'] = runtime_generation
                meta = self._write_whatsapp_approval_runtime_meta(normalized_key, meta)
            else:
                with log_path.open('a', encoding='utf-8') as log_file:
                    proc = subprocess.Popen(
                        ['npm', 'start'],
                        cwd=str(WHATSAPP_APPROVAL_WORKER_ROOT),
                        env=env,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                meta = self._write_whatsapp_approval_runtime_meta(normalized_key, {
                    'account_key': normalized_key,
                    'pid': proc.pid,
                    'port': port,
                    'base_url': base_url,
                    'auth_path': str(auth_path),
                    'client_id': self._whatsapp_approval_session_client_id(normalized_key),
                    'log_path': str(log_path),
                    'started_at': utc_now(),
                    'reset': reset,
                    'runtime_generation': runtime_generation,
                    'supervisor': 'popen',
                })
            deadline = time.time() + 60.0
            worker_health: Dict[str, Any] = {}
            last_error = ''
            while time.time() < deadline:
                try:
                    worker_health = self._request_whatsapp_approval_worker_health(base_url)
                    break
                except Exception as exc:
                    last_error = str(exc)
                    time.sleep(0.5)
            if not worker_health:
                self.stop_whatsapp_approval_account_runtime(normalized_key)
                raise HTTPException(status_code=500, detail=last_error or 'failed to start dedicated whatsapp approval runtime')
            runtime_state = self._build_whatsapp_approval_runtime_state(normalized_key, worker_health=worker_health, allow_shared_fallback=False)
            return {
                'started': True,
                'reset': reset,
                'account': self._build_whatsapp_approval_account_runtime(account_row, runtime_state=runtime_state, worker_health=worker_health),
                'runtime': runtime_state,
                'meta': meta,
            }

    def stop_whatsapp_approval_account_runtime(self, account_key: str) -> Dict[str, Any]:
        with self._whatsapp_approval_runtime_lock:
            normalized_key = str(account_key or '').strip()
            meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
            pid = meta.get('pid')
            auth_path = str(meta.get('auth_path') or self._whatsapp_approval_session_auth_path(normalized_key)).strip()
            systemd_unit = str(meta.get('systemd_unit') or '').strip()
            if systemd_unit:
                try:
                    subprocess.run(
                        ['systemctl', 'stop', systemd_unit],
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=10,
                    )
                except Exception:
                    pass
            runtime_pids: List[int] = []
            if pid:
                try:
                    runtime_pids.append(int(pid))
                except (TypeError, ValueError):
                    pass
            runtime_pids.extend(self._list_whatsapp_approval_runtime_processes(auth_path))
            self._terminate_whatsapp_approval_runtime_processes(runtime_pids)
            if meta:
                meta['stopped_at'] = utc_now()
                self._write_whatsapp_approval_runtime_meta(normalized_key, meta)
            runtime_state = self._build_whatsapp_approval_runtime_state(normalized_key, allow_shared_fallback=False)
            runtime_state['active'] = False
            runtime_state['status'] = 'stopped'
            runtime_state['status_text'] = '独立 Runtime 已停止'
            return {
                'stopped': True,
                'runtime': runtime_state,
            }

    def get_whatsapp_approval_account_session(self, account_key: str, *, include_qr_ascii: bool = True) -> Dict[str, Any]:
        account_row = self._get_whatsapp_approval_account_row(account_key)
        if not account_row:
            raise HTTPException(status_code=404, detail='whatsapp approval account not found')
        baileys_runtime_state, baileys_session_state, baileys_used = self._build_baileys_whatsapp_approval_runtime_and_session(
            account_row,
            include_qr_ascii=include_qr_ascii,
        )
        if baileys_used:
            baileys_session_state = enrich_whatsapp_login_state(
                baileys_session_state,
                runtime_state=baileys_runtime_state,
                account_enabled=bool(account_row.get('enabled')),
            )
            return {
                'account': self._build_baileys_whatsapp_approval_account_status_payload(
                    account_row,
                    runtime_state=baileys_runtime_state,
                    session_state=baileys_session_state,
                ),
                'runtime': baileys_runtime_state,
                'session': baileys_session_state,
            }
        runtime_state = self._build_whatsapp_approval_runtime_state(account_key)
        worker_health: Dict[str, Any] = {}
        health_error = ''
        if runtime_state.get('active') and runtime_state.get('base_url') and runtime_state.get('source') == 'dedicated':
            try:
                worker_health = self._request_whatsapp_approval_worker_health(str(runtime_state.get('base_url') or ''))
            except Exception as exc:
                health_error = str(exc)
                runtime_state['health_error'] = health_error
                runtime_state['ready'] = False
                runtime_state['authenticated'] = False
                runtime_state['status'] = 'unavailable'
                runtime_state['status_text'] = '扫码服务暂不可用，请重新生成二维码'
        elif runtime_state.get('source') == 'shared':
            try:
                worker_health = self._current_whatsapp_approval_worker_health()
            except Exception as exc:
                health_error = str(exc)
                runtime_state['health_error'] = health_error
                runtime_state['ready'] = False
                runtime_state['authenticated'] = False
                runtime_state['status'] = 'unavailable'
                runtime_state['status_text'] = '共享扫码服务暂不可用'
        session_state = self._build_whatsapp_approval_session_state(account_key, worker_health=worker_health, include_qr_ascii=include_qr_ascii)
        if health_error:
            session_state['login_verified'] = False
            session_state['qr_available'] = False
            session_state['login_check_status'] = 'runtime_unavailable'
            session_state['login_check_message'] = '扫码服务连接失败，请点击“重新生成二维码”。'
            session_state['health_error'] = health_error
        runtime_state, session_state, worker_health, _ = self._maybe_auto_recover_whatsapp_approval_account_session(
            account_key,
            runtime_state=runtime_state,
            session_state=session_state,
            worker_health=worker_health,
        )
        session_state = enrich_whatsapp_login_state(
            session_state,
            runtime_state=runtime_state,
            account_enabled=bool(account_row.get('enabled')),
        )
        return {
            'account': self._build_whatsapp_approval_account_runtime(account_row, runtime_state=runtime_state, worker_health=worker_health, session_state=session_state),
            'runtime': runtime_state,
            'session': session_state,
        }

    def start_whatsapp_approval_account_session(
        self,
        account_key: str,
        *,
        reset: bool = False,
        arm_recovery: bool = True,
    ) -> Dict[str, Any]:
        account_row = self._get_whatsapp_approval_account_row(account_key)
        if not account_row:
            raise HTTPException(status_code=404, detail='whatsapp approval account not found')
        baileys_context = self._preferred_baileys_whatsapp_approval_context(account_row)
        if baileys_context and str(baileys_context.get('base_url') or '').strip():
            init_payload: Dict[str, Any] = {}
            init_error = ''
            effective_reset = bool(reset)
            baileys_account_id = str(baileys_context.get('baileys_account_id') or '').strip()
            if arm_recovery:
                self._arm_baileys_qr_recovery_intent(account_key, baileys_account_id)
            try:
                if not effective_reset:
                    try:
                        current_health = self._request_baileys_provider_health(
                            str(baileys_context.get('base_url') or '').strip(),
                            str(baileys_context.get('token') or '').strip() or None,
                        )
                    except Exception:
                        current_health = {}
                    if current_health:
                        effective_reset = self._baileys_init_payload_should_reset(
                            current_health,
                            baileys_account_id,
                        )
                init_payload = self._initialize_baileys_whatsapp_approval_account(account_row, reset=effective_reset)
                if effective_reset:
                    effective_reset = self._baileys_reset_action_accepted(init_payload)
                if (
                    not effective_reset
                    and init_payload
                    and self._baileys_init_payload_should_reset(init_payload, baileys_account_id)
                ):
                    init_payload = self._initialize_baileys_whatsapp_approval_account(account_row, reset=True)
                    effective_reset = self._baileys_reset_action_accepted(init_payload)
            except Exception as exc:
                init_error = str(exc)
            runtime_state, session_state, _ = self._build_baileys_whatsapp_approval_runtime_and_session(
                account_row,
                include_qr_ascii=True,
                provider_health=init_payload if init_payload else None,
            )
            if (
                not init_error
                and not effective_reset
                and (
                    session_state.get('login_check_status') == 'session_mismatch'
                    or self._baileys_session_should_reset_for_qr(session_state, runtime_state)
                )
            ):
                try:
                    init_payload = self._initialize_baileys_whatsapp_approval_account(account_row, reset=True)
                    effective_reset = self._baileys_reset_action_accepted(init_payload)
                    runtime_state, session_state, _ = self._build_baileys_whatsapp_approval_runtime_and_session(
                        account_row,
                        include_qr_ascii=True,
                        provider_health=init_payload if init_payload else None,
                    )
                except Exception as reset_exc:
                    init_error = str(reset_exc)
            if init_error:
                runtime_state['health_error'] = init_error
                runtime_state['status'] = 'unavailable'
                runtime_state['status_text'] = 'Baileys POC 初始化失败'
                session_state['login_verified'] = False
                session_state['login_check_status'] = 'runtime_unavailable'
                session_state['login_check_message'] = f'Baileys 初始化失败：{init_error}'
                session_state['health_error'] = init_error
            elif bool(init_payload.get('pending')) and not (
                bool(session_state.get('login_verified')) or bool(session_state.get('qr_available'))
            ):
                deadline = time.monotonic() + 12.0
                while time.monotonic() < deadline:
                    try:
                        live_health = self._request_baileys_provider_health(
                            str(baileys_context.get('base_url') or '').strip(),
                            str(baileys_context.get('token') or '').strip() or None,
                        )
                        runtime_state, session_state, _ = self._build_baileys_whatsapp_approval_runtime_and_session(
                            account_row,
                            include_qr_ascii=True,
                            provider_health=live_health,
                        )
                    except Exception as poll_exc:
                        init_error = str(poll_exc)
                        break
                    if bool(session_state.get('login_verified')) or bool(session_state.get('qr_available')):
                        init_payload = live_health
                        break
                    if self._baileys_session_should_reset_for_qr(session_state, runtime_state) and not effective_reset:
                        try:
                            init_payload = self._initialize_baileys_whatsapp_approval_account(account_row, reset=True)
                            effective_reset = self._baileys_reset_action_accepted(init_payload)
                        except Exception as reset_exc:
                            init_error = str(reset_exc)
                            break
                    time.sleep(0.4)
            session_state = enrich_whatsapp_login_state(
                session_state,
                runtime_state=runtime_state,
                account_enabled=bool(account_row.get('enabled')),
            )
            started = bool(
                not init_error
                and init_payload.get('ok') is not False
                and (session_state.get('login_verified') or session_state.get('qr_available'))
            )
            pending = bool(
                not started
                and not init_error
                and init_payload.get('pending') is True
                and init_payload.get('action_accepted') is True
            )
            if pending:
                reset_queued = bool(init_payload.get('reset_queued'))
                runtime_state['status'] = 'initializing'
                runtime_state['status_text'] = '正在排队重建二维码登录会话' if reset_queued else '二维码登录会话正在初始化'
                session_state['login_state'] = 'waiting_for_scan_qr_pending'
                session_state['login_check_status'] = 'pending_runtime'
                session_state['login_check_message'] = (
                    '检测到旧初始化仍在结束，重置已安全排队；系统会串行生成新二维码，请保持弹窗打开。'
                    if reset_queued
                    else '二维码登录会话仍在初始化；系统会继续生成，请保持弹窗打开。'
                )
            if not started and not init_error:
                session_state['login_check_message'] = str(
                    session_state.get('login_check_message')
                    or '本次未生成可用二维码，请稍后重试。'
                )
            self._settle_baileys_qr_recovery_intent(
                account_key,
                runtime_state=runtime_state,
                session_state=session_state,
            )
            return {
                'started': started,
                'pending': pending,
                'reset': effective_reset,
                'provider': 'baileys',
                'init_result': init_payload,
                'account': self._build_baileys_whatsapp_approval_account_status_payload(
                    account_row,
                    runtime_state=runtime_state,
                    session_state=session_state,
                ),
                'runtime': runtime_state,
                'session': session_state,
            }
        last_runtime_state: Dict[str, Any] = {}
        last_start_error = ''
        last_health_error = ''
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            runtime_result = self.start_whatsapp_approval_account_runtime(account_key, reset=(reset and attempt == 1))
            runtime_state = dict(runtime_result.get('runtime') or {})
            last_runtime_state = dict(runtime_state)
            base_url = str(runtime_state.get('base_url') or '').strip()
            worker_health: Dict[str, Any] = {}
            try:
                response = requests.post(f'{base_url}/warmup', timeout=15.0)
                response.raise_for_status()
                worker_health = response.json()
                if not isinstance(worker_health, dict):
                    raise HTTPException(status_code=500, detail='runtime warmup must return a JSON object')
            except Exception as exc:
                last_start_error = str(exc)
                try:
                    worker_health = self._request_whatsapp_approval_worker_health(base_url)
                except Exception as health_exc:
                    last_health_error = str(health_exc)
                    if attempt < max_attempts:
                        try:
                            self.stop_whatsapp_approval_account_runtime(account_key)
                        except Exception:
                            pass
                        time.sleep(1.0)
                        continue
                    runtime_state['health_error'] = last_health_error
                    runtime_state['status'] = 'unavailable'
                    runtime_state['ready'] = False
                    runtime_state['authenticated'] = False
                    runtime_state['status_text'] = '独立 Runtime 多次自愈仍未稳定'
                    session_state = self._build_whatsapp_approval_session_state(account_key, worker_health={}, include_qr_ascii=True)
                    session_state['login_check_status'] = 'runtime_unstable'
                    session_state['login_check_message'] = '扫码服务多次自愈仍未稳定，系统已停止本轮异常服务。'
                    session_state['start_error'] = last_start_error
                    session_state['health_error'] = last_health_error
                    session_state['auto_recover_attempts'] = max_attempts
                    try:
                        self.stop_whatsapp_approval_account_runtime(account_key)
                    except Exception:
                        pass
                    return {
                        'started': False,
                        'reset': reset,
                        'auto_recover_attempts': max_attempts,
                        'account': self._build_whatsapp_approval_account_runtime(account_row, runtime_state=runtime_state, worker_health={}),
                        'runtime': runtime_state,
                        'session': session_state,
                    }
            runtime_state = self._build_whatsapp_approval_runtime_state(account_key, worker_health=worker_health, allow_shared_fallback=False)
            session_state = self._build_whatsapp_approval_session_state(account_key, worker_health=worker_health, include_qr_ascii=True)
            return {
                'started': True,
                'reset': reset,
                'auto_recover_attempts': attempt,
                'account': self._build_whatsapp_approval_account_runtime(account_row, runtime_state=runtime_state, worker_health=worker_health),
                'runtime': runtime_state,
                'session': session_state,
            }
        # Defensive fallback; loop always returns.
        session_state = self._build_whatsapp_approval_session_state(account_key, worker_health={}, include_qr_ascii=True)
        session_state['login_check_status'] = 'runtime_unstable'
        session_state['login_check_message'] = '扫码服务未稳定。'
        return {
            'started': False,
            'reset': reset,
            'auto_recover_attempts': max_attempts,
            'account': self._build_whatsapp_approval_account_runtime(account_row, runtime_state=last_runtime_state, worker_health={}),
            'runtime': last_runtime_state,
            'session': session_state,
        }

    def reset_whatsapp_approval_account_session(self, account_key: str) -> Dict[str, Any]:
        return self.start_whatsapp_approval_account_session(account_key, reset=True)

    def recover_whatsapp_approval_account_runtime(self, account_key: str) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            raise HTTPException(status_code=400, detail='account_key is required')
        account_row = self._get_whatsapp_approval_account_row(normalized_key)
        if not account_row:
            raise HTTPException(status_code=404, detail='whatsapp approval account not found')
        baileys_runtime_state, baileys_session_state, baileys_used = self._build_baileys_whatsapp_approval_runtime_and_session(
            account_row,
            include_qr_ascii=False,
        )
        if baileys_used:
            baileys_session_state = enrich_whatsapp_login_state(
                baileys_session_state,
                runtime_state=baileys_runtime_state,
                account_enabled=bool(account_row.get('enabled')),
            )
            meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
            if meta:
                meta.pop('manual_recovery_in_progress', None)
                meta.pop('manual_recovery_started_ts', None)
                meta.pop('manual_recovery_started_at', None)
                self._write_whatsapp_approval_runtime_meta(normalized_key, meta)
            baileys_ready = bool(baileys_session_state.get('login_verified')) or bool(baileys_session_state.get('can_probe')) or (
                bool(baileys_runtime_state.get('ready')) and bool(baileys_runtime_state.get('authenticated'))
            )
            if baileys_ready:
                return {
                    'started': False,
                    'recovery_locked': False,
                    'recovery_in_progress': False,
                    'already_production_ready': True,
                    'skipped_legacy_runtime': True,
                    'provider': 'baileys',
                    'reason': 'baileys_provider_skip_webjs_runtime_recovery',
                    'account': self._build_baileys_whatsapp_approval_account_status_payload(
                        account_row,
                        runtime_state=baileys_runtime_state,
                        session_state=baileys_session_state,
                    ),
                    'runtime': baileys_runtime_state,
                    'session': baileys_session_state,
                }
            return {
                'started': False,
                'recovery_locked': False,
                'recovery_in_progress': False,
                'skipped_legacy_runtime': True,
                'provider': 'baileys',
                'auth_start_required': True,
                'reason': 'baileys_auth_start_requires_operator',
                'account': self._build_baileys_whatsapp_approval_account_status_payload(
                    account_row,
                    runtime_state=baileys_runtime_state,
                    session_state=baileys_session_state,
                ),
                'runtime': baileys_runtime_state,
                'session': baileys_session_state,
            }
        meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
        runtime_state = self._build_whatsapp_approval_runtime_state(
            normalized_key,
            allow_shared_fallback=False,
            skip_health_check=True,
        )
        cached_session = self._cached_whatsapp_approval_session_snapshot(normalized_key, max_age_seconds=300.0)
        if bool(cached_session.get('login_verified')) or bool(cached_session.get('can_probe')) or (
            bool(runtime_state.get('ready')) and bool(runtime_state.get('authenticated'))
        ):
            meta.pop('manual_recovery_in_progress', None)
            meta.pop('manual_recovery_started_ts', None)
            meta.pop('manual_recovery_started_at', None)
            if meta:
                self._write_whatsapp_approval_runtime_meta(normalized_key, meta)
            return {
                'started': False,
                'recovery_locked': False,
                'already_production_ready': True,
                'account': self._build_whatsapp_approval_account_runtime(account_row, runtime_state=runtime_state, worker_health={}),
                'runtime': runtime_state,
                'session': cached_session,
            }
        now_ts = time.time()
        try:
            recovery_started_ts = float(meta.get('manual_recovery_started_ts') or 0.0)
        except (TypeError, ValueError):
            recovery_started_ts = 0.0
        if meta.get('manual_recovery_in_progress') and recovery_started_ts and now_ts - recovery_started_ts < 180.0:
            return {
                'started': False,
                'recovery_locked': True,
                'recovery_in_progress': True,
                'recovery_lock_remaining_seconds': round(max(0.0, 180.0 - (now_ts - recovery_started_ts)), 3),
                'account': self._build_whatsapp_approval_account_runtime(account_row, runtime_state=runtime_state, worker_health={}),
                'runtime': runtime_state,
                'session': cached_session,
            }
        meta['manual_recovery_in_progress'] = True
        meta['manual_recovery_started_ts'] = now_ts
        meta['manual_recovery_started_at'] = utc_now()
        self._write_whatsapp_approval_runtime_meta(normalized_key, meta)
        result = self.start_whatsapp_approval_account_session(normalized_key, reset=False)
        session_state = dict(result.get('session') or {})
        runtime_state = dict(result.get('runtime') or {})
        if bool(session_state.get('login_verified')) or bool(session_state.get('can_probe')) or (
            bool(runtime_state.get('ready')) and bool(runtime_state.get('authenticated'))
        ):
            meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
            meta.pop('manual_recovery_in_progress', None)
            meta.pop('manual_recovery_started_ts', None)
            meta.pop('manual_recovery_started_at', None)
            if meta:
                self._write_whatsapp_approval_runtime_meta(normalized_key, meta)
        result['recovery_locked'] = not (
            bool(session_state.get('login_verified')) or bool(session_state.get('can_probe')) or (
                bool(runtime_state.get('ready')) and bool(runtime_state.get('authenticated'))
            )
        )
        result['recovery_in_progress'] = bool(result.get('recovery_locked'))
        return result

    def _maybe_auto_recover_whatsapp_approval_account_session(
        self,
        account_key: str,
        *,
        runtime_state: Optional[Dict[str, Any]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        worker_health: Optional[Dict[str, Any]] = None,
        cooldown_seconds: float = 30.0,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], bool]:
        normalized_key = str(account_key or '').strip()
        runtime_state = dict(runtime_state or {})
        session_state = dict(session_state or {})
        worker_health = dict(worker_health or {})
        if not normalized_key:
            return runtime_state, session_state, worker_health, False
        if str(session_state.get('login_check_status') or '').strip() != 'session_mismatch':
            return runtime_state, session_state, worker_health, False
        if str(runtime_state.get('source') or '').strip() != 'dedicated':
            return runtime_state, session_state, worker_health, False

        meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
        if not meta:
            return runtime_state, session_state, worker_health, False

        now_ts = time.time()
        try:
            last_attempt_ts = float(meta.get('last_session_mismatch_recover_attempt_ts') or 0.0)
        except (TypeError, ValueError):
            last_attempt_ts = 0.0
        if last_attempt_ts and (now_ts - last_attempt_ts) < max(float(cooldown_seconds), 1.0):
            return runtime_state, session_state, worker_health, False

        meta['last_session_mismatch_recover_attempt_ts'] = now_ts
        meta['last_session_mismatch_recover_attempt_at'] = utc_now()
        self._write_whatsapp_approval_runtime_meta(normalized_key, meta)

        try:
            recovered = self.start_whatsapp_approval_account_session(normalized_key)
        except Exception as exc:
            latest_meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
            latest_meta['last_session_mismatch_recover_attempt_ts'] = now_ts
            latest_meta['last_session_mismatch_recover_attempt_at'] = meta.get('last_session_mismatch_recover_attempt_at')
            latest_meta['last_session_mismatch_recover_error'] = str(exc)
            self._write_whatsapp_approval_runtime_meta(normalized_key, latest_meta)
            return runtime_state, session_state, worker_health, False

        recovered_runtime = dict(recovered.get('runtime') or runtime_state)
        recovered_session = dict(recovered.get('session') or session_state)
        recovered_worker_health: Dict[str, Any] = {}
        recovered_base_url = str(recovered_runtime.get('base_url') or '').strip()
        if recovered_runtime.get('active') and recovered_base_url and str(recovered_runtime.get('source') or '').strip() == 'dedicated':
            try:
                recovered_worker_health = self._request_whatsapp_approval_worker_health(recovered_base_url)
            except Exception:
                recovered_worker_health = dict(worker_health or {})
        elif worker_health:
            recovered_worker_health = dict(worker_health or {})

        latest_meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
        latest_meta['last_session_mismatch_recover_attempt_ts'] = now_ts
        latest_meta['last_session_mismatch_recover_attempt_at'] = meta.get('last_session_mismatch_recover_attempt_at')
        latest_meta['last_session_mismatch_recover_error'] = None
        if recovered_session.get('login_verified'):
            latest_meta['last_session_mismatch_recovered_at'] = utc_now()
        self._write_whatsapp_approval_runtime_meta(normalized_key, latest_meta)
        return recovered_runtime, recovered_session, recovered_worker_health, True

    def _get_whatsapp_approval_account_runtime_row(self, account_key: str) -> Dict[str, Any]:
        account_row = self._get_whatsapp_approval_account_row(account_key)
        if not account_row:
            raise HTTPException(status_code=404, detail='whatsapp approval account not found')
        production_ops = self._production_ops_daemon_snapshot()
        official_bridge = self._official_group_bridge_summary_payload()
        try:
            shared_worker_health = self._current_whatsapp_approval_worker_health()
        except Exception:
            shared_worker_health = {}
        built, _ = self._build_whatsapp_approval_account_runtime_with_auto_recover(
            account_row,
            production_ops=production_ops,
            official_bridge=official_bridge,
            shared_worker_health=shared_worker_health,
        )
        return built

    def _get_whatsapp_approval_account_runtime_row_lightweight(self, account_key: str) -> Dict[str, Any]:
        account_row = self._get_whatsapp_approval_account_row(account_key)
        if not account_row:
            raise HTTPException(status_code=404, detail='whatsapp approval account not found')
        production_ops = self._production_ops_daemon_snapshot_light()
        runtime_state, session_state, account_worker_health = self._build_whatsapp_approval_lightweight_runtime_snapshot(account_row)
        built = self._build_whatsapp_approval_account_runtime(
            account_row,
            production_ops=production_ops,
            official_bridge={'configured': False, 'health': {}, 'summary': {}, 'lightweight': True},
            worker_health=account_worker_health,
            runtime_state=runtime_state,
            session_state=session_state,
            skip_live_probe=True,
            read_only=True,
        )
        built['list_mode'] = 'lightweight'
        return built

    def _get_whatsapp_approval_account_runtime_row_provider_snapshot(self, account_key: str) -> Dict[str, Any]:
        """Refresh one account's provider health outside the HTTP GET path."""
        account_row = self._get_whatsapp_approval_account_row(account_key)
        if not account_row:
            raise HTTPException(status_code=404, detail='whatsapp approval account not found')
        runtime_state, session_state, account_worker_health = self._build_whatsapp_approval_lightweight_runtime_snapshot(
            account_row,
            refresh_provider_health=True,
        )
        built = self._build_whatsapp_approval_account_runtime(
            account_row,
            production_ops=self._production_ops_daemon_snapshot_light(),
            official_bridge={'configured': False, 'health': {}, 'summary': {}, 'lightweight': True},
            worker_health=account_worker_health,
            runtime_state=runtime_state,
            session_state=session_state,
            skip_live_probe=True,
            read_only=True,
        )
        built['list_mode'] = 'background_provider_refresh'
        return built

    def _get_whatsapp_approval_account_runtime_row_for_operation(self, account_key: str) -> Dict[str, Any]:
        """Read the fast account snapshot, falling back to the live builder for legacy callers/tests."""
        try:
            return self._get_whatsapp_approval_account_runtime_row_lightweight(account_key)
        except HTTPException as exc:
            if getattr(exc, 'status_code', None) != 404:
                raise
            return self._get_whatsapp_approval_account_runtime_row(account_key)

    def _get_whatsapp_approval_binding_runtime_snapshot(self, account_key: str, binding_index: int) -> Optional[Dict[str, Any]]:
        account = self._get_whatsapp_approval_account_runtime_row_for_operation(account_key)
        bindings = account.get('group_binding_runtimes') if isinstance(account.get('group_binding_runtimes'), list) else account.get('group_link_bindings')
        if not isinstance(bindings, list) or binding_index < 0 or binding_index >= len(bindings):
            return None
        binding_runtime = dict(bindings[binding_index] or {})
        binding_runtime.pop('operation_state', None)
        return binding_runtime

    def _sync_manual_registration_group_approval_to_production_ops_state(
        self,
        *,
        binding: Dict[str, Any],
        probe: Optional[Dict[str, Any]],
        approved_at: Optional[str],
    ) -> None:
        state_path = PRODUCTION_OPS_DAEMON_STATE_PATH
        if not state_path:
            return
        try:
            state = load_json_state(state_path)
            approved_at_text = str(approved_at or '').strip()
            approved_dt = None
            if approved_at_text:
                try:
                    approved_dt = datetime.fromisoformat(approved_at_text.replace('Z', '+00:00'))
                except Exception:
                    approved_dt = None
            if approved_dt is None:
                approved_dt = utc_now()
            if approved_dt.tzinfo is None:
                approved_dt = approved_dt.replace(tzinfo=timezone.utc)
            probe_payload = dict(probe or {})
            fingerprint = requester_fingerprint(probe_payload)
            actions = state.setdefault('actions', {})
            if not isinstance(actions, dict):
                actions = {}
                state['actions'] = actions
            actions['last_registration_trigger'] = {
                'fingerprint': fingerprint,
                'at': approved_dt.isoformat(),
            }
            anchors = state.setdefault('registration_cycle_anchors', {})
            if not isinstance(anchors, dict):
                anchors = {}
                state['registration_cycle_anchors'] = anchors
            anchor_text = approved_dt.isoformat()
            candidate_keys = [
                str(binding.get('registration_group') or '').strip(),
                str(binding.get('group_id') or '').strip(),
                str(probe_payload.get('group_id') or '').strip(),
                str(binding.get('link') or '').strip(),
                str(binding.get('group_name') or '').strip(),
                str(probe_payload.get('group_name') or '').strip(),
            ]
            for candidate in candidate_keys:
                if candidate:
                    anchors[candidate] = anchor_text
            save_json_state(state_path, state)
        except Exception:
            return

    def _send_registration_group_binding_notification(
        self,
        *,
        binding: Dict[str, Any],
        incident: Dict[str, Any],
        cycle: Dict[str, Any],
        event_type: str,
    ) -> Dict[str, Any]:
        notify_profile_name = str(binding.get('notify_profile_name') or '').strip()
        notify_robot_name = str(binding.get('notify_robot_name') or self._notify_robot_name(notify_profile_name) or '').strip()
        payload = {
            'code': str(incident.get('code') or '').strip() or None,
            'dedupe_key': str(incident.get('dedupe_key') or '').strip() or None,
            'notify_profile_name': notify_profile_name or None,
            'notify_robot_name': notify_robot_name or None,
        }
        if not notify_profile_name:
            payload['status'] = 'skipped_notify_profile_missing'
            return payload
        targets = self._expand_notify_profile_targets(notify_profile_name, notify_robot_name)
        if not targets:
            payload['status'] = 'skipped_no_notifier'
            return payload
        group_name = str(((cycle.get('monitor_target') or {}) if isinstance(cycle.get('monitor_target'), dict) else {}).get('group_name') or '').strip() or None
        deliveries: List[Dict[str, Any]] = []
        for target in targets:
            target_profile_name = str(target.get('profile_name') or '').strip()
            target_robot_name = str(target.get('robot_name') or '').strip()
            delivery = {
                'notify_profile_name': target_profile_name or None,
                'notify_robot_name': target_robot_name or None,
            }
            env_values = self._load_profile_env_map(target_profile_name)
            app_id = str(env_values.get('LARK_APP_ID') or env_values.get('FEISHU_APP_ID') or '').strip()
            app_secret = str(env_values.get('LARK_APP_SECRET') or env_values.get('FEISHU_APP_SECRET') or '').strip()
            chat_id = str(env_values.get('LARK_HOME_CHANNEL') or env_values.get('FEISHU_HOME_CHANNEL') or '').strip()
            domain = str(env_values.get('LARK_DOMAIN') or env_values.get('FEISHU_DOMAIN') or 'lark').strip() or 'lark'
            if not app_id or not app_secret or not chat_id:
                delivery['status'] = 'skipped_no_notifier'
                deliveries.append(delivery)
                continue
            adapter = LiveLarkReplyAdapter(app_id=app_id, app_secret=app_secret, domain=domain)
            effective_cycle = {
                **cycle,
                'monitor_target': {
                    'notify_profile_name': target_profile_name,
                    'notify_robot_name': target_robot_name or None,
                    'group_name': group_name,
                },
            }
            message_text = format_lark_alert('production-ops-daemon', incident, effective_cycle)
            if should_suppress_lark_alert(incident, effective_cycle, message_text):
                delivery['status'] = 'skipped_suppressed_alert'
                delivery['suppressed_reason'] = 'invalid_registration_group_invite_404'
                deliveries.append(delivery)
                continue
            try:
                self.external_call_rate_limiter.allow(f'registration-group-notify:{target_profile_name}')
                response = adapter.send_text(chat_id=chat_id, text=message_text)
                delivery['status'] = 'sent'
                delivery['response'] = response
                delivery['message_text'] = message_text
                with self.db.connect() as conn:
                    self._record_audit_event(
                        conn,
                        event_type=event_type,
                        event_source='registration_group_manual_approval',
                        payload={
                            'account_key': str(binding.get('account_key') or '').strip() or None,
                            'registration_group': str(cycle.get('registration_group') or '').strip() or None,
                            'configured_registration_group': str(cycle.get('configured_registration_group') or '').strip() or None,
                            'group_name': group_name,
                            'notify_profile_name': target_profile_name,
                            'notify_robot_name': target_robot_name or None,
                            'message_text': message_text,
                            'response': response,
                            'dedupe_key': payload['dedupe_key'],
                        },
                    )
                    conn.commit()
            except Exception as exc:
                delivery['status'] = 'failed'
                delivery['error'] = str(exc)
            deliveries.append(delivery)
        payload['deliveries'] = deliveries
        sent_deliveries = [item for item in deliveries if str(item.get('status') or '') == 'sent']
        if sent_deliveries:
            payload['status'] = 'sent' if len(sent_deliveries) == len(deliveries) else 'partial_sent'
            first_sent = sent_deliveries[0]
            payload['response'] = first_sent.get('response')
            payload['message_text'] = first_sent.get('message_text')
        elif any(str(item.get('status') or '') == 'failed' for item in deliveries):
            payload['status'] = 'failed'
            failed_messages = [str(item.get('error') or '').strip() for item in deliveries if str(item.get('status') or '') == 'failed' and str(item.get('error') or '').strip()]
            if failed_messages:
                payload['error'] = '; '.join(failed_messages)
        else:
            payload['status'] = 'skipped_no_notifier'
        return payload

    @staticmethod
    def _whatsapp_binding_operation_key(account_key: str, binding_index: int) -> str:
        return f"{str(account_key or '').strip()}:{int(binding_index)}"

    @staticmethod
    def _whatsapp_binding_operation_label(operation: str) -> str:
        normalized = str(operation or '').strip()
        return {
            'manual_approve': '人工审批',
            'full_sync': '完整同步',
            'truth_refresh': '刷新人数',
            'probe_refresh': '强制实时校验',
            'rebuild_identity': '重建群绑定',
        }.get(normalized, normalized or '处理中')

    def _get_whatsapp_binding_operation_state(self, account_key: str, binding_index: int) -> Optional[Dict[str, Any]]:
        operation_key = self._whatsapp_binding_operation_key(account_key, binding_index)
        with self._whatsapp_binding_operation_lock:
            current = self._whatsapp_binding_operations.get(operation_key)
            return dict(current) if isinstance(current, dict) else None

    def _mark_whatsapp_binding_operation_started(
        self,
        account_key: str,
        binding_index: int,
        *,
        operation: str,
        detail: str = '',
        stage_code: str = '',
        stage_label: str = '',
        request_id: Optional[str] = None,
        allow_existing_request_id: bool = False,
    ) -> Dict[str, Any]:
        normalized_account_key = str(account_key or '').strip()
        operation_key = self._whatsapp_binding_operation_key(normalized_account_key, binding_index)
        normalized_request_id = str(request_id or '').strip()
        now_iso = utc_now()
        with self._whatsapp_binding_operation_lock:
            existing = self._whatsapp_binding_operations.get(operation_key)
            if isinstance(existing, dict):
                existing_operation = str(existing.get('operation') or '').strip()
                existing_request_id = str(existing.get('request_id') or '').strip()
                if (
                    allow_existing_request_id
                    and existing_operation == str(operation or '').strip()
                    and normalized_request_id
                    and existing_request_id == normalized_request_id
                ):
                    return dict(existing)
                if operation == 'manual_approve' and existing_operation == 'manual_approve':
                    raise HTTPException(status_code=409, detail='manual_approval_in_progress')
                raise HTTPException(
                    status_code=409,
                    detail={
                        'reason': 'binding_operation_in_progress',
                        'account_key': normalized_account_key,
                        'binding_index': int(binding_index),
                        'active_operation': existing_operation or None,
                        'active_operation_label': str(existing.get('operation_label') or '').strip() or self._whatsapp_binding_operation_label(existing_operation),
                        'active_detail': str(existing.get('detail') or '').strip() or None,
                        'active_stage_code': str(existing.get('stage_code') or '').strip() or None,
                        'active_stage_label': str(existing.get('stage_label') or '').strip() or None,
                        'request_id': str(existing.get('request_id') or '').strip() or None,
                        'started_at': existing.get('started_at'),
                    },
                )
            operation_label = self._whatsapp_binding_operation_label(operation)
            payload = {
                'active': True,
                'account_key': normalized_account_key,
                'binding_index': int(binding_index),
                'operation': str(operation or '').strip(),
                'operation_label': operation_label,
                'detail': str(detail or '').strip(),
                'stage_code': str(stage_code or '').strip(),
                'stage_label': str(stage_label or '').strip(),
                'request_id': normalized_request_id or create_id('approval_op'),
                'started_at': now_iso,
                'updated_at': now_iso,
            }
            self._whatsapp_binding_operations[operation_key] = payload
            return dict(payload)

    def _update_whatsapp_binding_operation_state(
        self,
        account_key: str,
        binding_index: int,
        **updates: Any,
    ) -> Optional[Dict[str, Any]]:
        operation_key = self._whatsapp_binding_operation_key(account_key, binding_index)
        now_iso = utc_now()
        with self._whatsapp_binding_operation_lock:
            current = self._whatsapp_binding_operations.get(operation_key)
            if not isinstance(current, dict):
                return None
            merged = dict(current)
            for key, value in updates.items():
                if value is None:
                    continue
                merged[key] = value
            merged['updated_at'] = now_iso
            self._whatsapp_binding_operations[operation_key] = merged
            return dict(merged)

    def _clear_whatsapp_binding_operation(self, account_key: str, binding_index: int) -> None:
        operation_key = self._whatsapp_binding_operation_key(account_key, binding_index)
        with self._whatsapp_binding_operation_lock:
            self._whatsapp_binding_operations.pop(operation_key, None)

    def _acquire_whatsapp_runtime_actor(
        self,
        *,
        account_key: str,
        operation: str,
        binding_index: int,
        wait_timeout_seconds: float = 90.0,
    ) -> Dict[str, Any]:
        normalized_account_key = str(account_key or '').strip()
        if not normalized_account_key:
            raise HTTPException(status_code=400, detail='account_key_required')
        deadline = time.monotonic() + max(1.0, float(wait_timeout_seconds or 90.0))
        thread_id = threading.get_ident()
        with self._whatsapp_runtime_actor_condition:
            while True:
                current = self._whatsapp_runtime_actor_states.get(normalized_account_key)
                if not isinstance(current, dict):
                    handle = {
                        'account_key': normalized_account_key,
                        'owner_thread': thread_id,
                        'depth': 1,
                        'operation': str(operation or '').strip(),
                        'binding_index': int(binding_index),
                        'acquired_at': utc_now(),
                    }
                    self._whatsapp_runtime_actor_states[normalized_account_key] = handle
                    return dict(handle)
                if int(current.get('owner_thread') or 0) == thread_id:
                    current['depth'] = int(current.get('depth') or 0) + 1
                    current['operation'] = str(operation or current.get('operation') or '').strip()
                    current['binding_index'] = int(binding_index)
                    return dict(current)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            'reason': 'runtime_actor_busy',
                            'account_key': normalized_account_key,
                            'active_operation': str(current.get('operation') or '').strip() or None,
                            'active_binding_index': current.get('binding_index'),
                        },
                    )
                self._whatsapp_runtime_actor_condition.wait(timeout=min(remaining, 0.5))

    def _release_whatsapp_runtime_actor(self, handle: Optional[Dict[str, Any]]) -> None:
        normalized_account_key = str((handle or {}).get('account_key') or '').strip()
        if not normalized_account_key:
            return
        thread_id = threading.get_ident()
        with self._whatsapp_runtime_actor_condition:
            current = self._whatsapp_runtime_actor_states.get(normalized_account_key)
            if not isinstance(current, dict):
                self._whatsapp_runtime_actor_condition.notify_all()
                return
            if int(current.get('owner_thread') or 0) != thread_id:
                self._whatsapp_runtime_actor_condition.notify_all()
                return
            depth = max(int(current.get('depth') or 1) - 1, 0)
            if depth <= 0:
                self._whatsapp_runtime_actor_states.pop(normalized_account_key, None)
            else:
                current['depth'] = depth
                self._whatsapp_runtime_actor_states[normalized_account_key] = current
            self._whatsapp_runtime_actor_condition.notify_all()

    def _approval_queue_recent_probe_fingerprint_stats(self, *, account_key: str, binding: Dict[str, Any], fingerprint: str) -> Dict[str, Any]:
        normalized_fingerprint = str(fingerprint or '').strip()
        if not normalized_fingerprint:
            return {'observed_count': 0, 'stable_count': 0, 'stable': False}
        object_key = self._approval_binding_truth_object_key(account_key, binding)
        if not object_key:
            return {'observed_count': 0, 'stable_count': 0, 'stable': False}
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM mcn_event_ledger
                    WHERE event_type='approval_queue_probe_observed'
                      AND object_type='registration_group_binding'
                      AND object_key=?
                    ORDER BY created_at DESC
                    LIMIT 3
                    """,
                    (object_key,),
                ).fetchall()
        except Exception:
            return {'observed_count': 0, 'stable_count': 0, 'stable': False}
        fingerprints: List[str] = []
        for row in rows:
            try:
                payload = json.loads(row['payload_json'] or '{}')
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                value = str(payload.get('fingerprint') or '').strip()
                if value:
                    fingerprints.append(value)
        stable_count = sum(1 for value in fingerprints if value == normalized_fingerprint)
        return {
            'observed_count': len(fingerprints),
            'stable_count': stable_count,
            'stable': stable_count >= 2,
        }

    @staticmethod
    def _approval_truth_has_explicit_empty_ui_evidence(result: Dict[str, Any]) -> bool:
        payload = dict(result or {})
        ui_pending_count = payload.get('ui_pending_count')
        try:
            ui_pending_count = int(ui_pending_count) if ui_pending_count is not None else None
        except Exception:
            ui_pending_count = None
        zero_pending_verified_by = str(payload.get('zero_pending_verified_by') or '').strip()
        pending_zero_confidence = str(payload.get('pending_zero_confidence') or '').strip().lower()
        explicit_empty_signal = bool(
            payload.get('empty_queue_visible')
            or payload.get('strong_empty_evidence')
            or zero_pending_verified_by
            or pending_zero_confidence in {'verified', 'stable', 'confirmed'}
        )
        if explicit_empty_signal:
            return True
        return ui_pending_count == 0 and bool(payload.get('review_surface_ready'))

    @staticmethod
    def _approval_truth_api_positive_override_eligibility(result: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(result or {})
        trust_status = str(payload.get('trust_status') or '').strip()
        reason_code = str(payload.get('reason_code') or '').strip()
        api_pending_count = payload.get('api_pending_count')
        try:
            api_pending_count = int(api_pending_count) if api_pending_count is not None else None
        except Exception:
            api_pending_count = None
        requester_ids = [str(item).strip() for item in (payload.get('requester_ids') or []) if str(item).strip()]
        issues: List[str] = []
        capability_evidence_reused = bool(
            payload.get('fingerprint_stable')
            or int(payload.get('fingerprint_stable_count') or 0) > 0
            or reason_code == 'api_pending_ui_not_converged'
        )
        if Service._approval_truth_has_explicit_empty_ui_evidence(payload):
            issues.append('ui_empty_evidence_present')
        if reason_code not in {'api_pending_ui_not_converged', 'untrusted_ui_not_converged'} and trust_status != 'UNTRUSTED_UI_NOT_CONVERGED':
            issues.append('reason_code_not_supported')
        if api_pending_count is None or api_pending_count <= 0:
            issues.append('api_pending_count_missing')
        if api_pending_count is not None and len(requester_ids) != api_pending_count:
            issues.append('requester_ids_incomplete')
        if not bool(payload.get('group_identity_verified')):
            issues.append('group_identity_unverified')
        if payload.get('runtime_identity_match') is not True:
            issues.append('runtime_identity_mismatch')
        if not bool(payload.get('session_authenticated')):
            issues.append('session_not_authenticated')
        if not capability_evidence_reused and payload.get('self_participant_found') is not True:
            issues.append('self_participant_missing')
        if not capability_evidence_reused and payload.get('self_is_admin') is not True:
            issues.append('self_is_not_admin')
        if not capability_evidence_reused and payload.get('can_manage_membership_requests') is not True:
            issues.append('manage_membership_requests_unconfirmed')
        if str(payload.get('fingerprint_quality') or '').strip() != 'strong':
            issues.append('fingerprint_not_strong')
        return {
            'eligible': not issues,
            'mode': 'requester_ids_direct',
            'issues': issues,
        }

    def _normalize_approval_truth_result(self, *, account_key: str, binding: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(result or {})
        trust_status = str(normalized.get('trust_status') or '').strip()
        reason_code = str(normalized.get('reason_code') or '').strip()
        api_pending_count = normalized.get('api_pending_count')
        ui_pending_count = normalized.get('ui_pending_count')
        try:
            api_pending_count = int(api_pending_count) if api_pending_count is not None else None
        except Exception:
            api_pending_count = None
        try:
            ui_pending_count = int(ui_pending_count) if ui_pending_count is not None else None
        except Exception:
            ui_pending_count = None
        fingerprint_stats = self._approval_queue_recent_probe_fingerprint_stats(
            account_key=account_key,
            binding=binding,
            fingerprint=str(normalized.get('fingerprint') or '').strip(),
        )
        normalized['fingerprint_stable'] = bool(fingerprint_stats.get('stable'))
        normalized['fingerprint_stable_count'] = int(fingerprint_stats.get('stable_count') or 0)
        normalized['fingerprint_observed_count'] = int(fingerprint_stats.get('observed_count') or 0)
        if api_pending_count not in (None, 0) and ui_pending_count in (None, 0):
            if reason_code in {
                'executor_group_state_fallback_pending_only',
                'api_pending_ui_not_converged',
                'untrusted_ui_not_converged',
                'ui_api_not_converged',
                'ui_count_greater_than_api_count',
                'ui_empty_api_has_historical_requests',
            } or trust_status in {'TRUTH_UNKNOWN', 'UNTRUSTED_API_STALE', 'UNTRUSTED_UI_NOT_CONVERGED'}:
                normalized['ui_pending_count'] = None
                ui_pending_count = None
            if reason_code == 'executor_group_state_fallback_pending_only' and trust_status == 'TRUTH_UNKNOWN':
                normalized['reason_code'] = 'api_pending_ui_not_converged'
            elif trust_status == 'UNTRUSTED_API_STALE' or reason_code in {
                'ui_api_not_converged',
                'ui_count_greater_than_api_count',
                'ui_empty_api_has_historical_requests',
            }:
                normalized['trust_status'] = 'UNTRUSTED_UI_NOT_CONVERGED'
                normalized['reason_code'] = 'untrusted_ui_not_converged'
        override = self._approval_truth_api_positive_override_eligibility(normalized)
        if (
            not bool(override.get('eligible'))
            and not self._approval_truth_has_explicit_empty_ui_evidence(normalized)
            and str(normalized.get('reason_code') or '').strip() == 'api_pending_ui_not_converged'
            and bool(normalized.get('group_identity_verified'))
            and str(normalized.get('fingerprint_quality') or '').strip() == 'strong'
        ):
            api_count = normalized.get('api_pending_count')
            try:
                api_count = int(api_count) if api_count is not None else None
            except Exception:
                api_count = None
            requester_ids = [str(item).strip() for item in (normalized.get('requester_ids') or []) if str(item).strip()]
            if api_count is not None and api_count > 0 and len(requester_ids) == api_count:
                override = {'eligible': True, 'mode': 'requester_ids_direct', 'issues': ['capability_reused_from_recent_strong_probe']}
        normalized['manual_override_eligible'] = bool(override.get('eligible'))
        normalized['manual_override_mode'] = override.get('mode')
        normalized['manual_override_issues'] = list(override.get('issues') or [])
        normalized = self._promote_authoritative_requester_ids_truth(normalized)
        return normalized

    @staticmethod
    def _promote_authoritative_requester_ids_truth(result: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(result or {})
        source_payload = dict(normalized.get('source') if isinstance(normalized.get('source'), dict) else {})
        source_mode = str(source_payload.get('mode') or '').strip()
        if source_mode == 'executor_group_state_fallback':
            normalized['authoritative_requester_ids_promoted'] = False
            return normalized
        pending_count = normalize_int_or_none(
            normalized.get('api_pending_count')
            if normalized.get('api_pending_count') is not None
            else normalized.get('pending_count')
        )
        requester_ids: List[str] = []
        raw_requester_ids = normalized.get('requester_ids') or normalized.get('requesterIds') or []
        if not isinstance(raw_requester_ids, list):
            raw_requester_ids = []
        for raw_requester_id in raw_requester_ids:
            requester_id = str(raw_requester_id or '').strip()
            if requester_id and requester_id not in requester_ids:
                requester_ids.append(requester_id)
        requesters = normalized.get('requesters') or []
        if not isinstance(requesters, list):
            requesters = []
        for requester in requesters:
            if not isinstance(requester, dict):
                continue
            requester_id = str(
                requester.get('requesterId')
                or requester.get('requester_id')
                or requester.get('jid')
                or requester.get('id')
                or ''
            ).strip()
            if requester_id and requester_id not in requester_ids:
                requester_ids.append(requester_id)
        identity_and_permission_verified = bool(
            normalized.get('group_identity_verified')
            and normalized.get('runtime_identity_match') is True
            and normalized.get('session_authenticated')
            and normalized.get('self_participant_found') is True
            and normalized.get('self_is_admin') is True
            and normalized.get('can_manage_membership_requests') is True
        )
        requester_list_closed = bool(pending_count is not None and pending_count >= 0 and len(requester_ids) == pending_count)
        strong_empty = bool(
            pending_count == 0
            and not requester_ids
            and (
                normalized.get('strong_empty_evidence')
                or normalized.get('empty_queue_visible')
                or normalized.get('zero_pending_verified_by')
            )
        )
        if identity_and_permission_verified and requester_list_closed and (pending_count > 0 or strong_empty):
            derived_pending_count = len(requester_ids)
            normalized.update({
                'trust_status': 'TRUSTED_CONFIRMED_PENDING' if derived_pending_count > 0 else 'TRUSTED_CONFIRMED_EMPTY',
                'pending_count': derived_pending_count,
                'trusted_pending_count': derived_pending_count,
                'api_pending_count': derived_pending_count,
                'ui_pending_count': derived_pending_count,
                'requester_ids': requester_ids,
                'display_trusted': True,
                'can_manual_approve': derived_pending_count > 0,
                'manual_approve_allowed': derived_pending_count > 0,
                'reason_code': 'authoritative_requester_list_complete',
                'authoritative_requester_ids_promoted': True,
            })
        else:
            normalized['authoritative_requester_ids_promoted'] = False
        return normalized

    @staticmethod
    def _approval_truth_acquisition_state_key(account_key: str, binding_index: int, trigger: str) -> str:
        return f"{str(account_key or '').strip()}:{int(binding_index)}:{str(trigger or '').strip() or 'manual_full_sync'}"

    def _begin_approval_truth_acquisition(self, *, account_key: str, binding_index: int, trigger: str) -> Dict[str, Any]:
        state_key = self._approval_truth_acquisition_state_key(account_key, binding_index, trigger)
        with self._approval_truth_acquisition_lock:
            current = self._approval_truth_acquisitions.get(state_key)
            if isinstance(current, dict):
                current['waiter_count'] = int(current.get('waiter_count') or 0) + 1
                return {
                    'state_key': state_key,
                    'acquisition_id': str(current.get('acquisition_id') or ''),
                    'owner': False,
                    'event': current['event'],
                }
            acquisition_id = create_id('truth_acq')
            state = {
                'acquisition_id': acquisition_id,
                'event': threading.Event(),
                'result': None,
                'error': None,
                'completed': False,
                'waiter_count': 0,
            }
            self._approval_truth_acquisitions[state_key] = state
            return {
                'state_key': state_key,
                'acquisition_id': acquisition_id,
                'owner': True,
                'event': state['event'],
            }

    def _finish_approval_truth_acquisition(self, acquisition: Dict[str, Any], *, result: Optional[Dict[str, Any]] = None, error: Optional[BaseException] = None) -> None:
        state_key = str((acquisition or {}).get('state_key') or '').strip()
        if not state_key:
            return
        event: Optional[threading.Event] = None
        should_remove = False
        with self._approval_truth_acquisition_lock:
            current = self._approval_truth_acquisitions.get(state_key)
            if not isinstance(current, dict):
                return
            current['result'] = dict(result) if isinstance(result, dict) else None
            current['error'] = error
            current['completed'] = True
            event = current.get('event')
            should_remove = int(current.get('waiter_count') or 0) <= 0
            if should_remove:
                self._approval_truth_acquisitions.pop(state_key, None)
        if isinstance(event, threading.Event):
            event.set()

    def _wait_for_approval_truth_acquisition(self, acquisition: Dict[str, Any], *, timeout_seconds: float = 90.0) -> Dict[str, Any]:
        state_key = str((acquisition or {}).get('state_key') or '').strip()
        event = (acquisition or {}).get('event')
        if not state_key or not isinstance(event, threading.Event):
            raise HTTPException(status_code=409, detail='truth_acquisition_wait_failed')
        try:
            wait_timeout = max(0.0, min(float(timeout_seconds), 90.0))
        except Exception:
            wait_timeout = 90.0
        event.wait(timeout=wait_timeout)
        result: Dict[str, Any] = {}
        error: Optional[BaseException] = None
        with self._approval_truth_acquisition_lock:
            current = self._approval_truth_acquisitions.get(state_key)
            if isinstance(current, dict):
                result = dict(current.get('result') or {}) if isinstance(current.get('result'), dict) else {}
                error = current.get('error')
                current['waiter_count'] = max(int(current.get('waiter_count') or 0) - 1, 0)
                if bool(current.get('completed')) and int(current.get('waiter_count') or 0) <= 0:
                    self._approval_truth_acquisitions.pop(state_key, None)
        if error is not None:
            if isinstance(error, HTTPException):
                raise error
            raise HTTPException(status_code=500, detail='truth_acquisition_failed')
        if not result:
            raise HTTPException(
                status_code=409,
                detail={
                    'reason': 'truth_acquisition_in_progress',
                    'acquisition_id': str((acquisition or {}).get('acquisition_id') or '').strip() or None,
                    'wait_timeout_seconds': wait_timeout,
                },
            )
        result['truth_acquisition_reused'] = True
        result['reused_existing_acquisition'] = True
        return result

    @staticmethod
    def _approval_truth_refresh_is_background_source(source: Any) -> bool:
        return str(source or '').strip() in {
            'production_ops_daemon_official_truth_refresh',
            'lightweight_probe_escalation',
            'scheduled_full_sync',
        }

    @staticmethod
    def _background_approval_truth_refresh_skip_payload(
        *,
        account_key: str,
        binding_index: int,
        source: str,
        reason: str,
        retry_after_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            'ok': True,
            'skipped': True,
            'background_refresh_skipped': True,
            'reason': str(reason or 'background_refresh_skipped').strip(),
            'source': str(source or '').strip() or 'background_refresh',
            'account_key': str(account_key or '').strip(),
            'binding_index': int(binding_index),
        }
        if retry_after_seconds is not None:
            try:
                payload['retry_after_seconds'] = round(max(float(retry_after_seconds), 0.0), 3)
            except Exception:
                pass
        return payload

    def _background_approval_truth_refresh_preflight(
        self,
        *,
        account_key: str,
        binding_index: int,
        source: str,
        cooldown_seconds: float = 60.0,
        fresh_seconds: float = APPROVAL_TRUTH_PENDING_TTL_SECONDS,
        reserve: bool = False,
    ) -> Dict[str, Any]:
        normalized_source = str(source or '').strip()
        if not self._approval_truth_refresh_is_background_source(normalized_source):
            return {}
        normalized_account = str(account_key or '').strip()
        if not normalized_account:
            return self._background_approval_truth_refresh_skip_payload(
                account_key=normalized_account,
                binding_index=binding_index,
                source=normalized_source,
                reason='background_refresh_missing_account',
            )
        now_monotonic = time.monotonic()
        throttle_key = f'{normalized_account}:binding:{int(binding_index)}'
        with self._background_approval_truth_refresh_lock:
            last_started = float(self._background_approval_truth_refresh_started_monotonic.get(throttle_key) or 0.0)
            elapsed = now_monotonic - last_started if last_started else None
            if elapsed is not None and elapsed < max(float(cooldown_seconds or 0), 1.0):
                return self._background_approval_truth_refresh_skip_payload(
                    account_key=normalized_account,
                    binding_index=binding_index,
                    source=normalized_source,
                    reason='background_binding_refresh_cooldown',
                    retry_after_seconds=max(float(cooldown_seconds or 0), 1.0) - elapsed,
                )
        with self._whatsapp_runtime_actor_condition:
            actor_state = self._whatsapp_runtime_actor_states.get(normalized_account)
            if isinstance(actor_state, dict) and actor_state:
                return self._background_approval_truth_refresh_skip_payload(
                    account_key=normalized_account,
                    binding_index=binding_index,
                    source=normalized_source,
                    reason='runtime_actor_busy_background_skip',
                    retry_after_seconds=5.0,
                )
        operation_state = self._get_whatsapp_binding_operation_state(normalized_account, int(binding_index))
        if isinstance(operation_state, dict) and bool(operation_state.get('active')):
            return self._background_approval_truth_refresh_skip_payload(
                account_key=normalized_account,
                binding_index=binding_index,
                source=normalized_source,
                reason='binding_operation_active_background_skip',
                retry_after_seconds=5.0,
            )
        try:
            account = self._get_whatsapp_approval_account_runtime_row_lightweight(normalized_account)
            bindings = list(account.get('group_binding_runtimes') or account.get('group_link_bindings') or [])
            binding = dict(bindings[int(binding_index)] or {}) if 0 <= int(binding_index) < len(bindings) else {}
            truth_view = dict(binding.get('approval_queue_truth') or {}) if isinstance(binding.get('approval_queue_truth'), dict) else {}
            current_truth = dict(truth_view.get('current_truth') or {}) if isinstance(truth_view.get('current_truth'), dict) else {}
            if self._approval_queue_current_truth_is_fresh(current_truth, max_age_seconds=fresh_seconds):
                return self._background_approval_truth_refresh_skip_payload(
                    account_key=normalized_account,
                    binding_index=binding_index,
                    source=normalized_source,
                    reason='current_truth_fresh_background_skip',
                    retry_after_seconds=max(float(fresh_seconds or 0), 1.0),
                )
        except Exception:
            pass
        if reserve:
            now_monotonic = time.monotonic()
            with self._background_approval_truth_refresh_lock:
                last_started = float(self._background_approval_truth_refresh_started_monotonic.get(throttle_key) or 0.0)
                elapsed = now_monotonic - last_started if last_started else None
                if elapsed is not None and elapsed < max(float(cooldown_seconds or 0), 1.0):
                    return self._background_approval_truth_refresh_skip_payload(
                        account_key=normalized_account,
                        binding_index=binding_index,
                        source=normalized_source,
                        reason='background_binding_refresh_cooldown',
                        retry_after_seconds=max(float(cooldown_seconds or 0), 1.0) - elapsed,
                    )
                self._background_approval_truth_refresh_started_monotonic[throttle_key] = now_monotonic
        return {}

    def _mark_background_approval_truth_refresh_started(self, *, account_key: str, binding_index: int = 0, source: str) -> None:
        if not self._approval_truth_refresh_is_background_source(source):
            return
        normalized_account = str(account_key or '').strip()
        if not normalized_account:
            return
        throttle_key = f'{normalized_account}:binding:{int(binding_index)}'
        with self._background_approval_truth_refresh_lock:
            self._background_approval_truth_refresh_started_monotonic[throttle_key] = time.monotonic()

    @staticmethod
    def _approval_truth_committed_pending_count(result: Dict[str, Any]) -> Optional[int]:
        if not bool((result or {}).get('current_truth_written')):
            return None
        approval_queue_truth = (result or {}).get('approval_queue_truth')
        if isinstance(approval_queue_truth, dict):
            pending_count = approval_queue_truth.get('pending_count')
        else:
            pending_count = (result or {}).get('pending_count')
        try:
            return int(pending_count) if pending_count is not None else None
        except Exception:
            return None

    @staticmethod
    def _approval_truth_failure_class(result: Dict[str, Any]) -> str:
        final_state = str((result or {}).get('final_state') or '').strip()
        if final_state in {'COMMIT_TRUTH_PENDING', 'COMMIT_TRUTH_EMPTY', 'COMMIT_PERMISSION_STATE'}:
            return 'NONE'
        if Service._approval_truth_committed_pending_count(result) is not None:
            return 'NONE'
        trust_status = str((result or {}).get('trust_status') or '').strip()
        reason_code = str((result or {}).get('reason_code') or '').strip()
        source_payload = dict((result or {}).get('source') or {}) if isinstance((result or {}).get('source'), dict) else {}
        source_mode = str(source_payload.get('mode') or '').strip()
        if trust_status.startswith('TRUSTED'):
            return 'NONE'
        if reason_code in {'identity_unresolved', 'binding_identity_not_resolved'} or trust_status == 'IDENTITY_UNRESOLVED':
            return 'IDENTITY_UNRESOLVED'
        if reason_code in {'registration_group_mismatch', 'group_id_missing'} or trust_status == 'IDENTITY_MISMATCH':
            return 'IDENTITY_MISMATCH'
        if reason_code in {'approval_capability_required', 'review_surface_required', 'not_group_member', 'not_group_admin'} or trust_status == 'PERMISSION_DENIED':
            return 'PERMISSION_DENIED'
        if reason_code in {'api_pending_ui_not_converged', 'untrusted_ui_not_converged'} or trust_status == 'UNTRUSTED_UI_NOT_CONVERGED':
            return 'UI_NOT_CONVERGED'
        if (result or {}).get('soft_reload_error'):
            return 'SOFT_RELOAD_FAILED'
        if source_mode == 'executor_group_state_fallback' and trust_status in {'TRUTH_UNKNOWN', 'EMPTY_UNVERIFIED'}:
            return 'INDEPENDENT_VERIFY_UNAVAILABLE'
        if reason_code in {'ui_api_not_converged', 'ui_count_greater_than_api_count', 'ui_empty_api_has_historical_requests'} or trust_status.startswith('UNTRUSTED_SYNC_INCONCLUSIVE'):
            return 'SYNC_INCONCLUSIVE'
        if reason_code in {'full_sync_hard_timeout'} or trust_status == 'SYNC_TIMEOUT':
            return 'BUDGET_EXHAUSTED'
        if trust_status == 'EMPTY_UNVERIFIED':
            return 'EMPTY_UNVERIFIED'
        if trust_status == 'RUNTIME_UNHEALTHY':
            return 'RUNTIME_UNHEALTHY'
        return 'INTERNAL_ERROR'

    @staticmethod
    def _approval_truth_final_state(result: Dict[str, Any]) -> str:
        trust_status = str((result or {}).get('trust_status') or '').strip()
        reason_code = str((result or {}).get('reason_code') or '').strip()
        if (
            trust_status == 'PERMISSION_DENIED'
            and reason_code in {'not_group_member', 'not_group_admin'}
            and bool((result or {}).get('current_truth_written'))
        ):
            return 'COMMIT_PERMISSION_STATE'
        if trust_status == 'TRUSTED_CONFIRMED_PENDING':
            return 'COMMIT_TRUTH_PENDING'
        if trust_status == 'TRUSTED_CONFIRMED_EMPTY':
            return 'COMMIT_TRUTH_EMPTY'
        committed_pending_count = Service._approval_truth_committed_pending_count(result)
        if committed_pending_count is not None:
            if committed_pending_count > 0:
                return 'COMMIT_TRUTH_PENDING'
            return 'COMMIT_TRUTH_EMPTY'
        return 'TRUTH_ACQUISITION_FAILED'

    def _approval_truth_recommended_action(self, result: Dict[str, Any]) -> str:
        final_state = self._approval_truth_final_state(result)
        if final_state in {'COMMIT_TRUTH_PENDING', 'COMMIT_TRUTH_EMPTY'}:
            return 'NONE'
        if final_state == 'COMMIT_PERMISSION_STATE':
            return 'RESTORE_APPROVAL_CAPABILITY'
        failure_class = self._approval_truth_failure_class(result)
        if failure_class in {'IDENTITY_UNRESOLVED', 'IDENTITY_MISMATCH'}:
            return 'REFRESH_BINDING_IDENTITY'
        if failure_class == 'PERMISSION_DENIED':
            return 'RESTORE_APPROVAL_CAPABILITY'
        if failure_class == 'UI_NOT_CONVERGED':
            return 'REPAIR_UI_ACTION_SURFACE'
        if failure_class in {'SOFT_RELOAD_FAILED', 'RUNTIME_UNHEALTHY'}:
            return 'RECOVER_RUNTIME'
        if failure_class == 'INDEPENDENT_VERIFY_UNAVAILABLE':
            return 'ESCALATE_INDEPENDENT_VERIFY'
        if failure_class == 'SYNC_INCONCLUSIVE':
            return 'RETRY_FULL_SYNC'
        if failure_class == 'BUDGET_EXHAUSTED':
            return 'RETRY_WITH_BACKGROUND_RECOVERY'
        if failure_class == 'EMPTY_UNVERIFIED':
            return 'WAIT_FOR_REVIEW_SURFACE'
        return 'MANUAL_REVIEW'

    @staticmethod
    def _approval_truth_recommended_action_text(action: str) -> str:
        normalized = str(action or '').strip()
        if normalized == 'REPAIR_UI_ACTION_SURFACE':
            return '审批面未收敛，请先修复审批面或执行完整同步后重试'
        if normalized == 'RESTORE_APPROVAL_CAPABILITY':
            return '审批账号缺少审批能力，请先恢复账号权限后重试'
        if normalized == 'RECOVER_RUNTIME':
            return '审批运行时未稳定，请先恢复运行时后重试'
        if normalized == 'REFRESH_BINDING_IDENTITY':
            return '群绑定身份未稳定，请先刷新探针或重建身份后重试'
        if normalized == 'ESCALATE_INDEPENDENT_VERIFY':
            return '当前真值仍不可靠，请先做独立核验后再审批'
        if normalized == 'RETRY_FULL_SYNC':
            return '同步结果暂未收敛，请先重新完整同步后重试'
        if normalized == 'RETRY_WITH_BACKGROUND_RECOVERY':
            return '实时同步预算不足，请等待后台恢复后重试'
        if normalized == 'WAIT_FOR_REVIEW_SURFACE':
            return '当前未看到明确审批面证据，请等待审批面刷新后重试'
        return '请先执行完整同步后重试'

    def _manual_approval_preflight_failure_detail(self, preflight: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(preflight or {})
        reason_code = str(payload.get('reason_code') or '').strip()
        trust_status = str(payload.get('trust_status') or '').strip()
        failure_class = str(payload.get('failure_class') or self._approval_truth_failure_class(payload) or '').strip()
        recommended_action = str(
            payload.get('recommended_action') or self._approval_truth_recommended_action(payload) or ''
        ).strip()
        action_text = self._approval_truth_recommended_action_text(recommended_action)
        if failure_class == 'UI_NOT_CONVERGED' or trust_status == 'UNTRUSTED_UI_NOT_CONVERGED':
            message = f'当前审批前同步已拦截：API 已看到待审批，但审批面未收敛。{action_text}'
        elif failure_class == 'PERMISSION_DENIED' or trust_status == 'PERMISSION_DENIED':
            if reason_code == 'not_group_member':
                message = '当前审批账号已不在该群，请让群管理员重新添加该账号并授予管理员权限后重试'
            elif reason_code == 'not_group_admin':
                message = '当前审批账号不是群管理员，请先授予管理员权限后重试'
            else:
                message = f'当前审批前同步已拦截：审批账号能力不足。{action_text}'
        elif failure_class in {'IDENTITY_UNRESOLVED', 'IDENTITY_MISMATCH'}:
            message = f'当前审批前同步已拦截：群绑定身份未稳定。{action_text}'
        elif failure_class in {'SOFT_RELOAD_FAILED', 'RUNTIME_UNHEALTHY'}:
            message = f'当前审批前同步已拦截：审批运行时未稳定。{action_text}'
        else:
            reason_hint = reason_code or trust_status or 'truth_not_trusted'
            message = f'当前审批前同步已拦截：{reason_hint}。{action_text}'
        return {
            'reason': 'manual_approval_full_sync_not_trusted',
            'message': message,
            'trust_status': trust_status or None,
            'reason_code': reason_code or None,
            'failure_class': failure_class or None,
            'recommended_action': recommended_action or None,
            'recommended_action_text': action_text,
            'manual_override_eligible': bool(payload.get('manual_override_eligible')),
            'manual_override_mode': payload.get('manual_override_mode'),
            'manual_override_issues': list(payload.get('manual_override_issues') or []),
            'stage_code': 'preflight_blocked',
            'stage_label': '前置拦截',
            'http_status': 409,
        }

    @staticmethod
    def _manual_approve_requesters_from_preflight(preflight: Dict[str, Any]) -> Tuple[List[str], List[Dict[str, Any]]]:
        requester_ids = (
            [str(item).strip() for item in (preflight.get('requester_ids') or []) if str(item).strip()]
            if isinstance(preflight.get('requester_ids'), list)
            else []
        )
        requesters = [dict(item) for item in (preflight.get('requesters') or []) if isinstance(item, dict)] if isinstance(preflight.get('requesters'), list) else []
        if not requester_ids:
            for requester in requesters:
                requester_id = str(requester.get('requesterId') or requester.get('requester_id') or requester.get('jid') or requester.get('id') or '').strip()
                if requester_id:
                    requester_ids.append(requester_id)
        requester_ids = list(dict.fromkeys(requester_ids))
        return requester_ids, requesters

    @staticmethod
    def _approval_truth_authoritative_source(result: Dict[str, Any]) -> str:
        source_payload = dict((result or {}).get('source') or {}) if isinstance((result or {}).get('source'), dict) else {}
        mode = str(source_payload.get('mode') or '').strip()
        if mode == 'executor_group_state_fallback':
            return 'executor_group_state_fallback'
        return 'worker_full_queue_sync'

    @staticmethod
    def _append_truth_acquisition_stage(stages: List[Dict[str, Any]], *, stage: str, status: str, **extra: Any) -> None:
        payload = {'stage': str(stage or '').strip(), 'status': str(status or '').strip()}
        for key, value in extra.items():
            if value is not None:
                payload[key] = value
        stages.append(payload)

    def _finalize_truth_acquisition_result(
        self,
        *,
        acquisition_id: str,
        trigger: str,
        result: Dict[str, Any],
        stages: List[Dict[str, Any]],
        latest_probe_write: Optional[Dict[str, Any]],
        current_truth_write: Optional[Dict[str, Any]],
        started_monotonic: float,
    ) -> Dict[str, Any]:
        finalized = dict(result or {})
        finalized['truth_acquisition_id'] = str(acquisition_id or '').strip() or create_id('truth_acq')
        finalized['trigger'] = str(trigger or '').strip() or 'manual_full_sync'
        finalized['latest_probe_written'] = bool((latest_probe_write or {}).get('written'))
        finalized['latest_probe_write_reason'] = (latest_probe_write or {}).get('reason')
        finalized['current_truth_written'] = bool((current_truth_write or {}).get('written'))
        finalized['current_truth_write_reason'] = (current_truth_write or {}).get('reason')
        finalized['commit_target'] = 'current_truth' if finalized['current_truth_written'] else 'latest_probe'
        finalized['final_state'] = self._approval_truth_final_state(finalized)
        if finalized['final_state'] in {'COMMIT_TRUTH_PENDING', 'COMMIT_TRUTH_EMPTY', 'COMMIT_PERMISSION_STATE'}:
            finalized['ok'] = True
        finalized['failure_class'] = self._approval_truth_failure_class(finalized)
        finalized['recommended_action'] = self._approval_truth_recommended_action(finalized)
        finalized['authoritative_source'] = self._approval_truth_authoritative_source(finalized)
        finalized['stages'] = list(stages or [])
        finalized['elapsed_ms'] = int(max((time.perf_counter() - float(started_monotonic or time.perf_counter())) * 1000.0, 0.0))
        finalized['foreground_budget_ms'] = int(max(float(finalized.get('foreground_budget_ms') or 0) or 0, 0)) or None
        finalized['background_budget_ms'] = finalized.get('background_budget_ms')
        finalized['truth_acquisition_reused'] = bool(finalized.get('truth_acquisition_reused'))
        finalized['reused_existing_acquisition'] = bool(finalized.get('reused_existing_acquisition'))
        return finalized

    def _whatsapp_approval_binding_operation_login_gate_detail(
        self,
        *,
        account: Dict[str, Any],
        binding: Dict[str, Any],
        binding_index: int,
        operation: str,
    ) -> Optional[Dict[str, Any]]:
        runtime_state = dict((account or {}).get('runtime_state') or {})
        session_state = dict((account or {}).get('session_state') or {})
        provider_name = str(
            (binding or {}).get('provider_name')
            or runtime_state.get('provider_name')
            or (account or {}).get('provider_name')
            or ''
        ).strip().lower()
        provider_mode = str(
            (binding or {}).get('provider_mode')
            or runtime_state.get('provider_mode')
            or (account or {}).get('provider_mode')
            or ''
        ).strip().lower()
        runtime_mode = str(runtime_state.get('mode') or '').strip()
        session_auth_strategy = str(session_state.get('auth_strategy') or '').strip().lower()
        has_baileys_runtime_context = bool(
            runtime_mode == 'baileys_provider_runtime'
            or session_auth_strategy == 'baileys'
            or (
                provider_name == 'baileys'
                and (
                    str(runtime_state.get('base_url') or '').strip()
                    or str(runtime_state.get('baileys_account_id') or runtime_state.get('provider_account_id') or runtime_state.get('account_id') or '').strip()
                )
            )
        )
        is_baileys_account = bool(has_baileys_runtime_context)
        if not is_baileys_account:
            return None
        login_state = map_whatsapp_login_state(
            runtime_state=runtime_state,
            session_state=session_state,
            account_enabled=bool((account or {}).get('enabled', True)),
        )
        if str(operation or '').strip() in {'probe_refresh', 'truth_refresh', 'full_sync'}:
            configured_base_url = str(
                runtime_state.get('base_url')
                or (binding or {}).get('baileys_base_url')
                or (binding or {}).get('provider_base_url')
                or (account or {}).get('baileys_base_url')
                or (account or {}).get('provider_base_url')
                or ''
            ).strip()
            configured_account_id = str(
                runtime_state.get('baileys_account_id')
                or runtime_state.get('provider_account_id')
                or runtime_state.get('account_id')
                or (binding or {}).get('baileys_account_id')
                or (binding or {}).get('provider_account_id')
                or (binding or {}).get('account_id')
                or (account or {}).get('baileys_account_id')
                or (account or {}).get('provider_account_id')
                or (account or {}).get('account_id')
                or ''
            ).strip()
            if (
                configured_base_url
                and configured_account_id
                and not self._baileys_session_has_explicit_login_block(session_state)
            ):
                return None
        if bool(login_state.get('can_probe')) or bool(session_state.get('login_verified')) or bool(session_state.get('can_probe')):
            return None
        operation_label = {
            'manual_approve': '审批',
            'truth_refresh': '刷新人数',
            'probe_refresh': '刷新状态',
            'full_sync': '完整同步',
        }.get(str(operation or '').strip(), '操作')
        return {
            'reason': 'whatsapp_account_not_logged_in',
            'message': f'账号未登录，无法执行{operation_label}。请先扫码登录后再操作。',
            'stage_code': 'login_preflight_blocked',
            'reason_code': str(login_state.get('login_state') or 'unknown'),
            'login_action': str(login_state.get('login_action') or 'refresh_status'),
            'login_state': str(login_state.get('login_state') or 'unknown'),
            'login_state_label': str(login_state.get('login_state_label') or '状态待确认'),
            'account_key': str((account or {}).get('account_key') or '').strip(),
            'binding_index': int(binding_index),
            'binding_id': str((binding or {}).get('binding_id') or '').strip() or None,
            'provider_name': provider_name or None,
            'runtime_state': runtime_state,
            'session_state': {
                'login_verified': session_state.get('login_verified'),
                'login_check_status': session_state.get('login_check_status'),
                'authenticated': session_state.get('authenticated'),
                'ready': session_state.get('ready'),
                'can_probe': login_state.get('can_probe'),
            },
        }

    @staticmethod
    def _official_group_requester_identity(requester: Dict[str, Any]) -> str:
        return str(
            (requester or {}).get('requesterId')
            or (requester or {}).get('requester_id')
            or (requester or {}).get('jid')
            or (requester or {}).get('id')
            or ''
        ).strip()

    def _manual_official_group_approval_plan(
        self,
        *,
        official_group: str,
        pending_count: int,
        requester_ids: List[str],
        requesters: List[Dict[str, Any]],
        decided_at: str,
        request_id: str,
    ) -> Dict[str, Any]:
        current_requester_ids = [str(item).strip() for item in (requester_ids or []) if str(item).strip()]
        current_requester_ids = list(dict.fromkeys(current_requester_ids))
        requester_by_id: Dict[str, Dict[str, Any]] = {}
        candidate_requesters: List[Dict[str, Any]] = []
        seen_candidate_ids: set[str] = set()
        for requester in list(requesters or []):
            if not isinstance(requester, dict):
                continue
            candidate = dict(requester)
            requester_id = self._official_group_requester_identity(candidate)
            if requester_id:
                requester_by_id[requester_id] = candidate
                seen_candidate_ids.add(requester_id)
            candidate_requesters.append(candidate)
        for requester_id in current_requester_ids:
            if requester_id in seen_candidate_ids:
                continue
            candidate = {'requesterId': requester_id}
            requester_by_id[requester_id] = candidate
            candidate_requesters.append(candidate)
            seen_candidate_ids.add(requester_id)

        release_count = min(
            len(candidate_requesters),
            max(0, int(pending_count or 0)) if pending_count else len(candidate_requesters),
        )
        matched_leads, unmatched_requesters = self._match_official_group_requesters_to_leads(
            lead_rows=self._official_group_customer_projection_candidate_rows(),
            requesters=candidate_requesters,
            release_count=release_count,
        )
        eligible_candidates: List[Dict[str, Any]] = []
        skipped_requesters: List[Dict[str, Any]] = []
        remaining_unmatched_requesters: List[Dict[str, Any]] = []
        for unmatched in unmatched_requesters:
            crm_row, _ = self._find_crm_customer_for_official_group_requester(unmatched)
            if crm_row:
                matched_leads.append({
                    'lead_id': None,
                    'mobile': str(crm_row.get('mobile') or '').strip(),
                    'area_code': 0,
                    'country': '',
                    'yw_id': str(crm_row.get('ywId') or '').strip(),
                    'app_name': str(crm_row.get('appName') or '').strip(),
                    'dept_name': str(crm_row.get('deptName') or '').strip(),
                    'pendaftaran_group': str(crm_row.get('pendaftaranGroup') or '').strip(),
                    'matched_customer_id': str(crm_row.get('id') or '').strip(),
                    'current_status': 'crm_phone_matched',
                    'matched_requester_id': str(unmatched.get('requester_id') or unmatched.get('requesterId') or '').strip() or None,
                    'matched_requester_phone_hint': str(unmatched.get('phone_normalized') or unmatched.get('phone_raw') or unmatched.get('debugLidPhoneRaw') or '').strip() or None,
                    'matched_requester_name_hint': str(unmatched.get('display_name') or unmatched.get('displayName') or '').strip() or None,
                })
                continue
            remaining_unmatched_requesters.append(unmatched)
        for unmatched in remaining_unmatched_requesters:
            skipped_requesters.append({
                **dict(unmatched),
                'reason_code': 'official_group_requester_phone_unmatched',
                'reason_detail': '当前申请人手机号未匹配到本地 CRM 投影或 live CRM 记录。',
                'next_action': 'manual_review_official_group_approval',
            })

        for lead in matched_leads:
            lead_dict = dict(lead)
            requester_id = str(lead_dict.get('matched_requester_id') or '').strip()
            requester = dict(requester_by_id.get(requester_id) or {'requesterId': requester_id})
            if not requester_id:
                skipped_requesters.append({
                    'lead_id': lead_dict.get('lead_id'),
                    'requester_id': None,
                    'display_name': lead_dict.get('matched_requester_name_hint'),
                    'reason_code': 'official_group_requester_id_missing',
                    'reason_detail': '当前申请人缺少可审批的 WhatsApp requesterId。',
                    'next_action': 'refresh_official_group_requesters',
                })
                continue
            if lead_dict.get('matched_requester_name_hint') and not str(requester.get('displayName') or requester.get('display_name') or '').strip():
                requester['displayName'] = lead_dict.get('matched_requester_name_hint')
            if lead_dict.get('matched_requester_phone_hint') and not str(requester.get('phoneRaw') or requester.get('phone_raw') or '').strip():
                requester['phoneRaw'] = lead_dict.get('matched_requester_phone_hint')
            try:
                check_result = self._official_group_phone_approval_check(
                    target_group=official_group,
                    target_phone_hint=str(lead_dict.get('matched_requester_phone_hint') or '').strip() or None,
                    checked_at=decided_at,
                    checked_by='ops:manual_official_approval',
                    checked_by_name='群审批控制台',
                )
            except HTTPException as exc:
                skipped_requesters.append({
                    'lead_id': lead_dict.get('lead_id'),
                    'requester_id': requester_id,
                    'display_name': requester.get('displayName') or requester.get('display_name'),
                    'reason_code': 'official_group_eligibility_check_failed',
                    'reason_detail': exc.detail,
                    'next_action': 'manual_review_official_group_approval',
                })
                continue
            if bool(check_result.get('eligible')):
                eligible_candidates.append({
                    'lead': lead_dict,
                    'requester': requester,
                    'requester_id': requester_id,
                    'check_result': check_result,
                })
                continue
            skipped_requesters.append({
                'lead_id': lead_dict.get('lead_id'),
                'requester_id': requester_id,
                'display_name': requester.get('displayName') or requester.get('display_name'),
                'phone_raw': requester.get('phoneRaw') or requester.get('phone_raw'),
                'reason_code': check_result.get('reason_code') or 'official_group_requester_not_eligible',
                'reason_detail': check_result.get('reason_detail'),
                'next_action': check_result.get('next_action') or 'manual_review_official_group_approval',
                'eligibility': check_result,
            })

        eligible_requester_ids = [str(item.get('requester_id') or '').strip() for item in eligible_candidates if str(item.get('requester_id') or '').strip()]
        eligible_requesters = [dict(item.get('requester') or {}) for item in eligible_candidates]
        return {
            'pending_count': pending_count,
            'current_requester_ids': current_requester_ids,
            'current_requesters': candidate_requesters,
            'eligible_count': len(eligible_requester_ids),
            'eligible_requester_ids': eligible_requester_ids,
            'eligible_requesters': eligible_requesters,
            'eligible_candidates': eligible_candidates,
            'skipped_count': len(skipped_requesters),
            'skipped_requesters': skipped_requesters,
        }

    @staticmethod
    def _successful_official_group_requester_ids_from_result(
        *,
        result: Dict[str, Any],
        fallback_requester_ids: List[str],
    ) -> List[str]:
        fallback_ids = [str(item).strip() for item in (fallback_requester_ids or []) if str(item).strip()]
        fallback_ids = list(dict.fromkeys(fallback_ids))
        raw_result = dict((result or {}).get('raw_result') or {}) if isinstance((result or {}).get('raw_result'), dict) else {}
        approval_results = [dict(item) for item in (raw_result.get('approval_results') or []) if isinstance(item, dict)]
        successful_ids: List[str] = []
        for item in approval_results:
            requester_id = str(item.get('requesterId') or item.get('requester_id') or item.get('jid') or '').strip()
            if not requester_id:
                continue
            status_text = str(item.get('status') or item.get('status_code') or item.get('code') or '').strip().lower()
            error_text = str(item.get('error') or item.get('error_message') or '').strip()
            if not error_text and status_text in {'', '200', 'ok', 'success', 'approved'}:
                successful_ids.append(requester_id)
        successful_ids = [item for item in fallback_ids if item in set(successful_ids)] if successful_ids else []
        if approval_results:
            return successful_ids
        reported_count = normalize_int_or_none((result or {}).get('approved_count'))
        if reported_count is None:
            reported_count = normalize_int_or_none(raw_result.get('approved_count'))
        if reported_count is None:
            return fallback_ids
        return fallback_ids[:max(0, min(int(reported_count or 0), len(fallback_ids)))]

    def _finalize_manual_official_group_lead_results(
        self,
        *,
        approval_run_id: str,
        official_group: str,
        group_name: str,
        approved_at: str,
        result: Dict[str, Any],
        eligible_candidates: List[Dict[str, Any]],
        approved_requester_ids: List[str],
    ) -> List[Dict[str, Any]]:
        approved_set = {str(item).strip() for item in (approved_requester_ids or []) if str(item).strip()}
        lead_results: List[Dict[str, Any]] = []
        for candidate in list(eligible_candidates or []):
            requester_id = str((candidate or {}).get('requester_id') or '').strip()
            if requester_id not in approved_set:
                continue
            lead = dict((candidate or {}).get('lead') or {})
            lead_id = str(lead.get('lead_id') or '').strip()
            if not lead_id:
                continue
            with self.db.connect() as conn:
                latest_task = self._latest_group_join_task(conn, lead_id=lead_id)
            if not latest_task:
                lead_results.append({
                    'lead_id': lead_id,
                    'requester_id': requester_id,
                    'status': 'skipped',
                    'reason_code': 'group_join_task_missing',
                })
                continue
            try:
                join_result = self.group_join_result(
                    str(latest_task.get('task_id') or '').strip(),
                    GroupJoinResultRequest(
                        status='success',
                        result_code=str((result or {}).get('result_code') or 'approved'),
                        result_reason=str((result or {}).get('result_reason') or 'official group manual approval succeeded'),
                        finished_at=approved_at,
                        raw_result={
                            'target_group': official_group,
                            'target_group_name': group_name,
                            'approval_run_id': approval_run_id,
                            'requester_id': requester_id,
                            'target_member': dict((candidate or {}).get('requester') or {}),
                            'source': 'official_group_manual_batch',
                        },
                    ),
                )
                lead_results.append({
                    'lead_id': lead_id,
                    'requester_id': requester_id,
                    'status': 'success',
                    'task_id': latest_task.get('task_id'),
                    'result': join_result,
                })
            except Exception as exc:
                lead_results.append({
                    'lead_id': lead_id,
                    'requester_id': requester_id,
                    'status': 'failed',
                    'task_id': latest_task.get('task_id'),
                    'reason_code': 'group_join_result_update_failed',
                    'reason_detail': str(exc),
                })
        return lead_results

    def manual_approve_whatsapp_approval_binding(self, account_key: str, binding_index: int, *, audit_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        inflight_key = f"{str(account_key or '').strip()}:{int(binding_index)}"
        runtime_actor: Optional[Dict[str, Any]] = None
        operation_started = False
        request_context = dict((audit_context or {}).get('request') or {})
        request_id = str(request_context.get('request_id') or '').strip() or create_id('approval_op')
        try:
            with self._manual_whatsapp_approval_inflight_lock:
                if inflight_key in self._manual_whatsapp_approval_inflight:
                    raise HTTPException(status_code=409, detail='manual_approval_in_progress')
                self._manual_whatsapp_approval_inflight.add(inflight_key)
            runtime_actor = self._acquire_whatsapp_runtime_actor(
                account_key=account_key,
                operation='manual_approve',
                binding_index=binding_index,
                wait_timeout_seconds=120.0,
            )
            self._mark_whatsapp_binding_operation_started(
                account_key,
                binding_index,
                operation='manual_approve',
                detail='正在执行人工审批',
                stage_code='preflight_sync',
                stage_label='审批前同步',
                request_id=request_id,
                allow_existing_request_id=True,
            )
            operation_started = True
            return self._manual_approve_whatsapp_approval_binding_locked(account_key, binding_index, audit_context=audit_context)
        finally:
            with self._manual_whatsapp_approval_inflight_lock:
                self._manual_whatsapp_approval_inflight.discard(inflight_key)
            if operation_started:
                self._clear_whatsapp_binding_operation(account_key, binding_index)
            self._release_whatsapp_runtime_actor(runtime_actor)

    def _manual_approve_official_group_binding_locked(
        self,
        account_key: str,
        binding_index: int,
        *,
        account: Dict[str, Any],
        binding: Dict[str, Any],
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        audit_context = dict(audit_context or {})
        operator = dict(audit_context.get('operator') or {})
        request_context = dict(audit_context.get('request') or {})
        request_id = str(request_context.get('request_id') or '').strip() or create_id('approval_op')
        login_gate_detail = self._whatsapp_approval_binding_operation_login_gate_detail(
            account=account,
            binding=binding,
            binding_index=binding_index,
            operation='manual_approve',
        )
        if login_gate_detail:
            self._update_whatsapp_binding_operation_state(
                account_key,
                binding_index,
                detail=str(login_gate_detail.get('message') or '账号未登录，无法执行审批'),
                stage_code='login_preflight_blocked',
                stage_label='登录校验',
                request_id=request_id,
            )
            raise HTTPException(status_code=409, detail=login_gate_detail)
        normalized_account_key = str(account.get('account_key') or account_key or '').strip()
        preflight = self._manual_approve_preflight_from_current_truth(
            account_key=normalized_account_key,
            binding=binding,
        )
        if preflight:
            self._update_whatsapp_binding_operation_state(
                account_key,
                binding_index,
                detail='已复用当前人数单，准备提交审批',
                stage_code='preflight_current_truth_reuse',
                stage_label='复用当前真值',
                request_id=request_id,
            )
        else:
            self._update_whatsapp_binding_operation_state(
                account_key,
                binding_index,
                detail='正在执行官方群审批前同步',
                stage_code='preflight_sync',
                stage_label='审批前同步',
                request_id=request_id,
            )
            try:
                preflight = self.full_sync_whatsapp_approval_binding(
                    normalized_account_key,
                    binding_index,
                    source='official_manual_approve_preflight',
                    timeout_seconds=30.0,
                    _skip_operation_lock=True,
                    request_id=request_id,
                )
            except Exception as exc:
                preflight = self._manual_approve_preflight_from_current_truth(
                    account_key=normalized_account_key,
                    binding=binding,
                    preflight_error=exc,
                )
                if not preflight:
                    raise
                self._update_whatsapp_binding_operation_state(
                    account_key,
                    binding_index,
                    detail='审批前同步抖动，已复用当前真值继续审批',
                    stage_code='preflight_current_truth_reuse',
                    stage_label='复用当前真值',
                    request_id=request_id,
                )
        if not bool(preflight.get('can_manual_approve')):
            failure_detail = self._manual_approval_preflight_failure_detail(preflight)
            self._update_whatsapp_binding_operation_state(
                account_key,
                binding_index,
                detail=str(failure_detail.get('message') or '审批前同步未通过'),
                stage_code=str(failure_detail.get('stage_code') or 'preflight_blocked'),
                stage_label=str(failure_detail.get('stage_label') or '前置拦截'),
                request_id=request_id,
            )
            raise HTTPException(status_code=409, detail=failure_detail)
        pending_count = max(int(preflight.get('trusted_pending_count') or preflight.get('ui_pending_count') or preflight.get('pending_count') or 0), 0)
        if pending_count <= 0:
            raise HTTPException(status_code=400, detail='current official group binding has no pending requests to approve')
        requester_ids, requesters = self._manual_approve_requesters_from_preflight(preflight)
        if len(requester_ids) < pending_count:
            detail = {
                'reason': 'official_manual_approve_requester_list_incomplete',
                'message': '当前审批列表缺少完整成员ID，无法保证一键审批通过全部待审批人员。请先刷新人数，确认列表可读后再审批。',
                'pending_count': pending_count,
                'requester_id_count': len(requester_ids),
                'stage_code': 'preflight_requester_ids_missing',
            }
            self._update_whatsapp_binding_operation_state(
                account_key,
                binding_index,
                detail=detail['message'],
                stage_code='preflight_requester_ids_missing',
                stage_label='审批名单校验',
                request_id=request_id,
            )
            raise HTTPException(status_code=409, detail=detail)
        runtime_state = dict(account.get('runtime_state') or {})
        executor = self._build_runtime_baileys_registration_group_executor(
            account=account,
            binding=binding,
            runtime_state=runtime_state,
        )
        if not getattr(executor, 'base_url', ''):
            raise HTTPException(
                status_code=400,
                detail={
                    'reason': 'official_group_baileys_runtime_missing',
                    'message': '官方群一键审批需要可用的 Baileys runtime。',
                },
            )
        decided_at = utc_now()
        official_group = str(
            preflight.get('group_id')
            or self._whatsapp_binding_runtime_group_id(binding)
            or binding.get('registration_group')
            or binding.get('group_id')
            or binding.get('link')
            or ''
        ).strip()
        if not official_group:
            raise HTTPException(status_code=400, detail='official group target is required')
        configured_official_group = str(binding.get('registration_group') or '').strip()
        group_name = str(preflight.get('group_name') or binding.get('group_name') or official_group).strip() or official_group
        approval_plan = self._manual_official_group_approval_plan(
            official_group=official_group,
            pending_count=pending_count,
            requester_ids=requester_ids,
            requesters=requesters,
            decided_at=decided_at,
            request_id=request_id,
        )
        eligible_requester_ids = list(approval_plan.get('eligible_requester_ids') or [])
        eligible_requesters = [dict(item) for item in (approval_plan.get('eligible_requesters') or []) if isinstance(item, dict)]
        eligible_count = len(eligible_requester_ids)
        if eligible_count <= 0:
            detail = {
                'reason': 'official_manual_approve_no_eligible_requesters',
                'message': '当前申请列表没有符合官方群资格规则的成员，未执行审批。',
                'pending_count': pending_count,
                'requester_id_count': len(requester_ids),
                'eligible_count': 0,
                'skipped_count': int(approval_plan.get('skipped_count') or 0),
                'skipped_requesters': approval_plan.get('skipped_requesters') or [],
                'stage_code': 'eligibility_blocked',
            }
            self._update_whatsapp_binding_operation_state(
                account_key,
                binding_index,
                detail=detail['message'],
                stage_code='eligibility_blocked',
                stage_label='资格校验',
                request_id=request_id,
            )
            raise HTTPException(status_code=409, detail=detail)
        baileys_account_id = str(
            binding.get('baileys_account_id')
            or binding.get('provider_account_id')
            or binding.get('account_id')
            or account.get('baileys_account_id')
            or account.get('provider_account_id')
            or runtime_state.get('baileys_account_id')
            or runtime_state.get('provider_account_id')
            or runtime_state.get('account_id')
            or ''
        ).strip()
        context = {
            'registration_group': official_group,
            'group_id': official_group,
            'groupId': official_group,
            'target_group': official_group,
            'decision': 'approve',
            'decided_at': decided_at,
            'decided_by': 'ops:manual_official_approval',
            'decided_by_name': '群审批控制台',
            'source_platform': 'ops_console',
            'approved_count': eligible_count,
            'expected_pending_count': pending_count,
            'expected_member_count': normalize_int_or_none(preflight.get('member_count')),
            'expected_requester_ids': eligible_requester_ids,
            'requester_ids': eligible_requester_ids,
            'requesterIds': eligible_requester_ids,
            'expected_requesters': eligible_requesters,
            'latest_group_state_before_approve': {
                **dict(preflight or {}),
                'requester_ids': requester_ids,
                'requesters': requesters,
                'eligible_requester_ids': eligible_requester_ids,
                'eligible_requesters': eligible_requesters,
                'skipped_requesters': approval_plan.get('skipped_requesters') or [],
            },
            'area': str(binding.get('area') or account.get('area') or 'Indonesia').strip() or 'Indonesia',
            'remark': 'official group manual approve eligible requesters',
            'force_immediate': True,
            'approval_run_id': request_id,
            'eligibility_plan': approval_plan,
            'approval_runtime_route': {
                'account_key': str(account.get('account_key') or account_key or '').strip(),
                'account_name': account.get('account_name'),
                'base_url': getattr(executor, 'base_url', None),
                'binding': binding,
                'responsible_type': 'official_group',
                'resolved_group_target': official_group,
            },
        }
        if baileys_account_id:
            context['accountId'] = baileys_account_id
            context['baileys_account_id'] = baileys_account_id
        if binding.get('link'):
            context['groupLink'] = binding.get('link')
            context['link'] = binding.get('link')
        self._update_whatsapp_binding_operation_state(
            account_key,
            binding_index,
            detail=f'正在提交官方群一键审批（符合资格 {eligible_count}/{pending_count} 人）',
            stage_code='approval_dispatch',
            stage_label='提交审批',
            request_id=request_id,
        )
        self.invalidate_approval_queue_truth_after_mutation(
            account_key=str(account.get('account_key') or account_key or '').strip(),
            binding=binding,
            invalidated_reason='approval_started',
            approved_count=eligible_count,
            approval_run_id=request_id,
            action_ts=decided_at,
        )
        runtime_action_id = self._record_wa_runtime_action(
            account_key=str(account.get('account_key') or account_key or '').strip(),
            binding=binding,
            action_type='official_manual_approve',
            status='started',
            request_payload={
                'request_id': request_id,
                'provider_mode': binding.get('provider_mode') or runtime_state.get('provider_mode') or account.get('provider_mode'),
                'responsible_type': 'official_group',
                'official_group': official_group,
                'expected_pending_count': pending_count,
                'eligible_count': eligible_count,
                'skipped_count': int(approval_plan.get('skipped_count') or 0),
                'requester_id_count': len(eligible_requester_ids),
            },
            result_payload={'preflight': preflight, 'eligibility_plan': approval_plan},
        )
        if hasattr(executor, 'official_group_approve'):
            result = executor.official_group_approve(context) or {}
        else:
            result = executor.approve(context) or {}
        if not isinstance(result, dict):
            raise HTTPException(status_code=500, detail='official group Baileys executor must return dict result')
        approval_run_id = str(result.get('approval_run_id') or (result.get('raw_result') or {}).get('approval_run_id') or request_id).strip() or request_id
        self._record_wa_runtime_action(
            account_key=str(account.get('account_key') or account_key or '').strip(),
            binding=binding,
            action_type='official_manual_approve_result',
            status='completed',
            request_payload={
                'request_id': request_id,
                'runtime_action_id': runtime_action_id,
                'approval_run_id': approval_run_id,
                'responsible_type': 'official_group',
            },
            result_payload=result,
        )
        raw_result = dict(result.get('raw_result') or {})
        approved_requester_ids = self._successful_official_group_requester_ids_from_result(
            result=result,
            fallback_requester_ids=eligible_requester_ids,
        )
        approved_requester_id_set = {str(item).strip() for item in approved_requester_ids if str(item).strip()}
        approved_count = len(approved_requester_ids)
        approved_requesters = [
            dict(item)
            for item in eligible_requesters
            if self._official_group_requester_identity(item) in approved_requester_id_set
        ]
        self.invalidate_approval_queue_truth_after_mutation(
            account_key=str(account.get('account_key') or account_key or '').strip(),
            binding=binding,
            invalidated_reason='approval_completed',
            approved_count=approved_count,
            pending_count=raw_result.get('pending_after'),
            approval_run_id=approval_run_id,
            action_ts=str(result.get('approved_at') or decided_at),
        )
        pending_after = raw_result.get('pending_after')
        raw_pending_after_available = pending_after is not None
        post_verify: Dict[str, Any] = {}
        deferred_post_verify: Dict[str, Any] = {}
        if raw_pending_after_available:
            self._update_whatsapp_binding_operation_state(
                account_key,
                binding_index,
                detail='审批结果已返回，正在写入人数单',
                stage_code='post_result_write',
                stage_label='写入结果',
                request_id=request_id,
            )
        else:
            self._update_whatsapp_binding_operation_state(
                account_key,
                binding_index,
                detail='正在核验官方群审批结果',
                stage_code='post_verify',
                stage_label='核验结果',
                request_id=request_id,
            )
            try:
                post_verify = self.full_sync_whatsapp_approval_binding(
                    normalized_account_key,
                    binding_index,
                    source='official_manual_approve_after_sync',
                    timeout_seconds=20.0,
                    _skip_operation_lock=True,
                    request_id=request_id,
                )
            except Exception:
                post_verify = {}
        if pending_after is None and isinstance(post_verify, dict):
            pending_after = post_verify.get('trusted_pending_count')
        if pending_after is None and isinstance(post_verify, dict):
            pending_after = post_verify.get('pending_count', post_verify.get('ui_pending_count', post_verify.get('api_pending_count')))
        post_approval_pending_count = normalize_int_or_none(pending_after)
        member_count_after = raw_result.get('member_count_after')
        if member_count_after is None and isinstance(post_verify, dict):
            member_count_after = post_verify.get('member_count')
        post_approval_member_count = normalize_int_or_none(member_count_after)
        post_requester_ids = [] if post_approval_pending_count == 0 else (
            list(post_verify.get('requester_ids') or []) if isinstance(post_verify, dict) and isinstance(post_verify.get('requester_ids'), list) else []
        )
        if post_approval_pending_count is not None:
            self._write_post_approval_queue_current_truth(
                account_key=str(account.get('account_key') or account_key or '').strip(),
                binding=binding,
                approved_count=approved_count,
                pending_count=post_approval_pending_count,
                approval_run_id=approval_run_id,
                action_ts=str(result.get('approved_at') or decided_at),
                requester_ids=post_requester_ids,
                member_count=post_approval_member_count,
            )
            if raw_pending_after_available:
                deferred_post_verify = self._enqueue_official_group_post_approval_verify_task(
                    account_key=normalized_account_key,
                    binding_index=binding_index,
                    request_id=request_id,
                    approval_run_id=approval_run_id,
                )
        success = bool(result.get('verified') is True or self._official_group_approval_executor_result_succeeded(result))
        retained_count = 0
        if success:
            selected_candidates = [dict(item) for item in (raw_result.get('selected_candidates') or []) if isinstance(item, dict)]
            approval_results = [dict(item) for item in (raw_result.get('approval_results') or []) if isinstance(item, dict)]
            if selected_candidates and approved_requester_id_set:
                selected_candidates = [
                    candidate
                    for candidate in selected_candidates
                    if str(candidate.get('requesterId') or candidate.get('requester_id') or candidate.get('jid') or '').strip() in approved_requester_id_set
                ]
            selected_candidates = self._merge_registration_group_candidate_metadata(
                selected_candidates=selected_candidates,
                expected_requesters=approved_requesters,
                approval_results=approval_results,
                target_member=dict(result.get('target_member') or {}) if isinstance(result.get('target_member'), dict) else None,
            )
            if not selected_candidates:
                requester_by_id = {
                    str(item.get('requesterId') or item.get('requester_id') or '').strip(): dict(item)
                    for item in approved_requesters
                    if isinstance(item, dict) and str(item.get('requesterId') or item.get('requester_id') or '').strip()
                }
                selected_candidates = [dict(requester_by_id.get(item) or {'requesterId': item}) for item in approved_requester_ids]
            eligibility_by_requester_id: Dict[str, Dict[str, Any]] = {}
            for item in list(approval_plan.get('eligible_candidates') or []):
                if not isinstance(item, dict):
                    continue
                requester_id = str(item.get('requester_id') or '').strip()
                if not requester_id:
                    requester = dict(item.get('requester') or {}) if isinstance(item.get('requester'), dict) else {}
                    requester_id = self._official_group_requester_identity(requester)
                if not requester_id:
                    continue
                eligibility_by_requester_id[requester_id] = self._registration_group_batch_member_eligibility_fields(
                    lead=dict(item.get('lead') or {}) if isinstance(item.get('lead'), dict) else {},
                    eligibility=dict(item.get('check_result') or {}) if isinstance(item.get('check_result'), dict) else {},
                    source='official_group_manual_approval_plan',
                )
            enriched_selected_candidates: List[Dict[str, Any]] = []
            for candidate in selected_candidates:
                row = dict(candidate)
                requester_id = self._official_group_requester_identity(row)
                if requester_id and requester_id in eligibility_by_requester_id:
                    row.update(eligibility_by_requester_id[requester_id])
                enriched_selected_candidates.append(row)
            selected_candidates = enriched_selected_candidates
            approved_at = str(result.get('approved_at') or result.get('finished_at') or decided_at)
            self._replace_registration_group_approval_batch_members(
                approval_run_id=approval_run_id,
                registration_group=official_group,
                registration_group_name=group_name,
                approved_at=approved_at,
                selected_candidates=selected_candidates,
                group_type='official_group',
            )
            retained_count = len(selected_candidates)
            with self.db.connect() as conn:
                persisted_member_rows = [
                    dict(item) for item in conn.execute(
                        'SELECT member_id, requester_id, display_name, display_name_source, display_name_enhanced_at, wa_phone_raw, wa_phone_normalized FROM registration_group_approval_batch_members WHERE approval_run_id = ? ORDER BY batch_index ASC',
                        (approval_run_id,),
                    ).fetchall()
                ]
            self._repair_registration_group_batch_member_rows(
                rows=persisted_member_rows,
                registration_group=official_group,
                registration_group_name=group_name,
            )
            lead_update_results = self._finalize_manual_official_group_lead_results(
                approval_run_id=approval_run_id,
                official_group=official_group,
                group_name=group_name,
                approved_at=approved_at,
                result=result,
                eligible_candidates=list(approval_plan.get('eligible_candidates') or []),
                approved_requester_ids=approved_requester_ids,
            )
        else:
            lead_update_results = []
        next_approval_runtime = self._build_binding_next_approval_runtime(
            responsible_type='official_group',
            binding=binding,
            probe=post_verify if isinstance(post_verify, dict) and post_verify else {
                'pending_count': post_approval_pending_count,
                'member_count': post_approval_member_count,
                'requester_ids': post_requester_ids,
                'group_id': official_group,
                'group_name': group_name,
            },
        )
        audit_payload = {
            'account_key': str(account.get('account_key') or '').strip(),
            'binding_index': binding_index,
            'official_group': official_group,
            'configured_official_group': configured_official_group or None,
            'group_name': group_name,
            'pending_count_before': pending_count,
            'eligible_count': eligible_count,
            'skipped_count': int(approval_plan.get('skipped_count') or 0),
            'approved_count': approved_count,
            'requester_ids': requester_ids,
            'approved_requester_ids': approved_requester_ids,
            'skipped_requesters': approval_plan.get('skipped_requesters') or [],
            'approval_run_id': approval_run_id,
            'operator': operator,
            'request': request_context,
            'result_code': result.get('result_code'),
            'verified': result.get('verified'),
            'retention_recorded_count': retained_count,
            'lead_update_results': lead_update_results,
            'post_verify_deferred': deferred_post_verify,
        }
        with self.db.connect() as conn:
            self._record_audit_event(
                conn,
                event_type='official_group_manual_approval_executed',
                event_source='ops_console_manual_approval',
                payload=audit_payload,
            )
            conn.commit()
        return {
            **result,
            'executed': success,
            'account_key': str(account.get('account_key') or '').strip(),
            'binding_index': binding_index,
            'binding': binding,
            'operator': operator,
            'request': request_context,
            'approval_scope': 'official_group',
            'approval_run_id': approval_run_id,
            'approved_count': approved_count,
            'pending_count_before': pending_count,
            'eligible_count': eligible_count,
            'skipped_count': int(approval_plan.get('skipped_count') or 0),
            'skipped_requesters': approval_plan.get('skipped_requesters') or [],
            'lead_update_results': lead_update_results,
            'retention_recorded_count': retained_count,
            'next_approval_runtime': next_approval_runtime,
            'post_verify_deferred': deferred_post_verify,
        }

    def _manual_approve_whatsapp_approval_binding_locked(self, account_key: str, binding_index: int, *, audit_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        audit_context = dict(audit_context or {})
        operator = dict(audit_context.get('operator') or {})
        request_context = dict(audit_context.get('request') or {})

        def _identity_gate(current_binding: Dict[str, Any]) -> Dict[str, Any]:
            binding_row = dict(current_binding or {})
            binding_id = str(binding_row.get('binding_id') or '').strip()
            identity_status = str(binding_row.get('identity_status') or '').strip()
            group_id = self._whatsapp_binding_runtime_group_id(binding_row)
            registration_group = str(binding_row.get('registration_group') or '').strip()
            if not binding_id:
                return {'ready': False, 'reason': 'binding_id_missing', 'identity_status': identity_status or 'unresolved'}
            if not group_id:
                return {
                    'ready': False,
                    'reason': 'identity_unresolved' if identity_status and identity_status != 'resolved' else 'group_id_missing',
                    'identity_status': identity_status or 'unresolved',
                }
            return {'ready': True, 'reason': 'resolved', 'identity_status': identity_status or 'resolved'}

        def _baileys_link_truth_allows_manual_approve(current_binding: Dict[str, Any]) -> bool:
            binding_row = dict(current_binding or {})
            provider_mode = resolve_whatsapp_approval_provider_mode(
                binding=binding_row,
                account=account,
                responsible_type='registration_group',
            )
            if not str(provider_mode or '').strip().lower().startswith('baileys'):
                return False
            if not _looks_like_whatsapp_invite_link(binding_row.get('link')):
                return False
            truth_view = dict(binding_row.get('approval_queue_truth') or {}) if isinstance(binding_row.get('approval_queue_truth'), dict) else {}
            if not truth_view:
                snapshots = self._load_approval_binding_queue_snapshots(
                    str(account.get('account_key') or account_key or '').strip(),
                    binding_row,
                )
                truth_view = self._approval_queue_truth_view(
                    snapshots.get('current_truth'),
                    snapshots.get('latest_probe'),
                )
            current_truth = dict(truth_view.get('current_truth') or truth_view.get('current_truth_raw') or {}) if isinstance(truth_view.get('current_truth') or truth_view.get('current_truth_raw'), dict) else {}
            facts = dict(current_truth.get('facts') or {}) if isinstance(current_truth.get('facts'), dict) else {}
            return bool(
                truth_view.get('can_manual_approve')
                or current_truth.get('can_manual_approve')
                or facts.get('can_manual_approve')
                or facts.get('manual_approve_allowed')
            ) and bool(
                current_truth.get('group_identity_verified')
                or facts.get('group_identity_verified')
                or facts.get('runtime_identity_match')
            )

        account = self._get_whatsapp_approval_account_runtime_row_for_operation(account_key)
        responsible_type = str(account.get('responsible_type') or '').strip()
        if responsible_type not in {'registration_group', 'official_group'}:
            raise HTTPException(status_code=400, detail='manual approve currently supports registration_group and official_group bindings only')
        bindings = list(account.get('group_binding_runtimes') or account.get('group_link_bindings') or [])
        if binding_index < 0 or binding_index >= len(bindings):
            raise HTTPException(status_code=404, detail='whatsapp approval binding not found')
        binding = dict(bindings[binding_index] or {})
        binding['account_key'] = str(account.get('account_key') or '').strip()
        if responsible_type == 'official_group':
            return self._manual_approve_official_group_binding_locked(
                account_key,
                binding_index,
                account=account,
                binding=binding,
                audit_context=audit_context,
            )
        request_id = str(request_context.get('request_id') or '').strip() or create_id('approval_op')
        login_gate_detail = self._whatsapp_approval_binding_operation_login_gate_detail(
            account=account,
            binding=binding,
            binding_index=binding_index,
            operation='manual_approve',
        )
        if login_gate_detail:
            self._update_whatsapp_binding_operation_state(
                account_key,
                binding_index,
                detail=str(login_gate_detail.get('message') or '账号未登录，无法执行审批'),
                stage_code='login_preflight_blocked',
                stage_label='登录校验',
                request_id=request_id,
            )
            raise HTTPException(status_code=409, detail=login_gate_detail)
        identity_gate = _identity_gate(binding)
        if not identity_gate.get('ready'):
            self._update_whatsapp_binding_operation_state(
                account_key,
                binding_index,
                detail='正在校验群绑定身份',
                stage_code='identity_preflight',
                stage_label='身份校验',
                request_id=request_id,
            )
            refresh_result = self.refresh_whatsapp_approval_binding_probe(
                str(account.get('account_key') or account_key or '').strip(),
                binding_index,
                _skip_operation_lock=True,
            )
            refreshed_binding = dict(refresh_result.get('binding_runtime') or {})
            if refreshed_binding:
                binding = {**binding, **refreshed_binding}
            identity_gate = _identity_gate(binding)
            if not identity_gate.get('ready'):
                if _baileys_link_truth_allows_manual_approve(binding):
                    identity_gate = {
                        'ready': True,
                        'reason': 'baileys_link_truth_ready',
                        'identity_status': identity_gate.get('identity_status') or 'unresolved',
                    }
                else:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            'reason': 'binding_identity_not_resolved',
                            'identity_status': identity_gate.get('identity_status'),
                            'reason_code': identity_gate.get('reason'),
                            'binding_id': str(binding.get('binding_id') or '').strip() or None,
                        },
                    )
        configured_registration_group = str(binding.get('registration_group') or '').strip()
        registration_group = (
            self._whatsapp_binding_runtime_group_id(binding)
            or configured_registration_group
        )
        if not registration_group and _baileys_link_truth_allows_manual_approve(binding):
            registration_group = str(binding.get('link') or '').strip()
        if not registration_group:
            raise HTTPException(status_code=400, detail='binding registration_group target is required')
        runtime_state = dict(account.get('runtime_state') or {})
        session_state = dict(account.get('session_state') or {})
        self._update_whatsapp_binding_operation_state(
            account_key,
            binding_index,
            detail='正在执行审批前同步',
            stage_code='preflight_sync',
            stage_label='审批前同步',
            request_id=request_id,
        )
        try:
            preflight = self.full_sync_whatsapp_approval_binding(
                str(account.get('account_key') or account_key or '').strip(),
                binding_index,
                source='manual_approve_preflight',
                timeout_seconds=30.0,
                _skip_operation_lock=True,
                request_id=request_id,
            )
        except Exception as exc:
            preflight = self._manual_approve_preflight_from_current_truth(
                account_key=str(account.get('account_key') or account_key or '').strip(),
                binding=binding,
                preflight_error=exc,
            )
            if not preflight:
                raise
            self._update_whatsapp_binding_operation_state(
                account_key,
                binding_index,
                detail='审批前同步抖动，已复用当前真值继续审批',
                stage_code='preflight_current_truth_reuse',
                stage_label='复用当前真值',
                request_id=request_id,
            )
        override_requested = bool(
            audit_context.get('allow_api_positive_override')
            or request_context.get('allow_api_positive_override')
        )
        override_allowed = bool(
            override_requested
            and self.whatsapp_approval_api_positive_override_enabled
            and preflight.get('manual_override_eligible')
        )
        if not bool(preflight.get('can_manual_approve')) and not override_allowed:
            failure_detail = self._manual_approval_preflight_failure_detail(preflight)
            self._update_whatsapp_binding_operation_state(
                account_key,
                binding_index,
                detail=str(failure_detail.get('message') or '审批前同步未通过'),
                stage_code=str(failure_detail.get('stage_code') or 'preflight_blocked'),
                stage_label=str(failure_detail.get('stage_label') or '前置拦截'),
                request_id=request_id,
            )
            raise HTTPException(status_code=409, detail=failure_detail)
        probe = dict(preflight or {})
        pending_count = max(int(probe.get('trusted_pending_count') or probe.get('ui_pending_count') or probe.get('pending_count') or 0), 0)
        if pending_count <= 0:
            raise HTTPException(status_code=400, detail='current binding has no pending requests to approve')
        approval_request_count = _coerce_registration_group_single_approval_count(
            pending_count,
            pending_count=pending_count,
        )
        if override_allowed:
            self._update_whatsapp_binding_operation_state(
                account_key,
                binding_index,
                detail='审批面未收敛，按灰度 API pending 直审条件继续执行',
                stage_code='api_positive_override',
                stage_label='灰度直审',
                request_id=request_id,
            )
        decided_at = utc_now()
        request = RegistrationGroupApprovalDecisionRequest(
            registration_group=registration_group,
            decision='approve',
            decided_at=decided_at,
            decided_by='ops:manual_approval',
            decided_by_name='群审批控制台',
            source_platform='ops_console',
            approved_count=approval_request_count,
            area=str(binding.get('area') or account.get('area') or 'Indonesia').strip() or 'Indonesia',
            remark='manual approval batch (api-positive override)' if override_allowed else 'manual approval batch',
            force_immediate=True,
            expected_pending_count=pending_count,
            expected_member_count=int(probe.get('member_count') or 0) if probe.get('member_count') is not None else None,
            expected_requester_ids=list(probe.get('requester_ids') or []) if isinstance(probe.get('requester_ids'), list) else None,
            expected_requesters=list(probe.get('requesters') or []) if isinstance(probe.get('requesters'), list) else None,
        )
        request.__dict__['provider_mode'] = resolve_whatsapp_approval_provider_mode(
            binding=binding,
            account=account,
            responsible_type='registration_group',
        )
        request.__dict__['registration_group_runtime'] = request.__dict__['provider_mode']
        request.__dict__['official_group_runtime'] = str(binding.get('official_group_runtime') or '').strip().lower()
        request.__dict__['group_assistant_runtime'] = str(binding.get('group_assistant_runtime') or '').strip().lower()
        self._update_whatsapp_binding_operation_state(
            account_key,
            binding_index,
            detail='正在提交人工审批' if approval_request_count == pending_count else f'正在提交人工审批（本次 {approval_request_count}/{pending_count} 人）',
            stage_code='approval_dispatch',
            stage_label='提交审批',
        )
        self.invalidate_approval_queue_truth_after_mutation(
            account_key=str(account.get('account_key') or account_key or '').strip(),
            binding=binding,
            invalidated_reason='approval_started',
            approved_count=approval_request_count,
            approval_run_id=request_id,
            action_ts=decided_at,
        )
        runtime_action_id = self._record_wa_runtime_action(
            account_key=str(account.get('account_key') or account_key or '').strip(),
            binding=binding,
            action_type='manual_approve',
            status='started',
            request_payload={
                'request_id': request_id,
                'provider_mode': request.__dict__.get('provider_mode'),
                'responsible_type': 'registration_group',
                'registration_group': registration_group,
                'expected_pending_count': pending_count,
                'requested_approved_count': approval_request_count,
                'override_allowed': override_allowed,
            },
            result_payload={'preflight': preflight},
        )
        result = self.whatsapp_approval_runtime_adapter.execute_registration_group_approval(
            service=self,
            payload=request,
        )
        approval_run_id = str(result.get('approval_run_id') or request_id).strip() or request_id
        self._record_wa_runtime_action(
            account_key=str(account.get('account_key') or account_key or '').strip(),
            binding=binding,
            action_type='manual_approve_result',
            status='completed',
            request_payload={
                'request_id': request_id,
                'runtime_action_id': runtime_action_id,
                'approval_run_id': approval_run_id,
                'provider_mode': str(result.get('provider_mode') or request.__dict__.get('provider_mode') or '').strip().lower(),
                'responsible_type': 'registration_group',
            },
            result_payload=result,
        )
        self.invalidate_approval_queue_truth_after_mutation(
            account_key=str(account.get('account_key') or account_key or '').strip(),
            binding=binding,
            invalidated_reason='approval_completed',
            approved_count=int(result.get('approved_count') or approval_request_count),
            pending_count=((result.get('raw_result') or {}).get('pending_after') if isinstance(result.get('raw_result'), dict) else None),
            approval_run_id=approval_run_id,
            action_ts=str(result.get('approved_at') or decided_at),
        )
        self._update_whatsapp_binding_operation_state(
            account_key,
            binding_index,
            detail='正在核验审批结果',
            stage_code='post_verify',
            stage_label='核验结果',
        )
        try:
            post_verify = self.full_sync_whatsapp_approval_binding(
                str(account.get('account_key') or account_key or '').strip(),
                binding_index,
                source='approval_after_sync',
                timeout_seconds=20.0,
                _skip_operation_lock=True,
                request_id=request_id,
            )
        except Exception as exc:
            try:
                self.write_event_ledger(
                    event_type='approval_truth_post_verify_failed',
                    object_type='registration_group_binding',
                    object_key=str(self._approval_binding_truth_object_key(str(account.get('account_key') or account_key or '').strip(), binding) or ''),
                    status='failed',
                    evidence_level='mutation',
                    payload={
                        'account_key': str(account.get('account_key') or account_key or '').strip(),
                        'binding_id': str(binding.get('binding_id') or '').strip() or None,
                        'approval_run_id': approval_run_id,
                        'error': str(exc),
                    },
                )
            except Exception:
                pass
            post_verify = {}
        if not (isinstance(post_verify, dict) and post_verify):
            try:
                self.write_event_ledger(
                    event_type='approval_truth_verify_deferred',
                    object_type='registration_group_binding',
                    object_key=str(self._approval_binding_truth_object_key(str(account.get('account_key') or account_key or '').strip(), binding) or ''),
                    status='pending',
                    evidence_level='mutation',
                    payload={
                        'account_key': str(account.get('account_key') or account_key or '').strip(),
                        'binding_id': str(binding.get('binding_id') or '').strip() or None,
                        'reason_code': 'approval_after_sync_failed',
                        'approval_run_id': approval_run_id,
                        'action_ts': str(result.get('approved_at') or decided_at),
                    },
                )
            except Exception as exc:
                self._record_worker_loop_error(exc)
        post_probe = {}
        post_approval_pending_count: Optional[int] = None
        post_approval_member_count: Optional[int] = None
        if isinstance(post_verify, dict) and post_verify:
            pending_after = ((result.get('raw_result') or {}).get('pending_after') if isinstance(result.get('raw_result'), dict) else None)
            if pending_after is None:
                pending_after = post_verify.get('trusted_pending_count')
            if pending_after is None:
                pending_after = post_verify.get('pending_count', post_verify.get('ui_pending_count', post_verify.get('api_pending_count')))
            member_count_after = ((result.get('raw_result') or {}).get('member_count_after') if isinstance(result.get('raw_result'), dict) else None)
            if member_count_after is None:
                member_count_after = post_verify.get('member_count')
            post_approval_pending_count = normalize_int_or_none(pending_after)
            post_approval_member_count = normalize_int_or_none(member_count_after)
            post_probe = {
                'group_id': str(post_verify.get('group_id') or registration_group).strip() or registration_group,
                'group_name': str(post_verify.get('group_name') or binding.get('group_name') or registration_group).strip() or registration_group,
                'pending_count': pending_after,
                'member_count': member_count_after,
                'requester_ids': [] if pending_after == 0 else (list(post_verify.get('requester_ids') or []) if isinstance(post_verify.get('requester_ids'), list) else []),
                'requesters': [] if pending_after == 0 else (list(post_verify.get('requesters') or []) if isinstance(post_verify.get('requesters'), list) else []),
                'self_participant_found': post_verify.get('self_participant_found'),
                'self_is_admin': post_verify.get('self_is_admin'),
                'can_manage_membership_requests': post_verify.get('can_manage_membership_requests'),
                'review_surface_ready': post_verify.get('review_surface_ready'),
                'empty_queue_visible': post_verify.get('empty_queue_visible'),
            }
        else:
            try:
                post_probe = self.whatsapp_approval_runtime_adapter.registration_group_executor_state(
                    service=self,
                    registration_group=registration_group,
                    allow_legacy_target=False,
                )
            except Exception:
                post_probe = self.whatsapp_approval_runtime_adapter.probe_binding_group_state(
                    service=self,
                    responsible_type='registration_group',
                    binding=binding,
                    runtime_state=runtime_state,
                    session_state=session_state,
                    allow_shared_fallback=False,
                    attempts=1,
                    timeout_seconds=2.0,
                )
            pending_after = post_probe.get('pending_count')
            if pending_after is None:
                pending_after = ((result.get('raw_result') or {}).get('pending_after') if isinstance(result.get('raw_result'), dict) else None)
            member_count_after = post_probe.get('member_count')
            if member_count_after is None:
                member_count_after = ((result.get('raw_result') or {}).get('member_count_after') if isinstance(result.get('raw_result'), dict) else None)
            post_approval_pending_count = normalize_int_or_none(pending_after)
            post_approval_member_count = normalize_int_or_none(member_count_after)
            if pending_after is None:
                post_probe = {**post_probe, 'pending_count': 0}
                pending_after = 0
        if post_approval_pending_count is not None:
            self._write_post_approval_queue_current_truth(
                account_key=str(account.get('account_key') or account_key or '').strip(),
                binding=binding,
                approved_count=int(result.get('approved_count') or approval_request_count),
                pending_count=post_approval_pending_count,
                approval_run_id=approval_run_id,
                action_ts=str(result.get('approved_at') or decided_at),
                requester_ids=list(post_probe.get('requester_ids') or []) if isinstance(post_probe.get('requester_ids'), list) else [],
                member_count=post_approval_member_count,
            )
        next_approval_runtime = self._build_binding_next_approval_runtime(
            responsible_type='registration_group',
            binding=binding,
            probe=post_probe if isinstance(post_probe, dict) else {},
        )
        notification = {
            'status': 'skipped_not_success',
            'code': 'manual_approval_succeeded',
        }
        if result.get('verified') is True and result.get('crm_recorded') is True:
            self._sync_manual_registration_group_approval_to_production_ops_state(
                binding=binding,
                probe=probe,
                approved_at=decided_at,
            )
            incident = {
                'severity': 'info',
                'code': 'manual_approval_succeeded',
                'summary': '注册群审批成功',
                'details': {
                    'approval_run_id': str(result.get('approval_run_id') or '').strip() or None,
                    'approved_count': int(result.get('approved_count') or approval_request_count),
                    'pending_after': max(int(pending_after or 0), 0),
                    'member_count_after': int(member_count_after) if member_count_after is not None else None,
                    'result_code': result.get('result_code'),
                },
                'dedupe_key': f"manual_approval_succeeded:{str(result.get('approval_run_id') or '').strip() or uuid.uuid4().hex[:12]}",
            }
            cycle = {
                'checked_at': decided_at,
                'registration_group': registration_group,
                'configured_registration_group': configured_registration_group or None,
                'monitor_target': {
                    'group_name': str(post_probe.get('group_name') or probe.get('group_name') or binding.get('group_name') or registration_group).strip() or registration_group,
                },
            }
            notification = self._send_registration_group_binding_notification(
                binding=binding,
                incident=incident,
                cycle=cycle,
                event_type='registration_group_manual_approval_notification_sent',
            )
        audit_payload = {
            'account_key': str(account.get('account_key') or '').strip(),
            'binding_index': binding_index,
            'registration_group': registration_group,
            'group_jid': registration_group,
            'configured_registration_group': configured_registration_group or None,
            'group_name': str(post_probe.get('group_name') or probe.get('group_name') or binding.get('group_name') or registration_group).strip() or registration_group,
            'pending_count_before': pending_count,
            'approved_count': int(result.get('approved_count') or approval_request_count),
            'requested_approved_count': approval_request_count,
            'approval_run_id': str(result.get('approval_run_id') or '').strip() or None,
            'operator': operator,
            'request': request_context,
            'result_code': result.get('result_code'),
            'verified': result.get('verified'),
            'crm_recorded': result.get('crm_recorded'),
            'manual_override_used': override_allowed,
            'manual_override_mode': probe.get('manual_override_mode'),
            'manual_override_reason_code': probe.get('reason_code') if override_allowed else None,
            'notification': {
                key: value
                for key, value in dict(notification or {}).items()
                if key != 'message_text'
            },
        }
        with self.db.connect() as conn:
            self._record_audit_event(
                conn,
                event_type='registration_group_manual_approval_executed',
                event_source='ops_console_manual_approval',
                payload=audit_payload,
            )
            conn.commit()
        return {
            **result,
            'account_key': str(account.get('account_key') or '').strip(),
            'binding_index': binding_index,
            'binding': binding,
            'operator': operator,
            'request': request_context,
            'manual_override_used': override_allowed,
            'manual_override_mode': probe.get('manual_override_mode'),
            'notification': notification,
            'next_approval_runtime': next_approval_runtime,
        }

    @staticmethod
    def _extract_whatsapp_group_jid_from_payload(payload: Any) -> str:
        preferred_keys = {
            'resolvedGroupId',
            'resolved_group_id',
            'groupJid',
            'group_jid',
            'groupId',
            'group_id',
            'chatId',
            'chat_id',
            'jid',
        }
        seen: set[int] = set()

        def visit(value: Any) -> str:
            if value is None:
                return ''
            if isinstance(value, str):
                return _sanitize_whatsapp_group_jid(value)
            if isinstance(value, (int, float, bool)):
                return ''
            marker = id(value)
            if marker in seen:
                return ''
            seen.add(marker)
            if isinstance(value, dict):
                for key in preferred_keys:
                    found = visit(value.get(key))
                    if found:
                        return found
                for nested in value.values():
                    found = visit(nested)
                    if found:
                        return found
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    found = visit(nested)
                    if found:
                        return found
            return ''

        return visit(payload)

    @staticmethod
    def _extract_whatsapp_group_name_from_payload(payload: Any) -> str:
        preferred_keys = (
            'groupSubject',
            'group_subject',
            'groupName',
            'group_name',
            'subject',
            'title',
        )
        seen: set[int] = set()

        def usable(value: Any) -> str:
            text = str(value or '').strip()
            if not text or _looks_like_whatsapp_invite_link(text) or _looks_like_whatsapp_group_jid(text):
                return ''
            return text

        def visit(value: Any) -> str:
            if value is None or isinstance(value, (int, float, bool, str)):
                return ''
            marker = id(value)
            if marker in seen:
                return ''
            seen.add(marker)
            if isinstance(value, dict):
                for key in preferred_keys:
                    found = usable(value.get(key))
                    if found:
                        return found
                for nested in value.values():
                    found = visit(nested)
                    if found:
                        return found
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    found = visit(nested)
                    if found:
                        return found
            return ''

        return visit(payload)

    def _persist_whatsapp_approval_binding_probe_identity(
        self,
        account_key: str,
        binding_index: int,
        binding: Dict[str, Any],
        live_probe: Dict[str, Any],
    ) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key or binding_index < 0 or not isinstance(live_probe, dict):
            return binding
        probed_group_id = self._extract_whatsapp_group_jid_from_payload(live_probe)
        probed_group_name = (
            str(live_probe.get('group_name') or '').strip()
            or self._extract_whatsapp_group_name_from_payload(live_probe)
        )
        if _looks_like_whatsapp_invite_link(probed_group_name):
            probed_group_name = ''
        invite_page_group_name = ''
        if _looks_like_whatsapp_group_jid(probed_group_name) or not probed_group_name:
            invite_page_group_name = _fetch_whatsapp_invite_page_group_name(binding.get('link'))
            if invite_page_group_name:
                probed_group_name = invite_page_group_name
        if not probed_group_id and not probed_group_name:
            return binding
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT responsible_type, group_links FROM whatsapp_approval_accounts WHERE account_key = ?",
                (normalized_key,),
            ).fetchone()
            if row is None:
                return binding
            try:
                raw_bindings = json.loads(str(row['group_links'] or '[]'))
            except Exception:
                raw_bindings = []
            if not isinstance(raw_bindings, list) or binding_index >= len(raw_bindings):
                return binding
            raw_target = dict(raw_bindings[binding_index] or {}) if isinstance(raw_bindings[binding_index], dict) else {}
            normalized_bindings = _normalize_group_link_bindings(
                [dict(item or {}) for item in raw_bindings if isinstance(item, dict)],
                responsible_type=str(row['responsible_type'] or '').strip(),
            )
            if binding_index >= len(normalized_bindings):
                return binding
            target = dict(normalized_bindings[binding_index] or {})
            changed = False
            stored_group_id = (
                _sanitize_whatsapp_group_jid(target.get('group_id'))
                or _sanitize_whatsapp_group_jid(raw_target.get('registration_group'))
                or _sanitize_whatsapp_group_jid(target.get('registration_group'))
            )
            probe_matches_stored_group = bool(probed_group_id and stored_group_id and probed_group_id == stored_group_id)
            probe_resolves_missing_group = bool(probed_group_id and not stored_group_id)
            probe_group_identity_safe = probe_matches_stored_group or probe_resolves_missing_group
            probe_group_identity_mismatch = bool(probed_group_id and stored_group_id and probed_group_id != stored_group_id)
            if probed_group_id and probe_group_identity_safe:
                if str(target.get('group_id') or '').strip() != probed_group_id:
                    target['group_id'] = probed_group_id
                    changed = True
                raw_registration_group = str(raw_target.get('registration_group') or '').strip()
                if (
                    str(target.get('registration_group') or '').strip() != probed_group_id
                    or raw_registration_group != probed_group_id
                ):
                    target['registration_group'] = probed_group_id
                    changed = True
            if probed_group_name and probe_group_identity_safe and str(target.get('group_name') or '').strip() != probed_group_name:
                target['group_name'] = probed_group_name
                changed = True
            identity_resolved = bool(probed_group_id and probe_group_identity_safe)
            metadata_updates = {
                'identity_status': 'resolved' if identity_resolved else 'unresolved',
                'identity_resolved_at': utc_now() if identity_resolved else str(target.get('identity_resolved_at') or ''),
                'identity_resolved_by': 'runtime_probe' if identity_resolved else str(target.get('identity_resolved_by') or ''),
                'runtime_probe_group_id': probed_group_id if probe_group_identity_safe else str(target.get('runtime_probe_group_id') or ''),
                'runtime_probe_group_name': probed_group_name if probe_group_identity_safe else str(target.get('runtime_probe_group_name') or ''),
                'last_probe_status': 'resolved' if identity_resolved else ('identity_mismatch' if probe_group_identity_mismatch else 'identity_unresolved'),
                'last_probe_reason': 'resolved' if identity_resolved else ('probe_group_id_mismatch' if probe_group_identity_mismatch else 'identity_unresolved'),
                'last_probe_at': utc_now(),
                'last_probe_had_group_id': bool(probed_group_id),
                'last_probe_had_group_name': bool(probed_group_name),
                'last_probe_self_participant_found': live_probe.get('self_participant_found'),
                'last_probe_self_is_admin': live_probe.get('self_is_admin'),
                'last_probe_can_manage_membership_requests': live_probe.get('can_manage_membership_requests'),
                'last_probe_member_count': live_probe.get('member_count'),
            }
            for key, value in metadata_updates.items():
                if target.get(key) != value:
                    target[key] = value
                    changed = True
            if not changed:
                return {**binding, **target}
            target['config_fingerprint'] = _whatsapp_approval_binding_config_fingerprint(target)
            normalized_bindings[binding_index] = target
            conn.execute(
                "UPDATE whatsapp_approval_accounts SET group_links = ?, verification_status = ?, updated_at = ? WHERE account_key = ?",
                (json.dumps(normalized_bindings, ensure_ascii=False), 'pending_verification', utc_now(), normalized_key),
            )
            conn.commit()
        return {**binding, **target}

    def _registration_group_group_state_with_legacy_fallback(self, registration_group: str) -> Dict[str, Any]:
        target = str(registration_group or '').strip()
        if not target:
            return {}
        try:
            return self.registration_group_approval_executor_group_state(target, allow_legacy_target=True)
        except TypeError as exc:
            if 'allow_legacy_target' not in str(exc):
                raise
            return self.registration_group_approval_executor_group_state(target)

    def refresh_whatsapp_approval_binding_probe(self, account_key: str, binding_index: int, *, probe_mode: str = 'strict', _skip_operation_lock: bool = False) -> Dict[str, Any]:
        normalized_probe_mode = self._normalize_whatsapp_probe_refresh_mode(probe_mode)
        provider_probe_priority = 'P0' if normalized_probe_mode == 'fast' else 'P1'
        runtime_actor: Optional[Dict[str, Any]] = None
        if not _skip_operation_lock:
            runtime_actor = self._acquire_whatsapp_runtime_actor(
                account_key=account_key,
                operation='probe_refresh',
                binding_index=binding_index,
                wait_timeout_seconds=90.0,
            )
            self._mark_whatsapp_binding_operation_started(
                account_key,
                binding_index,
                operation='probe_refresh',
                detail='正在刷新探针',
                stage_code='live_probe',
                stage_label='实时探针',
            )
        try:
            account = self._get_whatsapp_approval_account_runtime_row_for_operation(account_key)
            bindings = list(account.get('group_binding_runtimes') or account.get('group_link_bindings') or [])
            if binding_index < 0 or binding_index >= len(bindings):
                raise HTTPException(status_code=404, detail='whatsapp approval binding not found')
            binding = dict(bindings[binding_index] or {})
            binding['account_key'] = str(account.get('account_key') or '').strip()
            responsible_type = str(account.get('responsible_type') or '').strip()
            persisted_binding: Dict[str, Any] = {}
            try:
                with self.db.connect() as conn:
                    persisted_row = conn.execute(
                        "SELECT responsible_type, group_links FROM whatsapp_approval_accounts WHERE account_key = ?",
                        (str(account.get('account_key') or account_key or '').strip(),),
                    ).fetchone()
                if persisted_row is not None:
                    try:
                        raw_persisted_bindings = json.loads(str(persisted_row['group_links'] or '[]'))
                    except Exception:
                        raw_persisted_bindings = []
                    if isinstance(raw_persisted_bindings, list):
                        normalized_persisted_bindings = _normalize_group_link_bindings(
                            [dict(item or {}) for item in raw_persisted_bindings if isinstance(item, dict)],
                            responsible_type=str(persisted_row['responsible_type'] or responsible_type).strip(),
                        )
                        if 0 <= binding_index < len(normalized_persisted_bindings):
                            persisted_binding = dict(normalized_persisted_bindings[binding_index] or {})
            except Exception:
                persisted_binding = {}
            runtime_state = dict(account.get('runtime_state') or {})
            session_state = dict(account.get('session_state') or {})
            live_probe: Dict[str, Any] = {}
            provider_name = str(
                binding.get('provider_name')
                or runtime_state.get('provider_name')
                or account.get('provider_name')
                or ''
            ).strip().lower()
            provider_mode = str(
                binding.get('provider_mode')
                or runtime_state.get('provider_mode')
                or account.get('provider_mode')
                or ''
            ).strip().lower()
            baileys_provider = provider_name == 'baileys' or provider_mode.startswith('baileys')
            login_state = map_whatsapp_login_state(
                runtime_state=runtime_state,
                session_state=session_state,
                account_enabled=bool(account.get('enabled', True)),
            )
            if not _skip_operation_lock:
                login_gate_detail = self._whatsapp_approval_binding_operation_login_gate_detail(
                    account=account,
                    binding=binding,
                    binding_index=binding_index,
                    operation='probe_refresh',
                )
                if login_gate_detail:
                    self._update_whatsapp_binding_operation_state(
                        account_key,
                        binding_index,
                        detail=str(login_gate_detail.get('message') or '账号未登录，无法刷新状态'),
                        stage_code='login_preflight_blocked',
                        stage_label='登录校验',
                    )
                    raise HTTPException(status_code=409, detail=login_gate_detail)
            if responsible_type == 'registration_group':
                registration_group = self._whatsapp_binding_probe_target(binding)
                allow_non_jid_fallback = False
                if not registration_group and baileys_provider:
                    invite_target = self._whatsapp_binding_invite_link_target(binding)
                    resolved_target = self._resolve_whatsapp_runtime_target_group(
                        responsible_type='registration_group',
                        target_group=invite_target,
                    ) if invite_target else ''
                    if resolved_target:
                        registration_group = resolved_target
                        binding = {
                            **binding,
                            'group_id': resolved_target,
                            'registration_group': resolved_target,
                        }
                    else:
                        registration_group = invite_target
                        allow_non_jid_fallback = bool(registration_group)
                if not registration_group:
                    raise HTTPException(status_code=400, detail='binding registration_group target is required')
                snapshots = self._load_approval_binding_queue_snapshots(account_key, binding)
                current_truth = dict(snapshots.get('current_truth') or {}) if isinstance(snapshots.get('current_truth'), dict) else {}
                current_truth_ts = str(current_truth.get('source_ts') or current_truth.get('checked_at') or '').strip()
                current_truth_fresh = False
                if current_truth_ts:
                    try:
                        current_truth_fresh = (datetime.now(timezone.utc) - parse_iso_datetime(current_truth_ts)).total_seconds() <= 30
                    except Exception:
                        current_truth_fresh = False
                if normalized_probe_mode == 'fast':
                    probe_attempts = 1
                    probe_timeout = 4.0
                else:
                    probe_attempts = 1 if current_truth_fresh and str(binding.get('identity_status') or '').strip().lower() == 'resolved' else 2
                    probe_timeout = 12.0 if probe_attempts == 1 else 25.0
                live_probe = self.whatsapp_approval_runtime_adapter.probe_binding_group_state(
                    service=self,
                    responsible_type='registration_group',
                    binding=binding,
                    runtime_state=runtime_state,
                    session_state=session_state,
                    allow_shared_fallback=False,
                    allow_non_jid_fallback=allow_non_jid_fallback,
                    attempts=probe_attempts,
                    timeout_seconds=probe_timeout,
                    priority=provider_probe_priority,
                )
            else:
                if normalized_probe_mode == 'fast':
                    probe_attempts = 1
                    probe_timeout = 4.0
                else:
                    probe_attempts = 2
                    probe_timeout = 25.0
                direct_baileys_probe = bool(baileys_provider and normalized_probe_mode == 'fast')
                live_probe = self.whatsapp_approval_runtime_adapter.probe_binding_group_state(
                    service=self,
                    responsible_type=responsible_type,
                    binding=binding,
                    runtime_state=runtime_state,
                    session_state=session_state,
                    allow_shared_fallback=not direct_baileys_probe,
                    allow_non_jid_fallback=responsible_type == 'official_group',
                    attempts=probe_attempts,
                    timeout_seconds=probe_timeout,
                    priority=provider_probe_priority,
                )
            if isinstance(live_probe, dict):
                anchor_identity = self._lookup_binding_cycle_anchor_identity(
                    production_ops=self._production_ops_daemon_snapshot(),
                    responsible_type=responsible_type,
                    binding=binding,
                    probe=live_probe,
                )
                anchor_group_id = str(anchor_identity.get('group_id') or '').strip()
                anchor_group_name = str(anchor_identity.get('group_name') or '').strip()
                persisted_registration_group = str(persisted_binding.get('registration_group') or '').strip()
                persisted_registration_group_id = _sanitize_whatsapp_group_jid(persisted_registration_group)
                stored_group_id = str(
                    persisted_binding.get('group_id')
                    or persisted_binding.get('runtime_probe_group_id')
                    or persisted_registration_group_id
                    or ''
                ).strip()
                stored_group_name = str(
                    persisted_binding.get('group_name')
                    or persisted_binding.get('runtime_probe_group_name')
                    or ''
                ).strip()
                if anchor_group_id and not stored_group_id and not str(live_probe.get('group_id') or '').strip():
                    live_probe = {**live_probe, 'group_id': anchor_group_id}
                if anchor_group_name and not stored_group_name and not str(live_probe.get('group_name') or '').strip():
                    live_probe = {**live_probe, 'group_name': anchor_group_name}
                self._update_whatsapp_binding_operation_state(
                    account_key,
                    binding_index,
                    detail='正在写回探针识别结果',
                    stage_code='persist_runtime_identity',
                    stage_label='写回识别结果',
                )
                binding = self._persist_whatsapp_approval_binding_probe_identity(
                    str(account.get('account_key') or '').strip(),
                    binding_index,
                    binding,
                    live_probe,
                )
                persisted_group_id = str(binding.get('group_id') or '').strip()
                persisted_group_name = str(binding.get('group_name') or '').strip()
                if not persisted_group_id and str(live_probe.get('group_id') or '').strip():
                    persisted_group_id = str(live_probe.get('group_id') or '').strip()
                    binding = {**binding, 'group_id': persisted_group_id, 'registration_group': persisted_group_id}
                if not persisted_group_name and str(live_probe.get('group_name') or '').strip():
                    persisted_group_name = str(live_probe.get('group_name') or '').strip()
                    binding = {**binding, 'group_name': persisted_group_name}
                if persisted_group_name and _looks_like_whatsapp_group_jid(str(live_probe.get('group_name') or '').strip()):
                    live_probe = {**live_probe, 'group_name': persisted_group_name}
                if not str(live_probe.get('group_id') or '').strip():
                    fallback_group_id = self._whatsapp_binding_runtime_group_id(binding)
                    if fallback_group_id:
                        live_probe = {**live_probe, 'group_id': fallback_group_id}
                if not str(live_probe.get('group_name') or '').strip() and persisted_group_name:
                    live_probe = {**live_probe, 'group_name': persisted_group_name}
            verifier = self._binding_membership_verifier_state(
                binding,
                dict(account.get('membership_verifier') or {}),
                responsible_type=responsible_type,
                production_ops=self._production_ops_daemon_snapshot(),
                live_probe=live_probe,
                runtime_state=runtime_state,
                session_state=session_state,
            )
            next_runtime = self._build_binding_next_approval_runtime(
                responsible_type=responsible_type,
                binding=binding,
                probe=live_probe if isinstance(live_probe, dict) else {},
            )
            binding_runtime = {
                **binding,
                **next_runtime,
                'membership_verifier': verifier,
            }
            if isinstance(live_probe, dict):
                if not str(live_probe.get('group_id') or '').strip():
                    fallback_group_id = self._whatsapp_binding_runtime_group_id(binding)
                    if fallback_group_id:
                        live_probe = {**live_probe, 'group_id': fallback_group_id}
                if not str(live_probe.get('group_name') or '').strip() and str(binding.get('group_name') or '').strip():
                    live_probe = {**live_probe, 'group_name': str(binding.get('group_name') or '').strip()}
                binding_runtime['runtime_probe_group_name'] = live_probe.get('group_name')
                binding_runtime['runtime_probe_group_id'] = live_probe.get('group_id')
            return {
                'account_key': str(account.get('account_key') or '').strip(),
                'binding_index': binding_index,
                'binding_runtime': binding_runtime,
                'probe': live_probe,
                'probe_mode': normalized_probe_mode,
            }
        finally:
            if not _skip_operation_lock:
                self._clear_whatsapp_binding_operation(account_key, binding_index)
                self._release_whatsapp_runtime_actor(runtime_actor)

    def _upsert_intake_bot_preset_row(self, *, profile_name: str, app_id: Optional[str], robot_name: Optional[str], default_app: str, default_guild: str, enabled: int = 1) -> Dict[str, Any]:
        normalized_profile_name = str(profile_name or '').strip()
        normalized_robot_name = str(robot_name or '').strip() or normalized_profile_name
        row = {
            'profile_name': normalized_profile_name,
            'app_id': str(app_id or '').strip(),
            'robot_name': normalized_robot_name,
            'default_app': str(default_app or '').strip(),
            'default_guild': str(default_guild or '').strip(),
            'enabled': int(enabled),
            'updated_at': utc_now(),
        }
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO intake_bot_presets (profile_name, app_id, robot_name, default_app, default_guild, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_name)
                DO UPDATE SET app_id = excluded.app_id,
                              robot_name = excluded.robot_name,
                              default_app = excluded.default_app,
                              default_guild = excluded.default_guild,
                              enabled = excluded.enabled,
                              updated_at = excluded.updated_at
                """,
                (row['profile_name'], row['app_id'], row['robot_name'], row['default_app'], row['default_guild'], row['enabled'], row['updated_at']),
            )
            conn.commit()
        return row

    @staticmethod
    def _normalize_timo_id(value: Optional[str]) -> str:
        return ''.join(ch for ch in str(value or '').strip() if ch.isdigit())

    @staticmethod
    def _normalize_timo_mobile(value: Optional[str]) -> str:
        raw = str(value or '').strip()
        if not raw:
            return ''
        digits = ''.join(ch for ch in raw if ch.isdigit())
        return f'+{digits}' if raw.startswith('+') and digits else digits

    @staticmethod
    def _make_timo_id_only_phone(timo_id: Any) -> str:
        normalized = Service._normalize_timo_id(str(timo_id or ''))
        return f'Timo:{normalized}' if normalized else ''

    @staticmethod
    def _timo_mobile_format_valid(value: Optional[str]) -> bool:
        raw = str(value or '').strip()
        if not raw.startswith('+'):
            return False
        if validate_fast_intake_fields(mobile=value, app_name='Timo', account_id=None) is None:
            return True
        digits = ''.join(ch for ch in raw if ch.isdigit())
        return bool(re.fullmatch(r'\d{8,15}', digits))

    @staticmethod
    def _timo_id_format_valid(value: Optional[str]) -> bool:
        return validate_fast_intake_fields(mobile=None, app_name='Timo', account_id=value) is None

    @staticmethod
    def _extract_labeled_text_value(text: str, labels: Tuple[str, ...]) -> str:
        cleaned = str(text or '')
        for label in labels:
            pattern = rf'(?:^|\n|\b){re.escape(label)}[ \t]*[:：=]?[ \t]*([^\n,，;；]+)'
            match = re.search(pattern, cleaned, flags=re.IGNORECASE)
            if match:
                return str(match.group(1) or '').strip()
        return ''

    def parse_timo_intake_text(self, *, text: str, fields: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        fields = dict(fields or {})
        cleaned_text = str(text or '').strip()
        bare_candidates = extract_bare_multiline_candidates(cleaned_text)
        ocr_normalized = normalize_native_ocr_fields(cleaned_text) if cleaned_text else {}
        mobile = self._normalize_timo_mobile(
            fields.get('mobile')
            or fields.get('phone')
            or self._extract_labeled_text_value(cleaned_text, ('手机号', '手机', 'phone', 'mobile', 'tel', 'whatsapp', 'wa'))
        )
        if not mobile and bare_candidates.get('mobile_line'):
            mobile = self._normalize_timo_mobile(bare_candidates.get('mobile_line'))
        if not mobile:
            plus_mobile = next((candidate for candidate in re.findall(r'\+[ \t]*\d[\d \t().-]{6,}\d', cleaned_text) if ''.join(ch for ch in candidate if ch.isdigit())), '')
            mobile = self._normalize_timo_mobile(plus_mobile)
        mobile_digits = ''.join(ch for ch in mobile if ch.isdigit())
        explicit_timo_id = self._normalize_timo_id(
            fields.get('timo_id')
            or fields.get('timoId')
            or fields.get('account_id')
        )
        timo_id = self._normalize_timo_id(
            explicit_timo_id
            or ocr_normalized.get('timo_id')
            or ocr_normalized.get('account_id')
            or self._extract_labeled_text_value(cleaned_text, ('timo id', 'timoid', 'timo_id', 'timo', '主播id', '用户id', 'id'))
        )
        if not timo_id:
            bare_account_id = str(bare_candidates.get('account_id_line') or '').strip()
            if re.fullmatch(r'\d{12}', bare_account_id) and bare_account_id != mobile_digits:
                timo_id = bare_account_id
        if not timo_id:
            digit_candidates = re.findall(r'(?<!\d)\d{12}(?!\d)', cleaned_text)
            timo_id = next((value for value in digit_candidates if value != mobile_digits), '')
        if mobile_digits and timo_id == mobile_digits and not explicit_timo_id:
            timo_id = ''
        group_name = str(
            fields.get('group_name')
            or fields.get('group')
            or fields.get('guild')
            or self._extract_labeled_text_value(cleaned_text, ('群名', '群组', '公会', 'guild', 'group', 'agency'))
            or bare_candidates.get('registration_group_line')
            or ''
        ).strip()
        if group_name and timo_id and timo_id in group_name and re.search(r'\b(timo|id)\b', group_name, flags=re.IGNORECASE):
            group_name = ''
        if not group_name and (cleaned_text or mobile or timo_id):
            group_name = OTHER_CHANNEL_REGISTRATION_GROUP
        errors = []
        if mobile and not self._timo_mobile_format_valid(mobile):
            errors.append('invalid_mobile_format')
        if not timo_id:
            errors.append('missing_timo_id')
        elif not self._timo_id_format_valid(timo_id):
            errors.append('invalid_timo_id_format')
        mobile_valid = bool(not mobile or 'invalid_mobile_format' not in errors)
        timo_id_valid = bool(timo_id and 'invalid_timo_id_format' not in errors)
        return {
            'fields': {'mobile': mobile, 'timo_id': timo_id, 'group_name': group_name},
            'validation': {'mobile': mobile_valid, 'timo_id': timo_id_valid, 'group_name': bool(group_name)},
            'errors': errors,
            'can_submit': bool(mobile_valid and timo_id_valid and group_name),
            'raw_text': cleaned_text,
        }

    @staticmethod
    def _normalize_sogo_id(value: Optional[str]) -> str:
        return ''.join(ch for ch in str(value or '').strip() if ch.isdigit())

    def parse_sogo_intake_text(self, *, text: str, fields: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        fields = dict(fields or {})
        cleaned_text = str(text or '').strip()
        explicit_sogo_id = self._normalize_sogo_id(
            fields.get('sogo_id')
            or fields.get('sugo_id')
            or fields.get('account_id')
            or fields.get('id')
        )
        sogo_id = explicit_sogo_id or self._normalize_sogo_id(
            self._extract_labeled_text_value(cleaned_text, ('sogo id', 'sugoid', 'sogo_id', 'sugo id', 'sugo_id', '主播id', '用户id', 'id'))
        )
        if not sogo_id:
            digit_candidates = re.findall(r'(?<!\d)\d{5,15}(?!\d)', cleaned_text)
            sogo_id = next((value for value in digit_candidates), '')
        group_name = str(
            fields.get('group_name')
            or fields.get('group')
            or fields.get('guild')
            or self._extract_labeled_text_value(cleaned_text, ('群名', '群组', '公会', 'guild', 'group', 'agency'))
            or ''
        ).strip()
        errors = []
        if not sogo_id:
            errors.append('missing_sogo_id')
        elif not re.fullmatch(r'\d{5,15}', sogo_id):
            errors.append('invalid_sogo_id_format')
        return {
            'fields': {'sogo_id': sogo_id, 'group_name': group_name},
            'validation': {'sogo_id': bool(sogo_id and 'invalid_sogo_id_format' not in errors), 'group_name': bool(group_name)},
            'errors': errors,
            'can_submit': bool(sogo_id and not errors),
            'raw_text': cleaned_text,
        }

    def verify_sogo_intake_member(self, *, guild_name: str, sogo_id: str, user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized_guild = str(guild_name or '').strip()
        normalized_sogo_id = self._normalize_sogo_id(sogo_id)
        if not normalized_guild:
            raise HTTPException(status_code=400, detail='guild_name is required.')
        if not normalized_sogo_id:
            raise HTTPException(status_code=400, detail='Sugo ID is required.')
        if not re.fullmatch(r'\d{5,15}', normalized_sogo_id):
            raise HTTPException(status_code=400, detail='Sugo ID 格式有误，请填写数字 ID。')
        if not self._ops_intake_user_can_access_guild(user, normalized_guild):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        executor = self.resolve_guild_executor(normalized_guild, app_name=SUGO_APP_NAME) or self.resolve_guild_executor(normalized_guild, app_name=SUGO_LEGACY_APP_NAME)
        if not executor or not bool(executor.get('enabled')):
            raise HTTPException(status_code=404, detail='sugo guild executor not found')
        if not str(executor.get('platform_authorization') or '').strip():
            raise HTTPException(status_code=400, detail='Sugo Access Token 未配置。')
        timeout_seconds = float(executor.get('request_timeout_seconds') or 30)
        found_anchor: Optional[Dict[str, Any]] = None
        total_count: Optional[int] = None
        scanned_count = 0
        page_count = 0

        def _anchors_from_payload(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[int]]:
            data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
            anchors = data.get('anchors') if isinstance(data.get('anchors'), list) else []
            cleaned = [dict(item) for item in anchors if isinstance(item, dict)]
            total_raw = data.get('total_count')
            try:
                total = int(total_raw) if total_raw not in (None, '') else None
            except Exception:
                total = None
            return cleaned, total

        for index, params in enumerate((
            {'page': 1, 'page_size': 50, 'anchor_id': normalized_sogo_id},
            {'page': 1, 'page_size': 50},
        )):
            payload = self._sogo_guild_api_get(
                executor=executor,
                path='/union/anchor/',
                params=params,
                timeout_seconds=timeout_seconds,
            )
            anchors, total = _anchors_from_payload(payload)
            page_count += 1
            total_count = total if total is not None else total_count
            scanned_count += len(anchors)
            found_anchor = next((item for item in anchors if str(item.get('uid') or '').strip() == normalized_sogo_id), None)
            if found_anchor or index == 0 and anchors and total == 1:
                break

        page = 2
        max_pages = 20
        while not found_anchor and total_count and scanned_count < total_count and page <= max_pages:
            payload = self._sogo_guild_api_get(
                executor=executor,
                path='/union/anchor/',
                params={'page': page, 'page_size': 50},
                timeout_seconds=timeout_seconds,
            )
            anchors, total = _anchors_from_payload(payload)
            page_count += 1
            total_count = total if total is not None else total_count
            scanned_count += len(anchors)
            found_anchor = next((item for item in anchors if str(item.get('uid') or '').strip() == normalized_sogo_id), None)
            if not anchors:
                break
            page += 1

        verified = found_anchor is not None
        return {
            'ok': True,
            'verified': verified,
            'result_code': 'sogo_member_found' if verified else 'sogo_member_not_found',
            'result_reason': 'Sugo 成员已在当前公会内' if verified else '未在当前 Sugo 公会成员列表中找到该 ID',
            'guild_name': normalized_guild,
            'sogo_id': normalized_sogo_id,
            'member': found_anchor or None,
            'total_count': total_count,
            'scanned_count': scanned_count,
            'page_count': page_count,
            'checked_at': utc_now(),
        }

    def submit_timo_intake_item(self, *, payload: OpsTimoIntakeSubmitRequest, user: Dict[str, Any]) -> Dict[str, Any]:
        parsed = self.parse_timo_intake_text(
            text=payload.source_text or '',
            fields={'mobile': payload.mobile, 'timo_id': payload.timo_id, 'group_name': payload.group_name},
        )
        fields = parsed.get('fields') or {}
        role = str((user or {}).get('role') or '').strip().lower()
        fallback_executor = self._find_fallback_timo_guild_executor_config() if not str(payload.guild_name or '').strip() else {}
        guild_name = timo_guild_storage_name(
            payload.guild_name or fallback_executor.get('guild_name') or self._default_timo_intake_guild_name()
        ) or self._default_timo_intake_guild_name()
        timo_id = self._normalize_timo_id(fields.get('timo_id'))
        group_name = str(fields.get('group_name') or '').strip() or OTHER_CHANNEL_REGISTRATION_GROUP
        app_name = str(payload.app_name or os.getenv('TIMO_CRM_APP_NAME') or 'Timo').strip() or 'Timo'
        source_channel = str(payload.source_channel or 'ops_timo_intake').strip() or 'ops_timo_intake'
        mobile = self._normalize_timo_mobile(fields.get('mobile'))
        if not timo_id:
            raise HTTPException(status_code=400, detail='timo_id is required.')
        format_errors = []
        if mobile and not self._timo_mobile_format_valid(mobile):
            format_errors.append('手机号')
        if not self._timo_id_format_valid(timo_id):
            format_errors.append('Timo ID')
        if format_errors:
            if len(format_errors) == 2:
                message = '手机号和 Timo ID 格式有误，请修改后再提交。手机号需带 +国家区号，Timo ID 需为 12 位数字。'
            elif format_errors[0] == '手机号':
                message = '手机号格式有误，请填写带 +国家区号的手机号。'
            else:
                message = 'Timo ID 格式有误，请填写 12 位数字。'
            raise HTTPException(status_code=400, detail=message)
        if not guild_name and role not in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_INTERNAL}:
            raise HTTPException(status_code=400, detail='guild_name is required.')
        if guild_name and not self._ops_intake_user_can_access_guild(user, guild_name):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        now = utc_now()
        row = {
            'item_id': create_id('timo'),
            'guild_name': guild_name,
            'mobile': mobile,
            'timo_id': timo_id,
            'group_name': group_name,
            'app_name': app_name,
            'source_text': str(payload.source_text or '').strip(),
            'source_channel': source_channel,
            'source': source_channel,
            'external_user_id': None,
            'external_session_id': None,
            'external_message_id': None,
            'external_customer_service_id': None,
            'external_customer_service_name': None,
            'external_payload': '{}',
            'profile_name': str(payload.profile_name or '').strip(),
            'submitted_by_user_id': str(user.get('user_id') or '').strip(),
            'submitted_by_username': str(user.get('username') or user.get('display_name') or user.get('user_id') or '').strip(),
            'system_status': 'crm_pending',
            'feedback_status': 'not_feedbackable',
            'feedback_done_at': None,
            'feedback_done_by': None,
            'template_copied_at': None,
            'template_copied_by': None,
            'timo_verify_status': 'not_checked',
            'timo_result_code': '',
            'timo_result_reason': '',
            'timo_result_snapshot': '{}',
            'timo_verified_at': None,
            'crm_sync_status': 'not_started',
            'crm_result_code': '',
            'crm_result_reason': '',
            'crm_payload': '{}',
            'crm_response': '{}',
            'crm_synced_at': None,
            'created_at': now,
            'updated_at': now,
        }
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO ops_timo_intake_items (
                    item_id, guild_name, mobile, timo_id, group_name, app_name, source_text, source_channel, source,
                    external_user_id, external_session_id, external_message_id,
                    external_customer_service_id, external_customer_service_name, external_payload,
                    profile_name, submitted_by_user_id, submitted_by_username, system_status,
                    feedback_status, feedback_done_at, feedback_done_by, template_copied_at, template_copied_by,
                    timo_verify_status,
                    timo_result_code, timo_result_reason, timo_result_snapshot, timo_verified_at,
                    crm_sync_status, crm_result_code, crm_result_reason, crm_payload, crm_response,
                    crm_synced_at, created_at, updated_at
                ) VALUES (
                    :item_id, :guild_name, :mobile, :timo_id, :group_name, :app_name, :source_text, :source_channel, :source,
                    :external_user_id, :external_session_id, :external_message_id,
                    :external_customer_service_id, :external_customer_service_name, :external_payload,
                    :profile_name, :submitted_by_user_id, :submitted_by_username, :system_status,
                    :feedback_status, :feedback_done_at, :feedback_done_by, :template_copied_at, :template_copied_by,
                    :timo_verify_status,
                    :timo_result_code, :timo_result_reason, :timo_result_snapshot, :timo_verified_at,
                    :crm_sync_status, :crm_result_code, :crm_result_reason, :crm_payload, :crm_response,
                    :crm_synced_at, :created_at, :updated_at
                )
                """,
                row,
            )
            conn.commit()
        public_row = self._public_timo_intake_row(row)
        result = {'ok': True, 'item': public_row, 'parse': parsed, 'auto_verified': False}
        if bool(payload.auto_verify):
            started_at = time.time()
            verified_result = self.verify_timo_intake_item(item_id=str(public_row.get('item_id') or ''))
            result.update(verified_result)
            result['parse'] = parsed
            result['auto_verified'] = True
            result['auto_verify_elapsed_seconds'] = round(time.time() - started_at, 3)
        return result

    def _ops_timo_intake_user_can_access_item(self, user: Optional[Dict[str, Any]], item: Dict[str, Any]) -> bool:
        guild_name = str((item or {}).get('guild_name') or '').strip()
        role = str((user or {}).get('role') or '').strip().lower()
        if not guild_name:
            return role in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_INTERNAL} or not role
        return self._ops_intake_user_can_access_guild(user, guild_name)

    def list_timo_intake_items(
        self,
        *,
        page: int = 1,
        page_size: int = 30,
        status: Optional[str] = None,
        q: Optional[str] = None,
        guild_name: Optional[str] = None,
        date: Optional[str] = None,
        submitted_by: Optional[str] = None,
        user: Optional[Dict[str, Any]] = None,
        view: str = 'all',
    ) -> Dict[str, Any]:
        safe_page = max(1, int(page or 1))
        safe_page_size = max(1, min(100, int(page_size or 30)))
        where = []
        params: list[Any] = []
        requested_guild = timo_guild_storage_name(guild_name)
        requested_date = str(date or '').strip()
        requested_submitter = str(submitted_by or '').strip()
        role = str((user or {}).get('role') or '').strip().lower()
        visible_guilds: List[str] = []
        if requested_guild:
            if not self._ops_intake_user_can_access_guild(user, requested_guild):
                raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
            where.append('guild_name = ?')
            params.append(requested_guild)
        elif ops_role_is_business(role):
            visible_guilds = [str(row.get('guild_name') or '').strip() for row in self.list_timo_intake_guilds(user=user).get('rows', []) if str(row.get('guild_name') or '').strip()]
            if not visible_guilds:
                return {
                    'ok': True,
                    'rows': [],
                    'total': 0,
                    'page': safe_page,
                    'page_size': safe_page_size,
                    'total_pages': 0,
                    'summary': {'history_count': 0, 'view': str(view or 'all').strip().lower() or 'all'},
                    'filter_options': {'guild_names': []},
                }
            where.append('guild_name IN (%s)' % ','.join('?' for _ in visible_guilds))
            params.extend(visible_guilds)
        else:
            visible_guilds = [str(row.get('guild_name') or '').strip() for row in self.list_timo_intake_guilds(user=user).get('rows', []) if str(row.get('guild_name') or '').strip()]
        normalized_status = str(status or '').strip()
        if normalized_status and normalized_status != 'all':
            if normalized_status in {'feedback_done', 'cleared', 'pending_feedback', 'not_feedbackable'}:
                where.append('feedback_status = ?')
                params.append(normalized_status)
            else:
                where.append('system_status = ?')
                params.append(normalized_status)
        normalized_view = str(view or 'all').strip().lower()
        if normalized_view == 'current':
            where.append("COALESCE(feedback_status, '') NOT IN ('feedback_done', 'cleared')")
        if requested_date:
            where.append("date(created_at, '+8 hours') = ?")
            params.append(requested_date)
        if requested_submitter:
            like_submitter = f'%{requested_submitter}%'
            where.append('(submitted_by_user_id LIKE ? OR submitted_by_username LIKE ? OR external_customer_service_id LIKE ? OR external_customer_service_name LIKE ?)')
            params.extend([like_submitter, like_submitter, like_submitter, like_submitter])
        keyword = str(q or '').strip()
        if keyword:
            like = f'%{keyword}%'
            where.append('(item_id LIKE ? OR timo_id LIKE ? OR mobile LIKE ? OR group_name LIKE ? OR app_name LIKE ? OR submitted_by_username LIKE ? OR external_customer_service_id LIKE ? OR external_customer_service_name LIKE ? OR timo_result_reason LIKE ? OR crm_result_reason LIKE ?)')
            params.extend([like, like, like, like, like, like, like, like, like, like])
        where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
        offset = (safe_page - 1) * safe_page_size
        with self.db.connect() as conn:
            total = conn.execute(f'SELECT COUNT(*) AS n FROM ops_timo_intake_items{where_sql}', tuple(params)).fetchone()['n']
            summary_row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS history_count,
                    SUM(CASE WHEN system_status IN ('pending_verification', 'crm_pending') THEN 1 ELSE 0 END) AS pending_count,
                    SUM(CASE WHEN system_status IN ('verified_success', 'crm_success') THEN 1 ELSE 0 END) AS success_count,
                    SUM(CASE WHEN system_status = 'verify_failed' THEN 1 ELSE 0 END) AS failed_count,
                    SUM(CASE WHEN system_status = 'crm_failed' THEN 1 ELSE 0 END) AS crm_failed_count,
                    SUM(CASE WHEN feedback_status = 'feedback_done' THEN 1 ELSE 0 END) AS feedback_done_count,
                    SUM(CASE WHEN feedback_status = 'cleared' THEN 1 ELSE 0 END) AS cleared_count
                FROM ops_timo_intake_items{where_sql}
                """,
                tuple(params),
            ).fetchone()
            rows = conn.execute(
                f"SELECT * FROM ops_timo_intake_items{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                tuple(params + [safe_page_size, offset]),
            ).fetchall()
        summary = dict(summary_row or {})
        summary = {key: int(value or 0) for key, value in summary.items()}
        summary['view'] = normalized_view
        return {
            'ok': True,
            'rows': [self._public_timo_intake_row(dict(row)) for row in rows],
            'total': int(total or 0),
            'page': safe_page,
            'page_size': safe_page_size,
            'total_pages': math.ceil(int(total or 0) / safe_page_size) if safe_page_size else 0,
            'summary': summary,
            'filter_options': {
                'guild_names': visible_guilds,
            },
        }


__all__ = ['ApprovalServiceMixin']
