#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

TEMP_DIR_PREFIXES = (
    'webjs-fresh-state-',
    'webjs-approval-approve-profile-',
)
DEFAULT_PROTECTED_PORTS = (8011, 55801)
DEFAULT_PROTECTED_COMMAND_SUBSTRINGS = (
    'production_ops_daemon.py',
    'node src/server.js',
)
DEFAULT_PROTECTED_USER_DATA_SUBSTRINGS = (
    '/.wwebjs_auth_accounts/',
    '/session-wa-approval-',
)


JsonDict = Dict[str, Any]


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Safely scan and clean stale MCN webjs temp Chrome profiles.',
    )
    parser.add_argument('--apply', action='store_true', help='Actually kill stale orphan roots and remove dirs. Default is dry-run.')
    parser.add_argument('--min-age-hours', type=float, default=1.0, help='Only target temp roots older than this many hours. Default: 1.0')
    parser.add_argument('--temp-root', default=tempfile.gettempdir(), help='macOS temp root to scan. Default: current tempfile.gettempdir().')
    parser.add_argument('--protect-port', dest='protect_ports', action='append', type=int, default=[], help='Additional listener ports to protect.')
    parser.add_argument('--protect-cmd-substring', dest='protect_cmd_substrings', action='append', default=[], help='Additional process command substrings to protect.')
    parser.add_argument('--protect-user-data-substring', dest='protect_user_data_substrings', action='append', default=[], help='Additional user-data-dir substrings to protect.')
    parser.add_argument('--json-indent', type=int, default=2, help='JSON indent for output. Default: 2')
    parser.add_argument('--fixture', default='', help='Test-only fixture JSON path to inject ps/stat/time inputs.')
    return parser.parse_args(argv)


def _normalize_path(value: str) -> str:
    return str(Path(value).expanduser().resolve())


def _matches_temp_dir_name(path: str) -> bool:
    return any(Path(path).name.startswith(prefix) for prefix in TEMP_DIR_PREFIXES)


def _extract_user_data_dir(command: str) -> Optional[str]:
    match = re.search(r'--user-data-dir=([^ ]+)', str(command or ''))
    if not match:
        return None
    return match.group(1)


def parse_ps_rows(ps_output: str) -> List[JsonDict]:
    rows: List[JsonDict] = []
    for raw in str(ps_output or '').splitlines():
        match = re.match(r'\s*(\d+)\s+(\d+)\s+(.*)$', raw)
        if not match:
            continue
        pid = int(match.group(1))
        ppid = int(match.group(2))
        command = match.group(3)
        rows.append({'pid': pid, 'ppid': ppid, 'command': command})
    return rows


def load_fixture(path: str) -> JsonDict:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding='utf-8'))


