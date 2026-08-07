from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from collections import Counter, deque
from typing import Any, Deque, Dict, Optional


SLOW_SQL_THRESHOLD_MS = float(os.getenv('SQLITE_OBSERVABILITY_SLOW_SQL_MS') or '500')
SLOW_COMMIT_THRESHOLD_MS = float(os.getenv('SQLITE_OBSERVABILITY_SLOW_COMMIT_MS') or '500')
RECENT_EVENT_LIMIT = int(os.getenv('SQLITE_OBSERVABILITY_RECENT_LIMIT') or '80')
RECORD_SQLITE_ERRORS = str(os.getenv('SQLITE_OBSERVABILITY_RECORD_SQL_ERRORS') or '').strip().lower() in {'1', 'true', 'yes', 'on'}


_SQL_TABLE_RE = re.compile(
    r'^\s*(?:WITH\s+.+?\s+)?(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO|SELECT\s+.+?\s+FROM|CREATE\s+(?:TABLE|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?|ALTER\s+TABLE)\s+[`"\[]?([A-Za-z_][A-Za-z0-9_]*)',
    re.IGNORECASE | re.DOTALL,
)
_SQL_OP_RE = re.compile(r'^\s*([A-Za-z]+)', re.IGNORECASE)


class _SQLiteObservability:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.counters: Counter[str] = Counter()
        self.by_source: Counter[str] = Counter()
        self.by_table: Counter[str] = Counter()
        self.recent_events: Deque[Dict[str, Any]] = deque(maxlen=max(RECENT_EVENT_LIMIT, 1))

    def record(self, event: Dict[str, Any]) -> None:
        source = str(event.get('source') or 'unknown')
        table = str(event.get('table') or 'unknown')
        kind = str(event.get('kind') or 'event')
        with self._lock:
            self.counters[kind] += 1
            self.counters['events_total'] += 1
            self.by_source[source] += 1
            self.by_table[table] += 1
            payload = dict(event)
            payload['ts'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
            self.recent_events.append(payload)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(self.started_at)),
                'slow_sql_threshold_ms': SLOW_SQL_THRESHOLD_MS,
                'slow_commit_threshold_ms': SLOW_COMMIT_THRESHOLD_MS,
                'counters': dict(self.counters),
                'top_sources': self.by_source.most_common(20),
                'top_tables': self.by_table.most_common(20),
                'recent_events': list(self.recent_events),
            }


OBSERVABILITY = _SQLiteObservability()


def _classify_sql(sql: Any) -> Dict[str, str]:
    text = str(sql or '').strip()
    op_match = _SQL_OP_RE.search(text)
    table_match = _SQL_TABLE_RE.search(text)
    op = (op_match.group(1).upper() if op_match else 'UNKNOWN')[:24]
    table = (table_match.group(1) if table_match else 'unknown')[:80]
    return {'operation': op, 'table': table}


def _is_lock_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return 'database is locked' in text or 'database table is locked' in text or 'database schema is locked' in text


class ObservedSQLiteConnection(sqlite3.Connection):
    _observability_source: str = 'unknown'

    def execute(self, sql: Any, parameters: Any = (), /):  # type: ignore[override]
        return self._observed_call('execute', sql, super().execute, sql, parameters)

    def executemany(self, sql: Any, parameters: Any, /):  # type: ignore[override]
        return self._observed_call('executemany', sql, super().executemany, sql, parameters)

    def executescript(self, sql_script: str, /):  # type: ignore[override]
        return self._observed_call('executescript', sql_script, super().executescript, sql_script)

    def commit(self) -> None:  # type: ignore[override]
        started = time.perf_counter()
        try:
            return super().commit()
        except sqlite3.Error as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            is_lock_error = _is_lock_error(exc)
            if is_lock_error or RECORD_SQLITE_ERRORS:
                OBSERVABILITY.record({
                    'kind': 'lock_error' if is_lock_error else 'sqlite_error',
                    'source': self._observability_source,
                    'operation': 'COMMIT',
                    'table': 'transaction',
                    'duration_ms': round(duration_ms, 1),
                    'error': str(exc)[:200],
                })
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            if duration_ms >= SLOW_COMMIT_THRESHOLD_MS:
                OBSERVABILITY.record({
                    'kind': 'slow_commit',
                    'source': self._observability_source,
                    'operation': 'COMMIT',
                    'table': 'transaction',
                    'duration_ms': round(duration_ms, 1),
                })

    def rollback(self) -> None:  # type: ignore[override]
        started = time.perf_counter()
        try:
            return super().rollback()
        except sqlite3.Error as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            is_lock_error = _is_lock_error(exc)
            if is_lock_error or RECORD_SQLITE_ERRORS:
                OBSERVABILITY.record({
                    'kind': 'lock_error' if is_lock_error else 'sqlite_error',
                    'source': self._observability_source,
                    'operation': 'ROLLBACK',
                    'table': 'transaction',
                    'duration_ms': round(duration_ms, 1),
                    'error': str(exc)[:200],
                })
            raise

    def _observed_call(self, call_kind: str, sql: Any, fn: Any, *args: Any) -> Any:
        started = time.perf_counter()
        meta = _classify_sql(sql)
        try:
            return fn(*args)
        except sqlite3.Error as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            is_lock_error = _is_lock_error(exc)
            if is_lock_error or RECORD_SQLITE_ERRORS:
                OBSERVABILITY.record({
                    'kind': 'lock_error' if is_lock_error else 'sqlite_error',
                    'source': self._observability_source,
                    'call': call_kind,
                    **meta,
                    'duration_ms': round(duration_ms, 1),
                    'error': str(exc)[:200],
                })
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            if duration_ms >= SLOW_SQL_THRESHOLD_MS:
                OBSERVABILITY.record({
                    'kind': 'slow_sql',
                    'source': self._observability_source,
                    'call': call_kind,
                    **meta,
                    'duration_ms': round(duration_ms, 1),
                })


def connect_observed_sqlite(
    database: str,
    *,
    source: str,
    timeout: float = 30.0,
    check_same_thread: bool = True,
    uri: bool = False,
) -> ObservedSQLiteConnection:
    conn = sqlite3.connect(
        database,
        timeout=timeout,
        check_same_thread=check_same_thread,
        factory=ObservedSQLiteConnection,
        uri=uri,
    )
    conn._observability_source = str(source or 'unknown')[:120]
    return conn


def sqlite_observability_snapshot() -> Dict[str, Any]:
    from app.sqlite_job_lock import sqlite_lock_metrics_snapshot

    return {**OBSERVABILITY.snapshot(), **sqlite_lock_metrics_snapshot()}
