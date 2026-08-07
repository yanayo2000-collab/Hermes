from __future__ import annotations

from app.main_shared import *


class WhatsAppServiceMixin:
    @classmethod
    def _binding_has_recent_baileys_operational_probe(
        cls,
        binding: Optional[Dict[str, Any]],
        probe: Optional[Dict[str, Any]] = None,
        *,
        max_age_seconds: float = 3600.0,
    ) -> bool:
        live_probe = dict(probe or {})
        if live_probe and cls._binding_probe_has_group_evidence(live_probe) and not live_probe.get('error'):
            return True
        stored = dict(binding or {})
        status = str(stored.get('last_probe_status') or '').strip()
        if status and status not in {'resolved', 'live_probe_ready', 'mapped_live_probe_ready', 'inferred_live_probe_ready'}:
            return False
        if not cls._binding_probe_has_group_evidence(cls._stored_binding_probe_payload(stored)):
            return False
        return cls._iso_timestamp_within(stored.get('last_probe_at'), max_age_seconds=max_age_seconds)

    def _recent_group_atmosphere_baileys_success(self, account_key: str, *, max_age_seconds: float = 3600.0) -> bool:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            return False
        try:
            with self.db.connect() as conn:
                row = conn.execute(
                    """
                    SELECT created_at FROM whatsapp_group_atmosphere_logs
                    WHERE account_key = ?
                      AND direction = 'outbound'
                      AND delivery_state IN ('api_accepted', 'runtime_observed', 'readback_missing', 'readback_ambiguous', 'frontend_verified')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (normalized_key,),
                ).fetchone()
        except Exception:
            return False
        if not row:
            return False
        return self._iso_timestamp_within(str(row['created_at'] or ''), max_age_seconds=max_age_seconds)

    def _approval_membership_verifier_state(
        self,
        *,
        responsible_type: str,
        production_ops: Optional[Dict[str, Any]] = None,
        official_bridge: Optional[Dict[str, Any]] = None,
        runtime_state: Optional[Dict[str, Any]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        account_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if str(responsible_type or '').strip() != 'registration_group':
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'not_supported',
                'detail': '官方群当前仅接入 bridge 健康度与请求队列摘要，真实群成员/管理员权限校验执行器尚未接入。',
                'source': 'official_group_bridge_only',
                'probe': {},
            }
        executor_health = self.registration_group_approval_executor_health()
        supports = {str(item).strip() for item in (executor_health.get('supports') or []) if str(item).strip()}
        runtime_state = dict(runtime_state or {})
        session_state = dict(session_state or {})
        runtime_gate_context = bool(
            runtime_state.get('base_url')
            or (
                session_state.get('authenticated')
                and (
                    session_state.get('client_id')
                    or session_state.get('auth_path')
                )
            )
        )
        probe = self._extract_live_group_probe(
            production_ops,
            runtime_state=runtime_state if runtime_gate_context else None,
            session_state=session_state if runtime_gate_context else None,
            account_key=account_key,
        )
        truth_state = dict(probe.get('truth_state') or {})
        runtime_gate_applicable = runtime_gate_context
        if runtime_gate_applicable:
            gated_verifier = self._membership_verifier_gate_from_truth_state(
                truth_state,
                probe=probe,
                source_fallback=truth_state.get('source') or probe.get('source'),
            )
            if gated_verifier:
                return gated_verifier
        has_live_probe = bool(probe.get('group_name') or probe.get('group_id') or probe.get('member_count') is not None)
        ready = bool(has_live_probe and ('strict_queue_and_member_verify' in supports or 'approve' in supports))
        if ready:
            group_label = str(probe.get('group_name') or probe.get('group_id') or '-').strip() or '-'
            detail = self._format_group_probe_ready_detail(
                scope_text='注册群',
                probe_label=group_label,
                pending_count=probe.get('pending_count'),
                member_count=probe.get('member_count'),
                executor_text='共享执行器',
            )
            status = 'live_probe_ready'
            requires_manual_seed = False
        elif executor_health.get('configured'):
            detail = '注册群审批执行器已配置，但当前未拿到可用的实时群状态探针结果；暂不能判定真实成员/管理员权限。'
            status = 'probe_unavailable'
            requires_manual_seed = True
        else:
            detail = '注册群审批执行器未配置，暂不能做真实群成员/管理员权限校验。'
            status = 'executor_unconfigured'
            requires_manual_seed = True
        return {
            'ready': ready,
            'requires_manual_seed': requires_manual_seed,
            'status': status,
            'detail': detail,
            'source': probe.get('source'),
            'probe': probe,
            'truth_state': truth_state,
        }

    @staticmethod
    def _format_group_probe_ready_detail(
        *,
        scope_text: str,
        probe_label: str,
        pending_count: Any,
        member_count: Any,
        executor_text: str,
        suffix: str = '',
    ) -> str:
        pending_text = pending_count if pending_count is not None else '-'
        base = f'已接探针：待审批 {pending_text} 人。已有管理员权限。'
        suffix_text = str(suffix or '').strip()
        return f'{base}{suffix_text and "；" + suffix_text}'

    @staticmethod
    def _binding_membership_verifier_state(
        binding: Dict[str, Any],
        account_verifier: Dict[str, Any],
        *,
        responsible_type: str,
        production_ops: Optional[Dict[str, Any]] = None,
        live_probe: Optional[Dict[str, Any]] = None,
        runtime_state: Optional[Dict[str, Any]] = None,
        session_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if str(responsible_type or '').strip() != 'registration_group':
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'not_supported',
                'detail': '官方群绑定当前不支持逐群真实成员/管理员权限校验。',
            }
        if binding.get('enabled') is False:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'monitor_disabled',
                'detail': '不监控',
            }
        raw_runtime_state = dict(runtime_state or {})
        raw_session_state = dict(session_state or {})
        runtime_gate_context = bool(
            raw_runtime_state.get('base_url')
            or (
                raw_session_state.get('authenticated')
                and (
                    raw_session_state.get('client_id')
                    or raw_session_state.get('auth_path')
                )
            )
        )
        effective_runtime_state = raw_runtime_state if runtime_gate_context else {}
        effective_session_state = raw_session_state if runtime_gate_context else {}
        binding_probe = dict(live_probe or {})
        binding_probe_has_authoritative_data = bool(
            binding_probe.get('group_name')
            or binding_probe.get('group_id')
            or binding_probe.get('pending_count') is not None
            or binding_probe.get('member_count') is not None
        )
        inherited_truth_state = dict(account_verifier.get('truth_state') or {})
        inherited_truth_status = str(inherited_truth_state.get('status') or '').strip()
        runtime = dict((production_ops or {}).get('runtime') or {})
        status = dict(runtime.get('status') or {})
        monitor_target = dict(status.get('monitor_target') or {}) if isinstance(status.get('monitor_target'), dict) else {}
        monitor_registration_group = str(monitor_target.get('registration_group') or '').strip()
        monitor_group_id = str(((status.get('decision_group_state') or {}).get('payload') or {}).get('group_id') or '').strip() if isinstance(status.get('decision_group_state'), dict) else ''
        monitor_binding_link = str(monitor_target.get('binding_link') or '').strip()
        monitor_group_name = str(monitor_target.get('group_name') or monitor_target.get('binding_group_name') or '').strip()
        binding_group = str(binding.get('registration_group') or '').strip()
        binding_group_id = str(binding.get('group_id') or '').strip()
        binding_link = str(binding.get('link') or '').strip()
        binding_group_name = str(binding.get('group_name') or '').strip()
        target_matches_monitor = any([
            bool(binding_group and monitor_registration_group and binding_group == monitor_registration_group),
            bool(binding_group_id and monitor_group_id and binding_group_id == monitor_group_id),
            bool(binding_link and monitor_binding_link and binding_link == monitor_binding_link),
            bool(binding_group_name and monitor_group_name and binding_group_name == monitor_group_name),
        ])
        if target_matches_monitor and not binding_probe_has_authoritative_data:
            inherited_truth_gate = Service._membership_verifier_gate_from_truth_state(
                inherited_truth_state,
                probe=binding_probe,
                source_fallback=inherited_truth_state.get('source') or binding_probe.get('source') or binding_probe.get('source_base_url'),
            )
            if inherited_truth_gate:
                return inherited_truth_gate
        authoritative_probe = dict(binding_probe)
        authoritative_truth_state = inherited_truth_state if target_matches_monitor and inherited_truth_status else {}
        if authoritative_truth_state:
            authoritative_payload = dict(authoritative_truth_state.get('payload') or {})
            if authoritative_payload:
                authoritative_probe = {
                    **authoritative_payload,
                    'source': authoritative_truth_state.get('source') or authoritative_payload.get('source') or binding_probe.get('source') or binding_probe.get('source_base_url'),
                    'source_base_url': binding_probe.get('source_base_url'),
                    'probe_target': binding_probe.get('probe_target') or binding_group or binding_group_id,
                    'zero_pending_unverified': bool(authoritative_truth_state.get('zero_pending_unverified')),
                    'zero_pending_unverified_reason': authoritative_truth_state.get('zero_pending_unverified_reason'),
                    'zero_pending_verified_by': authoritative_truth_state.get('zero_pending_verified_by'),
                    'empty_queue_visible': bool(authoritative_truth_state.get('empty_queue_visible')),
                    'has_pending_section': bool(authoritative_truth_state.get('has_pending_section')),
                    'has_pending_request_row': bool(authoritative_truth_state.get('has_pending_request_row')),
                }
        binding_truth_state = Service._truth_state_from_probe_payload(
            authoritative_probe,
            source=authoritative_probe.get('source') or authoritative_probe.get('source_base_url') or 'binding_probe',
            runtime_state=effective_runtime_state,
            session_state=effective_session_state,
        )
        permission_status = str(
            authoritative_probe.get('permission_status')
            or authoritative_probe.get('permissionStatus')
            or ''
        ).strip().lower()
        if permission_status == 'not_group_member':
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'not_group_member',
                'detail': '当前审批账号已不在目标群，无法读取或处理待审批成员。请让群管理员重新添加该账号并授予管理员权限。',
                'source': authoritative_probe.get('source') or authoritative_probe.get('source_base_url'),
                'probe': authoritative_probe,
                'truth_state': binding_truth_state,
            }
        if permission_status == 'not_group_admin':
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'not_group_admin',
                'detail': '当前审批账号仍在目标群，但已不是群管理员，无法读取或处理待审批成员。请重新授予管理员权限。',
                'source': authoritative_probe.get('source') or authoritative_probe.get('source_base_url'),
                'probe': authoritative_probe,
                'truth_state': binding_truth_state,
            }
        if authoritative_probe.get('runtime_identity_match') is False:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'session_account_mismatch',
                'detail': '当前 WhatsApp runtime 登录账号与配置审批账号不一致，已停止使用该探针结果。',
                'source': authoritative_probe.get('source') or authoritative_probe.get('source_base_url'),
                'probe': authoritative_probe,
                'truth_state': binding_truth_state,
            }
        participants_status = str(authoritative_probe.get('participants_load_status') or '').strip()
        try:
            participants_count = int(authoritative_probe.get('participants_count_raw') if authoritative_probe.get('participants_count_raw') is not None else authoritative_probe.get('member_count') or 0)
        except (TypeError, ValueError):
            participants_count = 0
        if authoritative_probe.get('self_participant_found') is False:
            if participants_status == 'complete' and participants_count > 0:
                return {
                    'ready': False,
                    'requires_manual_seed': True,
                    'status': 'not_group_member',
                    'detail': '当前审批账号未出现在已完整读取的目标群成员列表中，无法读取待审批列表。请核对登录账号和目标群。',
                    'source': authoritative_probe.get('source') or authoritative_probe.get('source_base_url'),
                    'probe': authoritative_probe,
                    'truth_state': binding_truth_state,
                }
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'membership_unconfirmed',
                'detail': '已解析到目标群，但未成功读取完整成员列表，暂不能确认审批账号是否在群/是否管理员。',
                'source': authoritative_probe.get('source') or authoritative_probe.get('source_base_url'),
                'probe': authoritative_probe,
                'truth_state': binding_truth_state,
            }
        if authoritative_probe.get('self_participant_found') is True and authoritative_probe.get('self_is_admin') is False and authoritative_probe.get('can_manage_membership_requests') is not True:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'not_group_admin',
                'detail': '当前审批账号仍在目标群，但已不是群管理员，无法读取或处理待审批成员。请重新授予管理员权限。',
                'source': authoritative_probe.get('source') or authoritative_probe.get('source_base_url'),
                'probe': authoritative_probe,
                'truth_state': binding_truth_state,
            }
        if authoritative_probe.get('self_participant_found') is True and authoritative_probe.get('self_is_admin') is None:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'admin_unconfirmed',
                'detail': '已识别到审批账号在目标群内，但管理员身份未确认；请检查账号是否为群管理员，或等待成员列表加载完成。',
                'source': authoritative_probe.get('source') or authoritative_probe.get('source_base_url'),
                'probe': authoritative_probe,
                'truth_state': binding_truth_state,
            }
        if authoritative_probe.get('self_participant_found') is True and authoritative_probe.get('self_is_admin') is True and authoritative_probe.get('approval_action_visible') is True:
            authoritative_probe['can_manage_membership_requests'] = True
        elif authoritative_probe.get('can_manage_membership_requests') is False:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'admin_unconfirmed',
                'detail': '管理员权限未确认：已识别账号在群内，但审批入口/成员管理能力未被探针确认。',
                'source': authoritative_probe.get('source') or authoritative_probe.get('source_base_url'),
                'probe': authoritative_probe,
                'truth_state': binding_truth_state,
            }
        binding_truth_gate = Service._membership_verifier_gate_from_truth_state(
            binding_truth_state,
            probe=authoritative_probe,
            source_fallback=authoritative_probe.get('source') or authoritative_probe.get('source_base_url') or 'binding_probe',
        )
        if binding_truth_gate:
            return binding_truth_gate
        binding_probe_error = str(binding_probe.get('error') or '').strip()
        has_binding_probe = bool(
            binding_probe.get('group_name')
            or binding_probe.get('group_id')
            or binding_probe.get('pending_count') is not None
            or binding_probe.get('member_count') is not None
        )
        if authoritative_probe:
            probe = authoritative_probe
        elif has_binding_probe:
            probe = binding_probe
        else:
            probe = dict(account_verifier.get('probe') or {})
            if not account_verifier.get('ready'):
                if binding_probe_error:
                    return {
                        'ready': False,
                        'requires_manual_seed': True,
                        'status': 'probe_unavailable',
                        'detail': f'探针异常：{binding_probe_error}',
                        'source': binding_probe.get('source') or binding_probe.get('source_base_url'),
                        'probe': binding_probe,
                        'truth_state': binding_truth_state,
                    }
                return {
                    'ready': False,
                    'requires_manual_seed': True,
                    'status': account_verifier.get('status') or 'probe_unavailable',
                    'detail': account_verifier.get('detail') or '当前未拿到共享执行器实时探针结果。',
                    'truth_state': dict(account_verifier.get('truth_state') or {}),
                }
        runtime = dict((production_ops or {}).get('runtime') or {})
        status = dict(runtime.get('status') or {})
        monitor_target = dict(status.get('monitor_target') or {}) if isinstance(status.get('monitor_target'), dict) else {}
        monitor_registration_group = str(monitor_target.get('registration_group') or '').strip()
        monitor_group_id = str(((status.get('decision_group_state') or {}).get('payload') or {}).get('group_id') or '').strip() if isinstance(status.get('decision_group_state'), dict) else ''
        monitor_binding_link = str(monitor_target.get('binding_link') or '').strip()
        monitor_group_name = str(monitor_target.get('group_name') or monitor_target.get('binding_group_name') or '').strip()
        binding_group = str(binding.get('registration_group') or '').strip()
        binding_group_id = str(binding.get('group_id') or '').strip()
        binding_link = str(binding.get('link') or '').strip()
        probe_group = str(probe.get('group_name') or '').strip()
        probe_group_id = str(probe.get('group_id') or '').strip()
        if binding_probe_error and not (
            probe_group
            or probe_group_id
            or probe.get('pending_count') is not None
            or probe.get('member_count') is not None
        ):
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'probe_unavailable',
                'detail': f'探针异常：{binding_probe_error}',
                'source': binding_probe.get('source') or binding_probe.get('source_base_url'),
                'probe': binding_probe,
                'truth_state': binding_truth_state,
            }
        if binding_group or binding_group_id:
            binding_name_matches_probe = bool(binding_group_name and probe_group and binding_group_name == probe_group)
            group_ok = (not binding_group) or (binding_group and (binding_group == probe_group or binding_group == probe_group_id)) or binding_name_matches_probe
            group_id_ok = (not binding_group_id) or (binding_group_id and binding_group_id == probe_group_id) or (binding_name_matches_probe and not probe_group_id)
            if group_ok and group_id_ok:
                probe_has_evidence = bool(
                    probe.get('pending_count') is not None
                    or probe.get('member_count') is not None
                    or probe.get('participants_load_status')
                    or probe.get('self_participant_found') is not None
                    or probe.get('approval_action_visible') is not None
                )
                if not probe_has_evidence:
                    return {
                        'ready': False,
                        'requires_manual_seed': True,
                        'status': 'probe_unavailable',
                        'detail': '已匹配目标群配置，但实时探针未返回成员/审批证据，暂不能判定真实群状态。',
                        'source': probe.get('source') or probe.get('source_base_url'),
                        'probe': probe,
                        'truth_state': binding_truth_state,
                    }
                probe_label = str(probe_group or binding_group_name or binding_group or binding_group_id or probe_group_id or '-').strip() or '-'
                suffix_parts = []
                current_group_name = str(probe_group or binding.get('group_name') or monitor_group_name or '').strip()
                if current_group_name and not (binding_group and current_group_name == binding_group):
                    suffix_parts.append(f'当前群：{current_group_name}')
                elif not current_group_name and (binding_group_id or probe_group_id):
                    suffix_parts.append(f'当前群ID：{binding_group_id or probe_group_id}')
                return {
                    'ready': True,
                    'requires_manual_seed': False,
                    'status': 'mapped_live_probe_ready',
                    'detail': Service._format_group_probe_ready_detail(
                        scope_text='注册群',
                        probe_label=probe_label,
                        pending_count=probe.get('pending_count'),
                        member_count=probe.get('member_count'),
                        executor_text='共享执行器',
                        suffix='；'.join(suffix_parts),
                    ),
                    'source': probe.get('source') or probe.get('source_base_url'),
                    'probe': probe,
                    'truth_state': binding_truth_state,
                }
            if not has_binding_probe:
                monitor_target_matches_binding = any([
                    bool(binding_link and monitor_binding_link and binding_link == monitor_binding_link),
                    bool(binding_group and monitor_registration_group and binding_group == monitor_registration_group),
                    bool(binding_group_id and monitor_group_id and binding_group_id == monitor_group_id),
                ])
                if monitor_binding_link or monitor_registration_group or monitor_group_id:
                    if not monitor_target_matches_binding:
                        target_label = monitor_group_name or monitor_binding_link or monitor_registration_group or monitor_group_id or '-'
                        return {
                            'ready': False,
                            'requires_manual_seed': True,
                            'status': 'other_binding_live_probe_active',
                            'detail': '待本群探针刷新',
                            'truth_state': binding_truth_state,
                        }
            mismatch = []
            if binding_group and binding_group != probe_group:
                mismatch.append(f'registration_group={binding_group} ≠ {probe_group or "-"}')
            if binding_group_id and binding_group_id != probe_group_id:
                mismatch.append(f'group_id={binding_group_id} ≠ {probe_group_id or "-"}')
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'mapping_mismatch',
                'detail': '绑定映射与当前真实探针不一致：' + '；'.join(mismatch),
                'source': probe.get('source') or probe.get('source_base_url'),
                'probe': probe,
                'truth_state': binding_truth_state,
            }
        if binding_probe_error and not has_binding_probe:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'probe_unavailable',
                'detail': f'探针异常：{binding_probe_error}',
                'source': binding_probe.get('source') or binding_probe.get('source_base_url'),
                'probe': binding_probe,
                'truth_state': binding_truth_state,
            }
        return {
            'ready': True,
            'requires_manual_seed': False,
            'status': 'inferred_live_probe_ready',
            'detail': '',
            'source': probe.get('source') or probe.get('source_base_url'),
            'probe': probe,
            'truth_state': binding_truth_state,
        }

    def _official_group_binding_membership_verifier_state(
        self,
        binding: Dict[str, Any],
        *,
        runtime_state: Dict[str, Any],
        session_state: Dict[str, Any],
        live_probe: Optional[Dict[str, Any]] = None,
        allow_live_probe: bool = True,
    ) -> Dict[str, Any]:
        if binding.get('enabled') is False:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'monitor_disabled',
                'detail': '不监控',
                'source': None,
                'probe': {},
            }
        base_url = str(runtime_state.get('base_url') or '').strip()
        if not bool(runtime_state.get('active')) or not base_url:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'runtime_unavailable',
                'detail': '未就绪',
                'source': None,
                'probe': {},
            }
        if not bool(session_state.get('login_verified')):
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'login_unready',
                'detail': '待登录',
                'source': None,
                'probe': {},
            }
        binding_target = (
            str(binding.get('group_id') or '').strip()
            or str(binding.get('link') or '').strip()
            or str(binding.get('registration_group') or '').strip()
            or str(binding.get('group_name') or '').strip()
        )
        if not binding_target:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'binding_target_missing',
                'detail': '缺目标',
                'source': None,
                'probe': {},
            }
        probe = dict(live_probe or {})
        if probe.get('error') and not (probe.get('group_name') or probe.get('group_id')):
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'probe_unavailable',
                'detail': f"探针异常：{probe.get('error')}",
                'source': 'official_group_runtime_group_state',
                'probe': {},
            }
        if not (probe.get('group_name') or probe.get('group_id')):
            if allow_live_probe:
                try:
                    probe = self._request_whatsapp_approval_group_state_with_retry(base_url, binding_target)
                except Exception as exc:
                    return {
                        'ready': False,
                        'requires_manual_seed': True,
                        'status': 'probe_unavailable',
                        'detail': f'探针异常：{exc}',
                        'source': 'official_group_runtime_group_state',
                        'probe': {},
                    }
            else:
                probe = self._stored_binding_probe_payload(binding)
        if not self._binding_probe_has_group_evidence(probe):
            if not allow_live_probe:
                return {
                    'ready': False,
                    'requires_manual_seed': True,
                    'status': 'probe_unavailable',
                    'detail': '轻量快照未含可用群状态证据，等待手动刷新或后台自愈补齐。',
                    'source': 'official_group_snapshot',
                    'probe': {},
                }
            probe = {}
        probe_source = str(probe.get('source') or '').strip() or 'official_group_runtime_group_state'
        probe_payload = {
            'source': probe_source,
            'group_name': str(probe.get('group_name') or '').strip(),
            'group_id': str(probe.get('group_id') or '').strip(),
            'pending_count': probe.get('pending_count'),
            'member_count': probe.get('member_count'),
            'requester_ids': list(probe.get('requester_ids') or []),
            'requesters': list(probe.get('requesters') or []),
            'zero_pending_unverified': bool(probe.get('zero_pending_unverified')),
            'zero_pending_unverified_reason': probe.get('zero_pending_unverified_reason'),
            'zero_pending_verified_by': probe.get('zero_pending_verified_by'),
            'review_surface_ready': probe.get('review_surface_ready'),
            'empty_queue_visible': bool(probe.get('empty_queue_visible')),
            'has_pending_section': bool(probe.get('has_pending_section')),
            'has_pending_request_row': bool(probe.get('has_pending_request_row')),
            'zero_pending_recheck_attempted': bool(probe.get('zero_pending_recheck_attempted')),
            'zero_pending_recheck_resolved': bool(probe.get('zero_pending_recheck_resolved')),
            'zero_pending_recheck_count': probe.get('zero_pending_recheck_count'),
            'participants_load_status': probe.get('participants_load_status'),
            'participants_count_raw': probe.get('participants_count_raw'),
            'participants_count': probe.get('participants_count'),
            'self_participant_found': probe.get('self_participant_found'),
            'self_is_admin': probe.get('self_is_admin'),
            'can_manage_membership_requests': probe.get('can_manage_membership_requests'),
            'approval_action_visible': probe.get('approval_action_visible'),
        }
        binding_monitor_target = {
            'group_name': str(binding.get('group_name') or probe_payload.get('group_name') or '').strip(),
            'group_id': str(binding.get('group_id') or probe_payload.get('group_id') or '').strip(),
        }
        truth_state = self._truth_state_from_probe_payload(
            probe_payload,
            source=probe_source,
            runtime_state=runtime_state,
            session_state=session_state,
            monitor_target=binding_monitor_target,
        )
        if (
            probe_payload.get('self_participant_found') is True
            and (
                probe_payload.get('self_is_admin') is False
                or probe_payload.get('can_manage_membership_requests') is False
            )
        ):
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'not_group_admin',
                'detail': '无管理员权限：当前审批账号在目标群内，但不是群管理员或无法管理入群请求。',
                'source': probe_source,
                'probe': probe_payload,
                'truth_state': truth_state,
            }
        truth_gate = self._membership_verifier_gate_from_truth_state(
            truth_state,
            probe=probe_payload,
            source_fallback=probe_source,
            include_unconfirmed_probe_unavailable=True,
            probe_unavailable_detail='官方群实时探针结果证据不足，暂不能判定真实群状态。',
        )
        if truth_gate:
            return truth_gate
        live_group_name = str(probe_payload.get('group_name') or '').strip()
        configured_group_name = str(binding.get('group_name') or '').strip()
        detail_suffix = ''
        if configured_group_name and live_group_name and configured_group_name != live_group_name:
            detail_suffix = f'当前配置名为 {configured_group_name}，实时群名为 {live_group_name}。'
        detail = self._format_group_probe_ready_detail(
            scope_text='官方群',
            probe_label=live_group_name or configured_group_name or binding_target,
            pending_count=probe_payload.get('pending_count'),
            member_count=probe_payload.get('member_count'),
            executor_text='dedicated runtime',
            suffix=detail_suffix,
        )
        return {
            'ready': True,
            'requires_manual_seed': False,
            'status': 'live_probe_ready' if allow_live_probe else 'stored_probe_ready',
            'detail': detail,
            'source': truth_state.get('source') or probe_source,
            'probe': probe_payload,
            'truth_state': truth_state,
        }

    @staticmethod
    def _official_group_account_membership_verifier(
        binding_verifiers: List[Dict[str, Any]],
        *,
        enabled_binding_count: int,
    ) -> Dict[str, Any]:
        monitored = [item for item in binding_verifiers if item.get('status') != 'monitor_disabled']
        if enabled_binding_count <= 0:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'monitor_disabled',
                'detail': '当前未开启任何官方群绑定监控。',
                'source': None,
                'probe': {},
                'binding_count': 0,
            }
        if not monitored:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'probe_unavailable',
                'detail': '当前没有可用于逐群真实校验的已开启群绑定。',
                'source': None,
                'probe': {},
                'binding_count': 0,
            }
        ready_count = sum(1 for item in monitored if item.get('ready'))
        if ready_count == len(monitored):
            first_probe = dict(monitored[0].get('probe') or {}) if monitored else {}
            return {
                'ready': True,
                'requires_manual_seed': False,
                'status': 'live_probe_ready',
                'detail': Service._format_group_probe_ready_detail(
                    scope_text='官方群',
                    probe_label=str(first_probe.get('group_name') or first_probe.get('group_id') or '-').strip() or '-',
                    pending_count=first_probe.get('pending_count'),
                    member_count=first_probe.get('member_count'),
                    executor_text='dedicated runtime',
                ),
                'source': 'official_group_runtime_group_state',
                'probe': first_probe,
                'binding_count': len(monitored),
            }
        first_failed = next((item for item in monitored if not item.get('ready')), monitored[0])
        return {
            'ready': False,
            'requires_manual_seed': True,
            'status': first_failed.get('status') or 'probe_unavailable',
            'detail': f'当前仅有 {ready_count}/{len(monitored)} 条官方群绑定完成真实成员/管理员权限校验；{first_failed.get("detail") or "仍有绑定未拿到实时探针结果。"}',
            'source': first_failed.get('source'),
            'probe': dict(first_failed.get('probe') or {}),
            'binding_count': len(monitored),
        }

    def _resolve_truth_binding_identity(self, account_key: str, binding: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(binding or {})
        if str(item.get('binding_id') or '').strip() or self._whatsapp_binding_runtime_group_id(item):
            return item
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            return item
        try:
            account_row = self._get_whatsapp_approval_account_row(normalized_key) or {}
        except Exception:
            account_row = {}
        if isinstance(account_row, dict):
            raw_bindings = account_row.get('group_links')
        else:
            raw_bindings = None
        if isinstance(raw_bindings, str):
            try:
                raw_bindings = json.loads(raw_bindings)
            except Exception:
                raw_bindings = []
        bindings = list(raw_bindings or []) if isinstance(raw_bindings, list) else []
        if not bindings:
            return item
        link = str(item.get('link') or '').strip()
        registration_group = str(item.get('registration_group') or '').strip()
        group_id = str(item.get('group_id') or '').strip()
        for candidate in bindings:
            candidate_item = dict(candidate or {}) if isinstance(candidate, dict) else {}
            if not candidate_item:
                continue
            if link and str(candidate_item.get('link') or '').strip() == link:
                return candidate_item
            if registration_group and str(candidate_item.get('registration_group') or '').strip() == registration_group:
                return candidate_item
            if group_id and str(candidate_item.get('group_id') or '').strip() == group_id:
                return candidate_item
        return item

    def _approval_binding_truth_lookup_keys(self, account_key: str, binding: Dict[str, Any]) -> List[str]:
        resolved = self._resolve_truth_binding_identity(account_key, binding)
        return self._approval_binding_truth_object_keys(account_key, resolved)

    @staticmethod
    def _parse_truth_snapshot_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        try:
            facts = json.loads(row['facts_json'] or '{}')
        except Exception:
            facts = {}
        try:
            source = json.loads(row['source_json'] or '{}')
        except Exception:
            source = {}
        if not isinstance(facts, dict):
            facts = {}
        if not isinstance(source, dict):
            source = {}
        trust_status = str(facts.get('trust_status') or row['truth_status'] or '').strip()
        pending_count = facts.get('trusted_pending_count')
        if pending_count is None:
            pending_count = facts.get('pending_count')
        try:
            pending_count = int(pending_count) if pending_count is not None else None
        except Exception:
            pending_count = None
        api_pending_count = facts.get('api_pending_count')
        try:
            api_pending_count = int(api_pending_count) if api_pending_count is not None else None
        except Exception:
            api_pending_count = None
        source_priority = source.get('source_priority', facts.get('source_priority'))
        try:
            source_priority = int(source_priority)
        except Exception:
            source_priority = 0
        runtime_generation = facts.get('runtime_generation', source.get('runtime_generation'))
        try:
            runtime_generation = int(runtime_generation) if runtime_generation is not None else None
        except Exception:
            runtime_generation = None
        return {
            'object_key': str(row['object_key'] or '').strip() if 'object_key' in row.keys() else '',
            'snapshot_type': str(row['snapshot_type'] or '').strip(),
            'truth_status': trust_status,
            'trust_status': trust_status,
            'confidence': str(row['confidence'] or '').strip() if 'confidence' in row.keys() else 'verified',
            'confidence_reason': str(row['confidence_reason'] or '').strip() if 'confidence_reason' in row.keys() else str(facts.get('reason_code') or '').strip(),
            'pending_count': pending_count,
            'trusted_pending_count': pending_count if trust_status.startswith('TRUSTED') else None,
            'api_pending_count': api_pending_count,
            'ui_pending_count': normalize_int_or_none(facts.get('ui_pending_count')),
            'requester_ids': [str(item).strip() for item in (facts.get('requester_ids') or []) if str(item).strip()] if isinstance(facts.get('requester_ids'), list) else [],
            'display_trusted': bool(facts.get('display_trusted')),
            'can_manual_approve': bool(facts.get('can_manual_approve') or facts.get('manual_approve_allowed')),
            'stale': bool(facts.get('stale')),
            'syncing': bool(facts.get('syncing')),
            'fingerprint': str(facts.get('fingerprint') or '').strip(),
            'fingerprint_quality': str(facts.get('fingerprint_quality') or '').strip(),
            'reason_code': str(facts.get('reason_code') or '').strip(),
            'runtime_generation': runtime_generation,
            'strong_empty_evidence': bool(facts.get('strong_empty_evidence')),
            'checked_at': str(row['checked_at'] or '').strip(),
            'source_ts': str(facts.get('source_ts') or row['checked_at'] or '').strip() or None,
            'verified_at': str(facts.get('verified_at') or facts.get('source_ts') or row['checked_at'] or '').strip() or None,
            'expires_at': str(row['expires_at'] or '').strip() or None,
            'updated_at': str(row['updated_at'] or row['checked_at'] or '').strip() if 'updated_at' in row.keys() else str(row['checked_at'] or '').strip(),
            'source_priority': source_priority,
            'invalidated_reason': str(facts.get('invalidated_reason') or '').strip() or None,
            'active_approval_run_id': str(facts.get('active_approval_run_id') or '').strip() or None,
            'last_approval_action_ts': str(facts.get('last_approval_action_ts') or '').strip() or None,
            'last_approved_count': normalize_int_or_none(facts.get('last_approved_count')),
            'verifying_since': str(facts.get('verifying_since') or '').strip() or None,
            'display_schema_version': int(facts.get('display_schema_version') or 1),
            'store_revision': normalize_int_or_none(facts.get('store_revision')) or 0,
            'facts': facts,
            'source': source,
        }

    @staticmethod
    def _approval_queue_snapshot_is_newer(candidate: Dict[str, Any], current: Dict[str, Any]) -> bool:
        return (
            str(candidate.get('updated_at') or ''),
            str(candidate.get('checked_at') or ''),
        ) > (
            str(current.get('updated_at') or ''),
            str(current.get('checked_at') or ''),
        )

    @classmethod
    def _approval_queue_snapshot_result_from_cache(
        cls,
        cache: Dict[str, Any],
        *,
        object_keys: List[str],
        primary_object_key: str,
    ) -> Optional[Dict[str, Optional[Dict[str, Any]]]]:
        covered_keys = cache.get('covered_object_keys')
        rows_by_key = cache.get('rows_by_key')
        if not isinstance(covered_keys, set) or not isinstance(rows_by_key, dict):
            return None
        lookup_keys = [str(item or '').strip() for item in object_keys if str(item or '').strip()]
        if not lookup_keys or not set(lookup_keys).issubset(covered_keys):
            return None

        result: Dict[str, Optional[Dict[str, Any]]] = {'current_truth': None, 'latest_probe': None}
        snapshot_map = {
            'approval_queue_current_truth': 'current_truth',
            'approval_queue_latest_probe': 'latest_probe',
        }
        for snapshot_type, result_key in snapshot_map.items():
            candidates: List[Dict[str, Any]] = []
            for object_key in lookup_keys:
                parsed = (rows_by_key.get(object_key) or {}).get(snapshot_type)
                if isinstance(parsed, dict):
                    candidates.append(parsed)
            if not candidates:
                continue
            primary_candidates = [item for item in candidates if str(item.get('object_key') or '').strip() == primary_object_key]
            selected_from = primary_candidates or candidates
            result[result_key] = max(
                selected_from,
                key=lambda item: (str(item.get('updated_at') or ''), str(item.get('checked_at') or '')),
            )
        return result

    def _build_approval_queue_snapshot_cache_for_account_rows(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        object_keys: List[str] = []
        for raw_row in rows or []:
            row = dict(raw_row or {})
            responsible_type = str(row.get('responsible_type') or '').strip()
            if responsible_type not in {'registration_group', 'official_group'}:
                continue
            account_key = str(row.get('account_key') or '').strip()
            if not account_key:
                continue
            try:
                raw_bindings = json.loads(row.get('group_links') or '[]')
            except Exception:
                raw_bindings = []
            if not isinstance(raw_bindings, list):
                raw_bindings = []
            bindings = [dict(item) if isinstance(item, dict) else {'link': str(item or '').strip()} for item in raw_bindings]
            for binding in _normalize_group_link_bindings(bindings, responsible_type=responsible_type):
                object_keys.extend(self._approval_binding_truth_lookup_keys(account_key, binding))

        unique_keys = list(dict.fromkeys(str(item or '').strip() for item in object_keys if str(item or '').strip()))
        rows_by_key: Dict[str, Dict[str, Dict[str, Any]]] = {key: {} for key in unique_keys}
        if not unique_keys:
            return {'covered_object_keys': set(), 'rows_by_key': rows_by_key}

        try:
            with self.db.connect() as conn:
                for offset in range(0, len(unique_keys), 400):
                    chunk = unique_keys[offset:offset + 400]
                    placeholders = ','.join('?' for _ in chunk)
                    snapshot_rows = conn.execute(
                        f"""
                        SELECT object_key, snapshot_type, truth_status, facts_json, source_json, checked_at, expires_at, updated_at
                        FROM mcn_truth_snapshots
                        WHERE object_type = 'registration_group_binding'
                          AND object_key IN ({placeholders})
                          AND snapshot_type IN ('approval_queue_current_truth', 'approval_queue_latest_probe')
                        """,
                        tuple(chunk),
                    ).fetchall()
                    for snapshot_row in snapshot_rows:
                        parsed = self._parse_truth_snapshot_row(snapshot_row)
                        if not parsed:
                            continue
                        object_key = str(parsed.get('object_key') or '').strip()
                        snapshot_type = str(parsed.get('snapshot_type') or '').strip()
                        if not object_key or snapshot_type not in {'approval_queue_current_truth', 'approval_queue_latest_probe'}:
                            continue
                        existing = rows_by_key.setdefault(object_key, {}).get(snapshot_type)
                        if existing is None or self._approval_queue_snapshot_is_newer(parsed, existing):
                            rows_by_key[object_key][snapshot_type] = parsed
        except Exception:
            return {'covered_object_keys': set(), 'rows_by_key': {}}
        return {'covered_object_keys': set(unique_keys), 'rows_by_key': rows_by_key}

    def _load_approval_binding_queue_snapshots_raw(self, account_key: str, binding: Dict[str, Any]) -> Dict[str, Optional[Dict[str, Any]]]:
        object_keys = self._approval_binding_truth_lookup_keys(account_key, binding)
        primary_object_key = self._approval_binding_truth_object_key(account_key, binding)
        if not primary_object_key and object_keys:
            primary_object_key = object_keys[0]
        if primary_object_key and primary_object_key not in object_keys:
            object_keys = [primary_object_key, *object_keys]
        if not primary_object_key:
            return {'current_truth': None, 'latest_probe': None}
        cache = getattr(self, '_approval_queue_snapshot_cache', None)
        if isinstance(cache, dict):
            cached_result = self._approval_queue_snapshot_result_from_cache(
                cache,
                object_keys=object_keys,
                primary_object_key=primary_object_key,
            )
            if cached_result is not None:
                return cached_result
        try:
            placeholders = ','.join('?' for _ in object_keys)
            with self.db.connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT object_key, snapshot_type, truth_status, facts_json, source_json, checked_at, expires_at, updated_at
                    FROM mcn_truth_snapshots
                    WHERE object_type = 'registration_group_binding'
                      AND object_key IN ({placeholders})
                      AND snapshot_type IN ('approval_queue_current_truth', 'approval_queue_latest_probe')
                    ORDER BY CASE WHEN object_key = ? THEN 0 ELSE 1 END, updated_at DESC, checked_at DESC
                    """,
                    (*object_keys, primary_object_key),
                ).fetchall()
        except Exception:
            return {'current_truth': None, 'latest_probe': None}
        result: Dict[str, Optional[Dict[str, Any]]] = {'current_truth': None, 'latest_probe': None}
        for row in rows:
            parsed = self._parse_truth_snapshot_row(row)
            if not parsed:
                continue
            snapshot_type = parsed.get('snapshot_type')
            is_primary_row = str(parsed.get('object_key') or '').strip() == primary_object_key
            if snapshot_type == 'approval_queue_current_truth':
                if result['current_truth'] is None or is_primary_row:
                    result['current_truth'] = parsed
            elif snapshot_type == 'approval_queue_latest_probe':
                if result['latest_probe'] is None or is_primary_row:
                    result['latest_probe'] = parsed
        return result

    def _approval_binding_pending_truth_match_keys(self, account_key: str, binding: Dict[str, Any], registration_group: str = '') -> set[str]:
        return build_pending_truth_match_keys(
            lookup_keys=self._approval_binding_truth_lookup_keys(account_key, binding),
            binding=binding,
            registration_group=registration_group,
        )

    @staticmethod
    def _normalize_approval_queue_pending_truth_history_entry(
        *,
        object_key: str,
        truth_status: str,
        confidence: str,
        confidence_reason: str,
        facts: Dict[str, Any],
        source: Dict[str, Any],
        checked_at: str,
        expires_at: Optional[str],
        updated_at: str,
    ) -> Dict[str, Any]:
        return normalize_pending_truth_history_entry(
            object_key=object_key,
            truth_status=truth_status,
            confidence=confidence,
            confidence_reason=confidence_reason,
            facts=facts,
            source=source,
            checked_at=checked_at,
            expires_at=expires_at,
            updated_at=updated_at,
        )

    def _load_approval_queue_pending_truth_history_entries(self, account_key: str, binding: Dict[str, Any], registration_group: str = '') -> List[Dict[str, Any]]:
        match_keys = self._approval_binding_pending_truth_match_keys(account_key, binding, registration_group)
        object_keys = self._approval_binding_truth_lookup_keys(account_key, binding)
        object_key = object_keys[0] if object_keys else ''
        if not match_keys or not object_key:
            return []
        history: List[Dict[str, Any]] = []
        try:
            with self.db.connect() as conn:
                snapshot_rows = conn.execute(
                    """
                    SELECT object_key, truth_status, confidence, confidence_reason,
                           facts_json, source_json, checked_at, expires_at, updated_at
                    FROM mcn_truth_snapshots
                    WHERE object_type = 'registration_group_binding'
                      AND snapshot_type = 'pending_truth'
                      AND (object_key = ? OR object_key LIKE ?)
                    ORDER BY updated_at DESC
                    LIMIT 20
                    """,
                    (object_key, f'{account_key}:%'),
                ).fetchall()
                event_rows = conn.execute(
                    """
                    SELECT object_key, event_type, status, evidence_level, payload_json, created_at
                    FROM mcn_event_ledger
                    WHERE object_type = 'registration_group_binding'
                      AND event_type IN ('approval_queue_probe_observed', 'approval_queue_pending_truth_observed')
                      AND (object_key = ? OR object_key LIKE ?)
                    ORDER BY created_at DESC
                    LIMIT 50
                    """,
                    (object_key, f'{account_key}:%'),
                ).fetchall()
        except Exception:
            return []
        for row in snapshot_rows:
            try:
                facts = json.loads(row['facts_json'] or '{}')
            except Exception:
                facts = {}
            try:
                source = json.loads(row['source_json'] or '{}')
            except Exception:
                source = {}
            entry = self._normalize_approval_queue_pending_truth_history_entry(
                object_key=str(row['object_key'] or '').strip(),
                truth_status=str(row['truth_status'] or '').strip(),
                confidence=str(row['confidence'] or '').strip(),
                confidence_reason=str(row['confidence_reason'] or '').strip(),
                facts=facts,
                source=source,
                checked_at=str(row['checked_at'] or '').strip(),
                expires_at=str(row['expires_at'] or '').strip() or None,
                updated_at=str(row['updated_at'] or row['checked_at'] or '').strip(),
            )
            row_match_keys = {
                entry['object_key'],
                str(entry['facts'].get('configured_registration_group') or '').strip(),
                str(entry['facts'].get('configured_group_id') or '').strip(),
                str(entry['facts'].get('actual_group_id') or '').strip(),
                str(entry['facts'].get('configured_link') or '').strip(),
            }
            row_match_keys.discard('')
            if row_match_keys & match_keys:
                history.append(entry)
        for row in event_rows:
            try:
                payload = json.loads(row['payload_json'] or '{}')
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            event_status = str(row['status'] or '').strip()
            if event_status not in {'TRUSTED_CONFIRMED_PENDING', 'TRUSTED_CONFIRMED_EMPTY', 'confirmed_pending', 'confirmed_empty'}:
                continue
            event_source = {
                'source': 'mcn_event_ledger',
                'event_type': str(row['event_type'] or '').strip(),
                'created_at': str(row['created_at'] or '').strip(),
            }
            event_source.update(dict(payload.get('source') or {}) if isinstance(payload.get('source'), dict) else {})
            event_checked_at = str(payload.get('source_ts') or payload.get('checked_at') or row['created_at'] or '').strip()
            entry = self._normalize_approval_queue_pending_truth_history_entry(
                object_key=str(row['object_key'] or '').strip(),
                truth_status=event_status,
                confidence='verified',
                confidence_reason=str(payload.get('reason_code') or '').strip(),
                facts=payload,
                source=event_source,
                checked_at=event_checked_at,
                expires_at=str(payload.get('expires_at') or '').strip() or None,
                updated_at=str(row['created_at'] or event_checked_at or '').strip(),
            )
            row_match_keys = {
                entry['object_key'],
                str(entry['facts'].get('configured_registration_group') or '').strip(),
                str(entry['facts'].get('configured_group_id') or '').strip(),
                str(entry['facts'].get('actual_group_id') or '').strip(),
                str(entry['facts'].get('configured_link') or '').strip(),
            }
            row_match_keys.discard('')
            if row_match_keys & match_keys:
                history.append(entry)
        history.sort(key=lambda item: str(item.get('updated_at') or item.get('checked_at') or ''), reverse=True)
        deduped: List[Dict[str, Any]] = []
        seen: set[tuple[str, str, str, int]] = set()
        for entry in history:
            facts = dict(entry.get('facts') or {}) if isinstance(entry.get('facts'), dict) else {}
            dedupe_key = (
                str(entry.get('truth_status') or '').strip(),
                str(entry.get('checked_at') or '').strip(),
                str(entry.get('object_key') or '').strip(),
                int(normalize_int_or_none(facts.get('pending_count')) or -1),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            deduped.append(entry)
        return deduped

    def _load_approval_queue_pending_truth_confirmed_pending_candidate(self, account_key: str, binding: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        registration_group = str(binding.get('registration_group') or binding.get('group_id') or '').strip()
        rows = self._load_approval_queue_pending_truth_history_entries(account_key, binding, registration_group)
        if not rows:
            return None
        return select_pending_truth_confirmed_pending_candidate(rows)

    def _load_approval_queue_pending_truth_confirmed_empty_candidate(self, account_key: str, binding: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        registration_group = str(binding.get('registration_group') or binding.get('group_id') or '').strip()
        rows = self._load_approval_queue_pending_truth_history_entries(account_key, binding, registration_group)
        if not rows:
            return None
        return select_pending_truth_confirmed_empty_candidate(rows)

    def _load_approval_binding_queue_snapshots(self, account_key: str, binding: Dict[str, Any]) -> Dict[str, Optional[Dict[str, Any]]]:
        return self._load_approval_binding_queue_snapshots_raw(account_key, binding)

    def _load_pending_truth_snapshot_group_state(self, *, account_key: str, binding: Dict[str, Any], registration_group: str) -> Optional[Dict[str, Any]]:
        rows = self._load_approval_queue_pending_truth_history_entries(account_key, binding, registration_group)
        if not rows:
            return None
        return pending_truth_snapshot_group_state(rows, registration_group=registration_group)

    @staticmethod
    def _truth_acquisition_row_is_unresolved(row: Dict[str, Any]) -> bool:
        item = dict(row or {})
        if bool(item.get('current_truth_written')):
            return False
        try:
            result = json.loads(str(item.get('result_json') or '{}'))
        except Exception:
            result = {}
        if not isinstance(result, dict):
            result = {}
        truth = result.get('approval_queue_truth') if isinstance(result.get('approval_queue_truth'), dict) else {}
        current_truth = truth.get('current_truth') if isinstance(truth.get('current_truth'), dict) else {}
        pending_candidates = (
            result.get('pending_count'),
            truth.get('pending_count'),
            current_truth.get('pending_count'),
        )
        if any(normalize_int_or_none(value) is not None for value in pending_candidates):
            return False
        group_candidates = (
            result.get('group_id'),
            result.get('runtime_group_id'),
            truth.get('group_id'),
            current_truth.get('group_id'),
            current_truth.get('runtime_group_id'),
        )
        if any(str(value or '').strip() for value in group_candidates):
            return False
        trust_status = str(item.get('trust_status') or result.get('trust_status') or '').strip()
        if trust_status.startswith(('TRUSTED', 'POST_APPROVAL')):
            return False
        return bool(trust_status or str(item.get('final_state') or '').strip())

    @staticmethod
    def _truth_acquisition_row_permission_state(row: Dict[str, Any]) -> str:
        item = dict(row or {})
        try:
            result = json.loads(str(item.get('result_json') or '{}'))
        except Exception:
            result = {}
        if not isinstance(result, dict):
            return ''
        source = result.get('source') if isinstance(result.get('source'), dict) else {}
        bridge = result.get('bridge_snapshot') if isinstance(result.get('bridge_snapshot'), dict) else {}
        verifier = result.get('membership_verifier') if isinstance(result.get('membership_verifier'), dict) else {}
        probe = verifier.get('probe') if isinstance(verifier.get('probe'), dict) else {}
        truth = result.get('approval_queue_truth') if isinstance(result.get('approval_queue_truth'), dict) else {}
        current_truth = truth.get('current_truth') if isinstance(truth.get('current_truth'), dict) else {}
        current_facts = current_truth.get('facts') if isinstance(current_truth.get('facts'), dict) else {}
        current_payload = current_truth.get('payload') if isinstance(current_truth.get('payload'), dict) else {}
        status_candidates = (
            result.get('reason_code'),
            result.get('permission_status'),
            source.get('permission_status'),
            bridge.get('permission_status'),
            verifier.get('status'),
            probe.get('permission_status'),
            probe.get('status'),
            probe.get('reason_code'),
        )
        for value in status_candidates:
            normalized = str(value or '').strip().lower()
            if normalized in {'not_group_member', 'not_group_admin'}:
                return normalized
        if str(item.get('trust_status') or result.get('trust_status') or '').strip().upper() == 'PERMISSION_DENIED':
            if result.get('self_participant_found') is False or bridge.get('self_participant_found') is False:
                return 'not_group_member'
            return 'not_group_admin'
        for facts in (result, bridge, probe, current_facts, current_payload):
            if (
                facts.get('self_participant_found') is True
                and facts.get('self_is_admin') is True
                and facts.get('can_manage_membership_requests') is True
            ):
                return 'permission_ok'
        return ''

    def _approval_binding_repeated_unresolved_failure_locks(
        self,
        account_key: str,
        binding_ids: Iterable[str],
        *,
        minimum_failures: int = 2,
    ) -> Dict[str, Dict[str, Any]]:
        normalized_account_key = str(account_key or '').strip()
        normalized_binding_ids = list(dict.fromkeys(
            str(binding_id or '').strip()
            for binding_id in binding_ids
            if str(binding_id or '').strip()
        ))
        required = max(int(minimum_failures or 2), 2)
        scan_limit = max(required, 20)
        if not normalized_account_key or not normalized_binding_ids:
            return {}
        try:
            with self.db.connect() as conn:
                rows = []
                for binding_id in normalized_binding_ids:
                    rows.extend(conn.execute(
                        """
                        SELECT binding_id, trigger, final_state, trust_status, current_truth_written,
                               result_json, created_at
                        FROM truth_acquisition_logs
                        WHERE account_key = ?
                          AND binding_id = ?
                          AND trigger IN (
                              'manual_truth_refresh',
                              'scheduled_full_sync',
                              'lightweight_probe_escalation',
                              'manual_approve_preflight',
                              'official_manual_approve_preflight'
                          )
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (normalized_account_key, binding_id, scan_limit),
                    ).fetchall())
        except Exception:
            return {}
        recent_by_binding: Dict[str, List[Dict[str, Any]]] = {binding_id: [] for binding_id in normalized_binding_ids}
        for row in rows:
            item = dict(row)
            binding_id = str(item.get('binding_id') or '').strip()
            recent = recent_by_binding.get(binding_id)
            if recent is None or len(recent) >= scan_limit:
                continue
            recent.append(item)
        locks: Dict[str, Dict[str, Any]] = {}
        for binding_id, attempts in recent_by_binding.items():
            permission_decision = next(
                (
                    (item, state)
                    for item in attempts
                    for state in (self._truth_acquisition_row_permission_state(item),)
                    if state
                ),
                None,
            )
            if permission_decision and permission_decision[1] in {'not_group_member', 'not_group_admin'}:
                latest, permission_status = permission_decision
                locks[binding_id] = {
                    'active': True,
                    'detected_at': str(latest.get('created_at') or '').strip() or utc_now(),
                    'failure_count': 1,
                    'source': 'truth_acquisition_logs',
                    'reason_code': permission_status,
                }
                continue
            recent_attempts = attempts[:required]
            if (
                permission_decision
                or len(recent_attempts) < required
                or not all(self._truth_acquisition_row_is_unresolved(item) for item in recent_attempts)
            ):
                continue
            latest = recent_attempts[0]
            try:
                latest_result = json.loads(str(latest.get('result_json') or '{}'))
            except Exception:
                latest_result = {}
            if not isinstance(latest_result, dict):
                latest_result = {}
            locks[binding_id] = {
                'active': True,
                'detected_at': str(latest.get('created_at') or '').strip() or utc_now(),
                'failure_count': required,
                'source': 'truth_acquisition_logs',
                'reason_code': str(latest_result.get('reason_code') or latest.get('trust_status') or latest.get('final_state') or '').strip(),
            }
        return locks

    def _apply_approval_queue_truth_to_binding(
        self,
        account_key: str,
        runtime_row: Dict[str, Any],
        *,
        account: Optional[Dict[str, Any]] = None,
        production_ops: Optional[Dict[str, Any]] = None,
        allow_live_refresh: bool = True,
    ) -> None:
        snapshots = self._load_approval_binding_queue_snapshots(account_key, runtime_row)
        resolving_from_invite_link = self._whatsapp_binding_should_resolve_from_invite_link(runtime_row)
        if resolving_from_invite_link:
            snapshots = {'current_truth': None, 'latest_probe': None}
        truth_view = self._approval_queue_truth_view(snapshots.get('current_truth'), snapshots.get('latest_probe'))
        current_truth = dict(snapshots.get('current_truth') or {}) if isinstance(snapshots.get('current_truth'), dict) else {}
        latest_probe = dict(snapshots.get('latest_probe') or {}) if isinstance(snapshots.get('latest_probe'), dict) else {}
        flow_type = str(runtime_row.get('approval_scope') or runtime_row.get('responsible_type') or '').strip()
        allow_live_truth_refresh = bool(
            runtime_row.get('allow_live_truth_refresh')
            or (account or {}).get('allow_live_truth_refresh')
        )
        if flow_type in {'registration_group', 'official_group'} and allow_live_refresh and allow_live_truth_refresh:
            bridge_account = dict(account or {})
            bridge_account['account_key'] = str(bridge_account.get('account_key') or account_key or '').strip()
            bridge_account['responsible_type'] = flow_type
            if not isinstance(bridge_account.get('runtime_state'), dict):
                runtime_state = runtime_row.get('runtime_state') if isinstance(runtime_row.get('runtime_state'), dict) else {}
                bridge_account['runtime_state'] = dict(runtime_state or {})
            if isinstance(runtime_row.get('provider_decision'), dict) and not isinstance(bridge_account.get('provider_decision'), dict):
                bridge_account['provider_decision'] = dict(runtime_row.get('provider_decision') or {})
            bridge_binding = {**dict(runtime_row or {}), 'responsible_type': flow_type}
            bridge_snapshot = self._fetch_registration_group_bridge_snapshot(account=bridge_account, binding=bridge_binding)
            bridge_result = self._build_registration_group_bridge_result(
                account=bridge_account,
                binding=bridge_binding,
                snapshot=bridge_snapshot,
            )
            if bridge_result and normalize_int_or_none(bridge_result.get('pending_count')) is not None:
                write_result = self.upsert_approval_queue_current_truth(
                    account_key=account_key,
                    binding=bridge_binding,
                    sync_result=bridge_result,
                    source_priority=95,
                    observed_at=utc_now(),
                    force=False,
                )
                if write_result.get('written'):
                    snapshots = self._load_approval_binding_queue_snapshots(account_key, runtime_row)
                    truth_view = self._approval_queue_truth_view(snapshots.get('current_truth'), snapshots.get('latest_probe'))
                    current_truth = dict(snapshots.get('current_truth') or {}) if isinstance(snapshots.get('current_truth'), dict) else {}
                    latest_probe = dict(snapshots.get('latest_probe') or {}) if isinstance(snapshots.get('latest_probe'), dict) else {}
        def _readable_group_name_from_source(source: Dict[str, Any]) -> str:
            if not isinstance(source, dict):
                return ''
            candidates: List[Any] = []
            for key in ('group_name', 'actual_group_name', 'runtime_probe_group_name', 'target_group_label'):
                candidates.append(source.get(key))
            for nested_key in ('payload', 'facts', 'current_truth', 'latest_probe'):
                nested = source.get(nested_key)
                if isinstance(nested, dict):
                    for key in ('group_name', 'actual_group_name', 'runtime_probe_group_name', 'target_group_label'):
                        candidates.append(nested.get(key))
            for value in candidates:
                text = str(value or '').strip()
                if text and not _looks_like_whatsapp_group_jid(text) and not _looks_like_whatsapp_invite_link(text):
                    return text
            return ''

        readable_group_name = (
            _readable_group_name_from_source(runtime_row)
            or _readable_group_name_from_source(current_truth)
            or _readable_group_name_from_source(latest_probe)
            or _readable_group_name_from_source(truth_view)
        )
        if flow_type in {'registration_group', 'official_group'} and not resolving_from_invite_link:
            anchor_identity = self._lookup_binding_cycle_anchor_identity(
                production_ops=production_ops or self._production_ops_daemon_snapshot(),
                responsible_type=flow_type,
                binding=runtime_row,
                probe={},
            )
            anchor_group_id = str(anchor_identity.get('group_id') or '').strip()
            anchor_group_name = _readable_group_name_from_source({'group_name': anchor_identity.get('group_name')})
            if anchor_group_id:
                if not str(runtime_row.get('group_id') or '').strip():
                    runtime_row['group_id'] = anchor_group_id
                registration_group = str(runtime_row.get('registration_group') or '').strip()
                if not registration_group or _looks_like_whatsapp_invite_link(registration_group):
                    runtime_row['registration_group'] = anchor_group_id
                if not str(runtime_row.get('runtime_probe_group_id') or '').strip():
                    runtime_row['runtime_probe_group_id'] = anchor_group_id
            if not readable_group_name and anchor_group_name:
                readable_group_name = anchor_group_name
        if readable_group_name:
            if not _readable_group_name_from_source({'group_name': runtime_row.get('group_name')}):
                runtime_row['group_name'] = readable_group_name
            if not _readable_group_name_from_source({'runtime_probe_group_name': runtime_row.get('runtime_probe_group_name')}):
                runtime_row['runtime_probe_group_name'] = readable_group_name
            if not _readable_group_name_from_source({'target_group_label': runtime_row.get('target_group_label')}):
                runtime_row['target_group_label'] = readable_group_name
        def _member_count_candidate(source: Dict[str, Any], *keys: str) -> Tuple[Optional[int], Optional[datetime]]:
            count: Optional[int] = None
            for key in keys:
                count = normalize_int_or_none(source.get(key))
                if count is not None:
                    break
            if count is None:
                return None, None
            timestamp = None
            for key in ('verified_at', 'source_ts', 'checked_at', 'updated_at', 'observed_at', 'last_probe_at'):
                raw_ts = str(source.get(key) or '').strip()
                if not raw_ts:
                    continue
                try:
                    timestamp = parse_iso_datetime(raw_ts)
                    break
                except Exception:
                    continue
            return count, timestamp

        current_member_count = normalize_int_or_none(truth_view.get('member_count'))
        if current_member_count is None:
            current_member_count = normalize_int_or_none(current_truth.get('member_count') or current_truth.get('memberCount'))
        if current_member_count is None and isinstance(current_truth.get('facts'), dict):
            facts = dict(current_truth.get('facts') or {})
            current_member_count = normalize_int_or_none(facts.get('member_count') or facts.get('memberCount'))
        member_count = current_member_count
        if truth_view.get('stale') is True:
            candidates: List[Tuple[Optional[int], Optional[datetime]]] = []
            if current_member_count is not None:
                current_with_count = {
                    **current_truth,
                    'member_count': current_member_count,
                    'checked_at': current_truth.get('checked_at') or truth_view.get('verified_at'),
                }
                candidates.append(_member_count_candidate(current_with_count, 'member_count', 'memberCount'))
            if isinstance(current_truth.get('facts'), dict):
                facts = dict(current_truth.get('facts') or {})
                candidates.append(_member_count_candidate(
                    {**facts, 'checked_at': current_truth.get('checked_at') or truth_view.get('verified_at')},
                    'member_count',
                    'memberCount',
                ))
            candidates.append(_member_count_candidate(latest_probe, 'member_count', 'memberCount'))
            candidates.append(_member_count_candidate(runtime_row, 'last_probe_member_count', 'member_count'))
            valid_candidates = [(count, ts) for count, ts in candidates if count is not None]
            if valid_candidates:
                count, _ = max(valid_candidates, key=lambda item: item[1] or datetime.fromtimestamp(0, timezone.utc))
                member_count = count
        if member_count is None:
            member_count = normalize_int_or_none(latest_probe.get('member_count') or latest_probe.get('memberCount'))
        if member_count is None:
            member_count = normalize_int_or_none(runtime_row.get('last_probe_member_count') or runtime_row.get('member_count'))
        if member_count is not None:
            truth_view['member_count'] = member_count
            runtime_row['member_count'] = member_count
            runtime_row['last_probe_member_count'] = member_count
        if flow_type in {'registration_group', 'official_group'}:
            truth_view['flow_type'] = flow_type
        runtime_row['approval_queue_truth'] = truth_view
        runtime_row['syncing'] = truth_view.get('syncing')
        runtime_row['can_manual_approve'] = truth_view.get('can_manual_approve')
        runtime_row['manual_approve_allowed'] = truth_view.get('manual_approve_allowed')
        verifier = runtime_row.get('membership_verifier') if isinstance(runtime_row.get('membership_verifier'), dict) else {}
        runtime_row['membership_verifier'] = serialize_membership_verifier(verifier)

    @staticmethod
    def _approval_queue_truth_facts(sync_result: Dict[str, Any], *, source_priority: int, observed_at: str, syncing: bool = False) -> Dict[str, Any]:
        trust_status = str(sync_result.get('trust_status') or sync_result.get('status') or '').strip()
        trusted_pending_count = sync_result.get('trusted_pending_count')
        if trusted_pending_count is None and trust_status == 'TRUSTED_CONFIRMED_PENDING':
            trusted_pending_count = sync_result.get('ui_pending_count', sync_result.get('pending_count'))
        if trusted_pending_count is None and trust_status == 'TRUSTED_CONFIRMED_EMPTY':
            trusted_pending_count = 0
        try:
            trusted_pending_count = int(trusted_pending_count) if trusted_pending_count is not None else None
        except Exception:
            trusted_pending_count = None
        api_pending_count = sync_result.get('api_pending_count')
        try:
            api_pending_count = int(api_pending_count) if api_pending_count is not None else None
        except Exception:
            api_pending_count = None
        ui_pending_count = sync_result.get('ui_pending_count')
        try:
            ui_pending_count = int(ui_pending_count) if ui_pending_count is not None else None
        except Exception:
            ui_pending_count = None
        requester_ids = list(sync_result.get('requester_ids') or []) if isinstance(sync_result.get('requester_ids'), list) else []
        runtime_generation = sync_result.get('runtime_generation')
        source_payload = dict(sync_result.get('source') if isinstance(sync_result.get('source'), dict) else {})
        if runtime_generation is None:
            runtime_generation = source_payload.get('runtime_generation')
        try:
            runtime_generation = int(runtime_generation) if runtime_generation is not None else None
        except Exception:
            runtime_generation = None
        group_id = Service._extract_whatsapp_group_jid_from_payload(sync_result)
        if not group_id:
            for value in (
                source_payload.get('group_id'),
                source_payload.get('runtime_probe_group_id'),
                source_payload.get('probe_group_id'),
                source_payload.get('runtime_group_id'),
            ):
                group_id = _sanitize_whatsapp_group_jid(value)
                if group_id:
                    break
        group_name = (
            str(sync_result.get('group_name') or '').strip()
            or str(sync_result.get('actual_group_name') or '').strip()
            or str(sync_result.get('runtime_probe_group_name') or '').strip()
            or Service._extract_whatsapp_group_name_from_payload(sync_result)
            or str(source_payload.get('group_name') or '').strip()
            or str(source_payload.get('actual_group_name') or '').strip()
            or str(source_payload.get('runtime_probe_group_name') or '').strip()
        )
        if _looks_like_whatsapp_group_jid(group_name) or _looks_like_whatsapp_invite_link(group_name):
            group_name = ''
        return {
            'trust_status': trust_status,
            'trusted_pending_count': trusted_pending_count,
            'pending_count': trusted_pending_count if trusted_pending_count is not None else sync_result.get('pending_count'),
            'ui_pending_count': ui_pending_count,
            'api_pending_count': api_pending_count,
            'member_count': normalize_int_or_none(sync_result.get('member_count')),
            'group_id': group_id or None,
            'group_name': group_name or None,
            'actual_group_name': group_name or None,
            'runtime_probe_group_id': group_id or None,
            'runtime_probe_group_name': group_name or None,
            'requester_ids': requester_ids,
            'requesters': list(sync_result.get('requesters') or []) if isinstance(sync_result.get('requesters'), list) else [],
            'oldest_pending_at': str(sync_result.get('oldest_pending_at') or '').strip() or None,
            'fingerprint': str(sync_result.get('fingerprint') or '').strip(),
            'fingerprint_quality': str(sync_result.get('fingerprint_quality') or ('strong' if sync_result.get('requester_ids') else 'weak')).strip(),
            'reason_code': str(sync_result.get('reason_code') or '').strip(),
            'display_trusted': bool(sync_result.get('display_trusted')) if sync_result.get('display_trusted') is not None else trust_status.startswith('TRUSTED'),
            'can_manual_approve': bool(sync_result.get('can_manual_approve')) if sync_result.get('can_manual_approve') is not None else trust_status == 'TRUSTED_CONFIRMED_PENDING',
            'manual_approve_allowed': bool(sync_result.get('manual_approve_allowed')) if sync_result.get('manual_approve_allowed') is not None else trust_status == 'TRUSTED_CONFIRMED_PENDING',
            'group_identity_verified': bool(sync_result.get('group_identity_verified')),
            'runtime_identity_match': bool(sync_result.get('runtime_identity_match')) if sync_result.get('runtime_identity_match') is not None else None,
            'session_authenticated': bool(sync_result.get('session_authenticated')),
            'self_participant_found': bool(sync_result.get('self_participant_found')) if sync_result.get('self_participant_found') is not None else None,
            'self_is_admin': bool(sync_result.get('self_is_admin')) if sync_result.get('self_is_admin') is not None else None,
            'can_manage_membership_requests': bool(sync_result.get('can_manage_membership_requests')) if sync_result.get('can_manage_membership_requests') is not None else None,
            'review_surface_ready': bool(sync_result.get('review_surface_ready')),
            'empty_queue_visible': bool(sync_result.get('empty_queue_visible')),
            'strong_empty_evidence': bool(sync_result.get('strong_empty_evidence')),
            'zero_pending_verified_by': str(sync_result.get('zero_pending_verified_by') or '').strip() or None,
            'pending_zero_confidence': str(sync_result.get('pending_zero_confidence') or '').strip() or None,
            'manual_override_eligible': bool(sync_result.get('manual_override_eligible')),
            'manual_override_mode': str(sync_result.get('manual_override_mode') or '').strip() or None,
            'manual_override_issues': list(sync_result.get('manual_override_issues') or []) if isinstance(sync_result.get('manual_override_issues'), list) else [],
            'fingerprint_stable': bool(sync_result.get('fingerprint_stable')),
            'fingerprint_stable_count': int(sync_result.get('fingerprint_stable_count') or 0),
            'runtime_generation': runtime_generation,
            'stale': bool(sync_result.get('stale')),
            'syncing': bool(syncing),
            'source_priority': int(source_priority),
            'observed_at': observed_at,
            'source_ts': str(sync_result.get('source_ts') or observed_at),
            'verified_at': str(sync_result.get('verified_at') or sync_result.get('source_ts') or observed_at),
            'invalidated_reason': str(sync_result.get('invalidated_reason') or '').strip() or None,
            'active_approval_run_id': str(sync_result.get('active_approval_run_id') or '').strip() or None,
            'last_approval_action_ts': str(sync_result.get('last_approval_action_ts') or '').strip() or None,
            'last_approved_count': int(sync_result.get('last_approved_count') or 0),
            'verifying_since': str(sync_result.get('verifying_since') or '').strip() or None,
            'display_schema_version': int(sync_result.get('display_schema_version') or 1),
        }

    def _write_approval_queue_snapshot(
        self,
        *,
        account_key: str,
        binding: Dict[str, Any],
        snapshot_type: str,
        sync_result: Dict[str, Any],
        source_priority: int = 0,
        observed_at: Optional[str] = None,
        force: bool = False,
        syncing: bool = False,
    ) -> Dict[str, Any]:
        observed_at = str(observed_at or utc_now())
        object_key = self._approval_binding_truth_object_key(account_key, binding)
        if not object_key:
            raise HTTPException(status_code=400, detail='approval_queue_object_key_required')
        facts = self._approval_queue_truth_facts(sync_result, source_priority=source_priority, observed_at=observed_at, syncing=syncing)
        trust_status = str(facts.get('trust_status') or 'UNKNOWN').strip() or 'UNKNOWN'
        snapshots = self._load_approval_binding_queue_snapshots_raw(account_key, binding)
        current = snapshots.get('current_truth') if snapshot_type == 'approval_queue_current_truth' else snapshots.get('latest_probe')
        if current:
            current_generation = current.get('runtime_generation')
            next_generation = facts.get('runtime_generation')
            try:
                current_generation = int(current_generation) if current_generation is not None else None
            except Exception:
                current_generation = None
            try:
                next_generation = int(next_generation) if next_generation is not None else None
            except Exception:
                next_generation = None
            if current_generation is not None and next_generation is not None and next_generation < current_generation:
                return {
                    'written': False,
                    'object_key': object_key,
                    'snapshot_type': snapshot_type,
                    'reason': 'stale_runtime_generation',
                    'current_runtime_generation': current_generation,
                    'incoming_runtime_generation': next_generation,
                }
        skip_guard = bool(sync_result.get('skip_guard'))
        if snapshot_type == 'approval_queue_current_truth' and not skip_guard:
            guard_ok, guard_reason = self._approval_queue_current_truth_guard(sync_result, facts)
            if not guard_ok:
                return {'written': False, 'object_key': object_key, 'snapshot_type': snapshot_type, 'reason': guard_reason, 'facts': facts}
        allow_write = True
        if snapshot_type == 'approval_queue_current_truth' and current and not force:
            current_priority = int(current.get('source_priority') or 0)
            current_age = None
            try:
                current_age = (datetime.now(timezone.utc) - parse_iso_datetime(str(current.get('checked_at') or ''))).total_seconds()
            except Exception:
                current_age = None
            new_priority = int(source_priority or 0)
            trusted_success = trust_status.startswith('TRUSTED') or trust_status == 'PERMISSION_DENIED'
            current_ttl_seconds = self._approval_queue_current_truth_ttl_seconds(
                str(current.get('trust_status') or current.get('truth_status') or '')
            )
            current_expired = current_age is None or current_age > current_ttl_seconds
            allow_write = (new_priority >= current_priority) or (current_expired and trusted_success)
        if not allow_write:
            return {'written': False, 'object_key': object_key, 'snapshot_type': snapshot_type, 'reason': 'lower_priority_current_truth_preserved'}
        source = dict(sync_result.get('source') if isinstance(sync_result.get('source'), dict) else {})
        if not source:
            source = {'source': str(sync_result.get('source') or 'approval_queue_sync')}
        source['source_priority'] = int(source_priority or 0)
        snapshot_id = f'{snapshot_type}:{object_key}'
        expires_at = sync_result.get('expires_at')
        if not expires_at and snapshot_type == 'approval_queue_current_truth' and (trust_status.startswith('TRUSTED') or trust_status == 'PERMISSION_DENIED'):
            expires_at = (
                parse_iso_datetime(observed_at)
                + timedelta(seconds=self._approval_queue_current_truth_ttl_seconds(trust_status))
            ).isoformat()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO mcn_truth_snapshots (
                    snapshot_id, object_type, object_key, snapshot_type, truth_status,
                    confidence, confidence_reason, facts_json, source_json, checked_at,
                    expires_at, recommended_action, updated_at
                ) VALUES (?, 'registration_group_binding', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(object_type, object_key, snapshot_type) DO UPDATE SET
                    snapshot_id=excluded.snapshot_id,
                    truth_status=excluded.truth_status,
                    confidence=excluded.confidence,
                    confidence_reason=excluded.confidence_reason,
                    facts_json=excluded.facts_json,
                    source_json=excluded.source_json,
                    checked_at=excluded.checked_at,
                    expires_at=excluded.expires_at,
                    recommended_action=excluded.recommended_action,
                    updated_at=excluded.updated_at
                """,
                (
                    snapshot_id,
                    object_key,
                    snapshot_type,
                    trust_status,
                    'verified' if (trust_status.startswith('TRUSTED') or trust_status == 'PERMISSION_DENIED') else 'untrusted',
                    str(facts.get('reason_code') or ''),
                    json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(source, ensure_ascii=False, sort_keys=True, default=str),
                    observed_at,
                    expires_at,
                    'restore_approval_capability' if trust_status == 'PERMISSION_DENIED' else ('none' if trust_status.startswith('TRUSTED') else 'manual_full_sync_or_recovery'),
                    utc_now(),
                ),
            )
            if snapshot_type == 'approval_queue_latest_probe':
                self.write_event_ledger(
                    conn=conn,
                    event_type='approval_queue_probe_observed',
                    object_type='registration_group_binding',
                    object_key=object_key,
                    status=trust_status,
                    evidence_level=str(facts.get('fingerprint_quality') or ''),
                    payload={**facts, 'snapshot_type': snapshot_type},
                )
            conn.commit()
        wa_snapshot_type = 'current_truth' if snapshot_type == 'approval_queue_current_truth' else 'latest_probe'
        self._mirror_wa_truth_snapshot(
            account_key=account_key,
            binding=binding,
            snapshot_type=wa_snapshot_type,
            facts=facts,
            observed_at=observed_at,
            expires_at=expires_at,
        )
        provider_name = 'baileys' if str((source or {}).get('provider_name') or (source or {}).get('provider') or '').strip().lower().startswith('baileys') else 'legacy_playwright'
        self._upsert_wa_identity_map_from_result(provider_name=provider_name, result=facts)
        return {
            'written': True,
            'object_key': object_key,
            'snapshot_type': snapshot_type,
            'trust_status': trust_status,
            'facts': facts,
            'checked_at': observed_at,
            'expires_at': expires_at,
        }

    def upsert_approval_queue_latest_probe(self, *, account_key: str, binding: Dict[str, Any], probe_result: Dict[str, Any], observed_at: Optional[str] = None, syncing: bool = False) -> Dict[str, Any]:
        return self._write_approval_queue_snapshot(
            account_key=account_key,
            binding=binding,
            snapshot_type='approval_queue_latest_probe',
            sync_result=probe_result,
            source_priority=0,
            observed_at=observed_at,
            force=True,
            syncing=syncing,
        )

    def _enrich_baileys_result_group_identity_from_metadata(
        self,
        *,
        executor: Any,
        result: Dict[str, Any],
        binding: Dict[str, Any],
        target: str,
        account_key: str = '',
        baileys_account_id: str = '',
    ) -> Dict[str, Any]:
        enriched = dict(result or {})
        if self._extract_whatsapp_group_jid_from_payload(enriched):
            return enriched
        if not hasattr(executor, 'group_metadata'):
            return enriched
        normalized_target = str(target or '').strip()
        metadata_payload: Dict[str, Any] = {
            'binding_id': binding.get('binding_id'),
            'account_key': str(account_key or binding.get('account_key') or '').strip() or None,
            'accountId': str(baileys_account_id or binding.get('baileys_account_id') or '').strip() or None,
            'baileys_account_id': str(baileys_account_id or binding.get('baileys_account_id') or '').strip() or None,
            'group_name': enriched.get('group_name') or binding.get('group_name'),
            'groupName': enriched.get('group_name') or binding.get('group_name'),
            'link': binding.get('link'),
        }
        if _looks_like_whatsapp_invite_link(normalized_target):
            metadata_payload['groupLink'] = normalized_target
            metadata_payload['link'] = normalized_target
        elif normalized_target:
            metadata_payload['group_id'] = normalized_target
            metadata_payload['groupId'] = normalized_target
        try:
            metadata = executor.group_metadata({key: value for key, value in metadata_payload.items() if value})
        except Exception as exc:
            enriched['metadata_identity_probe_error'] = str(exc)
            return enriched
        if not isinstance(metadata, dict):
            return enriched
        if metadata.get('error'):
            enriched['metadata_identity_probe_error'] = str(metadata.get('error') or '')
            return enriched
        metadata_group_id = self._extract_whatsapp_group_jid_from_payload(metadata)
        metadata_group_name = self._extract_whatsapp_group_name_from_payload(metadata)
        if metadata_group_id:
            enriched['group_id'] = metadata_group_id
            enriched['groupId'] = metadata_group_id
            enriched['resolvedGroupId'] = metadata_group_id
        if metadata_group_name and not str(enriched.get('group_name') or '').strip():
            enriched['group_name'] = metadata_group_name
            enriched['groupName'] = metadata_group_name
        metadata_member_count = normalize_int_or_none(metadata.get('member_count'))
        if metadata_member_count is not None and enriched.get('member_count') is None:
            enriched['member_count'] = metadata_member_count
        metadata_requester_count = normalize_int_or_none(metadata.get('requester_count'))
        if metadata_requester_count is not None and enriched.get('pending_count') is None:
            enriched['pending_count'] = metadata_requester_count
            enriched['trusted_pending_count'] = metadata_requester_count
            enriched['api_pending_count'] = metadata_requester_count
            enriched['ui_pending_count'] = metadata_requester_count
        if isinstance(metadata.get('requesters'), list) and not isinstance(enriched.get('requesters'), list):
            enriched['requesters'] = list(metadata.get('requesters') or [])
        enriched['metadata_identity_probe'] = {
            'provider': metadata.get('provider'),
            'provider_endpoint': metadata.get('provider_endpoint'),
            'group_id': metadata_group_id or metadata.get('group_id') or metadata.get('groupId'),
            'group_name': metadata_group_name or metadata.get('group_name') or metadata.get('groupName'),
        }
        return enriched

    def _call_baileys_full_queue_sync(
        self,
        *,
        account: Dict[str, Any],
        binding: Dict[str, Any],
        timeout_seconds: float = 30.0,
        priority: str = 'P1',
    ) -> Dict[str, Any]:
        runtime_state = dict(account.get('runtime_state') or {})
        executor = self._build_runtime_baileys_registration_group_executor(
            account=account,
            binding=binding,
            runtime_state=runtime_state,
        )
        target = self._whatsapp_binding_runtime_group_id(binding)
        target_is_invite_link = False
        if not target:
            target = self._whatsapp_binding_invite_link_target(binding)
            target_is_invite_link = bool(target)
        if not target:
            raise RuntimeError('registration_group_runtime_group_id_required')
        baileys_account_id = str(
            binding.get('baileys_account_id')
            or account.get('baileys_account_id')
            or runtime_state.get('baileys_account_id')
            or os.getenv('REGISTRATION_GROUP_BAILEYS_ACCOUNT_ID', '')
            or ''
        ).strip()
        payload = {
            'registration_group': target,
            'binding_id': binding.get('binding_id'),
            'account_key': account.get('account_key') or binding.get('account_key'),
            'provider_mode': binding.get('provider_mode') or runtime_state.get('provider_mode') or account.get('provider_mode'),
            'priority': str(priority or 'P1').strip() or 'P1',
        }
        if target_is_invite_link:
            payload['groupLink'] = target
            payload['link'] = target
        else:
            payload['group_id'] = target
            payload['groupId'] = target
        if baileys_account_id:
            payload['accountId'] = baileys_account_id
            payload['baileys_account_id'] = baileys_account_id
        if binding.get('link'):
            payload['groupLink'] = binding.get('link')
            payload['link'] = binding.get('link')
        if hasattr(executor, 'full_queue_sync'):
            result = executor.full_queue_sync(payload, timeout_seconds=timeout_seconds)
        else:
            result = self._call_whatsapp_worker_full_queue_sync(account=account, binding={**dict(binding or {}), 'group_id': target}, timeout_seconds=timeout_seconds)
        if isinstance(result, dict):
            return self._enrich_baileys_result_group_identity_from_metadata(
                executor=executor,
                result=result,
                binding=binding,
                target=target,
                account_key=str(account.get('account_key') or binding.get('account_key') or '').strip(),
                baileys_account_id=baileys_account_id,
            )
        return result

    def _probe_baileys_binding_group_state(
        self,
        *,
        responsible_type: str,
        binding: Dict[str, Any],
        runtime_state: Optional[Dict[str, Any]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        allow_shared_fallback: bool = True,
        allow_non_jid_fallback: bool = False,
        attempts: int = 3,
        timeout_seconds: float = 25.0,
        priority: str = 'P1',
    ) -> Dict[str, Any]:
        normalized_type = str(responsible_type or '').strip().lower()
        runtime_state = dict(runtime_state or {})
        session_state = dict(session_state or {})
        target_label = self._whatsapp_binding_probe_label(binding)
        target_candidates = self._whatsapp_binding_probe_candidates(
            binding,
            allow_non_jid_fallback=allow_non_jid_fallback,
        )
        if not normalized_type or not target_candidates:
            return {}
        executor = self._build_runtime_baileys_registration_group_executor(
            binding=binding,
            runtime_state=runtime_state,
        )
        if not getattr(executor, 'base_url', ''):
            return {}
        expected_account_key = str(binding.get('account_key') or runtime_state.get('account_key') or '').strip()
        expected_runtime_phone = ''.join(ch for ch in expected_account_key if ch.isdigit())
        normalized_attempts = max(1, int(attempts or 1))
        provider_priority = str(priority or 'P1').strip() or 'P1'
        last_error: Optional[Exception] = None
        for _ in range(normalized_attempts):
            for probe_target in target_candidates:
                try:
                    payload = executor.group_state(
                        probe_target,
                        extra_payload={
                            'group_id': binding.get('group_id'),
                            'group_name': binding.get('group_name'),
                            'link': binding.get('link'),
                            'binding_id': binding.get('binding_id'),
                            'account_key': expected_account_key,
                            'baileys_account_id': binding.get('baileys_account_id') or runtime_state.get('baileys_account_id') or os.getenv('REGISTRATION_GROUP_BAILEYS_ACCOUNT_ID', ''),
                            'expected_runtime_phone': expected_runtime_phone or None,
                            'provider_mode': binding.get('provider_mode') or runtime_state.get('provider_mode'),
                            'login_verified': bool(session_state.get('login_verified')),
                            'allow_shared_fallback': bool(allow_shared_fallback and normalized_type == 'registration_group'),
                            'priority': provider_priority,
                        },
                    )
                except Exception as exc:
                    last_error = exc
                    continue
                if isinstance(payload, dict):
                    normalized = dict(payload)
                    if normalized_type == 'official_group' and hasattr(executor, 'group_metadata'):
                        metadata_target = str(
                            normalized.get('group_id')
                            or binding.get('group_id')
                            or binding.get('registration_group')
                            or probe_target
                            or ''
                        ).strip()
                        metadata_payload = {
                            'group_id': metadata_target,
                            'groupId': metadata_target,
                            'group_name': normalized.get('group_name') or binding.get('group_name'),
                            'groupName': normalized.get('group_name') or binding.get('group_name'),
                            'link': binding.get('link'),
                            'account_key': expected_account_key,
                            'accountId': binding.get('baileys_account_id') or runtime_state.get('baileys_account_id') or os.getenv('REGISTRATION_GROUP_BAILEYS_ACCOUNT_ID', ''),
                            'baileys_account_id': binding.get('baileys_account_id') or runtime_state.get('baileys_account_id') or os.getenv('REGISTRATION_GROUP_BAILEYS_ACCOUNT_ID', ''),
                            'priority': provider_priority,
                        }
                        try:
                            metadata = executor.group_metadata({k: v for k, v in metadata_payload.items() if v})
                        except Exception as exc:
                            metadata = {'error': str(exc)}
                        if isinstance(metadata, dict) and not metadata.get('error'):
                            metadata_member_count = normalize_int_or_none(metadata.get('member_count'))
                            metadata_requester_count = normalize_int_or_none(metadata.get('requester_count'))
                            if metadata_member_count is not None:
                                normalized['member_count'] = metadata_member_count
                                normalized['participants_count_raw'] = metadata_member_count
                                normalized['participants_count'] = metadata_member_count
                                normalized.setdefault('participants_load_status', 'complete')
                            if metadata_requester_count is not None and normalized.get('pending_count') is None:
                                normalized['pending_count'] = metadata_requester_count
                                normalized.setdefault('trusted_pending_count', metadata_requester_count)
                                normalized.setdefault('api_pending_count', metadata_requester_count)
                                normalized.setdefault('ui_pending_count', metadata_requester_count)
                            if str(metadata.get('group_id') or '').strip():
                                normalized['group_id'] = str(metadata.get('group_id') or '').strip()
                            if str(metadata.get('group_name') or '').strip():
                                normalized['group_name'] = str(metadata.get('group_name') or '').strip()
                            if isinstance(metadata.get('requesters'), list) and not isinstance(normalized.get('requesters'), list):
                                normalized['requesters'] = list(metadata.get('requesters') or [])
                            normalized['metadata_probe'] = {
                                'provider': metadata.get('provider'),
                                'provider_endpoint': metadata.get('provider_endpoint'),
                                'member_count': metadata.get('member_count'),
                                'requester_count': metadata.get('requester_count'),
                            }
                        elif isinstance(metadata, dict) and metadata.get('error'):
                            normalized['metadata_probe_error'] = str(metadata.get('error') or '')
                    if not self._extract_whatsapp_group_jid_from_payload(normalized):
                        normalized = self._enrich_baileys_result_group_identity_from_metadata(
                            executor=executor,
                            result=normalized,
                            binding=binding,
                            target=probe_target,
                            account_key=expected_account_key,
                            baileys_account_id=str(
                                binding.get('baileys_account_id')
                                or runtime_state.get('baileys_account_id')
                                or os.getenv('REGISTRATION_GROUP_BAILEYS_ACCOUNT_ID', '')
                                or ''
                            ).strip(),
                        )
                    normalized['source_base_url'] = getattr(executor, 'base_url', '')
                    normalized['probe_target'] = target_label or probe_target
                    normalized.setdefault('provider', 'baileys')
                    return normalized
        if last_error is not None:
            raise last_error
        return {}

    def _registration_group_baileys_executor_group_state(self, registration_group: str, *, allow_legacy_target: bool = False) -> Dict[str, Any]:
        target = str(registration_group or '').strip()
        if target:
            match = self._find_whatsapp_approval_account_binding(
                responsible_type='registration_group',
                target_group=target,
            )
            if isinstance(match, dict) and match:
                account_key = str(match.get('account_key') or '').strip()
                account_row = self._get_whatsapp_approval_account_row(account_key) or match
                runtime_state, session_state, _ = self._build_whatsapp_approval_lightweight_runtime_snapshot(account_row)
                legacy_runtime_aliases: Dict[str, Any] = {}
                try:
                    legacy_runtime_aliases = self._build_whatsapp_approval_runtime_state(
                        account_key,
                        allow_shared_fallback=False,
                        skip_health_check=True,
                    )
                except Exception:
                    legacy_runtime_aliases = {}
                binding = dict(match.get('binding') or {})
                executor = self._build_runtime_baileys_registration_group_executor(
                    account=match,
                    binding=binding,
                    runtime_state=runtime_state,
                )
                if getattr(executor, 'base_url', ''):
                    baileys_account_id = str(
                        binding.get('baileys_account_id')
                        or legacy_runtime_aliases.get('baileys_account_id')
                        or legacy_runtime_aliases.get('provider_account_id')
                        or legacy_runtime_aliases.get('account_id')
                        or runtime_state.get('baileys_account_id')
                        or runtime_state.get('provider_account_id')
                        or runtime_state.get('account_id')
                        or os.getenv('REGISTRATION_GROUP_BAILEYS_ACCOUNT_ID', '')
                        or ''
                    ).strip()
                    return executor.group_state(
                        self._whatsapp_binding_runtime_group_id(binding) or target,
                        extra_payload={
                            'binding_id': binding.get('binding_id'),
                            'account_key': match.get('account_key'),
                            'accountId': baileys_account_id or None,
                            'baileys_account_id': baileys_account_id or None,
                            'group_id': binding.get('group_id'),
                            'group_name': binding.get('group_name'),
                            'link': binding.get('link'),
                            'provider_mode': binding.get('provider_mode') or runtime_state.get('provider_mode'),
                            'login_verified': bool(session_state.get('login_verified')),
                        },
                    )
        return self.registration_group_approval_executor_group_state(
            registration_group,
            allow_legacy_target=allow_legacy_target,
        )

    def _registration_group_baileys_approval_decision_sync(
        self,
        payload: RegistrationGroupApprovalDecisionRequest,
        *,
        approval_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload_runtime = getattr(payload, '__dict__', None)
        if isinstance(payload_runtime, dict):
            has_runtime_mode = any(str(payload_runtime.get(key) or '').strip() for key in RUNTIME_MODE_KEYS)
            if not has_runtime_mode:
                payload_runtime['provider_mode'] = 'baileys_primary'
                payload_runtime['registration_group_runtime'] = 'baileys_primary'
        return self._registration_group_approval_decision_sync(payload, approval_run_id=approval_run_id)

    @staticmethod
    def _build_full_sync_fallback_from_executor_group_state(
        registration_group: str,
        group_state: Optional[Dict[str, Any]],
        *,
        source: str,
        error: str,
    ) -> Dict[str, Any]:
        state = dict(group_state or {})
        if not state:
            return {}
        return {
            'ok': False,
            'trust_status': 'TRUTH_UNKNOWN',
            'trusted_pending_count': None,
            'ui_pending_count': None,
            'api_pending_count': None,
            'pending_count': None,
            'member_count': None,
            'group_id': str(state.get('group_id') or '').strip() or None,
            'group_name': str(state.get('group_name') or registration_group).strip() or registration_group,
            'requester_ids': [],
            'requesters': [],
            'fingerprint_quality': 'debug_only',
            'converged': False,
            'reason_code': 'executor_group_state_fallback_disabled_for_single_truth',
            'source': {
                'source': source,
                'mode': 'executor_group_state_fallback',
                'fallback_reason': error,
                'debug_only': True,
            },
        }

    def _decorate_approval_truth_result(self, *, account: Dict[str, Any], binding: Dict[str, Any], result: Dict[str, Any], account_key: str, runtime_generation: Optional[int] = None) -> Dict[str, Any]:
        decorated = dict(result or {})
        runtime_state = dict(account.get('runtime_state') or {}) if isinstance(account.get('runtime_state'), dict) else {}
        session_state = dict(account.get('session_state') or {}) if isinstance(account.get('session_state'), dict) else {}
        source_payload = dict(decorated.get('source') if isinstance(decorated.get('source'), dict) else {})
        if runtime_generation is None:
            runtime_generation = self._whatsapp_approval_runtime_generation(account_key)
        source_payload['runtime_generation'] = runtime_generation
        decorated['source'] = source_payload
        decorated['runtime_generation'] = runtime_generation
        identity_status = str(binding.get('identity_status') or '').strip().lower()
        group_id = str(decorated.get('group_id') or binding.get('group_id') or binding.get('registration_group') or '').strip()
        decorated['group_identity_verified'] = bool(
            decorated.get('group_identity_verified')
            or identity_status == 'resolved'
            or group_id.endswith('@g.us')
        )
        if decorated.get('runtime_identity_match') is None:
            runtime_identity_match = runtime_state.get('session_target_match')
            if runtime_identity_match is None:
                runtime_identity_match = runtime_state.get('identity_match')
            decorated['runtime_identity_match'] = runtime_identity_match
        if decorated.get('session_authenticated') is None:
            session_authenticated = None
            if any(bool(value) for value in (session_state.get('login_verified'), runtime_state.get('authenticated'), runtime_state.get('ready'))):
                session_authenticated = True
            decorated['session_authenticated'] = session_authenticated
        else:
            decorated['session_authenticated'] = bool(decorated.get('session_authenticated'))
        for field in ('self_participant_found', 'self_is_admin', 'can_manage_membership_requests', 'review_surface_ready', 'empty_queue_visible'):
            if decorated.get(field) is None and binding.get(field) is not None:
                decorated[field] = binding.get(field)
        if decorated.get('self_participant_found') is None and binding.get('last_probe_self_participant_found') is not None:
            decorated['self_participant_found'] = binding.get('last_probe_self_participant_found')
        if decorated.get('self_is_admin') is None and binding.get('last_probe_self_is_admin') is not None:
            decorated['self_is_admin'] = binding.get('last_probe_self_is_admin')
        if decorated.get('can_manage_membership_requests') is None and binding.get('last_probe_can_manage_membership_requests') is not None:
            decorated['can_manage_membership_requests'] = binding.get('last_probe_can_manage_membership_requests')
        if decorated.get('review_surface_ready') is None:
            decorated['review_surface_ready'] = bool(binding.get('review_surface_ready'))
        if decorated.get('empty_queue_visible') is None:
            decorated['empty_queue_visible'] = bool(binding.get('empty_queue_visible'))
        capability_missing = any(
            decorated.get(field) is None for field in ('self_participant_found', 'self_is_admin', 'can_manage_membership_requests')
        )
        identity_missing = not str(decorated.get('group_id') or '').strip() or not str(decorated.get('group_name') or '').strip()
        if capability_missing or identity_missing:
            snapshots = self._load_approval_binding_queue_snapshots(account_key, binding)
            for candidate in (snapshots.get('latest_probe'), snapshots.get('current_truth')):
                if not isinstance(candidate, dict):
                    continue
                if not bool(candidate.get('group_identity_verified')) or candidate.get('runtime_identity_match') is not True or not bool(candidate.get('session_authenticated')):
                    continue
                for field in (
                    'group_identity_verified',
                    'runtime_identity_match',
                    'session_authenticated',
                    'self_participant_found',
                    'self_is_admin',
                    'can_manage_membership_requests',
                    'review_surface_ready',
                ):
                    if decorated.get(field) is None and candidate.get(field) is not None:
                        decorated[field] = candidate.get(field)
                if not str(decorated.get('group_id') or '').strip() and str(candidate.get('group_id') or '').strip():
                    decorated['group_id'] = str(candidate.get('group_id') or '').strip()
                if not str(decorated.get('group_name') or '').strip() and str(candidate.get('group_name') or '').strip():
                    decorated['group_name'] = str(candidate.get('group_name') or '').strip()
                if (
                    decorated.get('self_participant_found') is not None
                    and decorated.get('self_is_admin') is not None
                    and decorated.get('can_manage_membership_requests') is not None
                    and decorated.get('runtime_identity_match') is True
                    and bool(decorated.get('session_authenticated'))
                    and str(decorated.get('group_id') or '').strip()
                    and str(decorated.get('group_name') or '').strip()
                ):
                    break
        if capability_missing or identity_missing:
            object_key = self._approval_binding_truth_object_key(account_key, binding)
            if object_key:
                try:
                    with self.db.connect() as conn:
                        rows = conn.execute(
                            """
                            SELECT payload_json FROM mcn_event_ledger
                            WHERE event_type='approval_queue_probe_observed'
                              AND object_type='registration_group_binding'
                              AND object_key=?
                            ORDER BY created_at DESC
                            LIMIT 6
                            """,
                            (object_key,),
                        ).fetchall()
                except Exception:
                    rows = []
                for row in rows:
                    try:
                        candidate = json.loads(row['payload_json'] or '{}')
                    except Exception:
                        candidate = {}
                    if not isinstance(candidate, dict):
                        continue
                    if not bool(candidate.get('group_identity_verified')) or candidate.get('runtime_identity_match') is not True or not bool(candidate.get('session_authenticated')):
                        continue
                    for field in (
                        'group_identity_verified',
                        'runtime_identity_match',
                        'session_authenticated',
                        'self_participant_found',
                        'self_is_admin',
                        'can_manage_membership_requests',
                        'review_surface_ready',
                    ):
                        if decorated.get(field) is None and candidate.get(field) is not None:
                            decorated[field] = candidate.get(field)
                    if not str(decorated.get('group_id') or '').strip() and str(candidate.get('group_id') or '').strip():
                        decorated['group_id'] = str(candidate.get('group_id') or '').strip()
                    if not str(decorated.get('group_name') or '').strip() and str(candidate.get('group_name') or '').strip():
                        decorated['group_name'] = str(candidate.get('group_name') or '').strip()
                    if (
                        decorated.get('self_participant_found') is not None
                        and decorated.get('self_is_admin') is not None
                        and decorated.get('can_manage_membership_requests') is not None
                        and decorated.get('runtime_identity_match') is True
                        and bool(decorated.get('session_authenticated'))
                        and str(decorated.get('group_id') or '').strip()
                        and str(decorated.get('group_name') or '').strip()
                    ):
                        break
        trust_status = str(decorated.get('trust_status') or '').strip()
        if trust_status == 'TRUSTED_CONFIRMED_EMPTY' and not decorated.get('strong_empty_evidence'):
            decorated['strong_empty_evidence'] = bool(
                decorated.get('group_identity_verified')
                and decorated.get('session_authenticated')
                and decorated.get('self_participant_found') is True
                and decorated.get('can_manage_membership_requests') is True
                and decorated.get('review_surface_ready')
                and decorated.get('empty_queue_visible')
                and str(decorated.get('source', {}).get('mode') or '').strip() != 'executor_group_state_fallback'
                and decorated.get('api_pending_count') in (0, None)
                and decorated.get('ui_pending_count') in (0, None)
            )
        return decorated

    def _acquire_approval_truth_minimal(
        self,
        *,
        account_key: str,
        account: Dict[str, Any],
        binding: Dict[str, Any],
        registration_group: str,
        source: str,
        hard_timeout: float,
        allow_soft_reload: bool = True,
    ) -> Dict[str, Any]:
        runtime_generation = self._whatsapp_approval_runtime_generation(account_key)
        responsible_type = str(binding.get('responsible_type') or account.get('responsible_type') or '').strip().lower()

        if registration_group:
            bridge_snapshot = self._fetch_registration_group_bridge_snapshot(account=account, binding=binding)
            bridge_result = self._build_registration_group_bridge_result(
                account=account,
                binding=binding,
                snapshot=bridge_snapshot,
                acquisition_result=None,
            )
            if bridge_result and normalize_int_or_none(bridge_result.get('pending_count')) is not None:
                return bridge_result

        def _official_baileys_once(active_account: Dict[str, Any], active_generation: int) -> Dict[str, Any]:
            if responsible_type != 'official_group':
                return {}
            active_account = {**dict(active_account or {}), 'responsible_type': 'official_group'}
            active_binding = {**dict(binding or {}), 'responsible_type': 'official_group'}
            runtime_state = dict(active_account.get('runtime_state') or {})
            provider_decision = self._resolve_wa_provider_decision(
                account=active_account,
                binding=active_binding,
                runtime_state=runtime_state,
                responsible_type='official_group',
            )
            if (
                str(provider_decision.get('provider_name') or '').strip().lower() != 'baileys'
                or not bool(provider_decision.get('authoritative_read'))
            ):
                return {}
            provider_mode = str(provider_decision.get('provider_mode') or '').strip()
            if provider_mode:
                active_account.setdefault('provider_mode', provider_mode)
                active_binding.setdefault('provider_mode', provider_mode)
                active_binding.setdefault('official_group_runtime', provider_mode)
            baileys_result = self._call_baileys_full_queue_sync(
                account=active_account,
                binding=active_binding,
                timeout_seconds=hard_timeout,
                priority=(
                    'P0'
                    if source in {'manual_approve_preflight', 'official_manual_approve_preflight', 'official_ready_precise_sync'}
                    else 'P2'
                    if source in {'scheduled_full_sync', 'lightweight_probe_escalation'}
                    else 'P1'
                ),
            )
            if not isinstance(baileys_result, dict) or not baileys_result:
                return {}
            source_payload = dict(baileys_result.get('source') or {}) if isinstance(baileys_result.get('source'), dict) else {}
            source_payload.setdefault('provider', 'baileys')
            source_payload.setdefault('mode', 'official_group_full_sync_baileys')
            source_payload['full_sync_provider'] = 'official_group_authoritative_baileys'
            source_payload['trigger'] = source
            baileys_result['source'] = source_payload
            if str(baileys_result.get('trust_status') or '').strip() == 'TRUSTED_CONFIRMED_EMPTY':
                baileys_result['strong_empty_evidence'] = True
                baileys_result.setdefault('zero_pending_verified_by', 'official_group_authoritative_baileys')
            return self._decorate_approval_truth_result(
                account=active_account,
                binding=active_binding,
                result=baileys_result,
                account_key=account_key,
                runtime_generation=active_generation,
            )

        def _worker_once(active_account: Dict[str, Any], active_generation: int) -> Dict[str, Any]:
            worker_result = self.whatsapp_approval_runtime_adapter.full_queue_sync(
                service=self,
                account=active_account,
                binding=binding,
                timeout_seconds=hard_timeout,
                priority=(
                    'P0'
                    if source in {'manual_approve_preflight', 'official_manual_approve_preflight', 'official_ready_precise_sync'}
                    else 'P2'
                    if source in {'scheduled_full_sync', 'lightweight_probe_escalation'}
                    else 'P1'
                ),
            )
            if not isinstance(worker_result, dict):
                worker_result = {'ok': False, 'trust_status': 'UNTRUSTED_SYNC_INVALID', 'reason_code': 'invalid_worker_response', 'source': source}
            worker_result.setdefault('source', source)
            return self._decorate_approval_truth_result(
                account=active_account,
                binding=binding,
                result=worker_result,
                account_key=account_key,
                runtime_generation=active_generation,
            )

        result = _official_baileys_once(account, runtime_generation)
        if not result:
            try:
                result = _worker_once(account, runtime_generation)
            except Exception as exc:
                result = {
                    'ok': False,
                    'trust_status': 'SYNC_TIMEOUT',
                    'reason_code': 'full_sync_hard_timeout',
                    'error': str(exc),
                    'source': source,
                }
                result = self._decorate_approval_truth_result(
                    account=account,
                    binding=binding,
                    result=result,
                    account_key=account_key,
                    runtime_generation=runtime_generation,
                )

        trust_status = str(result.get('trust_status') or '').strip()
        reason_code = str(result.get('reason_code') or '').strip()
        soft_reload_candidates = {
            'full_sync_hard_timeout',
            'ui_api_not_converged',
            'ui_count_greater_than_api_count',
            'ui_empty_api_has_historical_requests',
            'invalid_worker_response',
        }
        soft_reload_reason = (
            reason_code in soft_reload_candidates
            or 'degraded_fail_closed' in reason_code.lower()
        )
        account_runtime_state = dict(account.get('runtime_state') or {})
        account_session_state = dict(account.get('session_state') or account.get('session') or {})
        account_login_ready = bool(
            account_session_state.get('login_verified')
            or account_session_state.get('can_probe')
            or (account_runtime_state.get('ready') and account_runtime_state.get('authenticated'))
        )
        if allow_soft_reload and registration_group and account_login_ready and bool(account_runtime_state.get('active')) and not trust_status.startswith('TRUSTED') and soft_reload_reason:
            try:
                self.recover_whatsapp_approval_account_runtime(account_key)
                refreshed_account = self._get_whatsapp_approval_account_runtime_row(account_key)
                refreshed_generation = self._whatsapp_approval_runtime_generation(account_key)
                retried = _worker_once(refreshed_account, refreshed_generation)
                retried['source'] = dict(retried.get('source') if isinstance(retried.get('source'), dict) else {})
                retried['source']['truth_acquisition_retry'] = 'soft_reload'
                result = retried
                account = refreshed_account
                runtime_generation = refreshed_generation
                trust_status = str(result.get('trust_status') or '').strip()
                reason_code = str(result.get('reason_code') or '').strip()
            except Exception as exc:
                result['soft_reload_error'] = str(exc)

        if registration_group:
            bridge_snapshot = self._fetch_registration_group_bridge_snapshot(account=account, binding=binding)
            bridge_result = self._build_registration_group_bridge_result(
                account=account,
                binding=binding,
                snapshot=bridge_snapshot,
                acquisition_result=result,
            )
            if bridge_result and (
                normalize_int_or_none(bridge_result.get('pending_count')) is not None
                or str(bridge_result.get('trust_status') or '').strip() == 'PERMISSION_DENIED'
            ):
                return bridge_result

        fallback_needed = registration_group and (
            trust_status in {'SYNC_TIMEOUT', 'TRUTH_UNKNOWN', 'EMPTY_UNVERIFIED'}
            or (not trust_status.startswith('TRUSTED') and reason_code in {
                'ui_api_not_converged',
                'ui_count_greater_than_api_count',
                'ui_empty_api_has_historical_requests',
                'full_sync_hard_timeout',
                'invalid_worker_response',
            })
        )
        if fallback_needed:
            fallback_result: Dict[str, Any] = {}
            try:
                fallback_result = self._build_full_sync_fallback_from_executor_group_state(
                    registration_group,
                    self.whatsapp_approval_runtime_adapter.registration_group_executor_state(
                        service=self,
                        registration_group=registration_group,
                        allow_legacy_target=False,
                    ),
                    source=source,
                    error=f'worker_untrusted:{trust_status}:{reason_code}',
                )
            except Exception:
                fallback_result = {}
            if not fallback_result:
                try:
                    snapshot_state = self._load_pending_truth_snapshot_group_state(
                        account_key=account_key,
                        binding=binding,
                        registration_group=registration_group,
                    )
                    fallback_result = self._build_full_sync_fallback_from_executor_group_state(
                        registration_group,
                        snapshot_state,
                        source=source,
                        error=f'pending_truth_snapshot:{trust_status}:{reason_code}',
                    ) if snapshot_state else {}
                except Exception:
                    fallback_result = {}
            if fallback_result:
                result = self._decorate_approval_truth_result(
                    account=account,
                    binding=binding,
                    result=fallback_result,
                    account_key=account_key,
                    runtime_generation=runtime_generation,
                )
        return result

    @staticmethod
    def _approval_probe_requester_ids(probe: Dict[str, Any]) -> List[str]:
        direct_ids = [str(item).strip() for item in (probe.get('requester_ids') or []) if str(item).strip()] if isinstance(probe.get('requester_ids'), list) else []
        if direct_ids:
            return direct_ids
        requesters = list(probe.get('requesters') or []) if isinstance(probe.get('requesters'), list) else []
        extracted: List[str] = []
        for requester in requesters:
            if not isinstance(requester, dict):
                continue
            candidate = str(requester.get('requesterId') or requester.get('requester_id') or requester.get('phone') or '').strip()
            if candidate:
                extracted.append(candidate)
        return extracted

    def _probe_official_approval_binding_fast(self, *, account: Dict[str, Any], binding: Dict[str, Any], timeout_seconds: float = 4.0) -> Dict[str, Any]:
        runtime_state = dict(account.get('runtime_state') or {}) if isinstance(account, dict) else {}
        session_state = dict(account.get('session_state') or {}) if isinstance(account, dict) else {}
        try:
            effective_timeout = min(max(float(timeout_seconds or 4.0), 0.5), 4.0)
        except Exception:
            effective_timeout = 4.0
        baileys_provider = getattr(self.whatsapp_approval_runtime_adapter, 'baileys_provider', None)
        if baileys_provider is not None and hasattr(baileys_provider, 'probe_binding_group_state'):
            live_probe = baileys_provider.probe_binding_group_state(
                service=self,
                responsible_type='official_group',
                binding=binding,
                runtime_state=runtime_state,
                session_state=session_state,
                allow_shared_fallback=False,
                allow_non_jid_fallback=True,
                attempts=1,
                timeout_seconds=effective_timeout,
                priority='P0',
            )
        else:
            live_probe = self.whatsapp_approval_runtime_adapter.probe_binding_group_state(
                service=self,
                responsible_type='official_group',
                binding=binding,
                runtime_state=runtime_state,
                session_state=session_state,
                allow_shared_fallback=False,
                allow_non_jid_fallback=True,
                attempts=1,
                timeout_seconds=effective_timeout,
                priority='P0',
            )
        binding_runtime = {
            **dict(binding or {}),
            'authenticated': bool(session_state.get('authenticated') or session_state.get('login_verified') or runtime_state.get('authenticated')),
            'ready': bool(session_state.get('ready') or runtime_state.get('ready')),
        }
        return {
            'binding_runtime': binding_runtime,
            'probe': dict(live_probe or {}) if isinstance(live_probe, dict) else {},
        }

    def evaluate_approval_queue_staleness(self, *, account_key: str, binding: Dict[str, Any], external_signal: str = '') -> Dict[str, Any]:
        object_key = self._approval_binding_truth_object_key(account_key, binding)
        if not object_key:
            return {'stale_detected': False, 'reason': 'object_key_missing'}
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json, created_at FROM mcn_event_ledger
                WHERE event_type='approval_queue_probe_observed'
                  AND object_type='registration_group_binding'
                  AND object_key=?
                ORDER BY created_at DESC LIMIT 3
                """,
                (object_key,),
            ).fetchall()
            fingerprints = []
            statuses = []
            for row in rows:
                try:
                    payload = json.loads(row['payload_json'] or '{}')
                except Exception:
                    payload = {}
                fingerprints.append(str(payload.get('fingerprint') or '').strip())
                statuses.append(str(payload.get('trust_status') or '').strip())
            stale = bool(external_signal) and len(fingerprints) >= 3 and len(set(fingerprints)) == 1 and fingerprints[0] and any(status.startswith('UNTRUSTED') for status in statuses)
            if not stale:
                conn.commit()
                return {'stale_detected': False, 'fingerprints': fingerprints}
            recovery_action = 'soft_reload'
            cooldown_row = conn.execute(
                """
                SELECT created_at FROM mcn_event_ledger
                WHERE event_type='approval_queue_recovery_event'
                  AND object_type='registration_group_binding'
                  AND object_key=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (object_key,),
            ).fetchone()
            if cooldown_row is not None:
                try:
                    if (datetime.now(timezone.utc) - parse_iso_datetime(str(cooldown_row['created_at'] or ''))).total_seconds() < 120:
                        conn.commit()
                        return {'stale_detected': True, 'recovery_action': recovery_action, 'cooldown_active': True, 'object_key': object_key}
                except Exception:
                    pass
            recovery_result = {'attempted': False, 'status': 'queued_for_worker_recovery', 'action': recovery_action}
            payload = {
                'account_key': account_key,
                'object_key': object_key,
                'external_signal': external_signal,
                'fingerprint': fingerprints[0],
                'recovery_action': recovery_action,
                'recovery_result': recovery_result,
            }
            self.write_event_ledger(
                conn=conn,
                event_type='approval_queue_recovery_event',
                object_type='registration_group_binding',
                object_key=object_key,
                status='pending',
                evidence_level='stale_probe_fingerprint',
                payload=payload,
            )
            conn.commit()
        return {'stale_detected': True, 'recovery_action': recovery_action, 'object_key': object_key}

    def _build_whatsapp_approval_account_runtime(self, row: Dict[str, Any], *, production_ops: Optional[Dict[str, Any]] = None, official_bridge: Optional[Dict[str, Any]] = None, worker_health: Optional[Dict[str, Any]] = None, runtime_state: Optional[Dict[str, Any]] = None, session_state: Optional[Dict[str, Any]] = None, skip_live_probe: bool = False, read_only: bool = False) -> Dict[str, Any]:
        serialized = dict(row)
        assigned_customer_service_user_ids = self._whatsapp_approval_assigned_customer_service_ids_from_row(serialized)
        serialized['assigned_customer_service_user_ids'] = assigned_customer_service_user_ids
        responsible_type = str(serialized.get('responsible_type') or '').strip()
        raw_group_links = []
        try:
            raw_group_links = json.loads(serialized.get('group_links') or '[]')
        except Exception:
            raw_group_links = []
        if not isinstance(raw_group_links, list):
            raw_group_links = []
        default_area = str(serialized.get('area') or '').strip()
        default_notify_profile_name = str(serialized.get('notify_profile_name') or '').strip()
        legacy_count_threshold, legacy_timeout_minutes = _legacy_approval_thresholds(serialized.get('approval_rule'))
        default_approval_count_threshold = _coerce_positive_int(serialized.get('approval_count_threshold'), legacy_count_threshold)
        default_approval_timeout_minutes = _coerce_positive_int(serialized.get('approval_timeout_minutes'), legacy_timeout_minutes)
        default_auto_recover_worker = bool(serialized.get('auto_recover_worker'))
        account_schedule_windows = _normalize_schedule_windows_payload(json.loads(serialized.get('schedule_windows') or '[]') if str(serialized.get('schedule_windows') or '').strip() else []) if isinstance(serialized.get('schedule_windows'), str) else _normalize_schedule_windows_payload(serialized.get('schedule_windows') or [])

        group_link_bindings: list[dict[str, Any]] = []
        for item in raw_group_links:
            if isinstance(item, dict):
                link = str(item.get('link') or '').strip()
                area = str(item.get('area') or '').strip()
                registration_group = str(item.get('registration_group') or '').strip()
                group_id = str(item.get('group_id') or '').strip()
                if link:
                    group_link_row = {
                        'binding_id': str(item.get('binding_id') or '').strip(),
                        'link': link,
                        'group_name': str(item.get('group_name') or '').strip(),
                        'area': area,
                        'notify_profile_name': str(item.get('notify_profile_name') or default_notify_profile_name).strip(),
                        'enabled': False if item.get('enabled') is False else True,
                        'registration_group': registration_group,
                        'group_id': group_id,
                        'approval_count_threshold': item.get('approval_count_threshold'),
                        'approval_timeout_minutes': item.get('approval_timeout_minutes'),
                        'auto_recover_worker': item.get('auto_recover_worker'),
                        'schedule_windows': item.get('schedule_windows') if isinstance(item.get('schedule_windows'), list) else account_schedule_windows,
                    }
                    for key in WHATSAPP_APPROVAL_RUNTIME_CONFIG_KEYS:
                        if key in item:
                            group_link_row[key] = item.get(key)
                    if isinstance(item.get('provider_capabilities'), dict):
                        group_link_row['provider_capabilities'] = dict(item.get('provider_capabilities') or {})
                    if item.get('baileys_enabled') is not None:
                        group_link_row['baileys_enabled'] = item.get('baileys_enabled')
                    for key in (
                        'identity_status', 'identity_rebuild_reason', 'identity_resolved_at', 'identity_resolved_by',
                        'last_probe_status', 'last_probe_reason', 'last_probe_at', 'last_probe_had_group_id',
                        'last_probe_had_group_name', 'last_probe_self_participant_found', 'last_probe_self_is_admin',
                        'last_probe_can_manage_membership_requests', 'last_probe_member_count', 'runtime_probe_group_id',
                        'runtime_probe_group_name', 'queue_status', 'queue_confidence', 'config_fingerprint',
                    ):
                        if key in item:
                            group_link_row[key] = item.get(key)
                    group_link_bindings.append(group_link_row)
            else:
                link = str(item or '').strip()
                if link:
                    group_link_bindings.append({
                        'link': link,
                        'group_name': '',
                        'area': default_area,
                        'notify_profile_name': default_notify_profile_name,
                        'registration_group': '',
                        'group_id': '',
                        'approval_count_threshold': default_approval_count_threshold,
                        'approval_timeout_minutes': default_approval_timeout_minutes,
                        'auto_recover_worker': default_auto_recover_worker,
                        'schedule_windows': account_schedule_windows,
                    })

        group_link_bindings = _normalize_group_link_bindings(group_link_bindings, responsible_type=responsible_type)
        for idx, item in enumerate(group_link_bindings):
            item['binding_index'] = normalize_int_or_none(item.get('binding_index'))
            if item['binding_index'] is None:
                item['binding_index'] = idx
            item['index'] = normalize_int_or_none(item.get('index'))
            if item['index'] is None:
                item['index'] = item['binding_index']
            item['notify_profile_name'] = str(item.get('notify_profile_name') or default_notify_profile_name).strip()
            item = self._apply_account_notify_profile_to_official_binding(
                item,
                account=serialized,
                responsible_type=responsible_type,
            )
            item['approval_count_threshold'] = _coerce_positive_int(item.get('approval_count_threshold'), default_approval_count_threshold)
            item['approval_timeout_minutes'] = _coerce_positive_int(item.get('approval_timeout_minutes'), default_approval_timeout_minutes)
            item['auto_recover_worker'] = default_auto_recover_worker if item.get('auto_recover_worker') is None else bool(item.get('auto_recover_worker'))
            item['schedule_windows'] = _normalize_schedule_windows_payload(item.get('schedule_windows') or account_schedule_windows)
            item['schedule_runtime'] = self._schedule_runtime(item['schedule_windows'])
            item['notify_robot_name'] = self._notify_robot_name(item.get('notify_profile_name'))
            item['approval_rule_text'] = _approval_condition_text(item['approval_count_threshold'], item['approval_timeout_minutes'])
            item['config_fingerprint'] = _whatsapp_approval_binding_config_fingerprint(item)
        serialized['group_link_bindings'] = group_link_bindings
        serialized['group_links'] = [str(item.get('link') or '').strip() for item in group_link_bindings if str(item.get('link') or '').strip()]
        account_runtime_config = _whatsapp_approval_runtime_config_from_dict(_preferred_group_binding(group_link_bindings))
        for key, value in account_runtime_config.items():
            if key == 'provider_capabilities':
                serialized[key] = dict(value or {})
            else:
                serialized[key] = value
        if not default_area and group_link_bindings:
            default_area = str(group_link_bindings[0].get('area') or '').strip()
        serialized['enabled'] = bool(serialized.get('enabled'))
        serialized['group_count'] = len(serialized['group_links'])

        production_ops = production_ops or self._production_ops_daemon_snapshot()
        production_config = production_ops.get('config') or {}
        production_runtime = production_ops.get('runtime') or {}
        production_status = production_runtime.get('status') or {}
        daemon_enabled = bool(production_config.get('enabled'))
        official_bridge = official_bridge or self._official_group_bridge_summary_payload()
        official_health = official_bridge.get('health') or {}
        official_summary = official_bridge.get('summary') or {}

        account_key = str(serialized.get('account_key') or '').strip()
        runtime_state = runtime_state or self._build_whatsapp_approval_runtime_state(serialized.get('account_key') or '', worker_health=worker_health)
        if session_state is None:
            if runtime_state.get('active') and worker_health:
                session_state = self._build_whatsapp_approval_session_state(serialized.get('account_key') or '', worker_health=worker_health, include_qr_ascii=False)
            else:
                session_state = self._build_whatsapp_approval_session_state(serialized.get('account_key') or '', worker_health=worker_health if runtime_state.get('source') == 'shared' else {}, include_qr_ascii=False)
        else:
            session_state = dict(session_state or {})
        session_state = enrich_whatsapp_login_state(
            session_state,
            runtime_state=runtime_state,
            account_enabled=bool(serialized.get('enabled')),
        )

        account_provider_decision = self._resolve_wa_provider_decision(
            account=serialized,
            runtime_state=runtime_state,
            responsible_type=responsible_type,
        )
        serialized['provider_name'] = account_provider_decision.get('provider_name')
        serialized['provider_mode'] = account_provider_decision.get('provider_mode')
        serialized['provider_capabilities'] = account_provider_decision.get('provider_capabilities') or {}
        serialized['provider_decision'] = account_provider_decision
        serialized['approval_scope'] = responsible_type
        account_provider_name = str(account_provider_decision.get('provider_name') or '').strip().lower()
        account_is_baileys = account_provider_name == 'baileys'
        runtime_is_baileys = False
        for item in group_link_bindings:
            item['approval_scope'] = responsible_type
            item['target_group_label'] = str(
                item.get('group_name')
                or item.get('group_id')
                or item.get('link')
                or item.get('registration_group')
                or ''
            ).strip()

        invalid_group_links = []
        invalid_binding_areas = []
        missing_binding_notify = []
        enabled_binding_count = 0
        binding_runtime_rows: list[dict[str, Any]] = []
        for item in group_link_bindings:
            link = _normalize_whatsapp_group_invite_link(item.get('link'))
            area = str(item.get('area') or '').strip()
            notify_profile_name = str(item.get('notify_profile_name') or '').strip()
            registration_group = str(item.get('registration_group') or '').strip()
            group_id = str(item.get('group_id') or '').strip()
            binding_enabled = bool(item.get('enabled', True))
            binding_index = normalize_int_or_none(item.get('binding_index'))
            legacy_index = normalize_int_or_none(item.get('index'))
            link_ok = bool(re.fullmatch(r'https://chat\.whatsapp\.com/[A-Za-z0-9_-]+', link))
            if not link_ok:
                invalid_group_links.append(link)
            if not area:
                invalid_binding_areas.append(link)
            if not notify_profile_name:
                missing_binding_notify.append(link)
            if binding_enabled:
                enabled_binding_count += 1
            runtime_row = {
                'binding_id': str(item.get('binding_id') or '').strip(),
                'binding_index': binding_index,
                'index': legacy_index,
                'link': link,
                'group_name': str(item.get('group_name') or '').strip(),
                'area': area,
                'notify_profile_name': notify_profile_name,
                'enabled': binding_enabled,
                'registration_group': registration_group,
                'group_id': group_id,
                'notify_robot_name': item.get('notify_robot_name') or self._notify_robot_name(notify_profile_name),
                'approval_count_threshold': item.get('approval_count_threshold'),
                'approval_timeout_minutes': item.get('approval_timeout_minutes'),
                'approval_rule_text': item.get('approval_rule_text') or _approval_condition_text(
                    _coerce_positive_int(item.get('approval_count_threshold'), default_approval_count_threshold),
                    _coerce_positive_int(item.get('approval_timeout_minutes'), default_approval_timeout_minutes),
                ),
                'auto_recover_worker': bool(item.get('auto_recover_worker')),
                'schedule_windows': item.get('schedule_windows') or [],
                'schedule_runtime': item.get('schedule_runtime') or self._schedule_runtime(item.get('schedule_windows') or []),
                'config_fingerprint': item.get('config_fingerprint') or _whatsapp_approval_binding_config_fingerprint(item),
                'link_ok': link_ok,
            }
            for key in WHATSAPP_APPROVAL_RUNTIME_CONFIG_KEYS:
                runtime_row[key] = item.get(key) or serialized.get(key) or ''
            runtime_row['provider_capabilities'] = dict(item.get('provider_capabilities') or serialized.get('provider_capabilities') or {}) if isinstance(item.get('provider_capabilities') or serialized.get('provider_capabilities'), dict) else {}
            runtime_row['baileys_enabled'] = item.get('baileys_enabled') if item.get('baileys_enabled') is not None else serialized.get('baileys_enabled')
            for key in (
                'identity_status', 'identity_rebuild_reason', 'identity_resolved_at', 'identity_resolved_by',
                'last_probe_status', 'last_probe_reason', 'last_probe_at', 'last_probe_had_group_id',
                'last_probe_had_group_name', 'last_probe_self_participant_found', 'last_probe_self_is_admin',
                'last_probe_can_manage_membership_requests', 'last_probe_member_count', 'runtime_probe_group_id',
                'runtime_probe_group_name', 'queue_status', 'queue_confidence',
            ):
                if key in item:
                    runtime_row[key] = item.get(key)
            binding_runtime_rows.append(runtime_row)

        persistent_permission_failure_locks = self._approval_binding_repeated_unresolved_failure_locks(
            account_key,
            [str(item.get('binding_id') or '').strip() for item in binding_runtime_rows],
        )

        if account_is_baileys:
            runtime_provider_name = str(runtime_state.get('provider_name') or '').strip().lower()
            runtime_is_baileys = bool(
                runtime_provider_name == 'baileys'
                or str(runtime_state.get('mode') or '').strip() == 'baileys_provider_runtime'
                or str(runtime_state.get('source') or '').strip() == 'baileys_poc'
            )
            baileys_base_url = str(
                (runtime_state.get('base_url') if runtime_is_baileys else '')
                or serialized.get('baileys_base_url')
                or serialized.get('provider_base_url')
                or ''
            ).strip().rstrip('/')
            baileys_account_id = str(
                (runtime_state.get('baileys_account_id') if runtime_is_baileys else '')
                or (runtime_state.get('provider_account_id') if runtime_is_baileys else '')
                or (runtime_state.get('account_id') if runtime_is_baileys else '')
                or serialized.get('baileys_account_id')
                or serialized.get('provider_account_id')
                or serialized.get('account_id')
                or ''
            ).strip()
            baileys_configured = bool(runtime_is_baileys and runtime_state.get('configured')) or bool(baileys_base_url and baileys_account_id)
            if not runtime_is_baileys and (baileys_base_url or baileys_account_id):
                runtime_state = {
                    **runtime_state,
                    'mode': 'baileys_provider_runtime',
                    'source': 'baileys_config',
                    'provider_name': 'baileys',
                    'provider_mode': account_provider_decision.get('provider_mode'),
                    'baileys_account_id': baileys_account_id,
                    'provider_account_id': baileys_account_id,
                    'account_id': baileys_account_id,
                    'base_url': baileys_base_url or None,
                    'configured': bool(baileys_configured),
                    'active': bool(baileys_base_url),
                    'status': 'configured' if baileys_base_url else 'not_started',
                    'ready': False,
                    'authenticated': False,
                    'health_error': None,
                    'status_text': 'Baileys provider 已配置，等待账号登录' if baileys_base_url else 'Baileys provider 服务地址未配置',
                }
                runtime_is_baileys = True
                session_state = enrich_whatsapp_login_state(
                    session_state,
                    runtime_state=runtime_state,
                    account_enabled=bool(serialized.get('enabled')),
                )
            baileys_runtime_status = str(runtime_state.get('status') or '').strip() if runtime_is_baileys else ''
            baileys_health_error = str(runtime_state.get('health_error') or '').strip() if runtime_is_baileys else ''
            service_ready = bool(baileys_configured and not baileys_health_error and baileys_runtime_status not in {'unavailable', 'runtime_unavailable'})
            service_scope = {
                'code': 'baileys_provider',
                'label': 'Baileys 账号运行时',
                'ready': bool(service_ready),
                'detail': 'Baileys provider 已配置，可按账号路由审批/刷新' if service_ready else 'Baileys provider 未就绪，请先恢复 POC 服务或补齐账号运行时配置',
                'runtime': {
                    'provider_name': 'baileys',
                    'provider_mode': account_provider_decision.get('provider_mode'),
                    'baileys_account_id': baileys_account_id or None,
                    'base_url': baileys_base_url or None,
                    'status': baileys_runtime_status or None,
                    'ready': bool(runtime_is_baileys and runtime_state.get('ready')),
                    'authenticated': bool(runtime_is_baileys and runtime_state.get('authenticated')),
                    'configured': bool(baileys_configured),
                    'health_error': baileys_health_error or None,
                },
            }
            if not runtime_is_baileys and not baileys_base_url and not baileys_account_id:
                service_ready = responsible_type == 'registration_group'
                service_scope['ready'] = bool(service_ready)
                service_scope['detail'] = 'Baileys provider 未配置，沿用账号本地 runtime/session 快照' if service_ready else service_scope['detail']
                service_scope['runtime']['status'] = str(runtime_state.get('status') or '').strip() or None
                service_scope['runtime']['ready'] = bool(runtime_state.get('ready'))
                service_scope['runtime']['authenticated'] = bool(runtime_state.get('authenticated'))
        elif responsible_type == 'registration_group':
            service_ready = True
            service_scope = {
                'code': 'registration_group_console',
                'label': '注册群账号级守护',
                'ready': service_ready,
                'detail': '当前账号已具备独立守护配置；共享 daemon 运行态请以上方实时状态卡片为准' if service_ready else '当前账号守护配置未就绪',
                'runtime': {
                    'launch_agent_installed': bool(production_runtime.get('launch_agent_installed')),
                    'checked_at': (production_status.get('status') or {}).get('checked_at') if isinstance(production_status.get('status'), dict) else production_status.get('checked_at'),
                    'runtime_mode': 'shared-daemon-status + account-scoped-configuration',
                },
            }
        else:
            service_ready = official_bridge.get('configured') and official_health.get('status') == 'healthy'
            service_scope = {
                'code': 'official_group_bridge',
                'label': '官方群审批桥接台',
                'ready': bool(service_ready),
                'detail': '官方群 bridge 健康，可继续接统一调度' if service_ready else '官方群 bridge 未就绪，需先恢复 bridge 服务',
                'runtime': {
                    'mode': official_health.get('mode'),
                    'pending_count': official_summary.get('pending_count'),
                    'resolved_count': official_summary.get('resolved_count'),
                },
            }

        provider_monitor_enabled = True if account_is_baileys else daemon_enabled
        monitor_runtime_active = bool(
            (service_scope.get('runtime') or {}).get('configured')
            or (account_is_baileys and (service_scope.get('runtime') or {}).get('ready'))
            or (account_is_baileys and (service_scope.get('runtime') or {}).get('authenticated'))
        ) if account_is_baileys else daemon_enabled and bool(production_runtime.get('launch_agent_installed'))

        active_binding_count = sum(1 for item in binding_runtime_rows if item.get('enabled'))
        all_binding_areas_ok = not invalid_binding_areas
        all_binding_notify_ok = not missing_binding_notify
        all_binding_rules_ok = all(
            _coerce_positive_int(item.get('approval_count_threshold'), default_approval_count_threshold) > 0
            and _coerce_positive_int(item.get('approval_timeout_minutes'), default_approval_timeout_minutes) > 0
            for item in binding_runtime_rows
        )
        has_monitored_bindings = enabled_binding_count > 0
        original_group_link_bindings = [dict(item or {}) for item in (serialized.get('group_link_bindings') or [])]
        binding_live_probes: list[dict[str, Any]] = []
        for item in original_group_link_bindings:
            daemon_probe = self._binding_probe_from_production_ops_status(
                production_ops,
                responsible_type=responsible_type,
                binding=item,
                account_key=account_key,
            )
            if daemon_probe:
                probe = daemon_probe
            elif skip_live_probe:
                probe = {}
            else:
                probe = self._apply_live_group_identity_to_binding(
                    dict(item or {}),
                    responsible_type=responsible_type,
                    runtime_state=runtime_state,
                    session_state=session_state,
                    allow_shared_fallback=responsible_type == 'registration_group',
                    attempts=1 if responsible_type == 'registration_group' else 3,
                    timeout_seconds=2.0,
                )
            binding_live_probes.append(probe if isinstance(probe, dict) else {})
        for runtime_row, binding, probe in zip(binding_runtime_rows, original_group_link_bindings, binding_live_probes):
            runtime_provider_decision = self._resolve_wa_provider_decision(
                account=serialized,
                binding=runtime_row,
                runtime_state=runtime_state,
                responsible_type=responsible_type,
            )
            runtime_row['provider_name'] = runtime_provider_decision.get('provider_name')
            runtime_row['provider_mode'] = runtime_provider_decision.get('provider_mode')
            runtime_row['provider_capabilities'] = runtime_provider_decision.get('provider_capabilities') or {}
            runtime_row['provider_decision'] = runtime_provider_decision
            stored_group_name = str(binding.get('group_name') or '').strip()
            stored_group_id = str(binding.get('group_id') or '').strip()
            live_group_name = str((probe or {}).get('group_name') or '').strip()
            live_group_id = str((probe or {}).get('group_id') or '').strip()
            anchor_identity = self._lookup_binding_cycle_anchor_identity(
                production_ops=production_ops,
                responsible_type=responsible_type,
                binding=runtime_row,
                probe=probe if isinstance(probe, dict) else {},
            )
            anchor_group_name = str(anchor_identity.get('group_name') or '').strip()
            anchor_group_id = str(anchor_identity.get('group_id') or '').strip()
            if not live_group_name and not stored_group_name and anchor_group_name:
                live_group_name = anchor_group_name
            if not live_group_id and not stored_group_id and anchor_group_id:
                live_group_id = anchor_group_id
            display_group_name = live_group_name
            if _looks_like_whatsapp_group_jid(display_group_name) and stored_group_name and not _looks_like_whatsapp_group_jid(stored_group_name):
                display_group_name = stored_group_name
            runtime_row['runtime_probe_group_name'] = display_group_name or live_group_name
            runtime_row['runtime_probe_group_id'] = live_group_id
            runtime_row['approval_scope'] = responsible_type
            runtime_row['group_name'] = display_group_name or stored_group_name
            runtime_row['group_id'] = live_group_id or str(binding.get('group_id') or '').strip()
            runtime_row['target_group_label'] = str(
                runtime_row.get('group_name')
                or runtime_row.get('group_id')
                or runtime_row.get('link')
                or runtime_row.get('registration_group')
                or ''
            ).strip()
            runtime_row['cycle_anchor_at'] = self._lookup_binding_cycle_anchor(
                production_ops=production_ops,
                responsible_type=responsible_type,
                binding=runtime_row,
                probe=probe if isinstance(probe, dict) else {},
            )
            runtime_row.update(self._build_binding_next_approval_runtime(
                responsible_type=responsible_type,
                binding=runtime_row,
                probe=probe if isinstance(probe, dict) else {},
            ))
            if responsible_type in {'registration_group', 'official_group'}:
                self._apply_approval_queue_truth_to_binding(
                    account_key,
                    runtime_row,
                    account={**dict(serialized or {}), 'runtime_state': runtime_state, 'responsible_type': responsible_type},
                    production_ops=production_ops,
                    allow_live_refresh=not read_only,
                )
            binding_enabled = bool(runtime_row.get('enabled'))
            runtime_row['monitoring_effective'] = bool(serialized.get('enabled')) and provider_monitor_enabled and binding_enabled
            if not serialized.get('enabled'):
                runtime_row['monitoring_status_text'] = '账号已关闭'
                runtime_row.update(self._paused_binding_next_approval_runtime(
                    pending_count=runtime_row.get('next_approval_pending_count') or 0,
                    batch_size=runtime_row.get('next_approval_batch_size') or runtime_row.get('approval_count_threshold') or 1,
                    timeout_minutes=runtime_row.get('next_approval_timeout_minutes') or runtime_row.get('approval_timeout_minutes') or 1,
                    reason_code='account_monitor_disabled',
                    eta_text='已暂停',
                ))
            elif not provider_monitor_enabled:
                runtime_row['monitoring_status_text'] = '未生效'
                runtime_row.update(self._paused_binding_next_approval_runtime(
                    pending_count=runtime_row.get('next_approval_pending_count') or 0,
                    batch_size=runtime_row.get('next_approval_batch_size') or runtime_row.get('approval_count_threshold') or 1,
                    timeout_minutes=runtime_row.get('next_approval_timeout_minutes') or runtime_row.get('approval_timeout_minutes') or 1,
                    reason_code='global_monitor_disabled',
                    eta_text='已暂停',
                ))
            elif not binding_enabled:
                runtime_row['monitoring_status_text'] = '不监控'
                runtime_row.update(self._paused_binding_next_approval_runtime(
                    pending_count=runtime_row.get('next_approval_pending_count') or 0,
                    batch_size=runtime_row.get('next_approval_batch_size') or runtime_row.get('approval_count_threshold') or 1,
                    timeout_minutes=runtime_row.get('next_approval_timeout_minutes') or runtime_row.get('approval_timeout_minutes') or 1,
                    reason_code='binding_monitor_disabled',
                    eta_text='已暂停',
                ))
            else:
                runtime_row['monitoring_status_text'] = '监控中'

        if (
            account_is_baileys
            and responsible_type in {'registration_group', 'official_group'}
            and not str(runtime_state.get('health_error') or '').strip()
            and self._baileys_session_can_be_marked_operational(session_state, runtime_state)
        ):
            operational_probe_indices = [
                idx
                for idx, (runtime_row, probe) in enumerate(zip(binding_runtime_rows, binding_live_probes))
                if runtime_row.get('enabled') is not False
                and self._binding_has_recent_baileys_operational_probe(runtime_row, probe)
            ]
            if operational_probe_indices:
                for idx in operational_probe_indices:
                    if not self._binding_probe_has_group_evidence(binding_live_probes[idx]):
                        binding_live_probes[idx] = self._stored_binding_probe_payload(binding_runtime_rows[idx])
                runtime_state, session_state = self._mark_baileys_session_operational(
                    runtime_state,
                    session_state,
                    message='Baileys 最近一次真实群探针已验证，账号可用于当前群操作。',
                )
                service_scope['ready'] = True
                service_scope['detail'] = 'Baileys 最近一次真实群探针已验证，可按账号路由审批/刷新'
                service_runtime = service_scope.get('runtime') if isinstance(service_scope.get('runtime'), dict) else {}
                service_runtime.update({
                    'ready': True,
                    'authenticated': True,
                    'status': 'running',
                    'configured': True,
                })
                service_scope['runtime'] = service_runtime
                monitor_runtime_active = True

        membership_verifier = self._approval_membership_verifier_state(
            responsible_type=str(serialized.get('responsible_type') or '').strip(),
            production_ops=production_ops,
            official_bridge=official_bridge,
            runtime_state=runtime_state,
            session_state=session_state,
            account_key=account_key,
        )
        if str(serialized.get('responsible_type') or '').strip() == 'official_group':
            binding_verifiers = [
                self._official_group_binding_membership_verifier_state(
                    item,
                    runtime_state=runtime_state,
                    session_state=session_state,
                    live_probe=probe,
                    allow_live_probe=not skip_live_probe,
                )
                for item, probe in zip(binding_runtime_rows, binding_live_probes)
            ]
            account_membership_verifier = self._official_group_account_membership_verifier(
                binding_verifiers,
                enabled_binding_count=enabled_binding_count,
            )
        else:
            binding_verifiers = [
                self._binding_membership_verifier_state(
                    item,
                    membership_verifier,
                    responsible_type=str(serialized.get('responsible_type') or '').strip(),
                    production_ops=production_ops,
                    live_probe=probe,
                    runtime_state=runtime_state,
                    session_state=session_state,
                )
                for item, probe in zip(binding_runtime_rows, binding_live_probes)
            ]
            monitored_binding_verifiers = [
                verifier for item, verifier in zip(binding_runtime_rows, binding_verifiers) if item.get('enabled')
            ]
            ready_binding_verifiers = [verifier for verifier in monitored_binding_verifiers if verifier.get('ready')]
            bindings_membership_ready = bool(monitored_binding_verifiers) and len(ready_binding_verifiers) == len(monitored_binding_verifiers)
            if not monitored_binding_verifiers:
                bindings_membership_ready = bool(membership_verifier.get('ready')) if not binding_runtime_rows else False
            if monitored_binding_verifiers:
                if bindings_membership_ready:
                    representative_verifier = ready_binding_verifiers[0]
                    account_membership_verifier = {
                        **membership_verifier,
                        'ready': True,
                        'requires_manual_seed': False,
                        'status': representative_verifier.get('status') or membership_verifier.get('status') or 'live_probe_ready',
                        'detail': representative_verifier.get('detail') or membership_verifier.get('detail') or '-',
                        'source': representative_verifier.get('source') or membership_verifier.get('source'),
                        'probe': dict(representative_verifier.get('probe') or membership_verifier.get('probe') or {}),
                        'binding_count': len(monitored_binding_verifiers),
                    }
                else:
                    first_failed = next((item for item in monitored_binding_verifiers if not item.get('ready')), monitored_binding_verifiers[0])
                    ready_count = len(ready_binding_verifiers)
                    account_membership_verifier = {
                        **membership_verifier,
                        'ready': False,
                        'requires_manual_seed': True,
                        'status': first_failed.get('status') or membership_verifier.get('status') or 'probe_unavailable',
                        'detail': f'当前仅有 {ready_count}/{len(monitored_binding_verifiers)} 条注册群绑定完成真实成员/管理员权限校验；{first_failed.get("detail") or membership_verifier.get("detail") or "仍有绑定未拿到实时探针结果。"}',
                        'source': first_failed.get('source') or membership_verifier.get('source'),
                        'probe': dict(first_failed.get('probe') or membership_verifier.get('probe') or {}),
                        'binding_count': len(monitored_binding_verifiers),
                    }
            else:
                account_membership_verifier = {
                    **membership_verifier,
                    'ready': bindings_membership_ready,
                    'requires_manual_seed': bool(membership_verifier.get('requires_manual_seed')),
                    'binding_count': len(monitored_binding_verifiers),
                }
        for item, verifier in zip(binding_runtime_rows, binding_verifiers):
            normalized_verifier = dict(verifier or {})
            persistent_lock = persistent_permission_failure_locks.get(str(item.get('binding_id') or '').strip())
            if isinstance(persistent_lock, dict) and persistent_lock.get('active') is True:
                truth = item.get('approval_queue_truth') if isinstance(item.get('approval_queue_truth'), dict) else {}
                current_truth = truth.get('current_truth') if isinstance(truth.get('current_truth'), dict) else {}
                has_current_truth_count = any(normalize_int_or_none(value) is not None for value in (
                    truth.get('pending_count'),
                    current_truth.get('pending_count'),
                ))
                probe = normalized_verifier.get('probe') if isinstance(normalized_verifier.get('probe'), dict) else {}
                stable_group_identity = any(_looks_like_whatsapp_group_jid(value) for value in (
                    item.get('group_id'),
                    item.get('runtime_probe_group_id'),
                    item.get('registration_group'),
                    probe.get('group_id'),
                ))
                verifier_status = str(normalized_verifier.get('status') or '').strip()
                admin_confirmed = bool(
                    normalized_verifier.get('has_admin_permission') is True
                    or normalized_verifier.get('is_admin') is True
                    or probe.get('self_is_admin') is True
                    or probe.get('can_manage_membership_requests') is True
                    or (
                        normalized_verifier.get('ready') is True
                        and verifier_status in {'mapped_live_probe_ready', 'live_probe_ready'}
                    )
                )
                locked_reason = str(persistent_lock.get('reason_code') or '').strip()
                exact_permission_failure = locked_reason in {'not_group_member', 'not_group_admin'}
                if exact_permission_failure or (
                    not has_current_truth_count
                    and not (stable_group_identity and admin_confirmed)
                ):
                    detail = '当前审批账号无法读取这个群的待审批队列。请确认账号是否在群组内或拥有管理员权限。'
                    permission_status = (
                        locked_reason
                        if locked_reason in {'not_group_member', 'not_group_admin'}
                        else str(normalized_verifier.get('status') or '').strip()
                    )
                    if permission_status not in {'not_group_member', 'not_group_admin'}:
                        permission_status = 'not_group_admin'
                    if permission_status == 'not_group_member':
                        detail = '当前审批账号已不在目标群，请让群管理员重新添加该账号并授予管理员权限。'
                    elif permission_status == 'not_group_admin':
                        detail = '当前审批账号仍在目标群，但已不是群管理员，请重新授予管理员权限。'
                    item['manual_permission_probe_error'] = dict(persistent_lock)
                    normalized_verifier = {
                        **normalized_verifier,
                        'ready': False,
                        'requires_manual_seed': True,
                        'status': permission_status,
                        'detail': detail,
                        'safe_detail': detail,
                        'probe': {
                            **probe,
                            'status': permission_status,
                            'permission_status': permission_status,
                            'reason_code': permission_status,
                            'error_message': detail,
                        },
                    }
            group_name = str(item.get('group_name') or '').strip()
            if group_name and not str(normalized_verifier.get('group_name') or '').strip():
                normalized_verifier['group_name'] = group_name
                normalized_verifier['current_group_name'] = group_name
            item['membership_verifier'] = serialize_membership_verifier(normalized_verifier)
            manual_permission_error = item.get('manual_permission_probe_error')
            if isinstance(manual_permission_error, dict) and manual_permission_error.get('active') is True:
                permission_status = str(item['membership_verifier'].get('status') or '').strip()
                permission_detail = (
                    '当前审批账号已不在目标群，请让群管理员重新添加该账号并授予管理员权限。'
                    if permission_status == 'not_group_member'
                    else '当前审批账号仍在目标群，但已不是群管理员，请重新授予管理员权限。'
                    if permission_status == 'not_group_admin'
                    else '当前审批账号无法读取这个群的待审批队列。请确认账号是否在群组内或拥有管理员权限。'
                )
                item['membership_verifier']['detail'] = permission_detail
                item['membership_verifier']['safe_detail'] = permission_detail
            if responsible_type == 'registration_group' and isinstance(item.get('approval_queue_truth'), dict):
                item['approval_queue_truth']['membership_safe_detail'] = item['membership_verifier'].get('safe_detail')

        if responsible_type in {'registration_group', 'official_group'} and not read_only:
            updated_bindings = self._persist_registration_group_binding_live_names(
                str(serialized.get('account_key') or '').strip(),
                original_group_link_bindings,
                binding_runtime_rows,
                binding_verifiers,
            )
            if updated_bindings != original_group_link_bindings:
                serialized['group_link_bindings'] = updated_bindings
                serialized['group_links'] = [
                    str(item.get('link') or '').strip()
                    for item in serialized['group_link_bindings']
                    if str(item.get('link') or '').strip()
                ]

        verification_checks = [
            {
                'code': 'group_link_format',
                'ok': not invalid_group_links,
                'detail': '群链接格式有效' if not invalid_group_links else f'存在 {len(invalid_group_links)} 条群链接格式异常',
            },
            {
                'code': 'group_link_area_binding',
                'ok': all_binding_areas_ok,
                'detail': '每条群链接都已绑定地区' if all_binding_areas_ok else f'存在 {len(invalid_binding_areas)} 条群链接未绑定地区',
            },
            {
                'code': 'binding_notify_robot',
                'ok': all_binding_notify_ok,
                'detail': '每条群绑定都已配置通知机器人' if all_binding_notify_ok else f'存在 {len(missing_binding_notify)} 条群绑定未配置通知机器人',
            },
            {
                'code': 'binding_approval_rule',
                'ok': all_binding_rules_ok,
                'detail': '每条群绑定都已配置审批条件' if all_binding_rules_ok else '存在群绑定审批条件不完整',
            },
            {
                'code': 'binding_schedule_window',
                'ok': True,
                'detail': f'当前有 {active_binding_count}/{enabled_binding_count or 0} 条已开启群绑定处于监控中',
            },
            {
                'code': 'binding_monitor_enabled',
                'ok': has_monitored_bindings,
                'detail': f'当前已开启 {enabled_binding_count}/{len(binding_runtime_rows) or 0} 条群绑定监控' if has_monitored_bindings else '当前未开启任何群绑定监控',
            },
            {
                'code': 'service_scope_ready',
                'ok': bool(service_scope.get('ready')),
                'detail': service_scope.get('detail') or '-',
            },
            {
                'code': 'admin_membership_verification',
                'ok': bool(account_membership_verifier.get('ready')),
                'detail': account_membership_verifier.get('detail') or '-',
            },
        ]

        membership_ready = bool(account_membership_verifier.get('ready'))
        config_ready = (
            not invalid_group_links
            and all_binding_areas_ok
            and all_binding_notify_ok
            and all_binding_rules_ok
            and has_monitored_bindings
            and bool(service_scope.get('ready'))
        )
        full_ready = bool(config_ready and membership_ready)
        if invalid_group_links:
            verification_status = 'invalid_group_links'
        elif not has_monitored_bindings:
            verification_status = 'monitor_disabled'
        elif full_ready:
            verification_status = 'ready'
        elif config_ready:
            verification_status = 'login_unready'
        else:
            verification_status = 'service_unready'

        account_schedule_runtime = self._schedule_runtime(account_schedule_windows)
        enabled_binding_rows = [item for item in binding_runtime_rows if item.get('enabled')]
        if enabled_binding_rows:
            account_active_now = True
            schedule_runtime = {
                'configured': False,
                'active_now': True,
                'status': 'always_on',
                'label': '账号已开启，全部已启用群绑定持续监控中',
                'active_binding_count': active_binding_count,
                'enabled_binding_count': len(enabled_binding_rows),
            }
        else:
            account_active_now = bool(account_schedule_runtime.get('active_now')) if account_schedule_windows else (True if not binding_runtime_rows else False)
            schedule_runtime = account_schedule_runtime
        representative_binding = _preferred_group_binding(binding_runtime_rows)
        representative_schedule_windows = list(representative_binding.get('schedule_windows') or account_schedule_windows)
        serialized['schedule_active_now'] = bool(account_active_now)
        serialized['schedule_runtime'] = schedule_runtime
        serialized['daemon_enabled'] = daemon_enabled
        serialized['provider_monitor_enabled'] = provider_monitor_enabled
        serialized['monitor_runtime_active'] = monitor_runtime_active
        serialized['area'] = str(representative_binding.get('area') or default_area).strip()
        serialized['notify_profile_name'] = str(representative_binding.get('notify_profile_name') or default_notify_profile_name).strip()
        serialized['notify_robot_name'] = str(representative_binding.get('notify_robot_name') or self._notify_robot_name(serialized['notify_profile_name'])).strip()
        serialized['target_group_label'] = str(
            representative_binding.get('group_name')
            or representative_binding.get('group_id')
            or representative_binding.get('link')
            or representative_binding.get('registration_group')
            or ''
        ).strip()
        serialized['approval_count_threshold'] = _coerce_positive_int(representative_binding.get('approval_count_threshold'), default_approval_count_threshold)
        serialized['approval_timeout_minutes'] = _coerce_positive_int(representative_binding.get('approval_timeout_minutes'), default_approval_timeout_minutes)
        serialized['approval_rule_text'] = representative_binding.get('approval_rule_text') or _approval_condition_text(serialized['approval_count_threshold'], serialized['approval_timeout_minutes'])
        serialized['approval_condition_text'] = serialized['approval_rule_text']
        serialized['auto_recover_worker'] = default_auto_recover_worker if representative_binding.get('auto_recover_worker') is None else bool(representative_binding.get('auto_recover_worker'))
        serialized['schedule_windows'] = representative_schedule_windows
        serialized['group_binding_runtimes'] = binding_runtime_rows
        serialized['runtime_state'] = runtime_state
        serialized['session_state'] = session_state

        production_ready = bool(config_ready and session_state.get('login_verified'))
        login_check_status = str(session_state.get('login_check_status') or '').strip()
        if production_ready:
            verification_status = 'ready'
        elif login_check_status == 'account_restricted':
            verification_status = 'account_restricted'
        elif login_check_status == 'auth_failed':
            verification_status = 'auth_failed'
        elif login_check_status == 'runtime_recovering':
            verification_status = 'runtime_recovering'
        elif invalid_group_links:
            verification_status = 'invalid_group_links'
        elif not has_monitored_bindings:
            verification_status = 'monitor_disabled'
        elif not config_ready:
            verification_status = 'service_unready'
        else:
            verification_status = 'login_unready'

        if not serialized['enabled']:
            runtime_status = 'disabled'
            status_color = 'gray'
            status_text = '已关闭'
            next_action = '如需纳入自动监控，请先开启账号'
        elif not provider_monitor_enabled:
            runtime_status = 'blocked'
            status_color = 'gray'
            status_text = '未生效'
            next_action = '先开启 WhatsApp 总监控开关后，分群监控才会实际生效'
        elif not has_monitored_bindings:
            runtime_status = 'blocked'
            status_color = 'gray'
            status_text = '未监控'
            next_action = '至少开启 1 个群监控后再纳入自动审批'
        elif invalid_group_links:
            runtime_status = 'blocked'
            status_color = 'amber'
            status_text = '待补齐'
            next_action = '先补齐群绑定配置，再纳入统一调度'
        elif login_check_status == 'account_restricted':
            runtime_status = 'blocked'
            status_color = 'red'
            status_text = '账号受限'
            next_action = '先在手机端核查封禁/限制状态，确认恢复后再重新登录'
        elif login_check_status == 'auth_failed':
            runtime_status = 'blocked'
            status_color = 'amber'
            status_text = '登录异常'
            next_action = '先重新登录账号，再继续可用性检测'
        elif login_check_status in {'waiting_for_scan', 'waiting_for_scan_qr_ready', 'waiting_for_scan_qr_pending'}:
            runtime_status = 'starting'
            status_color = 'amber'
            status_text = '待扫码'
            next_action = '请扫码完成登录后再继续可用性检测'
        elif login_check_status == 'auto_recovering':
            runtime_status = 'blocked'
            status_color = 'blue'
            status_text = '自动恢复中'
            next_action = '系统正在自动切换这个账号，通常几秒内会自动恢复'
        elif login_check_status == 'runtime_recovering':
            runtime_status = 'recovering'
            status_color = 'blue'
            status_text = '登录态恢复中'
            next_action = '账号已有服务器登录态，运行时正在恢复；请稍候刷新，不要重新扫码'
        elif login_check_status == 'session_mismatch':
            runtime_status = 'blocked'
            status_color = 'blue'
            status_text = '待切换'
            next_action = '点“生成二维码”或“刷新状态”，切到这个账号后再继续可用性检测'
        elif not config_ready:
            runtime_status = 'blocked'
            status_color = 'amber'
            status_text = '待补齐'
            next_action = '先补齐群绑定配置，再纳入统一调度'
        elif not session_state.get('login_verified'):
            runtime_status = 'blocked'
            status_color = 'amber'
            status_text = '待登录'
            next_action = '先完成扫码登录并通过可用性检测'
        else:
            runtime_status = 'active'
            status_color = 'green'
            status_text = '运行中'
            next_action = '可直接纳入统一调度'

        serialized['verification_status'] = verification_status
        serialized['verification_status_label'] = {
            'ready': '可投产',
            'invalid_group_links': '群链接配置异常',
            'monitor_disabled': '未启用监控群',
            'service_unready': '服务未就绪',
            'login_unready': '待登录',
            'runtime_recovering': '恢复中',
            'account_restricted': '账号受限',
            'auth_failed': '登录异常',
        }.get(verification_status, verification_status)
        serialized['membership_verifier'] = account_membership_verifier
        serialized['verification_scope_text'] = account_membership_verifier.get('detail') if account_membership_verifier.get('ready') else '当前控制台配置与调度就绪度已完成；逐群映射或真实校验结果见下方“真实校验”明细。'
        serialized['verification_checks'] = verification_checks
        serialized['service_scope'] = service_scope
        serialized['runtime_status'] = runtime_status
        serialized['status_color'] = status_color
        serialized['status_text'] = status_text
        serialized['next_action'] = next_action
        if not read_only:
            try:
                self._sync_wa_account_projection(serialized, runtime_state=serialized.get('runtime_state') or runtime_state)
                for binding in list(serialized.get('group_binding_runtimes') or serialized.get('group_link_bindings') or []):
                    if isinstance(binding, dict):
                        self._sync_wa_group_binding_projection(
                            str(serialized.get('account_key') or '').strip(),
                            binding,
                            responsible_type=str(serialized.get('responsible_type') or '').strip(),
                        )
            except Exception:
                pass
        return serialized

    def _ops_user_can_manage_whatsapp_approval_account(self, current_user: Optional[Dict[str, Any]], row: Optional[Dict[str, Any]]) -> bool:
        if not current_user:
            return True
        role = str(current_user.get('role') or '').strip().lower()
        if role in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_INTERNAL}:
            return True
        if not ops_role_is_business(role):
            return False
        if not row:
            return False
        assigned_user_ids = self._whatsapp_approval_assigned_customer_service_ids_from_row(row)
        if str(current_user.get('user_id') or '').strip() in assigned_user_ids:
            return True
        return str(row.get('responsible_type') or '').strip() == 'official_group' and not assigned_user_ids

    @staticmethod
    def _normalize_customer_service_assignment_ids(value: Any) -> List[str]:
        raw_values: List[Any] = []
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            raw_values = list(value)
        else:
            raw_text = str(value or '').strip()
            if not raw_text:
                return []
            if raw_text.startswith('['):
                try:
                    decoded = json.loads(raw_text)
                except Exception:
                    decoded = None
                if isinstance(decoded, list):
                    raw_values = decoded
                else:
                    raw_values = [raw_text]
            elif ',' in raw_text:
                raw_values = raw_text.split(',')
            else:
                raw_values = [raw_text]
        normalized: List[str] = []
        seen: set[str] = set()
        for item in raw_values:
            value_text = str(item or '').strip()
            if not value_text or value_text in seen:
                continue
            seen.add(value_text)
            normalized.append(value_text)
        return normalized

    @classmethod
    def _serialize_customer_service_assignment_ids(cls, user_ids: Any) -> str:
        normalized = cls._normalize_customer_service_assignment_ids(user_ids)
        if len(normalized) <= 1:
            return normalized[0] if normalized else ''
        return json.dumps(normalized, ensure_ascii=False)

    @classmethod
    def _whatsapp_approval_assigned_customer_service_ids_from_row(cls, row: Optional[Dict[str, Any]]) -> List[str]:
        if not isinstance(row, dict):
            return []
        explicit = row.get('assigned_customer_service_user_ids')
        explicit_ids = cls._normalize_customer_service_assignment_ids(explicit)
        if explicit_ids:
            return explicit_ids
        return cls._normalize_customer_service_assignment_ids(row.get('assigned_customer_service_user_id'))

    def _require_whatsapp_approval_account_access(self, account_key: str, current_user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        with self.db.connect() as conn:
            raw = conn.execute(
                "SELECT account_key, responsible_type, assigned_customer_service_user_id FROM whatsapp_approval_accounts WHERE account_key = ?",
                (normalized_key,),
            ).fetchone()
        if raw is None:
            raise HTTPException(status_code=404, detail='account_not_found')
        row = dict(raw)
        if not self._ops_user_can_manage_whatsapp_approval_account(current_user, row):
            raise HTTPException(status_code=403, detail='whatsapp_approval_account_not_assigned')
        return row

    def _list_customer_service_options(self) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, username, display_name, role, enabled
                FROM ops_users
                WHERE role IN (?, ?) AND enabled = 1
                ORDER BY COALESCE(NULLIF(display_name, ''), username) ASC, username ASC
                """,
                tuple(sorted(OPS_AUTH_BUSINESS_ROLES)),
            ).fetchall()
        return [
            {
                'user_id': row['user_id'],
                'username': row['username'],
                'display_name': row['display_name'] or row['username'],
                'role': row['role'],
                'enabled': bool(row['enabled']),
            }
            for row in rows
        ]

    def list_whatsapp_approval_account_options(self) -> Dict[str, Any]:
        area_option_payload = self.list_whatsapp_approval_area_options()
        return {
            'notify_robot_options': self._list_notify_robot_options(),
            'area_options': area_option_payload['options'],
            'area_option_source': area_option_payload['source_options'],
            'customer_service_options': self._list_customer_service_options(),
        }

    def _resolve_customer_service_assignment(self, user_id: str) -> Dict[str, Any]:
        normalized_user_id = str(user_id or '').strip()
        if not normalized_user_id:
            raise HTTPException(status_code=400, detail='assigned_customer_service_required')
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT user_id, username, display_name, role, enabled FROM ops_users WHERE user_id = ?",
                (normalized_user_id,),
            ).fetchone()
        if row is None or not ops_role_is_business(row['role']) or not bool(row['enabled']):
            raise HTTPException(status_code=400, detail='assigned_customer_service_invalid')
        return {
            'user_id': row['user_id'],
            'username': row['username'],
            'display_name': row['display_name'] or row['username'],
        }

    def _resolve_customer_service_assignments(self, user_ids: Any, *, required: bool = False) -> Dict[str, Any]:
        normalized_ids = self._normalize_customer_service_assignment_ids(user_ids)
        if required and not normalized_ids:
            raise HTTPException(status_code=400, detail='assigned_customer_service_required')
        resolved = [self._resolve_customer_service_assignment(user_id) for user_id in normalized_ids]
        usernames = [str(item.get('username') or '').strip() for item in resolved if str(item.get('username') or '').strip()]
        display_names = [str(item.get('display_name') or item.get('username') or '').strip() for item in resolved if str(item.get('display_name') or item.get('username') or '').strip()]
        return {
            'user_ids': [str(item.get('user_id') or '').strip() for item in resolved if str(item.get('user_id') or '').strip()],
            'user_id': self._serialize_customer_service_assignment_ids([item.get('user_id') for item in resolved]),
            'username': '、'.join(usernames),
            'display_name': '、'.join(display_names),
        }

    def _registration_group_cutover_applies(self, account_key: str, binding_index: int) -> bool:
        if self.whatsapp_registration_group_approval_cutover_enabled:
            return False
        try:
            account_row = self._get_whatsapp_approval_account_runtime_row_lightweight(account_key)
        except Exception:
            return False
        if str(account_row.get('responsible_type') or '').strip() != 'registration_group':
            return False
        bindings = account_row.get('group_binding_runtimes') if isinstance(account_row.get('group_binding_runtimes'), list) else account_row.get('group_link_bindings')
        return isinstance(bindings, list) and 0 <= int(binding_index) < len(bindings)

    def _ensure_registration_group_cutover_enabled(self, account_key: str, binding_index: int, operation: str) -> None:
        if not self._registration_group_cutover_applies(account_key, binding_index):
            return
        raise HTTPException(
            status_code=409,
            detail={
                'code': 'registration_group_cutover_disabled',
                'operation': str(operation or '').strip() or None,
                'message': '注册群审批新主链已关闭，请走完整同步/重建群绑定旧路径回退。',
            },
        )

    def _build_whatsapp_approval_lightweight_runtime_snapshot(
        self,
        row: Dict[str, Any],
        *,
        include_qr_ascii: bool = False,
        refresh_provider_health: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        account_worker_health: Dict[str, Any] = {}
        account_row = dict(row or {})
        normalized_account_key = str(account_row.get('account_key') or '')
        cached_baileys_runtime, cached_baileys_session, cached_baileys_used = self._build_cached_baileys_whatsapp_approval_runtime_and_session(
            account_row,
            include_qr_ascii=include_qr_ascii,
        )
        if refresh_provider_health and cached_baileys_used:
            baileys_runtime_state, baileys_session_state, baileys_used = self._build_baileys_whatsapp_approval_runtime_and_session(
                account_row,
                include_qr_ascii=include_qr_ascii,
            )
        else:
            baileys_runtime_state, baileys_session_state, baileys_used = cached_baileys_runtime, cached_baileys_session, cached_baileys_used
        if baileys_used:
            runtime_state = baileys_runtime_state
            session_state = baileys_session_state
        else:
            try:
                runtime_state = self._build_whatsapp_approval_runtime_state(
                    normalized_account_key,
                    worker_health=None,
                    allow_shared_fallback=False,
                    skip_health_check=True,
                )
            except TypeError as exc:
                if 'skip_health_check' not in str(exc):
                    raise
                runtime_state = self._build_whatsapp_approval_runtime_state(
                    normalized_account_key,
                    worker_health=None,
                    allow_shared_fallback=False,
                )
            cached_session_state = self._cached_whatsapp_approval_session_snapshot(normalized_account_key)
            if cached_session_state:
                session_state = cached_session_state
            else:
                meta_snapshot = self._read_whatsapp_approval_runtime_meta(normalized_account_key)
                cached_worker_health = meta_snapshot.get('last_worker_health') if isinstance(meta_snapshot.get('last_worker_health'), dict) else {}
                cached_client = cached_worker_health.get('approval_client') if isinstance(cached_worker_health.get('approval_client'), dict) else cached_worker_health
                if bool(runtime_state.get('active')) and bool(cached_client.get('authenticated')) and bool(cached_client.get('ready')):
                    session_state = self._build_whatsapp_approval_session_state(
                        normalized_account_key,
                        worker_health=cached_worker_health,
                        include_qr_ascii=include_qr_ascii,
                    )
                    session_state['from_cached_worker_health'] = True
                elif (
                    bool(runtime_state.get('active'))
                    and cached_worker_health
                    and self._whatsapp_approval_runtime_in_localauth_recovery_window(normalized_account_key, meta_snapshot)
                ):
                    session_state = self._build_whatsapp_approval_session_state(
                        normalized_account_key,
                        worker_health=cached_worker_health,
                        include_qr_ascii=include_qr_ascii,
                    )
                    session_state['from_cached_worker_health'] = True
                elif not bool(runtime_state.get('active')) and self._whatsapp_approval_has_local_auth_session(normalized_account_key):
                    session_state = {
                        'account_key': normalized_account_key,
                        'login_verified': False,
                        'login_state': 'recoverable',
                        'login_check_status': 'runtime_recoverable',
                        'login_check_message': '登录态可恢复，点击刷新状态恢复。',
                        'qr_available': False,
                        'can_show_qr': False,
                        'can_probe': False,
                    }
                else:
                    session_state = self._build_whatsapp_approval_session_state(
                        normalized_account_key,
                        worker_health=account_worker_health,
                        include_qr_ascii=include_qr_ascii,
                    )
        session_state = enrich_whatsapp_login_state(
            session_state,
            runtime_state=runtime_state,
            account_enabled=bool(account_row.get('enabled')),
        )
        return runtime_state, session_state, account_worker_health

    @classmethod
    def _compact_whatsapp_approval_lightweight_payload(cls, value: Any) -> Any:
        heavy_keys = {
            'pairingCode',
            'pairing_code',
            'qr',
            'qrAscii',
            'qrTerminal',
            'qrText',
            'qrImageDataUrl',
            'qr_ascii',
            'qr_terminal',
            'qr_text',
            'qr_image_data_url',
            'qrImage',
            'qr_image',
        }
        if isinstance(value, dict):
            return {
                key: cls._compact_whatsapp_approval_lightweight_payload(item)
                for key, item in value.items()
                if key not in heavy_keys
            }
        if isinstance(value, list):
            return [cls._compact_whatsapp_approval_lightweight_payload(item) for item in value]
        return copy.deepcopy(value)

    def list_whatsapp_approval_accounts(
        self,
        current_user: Optional[Dict[str, Any]] = None,
        *,
        lightweight: bool = False,
        include_options: bool = True,
    ) -> Dict[str, Any]:
        profile_started = time.perf_counter()
        profile_marks: Dict[str, int] = {}

        def _mark_profile_stage(name: str) -> None:
            if lightweight:
                profile_marks[name] = int(max((time.perf_counter() - profile_started) * 1000.0, 0.0))

        production_ops = self._production_ops_daemon_snapshot_light() if lightweight else self._production_ops_daemon_snapshot()
        _mark_profile_stage('production_ops')
        if lightweight:
            # 页面首屏只读本地 runtime/meta 快照，避免每次打开账号列表都同步探测所有 WA runtime/bridge。
            # 单账号“刷新登录状态/生成二维码/审批前校验”仍走实时健康检查，不放宽审批安全门禁。
            official_bridge = {'configured': False, 'health': {}, 'summary': {}, 'lightweight': True}
            shared_worker_health = {}
        else:
            official_bridge = self._official_group_bridge_summary_payload()
            try:
                shared_worker_health = self._current_whatsapp_approval_worker_health()
            except Exception:
                shared_worker_health = {}
        rows: list[Dict[str, Any]] = []
        user_role = str((current_user or {}).get('role') or '').strip().lower()
        user_id = str((current_user or {}).get('user_id') or '').strip()
        where_clause = "responsible_type IN ('registration_group', 'official_group')"
        params: tuple[Any, ...] = ()
        with self.db.connect() as conn:
            raw_rows = conn.execute(
                f"SELECT account_key, account_name, responsible_type, group_links, area, notify_profile_name, approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker, schedule_windows, enabled, verification_status, assigned_customer_service_user_id, assigned_customer_service_username, assigned_customer_service_display_name, notes, created_at, updated_at FROM whatsapp_approval_accounts WHERE {where_clause} ORDER BY CASE responsible_type WHEN 'registration_group' THEN 1 WHEN 'official_group' THEN 2 ELSE 99 END ASC, CASE WHEN NULLIF(created_at, '') IS NULL THEN 1 ELSE 0 END ASC, COALESCE(NULLIF(created_at, ''), '') ASC, account_key ASC",
                params,
            ).fetchall()
        _mark_profile_stage('db_rows')
        if ops_role_is_business(user_role):
            raw_rows = [
                row for row in raw_rows
                if (
                    user_id in self._whatsapp_approval_assigned_customer_service_ids_from_row(dict(row))
                    or (
                        str(dict(row).get('responsible_type') or '').strip() == 'official_group'
                        and not self._whatsapp_approval_assigned_customer_service_ids_from_row(dict(row))
                    )
                )
            ]
        had_snapshot_cache = hasattr(self, '_approval_queue_snapshot_cache')
        previous_snapshot_cache = getattr(self, '_approval_queue_snapshot_cache', None)
        if lightweight:
            self._approval_queue_snapshot_cache = self._build_approval_queue_snapshot_cache_for_account_rows([dict(row) for row in raw_rows])
            _mark_profile_stage('truth_snapshot_cache')
        try:
            for raw_row in raw_rows:
                row = dict(raw_row)
                if lightweight:
                    # Lightweight list mode must be side-effect free and non-blocking:
                    # do not call worker /health, warmup, session start, runtime start, or group-state here.
                    # Operators can use the explicit single-account refresh/session endpoints for live state.
                    runtime_state, session_state, account_worker_health = self._build_whatsapp_approval_lightweight_runtime_snapshot(row)
                    built = self._build_whatsapp_approval_account_runtime(
                        row,
                        production_ops=production_ops,
                        official_bridge=official_bridge,
                        worker_health=account_worker_health,
                        runtime_state=runtime_state,
                        session_state=session_state,
                        skip_live_probe=True,
                        read_only=True,
                    )
                    built['list_mode'] = 'lightweight'
                    built = self._compact_whatsapp_approval_lightweight_payload(built)
                else:
                    built, shared_worker_health = self._build_whatsapp_approval_account_runtime_with_auto_recover(
                        row,
                        production_ops=production_ops,
                        official_bridge=official_bridge,
                        shared_worker_health=shared_worker_health,
                    )
                rows.append(built)
        finally:
            if lightweight:
                if had_snapshot_cache:
                    self._approval_queue_snapshot_cache = previous_snapshot_cache
                else:
                    try:
                        delattr(self, '_approval_queue_snapshot_cache')
                    except AttributeError:
                        pass
        _mark_profile_stage('runtime_rows')
        option_payload = self.list_whatsapp_approval_account_options() if include_options else {}
        _mark_profile_stage('options')
        payload = {
            'rows': rows,
            'registration_group_cutover_enabled': bool(self.whatsapp_registration_group_approval_cutover_enabled),
            'list_mode': 'lightweight' if lightweight else 'live',
            'summary': {
                'total_accounts': len(rows),
                'enabled_accounts': sum(1 for row in rows if row.get('enabled')),
                'registration_group_accounts': sum(1 for row in rows if row.get('responsible_type') == 'registration_group'),
                'official_group_accounts': sum(1 for row in rows if row.get('responsible_type') == 'official_group'),
                'active_now_accounts': sum(1 for row in rows if row.get('runtime_status') == 'active'),
                'ready_accounts': sum(1 for row in rows if row.get('verification_status') == 'ready'),
                'verification_pending_accounts': sum(1 for row in rows if row.get('verification_status') != 'ready'),
            },
        }
        if include_options:
            payload.update(option_payload)
        if lightweight:
            total_ms = int(max((time.perf_counter() - profile_started) * 1000.0, 0.0))
            try:
                threshold_ms = float(os.getenv('OPS_APPROVAL_ACCOUNTS_PROFILE_THRESHOLD_MS') or '500')
            except (TypeError, ValueError):
                threshold_ms = 500.0
            if total_ms >= max(threshold_ms, 0.0):
                print(json.dumps({
                    'event': 'approval_accounts_lightweight_profile',
                    'total_ms': total_ms,
                    'include_options': bool(include_options),
                    'row_count': len(rows),
                    'stage_ms': profile_marks,
                }, ensure_ascii=False))
        return payload

    def list_whatsapp_approval_candidates(self, current_user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        account_state = self.list_whatsapp_approval_accounts(current_user=current_user, lightweight=True)
        rows = []
        for row in account_state.get('rows') or []:
            membership_verifier = dict(row.get('membership_verifier') or {})
            candidate_status = 'eligible' if row.get('runtime_status') == 'active' and row.get('verification_status') == 'ready' else 'not_ready'
            verification_scope = {
                'configuration_ready': row.get('verification_status') == 'ready',
                'schedule_active_now': bool(row.get('schedule_active_now')),
                'service_scope_ready': bool((row.get('service_scope') or {}).get('ready')),
                'real_membership_check_ready': bool(membership_verifier.get('ready')),
                'requires_manual_seed': bool(membership_verifier.get('requires_manual_seed', not membership_verifier.get('ready'))),
            }
            rows.append({
                'account_key': row.get('account_key'),
                'account_name': row.get('account_name'),
                'responsible_type': row.get('responsible_type'),
                'candidate_status': candidate_status,
                'runtime_status': row.get('runtime_status'),
                'verification_status': row.get('verification_status'),
                'status_text': row.get('status_text'),
                'group_count': row.get('group_count'),
                'next_action': row.get('next_action'),
                'verification_scope': verification_scope,
                'membership_verifier': membership_verifier,
            })
        rows.sort(key=lambda item: (0 if item.get('candidate_status') == 'eligible' else 1, str(item.get('account_key') or '')))
        verifier_ready_count = sum(1 for row in rows if (row.get('verification_scope') or {}).get('real_membership_check_ready'))
        any_manual_seed = any((row.get('verification_scope') or {}).get('requires_manual_seed') for row in rows)
        framework_status = 'live_probe_ready' if verifier_ready_count else ('seed_required' if any_manual_seed else 'unavailable')
        if verifier_ready_count:
            framework_detail = '已接入真实注册群状态探针；具备实时群成员/管理员权限校验能力的账号会在候选池中标记为 real_membership_check_ready=true。'
        elif any_manual_seed:
            framework_detail = '部分账号仍缺真实成员/管理员校验探针，需继续补齐执行器种子或 bridge 能力。'
        else:
            framework_detail = '当前没有可用于真实成员/管理员权限校验的账号执行器。'
        return {
            'rows': rows,
            'summary': {
                'eligible_count': sum(1 for row in rows if row.get('candidate_status') == 'eligible'),
                'registration_group_count': sum(1 for row in rows if row.get('responsible_type') == 'registration_group'),
                'official_group_count': sum(1 for row in rows if row.get('responsible_type') == 'official_group'),
                'verifier_ready_count': verifier_ready_count,
            },
            'verifier_framework': {
                'status': framework_status,
                'real_membership_check_ready': bool(verifier_ready_count),
                'requires_manual_seed': any_manual_seed,
                'detail': framework_detail,
            },
        }

    def _queue_registration_group_probe_tasks(
        self,
        *,
        account_key: str,
        bindings: List[Dict[str, Any]],
        binding_indexes: List[int],
        created_by: str = '',
        reason: str = 'config_change',
    ) -> List[Dict[str, Any]]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key or not binding_indexes:
            return []
        now_iso = utc_now()
        tasks: List[Dict[str, Any]] = []
        with self.db.connect() as conn:
            for binding_index in sorted(set(int(index) for index in binding_indexes if 0 <= int(index) < len(bindings))):
                binding = dict(bindings[binding_index] or {})
                config_fingerprint = str(binding.get('config_fingerprint') or _whatsapp_approval_binding_config_fingerprint(binding)).strip()
                input_payload = {
                    'account_key': normalized_key,
                    'binding_index': binding_index,
                    'link': str(binding.get('link') or '').strip(),
                    'registration_group': str(binding.get('registration_group') or '').strip(),
                    'group_id': str(binding.get('group_id') or '').strip(),
                    'group_name': str(binding.get('group_name') or '').strip(),
                    'config_fingerprint': config_fingerprint,
                    'reason': reason,
                }
                idempotency_key = f'probe_registration_group_truth:{normalized_key}:{binding_index}:{config_fingerprint}'
                task_id = f'op-task-{create_id("probe")}'
                queue_stage = 'queued_after_identity_rebuild' if reason in {'stale_identity_rebuild', 'manual_identity_rebuild'} else 'queued_after_config_change'
                conn.execute(
                    """
                    INSERT INTO mcn_operation_tasks (
                        task_id, task_type, object_type, object_key, idempotency_key, status, stage,
                        priority, retry_count, max_retries, input_json, result_json, error_code,
                        error_message, created_by, created_at
                    ) VALUES (?, 'probe_registration_group_truth', 'registration_group_binding', ?, ?, 'pending',
                              ?, 10, 0, 3, ?, '{}', '', '', ?, ?)
                    ON CONFLICT(task_type, idempotency_key)
                    DO UPDATE SET status = CASE WHEN mcn_operation_tasks.status IN ('success','running') THEN mcn_operation_tasks.status ELSE 'pending' END,
                                  stage = CASE WHEN mcn_operation_tasks.status IN ('success','running') THEN mcn_operation_tasks.stage ELSE excluded.stage END,
                                  priority = MIN(mcn_operation_tasks.priority, excluded.priority),
                                  input_json = excluded.input_json,
                                  error_code = CASE WHEN mcn_operation_tasks.status IN ('success','running') THEN mcn_operation_tasks.error_code ELSE '' END,
                                  error_message = CASE WHEN mcn_operation_tasks.status IN ('success','running') THEN mcn_operation_tasks.error_message ELSE '' END
                    """,
                    (
                        task_id,
                        f'{normalized_key}:{binding_index}',
                        idempotency_key,
                        queue_stage,
                        json.dumps(input_payload, ensure_ascii=False),
                        str(created_by or '').strip(),
                        now_iso,
                    ),
                )
                row = conn.execute(
                    "SELECT task_id, task_type, object_key, status, stage, priority FROM mcn_operation_tasks WHERE task_type='probe_registration_group_truth' AND idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    tasks.append({
                        'task_id': row['task_id'],
                        'task_type': row['task_type'],
                        'binding_index': binding_index,
                        'object_key': row['object_key'],
                        'status': row['status'],
                        'stage': row['stage'],
                        'priority': row['priority'],
                    })
            conn.commit()
        return tasks

    def update_whatsapp_approval_account(self, account_key: str, payload: WhatsAppApprovalAccountUpdateRequest, current_user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            raise HTTPException(status_code=400, detail='account_key is required')
        account_name = str(payload.account_name or '').strip()
        if not account_name:
            raise HTTPException(status_code=400, detail='account_name is required')
        responsible_type = str(payload.responsible_type or '').strip()
        if responsible_type not in {'registration_group', 'official_group'}:
            raise HTTPException(status_code=400, detail='responsible_type must be registration_group or official_group')
        required_notify_profile_name = _whatsapp_approval_notify_profile_for_responsible_type(responsible_type)
        with self.db.connect() as conn:
            existing_row_raw = conn.execute(
                "SELECT account_key, assigned_customer_service_user_id, assigned_customer_service_username, assigned_customer_service_display_name, group_links FROM whatsapp_approval_accounts WHERE account_key = ?",
                (normalized_key,),
            ).fetchone()
        existing_row = dict(existing_row_raw) if existing_row_raw is not None else None
        existing_bindings: List[Dict[str, Any]] = []
        if existing_row:
            try:
                existing_raw = json.loads(str(existing_row.get('group_links') or '[]'))
                if isinstance(existing_raw, list):
                    existing_bindings = [dict(item or {}) for item in existing_raw if isinstance(item, dict)]
            except Exception:
                existing_bindings = []
        user_role = str((current_user or {}).get('role') or '').strip().lower()
        is_admin_user = user_role in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_INTERNAL}
        if current_user and not is_admin_user:
            if not existing_row or not self._ops_user_can_manage_whatsapp_approval_account(current_user, existing_row):
                raise HTTPException(status_code=403, detail='whatsapp_approval_account_not_assigned')
        payload_fields_set_source = getattr(payload, 'model_fields_set', None)
        if payload_fields_set_source is None:
            payload_fields_set_source = getattr(payload, '__fields_set__', set())
        payload_fields_set = set(payload_fields_set_source or set())
        assignment_provided = bool(
            'assigned_customer_service_user_ids' in payload_fields_set
            or 'assigned_customer_service_user_id' in payload_fields_set
        )
        requested_assignment_ids = self._normalize_customer_service_assignment_ids(payload.assigned_customer_service_user_ids)
        if not requested_assignment_ids:
            requested_assignment_ids = self._normalize_customer_service_assignment_ids(payload.assigned_customer_service_user_id)
        existing_assignment_ids = self._whatsapp_approval_assigned_customer_service_ids_from_row(existing_row or {})
        assignment_required = responsible_type == 'registration_group'
        assignment: Dict[str, Any]
        if is_admin_user:
            if requested_assignment_ids:
                assignment = self._resolve_customer_service_assignments(requested_assignment_ids, required=assignment_required)
            elif existing_row and not assignment_provided:
                assignment = self._resolve_customer_service_assignments(existing_assignment_ids, required=assignment_required)
            elif user_role == OPS_AUTH_ROLE_INTERNAL or str((current_user or {}).get('user_id') or '').strip() == 'local-dev':
                assignment = self._resolve_customer_service_assignments([], required=False)
            elif assignment_required:
                raise HTTPException(status_code=400, detail='assigned_customer_service_required')
            else:
                assignment = self._resolve_customer_service_assignments([], required=False)
        elif current_user:
            assignment = self._resolve_customer_service_assignments(existing_assignment_ids, required=assignment_required)
        else:
            if requested_assignment_ids:
                assignment = self._resolve_customer_service_assignments(requested_assignment_ids, required=False)
            elif existing_row and not assignment_provided:
                assignment = self._resolve_customer_service_assignments(existing_assignment_ids, required=False)
            else:
                assignment = self._resolve_customer_service_assignments([], required=False)
        existing_runtime_config = _whatsapp_approval_runtime_config_from_dict(_preferred_group_binding(_normalize_group_link_bindings(existing_bindings, responsible_type=responsible_type)))
        payload_runtime_config = _whatsapp_approval_runtime_config_from_dict({
            'provider_mode': payload.provider_mode,
            'registration_group_runtime': payload.registration_group_runtime,
            'official_group_runtime': payload.official_group_runtime,
            'group_assistant_runtime': payload.group_assistant_runtime,
            'provider_capabilities': dict(payload.provider_capabilities or {}) if isinstance(payload.provider_capabilities, dict) else {},
            'baileys_enabled': payload.baileys_enabled,
            'baileys_base_url': payload.baileys_base_url,
            'provider_base_url': payload.provider_base_url,
            'baileys_token': payload.baileys_token,
            'provider_token': payload.provider_token,
            'runtime_token': payload.runtime_token,
            'baileys_account_id': payload.baileys_account_id,
            'provider_account_id': payload.provider_account_id,
            'account_id': payload.account_id,
        })
        account_runtime_config = _merge_whatsapp_approval_runtime_configs(existing_runtime_config, payload_runtime_config)
        raw_bindings = [
            {
                'binding_id': str(item.binding_id or '').strip(),
                'link': str(item.link or '').strip(),
                'group_name': str(item.group_name or '').strip(),
                'area': str(item.area or '').strip(),
                'notify_profile_name': str(item.notify_profile_name or '').strip(),
                'enabled': False if item.enabled is False else True,
                'registration_group': str(item.registration_group or '').strip(),
                'group_id': str(item.group_id or '').strip(),
                'provider_mode': str(item.provider_mode or '').strip().lower(),
                'registration_group_runtime': str(item.registration_group_runtime or '').strip().lower(),
                'official_group_runtime': str(item.official_group_runtime or '').strip().lower(),
                'group_assistant_runtime': str(item.group_assistant_runtime or '').strip().lower(),
                'provider_capabilities': dict(item.provider_capabilities or {}) if isinstance(item.provider_capabilities, dict) else {},
                'baileys_enabled': item.baileys_enabled,
                'baileys_base_url': str(item.baileys_base_url or '').strip().rstrip('/'),
                'provider_base_url': str(item.provider_base_url or '').strip().rstrip('/'),
                'baileys_token': str(item.baileys_token or '').strip(),
                'provider_token': str(item.provider_token or '').strip(),
                'runtime_token': str(item.runtime_token or '').strip(),
                'baileys_account_id': str(item.baileys_account_id or '').strip(),
                'provider_account_id': str(item.provider_account_id or '').strip(),
                'account_id': str(item.account_id or '').strip(),
                'approval_count_threshold': item.approval_count_threshold,
                'approval_timeout_minutes': item.approval_timeout_minutes,
                'auto_recover_worker': item.auto_recover_worker,
                'schedule_windows': [
                    {'start': str(window.start or '').strip(), 'end': str(window.end or '').strip()}
                    for window in (item.schedule_windows or [])
                ],
            }
            for item in (payload.group_link_bindings or [])
        ]
        if not raw_bindings:
            fallback_links = [str(item or '').strip() for item in (payload.group_links or []) if str(item or '').strip()]
            fallback_area = str(payload.area or '').strip()
            fallback_notify = str(payload.notify_profile_name or '').strip()
            legacy_count_threshold, legacy_timeout_minutes = _legacy_approval_thresholds(payload.approval_rule)
            fallback_count = _coerce_positive_int(payload.approval_count_threshold, legacy_count_threshold)
            fallback_timeout = _coerce_positive_int(payload.approval_timeout_minutes, legacy_timeout_minutes)
            fallback_schedule_windows = [
                {'start': str(item.start or '').strip(), 'end': str(item.end or '').strip()}
                for item in (payload.schedule_windows or [])
            ]
            raw_bindings = [{
                'link': link,
                'group_name': '',
                'area': fallback_area,
                'notify_profile_name': fallback_notify,
                'enabled': True,
                'registration_group': '',
                'group_id': '',
                'approval_count_threshold': fallback_count,
                'approval_timeout_minutes': fallback_timeout,
                'auto_recover_worker': payload.auto_recover_worker,
                'schedule_windows': fallback_schedule_windows,
            } for link in fallback_links]
        if account_runtime_config:
            raw_bindings = [
                _apply_whatsapp_approval_runtime_defaults(item, account_runtime_config)
                for item in raw_bindings
            ]
        group_link_bindings = []
        probe_refresh_bindings: list[int] = []
        probe_refresh_reasons: dict[int, str] = {}
        changed_identity_truth_object_keys: set[str] = set()
        existing_bindings_by_lookup: dict[tuple[str, str], Dict[str, Any]] = {}
        for existing_binding in existing_bindings:
            for lookup_key in _whatsapp_approval_binding_lookup_keys(existing_binding):
                existing_bindings_by_lookup.setdefault(lookup_key, existing_binding)
        for index, item in enumerate(raw_bindings, start=1):
            incoming_binding_id = str(item.get('binding_id') or '').strip()
            link = _normalize_whatsapp_group_invite_link(item.get('link'))
            area = str(item.get('area') or '').strip()
            notify_profile_name = required_notify_profile_name or str(item.get('notify_profile_name') or '').strip()
            registration_group = str(item.get('registration_group') or '').strip()
            if _looks_like_whatsapp_invite_link(registration_group):
                registration_group = ''
            elif not registration_group:
                fallback_group_id = _sanitize_whatsapp_group_jid(item.get('group_id'))
                if not fallback_group_id:
                    raw_group_id = str(item.get('group_id') or '').strip()
                    if raw_group_id and not _looks_like_whatsapp_invite_link(raw_group_id):
                        fallback_group_id = raw_group_id
                registration_group = fallback_group_id
            group_id = _sanitize_whatsapp_group_jid(item.get('group_id'))
            if not group_id:
                raw_group_id = str(item.get('group_id') or '').strip()
                if raw_group_id and not _looks_like_whatsapp_invite_link(raw_group_id):
                    group_id = raw_group_id
            if not link and not area and not notify_profile_name and not registration_group and not group_id:
                continue
            if link and not area:
                raise HTTPException(status_code=400, detail=f'group link #{index} must select an area')
            if area and not link:
                raise HTTPException(status_code=400, detail=f'group link #{index} is missing its link')
            if not notify_profile_name:
                raise HTTPException(status_code=400, detail=f'group link #{index} must select a notify robot')
            schedule_windows = _normalize_schedule_windows_payload(item.get('schedule_windows') or [])
            existing_binding: Dict[str, Any] = {}
            lookup_binding = {
                'binding_id': incoming_binding_id,
                'link': link,
                'group_id': group_id,
                'registration_group': registration_group,
                'area': area,
            }
            for lookup_key in _whatsapp_approval_binding_lookup_keys(lookup_binding):
                matched_binding = existing_bindings_by_lookup.get(lookup_key)
                if matched_binding:
                    existing_binding = matched_binding
                    break
            if not existing_binding and index - 1 < len(existing_bindings):
                existing_binding = existing_bindings[index - 1] if isinstance(existing_bindings[index - 1], dict) else {}
            existing_binding_id = str(existing_binding.get('binding_id') or '').strip()
            if not incoming_binding_id:
                incoming_binding_id = existing_binding_id or _new_whatsapp_approval_binding_id()
            existing_link = _normalize_whatsapp_group_invite_link(existing_binding.get('link')) if existing_binding else ''
            link_changed = bool(existing_link and link and existing_link != link)
            existing_identity_status = str(existing_binding.get('identity_status') or '').strip()
            existing_last_probe_reason = str(existing_binding.get('last_probe_reason') or '').strip()
            existing_had_failed_join_probe = existing_binding.get('last_probe_self_participant_found') is False
            existing_group_id_jid = _sanitize_whatsapp_group_jid(existing_binding.get('group_id')) or _sanitize_whatsapp_group_jid(existing_binding.get('registration_group')) or _sanitize_whatsapp_group_jid(existing_binding.get('runtime_probe_group_id'))
            manual_rebuild_with_verified_fallback = bool(existing_group_id_jid and existing_last_probe_reason == 'manual_identity_rebuild')
            stale_identity = bool(
                existing_binding
                and not link_changed
                and link
                and not manual_rebuild_with_verified_fallback
                and (
                    existing_identity_status in {'unresolved', 'stale', 'needs_rebuild', 'permission_pending'}
                    or existing_last_probe_reason in {'group_not_found', 'identity_unresolved', 'permission_pending'}
                    or existing_had_failed_join_probe
                    or (existing_binding.get('runtime_probe_group_id') is None and existing_last_probe_reason)
                )
            )
            if link_changed:
                # 运营换群链接后，旧 group_id/group_name/registration_group 不能继续随表单隐藏字段提交回来。
                for truth_key in Service._approval_binding_truth_object_keys(
                    normalized_key,
                    {**dict(existing_binding or {}), 'binding_id': incoming_binding_id or existing_binding_id},
                ):
                    changed_identity_truth_object_keys.add(truth_key)
                registration_group = ''
                group_id = ''
                probe_refresh_bindings.append(index - 1)
                probe_refresh_reasons[index - 1] = 'group_link_config_changed'
            elif stale_identity:
                # 账号最初不在群/探针未解析时留下的旧绑定身份不能沿用；普通保存也应等同“删除后重建”。
                for truth_key in Service._approval_binding_truth_object_keys(
                    normalized_key,
                    {**dict(existing_binding or {}), 'binding_id': incoming_binding_id or existing_binding_id},
                ):
                    changed_identity_truth_object_keys.add(truth_key)
                registration_group = ''
                group_id = ''
                probe_refresh_bindings.append(index - 1)
                probe_refresh_reasons[index - 1] = 'stale_identity_rebuild'
            elif link and not (registration_group or group_id):
                # 新增/未解析群组需要保存后自动探测一次，拿到稳定 group_id 后 daemon 才能持续监控。
                probe_refresh_bindings.append(index - 1)
                probe_refresh_reasons[index - 1] = 'group_link_config_changed'
            if any(not re.fullmatch(r'\d{2}:\d{2}', str(window.get('start') or '')) or not re.fullmatch(r'\d{2}:\d{2}', str(window.get('end') or '')) for window in (item.get('schedule_windows') or [])):
                raise HTTPException(status_code=400, detail=f'group link #{index} schedule window must use HH:MM format')
            resolved_from_explicit_target = bool(group_id)
            persisted_group_name = '' if (link_changed or stale_identity) else (
                str(existing_binding.get('group_name') or '').strip()
                or str(item.get('group_name') or '').strip()
            )
            binding_row = {
                'binding_id': incoming_binding_id,
                'link': link,
                'group_name': persisted_group_name,
                'area': area,
                'notify_profile_name': notify_profile_name,
                'enabled': False if item.get('enabled') is False else True,
                'registration_group': registration_group,
                'group_id': group_id,
                'provider_mode': str(item.get('provider_mode') or '').strip().lower(),
                'registration_group_runtime': str(item.get('registration_group_runtime') or '').strip().lower(),
                'official_group_runtime': str(item.get('official_group_runtime') or '').strip().lower(),
                'group_assistant_runtime': str(item.get('group_assistant_runtime') or '').strip().lower(),
                'provider_capabilities': dict(item.get('provider_capabilities') or {}) if isinstance(item.get('provider_capabilities'), dict) else {},
                'baileys_enabled': item.get('baileys_enabled'),
                'baileys_base_url': str(item.get('baileys_base_url') or item.get('provider_base_url') or '').strip().rstrip('/'),
                'provider_base_url': str(item.get('provider_base_url') or item.get('baileys_base_url') or '').strip().rstrip('/'),
                'baileys_token': str(item.get('baileys_token') or item.get('provider_token') or item.get('runtime_token') or '').strip(),
                'provider_token': str(item.get('provider_token') or item.get('baileys_token') or item.get('runtime_token') or '').strip(),
                'runtime_token': str(item.get('runtime_token') or item.get('baileys_token') or item.get('provider_token') or '').strip(),
                'baileys_account_id': str(item.get('baileys_account_id') or item.get('provider_account_id') or item.get('account_id') or '').strip(),
                'provider_account_id': str(item.get('provider_account_id') or item.get('baileys_account_id') or item.get('account_id') or '').strip(),
                'account_id': str(item.get('account_id') or item.get('baileys_account_id') or item.get('provider_account_id') or '').strip(),
                'approval_count_threshold': item.get('approval_count_threshold'),
                'approval_timeout_minutes': item.get('approval_timeout_minutes'),
                'auto_recover_worker': item.get('auto_recover_worker'),
                'schedule_windows': schedule_windows,
            }
            if link_changed:
                binding_row.update({
                    'identity_status': 'unresolved',
                    'identity_rebuild_reason': 'group_link_config_changed',
                    'last_probe_status': 'needs_rebuild',
                    'last_probe_reason': 'group_link_config_changed',
                    'runtime_probe_group_id': None,
                    'runtime_probe_group_name': None,
                })
            elif stale_identity:
                binding_row.update({
                    'identity_status': 'needs_rebuild',
                    'identity_rebuild_reason': 'stale_identity',
                    'last_probe_status': 'needs_rebuild',
                    'last_probe_reason': 'stale_identity_rebuild',
                    'runtime_probe_group_id': None,
                    'runtime_probe_group_name': None,
                })
            elif resolved_from_explicit_target:
                binding_row.update({
                    'identity_status': 'resolved',
                    'identity_resolved_at': str(existing_binding.get('identity_resolved_at') or '').strip() or utc_now(),
                    'identity_resolved_by': str(existing_binding.get('identity_resolved_by') or '').strip() or 'manual_config',
                    'last_probe_status': str(existing_binding.get('last_probe_status') or '').strip() or 'manual_seeded',
                    'last_probe_reason': str(existing_binding.get('last_probe_reason') or '').strip() or 'manual_group_id_seeded',
                    'runtime_probe_group_id': group_id,
                    'runtime_probe_group_name': persisted_group_name or None,
                })
            group_link_bindings.append(binding_row)
        group_link_bindings = _normalize_group_link_bindings(group_link_bindings, responsible_type=responsible_type)
        binding_baileys_account_ids = {first_baileys_account_id(item) for item in group_link_bindings}
        binding_baileys_account_ids.discard('')
        if len(binding_baileys_account_ids) > 1:
            raise HTTPException(status_code=400, detail='all group bindings in one approval account must use the same baileys_account_id')
        inherited_baileys_account_id = resolve_baileys_account_id_for_card(
            account_key=normalized_key,
            explicit_runtime=account_runtime_config,
            bindings=group_link_bindings,
        )
        if inherited_baileys_account_id:
            group_link_bindings = [
                _apply_baileys_runtime_assignment_defaults(
                    item,
                    responsible_type=responsible_type,
                    baileys_account_id=inherited_baileys_account_id,
                )
                for item in group_link_bindings
            ]
        group_links = [item['link'] for item in group_link_bindings]
        if not group_links:
            raise HTTPException(status_code=400, detail='at least one group link is required')
        if len(group_links) > 10:
            raise HTTPException(status_code=400, detail='each WhatsApp admin account can manage at most 10 groups in this console')
        area_options = self.list_whatsapp_approval_area_options()['options']
        area_values = {str(item.get('value') or '').strip() for item in area_options}
        area_values.discard('')
        for index, item in enumerate(group_link_bindings, start=1):
            raw_area = str(item.get('area') or '').strip()
            canonical_area = _canonical_mcn_region_value(raw_area)
            if canonical_area not in area_values:
                raise HTTPException(status_code=400, detail=f'group link #{index} area must be selected from configured options')
            item['area'] = canonical_area
        area = str(group_link_bindings[0].get('area') or '').strip()
        valid_notify_profiles = {str(item.get('profile_name') or '').strip() for item in self._list_notify_robot_options()}
        valid_notify_profiles.discard('')
        for index, item in enumerate(group_link_bindings, start=1):
            binding_notify_profile_name = str(item.get('notify_profile_name') or '').strip()
            if required_notify_profile_name and binding_notify_profile_name != required_notify_profile_name:
                item['notify_profile_name'] = required_notify_profile_name
                binding_notify_profile_name = required_notify_profile_name
            if binding_notify_profile_name not in valid_notify_profiles:
                raise HTTPException(status_code=400, detail=f'group link #{index} notify_profile_name must be selected from configured Lark robots')
            item['approval_count_threshold'] = _coerce_positive_int(item.get('approval_count_threshold'), WHATSAPP_APPROVAL_DEFAULT_COUNT_THRESHOLD)
            item['approval_timeout_minutes'] = _coerce_positive_int(item.get('approval_timeout_minutes'), WHATSAPP_APPROVAL_DEFAULT_TIMEOUT_MINUTES)
            if item['approval_count_threshold'] <= 0:
                raise HTTPException(status_code=400, detail=f'group link #{index} approval_count_threshold must be a positive integer')
            if item['approval_timeout_minutes'] <= 0:
                raise HTTPException(status_code=400, detail=f'group link #{index} approval_timeout_minutes must be a positive integer')
            item['auto_recover_worker'] = bool(item.get('auto_recover_worker')) if item.get('auto_recover_worker') is not None else bool(payload.auto_recover_worker)
            item['config_fingerprint'] = _whatsapp_approval_binding_config_fingerprint(item)
        # 保存配置必须是纯配置写入，不能同步等待 WhatsApp runtime / health / live probe。
        # 真实群名、group_id、成员/待审批探针由列表页轻量快照或显式刷新动作处理，避免点击保存后长时间无响应。
        runtime_state = self._build_whatsapp_approval_runtime_state(
            normalized_key,
            allow_shared_fallback=responsible_type == 'registration_group',
            skip_health_check=True,
        )
        representative_binding = _preferred_group_binding(group_link_bindings)
        area = str(representative_binding.get('area') or '').strip()
        notify_profile_name = str(representative_binding.get('notify_profile_name') or '').strip()
        approval_count_threshold = _coerce_positive_int(representative_binding.get('approval_count_threshold'), WHATSAPP_APPROVAL_DEFAULT_COUNT_THRESHOLD)
        approval_timeout_minutes = _coerce_positive_int(representative_binding.get('approval_timeout_minutes'), WHATSAPP_APPROVAL_DEFAULT_TIMEOUT_MINUTES)
        schedule_windows = _normalize_schedule_windows_payload(representative_binding.get('schedule_windows') or [])
        row = {
            'account_key': normalized_key,
            'account_name': account_name,
            'responsible_type': responsible_type,
            'group_links': json.dumps(group_link_bindings, ensure_ascii=False),
            'area': area,
            'notify_profile_name': notify_profile_name,
            'approval_rule': 'threshold_or_timeout',
            'approval_count_threshold': approval_count_threshold,
            'approval_timeout_minutes': approval_timeout_minutes,
            'auto_recover_worker': 1 if representative_binding.get('auto_recover_worker') else 0,
            'schedule_windows': json.dumps(schedule_windows, ensure_ascii=False),
            'enabled': 1 if payload.enabled else 0,
            'verification_status': 'pending_verification',
            'assigned_customer_service_user_id': self._serialize_customer_service_assignment_ids(assignment.get('user_ids') or assignment.get('user_id')),
            'assigned_customer_service_user_ids': self._normalize_customer_service_assignment_ids(assignment.get('user_ids') or assignment.get('user_id')),
            'assigned_customer_service_username': str(assignment.get('username') or '').strip(),
            'assigned_customer_service_display_name': str(assignment.get('display_name') or '').strip(),
            'notes': str(payload.notes or '').strip(),
            'updated_at': utc_now(),
        }
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO whatsapp_approval_accounts (
                    account_key, account_name, responsible_type, group_links, area, notify_profile_name, approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker, schedule_windows,
                    enabled, verification_status, assigned_customer_service_user_id, assigned_customer_service_username, assigned_customer_service_display_name, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_key)
                DO UPDATE SET account_name = excluded.account_name,
                              responsible_type = excluded.responsible_type,
                              group_links = excluded.group_links,
                              area = excluded.area,
                              notify_profile_name = excluded.notify_profile_name,
                              approval_rule = excluded.approval_rule,
                              approval_count_threshold = excluded.approval_count_threshold,
                              approval_timeout_minutes = excluded.approval_timeout_minutes,
                              auto_recover_worker = excluded.auto_recover_worker,
                              schedule_windows = excluded.schedule_windows,
                              enabled = excluded.enabled,
                              verification_status = excluded.verification_status,
                              assigned_customer_service_user_id = excluded.assigned_customer_service_user_id,
                              assigned_customer_service_username = excluded.assigned_customer_service_username,
                              assigned_customer_service_display_name = excluded.assigned_customer_service_display_name,
                              notes = excluded.notes,
                              created_at = COALESCE(NULLIF(whatsapp_approval_accounts.created_at, ''), excluded.created_at),
                              updated_at = excluded.updated_at
                """,
                (
                    row['account_key'], row['account_name'], row['responsible_type'], row['group_links'], row['area'], row['notify_profile_name'], row['approval_rule'], row['approval_count_threshold'], row['approval_timeout_minutes'], row['auto_recover_worker'], row['schedule_windows'],
                    row['enabled'], row['verification_status'], row['assigned_customer_service_user_id'], row['assigned_customer_service_username'], row['assigned_customer_service_display_name'], row['notes'], row['updated_at'], row['updated_at'],
                ),
            )
            if changed_identity_truth_object_keys:
                placeholders = ','.join('?' for _ in changed_identity_truth_object_keys)
                conn.execute(
                    f"DELETE FROM mcn_truth_snapshots WHERE object_type = 'registration_group_binding' AND object_key IN ({placeholders})",
                    tuple(sorted(changed_identity_truth_object_keys)),
                )
            conn.commit()
        created_by = str((current_user or {}).get('username') or (current_user or {}).get('user_id') or '').strip()
        probe_refresh_tasks: List[Dict[str, Any]] = []
        if responsible_type in {'registration_group', 'official_group'}:
            valid_probe_indexes = sorted(set(index for index in probe_refresh_bindings if 0 <= index < len(group_link_bindings)))
            for reason_value in sorted({probe_refresh_reasons.get(index, 'group_link_config_changed') for index in valid_probe_indexes}):
                reason_indexes = [index for index in valid_probe_indexes if probe_refresh_reasons.get(index, 'group_link_config_changed') == reason_value]
                probe_refresh_tasks.extend(self._queue_registration_group_probe_tasks(
                    account_key=normalized_key,
                    bindings=group_link_bindings,
                    binding_indexes=reason_indexes,
                    created_by=created_by,
                    reason=reason_value,
                ))
        runtime_state, session_state, account_worker_health = self._build_whatsapp_approval_lightweight_runtime_snapshot(row)
        return {
            'saved': True,
            'probe_refresh_bindings': sorted(set(index for index in probe_refresh_bindings if 0 <= index < len(group_link_bindings))),
            'probe_refresh_tasks': probe_refresh_tasks,
            'account': self._build_whatsapp_approval_account_runtime(
                row,
                runtime_state=runtime_state,
                session_state=session_state,
                worker_health=account_worker_health,
                skip_live_probe=True,
            ),
        }

    def rebuild_whatsapp_approval_binding_identity(
        self,
        account_key: str,
        binding_index: int,
        current_user: Optional[Dict[str, Any]] = None,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            raise HTTPException(status_code=400, detail='account_key is required')
        request_payload = dict(request_context or {})
        request_id = str(request_payload.get('request_id') or '').strip() or create_id('approval_op')
        self._mark_whatsapp_binding_operation_started(
            normalized_key,
            binding_index,
            operation='rebuild_identity',
            detail='正在重建群绑定',
            stage_code='mark_rebuild_pending',
            stage_label='标记待重建',
            request_id=request_id,
        )
        try:
            self._require_whatsapp_approval_account_access(normalized_key, current_user)
            with self.db.connect() as conn:
                row = conn.execute('SELECT account_key, responsible_type, group_links FROM whatsapp_approval_accounts WHERE account_key = ?', (normalized_key,)).fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail='whatsapp approval account not found')
                try:
                    raw_bindings = json.loads(str(row['group_links'] or '[]'))
                except Exception:
                    raw_bindings = []
                bindings = _normalize_group_link_bindings(raw_bindings if isinstance(raw_bindings, list) else [], responsible_type=str(row['responsible_type'] or '').strip())
                if binding_index < 0 or binding_index >= len(bindings):
                    raise HTTPException(status_code=404, detail='whatsapp approval binding not found')
                binding = dict(bindings[binding_index] or {})
                previous_group_id = _sanitize_whatsapp_group_jid(binding.get('group_id')) or _sanitize_whatsapp_group_jid(binding.get('registration_group')) or _sanitize_whatsapp_group_jid(binding.get('runtime_probe_group_id'))
                previous_registration_group = _sanitize_whatsapp_group_jid(binding.get('registration_group')) or previous_group_id
                previous_group_name = str(binding.get('group_name') or binding.get('runtime_probe_group_name') or '').strip()
                if _looks_like_whatsapp_invite_link(previous_group_name):
                    previous_group_name = ''
                rebuild_fingerprint_payload = {
                    **binding,
                    'registration_group': previous_registration_group,
                    'group_id': previous_group_id,
                    'group_name': previous_group_name,
                    'identity_status': 'needs_rebuild',
                    'identity_rebuild_reason': 'manual_rebuild',
                }
                binding.update({
                    'registration_group': previous_registration_group,
                    'group_id': previous_group_id,
                    'group_name': '',
                    'previous_verified_group_id': previous_group_id,
                    'previous_verified_group_name': '',
                    'previous_verified_registration_group': previous_registration_group,
                    'identity_status': 'needs_rebuild',
                    'identity_rebuild_reason': 'manual_rebuild',
                    'last_probe_status': 'queued_for_rebuild',
                    'last_probe_reason': 'manual_identity_rebuild',
                    'config_fingerprint': _whatsapp_approval_binding_config_fingerprint(rebuild_fingerprint_payload),
                })
                bindings[binding_index] = binding
                conn.execute(
                    'UPDATE whatsapp_approval_accounts SET group_links = ?, verification_status = ?, updated_at = ? WHERE account_key = ?',
                    (json.dumps(bindings, ensure_ascii=False), 'pending_verification', utc_now(), normalized_key),
                )
                conn.commit()
            self._update_whatsapp_binding_operation_state(
                normalized_key,
                binding_index,
                detail='正在排队刷新探针',
                stage_code='queue_probe_refresh',
                stage_label='排队刷新探针',
            )
            created_by = str((current_user or {}).get('username') or (current_user or {}).get('user_id') or '').strip()
            tasks = self._queue_registration_group_probe_tasks(
                account_key=normalized_key,
                bindings=bindings,
                binding_indexes=[binding_index],
                created_by=created_by,
                reason='manual_identity_rebuild',
            )
            return {'rebuilt': True, 'account_key': normalized_key, 'binding_index': binding_index, 'binding': binding, 'probe_refresh_tasks': tasks}
        finally:
            self._clear_whatsapp_binding_operation(normalized_key, binding_index)

    def delete_whatsapp_approval_account(self, account_key: str, current_user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            raise HTTPException(status_code=400, detail='account_key is required')
        self._require_whatsapp_approval_account_access(normalized_key, current_user)
        account_row = self._get_whatsapp_approval_account_row(normalized_key) or {}
        baileys_context = self._preferred_baileys_whatsapp_approval_context(account_row) if account_row else {}
        baileys_account_id = str(baileys_context.get('baileys_account_id') or '').strip()
        baileys_base_url = str(baileys_context.get('base_url') or '').strip().rstrip('/')
        baileys_token = str(baileys_context.get('token') or '').strip()
        self.stop_whatsapp_approval_account_runtime(normalized_key)
        with self.db.connect() as conn:
            conn.execute('DELETE FROM whatsapp_approval_accounts WHERE account_key = ?', (normalized_key,))
            conn.commit()
        actor_cleanup: Dict[str, Any] = {
            'requested': False,
            'account_id': baileys_account_id or None,
        }
        if baileys_account_id and baileys_base_url:
            try:
                headers = {'Authorization': f'Bearer {baileys_token}'} if baileys_token else {}
                response = requests.delete(
                    f'{baileys_base_url}/accounts/{quote(baileys_account_id, safe="")}',
                    headers=headers,
                    timeout=8.0,
                )
                response.raise_for_status()
                payload = response.json() if response.content else {}
                actor_cleanup.update({'requested': True, 'ok': True, 'result': payload if isinstance(payload, dict) else {}})
            except Exception as exc:
                actor_cleanup.update({'requested': True, 'ok': False, 'error': str(exc)[:300]})
        return {'deleted': True, 'account_key': normalized_key, 'baileys_actor_cleanup': actor_cleanup}

    def delete_whatsapp_approval_account_binding(self, account_key: str, binding_index: int, current_user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            raise HTTPException(status_code=400, detail='account_key is required')
        try:
            index = int(binding_index)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='binding_index is invalid')
        if index < 0:
            raise HTTPException(status_code=400, detail='binding_index is invalid')
        self._require_whatsapp_approval_account_access(normalized_key, current_user)
        with self.db.connect() as conn:
            row = conn.execute(
                'SELECT account_key, responsible_type, group_links FROM whatsapp_approval_accounts WHERE account_key = ?',
                (normalized_key,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='whatsapp approval account not found')
            responsible_type = str(row['responsible_type'] or '').strip()
            try:
                raw_bindings = json.loads(str(row['group_links'] or '[]'))
            except Exception:
                raw_bindings = []
            bindings = _normalize_group_link_bindings(raw_bindings if isinstance(raw_bindings, list) else [], responsible_type=responsible_type)
            if index >= len(bindings):
                raise HTTPException(status_code=404, detail='whatsapp approval binding not found')
            deleted_binding = dict(bindings[index] or {})
            remaining_bindings = [dict(item or {}) for item_index, item in enumerate(bindings) if item_index != index]
            representative_binding = _preferred_group_binding(remaining_bindings)
            schedule_windows = _normalize_schedule_windows_payload(representative_binding.get('schedule_windows') or []) if representative_binding else []
            deleted_binding_id = str(deleted_binding.get('binding_id') or '').strip()
            truth_object_keys = self._approval_binding_truth_lookup_keys(normalized_key, deleted_binding)
            now = utc_now()
            conn.execute(
                """
                UPDATE whatsapp_approval_accounts
                SET group_links = ?,
                    area = ?,
                    notify_profile_name = ?,
                    approval_count_threshold = ?,
                    approval_timeout_minutes = ?,
                    auto_recover_worker = ?,
                    schedule_windows = ?,
                    verification_status = ?,
                    updated_at = ?
                WHERE account_key = ?
                """,
                (
                    json.dumps(remaining_bindings, ensure_ascii=False),
                    str(representative_binding.get('area') or '').strip() if representative_binding else '',
                    str(representative_binding.get('notify_profile_name') or '').strip() if representative_binding else '',
                    _coerce_positive_int(representative_binding.get('approval_count_threshold'), WHATSAPP_APPROVAL_DEFAULT_COUNT_THRESHOLD) if representative_binding else WHATSAPP_APPROVAL_DEFAULT_COUNT_THRESHOLD,
                    _coerce_positive_int(representative_binding.get('approval_timeout_minutes'), WHATSAPP_APPROVAL_DEFAULT_TIMEOUT_MINUTES) if representative_binding else WHATSAPP_APPROVAL_DEFAULT_TIMEOUT_MINUTES,
                    1 if representative_binding and representative_binding.get('auto_recover_worker') else 0,
                    json.dumps(schedule_windows, ensure_ascii=False),
                    'pending_verification' if remaining_bindings else 'no_group_bindings',
                    now,
                    normalized_key,
                ),
            )
            if deleted_binding_id:
                conn.execute('DELETE FROM wa_group_bindings WHERE binding_id = ?', (deleted_binding_id,))
                conn.execute('DELETE FROM wa_truth_snapshots WHERE binding_id = ?', (deleted_binding_id,))
                conn.execute('DELETE FROM wa_runtime_actions WHERE binding_id = ?', (deleted_binding_id,))
            if truth_object_keys:
                placeholders = ','.join('?' for _ in truth_object_keys)
                conn.execute(
                    f"DELETE FROM mcn_truth_snapshots WHERE object_type = 'registration_group_binding' AND object_key IN ({placeholders})",
                    tuple(truth_object_keys),
                )
            conn.execute(
                """
                UPDATE mcn_operation_tasks
                SET status='cancelled', stage='binding_deleted', finished_at=?, error_code='binding_deleted', error_message='approval binding deleted', lease_owner='', lease_until=''
                WHERE task_type='probe_registration_group_truth'
                  AND object_type='registration_group_binding'
                  AND (object_key = ? OR object_key LIKE ?)
                  AND status IN ('pending','running')
                """,
                (now, f'{normalized_key}:{index}', f'{normalized_key}:%'),
            )
            conn.commit()
        account = self._get_whatsapp_approval_account_runtime_row_lightweight(normalized_key)
        return {
            'deleted': True,
            'account_key': normalized_key,
            'binding_index': index,
            'deleted_binding': deleted_binding,
            'remaining_count': len(remaining_bindings),
            'account': account,
        }

    def _default_production_ops_daemon_config(self) -> Dict[str, Any]:
        launch_agent_installed = PRODUCTION_OPS_DAEMON_LAUNCH_AGENT_PATH.exists()
        return {
            'config_name': 'default',
            'enabled': launch_agent_installed,
            'registration_group': '🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎',
            'api_base_url': 'http://127.0.0.1:8011',
            'worker_base_url': '',
            'interval_seconds': 20.0,
            'notify_chat_id': str(os.getenv('FEISHU_HOME_CHANNEL') or '').strip(),
            'area': 'Indonesia',
            'remark': 'production auto approval daemon',
            'approved_count': 1,
            'auto_recover_worker': True,
            'updated_at': utc_now(),
        }

    def _persist_production_ops_daemon_env(self, row: Dict[str, Any]) -> None:
        if self.db.db_path == ':memory:' or str(os.getenv('PRODUCTION_OPS_DAEMON_SKIP_RUNTIME_SYNC') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
            return
        existing_env: Dict[str, str] = {}
        if PRODUCTION_OPS_DAEMON_ENV_PATH.exists():
            try:
                for raw_line in PRODUCTION_OPS_DAEMON_ENV_PATH.read_text(encoding='utf-8', errors='ignore').splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    existing_env[str(key).strip()] = value.strip().strip('"').strip("'")
            except Exception:
                existing_env = {}
        env_rows = {
            'PRODUCTION_OPS_API_BASE_URL': str(row.get('api_base_url') or '').strip(),
            'PRODUCTION_OPS_WORKER_BASE_URL': _sanitize_legacy_shared_webjs_worker_base_url(row.get('worker_base_url')),
            'PRODUCTION_OPS_REGISTRATION_GROUP': str(row.get('registration_group') or '').strip(),
            'PRODUCTION_OPS_INTERVAL_SECONDS': str(row.get('interval_seconds') or 20),
            'PRODUCTION_OPS_NOTIFY_CHAT_ID': str(row.get('notify_chat_id') or '').strip(),
            'PRODUCTION_OPS_AREA': str(row.get('area') or '').strip(),
            'PRODUCTION_OPS_REMARK': str(row.get('remark') or '').strip(),
            'PRODUCTION_OPS_APPROVED_COUNT': str(row.get('approved_count') or 1),
            'PRODUCTION_OPS_AUTO_RECOVER_WORKER': '1' if row.get('auto_recover_worker') else '0',
        }
        for key in ('PRODUCTION_OPS_FEISHU_APP_ID', 'PRODUCTION_OPS_FEISHU_APP_SECRET', 'PRODUCTION_OPS_FEISHU_DOMAIN'):
            existing_value = str(existing_env.get(key) or '').strip()
            if existing_value:
                env_rows[key] = existing_value
        lines = [f"{key}={shlex.quote(value)}" for key, value in env_rows.items()]
        PRODUCTION_OPS_DAEMON_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        PRODUCTION_OPS_DAEMON_ENV_PATH.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def _sync_production_ops_daemon_launch_agent(self, *, enabled: bool) -> Dict[str, Any]:
        if self.db.db_path == ':memory:' or str(os.getenv('PRODUCTION_OPS_DAEMON_SKIP_RUNTIME_SYNC') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
            return {'attempted': False, 'skipped': True}
        script_path = PRODUCTION_OPS_DAEMON_INSTALL_SCRIPT if enabled else PRODUCTION_OPS_DAEMON_UNINSTALL_SCRIPT
        completed = subprocess.run([str(script_path)], capture_output=True, text=True, cwd=str(PROJECT_ROOT))
        return {
            'attempted': True,
            'enabled': enabled,
            'returncode': completed.returncode,
            'stdout': completed.stdout,
            'stderr': completed.stderr,
            'ok': completed.returncode == 0,
        }

    def _load_registration_group_truth_snapshot_cycles(self) -> List[Dict[str, Any]]:
        rows: List[sqlite3.Row] = []
        now = datetime.now(timezone.utc)
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT object_key, truth_status, confidence, confidence_reason,
                           facts_json, source_json, checked_at, expires_at, recommended_action, updated_at
                    FROM mcn_truth_snapshots
                    WHERE object_type = 'registration_group_binding'
                      AND snapshot_type = 'pending_truth'
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        except Exception:
            return []
        cycles: List[Dict[str, Any]] = []
        seen_binding_keys: set[str] = set()
        for row in rows:
            try:
                expires_at_text = str(row['expires_at'] or '').strip()
                expired = False
                if expires_at_text:
                    expired = parse_iso_datetime(expires_at_text) < now
                facts = json.loads(row['facts_json'] or '{}')
                source = json.loads(row['source_json'] or '{}')
                if not isinstance(facts, dict) or not isinstance(source, dict):
                    continue
            except Exception:
                continue
            truth_status = str(row['truth_status'] or '').strip()
            confidence = str(row['confidence'] or '').strip()
            object_key = str(row['object_key'] or '').strip()
            account_key = object_key.split(':', 1)[0] if ':' in object_key else ''
            configured_link = str(facts.get('configured_link') or '').strip()
            configured_group_id = str(facts.get('configured_group_id') or facts.get('actual_group_id') or facts.get('configured_registration_group') or '').strip()
            binding_dedupe_key = f'{account_key}:{configured_link or configured_group_id or object_key}' if account_key else (configured_link or configured_group_id or object_key)
            if binding_dedupe_key and binding_dedupe_key in seen_binding_keys:
                continue
            if binding_dedupe_key:
                seen_binding_keys.add(binding_dedupe_key)
            payload = {
                'group_id': str(facts.get('actual_group_id') or facts.get('configured_group_id') or facts.get('configured_registration_group') or '').strip(),
                'group_name': str(facts.get('actual_group_name') or facts.get('configured_group_name') or '').strip(),
                'pending_count': facts.get('pending_count'),
                'member_count': facts.get('member_count'),
                'requester_ids': list(facts.get('requester_ids') or []),
                'requesters': list(facts.get('requesters') or []),
                'source': 'mcn_truth_snapshots',
                'source_ts': row['checked_at'],
                'data_quality': 'stale' if expired else confidence,
                'session_health': 'stale' if expired else 'healthy',
                'zero_pending_unverified': bool(facts.get('zero_pending_unverified')),
                'zero_pending_unverified_reason': facts.get('zero_pending_unverified_reason'),
                'zero_pending_verified_by': facts.get('zero_pending_verified_by'),
                'pending_zero_confidence': confidence,
                'review_surface_ready': bool(facts.get('review_surface_ready')),
                'empty_queue_visible': bool(facts.get('empty_queue_visible')),
                'has_pending_section': bool(facts.get('has_pending_section')),
                'has_pending_request_row': bool(facts.get('has_pending_request_row')),
                'probe_data_quality': 'stale' if expired else confidence,
                'truth_snapshot': {
                    'object_key': row['object_key'],
                    'truth_status': truth_status,
                    'confidence': confidence,
                    'confidence_reason': row['confidence_reason'],
                    'checked_at': row['checked_at'],
                    'expires_at': row['expires_at'],
                    'expired': expired,
                    'recommended_action': row['recommended_action'],
                },
            }
            monitor_target = dict((source.get('monitor_target') if isinstance(source.get('monitor_target'), dict) else {}) or {})
            if not monitor_target:
                monitor_target = {
                    'registration_group': facts.get('configured_registration_group') or facts.get('configured_group_id') or facts.get('configured_link'),
                    'group_id': facts.get('configured_group_id') or facts.get('actual_group_id'),
                    'group_name': facts.get('configured_group_name') or facts.get('actual_group_name'),
                    'binding_link': facts.get('configured_link'),
                }
            decision_group_state = {
                'source': 'mcn_truth_snapshots',
                'payload': payload,
                'zero_pending_unverified': bool(facts.get('zero_pending_unverified')),
                'zero_pending_unverified_reason': facts.get('zero_pending_unverified_reason'),
                'zero_pending_verified_by': facts.get('zero_pending_verified_by'),
                'pending_zero_confidence': confidence,
                'probe_data_quality': payload['probe_data_quality'],
                'data_quality': payload['data_quality'],
            }
            truth_state = build_truth_state(
                status={'decision_group_state': decision_group_state},
                runtime_state={
                    'active': facts.get('runtime_active'),
                    'ready': facts.get('runtime_ready'),
                    'authenticated': facts.get('runtime_authenticated'),
                },
                session_state={
                    'session_target_match': facts.get('session_target_match'),
                    'login_verified': facts.get('login_verified'),
                },
                monitor_target=monitor_target,
            )
            normalized_truth_status = truth_status.strip().lower()
            normalized_confidence = confidence.strip().lower()
            if normalized_confidence == 'verified' and normalized_truth_status in {'confirmed_pending', 'confirmed_empty'}:
                truth_state = dict(truth_state or {})
                truth_state['status'] = normalized_truth_status
                truth_state['reason_code'] = str(row['confidence_reason'] or '').strip() or (
                    'pending_detected' if normalized_truth_status == 'confirmed_pending' else 'empty_queue_confirmed'
                )
                truth_state['recoverable'] = False
                if not truth_state.get('data_quality'):
                    truth_state['data_quality'] = 'stale' if expired else 'verified'
                if not truth_state.get('session_health'):
                    truth_state['session_health'] = 'stale' if expired else 'healthy'
            cycles.append({
                'registration_group': facts.get('configured_registration_group') or payload.get('group_id') or facts.get('configured_link'),
                'checked_at': row['checked_at'],
                'monitor_target': monitor_target,
                'decision_group_state': decision_group_state,
                'truth_state': truth_state,
                'truth_snapshot': payload['truth_snapshot'],
            })
        return cycles

    def get_production_ops_daemon_config(self, *, include_truth_snapshots: bool = True) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT config_name, enabled, registration_group, api_base_url, worker_base_url, interval_seconds, notify_chat_id, area, remark, approved_count, auto_recover_worker, updated_at FROM production_ops_daemon_configs WHERE config_name = 'default'"
            ).fetchone()
        if not row:
            config = self._default_production_ops_daemon_config()
        else:
            config = dict(row)
            config['enabled'] = bool(config.get('enabled'))
            config['auto_recover_worker'] = bool(config.get('auto_recover_worker'))
        raw_worker_base_url = str(config.get('worker_base_url') or '').strip()
        sanitized_worker_base_url = _sanitize_legacy_shared_webjs_worker_base_url(raw_worker_base_url)
        if raw_worker_base_url and sanitized_worker_base_url != raw_worker_base_url:
            config['worker_base_url'] = ''
            config['legacy_worker_base_url_ignored'] = True
            try:
                with self.db.connect() as conn:
                    conn.execute(
                        "UPDATE production_ops_daemon_configs SET worker_base_url = '', updated_at = ? WHERE config_name = 'default'",
                        (utc_now(),),
                    )
                    conn.commit()
                self._persist_production_ops_daemon_env({
                    **config,
                    'enabled': bool(config.get('enabled')),
                    'auto_recover_worker': bool(config.get('auto_recover_worker')),
                })
            except Exception:
                pass
        else:
            config['worker_base_url'] = sanitized_worker_base_url
        has_current_approval_accounts = False
        try:
            with self.db.connect() as conn:
                current_account_count = conn.execute(
                    "SELECT COUNT(1) FROM whatsapp_approval_accounts WHERE responsible_type IN ('registration_group', 'official_group')"
                ).fetchone()[0]
            has_current_approval_accounts = int(current_account_count or 0) > 0
        except Exception:
            has_current_approval_accounts = True
        runtime_status = {}
        status_path = Path(PRODUCTION_OPS_DAEMON_STATUS_PATH)
        if status_path.exists():
            try:
                runtime_status = json.loads(status_path.read_text(encoding='utf-8'))
                if not isinstance(runtime_status, dict):
                    runtime_status = {}
                runtime_status = _compact_runtime_log_payload(runtime_status)
            except Exception:
                runtime_status = {}
        runtime_state = {}
        state_path = Path(PRODUCTION_OPS_DAEMON_STATE_PATH)
        if state_path.exists():
            try:
                runtime_state = json.loads(state_path.read_text(encoding='utf-8'))
                if not isinstance(runtime_state, dict):
                    runtime_state = {}
                runtime_state = _compact_runtime_log_payload(runtime_state)
            except Exception:
                runtime_state = {}
        if not has_current_approval_accounts and runtime_status:
            runtime_status = dict(runtime_status)
            runtime_status['incidents'] = []
            runtime_status['observation_warnings'] = []
            runtime_status['notifications'] = []
            runtime_status['registration_group_cycles'] = []
            runtime_status['official_group_cycles'] = []
            runtime_status['worker_state'] = {
                'ok': True,
                'status': 'disabled_no_monitored_groups',
                'message': '当前没有配置 WhatsApp 审批监控群组，已忽略旧守护快照异常',
            }
            runtime_status['stale_snapshot_suppressed'] = True
        truth_snapshot_cycles = self._load_registration_group_truth_snapshot_cycles() if include_truth_snapshots else []
        if include_truth_snapshots and truth_snapshot_cycles:
            runtime_status = dict(runtime_status or {})
            existing_cycles = list(runtime_status.get('registration_group_cycles') or []) if isinstance(runtime_status.get('registration_group_cycles'), list) else []
            def cycle_binding_keys(cycle: Dict[str, Any]) -> set[str]:
                monitor = cycle.get('monitor_target') if isinstance(cycle.get('monitor_target'), dict) else {}
                truth_snapshot = cycle.get('truth_snapshot') if isinstance(cycle.get('truth_snapshot'), dict) else {}
                keys = {str(truth_snapshot.get('object_key') or '').strip()}
                account = str(monitor.get('account_key') or monitor.get('binding_key') or '').strip()
                for value in (
                    monitor.get('binding_link'),
                    monitor.get('link'),
                    cycle.get('binding_link'),
                    cycle.get('registration_group'),
                    monitor.get('registration_group'),
                    monitor.get('group_id'),
                ):
                    text = str(value or '').strip()
                    if text:
                        keys.add(f'{account}:{text}' if account else text)
                keys.discard('')
                return keys
            snapshot_keys = set()
            for cycle in truth_snapshot_cycles:
                if isinstance(cycle, dict):
                    snapshot_keys.update(cycle_binding_keys(cycle))
            filtered_existing_cycles = []
            for cycle in existing_cycles:
                if not isinstance(cycle, dict):
                    filtered_existing_cycles.append(cycle)
                    continue
                if cycle_binding_keys(cycle) & snapshot_keys:
                    continue
                filtered_existing_cycles.append(cycle)
            runtime_status['registration_group_cycles'] = [*truth_snapshot_cycles, *filtered_existing_cycles]
            runtime_status['truth_snapshots'] = {
                'source': 'mcn_truth_snapshots',
                'registration_group_cycles': truth_snapshot_cycles,
                'count': len(truth_snapshot_cycles),
            }
            primary_snapshot_cycle = truth_snapshot_cycles[0]
            for key in ('registration_group', 'monitor_target', 'decision_group_state', 'truth_state'):
                if key in primary_snapshot_cycle:
                    runtime_status[key] = primary_snapshot_cycle[key]
        today_approved_counts = self._approval_batch_member_today_counts()
        runtime_status = dict(runtime_status or {})
        runtime_status['today_approved_counts'] = today_approved_counts
        runtime_status['today_approved_count'] = int(today_approved_counts.get('registration_group') or 0)
        runtime_status['approved_today_count'] = int(today_approved_counts.get('registration_group') or 0)
        runtime_status['today_approved_count_source'] = 'registration_group_approval_batch_members'
        return {
            'config': config,
            'runtime': {
                'launch_agent_installed': PRODUCTION_OPS_DAEMON_LAUNCH_AGENT_PATH.exists(),
                'status_path': str(PRODUCTION_OPS_DAEMON_STATUS_PATH),
                'state_path': str(PRODUCTION_OPS_DAEMON_STATE_PATH),
                'env_path': str(PRODUCTION_OPS_DAEMON_ENV_PATH),
                'status': runtime_status,
                'state': runtime_state,
            },
        }


__all__ = ['WhatsAppServiceMixin']
