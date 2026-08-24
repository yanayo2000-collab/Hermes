#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


GIB = 1024**3
LONG_ACTION_PATH_PATTERNS = (
    re.compile(r'/manual-approve(?:/|$)'),
    re.compile(r'/truth-refresh(?:/|$)'),
    re.compile(r'/full-sync(?:/|$)'),
    re.compile(r'/refresh(?:/|$)'),
    re.compile(r'/approve(?:/|$)'),
)


def _ordinary_api_slow_count(journal: str, threshold_ms: float) -> int:
    count = 0
    for line in journal.splitlines():
        duration_match = re.search(
            r'"duration_ms"\s*:\s*([0-9]+(?:\.[0-9]+)?)', line,
        )
        if not duration_match or float(duration_match.group(1)) <= threshold_ms:
            continue
        path_match = re.search(r'"path"\s*:\s*"([^"]*)"', line)
        path = path_match.group(1) if path_match else ''
        if any(pattern.search(path) for pattern in LONG_ACTION_PATH_PATTERNS):
            continue
        count += 1
    return count


def filesystem_state(path: Path) -> dict[str, float | int | str]:
    resolved = path.resolve()
    stat = os.statvfs(resolved)
    total = stat.f_blocks * stat.f_frsize
    available = stat.f_bavail * stat.f_frsize
    used_percent = 0.0 if total <= 0 else ((total - available) / total) * 100.0
    return {
        'path': str(resolved),
        'total_bytes': total,
        'available_bytes': available,
        'used_percent': round(used_percent, 2),
    }


def _mem_available_bytes(path: Path = Path('/proc/meminfo')) -> int | None:
    try:
        match = re.search(
            r'^MemAvailable:\s+(\d+)\s+kB$',
            path.read_text(encoding='utf-8'),
            flags=re.MULTILINE,
        )
    except OSError:
        return None
    return int(match.group(1)) * 1024 if match else None


def _iowait_percent(sample_seconds: float, path: Path = Path('/proc/stat')) -> float | None:
    def read() -> tuple[int, int] | None:
        try:
            values = [int(value) for value in path.read_text().splitlines()[0].split()[1:]]
        except (OSError, IndexError, ValueError):
            return None
        return (sum(values), values[4] if len(values) > 4 else 0) if values else None

    before = read()
    if before is None:
        return None
    time.sleep(max(0.0, sample_seconds))
    after = read()
    if after is None:
        return None
    total_delta = after[0] - before[0]
    return 0.0 if total_delta <= 0 else max(0.0, (after[1] - before[1]) * 100.0 / total_delta)


def _health_samples(url: str, *, count: int, timeout: float) -> list[dict[str, Any]]:
    samples = []
    for _ in range(max(1, count)):
        started = time.monotonic()
        status = 0
        error = ''
        try:
            with urllib.request.urlopen(url, timeout=max(0.1, timeout)) as response:
                status = int(response.status)
                response.read(128)
        except Exception as exc:  # noqa: BLE001 - unavailable telemetry must fail closed
            error = f'{type(exc).__name__}:{str(exc)[:120]}'
        samples.append({
            'ok': 200 <= status < 300 and not error,
            'status': status,
            'latency_seconds': round(time.monotonic() - started, 4),
            'error': error,
        })
    return samples


