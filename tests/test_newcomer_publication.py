from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from urllib import error

from fastapi.testclient import TestClient
import pytest

from app.main import Database, create_app
from app.newcomer_publication import (
    NewcomerPublicationIntegrityError,
    canonical_json,
    dispatch_pending_newcomer_events,
    list_newcomer_publication,
    reconcile_newcomer_publication,
    send_newcomer_event,
)


def test_five_minute_notifier_drop_in_drains_durable_outbox():
    source = (
        Path(__file__).resolve().parents[1]
        / 'scripts/systemd/mcn-daily-data-completion-notifier.service.d/20-newcomer-publication.conf'
    ).read_text(encoding='utf-8')
    assert 'ExecStartPost=-' in source
    assert 'notify_newcomer_publications.py' in source


DATE = '2026-08-13'
NOW = '2026-08-14T01:00:00+00:00'


def _guild(conn: sqlite3.Connection, platform: str, name: str, guild_id: str) -> str:
    conn.execute(
        """
        INSERT INTO guild_executors (
            guild_name,app_name,backend_url,login_username,cms_guild_id,
            cms_guild_sid,country,guild_country,enabled,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,1,?)
        """,
        (name, platform, 'https://example.invalid', 'test', guild_id, '', 'Brazil', 'Brazil', NOW),
    )
    return f'{platform}:cms_guild_id:{guild_id}'


