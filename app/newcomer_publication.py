from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable
from urllib import error, request


SCHEMA_VERSION = 'mcn_newcomer_daily_v1'
EVENT_SCHEMA_VERSION = 1
PLATFORMS = {'linky', 'timo'}
DATE_CONTRACTS = {
    'linky': 'linky_created_at_utc_date_v1',
    'timo': 'timo_join_time_beijing_date_v1',
}


NEWCOMER_SCHEMA_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS newcomer_daily_publications (
        platform TEXT NOT NULL,
        business_date TEXT NOT NULL,
        revision INTEGER NOT NULL,
        status TEXT NOT NULL,
        publication_type TEXT NOT NULL,
        date_contract TEXT NOT NULL,
        expected_guild_count INTEGER NOT NULL,
        completed_guild_count INTEGER NOT NULL,
        summary_count INTEGER NOT NULL,
        member_count INTEGER NOT NULL,
        unique_member_count INTEGER NOT NULL,
        checksum TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (platform, business_date, revision)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS newcomer_daily_publication_guilds (
        platform TEXT NOT NULL,
        business_date TEXT NOT NULL,
        revision INTEGER NOT NULL,
        guild_executor_key TEXT NOT NULL,
        guild_id TEXT NOT NULL DEFAULT '',
        guild_name TEXT NOT NULL,
        country TEXT NOT NULL DEFAULT '',
        summary_count INTEGER NOT NULL,
        member_count INTEGER NOT NULL,
        unique_member_count INTEGER NOT NULL,
        real_person_count INTEGER NOT NULL,
        checksum TEXT NOT NULL,
        PRIMARY KEY (platform, business_date, revision, guild_executor_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS newcomer_daily_publication_members (
        platform TEXT NOT NULL,
        business_date TEXT NOT NULL,
        revision INTEGER NOT NULL,
        guild_executor_key TEXT NOT NULL,
        guild_id TEXT NOT NULL DEFAULT '',
        guild_name TEXT NOT NULL,
        country TEXT NOT NULL DEFAULT '',
        streamer_id TEXT NOT NULL,
        platform_user_uuid TEXT NOT NULL DEFAULT '',
        nickname TEXT NOT NULL DEFAULT '',
        joined_at TEXT NOT NULL DEFAULT '',
        is_real_person INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (
            platform, business_date, revision, guild_executor_key, streamer_id
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS newcomer_publication_events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        platform TEXT NOT NULL,
        business_date TEXT NOT NULL,
        revision INTEGER NOT NULL,
        checksum TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        delivery_status TEXT NOT NULL DEFAULT 'pending',
        attempt_count INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 8,
        next_attempt_at TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        delivered_at TEXT NOT NULL DEFAULT ''
    )
    """,
)

NEWCOMER_SCHEMA_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_newcomer_publications_latest "
    "ON newcomer_daily_publications(platform, business_date, revision DESC)",
    "CREATE INDEX IF NOT EXISTS idx_newcomer_members_page "
    "ON newcomer_daily_publication_members("
    "platform, business_date, revision, guild_executor_key, streamer_id)",
    "CREATE INDEX IF NOT EXISTS idx_newcomer_events_delivery "
    "ON newcomer_publication_events(delivery_status, next_attempt_at, created_at)",
)


class NewcomerPublicationNotReady(RuntimeError):
    pass


class NewcomerPublicationIntegrityError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _normalize_platform(platform: str) -> str:
    normalized = str(platform or '').strip().lower()
    if normalized not in PLATFORMS:
        raise ValueError('unsupported_newcomer_platform')
    return normalized


def _validate_business_date(value: str) -> str:
    normalized = str(value or '').strip()
    try:
        return datetime.strptime(normalized, '%Y-%m-%d').date().isoformat()
    except ValueError as exc:
        raise ValueError('invalid_business_date') from exc


def _raw_anchor_id(anchor_id: Any) -> str:
    value = str(anchor_id or '').strip()
    return value.split(':', 1)[1].strip() if ':' in value else value


def _utc_joined_at(epoch: Any) -> str:
    try:
        value = int(epoch or 0)
    except (TypeError, ValueError):
        return ''
    if value <= 0:
        return ''
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _configured_guilds(
    conn: sqlite3.Connection, platform: str,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT rowid AS guild_executor_id, guild_name,
                   COALESCE(app_name, 'linky') AS app_name,
                   COALESCE(cms_guild_id, '') AS cms_guild_id,
                   COALESCE(cms_guild_sid, '') AS cms_guild_sid,
                   COALESCE(NULLIF(guild_country, ''), country, '') AS country
            FROM guild_executors
            WHERE LOWER(COALESCE(app_name, 'linky')) = ?
            ORDER BY guild_name
            """,
            (platform,),
        ).fetchall()
    ]


def _executor_key(guild: dict[str, Any]) -> str:
    explicit = str(guild.get('guild_executor_key') or '').strip()
    if explicit:
        return explicit
    platform = str(guild.get('app_name') or 'linky').strip().lower() or 'linky'
    for key in ('cms_guild_sid', 'cms_guild_id', 'guild_executor_id'):
        value = str(guild.get(key) or '').strip()
        if value:
            return f'{platform}:{key}:{value}'
    raise NewcomerPublicationIntegrityError('newcomer_guild_identity_missing')


def _guild_id(guild: dict[str, Any]) -> str:
    return str(guild.get('cms_guild_id') or guild.get('cms_guild_sid') or '').strip()


def _load_linky_members(
    conn: sqlite3.Connection,
    *,
    guild: dict[str, Any],
    executor_key: str,
    business_date: str,
) -> list[dict[str, Any]]:
    run = conn.execute(
        """
        SELECT member_count
        FROM guild_anchor_newcomer_snapshot_runs
        WHERE guild_executor_key=? AND stat_date=?
        """,
        (executor_key, business_date),
    ).fetchone()
    if run is None:
        raise NewcomerPublicationIntegrityError(
            f'linky_newcomer_snapshot_run_missing:{guild["guild_name"]}'
        )
    rows = conn.execute(
        """
        SELECT streamer_sid,anchor_name,source_created_at,is_real_person
        FROM guild_anchor_newcomer_identity_snapshots
        WHERE guild_executor_key=? AND stat_date=?
        ORDER BY streamer_sid
        """,
        (executor_key, business_date),
    ).fetchall()
    members = [
        {
            'streamer_id': str(row['streamer_sid'] or '').strip(),
            'platform_user_uuid': '',
            'nickname': str(row['anchor_name'] or ''),
            'joined_at': _utc_joined_at(row['source_created_at']),
            'is_real_person': int(row['is_real_person'] or 0),
        }
        for row in rows
    ]
    if int(run['member_count'] or 0) != len(members):
        raise NewcomerPublicationIntegrityError(
            f'linky_newcomer_snapshot_count_mismatch:{guild["guild_name"]}'
        )
    return members


def _load_timo_members(
    conn: sqlite3.Connection,
    *,
    guild: dict[str, Any],
    executor_key: str,
    business_date: str,
) -> list[dict[str, Any]]:
    seen_rows = conn.execute(
        """
        SELECT anchor_id,anchor_name,is_real_person,created_at
        FROM guild_anchor_seen
        WHERE guild_executor_key=? AND created_date_bj=?
        ORDER BY anchor_id
        """,
        (executor_key, business_date),
    ).fetchall()
    profile_rows = conn.execute(
        """
        SELECT timo_id,user_uuid,nickname,
               COALESCE(NULLIF(joined_guild_at_bj, ''), registered_at_bj) AS joined_at,
               is_real_person
        FROM timo_external_streamers
        WHERE guild_executor_key=?
        """,
        (executor_key,),
    ).fetchall()
    profiles = {str(row['timo_id'] or '').strip(): row for row in profile_rows}
    members: list[dict[str, Any]] = []
    for seen in seen_rows:
        streamer_id = _raw_anchor_id(seen['anchor_id'])
        profile = profiles.get(streamer_id)
        if profile is None:
            raise NewcomerPublicationIntegrityError(
                f'timo_newcomer_profile_missing:{guild["guild_name"]}:{streamer_id}'
            )
        joined_at = str(profile['joined_at'] or '').strip()
        if joined_at[:10] != business_date:
            raise NewcomerPublicationIntegrityError(
                f'timo_newcomer_join_date_mismatch:{guild["guild_name"]}:{streamer_id}'
            )
        members.append({
            'streamer_id': streamer_id,
            'platform_user_uuid': str(profile['user_uuid'] or '').strip(),
            'nickname': str(profile['nickname'] or seen['anchor_name'] or ''),
            'joined_at': joined_at,
            'is_real_person': int(profile['is_real_person'] or seen['is_real_person'] or 0),
        })
    return members


def _enqueue_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    platform: str,
    business_date: str,
    revision: int,
    checksum: str,
    payload: dict[str, Any],
    created_at: str,
) -> str:
    event_id = (
        f'mcn:newcomer:{platform}:{business_date}:'
        f'{event_type.rsplit(".", 1)[-1]}:{revision}:{checksum[:16]}'
    )
    payload = {**payload, 'eventId': event_id}
    conn.execute(
        """
        INSERT OR IGNORE INTO newcomer_publication_events (
            event_id,event_type,platform,business_date,revision,checksum,
            payload_json,delivery_status,attempt_count,max_attempts,
            next_attempt_at,last_error,created_at,delivered_at
        ) VALUES (?,?,?,?,?,?,?,'pending',0,8,'','',?,'')
        """,
        (
            event_id, event_type, platform, business_date, revision, checksum,
            canonical_json(payload), created_at,
        ),
    )
    return event_id


def _failed_event_if_terminal(
    conn: sqlite3.Connection,
    *,
    platform: str,
    business_date: str,
    guilds: list[dict[str, Any]],
    jobs_by_key: dict[str, sqlite3.Row],
    created_at: str,
) -> dict[str, Any]:
    failures = []
    for guild in guilds:
        executor_key = _executor_key(guild)
        job = jobs_by_key.get(executor_key)
        if job is None:
            continue
        status = str(job['status'] or '')
        attempts = int(job['attempt_count'] or 0)
        max_attempts = int(job['max_attempts'] or 0)
        if status == 'dead' or (status != 'success' and attempts >= max_attempts > 0):
            failures.append({
                'guildId': _guild_id(guild),
                'guildName': str(guild['guild_name']),
                'status': status,
                'attemptCount': attempts,
                'maxAttempts': max_attempts,
                'reason': str(job['error'] or '')[:240],
            })
    if not failures:
        return {'status': 'pending', 'event_id': ''}
    latest = conn.execute(
        """
        SELECT COALESCE(MAX(revision), 0) AS revision
        FROM newcomer_daily_publications
        WHERE platform=? AND business_date=?
        """,
        (platform, business_date),
    ).fetchone()
    revision = int(latest['revision'] or 0)
    checksum = _sha256(failures)
    event_type = 'mcn.newcomers.daily.failed'
    payload = {
        'schemaVersion': EVENT_SCHEMA_VERSION,
        'eventType': event_type,
        'platform': platform.upper(),
        'businessDate': business_date,
        'dateContract': DATE_CONTRACTS[platform],
        'revision': revision,
        'checksum': checksum,
        'status': 'failed',
        'consumable': False,
        'expectedGuildCount': len(guilds),
        'completedGuildCount': sum(
            1
            for guild in guilds
            if (
                (job := jobs_by_key.get(_executor_key(guild))) is not None
                and str(job['status'] or '') == 'success'
            )
        ),
        'failures': failures,
        'publishedAt': created_at,
    }
    event_id = _enqueue_event(
        conn,
        event_type=event_type,
        platform=platform,
        business_date=business_date,
        revision=revision,
        checksum=checksum,
        payload=payload,
        created_at=created_at,
    )
    return {'status': 'failed', 'event_id': event_id}


def reconcile_newcomer_publication(
    conn: sqlite3.Connection,
    *,
    platform: str,
    business_date: str,
    created_at: str = '',
) -> dict[str, Any]:
    normalized_platform = _normalize_platform(platform)
    normalized_date = _validate_business_date(business_date)
    now_iso = str(created_at or '').strip() or _now_iso()
    jobs = conn.execute(
        """
        SELECT guild_executor_key,guild_name,status,attempt_count,max_attempts,error
        FROM guild_anchor_daily_stat_jobs
        WHERE stat_date=?
        """,
        (normalized_date,),
    ).fetchall()
    jobs_by_key = {
        str(row['guild_executor_key']): row
        for row in jobs
        if str(row['guild_executor_key']).startswith(f'{normalized_platform}:')
    }
    if not jobs_by_key:
        raise NewcomerPublicationIntegrityError('newcomer_daily_jobs_missing')
    configured_by_key = {
        _executor_key(guild): guild
        for guild in _configured_guilds(conn, normalized_platform)
    }
    guilds = []
    for executor_key, job in sorted(jobs_by_key.items()):
        guild = configured_by_key.get(executor_key)
        if guild is None:
            guild = {
                'guild_executor_key': executor_key,
                'guild_name': str(job['guild_name'] or ''),
                'app_name': normalized_platform,
                'cms_guild_id': executor_key.rsplit(':', 1)[-1],
                'cms_guild_sid': '',
                'country': '',
            }
        guilds.append(guild)
    expected_keys = {_executor_key(guild) for guild in guilds}
    completed_keys = {
        key for key in expected_keys
        if key in jobs_by_key and str(jobs_by_key[key]['status'] or '') == 'success'
    }
    if completed_keys != expected_keys:
        return _failed_event_if_terminal(
            conn,
            platform=normalized_platform,
            business_date=normalized_date,
            guilds=guilds,
            jobs_by_key=jobs_by_key,
            created_at=now_iso,
        )

    stats_rows = conn.execute(
        """
        SELECT guild_executor_key,joined_count,real_person_count,status,refreshed_at
        FROM guild_anchor_daily_stats
        WHERE stat_date=?
        """,
        (normalized_date,),
    ).fetchall()
    stats_by_key = {str(row['guild_executor_key']): row for row in stats_rows}
    if set(stats_by_key).intersection(expected_keys) != expected_keys:
        raise NewcomerPublicationIntegrityError('newcomer_summary_guild_count_mismatch')

    publication_guilds: list[dict[str, Any]] = []
    publication_members: list[dict[str, Any]] = []
    for guild in guilds:
        executor_key = _executor_key(guild)
        stats = stats_by_key[executor_key]
        if str(stats['status'] or '') != 'success':
            raise NewcomerPublicationIntegrityError(
                f'newcomer_summary_not_success:{guild["guild_name"]}'
            )
        members = (
            _load_linky_members(
                conn,
                guild=guild,
                executor_key=executor_key,
                business_date=normalized_date,
            )
            if normalized_platform == 'linky'
            else _load_timo_members(
                conn,
                guild=guild,
                executor_key=executor_key,
                business_date=normalized_date,
            )
        )
        streamer_ids = [str(member['streamer_id'] or '').strip() for member in members]
        if any(not value for value in streamer_ids):
            raise NewcomerPublicationIntegrityError(
                f'newcomer_streamer_id_missing:{guild["guild_name"]}'
            )
        if len(set(streamer_ids)) != len(streamer_ids):
            raise NewcomerPublicationIntegrityError(
                f'newcomer_streamer_id_duplicate_in_guild:{guild["guild_name"]}'
            )
        summary_count = int(stats['joined_count'] or 0)
        if summary_count != len(members):
            raise NewcomerPublicationIntegrityError(
                f'newcomer_summary_member_count_mismatch:{guild["guild_name"]}'
            )
        guild_row = {
            'guild_executor_key': executor_key,
            'guild_id': _guild_id(guild),
            'guild_name': str(guild['guild_name']),
            'country': str(guild.get('country') or ''),
            'summary_count': summary_count,
            'member_count': len(members),
            'unique_member_count': len(set(streamer_ids)),
            'real_person_count': int(stats['real_person_count'] or 0),
        }
        guild_row['checksum'] = _sha256({
            'guild': guild_row,
            'members': members,
        })
        publication_guilds.append(guild_row)
        publication_members.extend([
            {
                **member,
                'guild_executor_key': executor_key,
                'guild_id': guild_row['guild_id'],
                'guild_name': guild_row['guild_name'],
                'country': guild_row['country'],
            }
            for member in members
        ])

    all_ids = [str(member['streamer_id']) for member in publication_members]
    if len(set(all_ids)) != len(all_ids):
        raise NewcomerPublicationIntegrityError(
            'newcomer_streamer_id_duplicate_across_guilds'
        )
    summary_count = sum(int(row['summary_count']) for row in publication_guilds)
    member_count = len(publication_members)
    unique_member_count = len(set(all_ids))
    if summary_count != member_count or member_count != unique_member_count:
        raise NewcomerPublicationIntegrityError('newcomer_platform_count_gate_failed')

    checksum_rows = sorted(
        (
            {
                'guildId': str(member['guild_id']),
                'guildName': str(member['guild_name']),
                'subjectId': str(member['streamer_id']),
                **(
                    {'sourceUserUuid': str(member['platform_user_uuid'])}
                    if str(member['platform_user_uuid'])
                    else {}
                ),
            }
            for member in publication_members
        ),
        key=lambda row: (row['subjectId'], row['guildId'], row['guildName']),
    )
    checksum = _sha256(checksum_rows)
    latest = conn.execute(
        """
        SELECT revision,checksum,date_contract,expected_guild_count,
               completed_guild_count,summary_count,member_count,unique_member_count
        FROM newcomer_daily_publications
        WHERE platform=? AND business_date=?
        ORDER BY revision DESC LIMIT 1
        """,
        (normalized_platform, normalized_date),
    ).fetchone()
    unchanged = latest is not None and all((
        str(latest['checksum']) == checksum,
        str(latest['date_contract']) == DATE_CONTRACTS[normalized_platform],
        int(latest['expected_guild_count']) == len(guilds),
        int(latest['completed_guild_count']) == len(publication_guilds),
        int(latest['summary_count']) == summary_count,
        int(latest['member_count']) == member_count,
        int(latest['unique_member_count']) == unique_member_count,
    ))
    if unchanged:
        return {
            'status': 'unchanged',
            'revision': int(latest['revision']),
            'checksum': checksum,
            'event_id': '',
        }
    revision = (int(latest['revision']) + 1) if latest is not None else 1
    publication_type = 'revised' if latest is not None else 'complete'
    conn.execute(
        """
        INSERT INTO newcomer_daily_publications (
            platform,business_date,revision,status,publication_type,date_contract,
            expected_guild_count,completed_guild_count,summary_count,member_count,
            unique_member_count,checksum,completed_at,created_at
        ) VALUES (?,?,?,'complete',?,?,?,?,?,?,?,?,?,?)
        """,
        (
            normalized_platform, normalized_date, revision, publication_type,
            DATE_CONTRACTS[normalized_platform], len(guilds), len(publication_guilds),
            summary_count, member_count, unique_member_count, checksum, now_iso, now_iso,
        ),
    )
    conn.executemany(
        """
        INSERT INTO newcomer_daily_publication_guilds (
            platform,business_date,revision,guild_executor_key,guild_id,guild_name,
            country,summary_count,member_count,unique_member_count,real_person_count,
            checksum
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                normalized_platform, normalized_date, revision,
                row['guild_executor_key'], row['guild_id'], row['guild_name'],
                row['country'], row['summary_count'], row['member_count'],
                row['unique_member_count'], row['real_person_count'], row['checksum'],
            )
            for row in publication_guilds
        ],
    )
    conn.executemany(
        """
        INSERT INTO newcomer_daily_publication_members (
            platform,business_date,revision,guild_executor_key,guild_id,guild_name,
            country,streamer_id,platform_user_uuid,nickname,joined_at,is_real_person
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                normalized_platform, normalized_date, revision,
                row['guild_executor_key'], row['guild_id'], row['guild_name'],
                row['country'], row['streamer_id'], row['platform_user_uuid'],
                row['nickname'], row['joined_at'], row['is_real_person'],
            )
            for row in publication_members
        ],
    )
    event_type = (
        'mcn.newcomers.daily.revised'
        if publication_type == 'revised'
        else 'mcn.newcomers.daily.completed'
    )
    event_payload = {
        'schemaVersion': EVENT_SCHEMA_VERSION,
        'eventType': event_type,
        'platform': normalized_platform.upper(),
        'businessDate': normalized_date,
        'dateContract': DATE_CONTRACTS[normalized_platform],
        'revision': revision,
        'checksum': checksum,
        'status': 'complete',
        'consumable': True,
        'expectedGuildCount': len(guilds),
        'completedGuildCount': len(publication_guilds),
        'summaryCount': summary_count,
        'rosterCount': member_count,
        'uniqueIdCount': unique_member_count,
        'completedAt': now_iso,
        'publishedAt': now_iso,
    }
    event_id = _enqueue_event(
        conn,
        event_type=event_type,
        platform=normalized_platform,
        business_date=normalized_date,
        revision=revision,
        checksum=checksum,
        payload=event_payload,
        created_at=now_iso,
    )
    return {
        'status': publication_type,
        'revision': revision,
        'checksum': checksum,
        'event_id': event_id,
        'summary_count': summary_count,
        'member_count': member_count,
    }