def _recent_journal(window_minutes: float) -> str | None:
    try:
        result = subprocess.run(
            [
                'journalctl', '-u', 'mcn-backend.service', '-u', 'nginx.service',
                '--since', f'-{max(0.1, window_minutes):g} min', '--no-pager',
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _recent_journal_json(window_minutes: float) -> str | None:
    try:
        result = subprocess.run(
            [
                'journalctl', '-u', 'mcn-backend.service', '-u', 'nginx.service',
                '--since', f'-{max(0.1, window_minutes):g} min', '--no-pager',
                '--output', 'json',
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _journal_incident_summary(
    journal: str,
    needle: str,
    *,
    dedupe_seconds: float,
    cooldown_seconds: float,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Deduplicate cascaded journal records and retain only a short cooldown."""
    timestamps: list[datetime] = []
    unparseable_matches = 0
    raw_records = 0
    for line in str(journal or '').splitlines():
        if needle not in line:
            continue
        raw_records += 1
        try:
            payload = json.loads(line)
            message = str(payload.get('MESSAGE') or '')
            stamp = int(str(payload.get('__REALTIME_TIMESTAMP') or '').strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            unparseable_matches += 1
            continue
        if needle not in message:
            raw_records -= 1
            continue
        timestamps.append(datetime.fromtimestamp(stamp / 1_000_000, timezone.utc))

    timestamps.sort()
    incidents: list[datetime] = []
    dedupe = max(0.0, float(dedupe_seconds))
    for occurred_at in timestamps:
        if not incidents or (occurred_at - incidents[-1]).total_seconds() > dedupe:
            incidents.append(occurred_at)
        else:
            incidents[-1] = occurred_at

    observed_at = now_utc or datetime.now(timezone.utc)
    cooldown = max(0.0, float(cooldown_seconds))
    blocking_incidents = sum(
        0.0 <= (observed_at - occurred_at).total_seconds() <= cooldown
        for occurred_at in incidents
    ) + unparseable_matches
    latest_age = None
    if incidents:
        latest_age = max(0.0, (observed_at - incidents[-1]).total_seconds())
    return {
        'raw_record_count': raw_records,
        'incident_count': len(incidents) + unparseable_matches,
        'blocking_incident_count': blocking_incidents,
        'latest_incident_age_seconds': None if latest_age is None else round(latest_age, 3),
        'dedupe_seconds': dedupe,
        'cooldown_seconds': cooldown,
    }


def _nginx_504_count(path: Path, window_minutes: float) -> int | None:
    if not path.is_file():
        return None
    try:
        with path.open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 2 * 1024 * 1024), os.SEEK_SET)
            text = handle.read().decode('utf-8', 'replace')
    except OSError:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(0.1, window_minutes))
    count = 0
    for stamp, status in re.findall(r'\[([^\]]+)\]\s+"[^"]*"\s+(\d{3})\s', text):
        if status != '504':
            continue
        try:
            occurred_at = datetime.strptime(stamp, '%d/%b/%Y:%H:%M:%S %z')
        except ValueError:
            continue
        count += occurred_at.astimezone(timezone.utc) >= cutoff
    return count


def _mutex_available(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+', encoding='utf-8') as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False
    return True


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    state = filesystem_state(Path(args.path))
    min_free = int(max(args.min_free_gb, 0.0) * GIB)
    checks: dict[str, Any] = {}
    reasons: list[str] = []

    def add(name: str, ok: bool, **detail: Any) -> None:
        checks[name] = {'ok': ok, **detail}
        if not ok:
            reasons.append(name)

    add(
        'disk_guard',
        float(state['used_percent']) < args.max_used_percent and int(state['available_bytes']) >= min_free,
        used_percent=state['used_percent'],
        max_used_percent=args.max_used_percent,
        available_bytes=state['available_bytes'],
        min_free_bytes=min_free,
    )
    if args.min_mem_available_gb is not None:
        available = _mem_available_bytes()
        minimum = int(max(0.0, args.min_mem_available_gb) * GIB)
        add('memory_guard', available is not None and available >= minimum, available_bytes=available, minimum_bytes=minimum)
    if args.max_load1 is not None:
        try:
            load1 = float(os.getloadavg()[0])
        except OSError:
            load1 = None
        add('load_guard', load1 is not None and load1 <= args.max_load1, value=load1, maximum=args.max_load1)
    if args.max_iowait_percent is not None:
        iowait = _iowait_percent(args.iowait_sample_seconds)
        add(
            'iowait_guard',
            iowait is not None and iowait <= args.max_iowait_percent,
            value_percent=iowait,
            maximum_percent=args.max_iowait_percent,
            sample_seconds=args.iowait_sample_seconds,
        )
    if args.backend_health_url:
        samples = _health_samples(
            args.backend_health_url,
            count=args.backend_health_checks,
            timeout=args.backend_health_timeout_seconds,
        )
        add(
            'backend_health_guard',
            len(samples) == max(1, args.backend_health_checks) and all(
                sample['ok'] and sample['latency_seconds'] <= args.max_backend_health_latency_seconds
                for sample in samples
            ),
            samples=samples,
            maximum_latency_seconds=args.max_backend_health_latency_seconds,
        )

    needs_logs = args.observe_api_slow or any(value is not None for value in (
        args.max_db_locked_count, args.max_api_slow_count, args.max_nginx_504_count,
    ))
    if needs_logs:
        journal = _recent_journal(args.recent_window_minutes)
        lock_journal = (
            _recent_journal_json(args.recent_window_minutes)
            if args.max_db_locked_count is not None else None
        )
        locked = None if lock_journal is None else _journal_incident_summary(
            lock_journal,
            'database is locked',
            dedupe_seconds=args.db_lock_event_dedupe_seconds,
            cooldown_seconds=args.db_lock_cooldown_seconds,
        )
        slow = None if journal is None else _ordinary_api_slow_count(
            journal, args.api_slow_threshold_ms,
        )
        journal_504 = None if journal is None else journal.count(' 504 ')
        access_504 = _nginx_504_count(Path(args.nginx_access_log), args.recent_window_minutes)
        nginx_known = [value for value in (journal_504, access_504) if value is not None]
        nginx_504 = max(nginx_known) if nginx_known else None
        if args.max_db_locked_count is not None:
            busy_paths = [
                path for path in args.db_lock_path
                if not _mutex_available(Path(path))
            ]
            blocking_count = None if locked is None else int(locked['blocking_incident_count'])
            add(
                'database_locked_guard',
                locked is not None
                and not busy_paths
                and blocking_count is not None
                and blocking_count <= args.max_db_locked_count,
                count=blocking_count,
                current_busy_paths=busy_paths,
                observed=locked,
            )
        if args.max_api_slow_count is not None:
            add('api_slow_guard', slow is not None and slow <= args.max_api_slow_count, count=slow)
        elif args.observe_api_slow:
            checks['api_slow_observation'] = {
                'ok': True,
                'blocking': False,
                'count': slow,
                'threshold_ms': args.api_slow_threshold_ms,
            }
        if args.max_nginx_504_count is not None:
            add('nginx_504_guard', nginx_504 is not None and nginx_504 <= args.max_nginx_504_count, count=nginx_504)
    if args.mutex_lock_path:
        add('heavy_batch_mutex_guard', _mutex_available(Path(args.mutex_lock_path)), path=args.mutex_lock_path)

    allowed = not reasons
    admission = 'allowed'
    if not allowed:
        admission = 'skipped_disk_guard' if reasons == ['disk_guard'] else 'blocked_resource_guard'
    return {
        'ok': allowed,
        'admission': admission,
        'reasons': reasons,
        'checks': checks,
        'max_used_percent': args.max_used_percent,
        'min_free_bytes': min_free,
        **state,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Fail closed before a heavy MCN batch job starts.')
    parser.add_argument('--path', default='/opt/mcn-ai-automation/data')
    parser.add_argument('--max-used-percent', type=float, default=85.0)
    parser.add_argument('--min-free-gb', type=float, default=10.0)
    parser.add_argument('--min-mem-available-gb', type=float)
    parser.add_argument('--max-load1', type=float)
    parser.add_argument('--max-iowait-percent', type=float)
    parser.add_argument('--iowait-sample-seconds', type=float, default=2.0)
    parser.add_argument('--backend-health-url', default='')
    parser.add_argument('--backend-health-checks', type=int, default=3)
    parser.add_argument('--backend-health-timeout-seconds', type=float, default=2.0)
    parser.add_argument('--max-backend-health-latency-seconds', type=float, default=0.5)
    parser.add_argument('--recent-window-minutes', type=float, default=5.0)
    parser.add_argument('--max-db-locked-count', type=int)
    parser.add_argument('--db-lock-event-dedupe-seconds', type=float, default=10.0)
    parser.add_argument('--db-lock-cooldown-seconds', type=float, default=30.0)
    parser.add_argument('--db-lock-path', action='append', default=[])
    parser.add_argument('--max-api-slow-count', type=int)
    parser.add_argument('--observe-api-slow', action='store_true')
    parser.add_argument('--api-slow-threshold-ms', type=float, default=1000.0)
    parser.add_argument('--max-nginx-504-count', type=int)
    parser.add_argument('--nginx-access-log', default='/var/log/nginx/access.log')
    parser.add_argument('--mutex-lock-path')
    return parser


def main() -> int:
    result = evaluate(build_parser().parse_args())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
