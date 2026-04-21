from __future__ import annotations

import json
import os
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response


class WebhookState:
    def __init__(self, max_events: int = 50) -> None:
        self.max_events = max_events
        self.latest_event: Optional[Dict[str, Any]] = None
        self.recent_events: Deque[Dict[str, Any]] = deque(maxlen=max_events)

    def record(self, payload: Dict[str, Any]) -> None:
        self.latest_event = payload
        self.recent_events.append(payload)

    def summarize(self, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        payload = payload or {}
        entries = payload.get('entry') or []
        changes = []
        for entry in entries:
            changes.extend(entry.get('changes') or [])
        message_count = 0
        display_phone_number = None
        phone_number_id = None
        for change in changes:
            value = change.get('value') or {}
            metadata = value.get('metadata') or {}
            if display_phone_number is None:
                display_phone_number = metadata.get('display_phone_number')
            if phone_number_id is None:
                phone_number_id = metadata.get('phone_number_id')
            message_count += len(value.get('messages') or [])
        return {
            'object': payload.get('object'),
            'entry_count': len(entries),
            'change_count': len(changes),
            'message_count': message_count,
            'display_phone_number': display_phone_number,
            'phone_number_id': phone_number_id,
        }

    def latest_summary(self) -> Dict[str, Any]:
        return self.summarize(self.latest_event)

    def recent_payloads(self) -> List[Dict[str, Any]]:
        return list(reversed(self.recent_events))


def create_app(settings: Optional[Dict[str, Any]] = None) -> FastAPI:
    cfg = dict(settings or {})
    verify_token = cfg.get('WHATSAPP_WEBHOOK_VERIFY_TOKEN') or os.getenv('WHATSAPP_WEBHOOK_VERIFY_TOKEN')
    state = WebhookState()

    app = FastAPI(title='WhatsApp Webhook Bridge')

    @app.get('/healthz')
    def healthz() -> Dict[str, Any]:
        return {
            'ok': True,
            'verify_token_configured': bool(verify_token),
            'has_latest_event': state.latest_event is not None,
        }

    @app.get('/webhooks/whatsapp')
    def verify_webhook(
        hub_mode: str = Query(alias='hub.mode'),
        hub_verify_token: str = Query(alias='hub.verify_token'),
        hub_challenge: str = Query(alias='hub.challenge'),
    ) -> Response:
        if hub_mode != 'subscribe':
            raise HTTPException(status_code=400, detail='unsupported hub.mode')
        if not verify_token or hub_verify_token != verify_token:
            raise HTTPException(status_code=403, detail='invalid verify token')
        return Response(content=str(hub_challenge), media_type='text/plain')

    @app.post('/webhooks/whatsapp')
    async def receive_webhook(request: Request) -> Dict[str, Any]:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail='payload must be a JSON object')
        state.record(payload)
        entries = payload.get('entry') or []
        return {
            'received': True,
            'event_count': len(entries),
            'summary': state.latest_summary(),
        }

    @app.get('/ops/whatsapp-webhook/latest')
    def latest_event() -> Dict[str, Any]:
        return {
            'has_event': state.latest_event is not None,
            'summary': state.latest_summary() if state.latest_event is not None else None,
            'payload': state.latest_event,
        }

    @app.get('/ops/whatsapp-webhook/recent')
    def recent_events() -> Dict[str, Any]:
        events = [
            {
                'summary': state.summarize(payload),
                'payload': payload,
            }
            for payload in state.recent_payloads()
        ]
        return {
            'event_count': len(events),
            'max_events': state.max_events,
            'events': events,
        }

    return app
