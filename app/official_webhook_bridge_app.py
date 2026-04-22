from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections import Counter, defaultdict, deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Set

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse

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


OFFICIAL_GROUP_BRIDGE_PAGE_HTML = """
<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>官方群审批桥接操作台</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 24px; background: #f6f8fb; color: #111827; }
    h1 { margin: 0 0 8px 0; }
    .muted { color: #6b7280; font-size: 13px; }
    .card { background: #fff; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.08); padding: 16px; margin-top: 16px; }
    .toolbar { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-top: 12px; }
    input, select, textarea { width: 100%; box-sizing: border-box; padding: 8px 10px; border: 1px solid #d1d5db; border-radius: 8px; font-size: 14px; }
    button { padding: 8px 12px; border: none; border-radius: 8px; background: #2563eb; color: #fff; cursor: pointer; }
    button.secondary { background: #374151; }
    button.success { background: #047857; }
    button.warn { background: #b45309; }
    .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
    .summary-item { border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px; background: #fafbff; }
    .summary-item .label { color: #6b7280; font-size: 12px; }
    .summary-item .value { font-size: 20px; font-weight: 700; margin-top: 6px; }
    .layout { display: grid; grid-template-columns: minmax(420px, 1.2fr) minmax(360px, 1fr); gap: 16px; align-items: start; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 760px; }
    th, td { text-align: left; padding: 10px; border-bottom: 1px solid #e5e7eb; font-size: 13px; vertical-align: top; }
    th { background: #eef2ff; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; background: #e5e7eb; }
    .pill.pending { background: #fef3c7; color: #92400e; }
    .pill.resolved { background: #d1fae5; color: #065f46; }
    pre { white-space: pre-wrap; word-break: break-word; background: #0f172a; color: #e2e8f0; padding: 12px; border-radius: 10px; font-size: 12px; max-height: 240px; overflow: auto; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  </style>
</head>
<body>
  <h1>官方群审批桥接操作台</h1>
  <div class=\"muted\">用于长期运营 manual_queue 请求池，支持待处理筛选、详情查看和人工 resolve。接口基于 /ops/official-group-bridge/requests。</div>
  <div class=\"muted\" style=\"margin-top:8px;\"><a href=\"/ops\">返回主运营台</a></div>

  <div class=\"card\">
    <div class=\"summary-grid\">
      <div class=\"summary-item\"><div class=\"label\">待处理请求</div><div class=\"value\" id=\"pendingCount\">-</div></div>
      <div class=\"summary-item\"><div class=\"label\">已处理请求</div><div class=\"value\" id=\"resolvedCount\">-</div></div>
      <div class=\"summary-item\"><div class=\"label\">总请求数</div><div class=\"value\" id=\"totalCount\">-</div></div>
      <div class=\"summary-item\"><div class=\"label\">Bridge 模式</div><div class=\"value\" id=\"bridgeMode\">-</div></div>
    </div>
  </div>

  <div class=\"card\">
    <div class=\"toolbar\">
      <div><label class=\"muted\">状态</label><select id=\"filterStatus\"><option value=\"\">全部</option><option value=\"pending\">pending</option><option value=\"resolved\">resolved</option></select></div>
      <div><label class=\"muted\">target_group</label><input id=\"filterTargetGroup\" placeholder=\"official-group-a\" /></div>
      <div><label class=\"muted\">lead_id</label><input id=\"filterLeadId\" placeholder=\"lead_xxx\" /></div>
      <div><label class=\"muted\">request_id</label><input id=\"filterRequestId\" placeholder=\"bridge_xxx\" /></div>
    </div>
    <div class=\"actions\" style=\"margin-top:12px;\">
      <button onclick=\"loadRequests()\">刷新列表</button>
      <button class=\"secondary\" onclick=\"quickPending()\">只看待处理请求</button>
    </div>
  </div>

  <div class=\"layout\">
    <div class=\"card\">
      <h3 style=\"margin-top:0;\">待处理请求</h3>
      <div class=\"table-wrap\">
        <table>
          <thead>
            <tr>
              <th>request_id</th>
              <th>status</th>
              <th>target_group</th>
              <th>lead_id</th>
              <th>mode</th>
              <th>updated_at</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody id=\"requestRows\"></tbody>
        </table>
      </div>
    </div>

    <div class=\"card\">
      <h3 style=\"margin-top:0;\">详情 / 处理</h3>
      <div id=\"detailMeta\" class=\"muted\">选择一条请求查看详情。</div>
      <div style=\"margin-top:12px;\" class=\"actions\">
        <button class=\"success\" onclick=\"resolveCurrent('success')\">通过</button>
        <button class=\"secondary\" onclick=\"resolveCurrent('failed')\">拒绝</button>
        <button class=\"warn\" onclick=\"resolveCurrent('manual_required')\">挂起</button>
      </div>
      <div class=\"toolbar\" style=\"margin-top:12px;\">
        <div><label class=\"muted\">result_code</label><input id=\"resolveCode\" value=\"approved_by_operator\" /></div>
        <div><label class=\"muted\">resolved_by</label><input id=\"resolvedBy\" placeholder=\"ou_xxx / email / operator id\" /></div>
        <div><label class=\"muted\">resolved_by_name</label><input id=\"resolvedByName\" placeholder=\"处理人名称\" /></div>
        <div><label class=\"muted\">note</label><input id=\"resolveNote\" placeholder=\"处理说明\" /></div>
      </div>
      <div style=\"margin-top:12px;\"><label class=\"muted\">result_reason</label><textarea id=\"resolveReason\" rows=\"3\">manual review completed</textarea></div>
      <div style=\"margin-top:12px;\"><div class=\"muted\">请求</div><pre id=\"requestJson\">{}</pre></div>
      <div style=\"margin-top:12px;\"><div class=\"muted\">响应</div><pre id=\"responseJson\">{}</pre></div>
      <div style=\"margin-top:12px;\"><div class=\"muted\">resolution</div><pre id=\"resolutionJson\">null</pre></div>
    </div>
  </div>

<script>
let currentRequestId = null;

function fmtTs(ts) {
  if (!ts) return '-';
  const d = new Date(Number(ts) * 1000);
  return isNaN(d.getTime()) ? String(ts) : d.toLocaleString();
}

function setJson(id, value) {
  document.getElementById(id).textContent = JSON.stringify(value, null, 2);
}

async function fetchJson(url, options) {
  const resp = await fetch(url, options || {});
  const text = await resp.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { throw new Error(text || ('HTTP ' + resp.status)); }
  if (!resp.ok) throw new Error(data.detail || text || ('HTTP ' + resp.status));
  return data;
}

async function loadSummary() {
  const [health, pending, resolved, all] = await Promise.all([
    fetchJson('/ops/official-group-bridge/health'),
    fetchJson('/ops/official-group-bridge/requests?status=pending'),
    fetchJson('/ops/official-group-bridge/requests?status=resolved'),
    fetchJson('/ops/official-group-bridge/requests')
  ]);
  document.getElementById('bridgeMode').textContent = health.mode || '-';
  document.getElementById('pendingCount').textContent = pending.request_count || 0;
  document.getElementById('resolvedCount').textContent = resolved.request_count || 0;
  document.getElementById('totalCount').textContent = all.total_count || all.request_count || 0;
}

function currentQuery() {
  const params = new URLSearchParams();
  const mappings = {
    status: document.getElementById('filterStatus').value,
    target_group: document.getElementById('filterTargetGroup').value.trim(),
    lead_id: document.getElementById('filterLeadId').value.trim(),
    request_id: document.getElementById('filterRequestId').value.trim(),
    limit: '50'
  };
  Object.entries(mappings).forEach(([k, v]) => { if (v) params.set(k, v); });
  const query = params.toString();
  return query ? ('?' + query) : '';
}

async function loadRequests() {
  const data = await fetchJson('/ops/official-group-bridge/requests' + currentQuery());
  const tbody = document.getElementById('requestRows');
  tbody.innerHTML = '';
  for (const row of data.requests) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class=\"mono\">${row.request_id}</td>
      <td><span class=\"pill ${row.status}\">${row.status}</span></td>
      <td>${row.request?.target_group || '-'}</td>
      <td class=\"mono\">${row.request?.lead?.lead_id || '-'}</td>
      <td>${row.mode || '-'}</td>
      <td>${fmtTs(row.updated_at)}</td>
      <td><button onclick=\"showRequest('${row.request_id}')\">查看</button></td>
    `;
    tbody.appendChild(tr);
  }
  if (data.requests.length && !currentRequestId) {
    await showRequest(data.requests[0].request_id);
  }
}

async function showRequest(requestId) {
  currentRequestId = requestId;
  const row = await fetchJson('/ops/official-group-bridge/requests/' + encodeURIComponent(requestId));
  document.getElementById('detailMeta').textContent = `request_id=${row.request_id} | status=${row.status} | target_group=${row.request?.target_group || '-'} | lead_id=${row.request?.lead?.lead_id || '-'}`;
  setJson('requestJson', row.request || {});
  setJson('responseJson', row.response || {});
  setJson('resolutionJson', row.resolution);
}

async function resolveCurrent(status) {
  if (!currentRequestId) {
    alert('请先选择一条请求');
    return;
  }
  const payload = {
    status,
    result_code: document.getElementById('resolveCode').value.trim() || 'manual_resolution',
    result_reason: document.getElementById('resolveReason').value.trim(),
    resolved_by: document.getElementById('resolvedBy').value.trim(),
    resolved_by_name: document.getElementById('resolvedByName').value.trim(),
    note: document.getElementById('resolveNote').value.trim(),
  };
  await fetchJson('/ops/official-group-bridge/requests/' + encodeURIComponent(currentRequestId) + '/resolve', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  await loadSummary();
  await loadRequests();
  await showRequest(currentRequestId);
}

function quickPending() {
  document.getElementById('filterStatus').value = 'pending';
  loadRequests();
}

loadSummary().then(loadRequests).catch(err => {
  document.getElementById('detailMeta').textContent = '加载失败：' + err.message;
});
</script>
</body>
</html>
"""


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

    def list_requests(
        self,
        *,
        status: Optional[str] = None,
        target_group: Optional[str] = None,
        lead_id: Optional[str] = None,
        request_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        records = list(reversed(self.recent_requests))
        if status:
            records = [record for record in records if str(record.get('status') or '') == str(status)]
        if target_group:
            records = [record for record in records if str((record.get('request') or {}).get('target_group') or '') == str(target_group)]
        if lead_id:
            records = [record for record in records if str(((record.get('request') or {}).get('lead') or {}).get('lead_id') or '') == str(lead_id)]
        if request_id:
            records = [record for record in records if str(record.get('request_id') or '') == str(request_id)]
        total_count = len(records)
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        page = records[offset: offset + limit]
        return {
            'request_count': len(page),
            'total_count': total_count,
            'limit': limit,
            'offset': offset,
            'requests': page,
        }

    def get_request(self, request_id: str) -> Dict[str, Any]:
        record = self._find(request_id)
        if record is None:
            raise HTTPException(status_code=404, detail='bridge request not found')
        return record

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
            'resolved_by': str(resolution.get('resolved_by') or '').strip(),
            'resolved_by_name': str(resolution.get('resolved_by_name') or '').strip(),
            'note': str(resolution.get('note') or '').strip(),
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

    @app.get('/ops/official-group-bridge', response_class=HTMLResponse)
    def official_group_bridge_page() -> str:
        return OFFICIAL_GROUP_BRIDGE_PAGE_HTML

    @app.get('/ops/official-group-bridge/health')
    def official_group_bridge_health() -> Dict[str, Any]:
        return bridge_state.health()

    @app.get('/ops/official-group-bridge/requests')
    def official_group_bridge_requests(
        status: Optional[str] = None,
        target_group: Optional[str] = None,
        lead_id: Optional[str] = None,
        request_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        return bridge_state.list_requests(
            status=status,
            target_group=target_group,
            lead_id=lead_id,
            request_id=request_id,
            limit=limit,
            offset=offset,
        )

    @app.get('/ops/official-group-bridge/requests/{request_id}')
    def official_group_bridge_request_detail(request_id: str) -> Dict[str, Any]:
        return bridge_state.get_request(request_id)

    @app.post('/ops/official-group-bridge/requests/{request_id}/resolve')
    async def official_group_bridge_resolve(request_id: str, request: Request) -> Dict[str, Any]:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail='payload must be a JSON object')
        return bridge_state.resolve_request(request_id, payload)

    return app
