from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional


ACCOUNT_ACCESS_STATES = {
    'active', 'disabled', 'banned', 'access_revoked',
    'permission_error', 'unknown_403',
}
SILENT_STATES = {'disabled', 'banned'}
SYNCABLE_STATES = {'active', 'access_revoked', 'permission_error', 'unknown_403'}


def normalize_meta_account_id(value: object) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    return text if text.startswith('act_') else f'act_{text}'


def _account_set(values: Iterable[object]) -> set[str]:
    return {normalized for value in values if (normalized := normalize_meta_account_id(value))}


def _environment_values(name: str) -> list[str]:
    return [part.strip() for part in str(os.getenv(name) or '').split(',') if part.strip()]


def _error_payload(response: Any) -> dict[str, Any]:
    if response is None:
        return {}
    try:
        payload = response.json()
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _error_details(response: Any) -> tuple[int, str, str, str]:
    status_code = int(getattr(response, 'status_code', 0) or 0)
    payload = _error_payload(response)
    error = payload.get('error') if isinstance(payload.get('error'), dict) else payload
    code = str(error.get('code') or '') if isinstance(error, dict) else ''
    subcode = str(error.get('error_subcode') or '') if isinstance(error, dict) else ''
    message = str(error.get('message') or '') if isinstance(error, dict) else ''
    if not message:
        message = str(getattr(response, 'text', '') or '')
    return status_code, code, subcode, message[:500]


@dataclass(frozen=True)
class MetaAccountAccessDecision:
    account_id: str
    state: str
    should_sync: bool
    should_alert: bool
    reason: str
    status_code: int = 0
    error_code: str = ''
    error_subcode: str = ''

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MetaAdAccountAccessPolicy:
    def __init__(
        self,
        *,
        disabled_account_ids: Iterable[object] = (),
        banned_account_ids: Iterable[object] = (),
    ) -> None:
        self.disabled_account_ids = _account_set(disabled_account_ids)
        self.banned_account_ids = _account_set(banned_account_ids)

    @classmethod
    def from_environment(cls) -> 'MetaAdAccountAccessPolicy':
        return cls(
            disabled_account_ids=_environment_values('META_ADS_DISABLED_ACCOUNT_IDS'),
            banned_account_ids=_environment_values('META_ADS_BANNED_ACCOUNT_IDS'),
        )

    def configured(self, account_id: object) -> MetaAccountAccessDecision:
        normalized = normalize_meta_account_id(account_id)
        if normalized in self.banned_account_ids:
            return MetaAccountAccessDecision(
                normalized, 'banned', False, False, 'explicitly_confirmed_banned',
            )
        if normalized in self.disabled_account_ids:
            return MetaAccountAccessDecision(
                normalized, 'disabled', False, False, 'explicitly_disabled',
            )
        return MetaAccountAccessDecision(normalized, 'active', True, False, 'configured_active')

    def classify_response(
        self,
        account_id: object,
        response: Any,
        *,
        previous_state: str = 'active',
    ) -> MetaAccountAccessDecision:
        configured = self.configured(account_id)
        if not configured.should_sync:
            return configured
        status_code, code, subcode, message = _error_details(response)
        if status_code != 403:
            return MetaAccountAccessDecision(
                configured.account_id, 'active', True, False,
                'request_accessible' if status_code < 400 else f'http_{status_code}',
                status_code, code, subcode,
            )
        lowered = message.lower()
        if code == '190' or any(marker in lowered for marker in (
            'access token', 'session has expired', 'invalid oauth',
        )):
            state = 'access_revoked'
            reason = 'oauth_access_revoked'
        elif code == '200' or any(marker in lowered for marker in (
            'permission', 'not authorized', 'does not have permission',
        )):
            state = 'permission_error'
            reason = 'meta_permission_error'
        else:
            state = 'unknown_403'
            reason = 'meta_403_unclassified'
        previous = str(previous_state or 'active').strip().lower()
        return MetaAccountAccessDecision(
            configured.account_id, state, True, previous == 'active', reason,
            status_code, code, subcode,
        )


def classify_meta_exception(
    policy: MetaAdAccountAccessPolicy,
    account_id: object,
    exc: BaseException,
    *,
    previous_state: str = 'active',
) -> Optional[MetaAccountAccessDecision]:
    response = getattr(exc, 'response', None)
    if int(getattr(response, 'status_code', 0) or 0) != 403:
        return None
    return policy.classify_response(account_id, response, previous_state=previous_state)


def access_summary(decisions: Iterable[MetaAccountAccessDecision]) -> dict[str, Any]:
    rows = [decision.to_dict() for decision in decisions]
    return {
        'rows': rows,
        'counts': {
            state: sum(1 for row in rows if row['state'] == state)
            for state in sorted(ACCOUNT_ACCESS_STATES)
        },
        'silent_account_ids': [row['account_id'] for row in rows if row['state'] in SILENT_STATES],
        'alert_account_ids': [row['account_id'] for row in rows if row['should_alert']],
    }
