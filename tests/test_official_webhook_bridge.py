from fastapi.testclient import TestClient

from app.official_webhook_bridge_app import create_app


def test_webhook_verify_returns_challenge_when_token_matches():
    app = create_app({'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123'})
    client = TestClient(app)

    response = client.get(
        '/webhooks/whatsapp',
        params={
            'hub.mode': 'subscribe',
            'hub.verify_token': 'token-123',
            'hub.challenge': 'abc123',
        },
    )

    assert response.status_code == 200
    assert response.text == 'abc123'


def test_webhook_verify_rejects_invalid_token():
    app = create_app({'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123'})
    client = TestClient(app)

    response = client.get(
        '/webhooks/whatsapp',
        params={
            'hub.mode': 'subscribe',
            'hub.verify_token': 'wrong-token',
            'hub.challenge': 'abc123',
        },
    )

    assert response.status_code == 403


def test_webhook_post_accepts_payload_and_exposes_latest_event_summary():
    app = create_app({'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123'})
    client = TestClient(app)

    payload = {
        'object': 'whatsapp_business_account',
        'entry': [
            {
                'id': 'waba_1',
                'changes': [
                    {
                        'field': 'messages',
                        'value': {
                            'metadata': {'display_phone_number': '12345', 'phone_number_id': 'pnid_1'},
                            'messages': [{'from': '628111111111', 'id': 'wamid-1', 'type': 'text'}],
                        },
                    }
                ],
            }
        ],
    }
    response = client.post('/webhooks/whatsapp', json=payload)
    assert response.status_code == 200
    assert response.json()['received'] is True
    assert response.json()['event_count'] == 1

    latest = client.get('/ops/whatsapp-webhook/latest')
    assert latest.status_code == 200
    body = latest.json()
    assert body['has_event'] is True
    assert body['summary']['object'] == 'whatsapp_business_account'
    assert body['summary']['entry_count'] == 1
    assert body['summary']['message_count'] == 1
    assert body['summary']['display_phone_number'] == '12345'


def test_webhook_recent_events_keeps_last_50_and_returns_latest_first():
    app = create_app({'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123'})
    client = TestClient(app)

    for idx in range(55):
        payload = {
            'object': 'whatsapp_business_account',
            'entry': [
                {
                    'id': f'waba_{idx}',
                    'changes': [
                        {
                            'field': f'field_{idx}',
                            'value': {
                                'metadata': {
                                    'display_phone_number': f'phone_{idx}',
                                    'phone_number_id': f'pnid_{idx}',
                                },
                                'messages': [],
                            },
                        }
                    ],
                }
            ],
        }
        response = client.post('/webhooks/whatsapp', json=payload)
        assert response.status_code == 200

    recent = client.get('/ops/whatsapp-webhook/recent')
    assert recent.status_code == 200
    body = recent.json()
    assert body['event_count'] == 50
    assert len(body['events']) == 50
    assert body['events'][0]['summary']['display_phone_number'] == 'phone_54'
    assert body['events'][0]['payload']['entry'][0]['changes'][0]['field'] == 'field_54'
    assert body['events'][-1]['summary']['display_phone_number'] == 'phone_5'
    assert body['events'][-1]['payload']['entry'][0]['changes'][0]['field'] == 'field_5'