def get_ps_rows(*, fixture: Optional[JsonDict] = None) -> List[JsonDict]:
    fixture = fixture or {}
    if fixture.get('ps_rows') is not None:
        return list(fixture.get('ps_rows') or [])
    output = subprocess.run(
        ['ps', '-Ao', 'pid=,ppid=,command='],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return parse_ps_rows(output)


def _expand_descendant_pids(seed_pids: Set[int], ps_rows: Sequence[JsonDict]) -> Set[int]:
    expanded = set(int(pid) for pid in seed_pids)
    children_by_parent: Dict[int, List[int]] = {}
    for row in ps_rows:
        parent_pid = int(row.get('ppid') or 0)
        children_by_parent.setdefault(parent_pid, []).append(int(row.get('pid') or 0))

    queue = list(expanded)
    while queue:
        current = queue.pop()
        for child_pid in children_by_parent.get(current, []):
            if child_pid in expanded:
                continue
            expanded.add(child_pid)
            queue.append(child_pid)
    return expanded



def get_protected_pid_set(
    *,
    protected_ports: Sequence[int],
    protected_cmd_substrings: Sequence[str],
    ps_rows: Sequence[JsonDict],
    fixture: Optional[JsonDict] = None,
) -> Set[int]:
    fixture = fixture or {}
    protected: Set[int] = set(int(pid) for pid in fixture.get('protected_pids', []) or [])

    if fixture.get('protected_pids') is None:
        for port in protected_ports:
            output = subprocess.run(
                ['lsof', f'-iTCP:{int(port)}', '-sTCP:LISTEN', '-nP', '-Fp'],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.splitlines()
            for line in output:
                if line.startswith('p'):
                    protected.add(int(line[1:]))

    for row in ps_rows:
        command = str(row.get('command') or '')
        if any(snippet and snippet in command for snippet in protected_cmd_substrings):
            protected.add(int(row['pid']))
    return _expand_descendant_pids(protected, ps_rows)


def build_stat_map(temp_root: Path, *, fixture: Optional[JsonDict] = None) -> Dict[str, JsonDict]:
    fixture = fixture or {}
    if fixture.get('stat_map') is not None:
        return dict(fixture.get('stat_map') or {})
    stat_map: Dict[str, JsonDict] = {}
    if not temp_root.exists():
        return stat_map
    for child in temp_root.iterdir():
        if not child.is_dir() or not _matches_temp_dir_name(str(child)):
            continue
        try:
            stat_result = child.stat()
        except FileNotFoundError:
            continue
        du = subprocess.run(['du', '-sk', str(child)], capture_output=True, text=True, check=False).stdout.split()
        size_kb = int(du[0]) if du else 0
        stat_map[str(child)] = {
            'mtime': stat_result.st_mtime,
            'size_kb': size_kb,
        }
    return stat_map


def collect_cleanup_targets(
    *,
    temp_root: Path,
    ps_rows: Sequence[JsonDict],
    protected_pids: Set[int],
    min_age_hours: float,
    now: Optional[float] = None,
    stat_map: Optional[Dict[str, JsonDict]] = None,
    protected_user_data_substrings: Sequence[str] = DEFAULT_PROTECTED_USER_DATA_SUBSTRINGS,
) -> List[JsonDict]:
    stat_map = stat_map or {}
    current_time = float(time.time() if now is None else now)
    targets: List[JsonDict] = []
    temp_root_str = _normalize_path(str(temp_root))

    for row in ps_rows:
        pid = int(row.get('pid') or 0)
        ppid = int(row.get('ppid') or 0)
        command = str(row.get('command') or '')
        user_data_dir = _extract_user_data_dir(command)
        if not user_data_dir:
            continue
        normalized_dir = _normalize_path(user_data_dir)
        if pid in protected_pids:
            continue
        if any(snippet and snippet in normalized_dir for snippet in protected_user_data_substrings):
            continue
        if not normalized_dir.startswith(temp_root_str + os.sep):
            continue
        if not _matches_temp_dir_name(normalized_dir):
            continue
        if ppid != 1:
            continue
        stat_payload = stat_map.get(normalized_dir)
        if not stat_payload:
            continue
        age_hours = max((current_time - float(stat_payload.get('mtime') or 0.0)) / 3600.0, 0.0)
        if age_hours < float(min_age_hours):
            continue
        targets.append(
            {
                'pid': pid,
                'ppid': ppid,
                'command': command,
                'user_data_dir': normalized_dir,
                'age_hours': round(age_hours, 2),
                'size_kb': int(stat_payload.get('size_kb') or 0),
            }
        )

    targets.sort(key=lambda item: (item['user_data_dir'], item['pid']))
    return targets


def _default_referenced_dirs_provider() -> Set[str]:
    referenced: Set[str] = set()
    for row in get_ps_rows():
        user_data_dir = _extract_user_data_dir(str(row.get('command') or ''))
        if user_data_dir:
            referenced.add(_normalize_path(user_data_dir))
    return referenced


def _default_is_pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _current_temp_target_by_pid(ps_rows: Sequence[JsonDict]) -> Dict[int, JsonDict]:
    current: Dict[int, JsonDict] = {}
    for row in ps_rows:
        command = str(row.get('command') or '')
        user_data_dir = _extract_user_data_dir(command)
        if not user_data_dir:
            continue
        normalized_dir = _normalize_path(user_data_dir)
        if not _matches_temp_dir_name(normalized_dir):
            continue
        current[int(row.get('pid') or 0)] = {
            'pid': int(row.get('pid') or 0),
            'ppid': int(row.get('ppid') or 0),
            'command': command,
            'user_data_dir': normalized_dir,
        }
    return current



def execute_cleanup(
    *,
    targets: Sequence[JsonDict],
    temp_root: Path,
    min_age_hours: float,
    now: Optional[float] = None,
    stat_map: Optional[Dict[str, JsonDict]] = None,
    kill_fn: Callable[[int, int], None] = os.kill,
    is_pid_alive_fn: Callable[[int], bool] = _default_is_pid_alive,
    sleep_fn: Callable[[float], None] = time.sleep,
    referenced_dirs_provider: Callable[[], Set[str]] = _default_referenced_dirs_provider,
    ps_rows_provider: Callable[[], Sequence[JsonDict]] = get_ps_rows,
) -> JsonDict:
    result: JsonDict = {
        'term_sent': [],
        'kill_sent': [],
        'removed_dirs': [],
        'failed_remove': [],
        'skipped_pids': [],
    }
    current_time = float(time.time() if now is None else now)
    stat_map = stat_map or build_stat_map(temp_root)

    root_pids = [int(item['pid']) for item in targets]
    target_dirs = sorted({str(item['user_data_dir']) for item in targets})
    targets_by_pid = {int(item['pid']): str(item['user_data_dir']) for item in targets}

    current_targets = _current_temp_target_by_pid(ps_rows_provider())
    validated_pids: List[int] = []
    for pid in root_pids:
        expected_dir = _normalize_path(targets_by_pid.get(pid) or '')
        current_target = current_targets.get(pid)
        if not current_target or int(current_target.get('ppid') or 0) != 1 or _normalize_path(str(current_target.get('user_data_dir') or '')) != expected_dir:
            result['skipped_pids'].append({'pid': pid, 'reason': 'target_revalidation_failed'})
            continue
        validated_pids.append(pid)

    for pid in validated_pids:
        try:
            kill_fn(pid, signal.SIGTERM)
            result['term_sent'].append(pid)
        except ProcessLookupError:
            continue

    sleep_fn(3.0)

    for pid in validated_pids:
        if not is_pid_alive_fn(pid):
            continue
        current_targets = _current_temp_target_by_pid(ps_rows_provider())
        current_target = current_targets.get(pid)
        expected_dir = _normalize_path(targets_by_pid.get(pid) or '')
        if not current_target or int(current_target.get('ppid') or 0) != 1 or _normalize_path(str(current_target.get('user_data_dir') or '')) != expected_dir:
            result['skipped_pids'].append({'pid': pid, 'reason': 'target_revalidation_failed'})
            continue
        try:
            kill_fn(pid, signal.SIGKILL)
            result['kill_sent'].append(pid)
        except ProcessLookupError:
            continue

    sleep_fn(1.0)

    referenced_dirs = {_normalize_path(path) for path in referenced_dirs_provider()}
    for path in target_dirs:
        normalized_path = _normalize_path(path)
        if normalized_path in referenced_dirs:
            result['failed_remove'].append({'dir': normalized_path, 'reason': 'still_referenced_by_live_process'})
            continue
        try:
            shutil.rmtree(normalized_path)
            result['removed_dirs'].append(normalized_path)
        except FileNotFoundError:
            continue
        except Exception as exc:  # pragma: no cover - defensive
            result['failed_remove'].append({'dir': normalized_path, 'reason': str(exc)})

    if temp_root.exists():
        referenced_dirs = {_normalize_path(path) for path in referenced_dirs_provider()}
        for child in sorted(temp_root.iterdir()):
            child_path = _normalize_path(str(child))
            if not child.is_dir() or not _matches_temp_dir_name(child_path):
                continue
            if child_path in referenced_dirs:
                continue
            stat_payload = stat_map.get(child_path)
            if not stat_payload:
                continue
            size_kb = int(stat_payload.get('size_kb') or 0)
            age_hours = max((current_time - float(stat_payload.get('mtime') or 0.0)) / 3600.0, 0.0)
            if size_kb != 0 or age_hours < float(min_age_hours):
                continue
            try:
                shutil.rmtree(child_path)
                result['removed_dirs'].append(child_path)
            except FileNotFoundError:
                continue
            except Exception as exc:  # pragma: no cover - defensive
                result['failed_remove'].append({'dir': child_path, 'reason': str(exc)})

    result['remaining_temp_dirs'] = [
        _normalize_path(str(child))
        for child in sorted(temp_root.iterdir())
        if child.is_dir() and _matches_temp_dir_name(str(child))
    ] if temp_root.exists() else []
    result['remaining_temp_procs'] = [
        {
            'pid': int(row['pid']),
            'ppid': int(row['ppid']),
            'user_data_dir': _normalize_path(_extract_user_data_dir(str(row.get('command') or '')) or ''),
        }
        for row in ps_rows_provider()
        if _extract_user_data_dir(str(row.get('command') or ''))
        and _normalize_path(_extract_user_data_dir(str(row.get('command') or '')) or '').startswith(_normalize_path(str(temp_root)) + os.sep)
        and _matches_temp_dir_name(_extract_user_data_dir(str(row.get('command') or '')) or '')
    ]
    return result


def run_cli(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    fixture = load_fixture(args.fixture)
    temp_root = Path(fixture.get('temp_root') or args.temp_root).expanduser()
    ps_rows = get_ps_rows(fixture=fixture)
    protected_ports = list(DEFAULT_PROTECTED_PORTS) + list(args.protect_ports or [])
    protected_cmd_substrings = list(DEFAULT_PROTECTED_COMMAND_SUBSTRINGS) + list(args.protect_cmd_substrings or [])
    protected_user_data_substrings = list(DEFAULT_PROTECTED_USER_DATA_SUBSTRINGS) + list(args.protect_user_data_substrings or [])
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
        min_age_hours=args.min_age_hours,
        now=fixture.get('now'),
        stat_map=stat_map,
        protected_user_data_substrings=protected_user_data_substrings,
    )

    payload: JsonDict = {
        'apply': bool(args.apply),
        'temp_root': _normalize_path(str(temp_root)),
        'protected_pids': sorted(protected_pids),
        'targets': targets,
        'summary': {
            'target_count': len(targets),
            'target_size_kb': sum(int(item.get('size_kb') or 0) for item in targets),
        },
    }

    if args.apply:
        payload['cleanup'] = execute_cleanup(
            targets=targets,
            temp_root=temp_root,
            min_age_hours=args.min_age_hours,
            now=fixture.get('now'),
            stat_map=stat_map,
            ps_rows_provider=lambda: get_ps_rows(fixture=fixture),
        )

    print(json.dumps(payload, ensure_ascii=False, indent=args.json_indent))
    return 0


if __name__ == '__main__':
    raise SystemExit(run_cli())
