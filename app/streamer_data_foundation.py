from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

from app.streamer_app_fan import ensure_streamer_app_fan_table


STREAMER_UID_NAMESPACE = uuid.UUID('3e15e8bd-286a-4a1b-b0f0-ef74176a71e9')
LINKY_FOUNDATION_EXCLUDED_GUILDS = frozenset({'Nova'})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def _payload_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def ensure_streamer_foundation_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS streamer_identities (
            streamer_uid TEXT PRIMARY KEY,
            app_name TEXT NOT NULL,
            primary_id_type TEXT NOT NULL,
            primary_id_value TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(app_name, primary_id_type, primary_id_value)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_identities_app_seen
            ON streamer_identities(app_name, last_seen_at DESC);

        CREATE TABLE IF NOT EXISTS streamer_identity_aliases (
            app_name TEXT NOT NULL,
            alias_type TEXT NOT NULL,
            alias_value TEXT NOT NULL,
            streamer_uid TEXT NOT NULL,
            source_name TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(app_name, alias_type, alias_value)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_identity_alias_uid
            ON streamer_identity_aliases(streamer_uid, app_name);

        CREATE TABLE IF NOT EXISTS streamer_guild_memberships (
            membership_id TEXT PRIMARY KEY,
            streamer_uid TEXT NOT NULL,
            app_name TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            valid_from TEXT NOT NULL DEFAULT '',
            valid_to TEXT NOT NULL DEFAULT '',
            source_timezone TEXT NOT NULL DEFAULT '',
            is_current INTEGER NOT NULL DEFAULT 1,
            source_name TEXT NOT NULL DEFAULT '',
            source_run_id TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_streamer_membership_current
            ON streamer_guild_memberships(streamer_uid, app_name, guild_executor_key)
            WHERE is_current = 1;
        CREATE INDEX IF NOT EXISTS idx_streamer_membership_scope
            ON streamer_guild_memberships(app_name, guild_executor_key, is_current, last_seen_at DESC);

        CREATE TABLE IF NOT EXISTS streamer_profile_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            streamer_uid TEXT NOT NULL,
            membership_id TEXT NOT NULL,
            app_name TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            nickname TEXT NOT NULL DEFAULT '',
            registered_at TEXT NOT NULL DEFAULT '',
            registered_timezone TEXT NOT NULL DEFAULT '',
            last_active_at TEXT NOT NULL DEFAULT '',
            last_active_timezone TEXT NOT NULL DEFAULT '',
            is_real_person_status TEXT NOT NULL DEFAULT 'unknown',
            status TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            profile_hash TEXT NOT NULL,
            official_fields_json TEXT NOT NULL DEFAULT '{}',
            source_name TEXT NOT NULL DEFAULT '',
            source_run_id TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(streamer_uid, membership_id, profile_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_profile_snapshot_latest
            ON streamer_profile_snapshots(streamer_uid, observed_at DESC);

        CREATE TABLE IF NOT EXISTS streamer_daily_fact_revisions (
            revision_id TEXT PRIMARY KEY,
            app_name TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            business_date TEXT NOT NULL,
            source_timezone TEXT NOT NULL,
            streamer_uid TEXT NOT NULL,
            native_streamer_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            metric_hash TEXT NOT NULL,
            total_income REAL NOT NULL DEFAULT 0,
            chat_income REAL NOT NULL DEFAULT 0,
            voice_room_income REAL NOT NULL DEFAULT 0,
            video_income REAL NOT NULL DEFAULT 0,
            gift_income REAL NOT NULL DEFAULT 0,
            other_income REAL NOT NULL DEFAULT 0,
            agency_income REAL NOT NULL DEFAULT 0,
            active_days INTEGER NOT NULL DEFAULT 0,
            provisional INTEGER NOT NULL DEFAULT 0,
            official_fields_json TEXT NOT NULL DEFAULT '{}',
            source_name TEXT NOT NULL DEFAULT '',
            source_run_id TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(app_name, guild_executor_key, business_date, streamer_uid, revision)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_streamer_daily_fact_current
            ON streamer_daily_fact_revisions(app_name, guild_executor_key, business_date, streamer_uid)
            WHERE is_current = 1;
        CREATE INDEX IF NOT EXISTS idx_streamer_daily_fact_scope
            ON streamer_daily_fact_revisions(app_name, business_date, guild_executor_key, is_current);

        CREATE TABLE IF NOT EXISTS streamer_business_identities (
            business_system TEXT NOT NULL,
            business_id_type TEXT NOT NULL,
            business_id_value TEXT NOT NULL,
            streamer_uid TEXT NOT NULL,
            source_record_id TEXT NOT NULL DEFAULT '',
            verification_status TEXT NOT NULL DEFAULT 'verified',
            valid_from TEXT NOT NULL DEFAULT '',
            valid_to TEXT NOT NULL DEFAULT '',
            source_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(business_system, business_id_type, business_id_value)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_business_identity_uid
            ON streamer_business_identities(streamer_uid, business_system);

        CREATE TABLE IF NOT EXISTS streamer_raw_ingestion_objects (
            raw_object_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            app_name TEXT NOT NULL,
            dataset TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL DEFAULT '',
            guild_name TEXT NOT NULL DEFAULT '',
            business_date TEXT NOT NULL DEFAULT '',
            source_timezone TEXT NOT NULL DEFAULT '',
            page_number INTEGER NOT NULL DEFAULT 0,
            request_params_json TEXT NOT NULL DEFAULT '{}',
            schema_hash TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            media_type TEXT NOT NULL,
            content_encoding TEXT NOT NULL DEFAULT '',
            artifact_path TEXT NOT NULL,
            payload_size INTEGER NOT NULL DEFAULT 0,
            row_count INTEGER NOT NULL DEFAULT 0,
            retrieved_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, app_name, dataset, guild_executor_key, business_date, page_number, content_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_raw_scope
            ON streamer_raw_ingestion_objects(app_name, dataset, business_date, guild_executor_key, retrieved_at DESC);

        CREATE TABLE IF NOT EXISTS streamer_ingestion_run_scopes (
            scope_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            app_name TEXT NOT NULL,
            dataset TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL DEFAULT '',
            guild_name TEXT NOT NULL DEFAULT '',
            business_date TEXT NOT NULL DEFAULT '',
            source_timezone TEXT NOT NULL DEFAULT '',
            trigger_type TEXT NOT NULL DEFAULT 'scheduled',
            attempt INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            expected_rows INTEGER NOT NULL DEFAULT 0,
            scanned_rows INTEGER NOT NULL DEFAULT 0,
            saved_rows INTEGER NOT NULL DEFAULT 0,
            official_income REAL,
            detail_income REAL,
            reconciliation_delta REAL,
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, dataset, guild_executor_key, business_date, attempt)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_ingestion_scope_watermark
            ON streamer_ingestion_run_scopes(app_name, dataset, guild_executor_key, business_date DESC, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_streamer_ingestion_scope_status
            ON streamer_ingestion_run_scopes(status, updated_at DESC);
        """
    )
    ensure_streamer_app_fan_table(conn)


def ensure_streamer_foundation_views(conn: sqlite3.Connection) -> None:
    """Expose canonical current-state reads without duplicating stable history."""
    required = {
        'streamer_external_profiles',
        'streamer_external_revenue_daily',
        'timo_external_revenue_daily',
    }
    if not all(_table_exists(conn, table) for table in required):
        return
    conn.executescript(
        """
        CREATE VIEW IF NOT EXISTS streamer_profiles_current_v1 AS
        WITH ranked AS (
            SELECT
                snapshot.*,
                ROW_NUMBER() OVER (
                    PARTITION BY snapshot.streamer_uid, snapshot.membership_id
                    ORDER BY snapshot.observed_at DESC, snapshot.created_at DESC
                ) AS profile_rank
            FROM streamer_profile_snapshots AS snapshot
        )
        SELECT
            identity.streamer_uid,
            identity.app_name,
            identity.primary_id_type,
            identity.primary_id_value,
            membership.membership_id,
            membership.guild_executor_key,
            membership.guild_name,
            membership.country,
            membership.valid_from,
            membership.valid_to,
            membership.source_timezone,
            membership.is_current,
            ranked.nickname,
            ranked.registered_at,
            ranked.registered_timezone,
            ranked.last_active_at,
            ranked.last_active_timezone,
            ranked.is_real_person_status,
            ranked.status,
            ranked.role,
            ranked.official_fields_json,
            ranked.source_name,
            ranked.source_run_id,
            ranked.observed_at
        FROM streamer_identities AS identity
        JOIN streamer_guild_memberships AS membership
          ON membership.streamer_uid = identity.streamer_uid
        LEFT JOIN ranked
          ON ranked.streamer_uid = identity.streamer_uid
         AND ranked.membership_id = membership.membership_id
         AND ranked.profile_rank = 1;

        CREATE VIEW IF NOT EXISTS streamer_daily_facts_current_v1 AS
        SELECT
            revision.app_name,
            revision.guild_executor_key,
            revision.guild_name,
            revision.country,
            revision.business_date,
            revision.source_timezone,
            revision.streamer_uid,
            revision.native_streamer_id,
            revision.total_income,
            revision.chat_income,
            revision.voice_room_income,
            revision.video_income,
            revision.gift_income,
            revision.other_income,
            revision.agency_income,
            revision.active_days,
            revision.provisional,
            revision.official_fields_json,
            revision.source_name,
            revision.source_run_id,
            revision.observed_at,
            revision.revision,
            1 AS is_versioned
        FROM streamer_daily_fact_revisions AS revision
        WHERE revision.is_current = 1

        UNION ALL

        SELECT
            revenue.app_name,
            revenue.guild_executor_key,
            revenue.guild_name,
            revenue.country,
            revenue.stat_date_bj,
            CASE WHEN revenue.app_name = 'linky' THEN 'UTC' ELSE 'Asia/Shanghai' END,
            alias.streamer_uid,
            revenue.streamer_id,
            revenue.total_income,
            revenue.chat_income,
            revenue.voice_room_income,
            revenue.video_income,
            revenue.gift_income,
            revenue.other_income,
            revenue.agency_income,
            revenue.active_days,
            0,
            revenue.source_payload,
            revenue.source_name,
            '',
            revenue.updated_at,
            0,
            0
        FROM streamer_external_revenue_daily AS revenue
        LEFT JOIN streamer_external_profiles AS profile
          ON profile.app_name = revenue.app_name
         AND profile.guild_executor_key = revenue.guild_executor_key
         AND profile.streamer_id = revenue.streamer_id
        JOIN streamer_identity_aliases AS alias
          ON alias.app_name = revenue.app_name
         AND alias.alias_type = CASE WHEN revenue.app_name = 'linky' THEN 'sid' ELSE 'anchor_id' END
         AND alias.alias_value = CASE
              WHEN revenue.app_name = 'linky'
              THEN COALESCE(NULLIF(profile.platform_character_id, ''), revenue.streamer_id)
              ELSE revenue.streamer_id
         END
        WHERE revenue.app_name IN ('linky', 'sugo')
          AND NOT EXISTS (
              SELECT 1
              FROM streamer_daily_fact_revisions AS current_revision
              WHERE current_revision.app_name = revenue.app_name
                AND current_revision.guild_executor_key = revenue.guild_executor_key
                AND current_revision.business_date = revenue.stat_date_bj
                AND current_revision.streamer_uid = alias.streamer_uid
                AND current_revision.is_current = 1
          )

        UNION ALL

        SELECT
            'timo',
            revenue.guild_executor_key,
            revenue.guild_name,
            revenue.country,
            revenue.stat_date_bj,
            'Asia/Shanghai',
            alias.streamer_uid,
            revenue.timo_id,
            revenue.total_income,
            0,
            0,
            revenue.call_income,
            0,
            revenue.total_income,
            0,
            CASE WHEN revenue.total_income > 0 THEN 1 ELSE 0 END,
            revenue.provisional,
            revenue.source_payload,
            'timo_external_revenue_daily',
            '',
            revenue.updated_at,
            0,
            0
        FROM timo_external_revenue_daily AS revenue
        JOIN streamer_identity_aliases AS alias
          ON alias.app_name = 'timo'
         AND alias.alias_type = 'timo_id'
         AND alias.alias_value = revenue.timo_id
        WHERE NOT EXISTS (
              SELECT 1
              FROM streamer_daily_fact_revisions AS current_revision
              WHERE current_revision.app_name = 'timo'
                AND current_revision.guild_executor_key = revenue.guild_executor_key
                AND current_revision.business_date = revenue.stat_date_bj
                AND current_revision.streamer_uid = alias.streamer_uid
                AND current_revision.is_current = 1
        );
        """
    )


def _canonical_identity(
    app_name: str,
    *,
    streamer_id: str,
    platform_user_id: str = '',
    platform_character_id: str = '',
    user_uuid: str = '',
    official_fields: Optional[Dict[str, Any]] = None,
) -> tuple[str, str, list[tuple[str, str]]]:
    app = str(app_name or '').strip().lower()
    fields = official_fields or {}
    directory_fields = fields.get('anchor_directory') if isinstance(fields.get('anchor_directory'), dict) else {}
    aliases: list[tuple[str, str]] = []
    if app == 'linky':
        sid = str(platform_character_id or fields.get('sid') or directory_fields.get('sid') or streamer_id or '').strip()
        primary_type, primary_value = 'sid', sid
        aliases.extend([
            ('sid', sid),
            ('user_id', str(platform_user_id or fields.get('user_id') or directory_fields.get('user_id') or '').strip()),
            ('character_id', str(fields.get('character_id') or directory_fields.get('character_id') or '').strip()),
            ('legacy_streamer_id', str(streamer_id or '').strip() if str(streamer_id or '').strip() != sid else ''),
        ])
    elif app == 'timo':
        primary_type, primary_value = 'timo_id', str(streamer_id or '').strip()
        aliases.extend([
            ('timo_id', primary_value),
            ('user_uuid', str(user_uuid or fields.get('userUuid') or fields.get('user_uuid') or '').strip()),
        ])
    else:
        primary_type, primary_value = 'anchor_id', str(streamer_id or '').strip()
        aliases.append(('anchor_id', primary_value))
    return primary_type, primary_value, [(kind, value) for kind, value in aliases if value]


def streamer_uid_for_alias(
    conn: sqlite3.Connection,
    *,
    app_name: str,
    alias_type: str,
    alias_value: str,
) -> str:
    row = conn.execute(
        "SELECT streamer_uid FROM streamer_identity_aliases WHERE app_name=? AND alias_type=? AND alias_value=?",
        (str(app_name).lower(), str(alias_type), str(alias_value)),
    ).fetchone()
    return str(row[0]) if row else ''


def upsert_streamer_profile(
    conn: sqlite3.Connection,
    *,
    app_name: str,
    guild_executor_key: str,
    guild_name: str,
    country: str,
    streamer_id: str,
    platform_user_id: str = '',
    platform_character_id: str = '',
    user_uuid: str = '',
    nickname: str = '',
    registered_at: str = '',
    registered_timezone: str = '',
    last_active_at: str = '',
    last_active_timezone: str = '',
    is_real_person_status: str = 'unknown',
    status: str = '',
    role: str = '',
    official_fields: Optional[Dict[str, Any]] = None,
    source_name: str = '',
    source_run_id: str = '',
    observed_at: str = '',
) -> tuple[str, str]:
    now = observed_at or _now()
    app = str(app_name or '').strip().lower()
    official = official_fields or {}
    primary_type, primary_value, aliases = _canonical_identity(
        app,
        streamer_id=str(streamer_id or '').strip(),
        platform_user_id=str(platform_user_id or '').strip(),
        platform_character_id=str(platform_character_id or '').strip(),
        user_uuid=str(user_uuid or '').strip(),
        official_fields=official,
    )
    if not primary_value:
        raise ValueError('streamer_identity_missing_primary_id')
    alias_uid = ''
    if aliases:
        params: list[str] = []
        predicates: list[str] = []
        for alias_type, alias_value in aliases:
            params.extend((app, alias_type, alias_value))
            predicates.append('(app_name=? AND alias_type=? AND alias_value=?)')
        row = conn.execute(
            f"SELECT streamer_uid FROM streamer_identity_aliases WHERE {' OR '.join(predicates)} "
            "ORDER BY CASE alias_type WHEN ? THEN 0 ELSE 1 END LIMIT 1",
            (*params, primary_type),
        ).fetchone()
        alias_uid = str(row[0]) if row else ''
    identity_row = conn.execute(
        "SELECT streamer_uid FROM streamer_identities WHERE app_name=? AND primary_id_type=? AND primary_id_value=?",
        (app, primary_type, primary_value),
    ).fetchone()
    # The platform primary identifier is authoritative.  Secondary aliases may
    # have been attached to an older legacy row, but must never replace Linky's
    # SID (or the equivalent primary id on the other platforms).
    streamer_uid = (str(identity_row[0]) if identity_row else '') or alias_uid
    if not streamer_uid:
        streamer_uid = str(uuid.uuid5(STREAMER_UID_NAMESPACE, f'{app}:{primary_type}:{primary_value}'))
        conn.execute(
            "INSERT INTO streamer_identities(streamer_uid,app_name,primary_id_type,primary_id_value,first_seen_at,last_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (streamer_uid, app, primary_type, primary_value, now, now, now, now),
        )
    else:
        conn.execute(
            "UPDATE streamer_identities SET last_seen_at=?,updated_at=? WHERE streamer_uid=?",
            (now, now, streamer_uid),
        )
    for alias_type, alias_value in aliases:
        conn.execute(
            """
            INSERT INTO streamer_identity_aliases(
                app_name,alias_type,alias_value,streamer_uid,source_name,
                first_seen_at,last_seen_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(app_name,alias_type,alias_value) DO UPDATE SET
                streamer_uid=excluded.streamer_uid,
                source_name=excluded.source_name,
                last_seen_at=excluded.last_seen_at,
                updated_at=excluded.updated_at
            """,
            (app, alias_type, alias_value, streamer_uid, source_name, now, now, now, now),
        )
    current_membership = conn.execute(
        "SELECT membership_id,valid_from FROM streamer_guild_memberships WHERE streamer_uid=? AND app_name=? AND guild_executor_key=? AND is_current=1",
        (streamer_uid, app, guild_executor_key),
    ).fetchone()
    valid_from = str(registered_at or '')[:10]
    if current_membership:
        membership_id = str(current_membership[0])
        prior_valid_from = str(current_membership[1] or '')
        merged_valid_from = min(value for value in (prior_valid_from, valid_from) if value) if prior_valid_from or valid_from else ''
        conn.execute(
            """
            UPDATE streamer_guild_memberships
            SET guild_name=?,country=?,valid_from=?,source_timezone=?,source_name=?,source_run_id=?,
                last_seen_at=?,updated_at=?
            WHERE membership_id=?
            """,
            (
                guild_name, country, merged_valid_from, registered_timezone, source_name,
                source_run_id, now, now, membership_id,
            ),
        )
    else:
        membership_id = str(uuid.uuid5(STREAMER_UID_NAMESPACE, f'membership:{app}:{streamer_uid}:{guild_executor_key}'))
        conn.execute(
            """
            INSERT INTO streamer_guild_memberships(
                membership_id,streamer_uid,app_name,guild_executor_key,guild_name,country,
                valid_from,source_timezone,is_current,source_name,source_run_id,
                first_seen_at,last_seen_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)
            """,
            (
                membership_id, streamer_uid, app, guild_executor_key, guild_name, country,
                valid_from, registered_timezone, source_name, source_run_id,
                now, now, now, now,
            ),
        )
    normalized_real_person = str(is_real_person_status or 'unknown').strip().lower()
    if normalized_real_person not in {'verified', 'unverified', 'unknown'}:
        normalized_real_person = 'unknown'
    snapshot_payload = {
        'nickname': nickname,
        'registered_at': registered_at,
        'registered_timezone': registered_timezone,
        'last_active_at': last_active_at,
        'last_active_timezone': last_active_timezone,
        'is_real_person_status': normalized_real_person,
        'status': status,
        'role': role,
        'official_fields': official,
    }
    profile_hash = _sha256_bytes(_json_text(snapshot_payload).encode('utf-8'))
    snapshot_id = str(uuid.uuid5(STREAMER_UID_NAMESPACE, f'profile:{streamer_uid}:{membership_id}:{profile_hash}'))
    conn.execute(
        """
        INSERT OR IGNORE INTO streamer_profile_snapshots(
            snapshot_id,streamer_uid,membership_id,app_name,guild_executor_key,nickname,
            registered_at,registered_timezone,last_active_at,last_active_timezone,
            is_real_person_status,status,role,profile_hash,official_fields_json,
            source_name,source_run_id,observed_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            snapshot_id, streamer_uid, membership_id, app, guild_executor_key, nickname,
            registered_at, registered_timezone, last_active_at, last_active_timezone,
            normalized_real_person, status, role, profile_hash, _json_text(official),
            source_name, source_run_id, now, now,
        ),
    )
    return streamer_uid, membership_id


def record_streamer_daily_revision(
    conn: sqlite3.Connection,
    *,
    app_name: str,
    guild_executor_key: str,
    guild_name: str,
    country: str,
    business_date: str,
    source_timezone: str,
    native_streamer_id: str,
    total_income: float = 0,
    chat_income: float = 0,
    voice_room_income: float = 0,
    video_income: float = 0,
    gift_income: float = 0,
    other_income: float = 0,
    agency_income: float = 0,
    active_days: int = 0,
    provisional: int = 0,
    official_fields: Optional[Dict[str, Any]] = None,
    source_name: str = '',
    source_run_id: str = '',
    observed_at: str = '',
    platform_user_id: str = '',
    platform_character_id: str = '',
    user_uuid: str = '',
) -> Dict[str, Any]:
    now = observed_at or _now()
    primary_type, primary_value, aliases = _canonical_identity(
        app_name,
        streamer_id=native_streamer_id,
        platform_user_id=platform_user_id,
        platform_character_id=platform_character_id,
        user_uuid=user_uuid,
        official_fields=official_fields,
    )
    streamer_uid = ''
    for alias_type, alias_value in [(primary_type, primary_value), *aliases]:
        streamer_uid = streamer_uid_for_alias(
            conn,
            app_name=app_name,
            alias_type=alias_type,
            alias_value=alias_value,
        )
        if streamer_uid:
            break
    if not streamer_uid:
        streamer_uid, _ = upsert_streamer_profile(
            conn,
            app_name=app_name,
            guild_executor_key=guild_executor_key,
            guild_name=guild_name,
            country=country,
            streamer_id=native_streamer_id,
            platform_user_id=platform_user_id,
            platform_character_id=platform_character_id,
            user_uuid=user_uuid,
            official_fields=official_fields,
            source_name=source_name,
            source_run_id=source_run_id,
            observed_at=now,
            registered_timezone=source_timezone,
            last_active_timezone=source_timezone,
        )
    metrics = {
        'total_income': float(total_income or 0),
        'chat_income': float(chat_income or 0),
        'voice_room_income': float(voice_room_income or 0),
        'video_income': float(video_income or 0),
        'gift_income': float(gift_income or 0),
        'other_income': float(other_income or 0),
        'agency_income': float(agency_income or 0),
        'active_days': int(active_days or 0),
        'provisional': int(provisional or 0),
        'official_fields': official_fields or {},
    }
    metric_hash = _sha256_bytes(_json_text(metrics).encode('utf-8'))
    current = conn.execute(
        """
        SELECT revision_id,revision,metric_hash
        FROM streamer_daily_fact_revisions
        WHERE app_name=? AND guild_executor_key=? AND business_date=? AND streamer_uid=? AND is_current=1
        """,
        (str(app_name).lower(), guild_executor_key, business_date, streamer_uid),
    ).fetchone()
    if current and str(current[2]) == metric_hash:
        conn.execute(
            "UPDATE streamer_daily_fact_revisions SET observed_at=?,source_run_id=? WHERE revision_id=?",
            (now, source_run_id, str(current[0])),
        )
        return {'streamer_uid': streamer_uid, 'revision': int(current[1]), 'changed': False}
    revision = (int(current[1]) + 1) if current else 1
    if current:
        conn.execute(
            "UPDATE streamer_daily_fact_revisions SET is_current=0 WHERE revision_id=?",
            (str(current[0]),),
        )
    revision_id = str(uuid.uuid5(
        STREAMER_UID_NAMESPACE,
        f'daily:{str(app_name).lower()}:{guild_executor_key}:{business_date}:{streamer_uid}:{revision}:{metric_hash}',
    ))
    conn.execute(
        """
        INSERT INTO streamer_daily_fact_revisions(
            revision_id,app_name,guild_executor_key,guild_name,country,business_date,
            source_timezone,streamer_uid,native_streamer_id,revision,is_current,metric_hash,
            total_income,chat_income,voice_room_income,video_income,gift_income,other_income,
            agency_income,active_days,provisional,official_fields_json,source_name,
            source_run_id,observed_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            revision_id, str(app_name).lower(), guild_executor_key, guild_name, country,
            business_date, source_timezone, streamer_uid, native_streamer_id, revision,
            metric_hash, metrics['total_income'], metrics['chat_income'], metrics['voice_room_income'],
            metrics['video_income'], metrics['gift_income'], metrics['other_income'],
            metrics['agency_income'], metrics['active_days'], metrics['provisional'],
            _json_text(metrics['official_fields']), source_name, source_run_id, now, now,
        ),
    )
    return {'streamer_uid': streamer_uid, 'revision': revision, 'changed': True}


def record_ingestion_scope(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    app_name: str,
    dataset: str,
    guild_executor_key: str = '',
    guild_name: str = '',
    business_date: str = '',
    source_timezone: str = '',
    trigger_type: str = 'scheduled',
    attempt: int = 1,
    status: str,
    expected_rows: int = 0,
    scanned_rows: int = 0,
    saved_rows: int = 0,
    official_income: Optional[float] = None,
    detail_income: Optional[float] = None,
    reconciliation_delta: Optional[float] = None,
    error_code: str = '',
    error_message: str = '',
    started_at: str = '',
    completed_at: str = '',
) -> str:
    ensure_streamer_foundation_tables(conn)
    now = _now()
    scope_id = str(uuid.uuid5(
        STREAMER_UID_NAMESPACE,
        f'scope:{run_id}:{dataset}:{guild_executor_key}:{business_date}:{attempt}',
    ))
    conn.execute(
        """
        INSERT INTO streamer_ingestion_run_scopes(
            scope_id,run_id,app_name,dataset,guild_executor_key,guild_name,business_date,
            source_timezone,trigger_type,attempt,status,expected_rows,scanned_rows,saved_rows,
            official_income,detail_income,reconciliation_delta,error_code,error_message,
            started_at,completed_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id,dataset,guild_executor_key,business_date,attempt) DO UPDATE SET
            status=excluded.status,expected_rows=excluded.expected_rows,
            scanned_rows=excluded.scanned_rows,saved_rows=excluded.saved_rows,
            official_income=excluded.official_income,detail_income=excluded.detail_income,
            reconciliation_delta=excluded.reconciliation_delta,error_code=excluded.error_code,
            error_message=excluded.error_message,completed_at=excluded.completed_at,
            updated_at=excluded.updated_at
        """,
        (
            scope_id, run_id, str(app_name).lower(), dataset, guild_executor_key, guild_name,
            business_date, source_timezone, trigger_type, int(attempt or 1), status,
            int(expected_rows or 0), int(scanned_rows or 0), int(saved_rows or 0),
            official_income, detail_income, reconciliation_delta, error_code,
            str(error_message or '')[:500], started_at or now, completed_at or (now if status != 'running' else ''), now,
        ),
    )
    return scope_id


def _archive_root(conn: sqlite3.Connection) -> Path:
    configured = str(os.getenv('STREAMER_RAW_ARCHIVE_DIR') or '').strip()
    if configured:
        return Path(configured)
    row = conn.execute('PRAGMA database_list').fetchone()
    db_path = Path(str(row[2] or '')) if row and len(row) > 2 and str(row[2] or '') else Path.cwd() / 'data' / 'automation.db'
    return db_path.parent / 'streamer_raw_archive'


def _schema_hash(payload: Any) -> str:
    keys: set[str] = set()
    if isinstance(payload, dict):
        keys.update(str(key) for key in payload)
        for candidate in ('items', 'data', 'records', 'list'):
            rows = payload.get(candidate)
            if isinstance(rows, list):
                for row in rows[:10]:
                    if isinstance(row, dict):
                        keys.update(f'{candidate}.{key}' for key in row)
            elif isinstance(rows, dict):
                keys.update(f'{candidate}.{key}' for key in rows)
    return _sha256_bytes('\n'.join(sorted(keys)).encode('utf-8'))


def archive_raw_json(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    app_name: str,
    dataset: str,
    endpoint: str,
    payload: Any,
    guild_executor_key: str = '',
    guild_name: str = '',
    business_date: str = '',
    source_timezone: str = '',
    page_number: int = 0,
    request_params: Optional[Dict[str, Any]] = None,
    row_count: int = 0,
    retrieved_at: str = '',
) -> Dict[str, str]:
    ensure_streamer_foundation_tables(conn)
    raw = _json_text(payload).encode('utf-8')
    content_hash = _sha256_bytes(raw)
    schema_hash = _schema_hash(payload)
    date_part = business_date or (retrieved_at or _now())[:10]
    root = _archive_root(conn)
    relative = Path(str(app_name).lower()) / dataset / date_part / f'{content_hash}.json.gz'
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temp = target.with_suffix(target.suffix + '.tmp')
        with gzip.open(temp, 'wb') as handle:
            handle.write(raw)
        temp.replace(target)
    now = retrieved_at or _now()
    object_id = str(uuid.uuid5(
        STREAMER_UID_NAMESPACE,
        f'raw:{run_id}:{str(app_name).lower()}:{dataset}:{guild_executor_key}:{business_date}:{page_number}:{content_hash}',
    ))
    conn.execute(
        """
        INSERT OR IGNORE INTO streamer_raw_ingestion_objects(
            raw_object_id,run_id,app_name,dataset,endpoint,guild_executor_key,guild_name,
            business_date,source_timezone,page_number,request_params_json,schema_hash,
            content_hash,media_type,content_encoding,artifact_path,payload_size,row_count,
            retrieved_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'application/json','gzip',?,?,?,?,?)
        """,
        (
            object_id, run_id, str(app_name).lower(), dataset, endpoint, guild_executor_key,
            guild_name, business_date, source_timezone, int(page_number or 0),
            _json_text(request_params or {}), schema_hash, content_hash, str(target),
            len(raw), int(row_count or 0), now, now,
        ),
    )
    return {'raw_object_id': object_id, 'content_hash': content_hash, 'artifact_path': str(target)}


def archive_raw_bytes(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    app_name: str,
    dataset: str,
    endpoint: str,
    content: bytes,
    media_type: str,
    extension: str,
    guild_executor_key: str = '',
    guild_name: str = '',
    business_date: str = '',
    source_timezone: str = '',
    request_params: Optional[Dict[str, Any]] = None,
    row_count: int = 0,
    retrieved_at: str = '',
) -> Dict[str, str]:
    ensure_streamer_foundation_tables(conn)
    content_hash = _sha256_bytes(content)
    date_part = business_date or (retrieved_at or _now())[:10]
    suffix = extension if extension.startswith('.') else f'.{extension}'
    root = _archive_root(conn)
    relative = Path(str(app_name).lower()) / dataset / date_part / f'{content_hash}{suffix}'
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temp = target.with_suffix(target.suffix + '.tmp')
        temp.write_bytes(content)
        temp.replace(target)
    now = retrieved_at or _now()
    object_id = str(uuid.uuid5(
        STREAMER_UID_NAMESPACE,
        f'raw:{run_id}:{str(app_name).lower()}:{dataset}:{guild_executor_key}:{business_date}:0:{content_hash}',
    ))
    conn.execute(
        """
        INSERT OR IGNORE INTO streamer_raw_ingestion_objects(
            raw_object_id,run_id,app_name,dataset,endpoint,guild_executor_key,guild_name,
            business_date,source_timezone,page_number,request_params_json,schema_hash,
            content_hash,media_type,content_encoding,artifact_path,payload_size,row_count,
            retrieved_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,0,?,'',?,?, '',?,?,?,?,?)
        """,
        (
            object_id, run_id, str(app_name).lower(), dataset, endpoint, guild_executor_key,
            guild_name, business_date, source_timezone, _json_text(request_params or {}),
            content_hash, media_type, str(target), len(content), int(row_count or 0), now, now,
        ),
    )
    return {'raw_object_id': object_id, 'content_hash': content_hash, 'artifact_path': str(target)}


def _upsert_business_identity(
    conn: sqlite3.Connection,
    *,
    streamer_uid: str,
    business_system: str,
    business_id_type: str,
    business_id_value: str,
    source_record_id: str,
    source_name: str,
    observed_at: str,
) -> None:
    if not streamer_uid or not str(business_id_value or '').strip():
        return
    conn.execute(
        """
        INSERT INTO streamer_business_identities(
            business_system,business_id_type,business_id_value,streamer_uid,
            source_record_id,verification_status,source_name,created_at,updated_at
        ) VALUES(?,?,?,?,?,'verified',?,?,?)
        ON CONFLICT(business_system,business_id_type,business_id_value) DO UPDATE SET
            streamer_uid=excluded.streamer_uid,source_record_id=excluded.source_record_id,
            verification_status='verified',source_name=excluded.source_name,
            updated_at=excluded.updated_at
        """,
        (
            business_system, business_id_type, str(business_id_value), streamer_uid,
            source_record_id, source_name, observed_at, observed_at,
        ),
    )


def seed_business_identity_links(
    conn: sqlite3.Connection,
    *,
    commit_every: int = 0,
) -> Dict[str, int]:
    ensure_streamer_foundation_tables(conn)
    now = _now()
    counts = {'leads': 0, 'customers': 0, 'ops_timo': 0, 'ops_intake': 0}
    batch_size = max(0, int(commit_every or 0))
    pending_writes = 0

    def commit_if_due(write_count: int = 1) -> None:
        nonlocal pending_writes
        if batch_size <= 0:
            return
        pending_writes += write_count
        if pending_writes >= batch_size:
            conn.commit()
            pending_writes = 0
    if _table_exists(conn, 'leads'):
        rows = conn.execute(
            """
            SELECT lead_id,matched_customer_id,lower(COALESCE(app_name,'')) AS app_name,yw_id
            FROM leads
            WHERE COALESCE(yw_id,'')<>'' AND lower(COALESCE(app_name,'')) IN ('linky','timo','sugo','sogo')
            """
        ).fetchall()
        for row in rows:
            app = 'sugo' if str(row[2]) == 'sogo' else str(row[2])
            alias_type = {'linky': 'sid', 'timo': 'timo_id', 'sugo': 'anchor_id'}[app]
            uid = streamer_uid_for_alias(conn, app_name=app, alias_type=alias_type, alias_value=str(row[3]))
            if not uid:
                continue
            _upsert_business_identity(
                conn, streamer_uid=uid, business_system='crm', business_id_type='lead_id',
                business_id_value=str(row[0]), source_record_id=str(row[0]),
                source_name='leads.yw_id', observed_at=now,
            )
            counts['leads'] += 1
            commit_if_due()
            if str(row[1] or ''):
                _upsert_business_identity(
                    conn, streamer_uid=uid, business_system='crm', business_id_type='customer_id',
                    business_id_value=str(row[1]), source_record_id=str(row[0]),
                    source_name='leads.matched_customer_id', observed_at=now,
                )
                counts['customers'] += 1
                commit_if_due()
    if _table_exists(conn, 'ops_timo_intake_items'):
        rows = conn.execute(
            """
            SELECT item_id,timo_id FROM ops_timo_intake_items
            WHERE COALESCE(timo_id,'')<>''
              AND (lower(COALESCE(timo_verify_status,'')) IN ('verified','success')
                   OR lower(COALESCE(system_status,'')) IN ('crm_success','verified_success'))
            """
        ).fetchall()
        for row in rows:
            uid = streamer_uid_for_alias(conn, app_name='timo', alias_type='timo_id', alias_value=str(row[1]))
            if not uid:
                continue
            _upsert_business_identity(
                conn, streamer_uid=uid, business_system='ops_intake', business_id_type='item_id',
                business_id_value=str(row[0]), source_record_id=str(row[0]),
                source_name='ops_timo_intake_items.timo_id', observed_at=now,
            )
            counts['ops_timo'] += 1
            commit_if_due()
    if _table_exists(conn, 'ops_intake_items'):
        columns = {str(row[1]) for row in conn.execute('PRAGMA table_info(ops_intake_items)').fetchall()}
        if {'item_id', 'parsed_app', 'parsed_account_id', 'system_status'} <= columns:
            rows = conn.execute(
                """
                SELECT item_id,lower(COALESCE(parsed_app,'')),parsed_account_id
                FROM ops_intake_items
                WHERE COALESCE(parsed_account_id,'')<>''
                  AND lower(COALESCE(parsed_app,'')) IN ('linky','sugo','sogo')
                  AND lower(COALESCE(system_status,'')) IN ('fully_success','success','verified_success','crm_success')
                """
            ).fetchall()
            for row in rows:
                app = 'sugo' if str(row[1]) == 'sogo' else str(row[1])
                alias_type = 'sid' if app == 'linky' else 'anchor_id'
                uid = streamer_uid_for_alias(conn, app_name=app, alias_type=alias_type, alias_value=str(row[2]))
                if not uid:
                    continue
                _upsert_business_identity(
                    conn, streamer_uid=uid, business_system='ops_intake', business_id_type='item_id',
                    business_id_value=str(row[0]), source_record_id=str(row[0]),
                    source_name='ops_intake_items.parsed_account_id', observed_at=now,
                )
                counts['ops_intake'] += 1
                commit_if_due()
    return counts


def backfill_streamer_identities(
    conn: sqlite3.Connection,
    *,
    app_names: Sequence[str] = ('linky', 'sugo', 'timo'),
    commit_every: int = 2000,
) -> Dict[str, Any]:
    ensure_streamer_foundation_tables(conn)
    conn.row_factory = sqlite3.Row
    results: Dict[str, Any] = {
        'profiles': {},
        'business_links': {},
        'normalized_unknown_real_person_rows': 0,
    }
    selected = {str(app).strip().lower() for app in app_names}
    if _table_exists(conn, 'streamer_external_profiles'):
        before = conn.total_changes
        if 'linky' in selected:
            excluded = ','.join('?' for _ in LINKY_FOUNDATION_EXCLUDED_GUILDS)
            conn.execute(
                f"UPDATE streamer_external_profiles SET is_real_person=0 "
                f"WHERE app_name='linky' AND guild_name NOT IN ({excluded}) AND is_real_person<>0",
                tuple(sorted(LINKY_FOUNDATION_EXCLUDED_GUILDS)),
            )
        if 'sugo' in selected:
            conn.execute(
                "UPDATE streamer_external_profiles SET is_real_person=0 "
                "WHERE app_name='sugo' AND is_real_person<>0"
            )
        results['normalized_unknown_real_person_rows'] = conn.total_changes - before
        conn.commit()
    for app in ('linky', 'sugo'):
        if app not in selected or not _table_exists(conn, 'streamer_external_profiles'):
            continue
        count = 0
        exclusion_sql = ''
        params: tuple[Any, ...] = (app,)
        if app == 'linky':
            placeholders = ','.join('?' for _ in LINKY_FOUNDATION_EXCLUDED_GUILDS)
            exclusion_sql = f' AND guild_name NOT IN ({placeholders})'
            params = (app, *sorted(LINKY_FOUNDATION_EXCLUDED_GUILDS))
        query = f"""
            SELECT app_name,guild_executor_key,guild_name,country,streamer_id,
                   platform_user_id,platform_character_id,nickname,registered_at_bj,
                   last_active_at_bj,is_real_person,source_name,source_payload,updated_at
            FROM streamer_external_profiles WHERE app_name=?{exclusion_sql}
            ORDER BY guild_executor_key,streamer_id
            """
        batch_size = max(1, int(commit_every or 2000))
        offset = 0
        while True:
            batch = conn.execute(
                f'{query} LIMIT ? OFFSET ?',
                (*params, batch_size, offset),
            ).fetchall()
            conn.commit()
            if not batch:
                break
            for row in batch:
                official = _payload_dict(row['source_payload'])
                upsert_streamer_profile(
                    conn, app_name=app, guild_executor_key=str(row['guild_executor_key']),
                    guild_name=str(row['guild_name']), country=str(row['country'] or ''),
                    streamer_id=str(row['streamer_id']), platform_user_id=str(row['platform_user_id'] or ''),
                    platform_character_id=str(row['platform_character_id'] or ''), nickname=str(row['nickname'] or ''),
                    registered_at=str(row['registered_at_bj'] or ''),
                    registered_timezone='UTC' if app == 'linky' else 'Asia/Shanghai',
                    last_active_at=str(row['last_active_at_bj'] or ''),
                    last_active_timezone='UTC' if app == 'linky' else 'Asia/Shanghai',
                    is_real_person_status='unknown', official_fields=official,
                    source_name=str(row['source_name'] or ''), observed_at=str(row['updated_at'] or _now()),
                )
                count += 1
            conn.commit()
            offset += len(batch)
        conn.commit()
        results['profiles'][app] = count
    if 'timo' in selected and _table_exists(conn, 'timo_external_streamers'):
        count = 0
        query = """
            SELECT guild_executor_key,guild_name,country,timo_id,user_uuid,nickname,
                   registered_at_bj,last_active_at_bj,is_real_person,status,host_role,
                   source_payload,updated_at
            FROM timo_external_streamers ORDER BY guild_executor_key,timo_id
            """
        batch_size = max(1, int(commit_every or 2000))
        offset = 0
        while True:
            batch = conn.execute(f'{query} LIMIT ? OFFSET ?', (batch_size, offset)).fetchall()
            conn.commit()
            if not batch:
                break
            for row in batch:
                upsert_streamer_profile(
                    conn, app_name='timo', guild_executor_key=str(row['guild_executor_key']),
                    guild_name=str(row['guild_name']), country=str(row['country'] or ''),
                    streamer_id=str(row['timo_id']), user_uuid=str(row['user_uuid'] or ''),
                    nickname=str(row['nickname'] or ''), registered_at=str(row['registered_at_bj'] or ''),
                    registered_timezone='Asia/Shanghai', last_active_at=str(row['last_active_at_bj'] or ''),
                    last_active_timezone='Asia/Shanghai',
                    is_real_person_status='verified' if int(row['is_real_person'] or 0) == 1 else 'unverified',
                    status=str(row['status'] or ''), role=str(row['host_role'] or ''),
                    official_fields=_payload_dict(row['source_payload']), source_name='timo_get_host_list',
                    observed_at=str(row['updated_at'] or _now()),
                )
                count += 1
            conn.commit()
            offset += len(batch)
        conn.commit()
        results['profiles']['timo'] = count
    results['business_links'] = seed_business_identity_links(conn)
    conn.commit()
    results['identity_count'] = int(conn.execute('SELECT COUNT(*) FROM streamer_identities').fetchone()[0] or 0)
    results['alias_count'] = int(conn.execute('SELECT COUNT(*) FROM streamer_identity_aliases').fetchone()[0] or 0)
    results['membership_count'] = int(conn.execute('SELECT COUNT(*) FROM streamer_guild_memberships').fetchone()[0] or 0)
    return results


def capture_timo_foundation_snapshot(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    snapshot_at: str,
    business_date: str,
    provisional: bool,
    trigger_type: str = 'scheduled',
    commit_every: int = 0,
) -> Dict[str, int]:
    ensure_streamer_foundation_tables(conn)
    conn.row_factory = sqlite3.Row
    batch_size = max(0, int(commit_every or 0))
    profile_count = revision_count = task_count = 0
    profile_rows = [dict(row) for row in conn.execute(
        """
        SELECT guild_executor_key,guild_name,country,timo_id,user_uuid,nickname,
               registered_at_bj,last_active_at_bj,is_real_person,status,host_role,
               source_payload,updated_at
        FROM timo_external_streamers
        WHERE snapshot_at=?
        ORDER BY guild_executor_key,timo_id
        """,
        (snapshot_at,),
    ).fetchall()]
    revenue_rows = [dict(row) for row in conn.execute(
        """
        SELECT guild_executor_key,guild_name,country,timo_id,user_uuid,total_income,
               qualified_revenue,matching_income,private_message_income,private_gift_income,
               call_income,online_hours,call_count,quality_host,quality_revenue,provisional,
               source_payload,updated_at
        FROM timo_external_revenue_daily
        WHERE stat_date_bj=?
        ORDER BY guild_executor_key,timo_id
        """,
        (business_date,),
    ).fetchall()]
    task_rows = [dict(row) for row in conn.execute(
        """
        SELECT guild_executor_key,guild_name,source_payload
        FROM timo_external_guild_task_snapshots
        WHERE snapshot_at=?
        ORDER BY guild_executor_key,task_type
        """,
        (snapshot_at,),
    ).fetchall()]

    # Freeze all source inputs before the first foundation write. This keeps
    # parsing/grouping work outside the shared writer lane.
    profiles_by_guild: Dict[str, list[Dict[str, Any]]] = {}
    guild_meta = {
        str(row['guild_executor_key']): (str(row['guild_name']), str(row['country'] or ''))
        for row in profile_rows
    }
    for row in profile_rows:
        profiles_by_guild.setdefault(str(row['guild_executor_key']), []).append(
            _payload_dict(row['source_payload'])
        )
    revenue_by_guild: Dict[str, Dict[str, float]] = {}
    for row in revenue_rows:
        current = revenue_by_guild.setdefault(str(row['guild_executor_key']), {'rows': 0.0, 'income': 0.0})
        current['rows'] += 1
        current['income'] += float(row['total_income'] or 0)
    tasks_by_guild: Dict[str, list[Dict[str, Any]]] = {}
    for row in task_rows:
        tasks_by_guild.setdefault(str(row['guild_executor_key']), []).append(
            _payload_dict(row['source_payload'])
        )

    for row in profile_rows:
        official = _payload_dict(row['source_payload'])
        upsert_streamer_profile(
            conn,
            app_name='timo',
            guild_executor_key=str(row['guild_executor_key']),
            guild_name=str(row['guild_name']),
            country=str(row['country'] or ''),
            streamer_id=str(row['timo_id']),
            user_uuid=str(row['user_uuid'] or ''),
            nickname=str(row['nickname'] or ''),
            registered_at=str(row['registered_at_bj'] or ''),
            registered_timezone='Asia/Shanghai',
            last_active_at=str(row['last_active_at_bj'] or ''),
            last_active_timezone='Asia/Shanghai',
            is_real_person_status='verified' if int(row['is_real_person'] or 0) == 1 else 'unverified',
            status=str(row['status'] or ''),
            role=str(row['host_role'] or ''),
            official_fields=official,
            source_name='timo_get_host_list',
            source_run_id=run_id,
            observed_at=str(row['updated_at'] or snapshot_at),
        )
        profile_count += 1
        if batch_size and profile_count % batch_size == 0:
            conn.commit()
    if batch_size:
        conn.commit()
    for guild_key, payload_rows in profiles_by_guild.items():
        guild_name, _ = guild_meta[guild_key]
        archive_raw_json(
            conn,
            run_id=run_id,
            app_name='timo',
            dataset='host_list_snapshot',
            endpoint='website-frontend/v1/officalWebGuild/getHostList',
            payload={'items': payload_rows, 'total': len(payload_rows)},
            guild_executor_key=guild_key,
            guild_name=guild_name,
            business_date=business_date,
            source_timezone='Asia/Shanghai',
            row_count=len(payload_rows),
            retrieved_at=snapshot_at,
        )
        if batch_size:
            conn.commit()
    if batch_size:
        conn.commit()
    processed_revenue_rows = 0
    for row in revenue_rows:
        official = _payload_dict(row['source_payload'])
        revision = record_streamer_daily_revision(
            conn,
            app_name='timo',
            guild_executor_key=str(row['guild_executor_key']),
            guild_name=str(row['guild_name']),
            country=str(row['country'] or ''),
            business_date=business_date,
            source_timezone='Asia/Shanghai',
            native_streamer_id=str(row['timo_id']),
            total_income=float(row['total_income'] or 0),
            other_income=float(row['total_income'] or 0),
            active_days=1 if float(row['total_income'] or 0) > 0 else 0,
            provisional=int(row['provisional'] or 0),
            official_fields=official,
            source_name='timo_revenue_export',
            source_run_id=run_id,
            observed_at=str(row['updated_at'] or snapshot_at),
            user_uuid=str(row['user_uuid'] or ''),
        )
        revision_count += int(bool(revision['changed']))
        processed_revenue_rows += 1
        if batch_size and processed_revenue_rows % batch_size == 0:
            conn.commit()
    if batch_size:
        conn.commit()
    for guild_key, values in revenue_by_guild.items():
        guild_name, _ = guild_meta.get(guild_key, ('', ''))
        record_ingestion_scope(
            conn,
            run_id=run_id,
            app_name='timo',
            dataset='revenue_daily',
            guild_executor_key=guild_key,
            guild_name=guild_name,
            business_date=business_date,
            source_timezone='Asia/Shanghai',
            trigger_type=trigger_type,
            status='success',
            expected_rows=int(values['rows']),
            scanned_rows=int(values['rows']),
            saved_rows=int(values['rows']),
            detail_income=float(values['income']),
        )
        if batch_size:
            conn.commit()
    if batch_size:
        conn.commit()
    task_count = len(task_rows)
    for guild_key, payload_rows in tasks_by_guild.items():
        guild_name = str(next((row['guild_name'] for row in task_rows if str(row['guild_executor_key']) == guild_key), ''))
        archive_raw_json(
            conn,
            run_id=run_id,
            app_name='timo',
            dataset='guild_task_list',
            endpoint='website-frontend/v1/officalWebGuild/getGuildTaskList',
            payload={'items': payload_rows},
            guild_executor_key=guild_key,
            guild_name=guild_name,
            business_date=business_date,
            source_timezone='Asia/Shanghai',
            row_count=len(payload_rows),
            retrieved_at=snapshot_at,
        )
        if batch_size:
            conn.commit()
    if batch_size:
        conn.commit()
    seed_business_identity_links(conn, commit_every=batch_size)
    conn.commit()
    return {
        'profiles': profile_count,
        'daily_revision_changes': revision_count,
        'tasks': task_count,
        'provisional': int(bool(provisional)),
    }
