#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

LABEL = 'ai.hermes.gateway'
HERMES_HOME = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))).expanduser()
GATEWAY_LOG = HERMES_HOME / 'logs' / 'gateway.log'
GATEWAY_ERROR_LOG = HERMES_HOME / 'logs' / 'gateway.error.log'
AGENT_LOG = HERMES_HOME / 'logs' / 'agent.log'
DEFAULT_STATE_FILE = Path('/Users/chauncey/work/mcn-ai-automation/data/default_hermes_gateway_self_heal_state.json')
DEFAULT_SELF_HEAL_LOG = Path('/Users/chauncey/work/mcn-ai-automation/logs/default_hermes_gateway_self_heal.log')
TS_PATTERNS = [
    re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\]'),
    re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+'),
]


@dataclass
class Event:
    kind: str
    ts: datetime | None
    line: str
    source: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Status/doctor/self-heal helpers for default ai.hermes.gateway')
    sub = parser.add_subparsers(dest='cmd', required=False)

    status = sub.add_parser('status')
    status.add_argument('--window-minutes', type=int, default=45)
    status.add_argument('--tail-lines', type=int, default=4000)

    self_heal = sub.add_parser('self-heal')
    self_heal.add_argument('--window-minutes', type=int, default=45)
    self_heal.add_argument('--tail-lines', type=int, default=4000)
    self_heal.add_argument('--failure-threshold', type=int, default=3)
    self_heal.add_argument('--cooldown-minutes', type=int, default=30)
    self_heal.add_argument('--apply', action='store_true')
    self_heal.add_argument('--state-file', default=str(DEFAULT_STATE_FILE))
    self_heal.add_argument('--self-heal-log', default=str(DEFAULT_SELF_HEAL_LOG))

    parser.set_defaults(cmd='status')
    return parser.parse_args(argv)


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def launchctl_print(label: str) -> str:
    gui = f'gui/{os.getuid()}'
    return run(['launchctl', 'print', f'{gui}/{label}'])


def extract_field(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, flags=re.MULTILINE)
    return m.group(1) if m else None


def parse_timestamp(line: str) -> datetime | None:
    for pat in TS_PATTERNS:
        m = pat.search(line)
        if m:
            try:
                return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
            except ValueError:
                return None
    return None


def tail_lines(path: Path, limit: int) -> list[str]:
    if not path.exists():
        return []
    dq: deque[str] = deque(maxlen=limit)
    with path.open('r', encoding='utf-8', errors='replace') as f:
        for line in f:
            dq.append(line.rstrip('\n'))
    return list(dq)


def classify(line: str, source: str) -> Event | None:
    ts = parse_timestamp(line)
    pairs = [
        ('lark_disconnected', 'disconnected to wss://msg-frontier-sg.larksuite.com/ws/v2'),
        ('lark_connected', 'connected to wss://msg-frontier-sg.larksuite.com/ws/v2'),
        ('lark_dns_failure', 'Failed to resolve \'open.larksuite.com\''),
        ('lark_handshake_timeout', 'timed out during opening handshake'),
        ('lark_ping_timeout', 'keepalive ping timeout'),
        ('lark_connection_reset', 'Connection reset by peer'),
        ('provider_no_response', 'No response from provider'),
        ('response_ready', 'gateway.run: response ready:'),
        ('inbound_message', 'gateway.run: inbound message:'),
        ('feishu_send_error', '[Feishu] Send error:'),
    ]
    for kind, needle in pairs:
        if needle in line:
            return Event(kind=kind, ts=ts, line=line, source=source)
    if 'API call failed' in line and 'ConnectionError' in line:
        return Event(kind='provider_connection_error', ts=ts, line=line, source=source)
    return None


def gather_events(tail_limit: int) -> list[Event]:
    events: list[Event] = []
    for path, source in [
        (GATEWAY_LOG, 'gateway.log'),
        (GATEWAY_ERROR_LOG, 'gateway.error.log'),
        (AGENT_LOG, 'agent.log'),
    ]:
        for line in tail_lines(path, tail_limit):
            event = classify(line, source)
            if event:
                events.append(event)
    events.sort(key=lambda e: (e.ts or datetime.min, e.source, e.line))
    return events


def latest_event(events: list[Event], kind: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.kind == kind:
            return {'ts': event.ts.isoformat(sep=' ') if event.ts else None, 'line': event.line, 'source': event.source}
    return None


def recent_window_events(events: list[Event], window_minutes: int) -> list[Event]:
    now = datetime.now()
    cutoff = now - timedelta(minutes=window_minutes)
    return [e for e in events if e.ts and e.ts >= cutoff]


def dns_ok(host: str) -> tuple[bool, str | None]:
    try:
        addr = socket.getaddrinfo(host, 443)[0][4][0]
        return True, addr
    except Exception as exc:
        return False, repr(exc)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def append_self_heal_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(payload, ensure_ascii=False) + '\n')


