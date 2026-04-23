#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONITOR_CMD = [str(ROOT / '.venv' / 'bin' / 'python'), str(ROOT / 'scripts' / 'whatsapp_live_monitor.py'), '--initial-wait-ms', '3000']
OUTPUT_PATH = ROOT / 'next_registration_group_approval_precise_probe.json'
ANCHOR_EPOCH = time.time()
ANCHOR_UTC = datetime.now(timezone.utc).isoformat()
POLL_INTERVAL_SECONDS = 5
ENDPOINT = 'http://127.0.0.1:8011/api/registration-groups/approval-decisions'
HEADERS = {'Content-Type': 'application/json'}


def run_monitor() -> dict:
    proc = subprocess.run(MONITOR_CMD, cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f'monitor_failed rc={proc.returncode} stderr={proc.stderr.strip()} stdout={proc.stdout.strip()}')
    try:
        return json.loads(proc.stdout)
    except Exception as exc:
        raise RuntimeError(f'monitor_invalid_json: {exc}; stdout={proc.stdout[:1000]!r}') from exc


def call_approval() -> dict:
    start_epoch = time.time()
    started_at_utc = datetime.now(timezone.utc).isoformat()
    request_payload = {
        'registration_group': '8️⃣5️⃣',
        'decision': 'approve',
        'decided_at': started_at_utc,
        'approved_count': 1,
        'area': 'Indonesia',
        'force_immediate': True,
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(request_payload).encode('utf-8'),
        headers=HEADERS,
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body_bytes = resp.read()
            status = resp.getcode()
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        status = exc.code
    end_epoch = time.time()
    ended_at_utc = datetime.now(timezone.utc).isoformat()
    body_text = body_bytes.decode('utf-8', errors='replace')
    try:
        body = json.loads(body_text)
    except Exception:
        body = {'raw_body': body_text}
    return {
        'approval_request_started_at_utc': started_at_utc,
        'approval_request_completed_at_utc': ended_at_utc,
        'instruction_to_approval_request_start_seconds': round(start_epoch - ANCHOR_EPOCH, 3),
        'instruction_to_response_complete_seconds': round(end_epoch - ANCHOR_EPOCH, 3),
        'approval_http_elapsed_seconds': round(end_epoch - start_epoch, 3),
        'http_status': status,
        'request_payload': request_payload,
        'response': body,
    }


def write_output(payload: dict) -> None:
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def main() -> int:
    print(json.dumps({'status': 'watching', 'anchor_utc': ANCHOR_UTC, 'output_path': str(OUTPUT_PATH), 'poll_interval_seconds': POLL_INTERVAL_SECONDS}, ensure_ascii=False), flush=True)
    while True:
        checked_at_epoch = time.time()
        checked_at_utc = datetime.now(timezone.utc).isoformat()
        try:
            monitor = run_monitor()
            pending = int(monitor.get('registration_group', {}).get('pending', {}).get('pending_count') or 0)
            snapshot = {
                'status': 'waiting_for_pending',
                'anchor_utc': ANCHOR_UTC,
                'checked_at_utc': checked_at_utc,
                'instruction_elapsed_seconds': round(checked_at_epoch - ANCHOR_EPOCH, 3),
                'pending_count': pending,
                'monitor': monitor,
            }
            write_output(snapshot)
            print(json.dumps({'status': 'poll', 'checked_at_utc': checked_at_utc, 'instruction_elapsed_seconds': snapshot['instruction_elapsed_seconds'], 'pending_count': pending}, ensure_ascii=False), flush=True)
            if pending > 0:
                result = {
                    'status': 'triggering_approval',
                    'anchor_utc': ANCHOR_UTC,
                    'detected_pending_at_utc': checked_at_utc,
                    'instruction_to_pending_detected_seconds': round(checked_at_epoch - ANCHOR_EPOCH, 3),
                    'monitor': monitor,
                }
                write_output(result)
                approval = call_approval()
                result.update(approval)
                result['status'] = 'completed'
                write_output(result)
                print(json.dumps({'status': 'completed', 'instruction_to_approval_request_start_seconds': approval['instruction_to_approval_request_start_seconds'], 'instruction_to_response_complete_seconds': approval['instruction_to_response_complete_seconds'], 'approval_http_elapsed_seconds': approval['approval_http_elapsed_seconds'], 'verified': approval['response'].get('verified'), 'crm_recorded': approval['response'].get('crm_recorded')}, ensure_ascii=False), flush=True)
                return 0
        except Exception as exc:
            error_payload = {
                'status': 'monitor_error',
                'anchor_utc': ANCHOR_UTC,
                'checked_at_utc': checked_at_utc,
                'instruction_elapsed_seconds': round(checked_at_epoch - ANCHOR_EPOCH, 3),
                'error': str(exc),
            }
            write_output(error_payload)
            print(json.dumps(error_payload, ensure_ascii=False), flush=True)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == '__main__':
    raise SystemExit(main())
