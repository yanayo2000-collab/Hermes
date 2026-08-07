from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


AD_DASHBOARD_FACT_COLUMNS = (
    'cost',
    'installs',
    'af_installs',
    'registrations',
    'meta_installs',
    'meta_registrations',
    'af_registrations',
    'onsite_registrations',
    'high_value_users',
    'im_entries',
    'auto_apply_message_users',
    'im_first_replies',
    'im_step2_triggers',
    'im_manual_reply_3',
    'im_user_message_ge_5_users',
    'im_link_clicks',
    'im_link_click_users',
    'link_click_users',
    'linky_register_users',
    'bind_success_users',
    'crm_succeed_users',
    'high_intent_im_users',
    'guild_joins',
    'promotion_guild_joins',
    'organic_guild_joins',
    'tugao_join_success_users',
    'tugao_join_success_no_wa_users',
    'meta_guild_joins',
    'af_guild_joins',
    'purchases',
    'revenue',
    'clicks',
    'link_clicks',
    'impressions',
    'reach',
)

QUALIFIED_JOIN_SOURCE_FIELD = 'guild_join_success_users'
QUALIFIED_JOIN_METRIC_CONTRACT = 'tugao_funnel_daily_metrics_api_v1'
QUALIFIED_JOIN_FACT_COLUMNS = (
    'tugao_join_success_users',
    'tugao_join_success_no_wa_users',
)

AD_DASHBOARD_TARGET_APP_LINKY_META_ACCOUNTS = {
    '1898261564216326',
    '1511281443796277',
    '1022472447112808',
    '2014618999169375',
    '865675816544216',
}
AD_DASHBOARD_TARGET_APP_TIMO_META_ACCOUNTS = {
    '1293506106236750',
    '1625526805175773',
    '1457588552349197',
    '1250000910496826',
}
AD_DASHBOARD_TARGET_APP_LINKY_ALIASES = {'linky'}
AD_DASHBOARD_TARGET_APP_TIMO_ALIASES = {'com.timetrade.duitan', 'duitan', 'timo'}


def _parse_dashboard_date(value: Any) -> Optional[date]:
    text = str(value or '').strip()
    if not text:
        return None
    text = text[:10]
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%d/%m/%Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _empty_ad_metrics() -> Dict[str, float]:
    return {key: 0.0 for key in AD_DASHBOARD_FACT_COLUMNS}


def _add_ad_metrics(target: Dict[str, float], row: Dict[str, Any]) -> None:
    for key in AD_DASHBOARD_FACT_COLUMNS:
        target[key] = float(target.get(key) or 0.0) + float((row or {}).get(key) or 0.0)


def _qualified_join_row_metadata(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    observed = (row or {}).get('qualified_join_metric_observed')
    if observed is not True:
        return None
    if str((row or {}).get('data_source') or '').strip().lower() != 'tugaofunnel':
        raise ValueError('qualified_join_source_must_be_tugaofunnel')
    if str((row or {}).get('qualified_join_source_field') or '').strip() != QUALIFIED_JOIN_SOURCE_FIELD:
        raise ValueError('qualified_join_source_field_mismatch')
    if str((row or {}).get('source_metric_contract') or '').strip() != QUALIFIED_JOIN_METRIC_CONTRACT:
        raise ValueError('qualified_join_metric_contract_mismatch')
    counts: Dict[str, int] = {}
    for key in QUALIFIED_JOIN_FACT_COLUMNS:
        value = (row or {}).get(key)
        if isinstance(value, bool):
            raise ValueError(f'qualified_join_invalid_count:{key}')
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f'qualified_join_invalid_count:{key}') from None
        if number < 0 or not number.is_integer() or number == float('inf') or number != number:
            raise ValueError(f'qualified_join_invalid_count:{key}')
        counts[key] = int(number)
    identity = tuple(str((row or {}).get(key) or '').strip() for key in (
        'campaign_id', 'adset_id', 'ad_id', 'external_app',
    ))
    exact_ready = all(identity)
    return {
        'identity': identity,
        'exact_ready': exact_ready,
        'counts': counts,
    }


def _ad_missing_account_label() -> str:
    return '未归属广告账户'


def _ad_account_label_or_missing(value: Any) -> str:
    label = str(value or '').strip()
    if not label:
        return _ad_missing_account_label()
    if re.match(r'^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$', label, flags=re.IGNORECASE):
        return _ad_missing_account_label()
    return label


def _normalize_ad_dashboard_target_app(value: Any) -> str:
    normalized = str(value or '').strip().lower()
    if normalized in {'', 'all', '全部', '__all__'}:
        return 'all'
    if normalized in {'linky', 'link'}:
        return 'linky'
    if normalized == 'timo':
        return 'timo'
    return ''