def test_webhook_recent_events_expose_normalized_identifiers_and_dedupe_key():
    app = create_app({'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123'})
    client = TestClient(app)

    payload = {
        'object': 'whatsapp_business_account',
        'entry': [
            {
                'id': 'waba_1',
                'changes': [
                    {
                        'field': 'group_participants_update',
                        'value': {
                            'metadata': {'display_phone_number': '12345550001', 'phone_number_id': 'pnid_1'},
                            'groups': [
                                {
                                    'timestamp': 1695414936173,
                                    'type': 'group_participants_add',
                                    'group_id': 'group_1',
                                    'request_id': 'request_1',
                                    'join_request_id': 'join_1',
                                    'wa_id': '1800555555',
                                    'added_participants': [
                                        {'input': '+1(800)-555-5555', 'wa_id': '1800555555'},
                                        {'input': '+1(800)-555-5556', 'wa_id': '1800555556'},
                                    ],
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }

    response = client.post('/webhooks/whatsapp', json=payload)
    assert response.status_code == 200

    recent = client.get('/ops/whatsapp-webhook/recent')
    assert recent.status_code == 200
    event = recent.json()['events'][0]
    normalized = event['normalized']
    assert normalized['field'] == 'group_participants_update'
    assert normalized['phone_number_id'] == 'pnid_1'
    assert normalized['display_phone_number'] == '12345550001'
    assert normalized['group_ids'] == ['group_1']
    assert normalized['group_event_types'] == ['group_participants_add']
    assert normalized['request_ids'] == ['request_1']
    assert normalized['join_request_ids'] == ['join_1']
    assert normalized['wa_ids'] == ['1800555555', '1800555556']
    assert normalized['message_ids'] == []
    assert normalized['payload_hash']
    assert normalized['dedupe_key'].startswith('group_participants_update|pnid_1|group_1|group_participants_add|join_1')


def test_webhook_stats_separates_message_and_group_metrics_without_cross_number_merge():
    app = create_app({'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123'})
    client = TestClient(app)

    message_payload = {
        'object': 'whatsapp_business_account',
        'entry': [
            {
                'id': 'waba_message',
                'changes': [
                    {
                        'field': 'messages',
                        'value': {
                            'metadata': {'display_phone_number': '16505551111', 'phone_number_id': 'shared_pnid'},
                            'contacts': [{'wa_id': '16315551181'}],
                            'messages': [{'id': 'wamid-1', 'from': '16315551181', 'timestamp': '1504902988', 'type': 'text'}],
                        },
                    }
                ],
            }
        ],
    }
    group_payload = {
        'object': 'whatsapp_business_account',
        'entry': [
            {
                'id': 'waba_group',
                'changes': [
                    {
                        'field': 'group_participants_update',
                        'value': {
                            'metadata': {'display_phone_number': '12345550001', 'phone_number_id': 'shared_pnid'},
                            'groups': [
                                {
                                    'timestamp': 1695414936173,
                                    'type': 'group_participants_add',
                                    'group_id': 'group_1',
                                    'added_participants': [
                                        {'wa_id': '1800555555'},
                                        {'wa_id': '1800555556'},
                                    ],
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }

    assert client.post('/webhooks/whatsapp', json=message_payload).status_code == 200
    assert client.post('/webhooks/whatsapp', json=group_payload).status_code == 200

    stats = client.get('/ops/whatsapp-webhook/stats')
    assert stats.status_code == 200
    body = stats.json()
    assert body['total_events'] == 2
    assert body['by_field']['messages'] == 1
    assert body['by_field']['group_participants_update'] == 1
    assert body['message_metrics']['unique_message_ids'] == 1
    assert body['message_metrics']['unique_message_senders'] == 1
    assert body['group_metrics']['unique_group_ids'] == 1
    assert body['group_metrics']['unique_group_participant_wa_ids'] == 2
    assert body['phone_number_metrics']['shared_pnid']['event_count'] == 2
    assert sorted(body['phone_number_metrics']['shared_pnid']['display_phone_numbers']) == ['12345550001', '16505551111']
    assert body['phone_number_metrics']['shared_pnid']['message_event_count'] == 1
    assert body['phone_number_metrics']['shared_pnid']['group_event_count'] == 1


def test_webhook_stats_exposes_commercial_grade_group_metrics_breakdown():
    app = create_app({'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123'})
    client = TestClient(app)

    payloads = [
        {
            'object': 'whatsapp_business_account',
            'entry': [
                {
                    'id': 'waba_lifecycle',
                    'changes': [
                        {
                            'field': 'group_lifecycle_update',
                            'value': {
                                'metadata': {'display_phone_number': '12345550001', 'phone_number_id': 'shared_pnid'},
                                'groups': [
                                    {
                                        'timestamp': 1695414936173,
                                        'type': 'group_create',
                                        'group_id': 'group_1',
                                        'request_id': 'request_1',
                                        'added_participants': [
                                            {'wa_id': '1800555555'},
                                            {'wa_id': '1800555556'},
                                        ],
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        },
        {
            'object': 'whatsapp_business_account',
            'entry': [
                {
                    'id': 'waba_participants',
                    'changes': [
                        {
                            'field': 'group_participants_update',
                            'value': {
                                'metadata': {'display_phone_number': '12345550001', 'phone_number_id': 'shared_pnid'},
                                'groups': [
                                    {
                                        'timestamp': 1695414936173,
                                        'type': 'group_participants_add',
                                        'group_id': 'group_1',
                                        'request_id': 'request_1',
                                        'join_request_id': 'join_1',
                                        'wa_id': '1800555555',
                                        'added_participants': [
                                            {'wa_id': '1800555555'},
                                            {'wa_id': '1800555556'},
                                        ],
                                        'failed_participants': [
                                            {'wa_id': '1800555565'},
                                        ],
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        },
        {
            'object': 'whatsapp_business_account',
            'entry': [
                {
                    'id': 'waba_settings',
                    'changes': [
                        {
                            'field': 'group_settings_update',
                            'value': {
                                'metadata': {'display_phone_number': '12345550001', 'phone_number_id': 'shared_pnid'},
                                'groups': [
                                    {
                                        'timestamp': 1695414936174,
                                        'type': 'group_settings_update',
                                        'group_id': 'group_1',
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        },
        {
            'object': 'whatsapp_business_account',
            'entry': [
                {
                    'id': 'waba_status',
                    'changes': [
                        {
                            'field': 'group_status_update',
                            'value': {
                                'metadata': {'display_phone_number': '12345550001', 'phone_number_id': 'shared_pnid'},
                                'groups': [
                                    {
                                        'timestamp': 1695414936175,
                                        'type': 'group_suspend',
                                        'group_id': 'group_1',
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        },
    ]

    for payload in payloads:
        assert client.post('/webhooks/whatsapp', json=payload).status_code == 200

    stats = client.get('/ops/whatsapp-webhook/stats')
    assert stats.status_code == 200
    body = stats.json()
    group_metrics = body['group_metrics']
    assert group_metrics['unique_group_ids'] == 1
    assert group_metrics['unique_group_participant_wa_ids'] == 3
    assert group_metrics['participant_added_count'] == 4
    assert group_metrics['participant_failed_count'] == 1
    assert group_metrics['unique_added_participant_wa_ids'] == 2
    assert group_metrics['unique_failed_participant_wa_ids'] == 1
    assert group_metrics['group_create_count'] == 1
    assert group_metrics['group_settings_update_count'] == 1
    assert group_metrics['group_status_update_count'] == 1
    assert group_metrics['group_suspend_count'] == 1
    assert group_metrics['join_request_count'] == 1
    assert group_metrics['request_count'] == 1
    assert group_metrics['by_group_id']['group_1']['event_count'] == 4
    assert group_metrics['by_group_id']['group_1']['participant_added_count'] == 4
    assert group_metrics['by_group_id']['group_1']['participant_failed_count'] == 1
    assert group_metrics['by_group_id']['group_1']['join_request_count'] == 1
    assert group_metrics['by_group_id']['group_1']['request_count'] == 1
    assert group_metrics['by_group_id']['group_1']['group_create_count'] == 1
    assert group_metrics['by_group_id']['group_1']['group_settings_update_count'] == 1
    assert group_metrics['by_group_id']['group_1']['group_suspend_count'] == 1
    assert body['phone_number_metrics']['shared_pnid']['group_participant_added_count'] == 4
    assert body['phone_number_metrics']['shared_pnid']['group_participant_failed_count'] == 1
    assert body['phone_number_metrics']['shared_pnid']['join_request_count'] == 1


def test_official_group_bridge_approve_endpoint_supports_mock_success_and_ops_history():
    app = create_app({
        'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123',
        'OFFICIAL_GROUP_BRIDGE_TOKEN': 'bridge-token',
        'OFFICIAL_GROUP_BRIDGE_MODE': 'mock_success',
    })
    client = TestClient(app)

    payload = {
        'target_group': 'official-group-a',
        'lead': {'lead_id': 'lead_1', 'mobile': '85200011122'},
        'crm_snapshot': {'id': 'crm_1'},
        'task': {'task_id': 'task_1', 'status': 'pending'},
    }
    response = client.post(
        '/official-group/approve',
        json=payload,
        headers={'Authorization': 'Bearer bridge-token'},
    )
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'success'
    assert body['result_code'] == 'approval_ok'
    assert body['raw_result']['target_group'] == 'official-group-a'
    assert body['raw_result']['bridge_request_id']

    history = client.get('/ops/official-group-bridge/requests')
    assert history.status_code == 200
    history_body = history.json()
    assert history_body['request_count'] == 1
    assert history_body['requests'][0]['request']['target_group'] == 'official-group-a'
    assert history_body['requests'][0]['response']['status'] == 'success'
    assert history_body['requests'][0]['mode'] == 'mock_success'


def test_official_group_bridge_passthrough_routes_to_runtime_worker_payload():
    class StubResponse:
        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    class StubSession:
        def __init__(self):
            self.posts = []
            self.gets = []

        def get(self, url, timeout=None):
            self.gets.append({'url': url, 'timeout': timeout})
            return StubResponse({
                'rows': [{
                    'responsible_type': 'official_group',
                    'enabled': True,
                    'group_link_bindings': [{
                        'enabled': True,
                        'registration_group': 'official-group-a',
                        'group_name': '官方群A',
                        'group_id': '120363400000000001@g.us',
                        'link': 'https://chat.whatsapp.com/runtimeA',
                    }],
                }],
            })

        def post(self, url, json=None, headers=None, timeout=None):
            self.posts.append({'url': url, 'json': json, 'headers': headers, 'timeout': timeout})
            return StubResponse({
                'status': 'success',
                'result_code': 'approved',
                'result_reason': 'runtime approved',
                'raw_result': {
                    'group_id': '120363400000000001@g.us',
                    'group_name': '官方群A',
                },
            })

    session = StubSession()
    app = create_app({
        'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123',
        'OFFICIAL_GROUP_BRIDGE_TOKEN': 'bridge-token',
        'OFFICIAL_GROUP_BRIDGE_MODE': 'passthrough_webhook',
        'OFFICIAL_GROUP_BRIDGE_UPSTREAM_URL': 'http://127.0.0.1:63568/approve',
        'OFFICIAL_GROUP_BRIDGE_SESSION': session,
        'OFFICIAL_GROUP_BRIDGE_DB_PATH': '/tmp/nonexistent-official-bridge-test.db',
        'OFFICIAL_GROUP_BRIDGE_CONSOLE_BASE_URL': 'http://127.0.0.1:8011',
    })
    client = TestClient(app)

    response = client.post(
        '/official-group/approve',
        json={
            'target_group': 'official-group-a',
            'lead': {'lead_id': 'lead_1', 'mobile': '85200011122', 'name': 'Alice'},
            'crm_snapshot': {'id': 'crm_1', 'mobile': '85200011122'},
            'task': {'task_id': 'task_1', 'status': 'pending'},
        },
        headers={'Authorization': 'Bearer bridge-token'},
    )
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'success'
    assert body['result_code'] == 'approved'
    assert body['raw_result']['bridge_request_id']
    assert session.gets[0]['url'] == 'http://127.0.0.1:8011/api/ops/whatsapp-approval-accounts'
    upstream_request = session.posts[0]
    assert upstream_request['url'] == 'http://127.0.0.1:63568/approve'
    assert upstream_request['json']['registration_group'] == '120363400000000001@g.us'
    assert upstream_request['json']['target_phone_hint'] == '85200011122'
    assert upstream_request['json']['target_name_hint'] == 'Alice'
    assert upstream_request['json']['approval_run_id'] == body['raw_result']['bridge_request_id']


def test_official_group_bridge_manual_queue_supports_manual_resolution():
    app = create_app({
        'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123',
        'OFFICIAL_GROUP_BRIDGE_TOKEN': 'bridge-token',
        'OFFICIAL_GROUP_BRIDGE_MODE': 'manual_queue',
    })
    client = TestClient(app)

    payload = {
        'target_group': 'official-group-b',
        'lead': {'lead_id': 'lead_2'},
        'crm_snapshot': {'id': 'crm_2'},
        'task': {'task_id': 'task_2', 'status': 'pending'},
    }
    response = client.post(
        '/official-group/approve',
        json=payload,
        headers={'Authorization': 'Bearer bridge-token'},
    )
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'manual_required'
    request_id = body['raw_result']['bridge_request_id']

    pending = client.get('/ops/official-group-bridge/requests?status=pending')
    assert pending.status_code == 200
    pending_body = pending.json()
    assert pending_body['request_count'] == 1
    assert pending_body['requests'][0]['request_id'] == request_id
    assert pending_body['requests'][0]['status'] == 'pending'

    resolution = client.post(
        f'/ops/official-group-bridge/requests/{request_id}/resolve',
        json={'status': 'success', 'result_code': 'approved_by_operator', 'result_reason': 'manual pass'},
    )
    assert resolution.status_code == 200
    resolved = resolution.json()
    assert resolved['request_id'] == request_id
    assert resolved['status'] == 'resolved'
    assert resolved['resolution']['status'] == 'success'


def test_official_group_bridge_health_exposes_mode_and_token_requirement():
    app = create_app({
        'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123',
        'OFFICIAL_GROUP_BRIDGE_TOKEN': 'bridge-token',
        'OFFICIAL_GROUP_BRIDGE_MODE': 'mock_retryable_failed',
    })
    client = TestClient(app)

    response = client.get('/ops/official-group-bridge/health')
    assert response.status_code == 200
    body = response.json()
    assert body['provider'] == 'official-group-bridge'
    assert body['mode'] == 'mock_retryable_failed'
    assert body['has_token'] is True
    assert body['schema_version'] == 'official-group-webhook-v1'
    assert 'approve' in body['supports']


def test_official_group_bridge_defaults_to_manual_queue_when_mode_not_configured():
    app = create_app({'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123'})
    client = TestClient(app)

    response = client.get('/ops/official-group-bridge/health')
    assert response.status_code == 200
    body = response.json()
    assert body['mode'] == 'manual_queue'


def test_official_group_bridge_requests_support_filters_detail_and_resolution_audit_fields():
    app = create_app({
        'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123',
        'OFFICIAL_GROUP_BRIDGE_MODE': 'manual_queue',
    })
    client = TestClient(app)

    payload_a = {
        'target_group': 'official-group-a',
        'lead': {'lead_id': 'lead_a', 'mobile': '85200011122'},
        'crm_snapshot': {'id': 'crm_a'},
        'task': {'task_id': 'task_a', 'status': 'pending'},
    }
    payload_b = {
        'target_group': 'official-group-b',
        'lead': {'lead_id': 'lead_b', 'mobile': '85200033344'},
        'crm_snapshot': {'id': 'crm_b'},
        'task': {'task_id': 'task_b', 'status': 'pending'},
    }
    first = client.post('/official-group/approve', json=payload_a)
    second = client.post('/official-group/approve', json=payload_b)
    assert first.status_code == 200
    assert second.status_code == 200
    request_id = first.json()['raw_result']['bridge_request_id']

    filtered = client.get('/ops/official-group-bridge/requests?status=pending&target_group=official-group-a&lead_id=lead_a&limit=1')
    assert filtered.status_code == 200
    filtered_body = filtered.json()
    assert filtered_body['request_count'] == 1
    assert filtered_body['total_count'] == 1
    assert filtered_body['requests'][0]['request_id'] == request_id

    detail = client.get(f'/ops/official-group-bridge/requests/{request_id}')
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body['request_id'] == request_id
    assert detail_body['request']['target_group'] == 'official-group-a'
    assert detail_body['status'] == 'pending'

    resolution = client.post(
        f'/ops/official-group-bridge/requests/{request_id}/resolve',
        json={
            'status': 'success',
            'result_code': 'approved_by_operator',
            'result_reason': 'manual pass',
            'resolved_by': 'ou_xxx',
            'resolved_by_name': 'Bridge Operator',
            'note': 'checked crm and approved',
        },
    )
    assert resolution.status_code == 200
    resolution_body = resolution.json()
    assert resolution_body['resolution']['resolved_by'] == 'ou_xxx'
    assert resolution_body['resolution']['resolved_by_name'] == 'Bridge Operator'
    assert resolution_body['resolution']['note'] == 'checked crm and approved'

    detail_after = client.get(f'/ops/official-group-bridge/requests/{request_id}')
    assert detail_after.status_code == 200
    assert detail_after.json()['resolution']['resolved_by_name'] == 'Bridge Operator'


def test_official_group_bridge_resolution_defaults_match_status_when_fields_omitted():
    app = create_app({
        'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123',
        'OFFICIAL_GROUP_BRIDGE_MODE': 'manual_queue',
    })
    client = TestClient(app)

    response = client.post('/official-group/approve', json={
        'target_group': 'official-group-a',
        'lead': {'lead_id': 'lead_a'},
        'crm_snapshot': {'id': 'crm_a'},
        'task': {'task_id': 'task_a', 'status': 'pending'},
    })
    assert response.status_code == 200
    request_id = response.json()['raw_result']['bridge_request_id']

    manual_resolution = client.post(
        f'/ops/official-group-bridge/requests/{request_id}/resolve',
        json={'status': 'manual_required'},
    )
    assert manual_resolution.status_code == 200
    manual_body = manual_resolution.json()
    assert manual_body['resolution']['result_code'] == 'manual_follow_up_required'
    assert manual_body['resolution']['result_reason'] == 'manual follow-up required'

    detail = client.get(f'/ops/official-group-bridge/requests/{request_id}').json()
    assert detail['response']['result_code'] == 'manual_follow_up_required'
    assert detail['response']['raw_result']['execution_disposition'] == 'manual_required'
    assert detail['response']['raw_result']['requires_human_action'] is True


def test_official_group_bridge_dashboard_page_loads():
    app = create_app({
        'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123',
        'OFFICIAL_GROUP_BRIDGE_CONSOLE_BASE_URL': 'http://127.0.0.1:8011',
    })
    client = TestClient(app)

    response = client.get('/ops/official-group-bridge')
    assert response.status_code == 200
    text = response.text
    assert '官方群审批桥接台' in text
    assert 'page-shell' in text
    assert 'shell-nav' in text
    assert '桥接概况' in text
    assert '请求队列' in text
    assert '处理面板' in text
    assert '/ops/official-group-bridge/requests' in text
    assert '/ops/official-group-bridge/summary' in text
    assert '待处理请求' in text
    assert '待处理超时请求' in text
    assert 'approved_by_operator' in text
    assert 'rejected_by_operator' in text
    assert 'manual_follow_up_required' in text
    assert 'applyResolutionTemplate' in text
    assert 'http://127.0.0.1:8011/ops' in text
    assert 'http://127.0.0.1:8011/ops/intake-bot-presets' in text
    assert 'http://127.0.0.1:8011/ops/production-ops' in text
    assert 'http://127.0.0.1:8011/ops/official-group-bridge' in text


def test_official_group_bridge_summary_hides_resolved_only_historical_targets():
    app = create_app({
        'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123',
        'OFFICIAL_GROUP_BRIDGE_MODE': 'manual_queue',
    })
    client = TestClient(app)

    response = client.post('/official-group/approve', json={
        'target_group': 'official-group-old',
        'lead': {'lead_id': 'lead_old_only'},
        'crm_snapshot': {'id': 'crm_old_only'},
        'task': {'task_id': 'task_old_only', 'status': 'pending'},
    })
    assert response.status_code == 200
    request_id = response.json()['raw_result']['bridge_request_id']
    resolved = client.post(
        f'/ops/official-group-bridge/requests/{request_id}/resolve',
        json={'status': 'failed', 'result_code': 'stale_request_superseded', 'result_reason': 'stale'},
    )
    assert resolved.status_code == 200

    summary = client.get('/ops/official-group-bridge/summary')
    assert summary.status_code == 200
    body = summary.json()
    assert body['view_scope'] == 'current_window'
    assert body['total_count'] == 0
    assert body['pending_count'] == 0
    assert body['resolved_count'] == 0
    assert body['by_target_group'] == {}



def test_official_group_bridge_summary_and_sorted_requests_expose_timeout_and_trend_metrics():
    app = create_app({
        'WHATSAPP_WEBHOOK_VERIFY_TOKEN': 'token-123',
        'OFFICIAL_GROUP_BRIDGE_MODE': 'manual_queue',
    })
    client = TestClient(app)

    first = client.post('/official-group/approve', json={
        'target_group': 'official-group-a',
        'lead': {'lead_id': 'lead_old'},
        'crm_snapshot': {'id': 'crm_old'},
        'task': {'task_id': 'task_old', 'status': 'pending'},
    })
    second = client.post('/official-group/approve', json={
        'target_group': 'official-group-a',
        'lead': {'lead_id': 'lead_new'},
        'crm_snapshot': {'id': 'crm_new'},
        'task': {'task_id': 'task_new', 'status': 'pending'},
    })
    assert first.status_code == 200
    assert second.status_code == 200
    old_request_id = first.json()['raw_result']['bridge_request_id']
    new_request_id = second.json()['raw_result']['bridge_request_id']

    record = app.state.official_group_bridge_state.get_request(old_request_id)
    record['created_at'] = 100
    record['updated_at'] = 100

    summary = client.get('/ops/official-group-bridge/summary')
    assert summary.status_code == 200
    body = summary.json()
    assert body['pending_count'] == 2
    assert body['resolved_count'] == 0
    assert body['pending_timeout_over_1h_count'] == 1
    assert body['by_target_group']['official-group-a']['pending_count'] == 2
    assert body['today_created_count'] == 1

    sorted_requests = client.get('/ops/official-group-bridge/requests?sort_by=updated_at&sort_order=desc&limit=1')
    assert sorted_requests.status_code == 200
    sorted_body = sorted_requests.json()
    assert sorted_body['request_count'] == 1
    assert sorted_body['total_count'] == 2
    assert sorted_body['requests'][0]['request_id'] == new_request_id
