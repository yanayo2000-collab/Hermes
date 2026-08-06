"""Stable Timo guild identity and current provider-facing display names."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TimoGuildIdentity:
    storage_name: str
    display_name: str
    guild_id: str
    guild_sid: str
    crm_dept_id: str = ''


TIMO_GUILD_IDENTITIES = (
    TimoGuildIdentity('Agency MX somente', 'Royal Latam', '22000408', 'lvmy210446316420ie3d', '2065642336046751745'),
    TimoGuildIdentity('TIMO001', 'Royal ID', '11003905', '11003905', '2076510867797811201'),
    TimoGuildIdentity('agency of BR somente', 'Royal BR', '22000448', '22000448', '2075087447809826817'),
)


def _key(value: object) -> str:
    return str(value or '').strip().casefold()


def resolve_timo_guild_identity(
    value: object = '', *, guild_id: object = '', guild_sid: object = ''
) -> TimoGuildIdentity | None:
    candidates = {_key(value), _key(guild_id), _key(guild_sid)} - {''}
    for identity in TIMO_GUILD_IDENTITIES:
        known = {
            _key(identity.storage_name),
            _key(identity.display_name),
            _key(identity.guild_id),
            _key(identity.guild_sid),
        }
        if candidates & known:
            return identity
    return None


def require_timo_guild_identity(
    value: object = '', *, guild_id: object = '', guild_sid: object = ''
) -> TimoGuildIdentity:
    """Resolve one stable identity and fail closed on unknown or conflicting references."""
    references = [('guild_name', value), ('guild_id', guild_id), ('guild_sid', guild_sid)]
    resolved = []
    for field, raw in references:
        if not _key(raw):
            continue
        identity = resolve_timo_guild_identity(raw)
        if identity is None:
            raise ValueError(f'timo_guild_identity_unknown:{field}')
        resolved.append(identity)
    if not resolved:
        raise ValueError('timo_guild_identity_required')
    if len({identity.guild_id for identity in resolved}) != 1:
        raise ValueError('timo_guild_identity_mismatch')
    return resolved[0]


def timo_guild_contract_fields(
    value: object = '', *, guild_id: object = '', guild_sid: object = ''
) -> dict[str, str]:
    """Return the ID-first handshake fields for one known Timo guild."""
    identity = resolve_timo_guild_identity(value, guild_id=guild_id, guild_sid=guild_sid)
    if identity is None:
        return {}
    return {
        'guild_id': identity.guild_id,
        'guild_sid': identity.guild_sid,
        'guild_name': identity.display_name,
        'guild_display_name': identity.display_name,
        'guild_storage_name': identity.storage_name,
    }


def timo_guild_display_name(value: object = '', *, guild_id: object = '', guild_sid: object = '') -> str:
    identity = resolve_timo_guild_identity(value, guild_id=guild_id, guild_sid=guild_sid)
    return identity.display_name if identity else str(value or '').strip()


def timo_guild_storage_name(value: object = '', *, guild_id: object = '', guild_sid: object = '') -> str:
    identity = resolve_timo_guild_identity(value, guild_id=guild_id, guild_sid=guild_sid)
    return identity.storage_name if identity else str(value or '').strip()


def timo_guild_aliases(value: object = '', *, guild_id: object = '', guild_sid: object = '') -> tuple[str, ...]:
    identity = resolve_timo_guild_identity(value, guild_id=guild_id, guild_sid=guild_sid)
    if not identity:
        normalized = str(value or '').strip()
        return (normalized,) if normalized else ()
    return identity.storage_name, identity.display_name, identity.guild_id, identity.guild_sid


def decorate_timo_guild_display_names(value: Any) -> Any:
    """Add display names to public payload rows without changing storage keys."""
    if isinstance(value, list):
        return [decorate_timo_guild_display_names(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: decorate_timo_guild_display_names(item) for key, item in value.items()}
    storage_name = str(result.get('guild_storage_name') or result.get('guild_name') or '').strip()
    contract = timo_guild_contract_fields(
        storage_name,
        guild_id=result.get('guild_id') or result.get('cms_guild_id'),
        guild_sid=result.get('guild_sid') or result.get('cms_guild_sid'),
    )
    if contract:
        result['guild_id'] = contract['guild_id']
        result['guild_sid'] = contract['guild_sid']
        result['guild_display_name'] = contract['guild_display_name']
    elif storage_name:
        result['guild_display_name'] = storage_name
    return result


def externalize_timo_guild_names(value: Any) -> Any:
    """Expose current names to external consumers while retaining the stable storage key."""
    if isinstance(value, list):
        return [externalize_timo_guild_names(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: externalize_timo_guild_names(item) for key, item in value.items()}
    storage_name = str(result.get('guild_storage_name') or result.get('guild_name') or '').strip()
    contract = timo_guild_contract_fields(
        storage_name,
        guild_id=result.get('guild_id') or result.get('cms_guild_id'),
        guild_sid=result.get('guild_sid') or result.get('cms_guild_sid'),
    )
    if contract:
        result.update(contract)
    elif storage_name:
        result['guild_storage_name'] = storage_name
        result['guild_name'] = storage_name
        result['guild_display_name'] = storage_name
    return result
