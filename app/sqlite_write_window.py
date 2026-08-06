from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.sqlite_bootstrap import (
    ensure_sqlite_ready,
    sqlite_busy_timeout_ms,
    sqlite_write_window_timeout_seconds,
)
from app.sqlite_job_lock import JobLockBusy, SQLiteJobLock, acquire_sqlite_job_lock
from app.sqlite_observability import ObservedSQLiteConnection


_WRITE_SQL = re.compile(
    r"^\s*(?:"
    r"BEGIN\s+(?:IMMEDIATE|EXCLUSIVE)|"
    r"INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|VACUUM|REINDEX"
    r")\b",
    re.IGNORECASE,
)
_TEMP_WRITE_SQL = re.compile(
    r"^\s*(?:"
    r"(?:CREATE|DROP)\s+TEMP(?:ORARY)?\b|"
    r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?temp\.|"
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+temp\.|"
    r"(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)"
    r"\s+(?:temp\.)?[`\"\[]?_(?:source|stage|tmp|linky)_"
    r")",
    re.IGNORECASE,
)


class SQLiteWriteWindowTimeout(RuntimeError):
    pass


class SQLiteWriteWindowBusy(RuntimeError):
    pass


class ShortWriteWindowConnection(ObservedSQLiteConnection):
    _write_lock_name = "sqlite-etl"
    _write_lock: SQLiteJobLock | None = None
    _write_window_started = 0.0
    _write_window_timeout_seconds = 5.0
    _write_lock_timeout_seconds: float | None = None

    def _acquire_write_window(self, sql: Any) -> None:
        text = str(sql or "")
        if (
            self._write_lock is not None
            or not _WRITE_SQL.match(text)
            or _TEMP_WRITE_SQL.match(text)
        ):
            return
        try:
            self._write_lock = acquire_sqlite_job_lock(
                self._write_lock_name,
                timeout_seconds=self._write_lock_timeout_seconds,
                wait_forever=self._write_lock_timeout_seconds is None,
                auto_release=False,
            )
        except JobLockBusy as exc:
            raise SQLiteWriteWindowBusy(
                f"sqlite_write_window_busy:{self._write_lock_name}"
            ) from exc
        self._write_window_started = time.monotonic()

    def _release_write_window(self) -> None:
        lock, self._write_lock = self._write_lock, None
        self._write_window_started = 0.0
        if lock is not None:
            lock.release()

    def _call_with_write_window(self, sql: Any, fn: Any, *args: Any) -> Any:
        self._acquire_write_window(sql)
        try:
            result = fn(*args)
            # DDL and other autocommit statements do not produce a later
            # commit() callback.  Never retain the process-wide writer lock
            # when SQLite reports that no transaction is open.
            if not self.in_transaction:
                self._release_write_window()
            return result
        except Exception:
            super().rollback()
            self._release_write_window()
            raise

    def execute(self, sql: Any, parameters: Any = (), /):  # type: ignore[override]
        return self._call_with_write_window(sql, super().execute, sql, parameters)

    def executemany(self, sql: Any, parameters: Any, /):  # type: ignore[override]
        return self._call_with_write_window(sql, super().executemany, sql, parameters)

    def executescript(self, sql_script: str, /):  # type: ignore[override]
        self._acquire_write_window(sql_script)
        try:
            result = super().executescript(sql_script)
            # sqlite3.executescript() implicitly commits before running the
            # script.  When the script does not leave an explicit transaction
            # open, there is no later commit() callback on which to release the
            # process-wide writer lock.
            if not self.in_transaction:
                self._release_write_window()
            return result
        except Exception:
            super().rollback()
            self._release_write_window()
            raise

    def commit(self) -> None:  # type: ignore[override]
        if (
            self._write_lock is not None
            and time.monotonic() - self._write_window_started
            > self._write_window_timeout_seconds
        ):
            duration = time.monotonic() - self._write_window_started
            super().rollback()
            self._release_write_window()
            raise SQLiteWriteWindowTimeout(
                f"sqlite_write_window_timeout:{duration:.3f}s"
            )
        try:
            super().commit()
        finally:
            self._release_write_window()

    def rollback(self) -> None:  # type: ignore[override]
        try:
            super().rollback()
        finally:
            self._release_write_window()

    def close(self) -> None:  # type: ignore[override]
        try:
            if self.in_transaction:
                super().rollback()
        finally:
            self._release_write_window()
            super().close()


def connect_short_write_sqlite(
    database: str | Path,
    *,
    lock_name: str = "sqlite-etl",
    source: str = "batch-short-write",
    timeout: float | None = None,
    busy_timeout_ms_override: int | None = None,
    write_window_timeout_seconds: float | None = None,
    write_lock_timeout_seconds: float | None = None,
    uri: bool = False,
) -> ShortWriteWindowConnection:
    ensure_sqlite_ready(database, profile="batch")
    configured_busy_timeout_ms = sqlite_busy_timeout_ms("batch")
    busy_timeout_ms = (
        configured_busy_timeout_ms
        if busy_timeout_ms_override is None
        else max(0, int(busy_timeout_ms_override))
    )
    conn = sqlite3.connect(
        str(database),
        timeout=(busy_timeout_ms / 1000.0 if timeout is None else float(timeout)),
        uri=uri,
        factory=ShortWriteWindowConnection,
    )
    conn._write_lock_name = str(lock_name)
    conn._write_window_timeout_seconds = (
        sqlite_write_window_timeout_seconds()
        if write_window_timeout_seconds is None
        else max(0.1, float(write_window_timeout_seconds))
    )
    conn._write_lock_timeout_seconds = (
        None
        if write_lock_timeout_seconds is None
        else max(0.0, float(write_lock_timeout_seconds))
    )
    conn._observability_source = str(source or "batch-short-write")[:120]
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return conn
