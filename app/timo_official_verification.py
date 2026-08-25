"""Fail-closed evidence for user-authorized official Timo manual exports."""
from __future__ import annotations

from decimal import Decimal
import json
from typing import Any


VERIFICATION_MODE = 'manual_official_verified'
AUTHORIZATION_REF = 'codex-thread:019fd525-d473-7c60-84ce-e28bec016a30:2026-08-25'
MEXICO_GUILD_ID = '22000408'

# A job flag alone cannot bypass re-observation. Every authorized source is
# frozen here by date, guild, file digest, row counts, and total income.
OFFICIAL_MANUAL_VERIFIED_SCOPES: dict[tuple[str, str], dict[str, Any]] = {
    ('2026-08-19', MEXICO_GUILD_ID): {
        'source_sha256': '2f76c9ac028d97a539d81f9b9dff774a0d1fd0401c58a37da1fef90de5e92fae',
        'source_row_count': 5271, 'effective_row_count': 407, 'total_income': '6385485.350000',
    },
    ('2026-08-20', MEXICO_GUILD_ID): {
        'source_sha256': '04e5e87350f708423e5cae1ef89bab3a5f7d9603cb91042cb90b093e292792ac',
        'source_row_count': 5271, 'effective_row_count': 392, 'total_income': '6047524.700000',
    },
    ('2026-08-21', MEXICO_GUILD_ID): {
        'source_sha256': '838038325cd0936f8263c96c40c3c93c924509c7a2cec8753dad3aaf6cf75f52',
        'source_row_count': 5271, 'effective_row_count': 360, 'total_income': '5510762.250000',
    },
    ('2026-08-22', MEXICO_GUILD_ID): {
        'source_sha256': '4c5ceac693e690cd7ca8f28539e9f99d8b3a56dc5dd4b0a05e02fa66ccb126b1',
        'source_row_count': 5271, 'effective_row_count': 340, 'total_income': '5632363.150000',
    },
}


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal('0.000001'))


def build_official_verification_evidence(
    *, mode: str, business_date: str, guild_id: str, source_sha256: str,
    source_row_count: int, effective_row_count: int, total_income: Any,
) -> dict[str, Any] | None:
    if not mode:
        return None
    if mode != VERIFICATION_MODE:
        raise ValueError('official_verification_mode_invalid')
    expected = OFFICIAL_MANUAL_VERIFIED_SCOPES.get((business_date, guild_id))
    if expected is None:
        raise ValueError('official_verification_scope_not_authorized')
    actual = {
        'source_sha256': str(source_sha256).lower(),
        'source_row_count': int(source_row_count),
        'effective_row_count': int(effective_row_count),
        'total_income': f'{_money(total_income):.6f}',
    }
    if actual != expected:
        raise ValueError('official_verification_contract_mismatch')
    return {
        'mode': VERIFICATION_MODE,
        'authorization_ref': AUTHORIZATION_REF,
        'business_date': business_date,
        'guild_id': guild_id,
        **actual,
        'observation_policy': 'explicit_user_verified_no_reobserve',
    }


def scope_has_official_verification_override(
    conn: Any, *, guild_executor_key: str, stat_date_bj: str, checksum: str,
    last_success_sync_id: str, row_count: int, total_income: Any,
) -> bool:
    guild_id = str(guild_executor_key).rsplit(':', 1)[-1]
    expected = OFFICIAL_MANUAL_VERIFIED_SCOPES.get((stat_date_bj, guild_id))
    if expected is None:
        return False
    run = conn.execute(
        """SELECT status,checksum,row_count,gate_evidence_json
           FROM timo_sync_run_log
           WHERE sync_id=? AND guild_executor_key=? AND stat_date_bj=?""",
        (last_success_sync_id, guild_executor_key, stat_date_bj),
    ).fetchone()
    if run is None or str(run['status'] or '') not in {'success', 'no_op'}:
        return False
    if str(run['checksum'] or '') != checksum or int(run['row_count'] or 0) != int(row_count):
        return False
    try:
        provenance = json.loads(str(run['gate_evidence_json'] or '{}')).get('source_provenance') or {}
    except (TypeError, ValueError):
        return False
    verification = provenance.get('official_verification') or {}
    actual = {
        'source_sha256': str(provenance.get('raw_response_sha256') or '').lower(),
        'source_row_count': int(provenance.get('source_row_count') or 0),
        'effective_row_count': int(provenance.get('effective_row_count') or 0),
        'total_income': f'{_money(provenance.get("source_total_income")):.6f}',
    }
    return bool(
        actual == expected
        and verification.get('mode') == VERIFICATION_MODE
        and verification.get('authorization_ref') == AUTHORIZATION_REF
        and verification.get('business_date') == stat_date_bj
        and verification.get('guild_id') == guild_id
        and verification.get('observation_policy') == 'explicit_user_verified_no_reobserve'
        and {key: verification.get(key) for key in expected} == expected
        and int(row_count) == int(expected['effective_row_count'])
        and _money(total_income) == _money(expected['total_income'])
        and len(checksum) == 64
        and str(last_success_sync_id).startswith(
            f'timo_manual_official_{stat_date_bj.replace("-", "")}_{guild_id}_'
        )
    )
