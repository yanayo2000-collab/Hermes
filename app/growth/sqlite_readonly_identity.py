"""Cross-platform identity binding for SQLite opened through a held file descriptor."""

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


class SQLiteSourceIdentityError(ValueError):
    pass


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _database_list_identity(
    database_list: Iterable[sqlite3.Row | tuple[Any, ...]],
) -> tuple[tuple[int, str, str], ...]:
    try:
        return tuple((row[0], str(row[1]), str(row[2])) for row in database_list)
    except (IndexError, TypeError) as exc:
        raise SQLiteSourceIdentityError("sqlite_database_list_invalid") from exc


def _expected_alias(source_fd: int) -> str:
    return (
        f"/proc/self/fd/{source_fd}"
        if Path("/proc/self/fd").is_dir()
        else f"/dev/fd/{source_fd}"
    )


@dataclass
class HeldSQLiteSourceIdentity:
    reported_fd: int
    database_identity: tuple[tuple[int, str, str], ...]
    source_identity: tuple[int, ...]
    reported_identity: tuple[int, ...]

    def close(self) -> None:
        if self.reported_fd >= 0:
            os.close(self.reported_fd)
            self.reported_fd = -1


def hold_sqlite_source_identity(
    source_fd: int,
    sqlite_path: str,
    database_list: Iterable[sqlite3.Row | tuple[Any, ...]],
) -> HeldSQLiteSourceIdentity:
    """Hold SQLite's reported main file and bind it to the caller-held source inode."""
    reported_fd = -1
    try:
        identity = _database_list_identity(database_list)
        source_before = os.fstat(source_fd)
        if (
            len(identity) != 1
            or type(identity[0][0]) is not int
            or identity[0][0] != 0
            or identity[0][1] != "main"
            or not identity[0][2]
            or not Path(identity[0][2]).is_absolute()
            or not stat.S_ISREG(source_before.st_mode)
            or source_before.st_nlink != 1
            or sqlite_path != _expected_alias(source_fd)
        ):
            raise SQLiteSourceIdentityError("sqlite_source_identity_invalid")
        reported_path = identity[0][2]
        if reported_path == sqlite_path:
            reported_fd = os.dup(source_fd)
        else:
            reported_fd = os.open(
                reported_path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        reported_before = os.fstat(reported_fd)
        if (
            not stat.S_ISREG(reported_before.st_mode)
            or _file_identity(reported_before) != _file_identity(source_before)
        ):
            raise SQLiteSourceIdentityError("sqlite_source_identity_invalid")
        return HeldSQLiteSourceIdentity(
            reported_fd=reported_fd,
            database_identity=identity,
            source_identity=_file_identity(source_before),
            reported_identity=_file_identity(reported_before),
        )
    except SQLiteSourceIdentityError:
        if reported_fd >= 0:
            os.close(reported_fd)
        raise
    except OSError as exc:
        if reported_fd >= 0:
            os.close(reported_fd)
        raise SQLiteSourceIdentityError("sqlite_source_identity_invalid") from exc


def revalidate_sqlite_source_identity(
    conn: sqlite3.Connection,
    source_fd: int,
    sqlite_path: str,
    held: HeldSQLiteSourceIdentity,
) -> None:
    try:
        source_after = os.fstat(source_fd)
        reported_after = os.fstat(held.reported_fd)
        if (
            int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1
            or _database_list_identity(conn.execute("PRAGMA database_list").fetchall())
            != held.database_identity
            or _file_identity(source_after) != held.source_identity
            or _file_identity(reported_after) != held.reported_identity
            or _file_identity(reported_after) != _file_identity(source_after)
            or sqlite_path != _expected_alias(source_fd)
        ):
            raise SQLiteSourceIdentityError("sqlite_source_identity_invalid")
    except SQLiteSourceIdentityError:
        raise
    except (OSError, sqlite3.Error, IndexError, TypeError, ValueError) as exc:
        raise SQLiteSourceIdentityError("sqlite_source_identity_invalid") from exc