def list_newcomer_publication(
    conn: sqlite3.Connection,
    *,
    platform: str,
    business_date: str,
    revision: int = 0,
    limit: int = 500,
    offset: int = 0,
) -> dict[str, Any]:
    normalized_platform = _normalize_platform(platform)
    normalized_date = _validate_business_date(business_date)
    safe_limit = max(1, min(int(limit or 500), 1000))
    safe_offset = max(0, int(offset or 0))
    if int(revision or 0) > 0:
        publication = conn.execute(
            """
            SELECT * FROM newcomer_daily_publications
            WHERE platform=? AND business_date=? AND revision=? AND status='complete'
            """,
            (normalized_platform, normalized_date, int(revision)),
        ).fetchone()
    else:
        publication = conn.execute(
            """
            SELECT * FROM newcomer_daily_publications
            WHERE platform=? AND business_date=? AND status='complete'
            ORDER BY revision DESC LIMIT 1
            """,
            (normalized_platform, normalized_date),
        ).fetchone()
    if publication is None:
        raise NewcomerPublicationNotReady('newcomer_daily_publication_not_ready')
    effective_revision = int(publication['revision'])
    member_rows = conn.execute(
            """
            SELECT guild_id,guild_name,country,streamer_id,platform_user_uuid,
                   nickname,joined_at,is_real_person
            FROM newcomer_daily_publication_members
            WHERE platform=? AND business_date=? AND revision=?
            ORDER BY streamer_id,guild_id,guild_name
            LIMIT ? OFFSET ?
            """,
            (
                normalized_platform, normalized_date, effective_revision,
                safe_limit, safe_offset,
            ),
        ).fetchall()
    members = [
        {
            'guildId': str(row['guild_id']),
            'guildName': str(row['guild_name']),
            'subjectId': str(row['streamer_id']),
            **(
                {'sourceUserUuid': str(row['platform_user_uuid'])}
                if str(row['platform_user_uuid'])
                else {}
            ),
        }
        for row in member_rows
    ]
    data = {
        'schemaVersion': 1,
        'platform': normalized_platform.upper(),
        'businessDate': normalized_date,
        'dateContract': str(publication['date_contract']),
        'status': 'complete',
        'consumable': True,
        'publicationType': str(publication['publication_type']),
        'revision': effective_revision,
        'checksum': str(publication['checksum']),
        'expectedGuildCount': int(publication['expected_guild_count']),
        'completedGuildCount': int(publication['completed_guild_count']),
        'summaryCount': int(publication['summary_count']),
        'rosterCount': int(publication['member_count']),
        'uniqueIdCount': int(publication['unique_member_count']),
        'completedAt': str(publication['completed_at']),
        'total': int(publication['member_count']),
        'limit': safe_limit,
        'offset': safe_offset,
        'rows': members,
    }
    return {'ok': True, 'data': data}


