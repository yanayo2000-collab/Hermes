from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


TOKEN = 'fan-conversion-read-token'


def _client(tmp_path) -> TestClient:
    return TestClient(create_app({
        'DB_PATH': str(tmp_path / 'automation.db'),
        'AUTH_ENABLED': False,
        'NEWCOMER_EXTERNAL_FEED_TOKEN': TOKEN,
    }))


def _insert(
    client: TestClient,
    *,
    item_id: str,
    status: str,
    updated_at: str,
    result_code: str = 'bind_success',
    result_reason: str = 'ok',
    platform: str = 'Linky',
    phone: str = '+62 87722090497',
    subject_id: str = '53322723',
) -> None:
    with client.app.state.service.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO ops_intake_items (
                item_id,guild_name,submitted_by_user_id,submitted_by_username,
                raw_text,parsed_phone,parsed_account_id,parsed_group,parsed_code,
                parsed_app,parsed_agency,system_status,feedback_status,reply_text,
                result_code,result_reason,result_snapshot,created_at,processed_at,
                external_customer_service_id,external_customer_service_name
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item_id, 'Carote', 'local-user-id', 'local-user', 'raw', phone,
                subject_id, '其他渠道', 'ABC123', platform, 'Carote', status,
                'not_feedbackable', 'done', result_code, result_reason, '{}',
                '2026-08-14T00:00:00+00:00', updated_at,
                'external-cs-id', 'External Agent',
            ),
        )
        conn.commit()


def _get(client: TestClient, query: str = ''):
    return client.get(
        '/api/external/fan-conversions/daily' + query,
        headers={'Authorization': f'Bearer {TOKEN}'},
    )


def test_export_requires_bearer_and_returns_stable_success_fields(tmp_path):
    client = _client(tmp_path)
    _insert(
        client,
        item_id='success-1',
        status='fully_success',
        updated_at='2026-08-14T01:00:00+00:00',
    )

    assert client.get('/api/external/fan-conversions/daily').status_code == 401
    response = _get(client)
    assert response.status_code == 200
    data = response.json()['data']
    assert data['sourceContract'] == 'ops_intake_success_v1'
    assert data['total'] == 1
    assert data['rows'] == [{
        'sourceRecordKey': 'ops_intake_item:success-1',
        'idempotencyKey': 'ops_intake_item:success-1',
        'platform': 'LINKY',
        'subjectId': '53322723',
        'whatsappId': '+6287722090497',
        'operatorName': 'External Agent',
        'operatorAccountKey': 'external-cs-id',
        'guildName': 'Carote',
        'observedAt': '2026-08-14T01:00:00+00:00',
        'sourceUpdatedAt': '2026-08-14T01:00:00+00:00',
    }]
    with client.app.state.service.db.connect() as conn:
        assert conn.execute(
            'SELECT COUNT(*) FROM ops_intake_binding_history_projection_meta'
        ).fetchone()[0] == 0


def test_export_excludes_failure_processing_duplicate_and_invalid_rows(tmp_path):
    client = _client(tmp_path)
    _insert(client, item_id='success', status='success', updated_at='2026-08-14T01:00:00+00:00')
    _insert(
        client,
        item_id='timo-success',
        status='fully_success',
        updated_at='2026-08-14T01:00:30+00:00',
        platform='Timo',
        subject_id='T-100',
    )
    _insert(client, item_id='failed', status='bind_failed', updated_at='2026-08-14T01:01:00+00:00')
    _insert(client, item_id='processing', status='processing', updated_at='2026-08-14T01:02:00+00:00')
    _insert(
        client,
        item_id='duplicate-code',
        status='fully_success',
        updated_at='2026-08-14T01:03:00+00:00',
        result_code='duplicate_sid',
    )
    _insert(
        client,
        item_id='duplicate-reason',
        status='success',
        updated_at='2026-08-14T01:04:00+00:00',
        result_reason='SID already exists',
    )
    _insert(
        client,
        item_id='unsupported-app',
        status='success',
        updated_at='2026-08-14T01:05:00+00:00',
        platform='Sugo',
    )
    _insert(
        client,
        item_id='missing-subject',
        status='success',
        updated_at='2026-08-14T01:06:00+00:00',
        subject_id='',
    )

    response = _get(client)
    assert response.status_code == 200
    assert [row['sourceRecordKey'] for row in response.json()['data']['rows']] == [
        'ops_intake_item:success',
        'ops_intake_item:timo-success',
    ]
    assert response.json()['data']['rows'][1]['platform'] == 'TIMO'


def test_export_increment_is_inclusive_stable_and_paginated(tmp_path):
    client = _client(tmp_path)
    _insert(
        client,
        item_id='same-time-b',
        status='success',
        updated_at='2026-08-14T02:00:00+00:00',
        subject_id='2',
    )
    _insert(
        client,
        item_id='same-time-a',
        status='success',
        updated_at='2026-08-14T02:00:00+00:00',
        subject_id='1',
    )
    _insert(
        client,
        item_id='later',
        status='success',
        updated_at='2026-08-14T03:00:00+00:00',
        subject_id='3',
    )
    query = '?updated_since=2026-08-14T02%3A00%3A00%2B00%3A00&limit=2&offset=0'
    first = _get(client, query)
    data = first.json()['data']
    assert data['updatedSince'] == '2026-08-14T02:00:00+00:00'
    assert data['total'] == 3
    assert data['hasMore'] is True
    assert [row['sourceRecordKey'] for row in data['rows']] == [
        'ops_intake_item:same-time-a',
        'ops_intake_item:same-time-b',
    ]

    second = _get(
        client,
        '?updated_since=2026-08-14T02%3A00%3A00%2B00%3A00&limit=2&offset=2',
    )
    assert [row['sourceRecordKey'] for row in second.json()['data']['rows']] == [
        'ops_intake_item:later'
    ]
    assert second.json()['data']['hasMore'] is False


def test_export_rejects_invalid_watermark(tmp_path):
    client = _client(tmp_path)
    response = _get(client, '?updated_since=not-a-time')
    assert response.status_code == 400
    assert response.json()['detail']['reason'] == 'invalid_updated_since'
