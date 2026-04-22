from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections import Counter, defaultdict, deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Set

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


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
        added_participant_wa_ids: List[str] = []
        failed_participant_wa_ids: List[str] = []
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
                        participant_wa_id = participant.get('wa_id')
                        wa_ids.append(participant_wa_id)
                        added_participant_wa_ids.append(participant_wa_id)
                    for participant in group.get('failed_participants') or []:
                        participant_wa_id = participant.get('wa_id')
                        wa_ids.append(participant_wa_id)
                        failed_participant_wa_ids.append(participant_wa_id)

        fields = _unique_preserving_order(fields)
        phone_number_ids = _unique_preserving_order(phone_number_ids)
        display_phone_numbers = _unique_preserving_order(display_phone_numbers)
        group_ids = _unique_preserving_order(group_ids)
        group_event_types = _unique_preserving_order(group_event_types)
        request_ids = _unique_preserving_order(request_ids)
        join_request_ids = _unique_preserving_order(join_request_ids)
        wa_ids = _unique_preserving_order(wa_ids)
        added_participant_wa_ids = _unique_preserving_order(added_participant_wa_ids)
        failed_participant_wa_ids = _unique_preserving_order(failed_participant_wa_ids)
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
            'added_participant_wa_ids': added_participant_wa_ids,
            'failed_participant_wa_ids': failed_participant_wa_ids,
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
        added_participant_wa_ids: Set[str] = set()
        failed_participant_wa_ids: Set[str] = set()
        request_ids: Set[str] = set()
        join_request_ids: Set[str] = set()
        dedupe_keys: Set[str] = set()
        group_metrics_by_type = Counter()
        group_metrics_by_group_id: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                'event_count': 0,
                'fields': set(),
                'event_types': set(),
                'participant_added_count': 0,
                'participant_failed_count': 0,
                'added_participant_wa_ids': set(),
                'failed_participant_wa_ids': set(),
                'request_ids': set(),
                'join_request_ids': set(),
                'group_create_count': 0,
                'group_settings_update_count': 0,
                'group_status_update_count': 0,
                'group_suspend_count': 0,
            }
        )
        phone_number_metrics: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                'event_count': 0,
                'display_phone_numbers': set(),
                'fields': set(),
                'message_event_count': 0,
                'group_event_count': 0,
                'group_participant_added_count': 0,
                'group_participant_failed_count': 0,
                'request_ids': set(),
                'join_request_ids': set(),
                'dedupe_keys': set(),
            }
        )

        for record in records:
            normalized = record['normalized']
            payload = record['payload'] or {}
            field = normalized.get('field') or 'unknown'
            by_field[field] += 1
            dedupe_keys.add(normalized['dedupe_key'])
            message_ids.update(normalized.get('message_ids') or [])
            message_senders.update(normalized.get('message_senders') or [])
            group_ids.update(normalized.get('group_ids') or [])
            if field.startswith('group_'):
                group_participant_wa_ids.update(normalized.get('wa_ids') or [])
            added_participant_wa_ids.update(normalized.get('added_participant_wa_ids') or [])
            failed_participant_wa_ids.update(normalized.get('failed_participant_wa_ids') or [])
            request_ids.update(normalized.get('request_ids') or [])
            join_request_ids.update(normalized.get('join_request_ids') or [])

            phone_number_id = normalized.get('phone_number_id') or 'unknown'
            phone_bucket = phone_number_metrics[phone_number_id]
            phone_bucket['event_count'] += 1
            phone_bucket['display_phone_numbers'].update(normalized.get('display_phone_numbers') or [])
            phone_bucket['fields'].update(normalized.get('fields') or [])
            phone_bucket['dedupe_keys'].add(normalized['dedupe_key'])
            phone_bucket['request_ids'].update(normalized.get('request_ids') or [])
            phone_bucket['join_request_ids'].update(normalized.get('join_request_ids') or [])
            if field == 'messages':
                phone_bucket['message_event_count'] += 1
            if field.startswith('group_'):
                phone_bucket['group_event_count'] += 1
                phone_bucket['group_participant_added_count'] += len(normalized.get('added_participant_wa_ids') or [])
                phone_bucket['group_participant_failed_count'] += len(normalized.get('failed_participant_wa_ids') or [])

            for entry in payload.get('entry') or []:
                for change in entry.get('changes') or []:
                    change_field = change.get('field') or field
                    value = change.get('value') or {}
                    for group in value.get('groups') or []:
                        group_id = group.get('group_id') or 'unknown'
                        group_type = group.get('type') or 'unknown'
                        added_ids = [p.get('wa_id') for p in group.get('added_participants') or [] if p.get('wa_id')]
                        failed_ids = [p.get('wa_id') for p in group.get('failed_participants') or [] if p.get('wa_id')]
                        group_bucket = group_metrics_by_group_id[group_id]
                        group_bucket['event_count'] += 1
                        group_bucket['fields'].add(change_field)
                        group_bucket['event_types'].add(group_type)
                        group_bucket['participant_added_count'] += len(added_ids)
                        group_bucket['participant_failed_count'] += len(failed_ids)
                        group_bucket['added_participant_wa_ids'].update(added_ids)
                        group_bucket['failed_participant_wa_ids'].update(failed_ids)
                        if group.get('request_id'):
                            group_bucket['request_ids'].add(group.get('request_id'))
                        if group.get('join_request_id'):
                            group_bucket['join_request_ids'].add(group.get('join_request_id'))
                        if change_field == 'group_lifecycle_update' and group_type == 'group_create':
                            group_bucket['group_create_count'] += 1
                        if change_field == 'group_settings_update':
                            group_bucket['group_settings_update_count'] += 1
                        if change_field == 'group_status_update':
                            group_bucket['group_status_update_count'] += 1
                        if group_type == 'group_suspend':
                            group_bucket['group_suspend_count'] += 1
                        group_metrics_by_type[group_type] += 1

        serializable_phone_metrics = {}
        for phone_number_id, bucket in phone_number_metrics.items():
            serializable_phone_metrics[phone_number_id] = {
                'event_count': bucket['event_count'],
                'display_phone_numbers': sorted(bucket['display_phone_numbers']),
                'fields': sorted(bucket['fields']),
                'message_event_count': bucket['message_event_count'],
                'group_event_count': bucket['group_event_count'],
                'group_participant_added_count': bucket['group_participant_added_count'],
                'group_participant_failed_count': bucket['group_participant_failed_count'],
                'request_count': len(bucket['request_ids']),
                'join_request_count': len(bucket['join_request_ids']),
                'dedupe_count': len(bucket['dedupe_keys']),
            }

        serializable_group_metrics_by_group_id = {}
        for group_id, bucket in group_metrics_by_group_id.items():
            serializable_group_metrics_by_group_id[group_id] = {
                'event_count': bucket['event_count'],
                'fields': sorted(bucket['fields']),
                'event_types': sorted(bucket['event_types']),
                'participant_added_count': bucket['participant_added_count'],
                'participant_failed_count': bucket['participant_failed_count'],
                'unique_added_participant_wa_ids': len(bucket['added_participant_wa_ids']),
                'unique_failed_participant_wa_ids': len(bucket['failed_participant_wa_ids']),
                'request_count': len(bucket['request_ids']),
                'join_request_count': len(bucket['join_request_ids']),
                'group_create_count': bucket['group_create_count'],
                'group_settings_update_count': bucket['group_settings_update_count'],
                'group_status_update_count': bucket['group_status_update_count'],
                'group_suspend_count': bucket['group_suspend_count'],
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
                'participant_added_count': sum(bucket['participant_added_count'] for bucket in group_metrics_by_group_id.values()),
                'participant_failed_count': sum(bucket['participant_failed_count'] for bucket in group_metrics_by_group_id.values()),
                'unique_added_participant_wa_ids': len(added_participant_wa_ids),
                'unique_failed_participant_wa_ids': len(failed_participant_wa_ids),
                'request_count': len(request_ids),
                'join_request_count': len(join_request_ids),
                'group_create_count': group_metrics_by_type.get('group_create', 0),
                'group_settings_update_count': by_field.get('group_settings_update', 0),
                'group_status_update_count': by_field.get('group_status_update', 0),
                'group_suspend_count': group_metrics_by_type.get('group_suspend', 0),
                'by_group_id': serializable_group_metrics_by_group_id,
            },
            'phone_number_metrics': serializable_phone_metrics,
        }