def send_newcomer_event(
    event: dict[str, Any],
    *,
    url: str,
    secret: str,
    attempts: int = 3,
    timeout_seconds: float = 8.0,
    opener: Callable[..., Any] = request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if not str(url or '').strip():
        raise ValueError('newcomer_webhook_url_missing')
    if len(str(secret or '').strip()) < 32:
        raise ValueError('newcomer_webhook_secret_invalid')
    body = canonical_json(event).encode('utf-8')
    last_error = ''
    max_attempts = max(1, min(int(attempts or 1), 5))
    for attempt in range(1, max_attempts + 1):
        timestamp = str(int(time.time()))
        signature = hmac.new(
            secret.encode('utf-8'),
            f'{timestamp}.'.encode('utf-8') + body,
            hashlib.sha256,
        ).hexdigest()
        http_request = request.Request(
            url,
            data=body,
            method='POST',
            headers={
                'Content-Type': 'application/json',
                'X-MCN-Event-Id': str(event.get('eventId') or ''),
                'X-MCN-Timestamp': timestamp,
                'X-MCN-Signature': f'sha256={signature}',
                'User-Agent': 'mcn-newcomer-publication-notifier/1',
            },
        )
        try:
            with opener(http_request, timeout=max(1.0, float(timeout_seconds))) as response:
                response_body = json.loads(response.read(4096).decode('utf-8') or '{}')
                if response.status == 202 and response_body.get('ok') is True:
                    return {
                        'ok': True,
                        'event_id': str(event.get('eventId') or ''),
                        'duplicate': bool(response_body.get('duplicate')),
                        'attempts': attempt,
                    }
                last_error = f'unexpected_response:{response.status}'
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
        if attempt < max_attempts:
            sleep(float(2 ** (attempt - 1)))
    raise RuntimeError(f'newcomer_notification_failed:{last_error or "unknown"}')


def dispatch_pending_newcomer_events(
    conn: sqlite3.Connection,
    *,
    url: str,
    secret: str,
    limit: int = 20,
    now_iso: str = '',
    sender: Callable[..., dict[str, Any]] = send_newcomer_event,
) -> dict[str, Any]:
    current = str(now_iso or '').strip() or _now_iso()
    rows = conn.execute(
        """
        SELECT * FROM newcomer_publication_events
        WHERE delivery_status IN ('pending','retry_waiting')
          AND attempt_count < max_attempts
          AND (next_attempt_at='' OR next_attempt_at<=?)
        ORDER BY created_at,event_id
        LIMIT ?
        """,
        (current, max(1, min(int(limit or 20), 100))),
    ).fetchall()
    delivered = 0
    failed = 0
    for row in rows:
        event = json.loads(str(row['payload_json'] or '{}'))
        next_attempt_count = int(row['attempt_count'] or 0) + 1
        try:
            sender(event, url=url, secret=secret, attempts=3)
            conn.execute(
                """
                UPDATE newcomer_publication_events
                SET delivery_status='delivered',attempt_count=?,next_attempt_at='',
                    last_error='',delivered_at=?
                WHERE event_id=? AND delivery_status IN ('pending','retry_waiting')
                """,
                (next_attempt_count, current, row['event_id']),
            )
            delivered += 1
        except Exception as exc:
            terminal = next_attempt_count >= int(row['max_attempts'] or 8)
            delay_seconds = min(3600, 60 * (2 ** min(next_attempt_count - 1, 5)))
            parsed_current = datetime.fromisoformat(current.replace('Z', '+00:00'))
            if parsed_current.tzinfo is None:
                parsed_current = parsed_current.replace(tzinfo=timezone.utc)
            next_attempt = datetime.fromtimestamp(
                parsed_current.timestamp() + delay_seconds,
                tz=timezone.utc,
            ).isoformat()
            conn.execute(
                """
                UPDATE newcomer_publication_events
                SET delivery_status=?,attempt_count=?,next_attempt_at=?,last_error=?
                WHERE event_id=? AND delivery_status IN ('pending','retry_waiting')
                """,
                (
                    'dead' if terminal else 'retry_waiting',
                    next_attempt_count,
                    '' if terminal else next_attempt,
                    str(exc)[:240],
                    row['event_id'],
                ),
            )
            failed += 1
    conn.commit()
    return {
        'ok': failed == 0,
        'processed_count': len(rows),
        'delivered_count': delivered,
        'failed_count': failed,
    }


def load_secret(path: str) -> str:
    secret = Path(path).read_text(encoding='utf-8').strip()
    if len(secret) < 32:
        raise ValueError('newcomer_webhook_secret_invalid')
    return secret