def _success(conn: sqlite3.Connection, key: str, name: str, count: int) -> None:
    conn.execute(
        """
        INSERT INTO guild_anchor_daily_stat_jobs (
            job_id,guild_executor_key,guild_name,stat_date,status,attempt_count,
            max_attempts,created_at,updated_at
        ) VALUES (?,?,?,?, 'success',1,5,?,?)
        """,
        (f'{key}:{DATE}', key, name, DATE, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO guild_anchor_daily_stats (
            guild_executor_key,guild_name,stat_date,joined_count,real_person_count,
            status,refreshed_at
        ) VALUES (?,?,?,?,?,'success',?)
        """,
        (key, name, DATE, count, count, NOW),
    )


def _linky_member(conn: sqlite3.Connection, key: str, name: str, subject_id: str) -> None:
    conn.execute(
        """
        INSERT INTO guild_anchor_newcomer_identity_snapshots (
            guild_executor_key,guild_name,stat_date,anchor_id,streamer_sid,
            anchor_name,source_created_at,snapshot_refreshed_at,recorded_at
        ) VALUES (?,?,?,?,?,?,1,?,?)
        """,
        (key, name, DATE, f'anchor:{subject_id}', subject_id, f'host-{subject_id}', NOW, NOW),
    )


def _linky_run(conn: sqlite3.Connection, key: str, name: str, count: int) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO guild_anchor_newcomer_snapshot_runs (
            guild_executor_key,guild_name,stat_date,member_count,source_contract,
            snapshot_refreshed_at,recorded_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (key, name, DATE, count, 'created_at_utc_date_v1', NOW, NOW),
    )


def _timo_member(
    conn: sqlite3.Connection, key: str, name: str, subject_id: str, uuid: str,
) -> None:
    conn.execute(
        """
        INSERT INTO guild_anchor_seen (
            guild_executor_key,guild_name,anchor_id,anchor_name,created_at,
            created_date_bj,first_seen_at,last_seen_at
        ) VALUES (?,?,?,?,1,?,?,?)
        """,
        (key, name, f'timo:{subject_id}', f'host-{subject_id}', DATE, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO timo_external_streamers (
            guild_executor_key,guild_name,timo_id,user_uuid,nickname,
            joined_guild_at_bj,snapshot_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (key, name, subject_id, uuid, f'host-{subject_id}', f'{DATE} 08:00:00', NOW, NOW),
    )


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / 'automation.db'))
    with database.connect() as conn:
        yield conn


def test_linky_snapshot_is_immutable_and_revisioned(db):
    key = _guild(db, 'linky', 'Linky BR', 'L1')
    _success(db, key, 'Linky BR', 1)
    _linky_member(db, key, 'Linky BR', '200')
    _linky_run(db, key, 'Linky BR', 1)

    first = reconcile_newcomer_publication(db, platform='LINKY', business_date=DATE, created_at=NOW)
    assert first['status'] == 'complete'
    assert first['revision'] == 1
    complete_event = json.loads(
        db.execute(
            "SELECT payload_json FROM newcomer_publication_events WHERE event_type LIKE '%completed'"
        ).fetchone()[0]
    )
    assert complete_event['completedAt'] == NOW
    _guild(db, 'linky', 'Later Guild', 'L2')
    assert reconcile_newcomer_publication(db, platform='linky', business_date=DATE)['status'] == 'unchanged'

    _linky_member(db, key, 'Linky BR', '100')
    _linky_run(db, key, 'Linky BR', 2)
    db.execute(
        "UPDATE guild_anchor_daily_stats SET joined_count=2,real_person_count=2 WHERE stat_date=?",
        (DATE,),
    )
    revised = reconcile_newcomer_publication(db, platform='linky', business_date=DATE, created_at=NOW)
    assert revised['status'] == 'revised'
    assert revised['revision'] == 2
    assert db.execute(
        "SELECT COUNT(*) FROM newcomer_daily_publication_members WHERE revision=1"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM newcomer_daily_publication_members WHERE revision=2"
    ).fetchone()[0] == 2


def test_timo_contract_counts_and_checksum(db):
    key = _guild(db, 'timo', 'Royal BR', 'BR11501')
    _success(db, key, 'Royal BR', 2)
    _timo_member(db, key, 'Royal BR', '20', 'uuid-20')
    _timo_member(db, key, 'Royal BR', '10', 'uuid-10')
    result = reconcile_newcomer_publication(db, platform='TIMO', business_date=DATE, created_at=NOW)
    payload = list_newcomer_publication(db, platform='timo', business_date=DATE)['data']

    expected_rows = [
        {'guildId': 'BR11501', 'guildName': 'Royal BR', 'sourceUserUuid': 'uuid-10', 'subjectId': '10'},
        {'guildId': 'BR11501', 'guildName': 'Royal BR', 'sourceUserUuid': 'uuid-20', 'subjectId': '20'},
    ]
    expected_checksum = hashlib.sha256(canonical_json(expected_rows).encode()).hexdigest()
    assert result['checksum'] == expected_checksum
    assert payload['platform'] == 'TIMO'
    assert payload['dateContract'] == 'timo_join_time_beijing_date_v1'
    assert payload['summaryCount'] == payload['rosterCount'] == payload['uniqueIdCount'] == 2
    assert payload['rows'] == expected_rows


def test_count_mismatch_fails_closed(db):
    key = _guild(db, 'linky', 'Linky BR', 'L1')
    _success(db, key, 'Linky BR', 2)
    _linky_member(db, key, 'Linky BR', '100')
    _linky_run(db, key, 'Linky BR', 1)
    with pytest.raises(NewcomerPublicationIntegrityError, match='summary_member_count_mismatch'):
        reconcile_newcomer_publication(db, platform='linky', business_date=DATE)
    assert db.execute('SELECT COUNT(*) FROM newcomer_daily_publications').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM newcomer_publication_events').fetchone()[0] == 0


def test_terminal_job_emits_one_non_consumable_failure(db):
    key = _guild(db, 'timo', 'Royal BR', 'BR11501')
    db.execute(
        """
        INSERT INTO guild_anchor_daily_stat_jobs (
            job_id,guild_executor_key,guild_name,stat_date,status,attempt_count,
            max_attempts,error,created_at,updated_at
        ) VALUES (?,?,?,?, 'dead',5,5,'ticket expired',?,?)
        """,
        (f'{key}:{DATE}', key, 'Royal BR', DATE, NOW, NOW),
    )
    first = reconcile_newcomer_publication(db, platform='timo', business_date=DATE, created_at=NOW)
    second = reconcile_newcomer_publication(db, platform='timo', business_date=DATE, created_at=NOW)
    event = json.loads(db.execute('SELECT payload_json FROM newcomer_publication_events').fetchone()[0])
    assert first['status'] == second['status'] == 'failed'
    assert db.execute('SELECT COUNT(*) FROM newcomer_publication_events').fetchone()[0] == 1
    assert event['consumable'] is False
    assert event['completedGuildCount'] == 0


def test_read_api_uses_bearer_and_exact_contract(tmp_path):
    db_path = tmp_path / 'api.db'
    app = create_app({
        'DB_PATH': str(db_path),
        'AUTH_ENABLED': False,
        'NEWCOMER_EXTERNAL_FEED_TOKEN': 'read-token',
    })
    database = Database(str(db_path))
    with database.connect() as conn:
        key = _guild(conn, 'linky', 'Linky BR', 'L1')
        _success(conn, key, 'Linky BR', 1)
        _linky_member(conn, key, 'Linky BR', '100')
        _linky_run(conn, key, 'Linky BR', 1)
        reconcile_newcomer_publication(conn, platform='linky', business_date=DATE, created_at=NOW)
        conn.commit()

    client = TestClient(app)
    url = f'/api/external/newcomers/daily?platform=LINKY&business_date={DATE}&revision=1'
    assert client.get(url).status_code == 401
    response = client.get(url, headers={'Authorization': 'Bearer read-token'})
    assert response.status_code == 200
    assert response.json()['data']['schemaVersion'] == 1
    assert response.json()['data']['revision'] == 1


def test_hmac_http_202_and_bounded_retry():
    calls = []

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"ok":true,"duplicate":true}'

    def opener(http_request, timeout):
        calls.append((http_request, timeout))
        if len(calls) < 3:
            raise error.URLError('temporary')
        return Response()

    event = {'eventId': 'event-1', 'eventType': 'mcn.newcomers.daily.completed'}
    result = send_newcomer_event(
        event,
        url='https://nova.invalid/api/internal/mcn/newcomers/events',
        secret='s' * 32,
        attempts=3,
        opener=opener,
        sleep=lambda _seconds: None,
    )
    sent = calls[-1][0]
    timestamp = sent.headers['X-mcn-timestamp']
    expected = hmac.new(
        b's' * 32,
        f'{timestamp}.'.encode() + canonical_json(event).encode(),
        hashlib.sha256,
    ).hexdigest()
    assert result == {'ok': True, 'event_id': 'event-1', 'duplicate': True, 'attempts': 3}
    assert sent.headers['X-mcn-signature'] == f'sha256={expected}'


def test_outbox_dispatch_marks_delivery(db):
    key = _guild(db, 'linky', 'Linky BR', 'L1')
    _success(db, key, 'Linky BR', 1)
    _linky_member(db, key, 'Linky BR', '100')
    _linky_run(db, key, 'Linky BR', 1)
    reconcile_newcomer_publication(db, platform='linky', business_date=DATE, created_at=NOW)
    calls = []

    def sender(event, **_kwargs):
        calls.append(event['eventId'])
        return {'ok': True}

    result = dispatch_pending_newcomer_events(
        db, url='https://nova.invalid', secret='s' * 32, now_iso=NOW, sender=sender,
    )
    assert result['delivered_count'] == 1
    assert len(calls) == 1
    assert db.execute('SELECT delivery_status FROM newcomer_publication_events').fetchone()[0] == 'delivered'
