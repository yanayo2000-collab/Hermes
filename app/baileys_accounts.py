from __future__ import annotations

import re
import uuid
from typing import Any, Mapping, Optional


BAILEYS_ACCOUNT_ID_KEYS = ('baileys_account_id', 'provider_account_id', 'account_id')


def default_baileys_account_id_for_account_key(account_key: str) -> str:
    normalized = re.sub(r'[^a-zA-Z0-9_.-]+', '-', str(account_key or '').strip().lower()).strip('-._')
    if not normalized:
        normalized = f"wa-{uuid.uuid4().hex[:12]}"
    return normalized[:96].strip('-._') or f"wa-{uuid.uuid4().hex[:12]}"


def first_baileys_account_id(*sources: Optional[Mapping[str, Any]]) -> str:
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key in BAILEYS_ACCOUNT_ID_KEYS:
            value = str(source.get(key) or '').strip()
            if value:
                return value
    return ''


def apply_baileys_account_id_aliases(target: Mapping[str, Any], account_id: str) -> dict[str, Any]:
    row = dict(target or {})
    normalized = str(account_id or '').strip()
    if not normalized:
        return row
    row['baileys_account_id'] = normalized
    row['provider_account_id'] = normalized
    row['account_id'] = normalized
    return row


def resolve_baileys_account_id_for_card(*, account_key: str, explicit_runtime: Optional[Mapping[str, Any]] = None, bindings: Optional[list[Mapping[str, Any]]] = None) -> str:
    explicit = first_baileys_account_id(explicit_runtime)
    if explicit:
        return explicit
    binding_ids = {first_baileys_account_id(binding) for binding in bindings or []}
    binding_ids.discard('')
    if len(binding_ids) == 1:
        return next(iter(binding_ids))
    return default_baileys_account_id_for_account_key(account_key)
