#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.webjs_temp_cleanup import (  # noqa: E402
    DEFAULT_PROTECTED_COMMAND_SUBSTRINGS,
    DEFAULT_PROTECTED_PORTS,
    DEFAULT_PROTECTED_USER_DATA_SUBSTRINGS,
    build_stat_map,
    collect_cleanup_targets,
    execute_cleanup,
    get_protected_pid_set,
    get_ps_rows,
    load_fixture,
)

JsonDict = Dict[str, Any]

DEFAULT_FREE_MB_THRESHOLD = 512.0
DEFAULT_COMPRESSOR_MB_THRESHOLD = 2048.0
DEFAULT_MIN_AGE_HOURS = 0.5

VM_STAT_FIELD_MAP = {
    'Pages free': 'pages_free',
    'Pages speculative': 'pages_speculative',
    'Pages occupied by compressor': 'pages_occupied_by_compressor',
    'Pages stored in compressor': 'pages_stored_in_compressor',
    'Pages active': 'pages_active',
    'Pages inactive': 'pages_inactive',
    'Pages wired down': 'pages_wired_down',
    'Swapins': 'swapins',
    'Swapouts': 'swapouts',
}

PYTEST_COMMAND_PATTERN = re.compile(
    r'(^|\s)(pytest|py\.test)(\s|$)|python[^\n]*\s-m\s+pytest(\s|$)',
    re.IGNORECASE,
)


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Run safe MCN memory-pressure cleanup only when macOS memory is tight.',
    )
    parser.add_argument('--apply', action='store_true', help='Actually kill stale temp browser roots and remove dirs. Default is dry-run.')
    parser.add_argument('--free-mb-threshold', type=float, default=DEFAULT_FREE_MB_THRESHOLD, help=f'Trigger cleanup when immediately free+speculative memory is below this MB threshold. Default: {DEFAULT_FREE_MB_THRESHOLD}')
    parser.add_argument('--compressor-mb-threshold', type=float, default=DEFAULT_COMPRESSOR_MB_THRESHOLD, help=f'Trigger cleanup when occupied compressor memory is at or above this MB threshold. Default: {DEFAULT_COMPRESSOR_MB_THRESHOLD}')
    parser.add_argument('--min-age-hours', type=float, default=DEFAULT_MIN_AGE_HOURS, help=f'Only target stale temp browser roots and stale orphan pytest groups older than this many hours. Default: {DEFAULT_MIN_AGE_HOURS}')
    parser.add_argument('--temp-root', default=tempfile.gettempdir(), help='macOS temp root to scan. Default: current tempfile.gettempdir().')
    parser.add_argument('--protect-port', dest='protect_ports', action='append', type=int, default=[], help='Additional listener ports to protect.')
    parser.add_argument('--protect-cmd-substring', dest='protect_cmd_substrings', action='append', default=[], help='Additional process command substrings to protect.')
    parser.add_argument('--protect-user-data-substring', dest='protect_user_data_substrings', action='append', default=[], help='Additional user-data-dir substrings to protect.')
    parser.add_argument('--json-indent', type=int, default=2, help='JSON indent for output. Default: 2')
    parser.add_argument('--fixture', default='', help='Test-only fixture JSON path to inject vm_stat/ps/stat/time inputs.')
    return parser.parse_args(argv)


def parse_vm_stat_output(output: str) -> JsonDict:
    parsed: JsonDict = {}
    page_size_match = re.search(r'page size of\s+(\d+)\s+bytes', str(output or ''), flags=re.IGNORECASE)
    parsed['page_size_bytes'] = int(page_size_match.group(1)) if page_size_match else 4096
    for raw_line in str(output or '').splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for prefix, field_name in VM_STAT_FIELD_MAP.items():
            if not line.startswith(prefix + ':'):
                continue
            value_text = line.split(':', 1)[1].strip().rstrip('.')
            value_text = value_text.replace(',', '')
            number_match = re.search(r'-?\d+', value_text)
            if number_match:
                parsed[field_name] = int(number_match.group(0))
            break
    return parsed


def _pages_to_mb(page_count: int, page_size_bytes: int) -> float:
    return (max(int(page_count or 0), 0) * max(int(page_size_bytes or 4096), 1)) / (1024.0 * 1024.0)


