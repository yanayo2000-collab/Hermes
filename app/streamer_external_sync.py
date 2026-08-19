from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from app.streamer_analytics import (
    LINKY_ANALYTICS_EXCLUDED_GUILDS,
    ensure_streamer_analytics_views,
    normalize_streamer_app,
)
from app.linky_source_readiness import (
    LINKY_MIN_PREVIOUS_SOURCE_ROW_RATIO,
    is_linky_source_row_count_ready,
)
from app.streamer_data_foundation import (
    archive_raw_json,
    record_ingestion_scope,
    record_streamer_daily_revision,
    upsert_streamer_profile,
)


BJ = ZoneInfo('Asia/Shanghai')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _bj_iso_from_epoch(value: object) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return ''
    if number <= 0:
        return ''
    if number > 10**12:
        number //= 1000
    return datetime.fromtimestamp(number, timezone.utc).astimezone(BJ).isoformat()


def _utc_iso_from_epoch(value: object) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return ''
    if number <= 0:
        return ''
    if number > 10**12:
        number //= 1000
    return datetime.fromtimestamp(number, timezone.utc).isoformat()


def _executor_key(executor: sqlite3.Row) -> str:
    app_name = str(executor['app_name'] or '').strip().lower()
    if app_name == 'linky':
        for key in ('cms_guild_sid', 'cms_guild_id'):
            value = str(executor[key] or '').strip()
            if value:
                return f'linky:{key}:{value}'
    return str(executor['guild_name'] or '').strip()


def _auth_header(value: object) -> str:
    token = str(value or '').strip()
    if not token:
        return ''
    return token if token.lower().startswith('bearer ') else f'Bearer {token}'


def _enabled_executors(
    conn: sqlite3.Connection,
    app_name: str,
    guild_name: str = '',
) -> List[sqlite3.Row]:
    aliases = ('sugo', 'sogo') if app_name == 'sugo' else (app_name,)
    placeholders = ','.join('?' for _ in aliases)
    guild_filter = ' AND guild_name = ?' if guild_name else ''
    params: List[Any] = list(aliases)
    if guild_name:
        params.append(guild_name)
    elif app_name == 'linky' and LINKY_ANALYTICS_EXCLUDED_GUILDS:
        excluded_placeholders = ','.join('?' for _ in LINKY_ANALYTICS_EXCLUDED_GUILDS)
        guild_filter = f' AND guild_name NOT IN ({excluded_placeholders})'
        params.extend(sorted(LINKY_ANALYTICS_EXCLUDED_GUILDS))
    return conn.execute(
        f"SELECT * FROM guild_executors WHERE enabled = 1 AND lower(app_name) IN ({placeholders}){guild_filter} ORDER BY guild_name",
        params,
    ).fetchall()


