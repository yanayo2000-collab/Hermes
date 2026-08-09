from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

import pytest

from app.growth.evaluation_cell_metric_evidence import (
    G005ContractError,
    _require_sidecars_absent as require_metric_sidecars_absent,
)
from app.growth.evaluation_mutation_provenance import (
    MutationProvenanceError,
    _require_sidecars_absent as require_mutation_sidecars_absent,
)
from app.growth.sqlite_readonly_identity import (
    SQLiteSourceIdentityError,
    hold_sqlite_source_identity,
    revalidate_sqlite_source_identity,
)


def _database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.execute("INSERT INTO sample(value) VALUES ('value')")
    path.chmod(0o600)


def _fd_alias(source_fd: int) -> str:
    return (
        f"/proc/self/fd/{source_fd}"
        if Path("/proc/self/fd").is_dir()
        else f"/dev/fd/{source_fd}"
    )


def _immutable_connection(source_fd: int) -> tuple[sqlite3.Connection, str]:
    alias = _fd_alias(source_fd)
    uri = f"file:{quote(alias, safe='/')}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    return connection, alias


def test_real_fd_alias_connection_accepts_same_inode_database_list(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _database(source)
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    connection, alias = _immutable_connection(source_fd)
    held = None
    try:
        database_list = connection.execute("PRAGMA database_list").fetchall()
        held = hold_sqlite_source_identity(source_fd, alias, database_list)
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "value"
        revalidate_sqlite_source_identity(connection, source_fd, alias, held)
        assert held.source_identity == held.reported_identity
    finally:
        if held is not None:
            held.close()
        connection.close()
        os.close(source_fd)


def test_canonical_reported_path_must_match_held_inode_and_single_main(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    other = tmp_path / "other.db"
    _database(source)
    _database(other)
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    alias = _fd_alias(source_fd)
    held = None
    try:
        held = hold_sqlite_source_identity(
            source_fd, alias, [(0, "main", str(source.resolve()))],
        )
        assert held.source_identity == held.reported_identity
        held.close()
        held = None
        with pytest.raises(SQLiteSourceIdentityError, match="sqlite_source_identity_invalid"):
            hold_sqlite_source_identity(
                source_fd, alias, [(0, "main", str(other.resolve()))],
            )
        with pytest.raises(SQLiteSourceIdentityError, match="sqlite_source_identity_invalid"):
            hold_sqlite_source_identity(
                source_fd,
                alias,
                [
                    (0, "main", str(source.resolve())),
                    (2, "attached", str(other.resolve())),
                ],
            )
    finally:
        if held is not None:
            held.close()
        os.close(source_fd)


def test_reported_symlink_is_rejected_without_following_it(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    reported = tmp_path / "reported.db"
    _database(source)
    reported.symlink_to(source)
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        with pytest.raises(SQLiteSourceIdentityError, match="sqlite_source_identity_invalid"):
            hold_sqlite_source_identity(
                source_fd, _fd_alias(source_fd), [(0, "main", str(reported))],
            )
    finally:
        os.close(source_fd)


def test_revalidation_rejects_query_only_being_disabled(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _database(source)
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    connection, alias = _immutable_connection(source_fd)
    held = None
    try:
        held = hold_sqlite_source_identity(
            source_fd, alias, connection.execute("PRAGMA database_list").fetchall(),
        )
        connection.execute("PRAGMA query_only=OFF")
        with pytest.raises(SQLiteSourceIdentityError, match="sqlite_source_identity_invalid"):
            revalidate_sqlite_source_identity(connection, source_fd, alias, held)
    finally:
        if held is not None:
            held.close()
        connection.close()
        os.close(source_fd)


def test_revalidation_rejects_an_attached_database(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    attached = tmp_path / "attached.db"
    _database(source)
    _database(attached)
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    connection, alias = _immutable_connection(source_fd)
    held = None
    try:
        held = hold_sqlite_source_identity(
            source_fd, alias, connection.execute("PRAGMA database_list").fetchall(),
        )
        connection.execute("ATTACH DATABASE ? AS attached", (str(attached),))
        with pytest.raises(SQLiteSourceIdentityError, match="sqlite_source_identity_invalid"):
            revalidate_sqlite_source_identity(connection, source_fd, alias, held)
    finally:
        if held is not None:
            held.close()
        connection.close()
        os.close(source_fd)


@pytest.mark.parametrize(
    ("require_absent", "error_type", "error_code"),
    [
        (require_metric_sidecars_absent, G005ContractError, "G005_SOURCE_SIDECAR_PRESENT"),
        (
            require_mutation_sidecars_absent,
            MutationProvenanceError,
            "G104B3_SOURCE_SIDECAR_PRESENT",
        ),
    ],
)
@pytest.mark.parametrize("kind", ["empty", "symlink", "fifo"])
def test_any_sqlite_sidecar_directory_entry_is_rejected(
    tmp_path: Path,
    require_absent,
    error_type: type[Exception],
    error_code: str,
    kind: str,
) -> None:
    source = tmp_path / "source.db"
    source.write_bytes(b"sqlite-bytes")
    sidecar = Path(str(source) + "-wal")
    if kind == "empty":
        sidecar.write_bytes(b"")
    elif kind == "symlink":
        sidecar.symlink_to(source)
    else:
        os.mkfifo(sidecar)
    parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(error_type, match=error_code):
            require_absent(source, parent_fd)
    finally:
        os.close(parent_fd)
