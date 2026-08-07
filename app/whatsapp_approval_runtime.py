from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol


AUTHORITATIVE_PROVIDER_MODES = {
    'baileys_authoritative',
    'baileys_primary',
}

MANUAL_APPROVE_PROVIDER_MODES = {
    'baileys_manual_approve_gray',
    'baileys_primary',
}

SHADOW_PROVIDER_MODES = {
    'baileys_shadow',
    'baileys_advisory',
    'baileys_authoritative',
    'baileys_manual_approve_gray',
}

BAILEYS_PROVIDER_MODES = {
    'baileys_shadow',
    'baileys_advisory',
    'baileys_authoritative',
    'baileys_manual_approve_gray',
    'baileys_primary',
}

ADVISORY_PROVIDER_MODES = {
    'baileys_advisory',
}

DEFAULT_REGISTRATION_GROUP_PROVIDER_MODE = 'baileys_primary'
DEFAULT_OFFICIAL_GROUP_PROVIDER_MODE = 'baileys_manual_approve_gray'
DEFAULT_PROVIDER_MODE = 'legacy_only'

RUNTIME_MODE_KEYS = (
    'provider_mode',
    'registration_group_runtime',
    'official_group_runtime',
    'group_assistant_runtime',
    'runtime_mode',
)


def default_whatsapp_approval_provider_mode(*, responsible_type: Any = '') -> str:
    normalized_type = str(responsible_type or '').strip().lower()
    if normalized_type == 'registration_group':
        return DEFAULT_REGISTRATION_GROUP_PROVIDER_MODE
    if normalized_type == 'official_group':
        return DEFAULT_OFFICIAL_GROUP_PROVIDER_MODE
    return DEFAULT_PROVIDER_MODE


def resolve_whatsapp_approval_provider_mode(*, binding: Optional[Dict[str, Any]] = None, account: Optional[Dict[str, Any]] = None, responsible_type: Any = '') -> str:
    item = dict(binding or {})
    owner = dict(account or {})
    for key in RUNTIME_MODE_KEYS:
        value = str(item.get(key) or owner.get(key) or '').strip().lower()
        if value:
            return value
    return default_whatsapp_approval_provider_mode(
        responsible_type=responsible_type or item.get('responsible_type') or owner.get('responsible_type'),
    )


class WARuntimeProvider(Protocol):
    provider_name: str

    def full_queue_sync(self, *, service: Any, account: Dict[str, Any], binding: Dict[str, Any], timeout_seconds: float, priority: str = 'P1') -> Dict[str, Any]: ...

    def probe_binding_group_state(
        self,
        *,
        service: Any,
        responsible_type: str,
        binding: Dict[str, Any],
        runtime_state: Dict[str, Any],
        session_state: Dict[str, Any],
        allow_shared_fallback: bool = True,
        allow_non_jid_fallback: bool = False,
        attempts: int = 2,
        timeout_seconds: float = 25.0,
        priority: str = 'P1',
    ) -> Dict[str, Any]: ...

    def execute_registration_group_approval(
        self,
        *,
        service: Any,
        payload: Any,
        approval_run_id: Optional[str] = None,
    ) -> Dict[str, Any]: ...

    def registration_group_executor_state(
        self,
        *,
        service: Any,
        registration_group: str,
        allow_legacy_target: bool = False,
    ) -> Dict[str, Any]: ...


@dataclass
class ProviderDecision:
    provider_name: str
    provider_mode: str
    source: str
    capabilities: Dict[str, bool]
    shadow_enabled: bool = False
    advisory_enabled: bool = False
    authoritative_read: bool = False
    manual_approve_enabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'provider_name': self.provider_name,
            'provider_mode': self.provider_mode,
            'provider_source': self.source,
            'provider_capabilities': dict(self.capabilities),
            'shadow_enabled': self.shadow_enabled,
            'advisory_enabled': self.advisory_enabled,
            'authoritative_read': self.authoritative_read,
            'manual_approve_enabled': self.manual_approve_enabled,
        }