def _normalize_ad_account_id_candidate(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    act_match = re.search(r'act[_\s-]*(\d{8,})', text, flags=re.IGNORECASE)
    if act_match:
        return act_match.group(1)
    digit_match = re.search(r'\b(\d{12,20})\b', text)
    return digit_match.group(1) if digit_match else ''


def _normalize_ad_app_alias_candidate(value: Any) -> str:
    text = str(value or '').strip().lower()
    if not text:
        return ''
    return re.sub(r'\s+', '', text.replace('act_', ''))


def _ad_dashboard_target_app_from_account(
    *,
    platform: Any,
    account_id: Any = '',
    account_label: Any = '',
    appsflyer_app_id: Any = '',
) -> str:
    normalized_appsflyer_app_id = _normalize_ad_app_alias_candidate(appsflyer_app_id)
    if normalized_appsflyer_app_id in AD_DASHBOARD_TARGET_APP_LINKY_ALIASES:
        return 'linky'
    if normalized_appsflyer_app_id in AD_DASHBOARD_TARGET_APP_TIMO_ALIASES:
        return 'timo'
    if str(platform or '').strip().lower() != 'meta':
        return 'inactive'
    normalized_account_id = _normalize_ad_account_id_candidate(account_id) or _normalize_ad_account_id_candidate(account_label)
    if normalized_account_id in AD_DASHBOARD_TARGET_APP_LINKY_META_ACCOUNTS:
        return 'linky'
    if normalized_account_id in AD_DASHBOARD_TARGET_APP_TIMO_META_ACCOUNTS:
        return 'timo'
    account_text = ' '.join([str(account_id or ''), str(account_label or '')]).strip().lower()
    if re.search(r'(^|[\s_-])tm($|[\s_-])', account_text):
        return 'timo'
    if re.search(r'(^|[\s_-])lk($|[\s_-])', account_text):
        return 'linky'
    return 'inactive'


def _ad_dashboard_row_target_app(row: Dict[str, Any]) -> str:
    explicit = str((row or {}).get('target_app') or '').strip().lower()
    data_source = str((row or {}).get('data_source') or '').strip().lower()
    external_target_app = _normalize_ad_dashboard_target_app((row or {}).get('external_app'))
    is_tugao_natural = (
        data_source in {'tugaofunnel', 'tugao_funnel', 'tugao_onsite_funnel'}
        and str((row or {}).get('platform') or '').strip().lower() == 'internal'
    )
    if is_tugao_natural and external_target_app in {'linky', 'timo'}:
        return external_target_app
    inferred = _ad_dashboard_target_app_from_account(
        platform=(row or {}).get('platform'),
        account_id=(row or {}).get('account_id') or (row or {}).get('ad_account_id'),
        account_label=(row or {}).get('app_id'),
        appsflyer_app_id=(row or {}).get('appsflyer_app_id'),
    )
    if inferred in {'linky', 'timo'}:
        return inferred
    return explicit if explicit in {'linky', 'timo', 'inactive'} else 'inactive'


def _normalize_ad_fact_account_value(row: Dict[str, Any]) -> str:
    data_source = str((row or {}).get('data_source') or '').strip().lower()
    if data_source in {'tugaofunnel', 'tugao_funnel', 'tugao_onsite_funnel'}:
        return ''
    return str((row or {}).get('app_id') or '').strip()


def _ad_fact_grain_key(row: Dict[str, Any]) -> Tuple[str, ...]:
    base = (
        str((row or {}).get('date') or '').strip(),
        str((row or {}).get('data_source') or '').strip() or 'Unknown',
        str((row or {}).get('platform') or '').strip() or 'Unknown',
        _normalize_ad_fact_account_value(row),
        str((row or {}).get('appsflyer_app_id') or '').strip(),
        str((row or {}).get('country') or '').strip() or 'Unknown',
        str((row or {}).get('media_source') or '').strip(),
        str((row or {}).get('campaign') or '').strip() or '未命名',
        str((row or {}).get('ad_group') or '').strip(),
        str((row or {}).get('ad') or '').strip(),
        str((row or {}).get('source_type') or '').strip(),
    )
    if (
        (row or {}).get('qualified_join_metric_observed') is True
        and str((row or {}).get('data_source') or '').strip().lower() == 'tugaofunnel'
    ):
        return (
            'tugao-qualified-v1',
            str((row or {}).get('date') or '').strip(),
            str((row or {}).get('data_source') or '').strip() or 'TugaoFunnel',
            str((row or {}).get('country') or '').strip() or 'Unknown',
            str((row or {}).get('media_source') or '').strip(),
            str((row or {}).get('campaign_id') or '').strip(),
            str((row or {}).get('adset_id') or '').strip(),
            str((row or {}).get('ad_id') or '').strip(),
            str((row or {}).get('external_app') or '').strip(),
        )
    return base


def _ad_fact_row_id(row: Dict[str, Any]) -> str:
    raw = json.dumps(_ad_fact_grain_key(row), ensure_ascii=False, sort_keys=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _ad_country_is_unknown(value: Any) -> bool:
    return str(value or '').strip().lower() in {'', 'unknown', '未命名', '未知'}


AD_HISTORICAL_SETTLEMENT_UNALLOCATED_COUNTRY = 'HistoricalSettlementUnallocated'


def _ad_prepare_country_dimension(row: Dict[str, Any]) -> Dict[str, Any]:
    """Keep historical settlements explicit instead of disguising them as a country."""
    item = dict(row or {})
    data_source = str(item.get('data_source') or '').strip().lower()
    account_id = str(item.get('account_id') or item.get('ad_account_id') or '').strip().lower()
    historical_recovery = bool(item.get('historical_recovery')) or account_id == 'archived_settled_accounts'
    if data_source == 'meta' and historical_recovery and _ad_country_is_unknown(item.get('country')):
        item['country'] = AD_HISTORICAL_SETTLEMENT_UNALLOCATED_COUNTRY
        item['country_attribution_status'] = 'historical_settlement_unallocated'
        item['historical_recovery'] = True
    return item


def _ad_country_enrichment_key(row: Dict[str, Any], *, include_ad: bool = True) -> Tuple[str, ...]:
    return (
        str((row or {}).get('date') or '').strip(),
        str((row or {}).get('platform') or '').strip().lower(),
        _ad_account_label_or_missing((row or {}).get('app_id')).lower(),
        str((row or {}).get('campaign') or '').strip().lower(),
        str((row or {}).get('ad_group') or '').strip().lower(),
        str((row or {}).get('ad') or '').strip().lower() if include_ad else '',
    )


def _ad_enrich_unknown_countries(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    known_by_exact: Dict[Tuple[str, ...], str] = {}
    known_by_group: Dict[Tuple[str, ...], str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        country = str(row.get('country') or '').strip()
        if _ad_country_is_unknown(country):
            continue
        exact_key = _ad_country_enrichment_key(row, include_ad=True)
        group_key = _ad_country_enrichment_key(row, include_ad=False)
        known_by_exact.setdefault(exact_key, country)
        known_by_group.setdefault(group_key, country)
    enriched: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if (
            row.get('qualified_join_metric_observed') is True
            and str(row.get('data_source') or '').strip().lower() == 'tugaofunnel'
        ):
            enriched.append(row)
            continue
        if str(row.get('country_attribution_status') or '').strip() in {
            'unresolved_waiting_meta_delivery_country',
            'meta_delivery_country_ambiguous',
        }:
            enriched.append(row)
            continue
        if not _ad_country_is_unknown(row.get('country')):
            enriched.append(row)
            continue
        inferred = known_by_exact.get(_ad_country_enrichment_key(row, include_ad=True)) or known_by_group.get(
            _ad_country_enrichment_key(row, include_ad=False)
        )
        if inferred:
            updated = dict(row)
            updated['country'] = inferred
            enriched.append(updated)
        else:
            enriched.append(row)
    return enriched


def _ad_materialize_fact_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    prepared_rows = [_ad_prepare_country_dimension(row) for row in (rows or []) if isinstance(row, dict)]
    for row in _ad_enrich_unknown_countries(prepared_rows):
        qualified_metadata = _qualified_join_row_metadata(row)
        key = _ad_fact_grain_key(row)
        bucket = buckets.setdefault(key, {
            'date': str(row.get('date') or '').strip(),
            'data_source': str(row.get('data_source') or '').strip() or 'Unknown',
            'platform': str(row.get('platform') or '').strip() or 'Unknown',
            'app_id': _normalize_ad_fact_account_value(row),
            'appsflyer_app_id': str(row.get('appsflyer_app_id') or '').strip(),
            'target_app': _ad_dashboard_row_target_app(row),
            'account_id': row.get('account_id') or row.get('ad_account_id') or '',
            'account_name': row.get('account_name') or row.get('app_id') or '',
            'ad_account_id': row.get('ad_account_id') or row.get('account_id') or '',
            'external_app': row.get('external_app') or '',
            'country': str(row.get('country') or '').strip() or 'Unknown',
            'media_source': str(row.get('media_source') or '').strip(),
            'campaign': str(row.get('campaign') or '').strip() or '未命名',
            'ad_group': str(row.get('ad_group') or '').strip(),
            'ad': str(row.get('ad') or '').strip(),
            'source_type': str(row.get('source_type') or '').strip(),
            **_empty_ad_metrics(),
            'row_count': 0,
            '_qualified_join_identities': set(),
            '_qualified_join_input_rows': 0,
        })
        row_target_app = _ad_dashboard_row_target_app(row)
        if bucket.get('target_app') not in {'linky', 'timo'} and row_target_app in {'linky', 'timo'}:
            bucket['target_app'] = row_target_app
        for source_key, target_key in (
            ('account_id', 'account_id'),
            ('account_name', 'account_name'),
            ('ad_account_id', 'ad_account_id'),
            ('external_app', 'external_app'),
        ):
            value = row.get(source_key)
            if not value and source_key == 'account_id':
                value = row.get('ad_account_id')
            if not value and source_key == 'ad_account_id':
                value = row.get('account_id')
            if value and not bucket.get(target_key):
                bucket[target_key] = value
        for metadata_key in (
            'historical_recovery',
            'historical_source_created_at',
            'country_attribution_status',
            'country_attribution_source',
            'country_attribution_grain',
            'campaign_id',
            'adset_id',
            'ad_id',
            'ad_name',
            'qualified_join_source_field',
            'source_metric_contract',
        ):
            value = row.get(metadata_key)
            if value in (None, ''):
                continue
            if metadata_key not in bucket:
                bucket[metadata_key] = value
            elif metadata_key in {'campaign_id', 'adset_id', 'ad_id'} and str(bucket[metadata_key]) != str(value):
                bucket[metadata_key] = ''
        if qualified_metadata is not None:
            bucket['_qualified_join_input_rows'] += 1
            bucket['_qualified_join_identities'].add(qualified_metadata['identity'])
        bucket['row_count'] = int(bucket.get('row_count') or 0) + 1
        _add_ad_metrics(bucket, row)
    materialized = list(buckets.values())
    for bucket in materialized:
        identities = set(bucket.pop('_qualified_join_identities', set()))
        qualified_rows = int(bucket.pop('_qualified_join_input_rows', 0) or 0)
        if qualified_rows:
            all_rows_observed = qualified_rows == int(bucket.get('row_count') or 0)
            exact_ready = (
                all_rows_observed
                and qualified_rows == 1
                and len(identities) == 1
                and all(next(iter(identities)))
            )
            bucket['qualified_join_metric_observed'] = all_rows_observed
            bucket['qualified_join_exact_attribution'] = exact_ready
            if exact_ready:
                status = 'exact'
            elif not all_rows_observed:
                status = 'mixed_observation'
            elif qualified_rows > 1 and len(identities) == 1:
                status = 'duplicate_exact_tuple'
            else:
                status = 'identity_conflict_or_missing'
            bucket['qualified_join_attribution_status'] = status
            if not exact_ready:
                bucket['campaign_id'] = ''
                bucket['adset_id'] = ''
                bucket['ad_id'] = ''
    materialized.sort(key=lambda item: (
        str(item.get('date') or ''),
        str(item.get('data_source') or ''),
        str(item.get('platform') or ''),
        str(item.get('app_id') or ''),
        str(item.get('country') or ''),
        str(item.get('campaign') or ''),
        str(item.get('ad_group') or ''),
        str(item.get('ad') or ''),
    ))
    return materialized


def ensure_ad_dashboard_fact_tables(conn: sqlite3.Connection) -> None:
    metric_columns = ',\n                    '.join(f'{key} REAL NOT NULL DEFAULT 0' for key in AD_DASHBOARD_FACT_COLUMNS)
    conn.execute(f"""
                CREATE TABLE IF NOT EXISTS ad_dashboard_fact_rows (
                    row_id TEXT PRIMARY KEY,
                    date TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    app_id TEXT NOT NULL DEFAULT '',
                    appsflyer_app_id TEXT NOT NULL DEFAULT '',
                    target_app TEXT NOT NULL DEFAULT 'inactive',
                    account_id TEXT NOT NULL DEFAULT '',
                    account_name TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL DEFAULT '',
                    media_source TEXT NOT NULL DEFAULT '',
                    campaign TEXT NOT NULL DEFAULT '',
                    campaign_id TEXT NOT NULL DEFAULT '',
                    adset_id TEXT NOT NULL DEFAULT '',
                    ad_id TEXT NOT NULL DEFAULT '',
                    ad_group TEXT NOT NULL DEFAULT '',
                    ad TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL DEFAULT '',
                    row_count INTEGER NOT NULL DEFAULT 0,
                    {metric_columns},
                    payload_json TEXT NOT NULL DEFAULT '{{}}',
                    updated_at TEXT NOT NULL
                )
                """)
    conn.execute("""
                CREATE TABLE IF NOT EXISTS ad_dashboard_sync_state (
                    source TEXT NOT NULL,
                    date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source, date)
                )
                """)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ad_dashboard_fact_date_platform ON ad_dashboard_fact_rows(date, platform)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ad_dashboard_fact_dims ON ad_dashboard_fact_rows(platform, country, app_id, campaign, ad_group, ad)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ad_dashboard_sync_date ON ad_dashboard_sync_state(date, status)')
    try:
        existing_columns = {str(row[1]) for row in conn.execute('PRAGMA table_info(ad_dashboard_fact_rows)').fetchall()}
        if 'target_app' not in existing_columns:
            conn.execute("ALTER TABLE ad_dashboard_fact_rows ADD COLUMN target_app TEXT NOT NULL DEFAULT 'inactive'")
        lineage_columns_added = False
        for key in ('account_id', 'account_name', 'campaign_id', 'adset_id', 'ad_id'):
            if key not in existing_columns:
                conn.execute(f"ALTER TABLE ad_dashboard_fact_rows ADD COLUMN {key} TEXT NOT NULL DEFAULT ''")
                lineage_columns_added = True
        for key in AD_DASHBOARD_FACT_COLUMNS:
            if key not in existing_columns:
                conn.execute(f'ALTER TABLE ad_dashboard_fact_rows ADD COLUMN {key} REAL NOT NULL DEFAULT 0')

        if lineage_columns_added:
            conn.execute("""
            UPDATE ad_dashboard_fact_rows
            SET account_id = COALESCE(NULLIF(account_id, ''),
                    CASE WHEN json_valid(payload_json) THEN NULLIF(json_extract(payload_json, '$.account_id'), '') END,
                    CASE WHEN json_valid(payload_json) THEN NULLIF(json_extract(payload_json, '$.ad_account_id'), '') END, ''),
                account_name = COALESCE(NULLIF(account_name, ''),
                    CASE WHEN json_valid(payload_json) THEN NULLIF(json_extract(payload_json, '$.account_name'), '') END,
                    NULLIF(app_id, ''), ''),
                campaign_id = COALESCE(NULLIF(campaign_id, ''),
                    CASE WHEN json_valid(payload_json) THEN NULLIF(json_extract(payload_json, '$.campaign_id'), '') END, ''),
                adset_id = COALESCE(NULLIF(adset_id, ''),
                    CASE WHEN json_valid(payload_json) THEN NULLIF(json_extract(payload_json, '$.adset_id'), '') END, ''),
                ad_id = COALESCE(NULLIF(ad_id, ''),
                    CASE WHEN json_valid(payload_json) THEN NULLIF(json_extract(payload_json, '$.ad_id'), '') END, '')
            """)

        def _backfill_target_app(target_app: str, account_ids: set[str], aliases: set[str]) -> None:
            variants = sorted(
                {str(value).strip() for value in account_ids if str(value).strip()}
                | {f'act_{value}' for value in account_ids if str(value).strip()}
                | {str(value).strip() for value in aliases if str(value).strip()}
            )
            if not variants:
                return
            placeholders = ','.join('?' for _ in variants)
            conn.execute(
                f"""
                UPDATE ad_dashboard_fact_rows
                SET target_app = ?
                WHERE app_id IN ({placeholders})
                   OR appsflyer_app_id IN ({placeholders})
                """,
                [target_app, *variants, *variants],
            )

        _backfill_target_app('linky', AD_DASHBOARD_TARGET_APP_LINKY_META_ACCOUNTS, AD_DASHBOARD_TARGET_APP_LINKY_ALIASES)
        _backfill_target_app('timo', AD_DASHBOARD_TARGET_APP_TIMO_META_ACCOUNTS, AD_DASHBOARD_TARGET_APP_TIMO_ALIASES)
        conn.execute("""
            UPDATE ad_dashboard_fact_rows
            SET target_app = 'inactive'
            WHERE target_app IS NULL
               OR target_app = ''
               OR target_app NOT IN ('linky', 'timo', 'inactive')
        """)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ad_dashboard_fact_target_app ON ad_dashboard_fact_rows(target_app, platform, date)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ad_dashboard_fact_lineage ON ad_dashboard_fact_rows(date, platform, account_id, campaign_id, campaign)')
    except Exception:
        # Older databases may be opened read-only during diagnostics. Schema
        # compatibility backfills are best effort, matching the legacy path.
        pass


def _ad_dashboard_fact_insert_sql() -> str:
    columns = [
        'row_id', 'date', 'data_source', 'platform', 'app_id', 'appsflyer_app_id',
        'target_app', 'account_id', 'account_name', 'country', 'media_source',
        'campaign', 'campaign_id', 'adset_id', 'ad_id', 'ad_group', 'ad', 'source_type',
        'row_count', *AD_DASHBOARD_FACT_COLUMNS, 'payload_json', 'updated_at',
    ]
    placeholders = ','.join('?' for _ in columns)
    update_columns = [column for column in columns if column != 'row_id']
    update_clause = ','.join(f'{column}=excluded.{column}' for column in update_columns)
    return f"INSERT INTO ad_dashboard_fact_rows ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT(row_id) DO UPDATE SET {update_clause}"


def _remove_reclassified_unknown_fact(conn: sqlite3.Connection, row: Dict[str, Any]) -> int:
    attribution_status = str(row.get('country_attribution_status') or '').strip()
    data_source = str(row.get('data_source') or '').strip().lower()
    replaces_unknown = (
        attribution_status in {'meta_delivery_country_peer', 'appsflyer_report_country'}
        or data_source == 'appsflyer'
    )
    if not replaces_unknown:
        return 0
    if _ad_country_is_unknown(row.get('country')):
        return 0
    cursor = conn.execute(
        """
        DELETE FROM ad_dashboard_fact_rows
        WHERE date = ?
          AND data_source = ?
          AND platform = ?
          AND app_id = ?
          AND appsflyer_app_id = ?
          AND country IN ('', 'Unknown', '未知', '未命名')
          AND media_source = ?
          AND campaign = ?
          AND ad_group = ?
          AND ad = ?
          AND source_type = ?
        """,
        (
            str(row.get('date') or '').strip(),
            str(row.get('data_source') or '').strip() or 'Unknown',
            str(row.get('platform') or '').strip() or 'Unknown',
            _normalize_ad_fact_account_value(row),
            str(row.get('appsflyer_app_id') or '').strip(),
            str(row.get('media_source') or '').strip(),
            str(row.get('campaign') or '').strip() or '未命名',
            str(row.get('ad_group') or '').strip(),
            str(row.get('ad') or '').strip(),
            str(row.get('source_type') or '').strip(),
        ),
    )
    return max(int(cursor.rowcount or 0), 0)


def upsert_ad_dashboard_fact_rows(
    conn: sqlite3.Connection,
    rows: List[Dict[str, Any]],
    *,
    synced_at: Optional[str] = None,
) -> int:
    ensure_ad_dashboard_fact_tables(conn)
    now = synced_at or datetime.now(timezone.utc).isoformat()
    materialized = _ad_materialize_fact_rows(rows)
    sql = _ad_dashboard_fact_insert_sql()
    count = 0
    for row in materialized:
        stored = {key: row.get(key) for key in (
            'date', 'data_source', 'platform', 'app_id', 'appsflyer_app_id',
            'target_app', 'account_id', 'account_name', 'country', 'media_source',
            'campaign', 'campaign_id', 'adset_id', 'ad_id', 'ad_group', 'ad', 'source_type',
        )}
        stored['account_id'] = row.get('account_id') or row.get('ad_account_id') or ''
        stored['account_name'] = row.get('account_name') or row.get('app_id') or ''
        stored['campaign_id'] = row.get('campaign_id') or ''
        stored['adset_id'] = row.get('adset_id') or ''
        stored['ad_id'] = row.get('ad_id') or ''
        stored['external_app'] = row.get('external_app')
        stored['target_app'] = _ad_dashboard_row_target_app(stored)
        stored['row_count'] = int(row.get('row_count') or 0)
        for key in AD_DASHBOARD_FACT_COLUMNS:
            stored[key] = float(row.get(key) or 0.0)
        payload = {key: value for key, value in row.items() if key != 'payload_json'}
        values = [
            _ad_fact_row_id(row),
            stored['date'],
            stored['data_source'],
            stored['platform'],
            stored['app_id'],
            stored['appsflyer_app_id'],
            stored['target_app'],
            stored['account_id'],
            stored['account_name'],
            stored['country'],
            stored['media_source'],
            stored['campaign'],
            stored['campaign_id'],
            stored['adset_id'],
            stored['ad_id'],
            stored['ad_group'],
            stored['ad'],
            stored['source_type'],
            stored['row_count'],
            *[stored[key] for key in AD_DASHBOARD_FACT_COLUMNS],
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            now,
        ]
        _remove_reclassified_unknown_fact(conn, row)
        conn.execute(sql, values)
        count += 1
    return count


def replace_ad_dashboard_fact_rows_for_dates(
    conn: sqlite3.Connection,
    rows: List[Dict[str, Any]],
    *,
    start_date: date,
    end_date: date,
    synced_at: Optional[str] = None,
    tugao_funnel_required: bool = False,
) -> int:
    """Merge refreshed facts without erasing accounts absent from this fetch.

    Account access is a fetch concern, never a historical-retention rule. A
    disabled, banned, or permission-lost account must remain in every settled
    day/week/month aggregation after its rows have been stored.
    """
    ensure_ad_dashboard_fact_tables(conn)
    authoritative_rows = _ad_materialize_fact_rows(rows) if tugao_funnel_required else rows
    if tugao_funnel_required:
        completeness = ad_dashboard_fact_rows_completeness(
            authoritative_rows,
            start_date=start_date,
            end_date=end_date,
            appsflyer_required=False,
            tugao_funnel_required=True,
        )
        if not completeness.get('complete'):
            raise ValueError(
                'tugao_qualified_join_replace_window_incomplete:'
                + str(completeness.get('error_message') or 'unknown')
            )
        conn.execute(
            "DELETE FROM ad_dashboard_fact_rows "
            "WHERE lower(data_source)='tugaofunnel' AND date BETWEEN ? AND ?",
            (start_date.isoformat(), end_date.isoformat()),
        )
    return upsert_ad_dashboard_fact_rows(conn, authoritative_rows, synced_at=synced_at)


def mark_ad_dashboard_sync_state(
    conn: sqlite3.Connection,
    *,
    source: str,
    start_date: date,
    end_date: date,
    status: str,
    row_count: int = 0,
    error_message: str = '',
    synced_at: Optional[str] = None,
) -> None:
    ensure_ad_dashboard_fact_tables(conn)
    now = synced_at or datetime.now(timezone.utc).isoformat()
    cursor = start_date
    while cursor <= end_date:
        conn.execute(
            """
            INSERT INTO ad_dashboard_sync_state(source, date, status, row_count, error_message, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, date) DO UPDATE SET
                status=excluded.status,
                row_count=excluded.row_count,
                error_message=excluded.error_message,
                updated_at=excluded.updated_at
            """,
            (source, cursor.isoformat(), status, int(row_count or 0), str(error_message or ''), now),
        )
        cursor += timedelta(days=1)


def ad_dashboard_fact_rows_completeness(
    rows: List[Dict[str, Any]],
    *,
    start_date: date,
    end_date: date,
    appsflyer_required: bool = True,
    tugao_funnel_required: bool = False,
) -> Dict[str, Any]:
    if tugao_funnel_required:
        tugao_rows = [
            row for row in (rows or [])
            if str((row or {}).get('data_source') or '').strip().lower() == 'tugaofunnel'
        ]
        if (
            tugao_rows
            and all((row or {}).get('qualified_join_metric_observed') is True for row in tugao_rows)
            and not any('qualified_join_attribution_status' in (row or {}) for row in tugao_rows)
        ):
            rows = _ad_materialize_fact_rows(rows)
    expected_dates: List[str] = []
    cursor = start_date
    while cursor <= end_date:
        expected_dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    rows_by_date: Dict[str, List[Dict[str, Any]]] = {day: [] for day in expected_dates}
    for row in rows or []:
        row_date = _parse_dashboard_date((row or {}).get('date'))
        if row_date and start_date <= row_date <= end_date:
            rows_by_date.setdefault(row_date.isoformat(), []).append(row)
    missing_dates = [day for day in expected_dates if not rows_by_date.get(day)]
    missing_appsflyer: List[Dict[str, str]] = []
    unresolved_meta_country: List[Dict[str, str]] = []
    missing_meta_lineage: List[Dict[str, str]] = []
    missing_tugao_funnel: List[str] = []
    invalid_qualified_join: List[Dict[str, str]] = []
    for day in expected_dates:
        for row in rows_by_date.get(day) or []:
            if str((row or {}).get('data_source') or '').strip().lower() != 'meta':
                continue
            has_delivery = any(
                float((row or {}).get(key) or 0.0)
                for key in ('cost', 'impressions', 'clicks', 'link_clicks', 'meta_installs', 'installs')
            )
            account_id = str((row or {}).get('account_id') or (row or {}).get('ad_account_id') or '').strip()
            campaign = str((row or {}).get('campaign') or '').strip()
            campaign_id = str((row or {}).get('campaign_id') or '').strip()
            if has_delivery and (not account_id or not campaign or not campaign_id):
                missing_meta_lineage.append({
                    'date': day,
                    'account_id': account_id,
                    'campaign': campaign,
                    'campaign_id': campaign_id,
                })
            status = str((row or {}).get('country_attribution_status') or '').strip()
            if status not in {'unresolved_waiting_meta_delivery_country', 'meta_delivery_country_ambiguous'}:
                continue
            if not _ad_country_is_unknown((row or {}).get('country')):
                continue
            if not any(float((row or {}).get(key) or 0.0) for key in ('cost', 'impressions', 'clicks', 'link_clicks')):
                continue
            unresolved_meta_country.append({'date': day, 'account_id': str((row or {}).get('account_id') or (row or {}).get('ad_account_id') or ''), 'campaign': str((row or {}).get('campaign') or '')})
    if tugao_funnel_required:
        seen_qualified_identities: set[Tuple[str, ...]] = set()
        for day in expected_dates:
            tugao_rows = [
                row for row in rows_by_date.get(day) or []
                if str((row or {}).get('data_source') or '').strip().lower() == 'tugaofunnel'
            ]
            if not tugao_rows:
                missing_tugao_funnel.append(day)
                continue
            for row in tugao_rows:
                marker = (row or {}).get('qualified_join_metric_observed')
                if marker is not True:
                    reason = (
                        'qualified_join_observation_marker_invalid'
                        if 'qualified_join_metric_observed' in (row or {})
                        else 'qualified_join_not_observed'
                    )
                    invalid_qualified_join.append({'date': day, 'reason': reason})
                    continue
                try:
                    metadata = _qualified_join_row_metadata(row)
                except ValueError as exc:
                    invalid_qualified_join.append({'date': day, 'reason': str(exc)})
                    continue
                if metadata is None:
                    invalid_qualified_join.append({'date': day, 'reason': 'qualified_join_not_observed'})
                    continue
                platform = str((row or {}).get('platform') or '').strip().lower()
                if platform != 'internal' and not metadata['exact_ready']:
                    status = str((row or {}).get('qualified_join_attribution_status') or '').strip()
                    reason = (
                        'qualified_join_exact_identity_missing'
                        if status == 'identity_conflict_or_missing'
                        else 'qualified_join_materialized_exact_state_invalid'
                    )
                    invalid_qualified_join.append({'date': day, 'reason': reason})
                    continue
                if platform != 'internal' and (
                    (row or {}).get('qualified_join_exact_attribution') is not True
                    or str((row or {}).get('qualified_join_attribution_status') or '').strip() != 'exact'
                ):
                    invalid_qualified_join.append({'date': day, 'reason': 'qualified_join_materialized_exact_state_invalid'})
                    continue
                identity = (
                    day,
                    str((row or {}).get('country') or '').strip(),
                    str((row or {}).get('media_source') or '').strip(),
                    *metadata['identity'],
                )
                if identity in seen_qualified_identities:
                    invalid_qualified_join.append({'date': day, 'reason': 'qualified_join_exact_identity_duplicate'})
                    continue
                seen_qualified_identities.add(identity)
    if appsflyer_required:
        media_sources = {'meta', 'google', 'tiktok'}
        for day in expected_dates:
            day_rows = rows_by_date.get(day) or []
            platforms_with_media = {
                str((row or {}).get('platform') or '').strip()
                for row in day_rows
                if str((row or {}).get('platform') or '').strip().lower() != 'internal'
                and str((row or {}).get('data_source') or '').strip().lower() in media_sources
                and (
                    float((row or {}).get('cost') or 0.0)
                    or float((row or {}).get('meta_installs') or 0.0)
                    or float((row or {}).get('installs') or 0.0)
                )
            }
            platforms_with_appsflyer = {
                str((row or {}).get('platform') or '').strip()
                for row in day_rows
                if str((row or {}).get('data_source') or '').strip().lower() == 'appsflyer'
            }
            for platform in sorted(platforms_with_media - platforms_with_appsflyer):
                missing_appsflyer.append({'date': day, 'platform': platform})
    # Preserve the current production fail-closed contract: a missing date is
    # incomplete even when AppsFlyer coverage for the remaining dates is fine.
    complete = (
        not missing_dates
        and not missing_appsflyer
        and not unresolved_meta_country
        and not missing_meta_lineage
        and not missing_tugao_funnel
        and not invalid_qualified_join
    )
    reason_parts: List[str] = []
    if missing_dates:
        reason_parts.append('missing_dates=' + ','.join(missing_dates[:5]))
    if missing_appsflyer:
        sample = ','.join(f"{item['date']}:{item['platform']}" for item in missing_appsflyer[:5])
        reason_parts.append('missing_appsflyer=' + sample)
    if unresolved_meta_country:
        sample = ','.join(f"{item['date']}:{item['account_id']}:{item['campaign']}" for item in unresolved_meta_country[:5])
        reason_parts.append('unresolved_meta_country=' + sample)
    if missing_meta_lineage:
        sample = ','.join(
            f"{item['date']}:{item['account_id'] or '-'}:{item['campaign_id'] or item['campaign'] or '-'}"
            for item in missing_meta_lineage[:5]
        )
        reason_parts.append('missing_meta_lineage=' + sample)
    if missing_tugao_funnel:
        reason_parts.append('missing_tugao_funnel=' + ','.join(missing_tugao_funnel[:5]))
    if invalid_qualified_join:
        sample = ','.join(f"{item['date']}:{item['reason']}" for item in invalid_qualified_join[:5])
        reason_parts.append('invalid_qualified_join=' + sample)
    return {
        'complete': complete,
        'missing_dates': missing_dates,
        'missing_appsflyer': missing_appsflyer,
        'unresolved_meta_country': unresolved_meta_country,
        'missing_meta_lineage': missing_meta_lineage,
        'missing_tugao_funnel': missing_tugao_funnel,
        'invalid_qualified_join': invalid_qualified_join,
        'status': 'ok' if complete else 'partial',
        'error_message': '; '.join(reason_parts),
    }
