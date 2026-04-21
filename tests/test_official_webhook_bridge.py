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