class LegacyPlaywrightRuntime:
    provider_name = 'legacy_playwright'

    def full_queue_sync(self, *, service: Any, account: Dict[str, Any], binding: Dict[str, Any], timeout_seconds: float, priority: str = 'P1') -> Dict[str, Any]:
        return service._call_whatsapp_worker_full_queue_sync(account=account, binding=binding, timeout_seconds=timeout_seconds)

    def registration_group_executor_state(
        self,
        *,
        service: Any,
        registration_group: str,
        allow_legacy_target: bool = False,
    ) -> Dict[str, Any]:
        try:
            return service.registration_group_approval_executor_group_state(
                registration_group,
                allow_legacy_target=allow_legacy_target,
            )
        except TypeError as exc:
            if 'allow_legacy_target' not in str(exc):
                raise
            return service.registration_group_approval_executor_group_state(registration_group)

    def probe_binding_group_state(
        self,
        *,
        service: Any,
        responsible_type: str,
        binding: Dict[str, Any],
        runtime_state: Dict[str, Any],
        session_state: Dict[str, Any],
        allow_shared_fallback: bool = True,
        allow_non_jid_fallback: bool = False,
        attempts: int = 2,
        timeout_seconds: float = 25.0,
        priority: str = 'P1',
    ) -> Dict[str, Any]:
        return service._probe_whatsapp_binding_group_state(
            responsible_type=responsible_type,
            binding=binding,
            runtime_state=runtime_state,
            session_state=session_state,
            allow_shared_fallback=allow_shared_fallback,
            allow_non_jid_fallback=allow_non_jid_fallback,
            attempts=attempts,
            timeout_seconds=timeout_seconds,
        )

    def execute_registration_group_approval(
        self,
        *,
        service: Any,
        payload: Any,
        approval_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return service._registration_group_approval_decision_sync(payload, approval_run_id=approval_run_id)


class BaileysRuntimeProvider(LegacyPlaywrightRuntime):
    provider_name = 'baileys'

    def full_queue_sync(self, *, service: Any, account: Dict[str, Any], binding: Dict[str, Any], timeout_seconds: float, priority: str = 'P1') -> Dict[str, Any]:
        if hasattr(service, '_call_baileys_full_queue_sync'):
            result = service._call_baileys_full_queue_sync(account=account, binding=binding, timeout_seconds=timeout_seconds, priority=priority)
            if isinstance(result, dict) and result:
                return result
        return {}

    def probe_binding_group_state(
        self,
        *,
        service: Any,
        responsible_type: str,
        binding: Dict[str, Any],
        runtime_state: Dict[str, Any],
        session_state: Dict[str, Any],
        allow_shared_fallback: bool = True,
        allow_non_jid_fallback: bool = False,
        attempts: int = 2,
        timeout_seconds: float = 25.0,
        priority: str = 'P1',
    ) -> Dict[str, Any]:
        if hasattr(service, '_probe_baileys_binding_group_state'):
            result = service._probe_baileys_binding_group_state(
                responsible_type=responsible_type,
                binding=binding,
                runtime_state=runtime_state,
                session_state=session_state,
                allow_shared_fallback=allow_shared_fallback,
                allow_non_jid_fallback=allow_non_jid_fallback,
                attempts=attempts,
                timeout_seconds=timeout_seconds,
                priority=priority,
            )
            if isinstance(result, dict) and result:
                return result
        return {}

    def execute_registration_group_approval(
        self,
        *,
        service: Any,
        payload: Any,
        approval_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if hasattr(service, '_registration_group_baileys_approval_decision_sync'):
            result = service._registration_group_baileys_approval_decision_sync(payload, approval_run_id=approval_run_id)
            if isinstance(result, dict) and result:
                return result
        return {}


class DefaultWhatsAppApprovalRuntimeAdapter:
    """Runtime abstraction layer for WhatsApp approval operations."""

    def __init__(self) -> None:
        self.legacy_provider = LegacyPlaywrightRuntime()
        self.baileys_provider = BaileysRuntimeProvider()

    @staticmethod
    def _binding_mode(binding: Dict[str, Any], account: Optional[Dict[str, Any]] = None) -> str:
        return resolve_whatsapp_approval_provider_mode(binding=binding, account=account)

    @staticmethod
    def _shadow_meta(*, provider_name: str, provider_mode: str, result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        payload = dict(result or {}) if isinstance(result, dict) else {}
        source = dict(payload.get('source') or {}) if isinstance(payload.get('source'), dict) else {}
        return {
            'provider': provider_name,
            'provider_mode': provider_mode,
            'trust_status': payload.get('trust_status'),
            'reason_code': payload.get('reason_code'),
            'pending_count': payload.get('pending_count'),
            'trusted_pending_count': payload.get('trusted_pending_count'),
            'group_id': payload.get('group_id'),
            'group_name': payload.get('group_name'),
            'source': source,
        }

    @classmethod
    def _attach_shadow_compare(
        cls,
        result: Dict[str, Any],
        *,
        decision: ProviderDecision,
        primary_provider: str,
        shadow_result: Optional[Dict[str, Any]] = None,
        legacy_result: Optional[Dict[str, Any]] = None,
        baileys_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        merged = dict(result or {})
        merged['primary_provider'] = primary_provider
        merged['provider_mode'] = decision.provider_mode
        if shadow_result is not None:
            merged['shadow_compare'] = cls._shadow_meta(
                provider_name='baileys' if primary_provider != 'baileys' else 'legacy_playwright',
                provider_mode=decision.provider_mode,
                result=shadow_result,
            )
        if legacy_result is not None:
            merged['legacy_result_meta'] = cls._shadow_meta(
                provider_name='legacy_playwright',
                provider_mode='legacy_only',
                result=legacy_result,
            )
        if baileys_result is not None:
            merged['baileys_result_meta'] = cls._shadow_meta(
                provider_name='baileys',
                provider_mode=decision.provider_mode,
                result=baileys_result,
            )
        return merged

    def provider_decision(self, *, account: Dict[str, Any], binding: Dict[str, Any]) -> ProviderDecision:
        mode = self._binding_mode(binding, account)
        provider_name = 'baileys' if mode in BAILEYS_PROVIDER_MODES else 'legacy_playwright'
        shadow_enabled = mode in SHADOW_PROVIDER_MODES
        advisory_enabled = mode in {'baileys_advisory', 'baileys_authoritative', 'baileys_manual_approve_gray', 'baileys_primary'}
        authoritative_read = mode in AUTHORITATIVE_PROVIDER_MODES or mode == 'baileys_manual_approve_gray'
        manual_approve_enabled = mode in MANUAL_APPROVE_PROVIDER_MODES
        capabilities = {
            'shadow_read': shadow_enabled,
            'advisory_verify': advisory_enabled,
            'authoritative_read': authoritative_read,
            'manual_approve': manual_approve_enabled,
            'auto_approve': False,
            'official_group_approval': manual_approve_enabled,
            'group_member_lookup': provider_name == 'baileys',
            'group_metadata': provider_name == 'baileys',
            'assistant_group_runtime': provider_name == 'baileys',
        }
        return ProviderDecision(
            provider_name=provider_name,
            provider_mode=mode,
            source='binding' if binding else 'account',
            capabilities=capabilities,
            shadow_enabled=shadow_enabled,
            advisory_enabled=advisory_enabled,
            authoritative_read=authoritative_read,
            manual_approve_enabled=manual_approve_enabled,
        )

    def _provider(self, *, account: Dict[str, Any], binding: Dict[str, Any]) -> WARuntimeProvider:
        decision = self.provider_decision(account=account, binding=binding)
        if decision.provider_name == 'baileys':
            return self.baileys_provider
        return self.legacy_provider

    def full_queue_sync(self, *, service: Any, account: Dict[str, Any], binding: Dict[str, Any], timeout_seconds: float, priority: str = 'P1') -> Dict[str, Any]:
        decision = self.provider_decision(account=account, binding=binding)
        legacy_result: Optional[Dict[str, Any]] = None
        baileys_result: Optional[Dict[str, Any]] = None

        if decision.provider_mode in BAILEYS_PROVIDER_MODES:
            baileys_result = self.baileys_provider.full_queue_sync(service=service, account=account, binding=binding, timeout_seconds=timeout_seconds, priority=priority)
        if decision.provider_mode == 'legacy_only' or decision.provider_mode in SHADOW_PROVIDER_MODES:
            legacy_result = self.legacy_provider.full_queue_sync(service=service, account=account, binding=binding, timeout_seconds=timeout_seconds, priority=priority)

        if decision.provider_mode in AUTHORITATIVE_PROVIDER_MODES or decision.provider_mode == 'baileys_manual_approve_gray':
            provider = self.baileys_provider
            result = self._attach_shadow_compare(
                dict((baileys_result if isinstance(baileys_result, dict) and baileys_result else legacy_result) or {}),
                decision=decision,
                primary_provider='baileys' if isinstance(baileys_result, dict) and baileys_result else 'legacy_playwright',
                legacy_result=legacy_result,
                baileys_result=baileys_result,
            )
        elif decision.provider_mode in ADVISORY_PROVIDER_MODES:
            provider = self.legacy_provider
            result = self._attach_shadow_compare(
                dict(legacy_result or {}),
                decision=decision,
                primary_provider='legacy_playwright',
                shadow_result=baileys_result,
                legacy_result=legacy_result,
                baileys_result=baileys_result,
            )
            result['advisory_match'] = bool(
                isinstance(legacy_result, dict)
                and isinstance(baileys_result, dict)
                and str(legacy_result.get('trust_status') or '') == str(baileys_result.get('trust_status') or '')
                and legacy_result.get('pending_count') == baileys_result.get('pending_count')
            )
        elif decision.provider_mode == 'baileys_shadow':
            provider = self.legacy_provider
            result = self._attach_shadow_compare(
                dict(legacy_result or {}),
                decision=decision,
                primary_provider='legacy_playwright',
                shadow_result=baileys_result,
                legacy_result=legacy_result,
                baileys_result=baileys_result,
            )
        else:
            provider = self.legacy_provider
            result = legacy_result if isinstance(legacy_result, dict) else provider.full_queue_sync(service=service, account=account, binding=binding, timeout_seconds=timeout_seconds, priority=priority)

        if isinstance(result, dict):
            result = {**result, **decision.to_dict(), 'provider': provider.provider_name}
            source = dict(result.get('source') or {}) if isinstance(result.get('source'), dict) else {}
            source.setdefault('provider', provider.provider_name)
            source.setdefault('mode', decision.provider_mode)
            result['source'] = source
        return result

    def registration_group_executor_state(
        self,
        *,
        service: Any,
        registration_group: str,
        allow_legacy_target: bool = False,
    ) -> Dict[str, Any]:
        if hasattr(service, '_registration_group_baileys_executor_group_state'):
            result = service._registration_group_baileys_executor_group_state(
                registration_group,
                allow_legacy_target=allow_legacy_target,
            )
            if isinstance(result, dict) and result:
                return result
        return self.legacy_provider.registration_group_executor_state(
            service=service,
            registration_group=registration_group,
            allow_legacy_target=allow_legacy_target,
        )

    def probe_binding_group_state(
        self,
        *,
        service: Any,
        responsible_type: str,
        binding: Dict[str, Any],
        runtime_state: Dict[str, Any],
        session_state: Dict[str, Any],
        allow_shared_fallback: bool = True,
        allow_non_jid_fallback: bool = False,
        attempts: int = 2,
        timeout_seconds: float = 25.0,
        priority: str = 'P1',
    ) -> Dict[str, Any]:
        account = {
            'provider_mode': runtime_state.get('provider_mode')
            or binding.get('provider_mode')
            or binding.get('registration_group_runtime')
            or binding.get('official_group_runtime')
            or binding.get('group_assistant_runtime'),
            'responsible_type': responsible_type,
        }
        decision = self.provider_decision(account=account, binding=binding)
        legacy_result: Optional[Dict[str, Any]] = None
        baileys_result: Optional[Dict[str, Any]] = None

        if decision.provider_mode in BAILEYS_PROVIDER_MODES:
            baileys_result = self.baileys_provider.probe_binding_group_state(
                service=service,
                responsible_type=responsible_type,
                binding=binding,
                runtime_state=runtime_state,
                session_state=session_state,
                allow_shared_fallback=allow_shared_fallback,
                allow_non_jid_fallback=allow_non_jid_fallback,
                attempts=attempts,
                timeout_seconds=timeout_seconds,
                priority=priority,
            )
        if decision.provider_mode == 'legacy_only' or decision.provider_mode in SHADOW_PROVIDER_MODES:
            legacy_result = self.legacy_provider.probe_binding_group_state(
                service=service,
                responsible_type=responsible_type,
                binding=binding,
                runtime_state=runtime_state,
                session_state=session_state,
                allow_shared_fallback=allow_shared_fallback,
                allow_non_jid_fallback=allow_non_jid_fallback,
                attempts=attempts,
                timeout_seconds=timeout_seconds,
                priority=priority,
            )

        if decision.provider_mode in AUTHORITATIVE_PROVIDER_MODES or decision.provider_mode == 'baileys_manual_approve_gray':
            provider = self.baileys_provider
            result = self._attach_shadow_compare(
                dict((baileys_result if isinstance(baileys_result, dict) and baileys_result else legacy_result) or {}),
                decision=decision,
                primary_provider='baileys' if isinstance(baileys_result, dict) and baileys_result else 'legacy_playwright',
                legacy_result=legacy_result,
                baileys_result=baileys_result,
            )
        elif decision.provider_mode in ADVISORY_PROVIDER_MODES or decision.provider_mode == 'baileys_shadow':
            provider = self.legacy_provider
            result = self._attach_shadow_compare(
                dict(legacy_result or {}),
                decision=decision,
                primary_provider='legacy_playwright',
                shadow_result=baileys_result,
                legacy_result=legacy_result,
                baileys_result=baileys_result,
            )
        else:
            provider = self.legacy_provider
            result = legacy_result if isinstance(legacy_result, dict) else provider.probe_binding_group_state(
                service=service,
                responsible_type=responsible_type,
                binding=binding,
                runtime_state=runtime_state,
                session_state=session_state,
                allow_shared_fallback=allow_shared_fallback,
                allow_non_jid_fallback=allow_non_jid_fallback,
                attempts=attempts,
                timeout_seconds=timeout_seconds,
                priority=priority,
            )

        if isinstance(result, dict):
            result = {**result, **decision.to_dict(), 'provider': provider.provider_name}
        return result

    def execute_registration_group_approval(
        self,
        *,
        service: Any,
        payload: Any,
        approval_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        account = getattr(payload, '__dict__', {}) if payload is not None else {}
        binding = {
            'provider_mode': account.get('provider_mode')
            or account.get('registration_group_runtime')
            or account.get('official_group_runtime')
            or account.get('group_assistant_runtime'),
            'responsible_type': 'registration_group',
        }
        provider = self._provider(account=account, binding=binding)
        result = provider.execute_registration_group_approval(
            service=service,
            payload=payload,
            approval_run_id=approval_run_id,
        )
        if isinstance(result, dict):
            decision = self.provider_decision(account=account, binding=binding)
            result = {**result, **decision.to_dict(), 'provider': provider.provider_name}
        return result