def evaluate_memory_pressure(
    parsed_vm_stat: JsonDict,
    *,
    free_mb_threshold: float,
    compressor_mb_threshold: float,
) -> JsonDict:
    page_size_bytes = int(parsed_vm_stat.get('page_size_bytes') or 4096)
    pages_free = max(int(parsed_vm_stat.get('pages_free') or 0), 0)
    pages_speculative = max(int(parsed_vm_stat.get('pages_speculative') or 0), 0)
    pages_occupied_by_compressor = max(int(parsed_vm_stat.get('pages_occupied_by_compressor') or 0), 0)
    pages_stored_in_compressor = max(int(parsed_vm_stat.get('pages_stored_in_compressor') or 0), 0)

    free_mb = _pages_to_mb(pages_free + pages_speculative, page_size_bytes)
    compressor_mb = _pages_to_mb(pages_occupied_by_compressor, page_size_bytes)
    compressed_payload_mb = _pages_to_mb(pages_stored_in_compressor, page_size_bytes)

    reasons = []
    if free_mb < float(free_mb_threshold):
        reasons.append('free_mb_below_threshold')
    if compressor_mb >= float(compressor_mb_threshold):
        reasons.append('compressor_mb_above_threshold')

    return {
        'triggered': bool(reasons),
        'reasons': reasons,
        'page_size_bytes': page_size_bytes,
        'pages_free': pages_free,
        'pages_speculative': pages_speculative,
        'pages_occupied_by_compressor': pages_occupied_by_compressor,
        'pages_stored_in_compressor': pages_stored_in_compressor,
        'free_mb': round(free_mb, 2),
        'compressor_mb': round(compressor_mb, 2),
        'compressed_payload_mb': round(compressed_payload_mb, 2),
        'free_mb_threshold': float(free_mb_threshold),
        'compressor_mb_threshold': float(compressor_mb_threshold),
    }


