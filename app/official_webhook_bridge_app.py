from __future__ import annotations

import hashlib
import json
import os
from collections import Counter, defaultdict, deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Set

from fastapi import FastAPI, HTTPException, Query, Request, Response


def _unique_preserving_order(values: Iterable[Any]) -> List[Any]:
    seen: Set[Any] = set()
    ordered: List[Any] = []
    for value in values:
        if value in (None, ''):
            continue
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


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

    def normalize(self, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        payload = payload or {}
        entries = payload.get('entry') or []
        fields: List[str] = []
        phone_number_ids: List[str] = []
        display_phone_numbers: List[str] = []
        group_ids: List[str] = []
        group_event_types: List[str] = []
        request_ids: List[str] = []
        join_request_ids: List[str] = []
        wa_ids: List[str] = []
        message_ids: List[str] = []
        message_senders: List[str] = []
        event_timestamps: List[str] = []

        for entry in entries:
            for change in entry.get('changes') or []:
                field = change.get('field')
                if field:
                    fields.append(field)
                value = change.get('value') or {}
                metadata = value.get('metadata') or {}
                phone_number_ids.append(metadata.get('phone_number_id'))
                display_phone_numbers.append(metadata.get('display_phone_number'))

                for contact in value.get('contacts') or []:
                    wa_ids.append(contact.get('wa_id'))

                for message in value.get('messages') or []:
                    message_ids.append(message.get('id'))
                    message_senders.append(message.get('from'))
                    wa_ids.append(message.get('from'))
                    event_timestamps.append(str(message.get('timestamp')) if message.get('timestamp') is not None else None)

                for group in value.get('groups') or []:
                    group_ids.append(group.get('group_id'))
                    group_event_types.append(group.get('type'))
                    request_ids.append(group.get('request_id'))
                    join_request_ids.append(group.get('join_request_id'))
                    wa_ids.append(group.get('wa_id'))
                    event_timestamps.append(str(group.get('timestamp')) if group.get('timestamp') is not None else None)
                    for participant in group.get('added_participants') or []:
                        wa_ids.append(participant.get('wa_id'))
                    for participant in group.get('failed_participants') or []:
                        wa_ids.append(participant.get('wa_id'))

        fields = _unique_preserving_order(fields)
        phone_number_ids = _unique_preserving_order(phone_number_ids)
        display_phone_numbers = _unique_preserving_order(display_phone_numbers)
        group_ids = _unique_preserving_order(group_ids)
        group_event_types = _unique_preserving_order(group_event_types)
        request_ids = _unique_preserving_order(request_ids)
        join_request_ids = _unique_preserving_order(join_request_ids)
        wa_ids = _unique_preserving_order(wa_ids)
        message_ids = _unique_preserving_order(message_ids)
        message_senders = _unique_preserving_order(message_senders)
        event_timestamps = _unique_preserving_order(event_timestamps)

        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

        dedupe_key_parts = [
            fields[0] if fields else '',
            phone_number_ids[0] if phone_number_ids else '',
            group_ids[0] if group_ids else '',
            group_event_types[0] if group_event_types else '',
            join_request_ids[0] if join_request_ids else '',
            message_ids[0] if message_ids else '',
            wa_ids[0] if wa_ids else '',
            event_timestamps[0] if event_timestamps else '',
        ]
        dedupe_key = '|'.join(dedupe_key_parts)
        if dedupe_key.strip('|') == '':
            dedupe_key = payload_hash

        return {
            'field': fields[0] if fields else None,
            'fields': fields,
            'phone_number_id': phone_number_ids[0] if phone_number_ids else None,
            'phone_number_ids': phone_number_ids,
            'display_phone_number': display_phone_numbers[0] if display_phone_numbers else None,
            'display_phone_numbers': display_phone_numbers,
            'group_ids': group_ids,
            'group_event_types': group_event_types,
            'request_ids': request_ids,
            'join_request_ids': join_request_ids,
            'wa_ids': wa_ids,
            'message_ids': message_ids,
            'message_senders': message_senders,
            'event_timestamps': event_timestamps,
            'payload_hash': payload_hash,
            'dedupe_key': dedupe_key,
        }

    def latest_summary(self) -> Dict[str, Any]:
        return self.summarize(self.latest_event)

    def recent_payloads(self) -> List[Dict[str, Any]]:
        return list(reversed(self.recent_events))

    def recent_records(self) -> List[Dict[str, Any]]:
        return [
            {
                'summary': self.summarize(payload),
                'normalized': self.normalize(payload),
                'payload': payload,
            }
            for payload in self.recent_payloads()
        ]

    def stats(self) -> Dict[str, Any]:
        records = self.recent_records()
        by_field = Counter()
        message_ids: Set[str] = set()
        message_senders: Set[str] = set()
        group_ids: Set[str] = set()
        group_participant_wa_ids: Set[str] = set()
        dedupe_keys: Set[str] = set()
        phone_number_metrics: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                'event_count': 0,
                'display_phone_numbers': set(),
                'fields': set(),
                'message_event_count': 0,
                'group_event_count': 0,
                'dedupe_keys': set(),
            }
        )

        for record in records:
            normalized = record['normalized']
            field = normalized.get('field') or 'unknown'
            by_field[field] += 1
            dedupe_keys.add(normalized['dedupe_key'])
            message_ids.update(normalized.get('message_ids') or [])
            message_senders.update(normalized.get('message_senders') or [])
            group_ids.update(normalized.get('group_ids') or [])
            if field.startswith('group_'):
                group_participant_wa_ids.update(normalized.get('wa_ids') or [])

            phone_number_id = normalized.get('phone_number_id') or 'unknown'
            bucket = phone_number_metrics[phone_number_id]
            bucket['event_count'] += 1
            bucket['display_phone_numbers'].update(normalized.get('display_phone_numbers') or [])
            bucket['fields'].update(normalized.get('fields') or [])
            bucket['dedupe_keys'].add(normalized['dedupe_key'])
            if field == 'messages':
                bucket['message_event_count'] += 1
            if field.startswith('group_'):
                bucket['group_event_count'] += 1

        serializable_phone_metrics = {}
        for phone_number_id, bucket in phone_number_metrics.items():
            serializable_phone_metrics[phone_number_id] = {
                'event_count': bucket['event_count'],
                'display_phone_numbers': sorted(bucket['display_phone_numbers']),
                'fields': sorted(bucket['fields']),
                'message_event_count': bucket['message_event_count'],
                'group_event_count': bucket['group_event_count'],
                'dedupe_count': len(bucket['dedupe_keys']),
            }

        return {
            'total_events': len(records),
            'dedupe_event_count': len(dedupe_keys),
            'by_field': dict(by_field),
            'message_metrics': {
                'unique_message_ids': len(message_ids),
                'unique_message_senders': len(message_senders),
            },
            'group_metrics': {
                'unique_group_ids': len(group_ids),
                'unique_group_participant_wa_ids': len(group_participant_wa_ids),
            },
            'phone_number_metrics': serializable_phone_metrics,
        }


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
            'normalized': state.normalize(payload),
        }

    @app.get('/ops/whatsapp-webhook/latest')
    def latest_event() -> Dict[str, Any]:
        return {
            'has_event': state.latest_event is not None,
            'summary': state.latest_summary() if state.latest_event is not None else None,
            'normalized': state.normalize(state.latest_event) if state.latest_event is not None else None,
            'payload': state.latest_event,
        }

    @app.get('/ops/whatsapp-webhook/recent')
    def recent_events() -> Dict[str, Any]:
        events = state.recent_records()
        return {
            'event_count': len(events),
            'max_events': state.max_events,
            'events': events,
        }

    @app.get('/ops/whatsapp-webhook/stats')
    def webhook_stats() -> Dict[str, Any]:
        return state.stats()

    return app
