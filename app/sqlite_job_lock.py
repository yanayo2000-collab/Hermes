from __future__ import annotations

import atexit
import fcntl
import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


DEFAULT_LOCK_DIR = Path(os.getenv('MCN_SQLITE_JOB_LOCK_DIR') or '/tmp/mcn-ai-automation-sqlite-job-locks')
_LOCK_HOLD_SECONDS: dict[str, deque[float]] = {}
_PROCESS_LOCKS: dict[tuple[int, str], dict[str, Any]] = {}
_PROCESS_LOCKS_GUARD = threading.RLock()
logger = logging.getLogger(__name__)


def _journal_lock_names() -> set[str]:
    raw = str(os.getenv('MCN_SQLITE_JOB_LOCK_JOURNAL_NAMES') or 'sqlite-etl')
    return {
        _safe_lock_name(item)
        for item in raw.split(',')
        if str(item or '').strip()
    }


def _emit_lock_hold_sample(name: str, hold_seconds: float) -> None:
    logger.info(
        'sqlite_lock_hold_seconds lock_name=%s seconds=%.6f',
        name,
        hold_seconds,
    )
    if name not in _journal_lock_names():
        return
    # systemd oneshot jobs do not configure an INFO logging handler.  Emit the
    # acceptance metric on stdout so journald receives a durable, parseable
    # sample instead of leaving it only in this process' in-memory deque.
    print(json.dumps({
        'event': 'sqlite_lock_hold_seconds',
        'lock_name': name,
        'seconds': round(hold_seconds, 6),
        'pid': os.getpid(),
    }, sort_keys=True), flush=True)


class JobLockBusy(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__('sqlite_job_lock_busy')
        self.payload = payload


def sqlite_job_lock_timeout_seconds(default: float = 0.0) -> float:
    raw = str(os.getenv('MCN_SQLITE_JOB_LOCK_TIMEOUT_SECONDS') or '').strip()
    if not raw:
        return float(default)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(default)


def _safe_lock_name(name: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9_.-]+', '-', str(name or '').strip()).strip('.-')
    return safe[:120] or 'sqlite-job'


@dataclass
class SQLiteJobLock:
    name: str
    path: Path
    handle: Any
    acquired_at: float
    process_key: tuple[int, str] | None = None
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.process_key is not None:
            with _PROCESS_LOCKS_GUARD:
                state = _PROCESS_LOCKS.get(self.process_key)
                if state is not None and state["handle"] is self.handle:
                    state["references"] -= 1
                    if state["references"] > 0:
                        return
                    _PROCESS_LOCKS.pop(self.process_key, None)
        hold_seconds = max(0.0, time.time() - self.acquired_at)
        _LOCK_HOLD_SECONDS.setdefault(self.name, deque(maxlen=1024)).append(
            hold_seconds
        )
        _emit_lock_hold_sample(self.name, hold_seconds)
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                self.handle.close()
            except Exception:
                pass

    def __enter__(self) -> 'SQLiteJobLock':
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def sqlite_lock_metrics_snapshot() -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for name, values in _LOCK_HOLD_SECONDS.items():
        samples = sorted(values)
        if not samples:
            continue
        index = min(
            len(samples) - 1,
            max(0, int((len(samples) * 0.95) + 0.999999) - 1),
        )
        metrics[name] = {
            'count': len(samples),
            'p95': samples[index],
            'max': samples[-1],
        }
    return {'sqlite_lock_hold_seconds': metrics}


def acquire_sqlite_job_lock(
    name: str,
    *,
    timeout_seconds: Optional[float] = None,
    wait_forever: bool = False,
    lock_dir: Optional[Path] = None,
    auto_release: bool = True,
    metadata: Optional[dict[str, Any]] = None,
) -> SQLiteJobLock:
    lock_name = _safe_lock_name(name)
    resolved_dir = Path(lock_dir or DEFAULT_LOCK_DIR)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    path = resolved_dir / f'{lock_name}.lock'
    process_key = (os.getpid(), str(path.resolve()))
    with _PROCESS_LOCKS_GUARD:
        state = _PROCESS_LOCKS.get(process_key)
        if state is not None:
            state["references"] += 1
            lock = SQLiteJobLock(
                name=lock_name,
                path=path,
                handle=state["handle"],
                acquired_at=state["acquired_at"],
                process_key=process_key,
            )
            if auto_release:
                atexit.register(lock.release)
            return lock
    handle = path.open('a+', encoding='utf-8')
    timeout = sqlite_job_lock_timeout_seconds() if timeout_seconds is None else max(0.0, float(timeout_seconds))
    deadline = None if wait_forever else time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError as exc:
            with _PROCESS_LOCKS_GUARD:
                state = _PROCESS_LOCKS.get(process_key)
                if state is not None:
                    state["references"] += 1
                    handle.close()
                    lock = SQLiteJobLock(
                        name=lock_name,
                        path=path,
                        handle=state["handle"],
                        acquired_at=state["acquired_at"],
                        process_key=process_key,
                    )
                    if auto_release:
                        atexit.register(lock.release)
                    return lock
            if deadline is not None and time.monotonic() >= deadline:
                handle.seek(0)
                holder = handle.read(4096).strip()
                handle.close()
                raise JobLockBusy({
                    'lock_name': lock_name,
                    'lock_path': str(path),
                    'holder': holder,
                    'timeout_seconds': timeout,
                }) from exc
            remaining = 0.25 if deadline is None else max(0.01, deadline - time.monotonic())
            time.sleep(min(0.25, remaining))

    acquired_at = time.time()
    payload = {
        'lock_name': lock_name,
        'pid': os.getpid(),
        'argv': sys.argv[:6],
        'acquired_at': int(acquired_at),
        'acquired_at_iso': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(acquired_at)),
    }
    for key in ('stage', 'job_type', 'task_id', 'guild_id', 'source'):
        value = (metadata or {}).get(key)
        if value not in (None, ''):
            payload[key] = str(value)[:240]
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    handle.flush()
    os.fsync(handle.fileno())
    with _PROCESS_LOCKS_GUARD:
        _PROCESS_LOCKS[process_key] = {
            "handle": handle,
            "acquired_at": acquired_at,
            "references": 1,
        }
    lock = SQLiteJobLock(
        name=lock_name,
        path=path,
        handle=handle,
        acquired_at=acquired_at,
        process_key=process_key,
    )
    if auto_release:
        atexit.register(lock.release)
    return lock


def print_job_lock_skip(exc: JobLockBusy) -> None:
    print(json.dumps({
        'ok': True,
        'skipped': 'sqlite_job_lock_busy',
        **exc.payload,
    }, ensure_ascii=False, sort_keys=True))