def build_status(window_minutes: int, tail_limit: int) -> dict[str, Any]:
    lc = launchctl_print(LABEL)
    state = extract_field(r'^\s*state = (.+)$', lc)
    runs = extract_field(r'^\s*runs =\s*(\d+)$', lc)
    pid = extract_field(r'^\s*pid =\s*(\d+)$', lc)
    last_signal = extract_field(r'^\s*last terminating signal = (.+)$', lc)
    events = gather_events(tail_limit)
    recent = recent_window_events(events, window_minutes)
    dns_status, dns_detail = dns_ok('open.larksuite.com')
    ws_dns_status, ws_dns_detail = dns_ok('msg-frontier-sg.larksuite.com')

    counts: dict[str, int] = {}
    for event in recent:
        counts[event.kind] = counts.get(event.kind, 0) + 1

    monitor_kinds = {'lark_dns_failure', 'lark_handshake_timeout'}
    success_kinds = {'lark_connected', 'response_ready', 'inbound_message'}
    consecutive_failures = 0
    trailing_events: list[dict[str, Any]] = []
    for event in reversed(recent):
        if event.kind in success_kinds:
            break
        if event.kind in monitor_kinds:
            consecutive_failures += 1
            trailing_events.append({'kind': event.kind, 'ts': event.ts.isoformat(sep=' ') if event.ts else None, 'line': event.line})

    return {
        'service': 'default_hermes_gateway',
        'label': LABEL,
        'launchd': {
            'state': state,
            'runs': int(runs) if runs else None,
            'pid': int(pid) if pid else None,
            'last_terminating_signal': last_signal,
        },
        'logs': {
            'gateway_log': str(GATEWAY_LOG),
            'gateway_error_log': str(GATEWAY_ERROR_LOG),
            'agent_log': str(AGENT_LOG),
            'gateway_log_exists': GATEWAY_LOG.exists(),
            'gateway_error_log_exists': GATEWAY_ERROR_LOG.exists(),
            'agent_log_exists': AGENT_LOG.exists(),
        },
        'dns': {
            'open_larksuite': {'ok': dns_status, 'detail': dns_detail},
            'msg_frontier_sg': {'ok': ws_dns_status, 'detail': ws_dns_detail},
        },
        'window_minutes': window_minutes,
        'recent_counts': counts,
        'latest': {
            'lark_connected': latest_event(events, 'lark_connected'),
            'lark_dns_failure': latest_event(events, 'lark_dns_failure'),
            'lark_handshake_timeout': latest_event(events, 'lark_handshake_timeout'),
            'lark_ping_timeout': latest_event(events, 'lark_ping_timeout'),
            'provider_no_response': latest_event(events, 'provider_no_response'),
            'response_ready': latest_event(events, 'response_ready'),
            'inbound_message': latest_event(events, 'inbound_message'),
            'feishu_send_error': latest_event(events, 'feishu_send_error'),
        },
        'recent_consecutive_failures': {
            'threshold_monitored_kinds': sorted(monitor_kinds),
            'count': consecutive_failures,
            'events': list(reversed(trailing_events)),
        },
    }


def perform_self_heal(status: dict[str, Any], failure_threshold: int, cooldown_minutes: int, state_file: Path, apply: bool) -> dict[str, Any]:
    state = load_state(state_file)
    now = datetime.now()
    cooldown_until = None
    last_restart_at = state.get('last_restart_at')
    if last_restart_at:
        try:
            cooldown_until = datetime.fromisoformat(last_restart_at) + timedelta(minutes=cooldown_minutes)
        except ValueError:
            cooldown_until = None

    eligible = (
        status['launchd'].get('state') == 'running'
        and status['recent_consecutive_failures'].get('count', 0) >= failure_threshold
        and status['dns']['open_larksuite']['ok']
        and status['dns']['msg_frontier_sg']['ok']
    )
    cooldown_blocked = bool(cooldown_until and now < cooldown_until)
    should_restart = eligible and not cooldown_blocked

    action = 'noop'
    restart_result = None
    if apply and should_restart:
        gui = f'gui/{os.getuid()}'
        proc = subprocess.run(['launchctl', 'kickstart', '-k', f'{gui}/{LABEL}'], capture_output=True, text=True, check=False)
        action = 'restart_attempted'
        restart_result = {'exit_code': proc.returncode, 'stdout': proc.stdout.strip(), 'stderr': proc.stderr.strip()}
        if proc.returncode == 0:
            state.update({
                'last_restart_at': now.isoformat(sep=' '),
                'last_restart_reason': 'consecutive_dns_or_handshake_failures',
            })
            save_state(state_file, state)
    payload = {
        'checked_at': now.isoformat(sep=' '),
        'eligible': eligible,
        'cooldown_blocked': cooldown_blocked,
        'cooldown_minutes': cooldown_minutes,
        'failure_threshold': failure_threshold,
        'should_restart': should_restart,
        'apply': apply,
        'action': action,
        'restart_result': restart_result,
        'state_file': str(state_file),
        'last_restart_at': state.get('last_restart_at'),
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.cmd == 'status':
        payload = build_status(window_minutes=args.window_minutes, tail_limit=args.tail_lines)
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    status = build_status(window_minutes=args.window_minutes, tail_limit=args.tail_lines)
    heal = perform_self_heal(
        status=status,
        failure_threshold=args.failure_threshold,
        cooldown_minutes=args.cooldown_minutes,
        state_file=Path(args.state_file).expanduser(),
        apply=args.apply,
    )
    payload = {'status': status, 'self_heal': heal}
    append_self_heal_log(Path(args.self_heal_log).expanduser(), payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
