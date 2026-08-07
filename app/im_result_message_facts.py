from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Sequence, Tuple

from app.im_result_message_api import RESULT_MESSAGE_STEPS


FACT_TABLES = {
    'deliveries': ('im_result_message_deliveries', 'delivery_event_id'),
    'interactions': ('im_result_message_interactions', 'interaction_event_id'),
}


def _text(value: Any) -> str:
    return str(value or '').strip()


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(*parts: Any, prefix: str = '') -> str:
    digest = hashlib.sha256('|'.join(_text(part) for part in parts).encode('utf-8')).hexdigest()[:32]
    return f'{prefix}{digest}' if prefix else digest


def ensure_im_result_message_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS im_result_message_deliveries (
            delivery_event_id TEXT PRIMARY KEY,
            business_date_utc TEXT NOT NULL,
            step_code TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            external_app TEXT NOT NULL DEFAULT '',
            guild_name TEXT NOT NULL DEFAULT '',
            reception_mode TEXT NOT NULL DEFAULT '',
            conversation_id TEXT NOT NULL DEFAULT '',
            association_confidence TEXT NOT NULL DEFAULT '',
            is_countable INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL,
            source_updated_at TEXT NOT NULL DEFAULT '',
            synced_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS im_result_message_interactions (
            interaction_event_id TEXT PRIMARY KEY,
            business_date_utc TEXT NOT NULL,
            step_code TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            external_app TEXT NOT NULL DEFAULT '',
            guild_name TEXT NOT NULL DEFAULT '',
            reception_mode TEXT NOT NULL DEFAULT '',
            conversation_id TEXT NOT NULL DEFAULT '',
            association_confidence TEXT NOT NULL DEFAULT '',
            interaction_type TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            source_updated_at TEXT NOT NULL DEFAULT '',
            synced_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS im_result_message_daily (
            daily_fact_id TEXT PRIMARY KEY,
            business_date_utc TEXT NOT NULL,
            step_code TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            external_app TEXT NOT NULL DEFAULT '',
            guild_name TEXT NOT NULL DEFAULT '',
            data_maturity_status TEXT NOT NULL DEFAULT '',
            success_episode_count INTEGER NOT NULL DEFAULT 0,
            success_user_uv INTEGER NOT NULL DEFAULT 0,
            result_message_delivered_uv INTEGER NOT NULL DEFAULT 0,
            ops_group_link_delivered_uv INTEGER NOT NULL DEFAULT 0,
            result_message_interacted_uv INTEGER NOT NULL DEFAULT 0,
            link_clicked_uv INTEGER NOT NULL DEFAULT 0,
            invite_card_clicked_uv INTEGER NOT NULL DEFAULT 0,
            unmatched_success_episode_count INTEGER NOT NULL DEFAULT 0,
            unmatched_interaction_count INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL,
            source_updated_at TEXT NOT NULL DEFAULT '',
            synced_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_im_result_delivery_window
            ON im_result_message_deliveries(business_date_utc, step_code, country, external_app, guild_name);
        CREATE INDEX IF NOT EXISTS idx_im_result_interaction_window
            ON im_result_message_interactions(business_date_utc, step_code, country, external_app, guild_name);
        CREATE INDEX IF NOT EXISTS idx_im_result_daily_window
            ON im_result_message_daily(business_date_utc, step_code, country, external_app, guild_name);
        """
    )


def persist_im_result_message_bundle(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    deliveries: Sequence[Dict[str, Any]],
    interactions: Sequence[Dict[str, Any]],
    daily: Sequence[Dict[str, Any]],
    replace_window: bool = True,
) -> Dict[str, Any]:
    ensure_im_result_message_tables(conn)
    start = date.fromisoformat(_text(start_date)[:10]).isoformat()
    end = date.fromisoformat(_text(end_date)[:10]).isoformat()
    if start > end:
        raise ValueError('start_date_after_end_date')
    synced_at = _utc_now()
    counts: Dict[str, int] = {}
    with conn:
        if replace_window:
            for table, _ in FACT_TABLES.values():
                conn.execute(
                    f'DELETE FROM {table} WHERE business_date_utc BETWEEN ? AND ?',
                    (start, end),
                )
            conn.execute(
                'DELETE FROM im_result_message_daily WHERE business_date_utc BETWEEN ? AND ?',
                (start, end),
            )
        for kind, rows in (('deliveries', deliveries), ('interactions', interactions)):
            table, id_field = FACT_TABLES[kind]
            inserted = 0
            for raw in rows:
                row = dict(raw or {})
                event_id = _text(row.get(id_field))
                business_date = _text(row.get('business_date_utc'))[:10]
                if not event_id or not business_date or not (start <= business_date <= end):
                    continue
                columns = (
                    event_id,
                    business_date,
                    _text(row.get('step_code')).upper(),
                    _text(row.get('country')),
                    _text(row.get('external_app')),
                    _text(row.get('guild_name')),
                    _text(row.get('reception_mode')),
                    _text(row.get('conversation_id')),
                    _text(row.get('association_confidence')),
                )
                if kind == 'deliveries':
                    conn.execute(
                        f"""INSERT OR REPLACE INTO {table} (
                            delivery_event_id, business_date_utc, step_code, country, external_app,
                            guild_name, reception_mode, conversation_id, association_confidence,
                            is_countable, payload_json, source_updated_at, synced_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (*columns, 1 if row.get('is_countable') else 0, json.dumps(row, ensure_ascii=False, separators=(',', ':')), _text(row.get('updated_at')), synced_at),
                    )
                else:
                    conn.execute(
                        f"""INSERT OR REPLACE INTO {table} (
                            interaction_event_id, business_date_utc, step_code, country, external_app,
                            guild_name, reception_mode, conversation_id, association_confidence,
                            interaction_type, payload_json, source_updated_at, synced_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (*columns, _text(row.get('interaction_type')), json.dumps(row, ensure_ascii=False, separators=(',', ':')), _text(row.get('updated_at')), synced_at),
                    )
                inserted += 1
            counts[kind] = inserted
        daily_count = 0
        for raw in daily:
            row = dict(raw or {})
            business_date = _text(row.get('business_date_utc'))[:10]
            if not business_date or not (start <= business_date <= end):
                continue
            step_code = _text(row.get('step_code')).upper()
            country = _text(row.get('country'))
            external_app = _text(row.get('external_app'))
            guild_name = _text(row.get('guild_name'))
            fact_id = _stable_id(
                business_date,
                step_code,
                country,
                external_app,
                guild_name,
                row.get('reception_mode'),
                row.get('ab_group_at_entry'),
                row.get('success_type'),
                prefix='im_result_daily_',
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO im_result_message_daily (
                    daily_fact_id, business_date_utc, step_code, country, external_app, guild_name,
                    data_maturity_status, success_episode_count, success_user_uv,
                    result_message_delivered_uv, ops_group_link_delivered_uv,
                    result_message_interacted_uv, link_clicked_uv, invite_card_clicked_uv,
                    unmatched_success_episode_count, unmatched_interaction_count,
                    payload_json, source_updated_at, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact_id, business_date, step_code, country, external_app, guild_name,
                    _text(row.get('data_maturity_status')), _int(row.get('success_episode_count')),
                    _int(row.get('success_user_uv')), _int(row.get('result_message_delivered_uv')),
                    _int(row.get('ops_group_link_delivered_uv')), _int(row.get('result_message_interacted_uv')),
                    _int(row.get('link_clicked_uv')), _int(row.get('invite_card_clicked_uv')),
                    _int(row.get('unmatched_success_episode_count')), _int(row.get('unmatched_interaction_count')),
                    json.dumps(row, ensure_ascii=False, separators=(',', ':')), _text(row.get('updated_at')), synced_at,
                ),
            )
            daily_count += 1
        counts['daily'] = daily_count
    return {'ok': True, 'start_date_utc': start, 'end_date_utc': end, 'persisted': counts, 'synced_at': synced_at}


def _region_clause(region: str) -> Tuple[str, Tuple[Any, ...]]:
    normalized = _text(region).lower()
    if normalized in {'br', 'bra', 'brazil'}:
        return "AND LOWER(country) IN ('brazil', 'br')", ()
    if normalized in {'id', 'idn', 'indonesia'}:
        return "AND LOWER(country) IN ('indonesia', 'id')", ()
    if normalized in {'es', 'spanish', 'latam', 'latam_es', 'hispanic'}:
        return "AND LOWER(country) IN ('mexico','venezuela','colombia','chile','peru','ecuador','argentina','bolivia','paraguay','uruguay','mex','ve','co','cl','pe','ec','ar','bo','py','uy')", ()
    return '', ()


def im_result_message_summary(
    conn: sqlite3.Connection,
    *,
    start_date_utc: str,
    end_date_utc: str,
    region: str = '',
    external_app: str = '',
) -> Dict[str, Any]:
    ensure_im_result_message_tables(conn)
    start = date.fromisoformat(_text(start_date_utc)[:10]).isoformat()
    end = date.fromisoformat(_text(end_date_utc)[:10]).isoformat()
    region_sql, region_params = _region_clause(region)
    app_sql = 'AND LOWER(external_app) = LOWER(?)' if _text(external_app) else ''
    params: List[Any] = [start, end, *region_params]
    if app_sql:
        params.append(_text(external_app))
    where_sql = f"business_date_utc BETWEEN ? AND ? {region_sql} {app_sql}"
    rows = conn.execute(
        f"SELECT * FROM im_result_message_daily WHERE {where_sql} AND step_code = 'ANY'",
        tuple(params),
    ).fetchall()
    coverage_rows = conn.execute(
        f"SELECT step_code, COUNT(*) AS n FROM im_result_message_daily WHERE {where_sql} AND step_code IN ('R101','R104','R105') GROUP BY step_code",
        tuple(params),
    ).fetchall()
    available_steps = {str(row['step_code']): int(row['n'] or 0) for row in coverage_rows}
    coverage = [
        {'step_code': step, 'coverage_status': 'available' if available_steps.get(step) else 'missing'}
        for step in RESULT_MESSAGE_STEPS
    ]
    if not rows:
        return {
            'coverage_status': 'missing',
            'data_quality_note': f'UTC {start} 至 {end} 未同步结果话术事实。',
            'timezone': 'UTC+0',
            'start_date_utc': start,
            'end_date_utc': end,
            'step_coverage': coverage,
            'data_maturity_status': 'unknown',
            'metrics': {},
        }
    metric_keys = (
        'success_episode_count', 'success_user_uv', 'result_message_delivered_uv',
        'ops_group_link_delivered_uv', 'result_message_interacted_uv', 'link_clicked_uv',
        'invite_card_clicked_uv', 'unmatched_success_episode_count', 'unmatched_interaction_count',
    )
    metrics = {key: sum(_int(row[key]) for row in rows) for key in metric_keys}
    maturity = sorted({_text(row['data_maturity_status']) or 'unknown' for row in rows})
    missing_steps = [item['step_code'] for item in coverage if item['coverage_status'] == 'missing']
    coverage_status = 'partial' if missing_steps else 'available'
    note_parts = [f'UTC {start} 至 {end}；结果话术事实按官方用户 UV 汇总。']
    if missing_steps:
        note_parts.append(f"{','.join(missing_steps)} 未同步/无覆盖。")
    if any(value != 'final' for value in maturity):
        note_parts.append('当前数据尚未最终定稿。')
    return {
        'coverage_status': coverage_status,
        'data_quality_note': ''.join(note_parts),
        'timezone': 'UTC+0',
        'start_date_utc': start,
        'end_date_utc': end,
        'step_coverage': coverage,
        'data_maturity_status': 'provisional' if any(value != 'final' for value in maturity) else 'final',
        'metrics': metrics,
        'source': 'im_result_message_facts_v1',
        'synced_at': max((_text(row['synced_at']) for row in rows), default=''),
    }


def im_result_message_detail_rows(
    conn: sqlite3.Connection,
    *,
    kind: str,
    start_date_utc: str,
    end_date_utc: str,
    step_code: str = '',
    country: str = '',
    external_app: str = '',
    guild_name: str = '',
    limit: int = 100,
) -> List[Dict[str, Any]]:
    ensure_im_result_message_tables(conn)
    if kind == 'daily':
        table = 'im_result_message_daily'
        order_column = 'business_date_utc'
    elif kind in FACT_TABLES:
        table = FACT_TABLES[kind][0]
        order_column = 'source_updated_at'
    else:
        raise ValueError('invalid_result_message_kind')
    where = ['business_date_utc BETWEEN ? AND ?']
    params: List[Any] = [date.fromisoformat(_text(start_date_utc)[:10]).isoformat(), date.fromisoformat(_text(end_date_utc)[:10]).isoformat()]
    for column, value in (('step_code', step_code), ('country', country), ('external_app', external_app), ('guild_name', guild_name)):
        if _text(value):
            where.append(f'LOWER({column}) = LOWER(?)')
            params.append(_text(value))
    rows = conn.execute(
        f"SELECT payload_json FROM {table} WHERE {' AND '.join(where)} ORDER BY {order_column} DESC LIMIT ?",
        (*params, max(1, min(int(limit or 100), 500))),
    ).fetchall()
    result = []
    for row in rows:
        try:
            payload = json.loads(str(row['payload_json'] or '{}'))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            result.append(payload)
    return result


def result_message_chain_steps(facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    metrics = dict(facts.get('metrics') or {})
    quality = str(facts.get('coverage_status') or 'missing')
    available = bool(metrics)
    note = str(facts.get('data_quality_note') or '')

    def step(key: str, label: str, count_key: str, denominator_key: str, metric_label: str, next_event: str) -> Dict[str, Any]:
        count = _int(metrics.get(count_key)) if available else 0
        denominator = _int(metrics.get(denominator_key)) if available else 0
        return {
            'step_key': key,
            'step_label': label,
            'event_name': count_key,
            'template_step': 'TUGAO R101 / R104 / R105 可信事实',
            'count': count,
            'raw_count': count,
            'denominator': denominator,
            'rate': round(count / denominator, 4) if denominator else 0.0,
            'actual_dropoff_count': max(denominator - count, 0) if available else 0,
            'actual_dropoff_rate': round(max(denominator - count, 0) / denominator, 4) if denominator else 0.0,
            'attributed_conversations': 0,
            'attribution_coverage': 0.0,
            'unattributed_dropoff_count': 0,
            'primary_diagnosis': '',
            'primary_diagnosis_zh': '官方结果话术事实',
            'top_countries': [],
            'lost_conversations': 0,
            'loss_share': 0.0,
            'metric_label': metric_label,
            'next_event': next_event,
            'recommended_action': '按官方送达与高置信互动明细下钻，不再使用历史消息推断。',
            'data_quality_status': quality,
            'data_quality_note': note,
            'metric_available': available,
            'independent_fact_window': True,
            'timezone': facts.get('timezone') or 'UTC+0',
            'window_start': facts.get('start_date_utc') or '',
            'window_end': facts.get('end_date_utc') or '',
            'step_coverage': facts.get('step_coverage') or [],
            'data_maturity_status': facts.get('data_maturity_status') or 'unknown',
        }

    return [
        step('result_message_delivered', '结果话术送达', 'result_message_delivered_uv', 'success_user_uv', '成功用户→结果话术送达率', '运营群链接送达'),
        step('ops_group_link_delivered', '运营群链接送达', 'ops_group_link_delivered_uv', 'success_user_uv', '成功用户→运营群链接送达率', '结果话术互动'),
        step('result_message_interacted', '结果话术互动', 'result_message_interacted_uv', 'result_message_delivered_uv', '送达→高置信互动率', '-'),
    ]