def _sample_vm_stat_output(*, fixture: Optional[JsonDict] = None) -> str:
    fixture = fixture or {}
    vm_stat_output = fixture.get('vm_stat_output')
    if vm_stat_output is not None:
        return str(vm_stat_output)
    return subprocess.run(
        ['vm_stat'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _parse_etime_to_seconds(value: str) -> int:
    text = str(value or '').strip()
    if not text:
        return 0
    days = 0
    if '-' in text:
        maybe_days, text = text.split('-', 1)
        if maybe_days.isdigit():
            days = int(maybe_days)
    parts = [int(part) for part in text.split(':') if part.isdigit()]
    if not parts:
        return 0
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    else:
        hours = 0
        minutes = 0
        seconds = parts[0]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _looks_like_pytest_command(command: str) -> bool:
    return bool(PYTEST_COMMAND_PATTERN.search(str(command or '')))


def get_pytest_ps_rows(*, fixture: Optional[JsonDict] = None) -> List[JsonDict]:
    fixture = fixture or {}
    if fixture.get('pytest_ps_rows') is not None:
        return [dict(row) for row in (fixture.get('pytest_ps_rows') or [])]
    output = subprocess.run(
        ['ps', '-Ao', 'pid=,ppid=,pgid=,etime=,command='],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rows: List[JsonDict] = []
    for raw in str(output or '').splitlines():
        match = re.match(r'\s*(\d+)\s+(\d+)\s+(\d+)\s+([^ ]+)\s+(.*)$', raw)
        if not match:
            continue
        etime = match.group(4)
        rows.append(
            {
                'pid': int(match.group(1)),
                'ppid': int(match.group(2)),
                'pgid': int(match.group(3)),
                'etime': etime,
                'etime_seconds': _parse_etime_to_seconds(etime),
                'command': match.group(5),
            }
        )
    return rows


def get_current_pgid(*, fixture: Optional[JsonDict] = None) -> int:
    fixture = fixture or {}
    if fixture.get('current_pgid') is not None:
        return int(fixture.get('current_pgid') or 0)
    return os.getpgrp()


def collect_stale_pytest_groups(
    *,
    ps_rows: Sequence[JsonDict],
    min_age_hours: float,
    current_pgid: int,
) -> List[JsonDict]:
    rows_by_group: Dict[int, List[JsonDict]] = {}
    for row in ps_rows:
        pgid = int(row.get('pgid') or 0)
        if pgid <= 1:
            continue
        normalized = dict(row)
        normalized['pid'] = int(normalized.get('pid') or 0)
        normalized['ppid'] = int(normalized.get('ppid') or 0)
        normalized['pgid'] = pgid
        normalized['etime_seconds'] = int(normalized.get('etime_seconds') or _parse_etime_to_seconds(str(normalized.get('etime') or '0')))
        rows_by_group.setdefault(pgid, []).append(normalized)

    min_age_seconds = max(float(min_age_hours), 0.0) * 3600.0
    targets: List[JsonDict] = []
    for pgid, group_rows in sorted(rows_by_group.items()):
        if pgid == int(current_pgid or 0):
            continue
        leader = next((row for row in group_rows if int(row.get('pid') or 0) == pgid), None)
        if not leader:
            continue
        leader_command = str(leader.get('command') or '')
        leader_ppid = int(leader.get('ppid') or 0)
        leader_age_seconds = int(leader.get('etime_seconds') or 0)
        if leader_ppid != 1:
            continue
        if not _looks_like_pytest_command(leader_command):
            continue
        if leader_age_seconds < min_age_seconds:
            continue
        sorted_rows = sorted(group_rows, key=lambda item: int(item.get('pid') or 0))
        targets.append(
            {
                'pgid': pgid,
                'root_pid': int(leader.get('pid') or 0),
                'root_ppid': leader_ppid,
                'root_command': leader_command,
                'age_hours': round(leader_age_seconds / 3600.0, 2),
                'process_count': len(sorted_rows),
                'pids': [int(row.get('pid') or 0) for row in sorted_rows],
                'commands': [str(row.get('command') or '') for row in sorted_rows],
            }
        )
    return targets


def execute_pytest_group_cleanup(
    *,
    targets: Sequence[JsonDict],
    current_pgid: int,
    killpg_fn=os.killpg,
    sleep_fn=time.sleep,
    ps_rows_provider=None,
) -> JsonDict:
    ps_rows_provider = ps_rows_provider or get_pytest_ps_rows
    result: JsonDict = {
        'term_sent': [],
        'kill_sent': [],
        'skipped_groups': [],
        'remaining_groups': [],
    }

    def current_target_map() -> Dict[int, JsonDict]:
        return {
            int(item.get('pgid') or 0): item
            for item in collect_stale_pytest_groups(
                ps_rows=ps_rows_provider(),
                min_age_hours=0.0,
                current_pgid=current_pgid,
            )
        }

    initial_targets = current_target_map()
    validated_groups: List[int] = []
    for target in targets:
        pgid = int(target.get('pgid') or 0)
        current = initial_targets.get(pgid)
        if not current or str(current.get('root_command') or '') != str(target.get('root_command') or ''):
            result['skipped_groups'].append({'pgid': pgid, 'reason': 'target_revalidation_failed'})
            continue
        validated_groups.append(pgid)

    for pgid in validated_groups:
        try:
            killpg_fn(pgid, signal.SIGTERM)
            result['term_sent'].append(pgid)
        except ProcessLookupError:
            continue

    sleep_fn(2.0)

    mid_targets = current_target_map()
    for pgid in validated_groups:
        if pgid not in mid_targets:
            continue
        try:
            killpg_fn(pgid, signal.SIGKILL)
            result['kill_sent'].append(pgid)
        except ProcessLookupError:
            continue

    result['remaining_groups'] = list(current_target_map().values())
    return result


def _run_webjs_cleanup(
    *,
    apply: bool,
    min_age_hours: float,
    temp_root: Path,
    protected_ports: Sequence[int],
    protected_cmd_substrings: Sequence[str],
    protected_user_data_substrings: Sequence[str],
    fixture: Optional[JsonDict] = None,
) -> JsonDict:
    fixture = fixture or {}
    ps_rows = get_ps_rows(fixture=fixture)
    protected_pids = get_protected_pid_set(
        protected_ports=protected_ports,
        protected_cmd_substrings=protected_cmd_substrings,
        ps_rows=ps_rows,
        fixture=fixture,
    )
    stat_map = build_stat_map(temp_root, fixture=fixture)
    targets = collect_cleanup_targets(
        temp_root=temp_root,
        ps_rows=ps_rows,
        protected_pids=protected_pids,
        min_age_hours=min_age_hours,
        now=fixture.get('now'),
        stat_map=stat_map,
        protected_user_data_substrings=protected_user_data_substrings,
    )
    payload: JsonDict = {
        'apply': bool(apply),
        'temp_root': str(temp_root.resolve()),
        'protected_pids': sorted(protected_pids),
        'summary': {
            'target_count': len(targets),
            'target_size_kb': sum(int(item.get('size_kb') or 0) for item in targets),
        },
        'targets': targets,
    }
    if apply:
        payload['cleanup'] = execute_cleanup(
            targets=targets,
            temp_root=temp_root,
            min_age_hours=min_age_hours,
            now=fixture.get('now'),
            stat_map=stat_map,
            ps_rows_provider=lambda: get_ps_rows(fixture=fixture),
        )
    return payload


def _run_pytest_cleanup(
    *,
    apply: bool,
    min_age_hours: float,
    fixture: Optional[JsonDict] = None,
) -> JsonDict:
    fixture = fixture or {}
    current_pgid = get_current_pgid(fixture=fixture)
    ps_rows = get_pytest_ps_rows(fixture=fixture)
    targets = collect_stale_pytest_groups(
        ps_rows=ps_rows,
        min_age_hours=min_age_hours,
        current_pgid=current_pgid,
    )
    payload: JsonDict = {
        'apply': bool(apply),
        'current_pgid': current_pgid,
        'summary': {
            'target_group_count': len(targets),
            'target_process_count': sum(int(item.get('process_count') or 0) for item in targets),
        },
        'targets': targets,
    }
    if apply:
        payload['cleanup'] = execute_pytest_group_cleanup(
            targets=targets,
            current_pgid=current_pgid,
            ps_rows_provider=lambda: get_pytest_ps_rows(fixture=fixture),
        )
    return payload


def run_guard(
    *,
    apply: bool,
    min_age_hours: float,
    free_mb_threshold: float,
    compressor_mb_threshold: float,
    temp_root: Optional[Path] = None,
    protected_ports: Optional[Sequence[int]] = None,
    protected_cmd_substrings: Optional[Sequence[str]] = None,
    protected_user_data_substrings: Optional[Sequence[str]] = None,
    fixture: Optional[JsonDict] = None,
) -> JsonDict:
    fixture = fixture or {}
    resolved_temp_root = Path(fixture.get('temp_root') or temp_root or tempfile.gettempdir()).expanduser()
    protected_ports = list(DEFAULT_PROTECTED_PORTS) + list(protected_ports or [])
    protected_cmd_substrings = list(DEFAULT_PROTECTED_COMMAND_SUBSTRINGS) + list(protected_cmd_substrings or [])
    protected_user_data_substrings = list(DEFAULT_PROTECTED_USER_DATA_SUBSTRINGS) + list(protected_user_data_substrings or [])

    vm_stat_output = _sample_vm_stat_output(fixture=fixture)
    parsed_vm_stat = parse_vm_stat_output(vm_stat_output)
    pressure = evaluate_memory_pressure(
        parsed_vm_stat,
        free_mb_threshold=free_mb_threshold,
        compressor_mb_threshold=compressor_mb_threshold,
    )

    payload: JsonDict = {
        'apply': bool(apply),
        'pressure': pressure,
        'temp_root': str(resolved_temp_root.resolve()),
        'cleanup': None,
        'pytest_cleanup': None,
    }

    if pressure['triggered']:
        payload['cleanup'] = _run_webjs_cleanup(
            apply=apply,
            min_age_hours=min_age_hours,
            temp_root=resolved_temp_root,
            protected_ports=protected_ports,
            protected_cmd_substrings=protected_cmd_substrings,
            protected_user_data_substrings=protected_user_data_substrings,
            fixture=fixture,
        )
        payload['pytest_cleanup'] = _run_pytest_cleanup(
            apply=apply,
            min_age_hours=min_age_hours,
            fixture=fixture,
        )

    return payload


def run_cli(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    fixture = load_fixture(args.fixture)
    payload = run_guard(
        apply=args.apply,
        min_age_hours=args.min_age_hours,
        free_mb_threshold=args.free_mb_threshold,
        compressor_mb_threshold=args.compressor_mb_threshold,
        temp_root=Path(args.temp_root).expanduser(),
        protected_ports=args.protect_ports,
        protected_cmd_substrings=args.protect_cmd_substrings,
        protected_user_data_substrings=args.protect_user_data_substrings,
        fixture=fixture,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=args.json_indent))
    return 0


if __name__ == '__main__':
    raise SystemExit(run_cli())
