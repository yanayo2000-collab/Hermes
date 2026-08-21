from __future__ import annotations

from typing import Any, Mapping

from app.timo_guild_identity import TIMO_GUILD_IDENTITIES


COUNTRY_BY_GUILD_ID = {'22000408': 'MX', '11003905': 'ID', '22000448': 'BR'}
COUNTRY_NAME_BY_CODE = {'MX': 'Mexico', 'ID': 'Indonesia', 'BR': 'Brazil'}
SOURCE_MISSING_MARKERS = (
    'source_not_ready',
    'timo_revenue_export_not_ready',
    'export_url_not_ready',
    'circuit open until',
    'ticket',
)


def _expected_identities(*, country: str = '', guild_name: str = '') -> list[Any]:
    normalized_country = str(country or '').strip().casefold()
    normalized_guild = str(guild_name or '').strip().casefold()
    expected = []
    for identity in TIMO_GUILD_IDENTITIES:
        code = COUNTRY_BY_GUILD_ID[identity.guild_id]
        country_names = {code.casefold(), COUNTRY_NAME_BY_CODE[code].casefold()}
        if normalized_country and normalized_country not in country_names:
            continue
        if normalized_guild and normalized_guild not in {
            identity.storage_name.casefold(),
            identity.display_name.casefold(),
            identity.guild_id.casefold(),
            identity.guild_sid.casefold(),
        }:
            continue
        expected.append(identity)
    return expected


def _latest_failures(conn: Any, business_date: str) -> dict[str, dict[str, str]]:
    rows = conn.execute(
        """
        SELECT guild_name, status, error_code, error, start_time
        FROM timo_sync_run_log
        WHERE stat_date_bj=?
        ORDER BY guild_name, start_time DESC
        """,
        (business_date,),
    ).fetchall()
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        guild_name = str(row['guild_name'] or '')
        if guild_name in result:
            continue
        result[guild_name] = {
            'status': str(row['status'] or ''),
            'error_code': str(row['error_code'] or '')[:120],
            'error': str(row['error'] or '')[:240],
        }
    return result


def _quality_status(failure: Mapping[str, str], integrity_errors: list[str]) -> str:
    if integrity_errors:
        return 'ANOMALY'
    evidence = f"{failure.get('error_code', '')}:{failure.get('error', '')}".casefold()
    return 'SOURCE_MISSING' if any(marker in evidence for marker in SOURCE_MISSING_MARKERS) else 'ANOMALY'


def enrich_timo_scope_feed_status(
    conn: Any,
    feed_status: Mapping[str, Any],
    *,
    business_date: str,
    country: str = '',
    guild_name: str = '',
) -> dict[str, Any]:
    """Return a complete expected-scope manifest without inventing missing facts."""
    expected = _expected_identities(country=country, guild_name=guild_name)
    existing = {
        str(item.get('guild_name') or item.get('guild_storage_name') or ''): dict(item)
        for item in feed_status.get('scope_manifests') or []
        if isinstance(item, Mapping)
    }
    failures = _latest_failures(conn, business_date)
    watermarks = {
        str(row['guild_name'] or ''): str(row['last_success_time'] or '')
        for row in conn.execute(
            'SELECT guild_name,last_success_time FROM timo_sync_watermark WHERE stat_date_bj=?',
            (business_date,),
        ).fetchall()
    }
    manifests: list[dict[str, Any]] = []
    succeeded = 0
    for identity in expected:
        manifest = existing.get(identity.storage_name) or {}
        integrity_errors = [str(item) for item in manifest.get('integrity_errors') or []]
        row_count = int(manifest.get('row_count') or 0)
        total_income = manifest.get('total_income')
        checksum = str(manifest.get('checksum') or '')
        revision = int(manifest.get('revision_version') or 0)
        sync_id = str(manifest.get('last_success_sync_id') or '')
        ready = bool(
            manifest.get('publication_ready') is True
            and not integrity_errors
            and row_count > 0
            and float(total_income or 0) > 0
            and len(checksum) == 64
            and revision > 0
            and sync_id
        )
        code = COUNTRY_BY_GUILD_ID[identity.guild_id]
        common = {
            'guild_id': identity.guild_id,
            'guild_name': identity.storage_name,
            'guild_storage_name': identity.storage_name,
            'country': COUNTRY_NAME_BY_CODE[code],
            'country_code': code,
            'stat_date_bj': business_date,
        }
        if ready:
            succeeded += 1
            manifests.append({
                **common,
                'data_status': 'complete',
                'quality_status': 'COMPLETE',
                'publication_ready': True,
                'consumable': True,
                'row_count': row_count,
                'total_income': f'{float(total_income):.6f}',
                'checksum': checksum,
                'revision_version': revision,
                'last_success_sync_id': sync_id,
                'source_snapshot_at': str(manifest.get('source_snapshot_at') or ''),
                'last_success_time': watermarks.get(identity.storage_name, ''),
                'observation_count': int(manifest.get('observation_count') or 0),
                'stability_age_seconds': int(manifest.get('stability_age_seconds') or 0),
                'integrity_errors': [],
                'failure_reason': None,
            })
            continue
        failure = failures.get(identity.storage_name) or {}
        failure_reason = (
            failure.get('error_code')
            or (integrity_errors[0] if integrity_errors else '')
            or 'scope_not_observed'
        )
        manifests.append({
            **common,
            'data_status': 'failed',
            'quality_status': _quality_status(failure, integrity_errors),
            'publication_ready': False,
            'consumable': False,
            'row_count': None,
            'total_income': None,
            'checksum': None,
            'revision_version': None,
            'last_success_sync_id': None,
            'source_snapshot_at': None,
            'last_success_time': None,
            'observation_count': int(manifest.get('observation_count') or 0),
            'stability_age_seconds': int(manifest.get('stability_age_seconds') or 0),
            'integrity_errors': integrity_errors,
            'failure_reason': failure_reason,
        })

    failed = len(manifests) - succeeded
    if manifests and failed == 0:
        day_status = 'COMPLETE'
        status = 'complete'
        data_status = 'complete'
    elif succeeded > 0:
        day_status = 'PARTIAL'
        status = 'partial'
        data_status = 'partial'
    else:
        day_status = 'FAILED'
        status = 'failed'
        data_status = 'failed'
    return {
        **dict(feed_status),
        'status': status,
        'data_status': data_status,
        'day_status': day_status,
        'publication_ready': bool(manifests) and failed == 0,
        'consumable': bool(manifests) and failed == 0,
        'expected_scope_count': len(expected),
        'succeeded_scope_count': succeeded,
        'failed_scope_count': failed,
        'failed_scopes': [item['country_code'] for item in manifests if not item['consumable']],
        'scope_manifests': manifests,
        'integrity_contract_version': 'timo_scope_manifest_v2',
    }