def _upsert_profile(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO streamer_external_profiles (
            app_name, guild_executor_key, guild_name, country, streamer_id,
            platform_user_id, platform_character_id, nickname, registered_at_bj,
            last_active_at_bj, is_real_person, source_name, source_payload,
            snapshot_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(app_name, guild_executor_key, streamer_id) DO UPDATE SET
            guild_name = excluded.guild_name,
            country = CASE WHEN excluded.country <> '' THEN excluded.country ELSE streamer_external_profiles.country END,
            platform_user_id = CASE WHEN excluded.platform_user_id <> '' THEN excluded.platform_user_id ELSE streamer_external_profiles.platform_user_id END,
            platform_character_id = CASE WHEN excluded.platform_character_id <> '' THEN excluded.platform_character_id ELSE streamer_external_profiles.platform_character_id END,
            nickname = CASE WHEN excluded.nickname <> '' THEN excluded.nickname ELSE streamer_external_profiles.nickname END,
            registered_at_bj = CASE
                WHEN excluded.registered_at_bj = '' THEN streamer_external_profiles.registered_at_bj
                WHEN streamer_external_profiles.registered_at_bj = '' THEN excluded.registered_at_bj
                WHEN excluded.registered_at_bj < streamer_external_profiles.registered_at_bj THEN excluded.registered_at_bj
                ELSE streamer_external_profiles.registered_at_bj
            END,
            last_active_at_bj = CASE
                WHEN excluded.last_active_at_bj > streamer_external_profiles.last_active_at_bj THEN excluded.last_active_at_bj
                ELSE streamer_external_profiles.last_active_at_bj
            END,
            is_real_person = excluded.is_real_person,
            source_name = excluded.source_name,
            source_payload = excluded.source_payload,
            snapshot_at = excluded.snapshot_at,
            updated_at = excluded.updated_at
        """,
        (
            row['app_name'], row['guild_executor_key'], row['guild_name'], row.get('country', ''),
            row['streamer_id'], row.get('platform_user_id', ''), row.get('platform_character_id', ''),
            row.get('nickname', ''), row.get('registered_at_bj', ''), row.get('last_active_at_bj', ''),
            int(row.get('is_real_person', 0)), row['source_name'], row.get('source_payload', '{}'),
            row['snapshot_at'], row['updated_at'],
        ),
    )
    source_payload = row.get('source_payload', '{}')
    try:
        official_fields = json.loads(source_payload) if isinstance(source_payload, str) else dict(source_payload or {})
    except (TypeError, ValueError):
        official_fields = {}
    app_name = str(row['app_name'] or '').strip().lower()
    source_timezone = str(row.get('source_timezone') or ('UTC' if app_name == 'linky' else 'Asia/Shanghai'))
    upsert_streamer_profile(
        conn,
        app_name=app_name,
        guild_executor_key=str(row['guild_executor_key']),
        guild_name=str(row['guild_name']),
        country=str(row.get('country') or ''),
        streamer_id=str(row['streamer_id']),
        platform_user_id=str(row.get('platform_user_id') or ''),
        platform_character_id=str(row.get('platform_character_id') or ''),
        nickname=str(row.get('nickname') or ''),
        registered_at=str(row.get('registered_at_bj') or ''),
        registered_timezone=source_timezone,
        last_active_at=str(row.get('last_active_at_bj') or ''),
        last_active_timezone=source_timezone,
        is_real_person_status=str(row.get('is_real_person_status') or 'unknown'),
        status=str(row.get('status') or ''),
        role=str(row.get('role') or ''),
        official_fields=official_fields if isinstance(official_fields, dict) else {},
        source_name=str(row['source_name']),
        source_run_id=str(row.get('run_id') or ''),
        observed_at=str(row['updated_at']),
    )


def _upsert_revenue(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO streamer_external_revenue_daily (
            app_name, guild_executor_key, guild_name, country, stat_date_bj,
            streamer_id, nickname, total_income, chat_income, voice_room_income,
            video_income, gift_income, other_income, agency_income, active_days,
            source_name, source_payload, snapshot_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(app_name, guild_executor_key, stat_date_bj, streamer_id) DO UPDATE SET
            guild_name = excluded.guild_name,
            country = excluded.country,
            nickname = excluded.nickname,
            total_income = excluded.total_income,
            chat_income = excluded.chat_income,
            voice_room_income = excluded.voice_room_income,
            video_income = excluded.video_income,
            gift_income = excluded.gift_income,
            other_income = excluded.other_income,
            agency_income = excluded.agency_income,
            active_days = excluded.active_days,
            source_name = excluded.source_name,
            source_payload = excluded.source_payload,
            snapshot_at = excluded.snapshot_at,
            updated_at = excluded.updated_at
        """,
        (
            row['app_name'], row['guild_executor_key'], row['guild_name'], row.get('country', ''),
            row['stat_date_bj'], row['streamer_id'], row.get('nickname', ''), row.get('total_income', 0),
            row.get('chat_income', 0), row.get('voice_room_income', 0), row.get('video_income', 0),
            row.get('gift_income', 0), row.get('other_income', 0), row.get('agency_income', 0),
            row.get('active_days', 0), row['source_name'], row.get('source_payload', '{}'),
            row['snapshot_at'], row['updated_at'],
        ),
    )
    source_payload = row.get('source_payload', '{}')
    try:
        official_fields = json.loads(source_payload) if isinstance(source_payload, str) else dict(source_payload or {})
    except (TypeError, ValueError):
        official_fields = {}
    app_name = str(row['app_name'] or '').strip().lower()
    record_streamer_daily_revision(
        conn,
        app_name=app_name,
        guild_executor_key=str(row['guild_executor_key']),
        guild_name=str(row['guild_name']),
        country=str(row.get('country') or ''),
        business_date=str(row['stat_date_bj']),
        source_timezone=str(row.get('source_timezone') or ('UTC' if app_name == 'linky' else 'Asia/Shanghai')),
        native_streamer_id=str(row['streamer_id']),
        total_income=float(row.get('total_income') or 0),
        chat_income=float(row.get('chat_income') or 0),
        voice_room_income=float(row.get('voice_room_income') or 0),
        video_income=float(row.get('video_income') or 0),
        gift_income=float(row.get('gift_income') or 0),
        other_income=float(row.get('other_income') or 0),
        agency_income=float(row.get('agency_income') or 0),
        active_days=int(row.get('active_days') or 0),
        official_fields=official_fields if isinstance(official_fields, dict) else {},
        source_name=str(row['source_name']),
        source_run_id=str(row.get('run_id') or ''),
        observed_at=str(row['updated_at']),
        platform_user_id=str(row.get('platform_user_id') or ''),
        platform_character_id=str(row.get('platform_character_id') or row['streamer_id'] if app_name == 'linky' else ''),
    )


def _upsert_guild_revenue(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO streamer_external_guild_revenue_daily (
            app_name, guild_executor_key, guild_name, country, stat_date_bj,
            total_income, chat_income, voice_room_income, platform_total_income,
            streamer_detail_income, reconciliation_delta,
            source_row_count, streamer_count, source_name, source_payload,
            snapshot_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(app_name, guild_executor_key, stat_date_bj) DO UPDATE SET
            guild_name = excluded.guild_name,
            country = excluded.country,
            total_income = excluded.total_income,
            chat_income = excluded.chat_income,
            voice_room_income = excluded.voice_room_income,
            platform_total_income = excluded.platform_total_income,
            streamer_detail_income = excluded.streamer_detail_income,
            reconciliation_delta = excluded.reconciliation_delta,
            source_row_count = excluded.source_row_count,
            streamer_count = excluded.streamer_count,
            source_name = excluded.source_name,
            source_payload = excluded.source_payload,
            snapshot_at = excluded.snapshot_at,
            updated_at = excluded.updated_at
        """,
        (
            row['app_name'], row['guild_executor_key'], row['guild_name'], row.get('country', ''),
            row['stat_date_bj'], row.get('total_income', 0), row.get('chat_income', 0),
            row.get('voice_room_income', 0), row.get('platform_total_income', 0),
            row.get('streamer_detail_income', 0), row.get('reconciliation_delta', 0),
            row.get('source_row_count', 0), row.get('streamer_count', 0), row['source_name'],
            row.get('source_payload', '{}'), row['snapshot_at'], row['updated_at'],
        ),
    )


def _sugo_get(executor: sqlite3.Row, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    base = str(executor['platform_backend_url'] or 'https://union.sugo.com/union_leader/api').rstrip('/')
    response = requests.get(
        base + '/' + path.lstrip('/'),
        params=params,
        headers={
            'Authorization': _auth_header(executor['platform_authorization']),
            'Accept': 'application/json',
            'Origin': 'https://union.sugo.com',
            'Referer': 'https://union.sugo.com/',
            'User-Agent': 'Mozilla/5.0 MCN-Automation StreamerAnalytics/1.0',
        },
        timeout=max(10.0, min(float(executor['request_timeout_seconds'] or 30), 60.0)),
    )
    if response.status_code in (401, 403):
        raise RuntimeError('sugo_income_permission_denied')
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get('code') not in (None, 0, 200, '200'):
        raise RuntimeError('sugo_income_api_rejected')
    return payload


def sync_sugo(
    conn: sqlite3.Connection,
    executors: Iterable[sqlite3.Row],
    start: date,
    end: date,
    *,
    run_id: str = '',
    trigger_type: str = 'scheduled',
) -> Dict[str, Any]:
    snapshot_at = _now()
    profile_count = 0
    revenue_count = 0
    guild_count = 0
    for executor in executors:
        guild_count += 1
        executor_key = _executor_key(executor)
        day = start
        while day <= end:
            scope_started_at = _now()
            page = 1
            total = None
            raw_rows: List[Dict[str, Any]] = []
            while True:
                request_params = {
                    'page': page,
                    'page_size': 500,
                    'start_time': f'{day.isoformat()} 00:00:00',
                    'end_time': f'{day.isoformat()} 23:59:59',
                }
                payload = _sugo_get(executor, '/anchor/anchor_income/', request_params)
                data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
                rows = data.get('data') if isinstance(data.get('data'), list) else []
                archive_raw_json(
                    conn,
                    run_id=run_id,
                    app_name='sugo',
                    dataset='anchor_income',
                    endpoint='/anchor/anchor_income/',
                    payload=payload,
                    guild_executor_key=executor_key,
                    guild_name=str(executor['guild_name']),
                    business_date=day.isoformat(),
                    source_timezone='Asia/Shanghai',
                    page_number=page,
                    request_params=request_params,
                    row_count=len(rows),
                )
                try:
                    total = int(data.get('total') or 0)
                except (TypeError, ValueError):
                    total = len(rows)
                raw_rows.extend(raw for raw in rows if isinstance(raw, dict))
                if not rows or page * 500 >= (total or 0):
                    break
                page += 1
            expected_count = int(total or 0)
            if len(raw_rows) != expected_count:
                raise RuntimeError(
                    f'sugo_pagination_incomplete:{executor["guild_name"]}:{day.isoformat()}:'
                    f'expected={expected_count}:scanned={len(raw_rows)}'
                )
            streamer_ids = [str(raw.get('anchorId') or '').strip() for raw in raw_rows]
            if any(not streamer_id for streamer_id in streamer_ids):
                raise RuntimeError(f'sugo_missing_streamer_id:{executor["guild_name"]}:{day.isoformat()}')
            if len(set(streamer_ids)) != len(streamer_ids):
                raise RuntimeError(f'sugo_duplicate_streamer_id:{executor["guild_name"]}:{day.isoformat()}')

            conn.execute(
                "DELETE FROM streamer_external_revenue_daily "
                "WHERE app_name = 'sugo' AND guild_executor_key = ? AND stat_date_bj = ?",
                (executor_key, day.isoformat()),
            )
            for raw in raw_rows:
                streamer_id = str(raw.get('anchorId') or '').strip()
                total_income = _number(raw.get('total_Income'))
                chat_income = _number(raw.get('chat_revenue'))
                video_income = _number(raw.get('video_revenue'))
                gift_income = sum(_number(raw.get(key)) for key in ('chat_gift', 'party_gift', 'video_gift', 'family_gift'))
                known = chat_income + video_income + gift_income
                source_payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
                last_active = f'{day.isoformat()}T23:59:59+08:00' if total_income != 0 or int(raw.get('activeDays') or 0) > 0 else ''
                _upsert_profile(conn, {
                    'app_name': 'sugo', 'guild_executor_key': executor_key,
                    'guild_name': str(executor['guild_name']), 'country': str(raw.get('country') or executor['country'] or ''),
                    'streamer_id': streamer_id, 'nickname': str(raw.get('nickname') or ''),
                    'registered_at_bj': _bj_iso_from_epoch(raw.get('joinUnionTime')),
                    'last_active_at_bj': last_active, 'source_name': 'sugo_anchor_income',
                    'is_real_person': 0, 'is_real_person_status': 'unknown',
                    'source_timezone': 'Asia/Shanghai', 'run_id': run_id,
                    'source_payload': source_payload, 'snapshot_at': snapshot_at, 'updated_at': snapshot_at,
                })
                _upsert_revenue(conn, {
                    'app_name': 'sugo', 'guild_executor_key': executor_key,
                    'guild_name': str(executor['guild_name']), 'country': str(raw.get('country') or executor['country'] or ''),
                    'stat_date_bj': day.isoformat(), 'streamer_id': streamer_id,
                    'nickname': str(raw.get('nickname') or ''), 'total_income': total_income,
                    'chat_income': chat_income, 'video_income': video_income, 'gift_income': gift_income,
                    'other_income': total_income - known, 'active_days': int(raw.get('activeDays') or 0),
                    'source_name': 'sugo_anchor_income', 'source_payload': source_payload,
                    'source_timezone': 'Asia/Shanghai', 'run_id': run_id,
                    'platform_character_id': streamer_id,
                    'snapshot_at': snapshot_at, 'updated_at': snapshot_at,
                })
                profile_count += 1
                revenue_count += 1
            record_ingestion_scope(
                conn,
                run_id=run_id,
                app_name='sugo',
                dataset='anchor_income',
                guild_executor_key=executor_key,
                guild_name=str(executor['guild_name']),
                business_date=day.isoformat(),
                source_timezone='Asia/Shanghai',
                trigger_type=trigger_type,
                status='success',
                expected_rows=expected_count,
                scanned_rows=len(raw_rows),
                saved_rows=len(raw_rows),
                detail_income=sum(_number(raw.get('total_Income')) for raw in raw_rows),
                started_at=scope_started_at,
            )
            conn.commit()
            day += timedelta(days=1)
    return {'guild_count': guild_count, 'profile_count': profile_count, 'revenue_count': revenue_count}


def _sync_sugo_isolated(
    conn: sqlite3.Connection,
    executors: Iterable[sqlite3.Row],
    start: date,
    end: date,
    *,
    run_id: str,
    trigger_type: str,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        'guild_count': 0,
        'successful_guild_count': 0,
        'profile_count': 0,
        'revenue_count': 0,
        'failed_guilds': [],
    }
    for executor in executors:
        result['guild_count'] += 1
        guild_name = str(executor['guild_name'] or '').strip()
        try:
            guild_result = sync_sugo(
                conn,
                [executor],
                start,
                end,
                run_id=run_id,
                trigger_type=trigger_type,
            )
            result['successful_guild_count'] += 1
            result['profile_count'] += int(guild_result.get('profile_count') or 0)
            result['revenue_count'] += int(guild_result.get('revenue_count') or 0)
        except Exception as exc:
            conn.rollback()
            detail = str(exc)
            result['failed_guilds'].append({'guild_name': guild_name, 'error': detail[:160]})
            record_ingestion_scope(
                conn,
                run_id=run_id,
                app_name='sugo',
                dataset='anchor_income',
                guild_executor_key=_executor_key(executor),
                guild_name=guild_name,
                business_date=end.isoformat(),
                source_timezone='Asia/Shanghai',
                trigger_type=trigger_type,
                status='failed',
                error_code=detail.split(':', 1)[0],
                error_message=detail,
            )
            conn.commit()
    return result


def _linky_country_header(value: object) -> str:
    normalized = str(value or '').strip().lower()
    if normalized in {'indonesia', 'indonesia/id', 'id', '印尼'}:
        return 'ID'
    if normalized in {'brazil', 'br', '巴西'}:
        return 'BR'
    if normalized in {'mexico', 'mx', '墨西哥'}:
        return 'MX'
    return 'US'


def _linky_proxy_url(executor: sqlite3.Row) -> str:
    explicit = str(executor['proxy_url'] or '').strip()
    if explicit:
        return explicit
    region = str(executor['proxy_region'] or '').strip()
    if not region:
        return ''
    try:
        mapping = json.loads(os.getenv('GUILD_EXECUTOR_PROXY_REGION_URLS') or '{}')
    except (TypeError, ValueError):
        mapping = {}
    return str(mapping.get(region) or '').strip() if isinstance(mapping, dict) else ''


def _linky_signed_get(executor: sqlite3.Row, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    oauth_token = str(executor['oauth_token'] or '').strip()
    oauth_secret = str(executor['oauth_token_secret'] or '').strip()
    if not oauth_token or not oauth_secret:
        raise RuntimeError('linky_oauth_not_configured')
    ordered = [(str(key), value) for key, value in params.items() if value is not None and str(value) != '']
    query = '?' + '&'.join(
        f"{quote(key, safe='')}={quote(str(value), safe='')}" for key, value in ordered
    ) if ordered else ''
    country = _linky_country_header(executor['country'])
    proxy_url = _linky_proxy_url(executor)
    proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None
    timeout = max(10.0, min(float(executor['request_timeout_seconds'] or 30), 60.0))
    for attempt in range(3):
        timestamp_ms = str(int(time.time() * 1000))
        signature_base = f'{path}{query}&{timestamp_ms}' if query else f'{path}&{timestamp_ms}'
        signature = base64.b64encode(
            hmac.new(oauth_secret.encode('utf-8'), signature_base.encode('utf-8'), hashlib.sha1).digest()
        ).decode('ascii')
        response = requests.get(
            f'https://api.linke.ai{path}',
            params=dict(ordered),
            headers={
                'X-Auth-Token': oauth_token,
                'X-Auth-Timestamp': timestamp_ms,
                'X-Auth-Signature': signature,
                'X-App-Language': 'en',
                'Country': country,
                'Accept': 'application/json, text/plain, */*',
                'Origin': 'https://guild.linke.ai',
                'Referer': 'https://guild.linke.ai/guild/anchorData',
                'User-Agent': f'Mozilla/5.0 MCN-Automation StreamerAnalytics/1.0 Language/en Country/{country}',
            },
            proxies=proxies,
            timeout=timeout,
        )
        if response.status_code in (401, 403):
            raise RuntimeError('linky_oauth_rejected')
        if response.status_code not in (429,) and response.status_code < 500:
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError('linky_guild_api_invalid_response')
            if payload.get('error'):
                raise RuntimeError('linky_guild_api_rejected')
            return payload
        if attempt == 2:
            response.raise_for_status()
        time.sleep(attempt + 1)
    raise RuntimeError('linky_guild_api_unavailable')


def _linky_anchor_directory(
    executor: sqlite3.Row,
    *,
    recent_only: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    run_id: str = '',
    trigger_type: str = 'scheduled',
) -> Dict[str, Any]:
    page_size = max(100, min(2000, int(os.getenv('LINKY_ANCHOR_DIRECTORY_PAGE_SIZE') or 1000)))
    first = _linky_signed_get(executor, '/api/guild/search_anchors', {
        'id': '', 'page': 1, 'page_size': page_size,
    })
    total_anchors = int(first.get('total_anchors') or 0)
    page_count = max(1, (total_anchors + page_size - 1) // page_size)
    pages = [1]
    if recent_only and page_count > 1:
        pages.append(page_count)
    elif not recent_only:
        pages.extend(range(2, page_count + 1))

    by_sid: Dict[str, Dict[str, Any]] = {}
    by_user_id: Dict[str, Dict[str, Any]] = {}
    raw_count = 0
    for page in pages:
        payload = first if page == 1 else _linky_signed_get(executor, '/api/guild/search_anchors', {
            'id': '', 'page': page, 'page_size': page_size,
        })
        items = [dict(row) for row in (payload.get('items') or []) if isinstance(row, dict)]
        if conn is not None:
            archive_raw_json(
                conn,
                run_id=run_id,
                app_name='linky',
                dataset='anchor_directory',
                endpoint='/api/guild/search_anchors',
                payload=payload,
                guild_executor_key=_executor_key(executor),
                guild_name=str(executor['guild_name'] or ''),
                source_timezone='UTC',
                page_number=page,
                request_params={'id': '', 'page': page, 'page_size': page_size},
                row_count=len(items),
            )
            conn.commit()
        raw_count += len(items)
        for raw in items:
            sid = str(raw.get('sid') or '').strip()
            user_id = str(raw.get('user_id') or '').strip()
            metadata: Dict[str, Any] = {
                'sid': sid,
                'user_id': user_id,
                'nickname': str(raw.get('nick_name') or ''),
                'registered_at_bj': _utc_iso_from_epoch(raw.get('created_at')),
                'official_fields': raw,
            }
            if sid:
                by_sid[sid] = metadata
            if user_id:
                by_user_id[user_id] = metadata
    result = {
        'by_sid': by_sid,
        'by_user_id': by_user_id,
        'total_anchors': total_anchors,
        'raw_count': raw_count,
    }
    if conn is not None:
        record_ingestion_scope(
            conn,
            run_id=run_id,
            app_name='linky',
            dataset='anchor_directory',
            guild_executor_key=_executor_key(executor),
            guild_name=str(executor['guild_name'] or ''),
            source_timezone='UTC',
            trigger_type=trigger_type,
            status='success',
            expected_rows=total_anchors,
            scanned_rows=raw_count,
            saved_rows=raw_count,
        )
    return result


def backfill_linky_profile_metadata(
    conn: sqlite3.Connection,
    *,
    guild_name: str = '',
) -> Dict[str, Any]:
    ensure_streamer_analytics_views(conn)
    conn.row_factory = sqlite3.Row
    executors = _enabled_executors(conn, 'linky', str(guild_name or '').strip())
    matched_profiles = 0
    updated_profiles = 0
    inserted_profiles = 0
    directory_rows = 0
    failed_guilds: List[Dict[str, str]] = []
    run_id = f'linky_profile_backfill_{uuid.uuid4().hex}'
    for executor in executors:
        executor_key = _executor_key(executor)
        current_guild = str(executor['guild_name'] or '')
        try:
            try:
                directory = _linky_anchor_directory(
                    executor,
                    conn=conn,
                    run_id=run_id,
                    trigger_type='backfill',
                )
            except TypeError as exc:
                if 'unexpected keyword argument' not in str(exc):
                    raise
                directory = _linky_anchor_directory(executor)
            directory_rows += int(directory['raw_count'] or 0)
            directory_snapshot_at = _now()
            profiles = conn.execute(
                """
                SELECT streamer_id, platform_user_id, platform_character_id, registered_at_bj
                FROM streamer_external_profiles
                WHERE app_name = 'linky' AND guild_executor_key = ?
                """,
                (executor_key,),
            ).fetchall()
            existing_streamer_ids = {
                str(profile['streamer_id'] or '').strip() for profile in profiles
            }
            seen_profiles = _linky_seen_profiles(conn, executor_key)
            for metadata in directory['by_sid'].values():
                sid = str(metadata.get('sid') or '').strip()
                if not sid:
                    continue
                user_id = str(metadata.get('user_id') or '').strip()
                seen = seen_profiles.get(user_id) or seen_profiles.get(sid, {})
                registered_at_bj = str(
                    metadata.get('registered_at_bj') or seen.get('registered_at_bj') or ''
                )
                nickname = str(metadata.get('nickname') or seen.get('nickname') or '')
                if sid in existing_streamer_ids:
                    matched_profiles += 1
                else:
                    inserted_profiles += 1
                official_fields = dict(metadata.get('official_fields') or {})
                source_payload = json.dumps(
                    official_fields or {
                        'sid': sid,
                        'user_id': user_id,
                        'created_at': registered_at_bj,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(',', ':'),
                    default=str,
                )
                _upsert_profile(conn, {
                    'app_name': 'linky',
                    'guild_executor_key': executor_key,
                    'guild_name': current_guild,
                    'country': str(executor['country'] or ''),
                    'streamer_id': sid,
                    'platform_user_id': user_id,
                    'platform_character_id': sid,
                    'nickname': nickname,
                    'registered_at_bj': registered_at_bj,
                    'last_active_at_bj': '',
                    'is_real_person': 0,
                    'is_real_person_status': 'unknown',
                    'source_timezone': 'UTC',
                    'run_id': run_id,
                    'source_name': 'linky_anchor_directory',
                    'source_payload': source_payload,
                    'snapshot_at': directory_snapshot_at,
                    'updated_at': directory_snapshot_at,
                })
                updated_profiles += 1
            for profile in profiles:
                sid = str(profile['platform_character_id'] or profile['streamer_id'] or '').strip()
                user_id = str(profile['platform_user_id'] or '').strip()
                metadata = directory['by_sid'].get(sid) or directory['by_user_id'].get(user_id)
                if not metadata:
                    continue
                registered_at = str(metadata.get('registered_at_bj') or '')
                if not registered_at:
                    continue
                conn.execute(
                    """
                    UPDATE streamer_external_profiles
                    SET registered_at_bj = CASE
                            WHEN registered_at_bj = '' OR ? < registered_at_bj THEN ?
                            ELSE registered_at_bj
                        END,
                        nickname = CASE WHEN nickname = '' AND ? <> '' THEN ? ELSE nickname END,
                        updated_at = ?
                    WHERE app_name = 'linky' AND guild_executor_key = ? AND streamer_id = ?
                    """,
                    (
                        registered_at, registered_at,
                        str(metadata.get('nickname') or ''), str(metadata.get('nickname') or ''),
                        _now(), executor_key, str(profile['streamer_id']),
                    ),
                )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            failed_guilds.append({'guild_name': current_guild, 'error': str(exc)[:160]})
    return {
        'ok': not failed_guilds,
        'guild_count': len(executors),
        'directory_rows': directory_rows,
        'matched_profiles': matched_profiles,
        'updated_profiles': updated_profiles,
        'inserted_profiles': inserted_profiles,
        'failed_guilds': failed_guilds,
    }


def _linky_seen_profiles(conn: sqlite3.Connection, executor_key: str) -> Dict[str, Dict[str, str]]:
    rows = conn.execute(
        """
        SELECT anchor_id, anchor_name, created_at, created_date_utc, created_date_bj
        FROM guild_anchor_seen
        WHERE guild_executor_key = ?
        """,
        (executor_key,),
    ).fetchall()
    return {
        str(row['anchor_id'] or ''): {
            'nickname': str(row['anchor_name'] or ''),
            'registered_at_bj': _utc_iso_from_epoch(row['created_at'])
            or (f"{row['created_date_utc']}T00:00:00+00:00" if row['created_date_utc'] else ''),
        }
        for row in rows
        if str(row['anchor_id'] or '').strip()
    }


def _linky_is_active(raw: Dict[str, Any]) -> bool:
    activity_fields = (
        'total_earns', 'online_time', 'send_msg_num', 'recv_msg_num',
        'recv_conversation_num', 'reply_conversation_num', 'new_level4_num',
    )
    return any(_number(raw.get(key)) != 0 for key in activity_fields)


LINKY_EFFECTIVE_INCOME_BASIS = 'chat_earns_plus_live_room_receive_diamonds'
LINKY_NON_INDONESIA_EFFECTIVE_INCOME_BASIS = 'chat_earns_only_voice_room_stored'
LINKY_EFFECTIVE_INCOME_VERSION = 4


def _assert_linky_streamer_stat_ready(
    conn: sqlite3.Connection,
    executor_key: str,
    business_date: date,
    source_row_count: int,
) -> None:
    previous = conn.execute(
        """
        SELECT source_row_count
        FROM streamer_external_guild_revenue_daily
        WHERE app_name='linky' AND guild_executor_key=? AND stat_date_bj<?
        ORDER BY stat_date_bj DESC
        LIMIT 1
        """,
        (executor_key, business_date.isoformat()),
    ).fetchone()
    if previous is None:
        return
    previous_count = int(previous[0] or 0)
    if previous_count <= 0:
        return
    if not is_linky_source_row_count_ready(
        current_count=source_row_count,
        previous_count=previous_count,
    ):
        raise RuntimeError(
            'linky_guild_source_not_ready:'
            f'current={source_row_count}:previous={previous_count}'
        )


def _linky_voice_room_included_in_analytics(country: object) -> bool:
    return str(country or '').strip().lower() == 'indonesia'


def _linky_effective_income_basis(country: object) -> str:
    if _linky_voice_room_included_in_analytics(country):
        return LINKY_EFFECTIVE_INCOME_BASIS
    return LINKY_NON_INDONESIA_EFFECTIVE_INCOME_BASIS


def _linky_live_room_income_attempt(
    executor: sqlite3.Row,
    day: date,
    *,
    conn: Optional[sqlite3.Connection] = None,
    run_id: str = '',
    consistency_attempt: int = 1,
) -> Dict[str, Any]:
    page_size = max(100, min(5000, int(os.getenv('LINKY_LIVE_ROOM_STAT_PAGE_SIZE') or 1000)))
    page = 1
    total_rows = 0
    official_income = 0.0
    detail_income = 0.0
    scanned_count = 0
    positive_row_count = 0
    by_sid: Dict[str, Dict[str, Any]] = {}
    seen_positive_rows: set[str] = set()
    while True:
        request_params = {
            'begin': int(day.strftime('%Y%m%d')),
            'end': int(day.strftime('%Y%m%d')),
            'page_num': page,
            'page_size': page_size,
            'type': 0,
            'sid': '',
        }
        payload = _linky_signed_get(executor, '/api/guild/live_room_stat', request_params)
        batch = [dict(row) for row in (payload.get('items') or []) if isinstance(row, dict)]
        if conn is not None:
            archive_raw_json(
                conn,
                run_id=run_id,
                app_name='linky',
                dataset='live_room_stat',
                endpoint='/api/guild/live_room_stat',
                payload=payload,
                guild_executor_key=_executor_key(executor),
                guild_name=str(executor['guild_name'] or ''),
                business_date=day.isoformat(),
                source_timezone='UTC',
                page_number=((consistency_attempt - 1) * 10000) + page,
                request_params={**request_params, 'consistency_attempt': consistency_attempt},
                row_count=len(batch),
            )
            conn.commit()
        # Linky's previous-day aggregate can still settle while pages are
        # being read. Follow the newest total instead of freezing page 1.
        total_rows = max(total_rows, int(payload.get('total') or 0))
        total_item = payload.get('total_item') if isinstance(payload.get('total_item'), dict) else {}
        official_income = _number(total_item.get('receive_diamonds'))
        scanned_count += len(batch)
        for raw in batch:
            income = _number(raw.get('receive_diamonds'))
            if income == 0:
                continue
            row_fingerprint = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
            if row_fingerprint in seen_positive_rows:
                continue
            seen_positive_rows.add(row_fingerprint)
            sid = str(raw.get('sid') or '').strip()
            if not sid:
                raise RuntimeError('linky_live_room_income_missing_streamer_id')
            positive_row_count += 1
            detail_income += income
            current = by_sid.setdefault(sid, {
                'income': 0.0,
                'user_id': str(raw.get('user_id') or '').strip(),
                'nickname': str(raw.get('nick_name') or ''),
                'on_mic_time': 0.0,
                'valid_days': 0,
                'room_ids': [],
            })
            current['income'] += income
            current['on_mic_time'] += _number(raw.get('on_mic_time'))
            current['valid_days'] += int(_number(raw.get('valid_days')))
            room_id = str(raw.get('room_id') or '').strip()
            if room_id and room_id not in current['room_ids']:
                current['room_ids'].append(room_id)
            if not current['user_id']:
                current['user_id'] = str(raw.get('user_id') or '').strip()
            if not current['nickname']:
                current['nickname'] = str(raw.get('nick_name') or '')
        if abs(detail_income - official_income) <= 0.000001:
            break
        if scanned_count >= total_rows or not batch:
            break
        page += 1
        if page > 1000:
            raise RuntimeError('linky_live_room_page_limit_exceeded')
    verification_params = {
        'begin': int(day.strftime('%Y%m%d')),
        'end': int(day.strftime('%Y%m%d')),
        'page_num': 1,
        'page_size': page_size,
        'type': 0,
        'sid': '',
    }
    verification = _linky_signed_get(executor, '/api/guild/live_room_stat', verification_params)
    verification_items = [dict(row) for row in (verification.get('items') or []) if isinstance(row, dict)]
    if conn is not None:
        archive_raw_json(
            conn,
            run_id=run_id,
            app_name='linky',
            dataset='live_room_stat_verify',
            endpoint='/api/guild/live_room_stat',
            payload=verification,
            guild_executor_key=_executor_key(executor),
            guild_name=str(executor['guild_name'] or ''),
            business_date=day.isoformat(),
            source_timezone='UTC',
            page_number=consistency_attempt,
            request_params={**verification_params, 'consistency_attempt': consistency_attempt},
            row_count=len(verification_items),
        )
        conn.commit()
    verification_total = int(verification.get('total') or 0)
    verification_item = verification.get('total_item') if isinstance(verification.get('total_item'), dict) else {}
    verification_income = _number(verification_item.get('receive_diamonds'))
    return {
        'official_income': official_income,
        'detail_income': detail_income,
        'by_sid': by_sid,
        'source_row_count': total_rows,
        'positive_row_count': positive_row_count,
        'page_count': page,
        'source_stable': verification_total == total_rows and abs(verification_income - official_income) <= 0.000001,
        'verification_total': verification_total,
        'verification_income': verification_income,
    }


def _linky_live_room_income(
    executor: sqlite3.Row,
    day: date,
    *,
    conn: Optional[sqlite3.Connection] = None,
    run_id: str = '',
) -> Dict[str, Any]:
    max_attempts = max(1, min(5, int(os.getenv('LINKY_LIVE_ROOM_CONSISTENCY_ATTEMPTS') or 3)))
    retry_delay = max(0, min(300, int(os.getenv('LINKY_LIVE_ROOM_CONSISTENCY_RETRY_SECONDS') or 60)))
    last_result: Dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        last_result = _linky_live_room_income_attempt(
            executor,
            day,
            conn=conn,
            run_id=run_id,
            consistency_attempt=attempt,
        )
        income_matches = abs(
            float(last_result.get('detail_income') or 0)
            - float(last_result.get('official_income') or 0)
        ) <= 0.000001
        if bool(last_result.get('source_stable')) and income_matches:
            last_result.pop('source_stable', None)
            last_result.pop('verification_total', None)
            last_result.pop('verification_income', None)
            last_result['consistency_attempts'] = attempt
            return last_result
        if attempt < max_attempts and retry_delay:
            time.sleep(retry_delay)
    raise RuntimeError(
        'linky_live_room_income_mismatch:'
        f"attempts={max_attempts}:official={float(last_result.get('official_income') or 0)}:"
        f"detail={float(last_result.get('detail_income') or 0)}:"
        f"verification_official={float(last_result.get('verification_income') or 0)}:"
        f"rows={int(last_result.get('source_row_count') or 0)}:"
        f"verification_rows={int(last_result.get('verification_total') or 0)}"
    )


def _linky_source_payload(
    raw: Dict[str, Any],
    *,
    country: object = '',
    voice_room: Optional[Dict[str, Any]] = None,
) -> str:
    payload = dict(raw)
    room = voice_room or {}
    payload.update({
        'voice_room_income': _number(room.get('income')),
        'voice_room_on_mic_time': _number(room.get('on_mic_time')),
        'voice_room_valid_days': int(_number(room.get('valid_days'))),
        'voice_room_ids': list(room.get('room_ids') or []),
        'voice_room_analytics_included': _linky_voice_room_included_in_analytics(country),
        'effective_income_basis': _linky_effective_income_basis(country),
        'effective_income_version': LINKY_EFFECTIVE_INCOME_VERSION,
    })
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


def sync_linky(
    conn: sqlite3.Connection,
    executors: Iterable[sqlite3.Row],
    start: date,
    end: date,
    *,
    run_id: str = '',
    trigger_type: str = 'scheduled',
) -> Dict[str, Any]:
    snapshot_at = _now()
    profile_count = 0
    revenue_count = 0
    guild_count = 0
    successful_guild_count = 0
    failed_guilds: List[Dict[str, str]] = []
    for executor in executors:
        guild_count += 1
        guild_name = str(executor['guild_name'] or '').strip()
        country = str(executor['country'] or '')
        include_voice_room_income = _linky_voice_room_included_in_analytics(country)
        executor_key = _executor_key(executor)
        try:
            seen_profiles = _linky_seen_profiles(conn, executor_key)
            try:
                directory = _linky_anchor_directory(
                    executor,
                    recent_only=True,
                    conn=conn,
                    run_id=run_id,
                    trigger_type=trigger_type,
                )
            except Exception as directory_exc:
                record_ingestion_scope(
                    conn,
                    run_id=run_id,
                    app_name='linky',
                    dataset='anchor_directory',
                    guild_executor_key=executor_key,
                    guild_name=guild_name,
                    source_timezone='UTC',
                    trigger_type=trigger_type,
                    status='failed',
                    error_code=str(directory_exc).split(':', 1)[0],
                    error_message=str(directory_exc),
                )
                directory = {'by_sid': {}, 'by_user_id': {}}
            # Raw directory pages are durable audit data.  Commit them before
            # the next remote request so SQLite's write lock is not held while
            # waiting on Linky.
            conn.commit()
            day = start
            while day <= end:
                scope_started_at = _now()
                page_size = max(100, min(5000, int(os.getenv('LINKY_STREAMER_STAT_PAGE_SIZE') or 5000)))
                reported_platform_income: Optional[float] = None
                reported_chat_income: Optional[float] = None
                deduped_rows: Dict[str, Dict[str, Any]] = {}
                page = 1
                total_rows: Optional[int] = None
                scanned_count = 0
                while True:
                    request_params = {
                        'begin': int(day.strftime('%Y%m%d')),
                        'end': int(day.strftime('%Y%m%d')),
                        'page_num': page,
                        'page_size': page_size,
                        'type': 0,
                    }
                    payload = _linky_signed_get(executor, '/api/guild/streamer_stat', request_params)
                    batch = [dict(row) for row in (payload.get('items') or []) if isinstance(row, dict)]
                    archive_raw_json(
                        conn,
                        run_id=run_id,
                        app_name='linky',
                        dataset='streamer_stat',
                        endpoint='/api/guild/streamer_stat',
                        payload=payload,
                        guild_executor_key=executor_key,
                        guild_name=guild_name,
                        business_date=day.isoformat(),
                        source_timezone='UTC',
                        page_number=page,
                        request_params=request_params,
                        row_count=len(batch),
                    )
                    conn.commit()
                    if total_rows is None:
                        total_rows = int(payload.get('total') or 0)
                        total_item = payload.get('total_item') if isinstance(payload.get('total_item'), dict) else {}
                        reported_platform_income = _number(total_item.get('total_earns'))
                        if 'chat_earns' in total_item:
                            reported_chat_income = _number(total_item.get('chat_earns'))
                    scanned_count += len(batch)
                    for raw in batch:
                        sid = str(raw.get('sid') or '').strip()
                        if sid:
                            existing = deduped_rows.get(sid)
                            if existing is None:
                                deduped_rows[sid] = raw
                            elif existing != raw:
                                raise RuntimeError(
                                    'linky_guild_conflicting_duplicate_streamer_id'
                                )
                        elif _linky_is_active(raw):
                            raise RuntimeError('linky_guild_active_row_missing_streamer_id')
                    if scanned_count >= (total_rows or 0) or not batch:
                        break
                    page += 1
                    if page > 1000:
                        raise RuntimeError('linky_guild_page_limit_exceeded')
                if scanned_count != (total_rows or 0):
                    raise RuntimeError('linky_guild_row_count_mismatch')
                _assert_linky_streamer_stat_ready(
                    conn, executor_key, day, int(total_rows or 0),
                )
                # The page archive is independent of the derived daily rows.
                # Release the writer lock before the optional live-room API.
                conn.commit()
                room_result = _linky_live_room_income(
                    executor,
                    day,
                    conn=conn,
                    run_id=run_id,
                )
                conn.commit()
                room_by_sid = dict(room_result.get('by_sid') or {})
                detail_chat_income = sum(_number(raw.get('chat_earns')) for raw in deduped_rows.values())
                detail_voice_room_income = float(room_result.get('detail_income') or 0)
                chat_income_source = 'total_item'
                if reported_chat_income is None:
                    reported_chat_income = detail_chat_income
                    chat_income_source = 'complete_streamer_detail_sum'
                official_chat_income = float(reported_chat_income or 0)
                official_voice_room_income = float(room_result.get('official_income') or 0)
                detail_income = detail_chat_income + (
                    detail_voice_room_income if include_voice_room_income else 0
                )
                official_income = official_chat_income + (
                    official_voice_room_income if include_voice_room_income else 0
                )
                guild_source_payload = json.dumps({
                    'reported_platform_total_income': float(reported_platform_income or 0),
                    'reported_chat_income': official_chat_income,
                    'reported_chat_income_source': chat_income_source,
                    'reported_voice_room_income': official_voice_room_income,
                    'effective_total_income': official_income,
                    'streamer_detail_income': detail_income,
                    'reconciliation_delta': official_income - detail_income,
                    'source_row_count': int(total_rows or 0),
                    'voice_room_source_row_count': int(room_result.get('source_row_count') or 0),
                    'voice_room_positive_row_count': int(room_result.get('positive_row_count') or 0),
                    'voice_room_page_count': int(room_result.get('page_count') or 0),
                    'streamer_count': len(set(deduped_rows) | set(room_by_sid)),
                    'voice_room_analytics_included': include_voice_room_income,
                    'effective_income_basis': _linky_effective_income_basis(country),
                    'effective_income_version': LINKY_EFFECTIVE_INCOME_VERSION,
                    'official_total_item': total_item,
                }, ensure_ascii=False, separators=(',', ':'))

                conn.execute(
                    "DELETE FROM streamer_external_revenue_daily WHERE app_name = 'linky' AND guild_executor_key = ? AND stat_date_bj = ?",
                    (executor_key, day.isoformat()),
                )
                _upsert_guild_revenue(conn, {
                    'app_name': 'linky', 'guild_executor_key': executor_key,
                    'guild_name': guild_name, 'country': country,
                    'stat_date_bj': day.isoformat(), 'total_income': official_income,
                    'chat_income': official_chat_income,
                    'voice_room_income': official_voice_room_income,
                    'platform_total_income': float(reported_platform_income or 0),
                    'streamer_detail_income': detail_income,
                    'reconciliation_delta': official_income - detail_income,
                    'source_row_count': int(total_rows or 0),
                    'streamer_count': len(set(deduped_rows) | set(room_by_sid)),
                    'source_name': 'linky_guild_streamer_stat_total',
                    'source_payload': guild_source_payload,
                    'snapshot_at': snapshot_at, 'updated_at': snapshot_at,
                })
                write_chunk_rows = max(25, min(500, int(os.getenv('LINKY_SQLITE_WRITE_CHUNK_ROWS') or 50)))
                write_chunk_seconds = max(0.25, min(5.0, float(os.getenv('LINKY_SQLITE_WRITE_CHUNK_SECONDS') or 0.5)))
                chunk_started_at = time.monotonic()
                chunk_row_count = 0
                for streamer_id in sorted(set(deduped_rows) | set(room_by_sid)):
                    raw = deduped_rows.get(streamer_id, {'sid': streamer_id})
                    voice_room = room_by_sid.get(streamer_id, {})
                    if not _linky_is_active(raw) and _number(voice_room.get('income')) == 0:
                        continue
                    user_id = str(raw.get('user_id') or voice_room.get('user_id') or '').strip()
                    sid = str(raw.get('sid') or '').strip()
                    seen = (
                        directory['by_sid'].get(sid)
                        or directory['by_user_id'].get(user_id)
                        or seen_profiles.get(user_id)
                        or seen_profiles.get(streamer_id, {})
                    )
                    nickname = str(raw.get('nickname') or voice_room.get('nickname') or seen.get('nickname') or '')
                    source_payload = _linky_source_payload(
                        raw, country=country, voice_room=voice_room,
                    )
                    profile_source_payload = json.dumps(
                        {
                            'streamer_stat': json.loads(source_payload),
                            'anchor_directory': dict(seen.get('official_fields') or {}),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(',', ':'),
                        default=str,
                    )
                    chat_income = _number(raw.get('chat_earns'))
                    voice_room_income = _number(voice_room.get('income'))
                    total_income = chat_income + (
                        voice_room_income if include_voice_room_income else 0
                    )
                    gift_income = _number(raw.get('gift_earns'))
                    call_income = _number(raw.get('voice_call_earns'))
                    active_at = f'{day.isoformat()}T23:59:59+08:00'
                    _upsert_profile(conn, {
                        'app_name': 'linky', 'guild_executor_key': executor_key,
                        'guild_name': guild_name, 'country': country,
                        'streamer_id': streamer_id, 'platform_user_id': user_id,
                        'platform_character_id': sid, 'nickname': nickname,
                        'registered_at_bj': str(seen.get('registered_at_bj') or ''),
                        'last_active_at_bj': active_at, 'source_name': 'linky_guild_streamer_stat',
                        'is_real_person': 0, 'is_real_person_status': 'unknown',
                        'source_timezone': 'UTC', 'run_id': run_id,
                        'source_payload': profile_source_payload,
                        'snapshot_at': snapshot_at, 'updated_at': snapshot_at,
                    })
                    _upsert_revenue(conn, {
                        'app_name': 'linky', 'guild_executor_key': executor_key,
                        'guild_name': guild_name, 'country': country,
                        'stat_date_bj': day.isoformat(), 'streamer_id': streamer_id,
                        'nickname': nickname, 'total_income': total_income,
                        'chat_income': chat_income, 'voice_room_income': voice_room_income,
                        'video_income': call_income, 'gift_income': gift_income,
                        'other_income': _number(raw.get('total_earns')) - chat_income,
                        'active_days': 1, 'source_name': 'linky_guild_streamer_stat',
                        'source_payload': source_payload,
                        'snapshot_at': snapshot_at, 'updated_at': snapshot_at,
                        'source_timezone': 'UTC', 'run_id': run_id,
                        'platform_user_id': user_id, 'platform_character_id': sid,
                    })
                    profile_count += 1
                    revenue_count += 1
                    chunk_row_count += 1
                    if (
                        chunk_row_count >= write_chunk_rows
                        or time.monotonic() - chunk_started_at >= write_chunk_seconds
                    ):
                        # The active analytics store is published separately and
                        # atomically.  Commit source rows in bounded chunks so
                        # online heartbeats/claims can acquire SQLite between
                        # chunks instead of waiting behind one guild-sized txn.
                        conn.commit()
                        time.sleep(0.02)
                        chunk_started_at = time.monotonic()
                        chunk_row_count = 0
                record_ingestion_scope(
                    conn,
                    run_id=run_id,
                    app_name='linky',
                    dataset='streamer_stat',
                    guild_executor_key=executor_key,
                    guild_name=guild_name,
                    business_date=day.isoformat(),
                    source_timezone='UTC',
                    trigger_type=trigger_type,
                    status='success',
                    expected_rows=int(total_rows or 0),
                    scanned_rows=scanned_count,
                    saved_rows=len(set(deduped_rows) | set(room_by_sid)),
                    official_income=official_income,
                    detail_income=detail_income,
                    reconciliation_delta=official_income - detail_income,
                    started_at=scope_started_at,
                )
                conn.commit()
                day += timedelta(days=1)
            successful_guild_count += 1
        except Exception as exc:
            conn.rollback()
            failed_guilds.append({'guild_name': guild_name, 'error': str(exc)[:120]})
    if failed_guilds and not successful_guild_count:
        raise RuntimeError(str(failed_guilds[0]['error'] or 'linky_guild_sync_failed'))
    return {
        'guild_count': guild_count,
        'successful_guild_count': successful_guild_count,
        'failed_guilds': failed_guilds,
        'profile_count': profile_count,
        'revenue_count': revenue_count,
    }


def sync_streamer_external_data(
    conn: sqlite3.Connection,
    *,
    app_name: str,
    start: date,
    end: date,
    guild_name: str = '',
    trigger_type: str = 'manual',
) -> Dict[str, Any]:
    app = normalize_streamer_app(app_name)
    normalized_guild_name = str(guild_name or '').strip()
    run_scope = 'guild_backfill' if normalized_guild_name else 'full'
    ensure_streamer_analytics_views(conn)
    conn.row_factory = sqlite3.Row
    run_id = uuid.uuid4().hex
    now = _now()
    conn.execute(
        """
        INSERT INTO streamer_external_sync_runs(
            run_id, app_name, date_from, date_to, status, run_scope, scope_key,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)
        """,
        (run_id, app, start.isoformat(), end.isoformat(), run_scope, normalized_guild_name, now, now),
    )
    conn.commit()
    try:
        executors = _enabled_executors(conn, app, normalized_guild_name)
        result = (
            _sync_sugo_isolated(
                conn, executors, start, end,
                run_id=run_id, trigger_type=trigger_type,
            )
            if app == 'sugo'
            else sync_linky(
                conn, executors, start, end,
                run_id=run_id, trigger_type=trigger_type,
            )
        )
        failed_guilds = result.get('failed_guilds') or []
        successful_guild_count = int(result.get('successful_guild_count', result.get('guild_count', 0)) or 0)
        status = (
            'failed' if failed_guilds and not successful_guild_count
            else 'partial' if failed_guilds
            else 'success' if result['revenue_count']
            else 'success_no_data'
        )
        first_failure = str((failed_guilds[0] if failed_guilds else {}).get('error') or '')
        error_code = (
            first_failure.split(':', 1)[0]
            if failed_guilds and not successful_guild_count
            else f'{app}_guild_sync_partial' if failed_guilds
            else ''
        )
        error_message = json.dumps(failed_guilds, ensure_ascii=False, separators=(',', ':')) if failed_guilds else ''
        conn.execute(
            """
            UPDATE streamer_external_sync_runs
            SET status = ?, guild_count = ?, profile_count = ?, revenue_count = ?,
                error_code = ?, error_message = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (
                status, result['guild_count'], result['profile_count'], result['revenue_count'],
                error_code, error_message, _now(), run_id,
            ),
        )
        conn.commit()
        return {
            'ok': not failed_guilds,
            'run_id': run_id,
            'app': app,
            'status': status,
            'error_code': error_code,
            'run_scope': run_scope,
            'scope_key': normalized_guild_name,
            **result,
        }
    except Exception as exc:
        detail = str(exc)
        error_code = (
            detail.split(':', 1)[0]
            if detail.startswith(('linky_', 'sugo_'))
            else 'streamer_external_sync_failed'
        )
        conn.rollback()
        record_ingestion_scope(
            conn,
            run_id=run_id,
            app_name=app,
            dataset='external_sync',
            business_date=end.isoformat(),
            source_timezone='UTC' if app == 'linky' else 'Asia/Shanghai',
            trigger_type=trigger_type,
            status='failed',
            error_code=error_code,
            error_message=detail,
        )
        conn.execute(
            "UPDATE streamer_external_sync_runs SET status = 'failed', error_code = ?, error_message = ?, updated_at = ? WHERE run_id = ?",
            (error_code, detail[:2000], _now(), run_id),
        )
        conn.commit()
        return {
            'ok': False,
            'run_id': run_id,
            'app': app,
            'status': 'failed',
            'run_scope': run_scope,
            'scope_key': normalized_guild_name,
            'error_code': error_code,
        }