class OfficialGroupBridgeState:
    def __init__(
        self,
        *,
        token: Optional[str] = None,
        mode: str = 'mock_success',
        max_requests: int = 200,
        upstream_url: Optional[str] = None,
        upstream_token: Optional[str] = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.token = str(token or '').strip() or None
        self.mode = str(mode or 'mock_success').strip().lower() or 'mock_success'
        self.max_requests = max(10, int(max_requests or 200))
        self.recent_requests: Deque[Dict[str, Any]] = deque(maxlen=self.max_requests)
        self.timeout_seconds = max(3.0, float(timeout_seconds or 20.0))
        self.upstream_url = str(upstream_url or '').strip() or None
        self.upstream_token = str(upstream_token or '').strip() or None
        self.session = requests.Session() if requests is not None else None

    def _require_auth(self, authorization: Optional[str]) -> None:
        if not self.token:
            return
        expected = f'Bearer {self.token}'
        if (authorization or '').strip() != expected:
            raise HTTPException(status_code=401, detail='unauthorized official-group bridge request')

    def _new_request_id(self, lead: Dict[str, Any]) -> str:
        lead_id = str(lead.get('lead_id') or 'unknown').strip() or 'unknown'
        return f'bridge_{lead_id}_{uuid.uuid4().hex[:10]}'

    def _store(self, record: Dict[str, Any]) -> None:
        self.recent_requests.append(record)

    def _find(self, request_id: str) -> Optional[Dict[str, Any]]:
        for record in reversed(self.recent_requests):
            if record.get('request_id') == request_id:
                return record
        return None

    def health(self) -> Dict[str, Any]:
        supports = ['approve', 'ops_history', 'manual_resolution']
        if self.mode == 'passthrough_webhook':
            supports.append('upstream_passthrough')
        return {
            'provider': 'official-group-bridge',
            'status': 'healthy' if self.mode != 'passthrough_webhook' or bool(self.upstream_url) else 'misconfigured',
            'mode': self.mode,
            'has_token': bool(self.token),
            'supports': supports,
            'schema_version': 'official-group-webhook-v1',
            'recent_request_count': len(self.recent_requests),
            'upstream_url_configured': bool(self.upstream_url),
            'timeout_seconds': self.timeout_seconds,
        }

    def _mock_response(self, *, request_id: str, target_group: str) -> Dict[str, Any]:
        if self.mode == 'mock_retryable_failed':
            return {
                'status': 'retryable_failed',
                'result_code': 'bridge_timeout',
                'result_reason': 'mock upstream timeout',
                'raw_result': {'target_group': target_group, 'bridge_request_id': request_id},
            }
        if self.mode == 'manual_queue':
            return {
                'status': 'manual_required',
                'result_code': 'manual_queue_pending',
                'result_reason': 'queued for manual bridge resolution',
                'raw_result': {'target_group': target_group, 'bridge_request_id': request_id},
            }
        return {
            'status': 'success',
            'result_code': 'approval_ok',
            'result_reason': 'mock bridge approved official group request',
            'raw_result': {'target_group': target_group, 'bridge_request_id': request_id},
        }

    def _passthrough_response(self, payload: Dict[str, Any], *, request_id: str) -> Dict[str, Any]:
        if not self.upstream_url:
            return {
                'status': 'manual_required',
                'result_code': 'upstream_not_configured',
                'result_reason': 'upstream webhook url is not configured',
                'raw_result': {'target_group': payload.get('target_group'), 'bridge_request_id': request_id},
            }
        if self.session is None:
            return {
                'status': 'manual_required',
                'result_code': 'requests_unavailable',
                'result_reason': 'requests package unavailable for passthrough mode',
                'raw_result': {'target_group': payload.get('target_group'), 'bridge_request_id': request_id},
            }
        headers = {'Content-Type': 'application/json'}
        if self.upstream_token:
            headers['Authorization'] = f'Bearer {self.upstream_token}'
        response = self.session.post(self.upstream_url, json=payload, headers=headers, timeout=self.timeout_seconds)
        try:
            body = response.json()
        except Exception as exc:
            return {
                'status': 'manual_required',
                'result_code': 'upstream_invalid_response',
                'result_reason': f'upstream returned non-json response: {exc}',
                'raw_result': {'target_group': payload.get('target_group'), 'bridge_request_id': request_id},
            }
        if not isinstance(body, dict):
            return {
                'status': 'manual_required',
                'result_code': 'upstream_invalid_response',
                'result_reason': 'upstream returned non-object response',
                'raw_result': {'target_group': payload.get('target_group'), 'bridge_request_id': request_id},
            }
        raw_result = dict(body.get('raw_result') or {})
        raw_result.setdefault('bridge_request_id', request_id)
        raw_result.setdefault('target_group', payload.get('target_group'))
        body['raw_result'] = raw_result
        body.setdefault('status', 'failed')
        body.setdefault('result_code', 'upstream_failed')
        body.setdefault('result_reason', '')
        return body

    def approve(self, payload: Dict[str, Any], *, authorization: Optional[str]) -> Dict[str, Any]:
        self._require_auth(authorization)
        target_group = str(payload.get('target_group') or '').strip()
        if not target_group:
            raise HTTPException(status_code=400, detail='target_group is required')
        lead = payload.get('lead') or {}
        crm_snapshot = payload.get('crm_snapshot') or {}
        task = payload.get('task') or {}
        if not isinstance(lead, dict) or not isinstance(crm_snapshot, dict) or not isinstance(task, dict):
            raise HTTPException(status_code=400, detail='lead, crm_snapshot, and task must be objects')
        request_id = self._new_request_id(lead)
        now = int(time.time())
        request_payload = {
            'schema_version': 'official-group-webhook-v1',
            'target_group': target_group,
            'lead': dict(lead),
            'crm_snapshot': dict(crm_snapshot),
            'task': dict(task),
        }
        if self.mode == 'passthrough_webhook':
            response_payload = self._passthrough_response(request_payload, request_id=request_id)
            status = 'resolved'
        else:
            response_payload = self._mock_response(request_id=request_id, target_group=target_group)
            status = 'pending' if self.mode == 'manual_queue' else 'resolved'
        record = {
            'request_id': request_id,
            'created_at': now,
            'updated_at': now,
            'status': status,
            'mode': self.mode,
            'request': request_payload,
            'response': response_payload,
            'resolution': None,
        }
        self._store(record)
        return response_payload

    def list_requests(self, *, status: Optional[str] = None) -> Dict[str, Any]:
        records = list(reversed(self.recent_requests))
        if status:
            records = [record for record in records if str(record.get('status') or '') == str(status)]
        return {
            'request_count': len(records),
            'requests': records,
        }

    def resolve_request(self, request_id: str, resolution: Dict[str, Any]) -> Dict[str, Any]:
        record = self._find(request_id)
        if record is None:
            raise HTTPException(status_code=404, detail='bridge request not found')
        status = str(resolution.get('status') or '').strip().lower()
        if status not in {'success', 'retryable_failed', 'manual_required', 'failed'}:
            raise HTTPException(status_code=400, detail='resolution status must be success|retryable_failed|manual_required|failed')
        record['status'] = 'resolved'
        record['updated_at'] = int(time.time())
        record['resolution'] = {
            'status': status,
            'result_code': str(resolution.get('result_code') or '').strip(),
            'result_reason': str(resolution.get('result_reason') or '').strip(),
        }
        record['response'] = {
            'status': status,
            'result_code': record['resolution']['result_code'] or 'manual_resolution',
            'result_reason': record['resolution']['result_reason'],
            'raw_result': {
                'target_group': record['request']['target_group'],
                'bridge_request_id': request_id,
                'resolution_source': 'ops_manual_resolution',
            },
        }
        return {
            'request_id': request_id,
            'status': 'resolved',
            'resolution': record['resolution'],
        }


def create_app(settings: Optional[Dict[str, Any]] = None) -> FastAPI:
    cfg = dict(settings or {})
    verify_token = cfg.get('WHATSAPP_WEBHOOK_VERIFY_TOKEN') or os.getenv('WHATSAPP_WEBHOOK_VERIFY_TOKEN')
    webhook_state = WebhookState()
    bridge_state = OfficialGroupBridgeState(
        token=cfg.get('OFFICIAL_GROUP_BRIDGE_TOKEN') or os.getenv('OFFICIAL_GROUP_BRIDGE_TOKEN'),
        mode=cfg.get('OFFICIAL_GROUP_BRIDGE_MODE') or os.getenv('OFFICIAL_GROUP_BRIDGE_MODE') or 'manual_queue',
        max_requests=int(cfg.get('OFFICIAL_GROUP_BRIDGE_MAX_REQUESTS') or os.getenv('OFFICIAL_GROUP_BRIDGE_MAX_REQUESTS') or 200),
        upstream_url=cfg.get('OFFICIAL_GROUP_BRIDGE_UPSTREAM_URL') or os.getenv('OFFICIAL_GROUP_BRIDGE_UPSTREAM_URL'),
        upstream_token=cfg.get('OFFICIAL_GROUP_BRIDGE_UPSTREAM_TOKEN') or os.getenv('OFFICIAL_GROUP_BRIDGE_UPSTREAM_TOKEN'),
        timeout_seconds=float(cfg.get('OFFICIAL_GROUP_BRIDGE_TIMEOUT_SECONDS') or os.getenv('OFFICIAL_GROUP_BRIDGE_TIMEOUT_SECONDS') or 20.0),
    )

    app = FastAPI(title='WhatsApp Webhook Bridge')

    @app.get('/healthz')
    def healthz() -> Dict[str, Any]:
        return {
            'ok': True,
            'verify_token_configured': bool(verify_token),
            'has_latest_event': webhook_state.latest_event is not None,
            'official_group_bridge': bridge_state.health(),
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
        webhook_state.record(payload)
        entries = payload.get('entry') or []
        return {
            'received': True,
            'event_count': len(entries),
            'summary': webhook_state.latest_summary(),
            'normalized': webhook_state.normalize(payload),
        }

    @app.get('/ops/whatsapp-webhook/latest')
    def latest_event() -> Dict[str, Any]:
        return {
            'has_event': webhook_state.latest_event is not None,
            'summary': webhook_state.latest_summary() if webhook_state.latest_event is not None else None,
            'normalized': webhook_state.normalize(webhook_state.latest_event) if webhook_state.latest_event is not None else None,
            'payload': webhook_state.latest_event,
        }

    @app.get('/ops/whatsapp-webhook/recent')
    def recent_events() -> Dict[str, Any]:
        events = webhook_state.recent_records()
        return {
            'event_count': len(events),
            'max_events': webhook_state.max_events,
            'events': events,
        }

    @app.get('/ops/whatsapp-webhook/stats')
    def webhook_stats() -> Dict[str, Any]:
        return webhook_state.stats()

    @app.post('/official-group/approve')
    async def official_group_approve(request: Request, authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail='payload must be a JSON object')
        return bridge_state.approve(payload, authorization=authorization)

    @app.get('/ops/official-group-bridge/health')
    def official_group_bridge_health() -> Dict[str, Any]:
        return bridge_state.health()

    @app.get('/ops/official-group-bridge/requests')
    def official_group_bridge_requests(status: Optional[str] = None) -> Dict[str, Any]:
        return bridge_state.list_requests(status=status)

    @app.post('/ops/official-group-bridge/requests/{request_id}/resolve')
    async def official_group_bridge_resolve(request_id: str, request: Request) -> Dict[str, Any]:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail='payload must be a JSON object')
        return bridge_state.resolve_request(request_id, payload)

    return app
