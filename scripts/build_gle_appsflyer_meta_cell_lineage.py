#!/usr/bin/env python3
"""Build one non-promoting AppsFlyer + Meta Cell-lineage evidence file."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
from typing import Iterable

import requests


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.appsflyer_meta_cell_lineage import (  # noqa: E402
    MAX_CSV_BYTES,
    MAX_JSON_BYTES,
    CellLineageEvidenceError,
    capture_meta_graph,
    derive_lineage_evidence,
    json_bytes,
    parse_request,
)


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _parent_identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_mtime_ns, value.st_ctime_ns)


def _parent_binding_identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode)


def _read_stable_file(
    path: Path,
    *,
    maximum: int,
    allowed_modes: Iterable[int],
) -> tuple[bytes, str]:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise CellLineageEvidenceError("G104B6_INPUT_PATH_INVALID")
    parent = path.parent.resolve(strict=True)
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    parent_fd = os.open(parent, dir_flags)
    fd = -1
    try:
        parent_before = os.fstat(parent_fd)
        fd = os.open(path.name, file_flags, dir_fd=parent_fd)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) not in set(allowed_modes)
            or before.st_size <= 0
            or before.st_size > maximum
        ):
            raise CellLineageEvidenceError("G104B6_INPUT_FILE_INVALID")
        chunks: list[bytes] = []
        consumed = 0
        while True:
            chunk = os.read(fd, min(65536, maximum + 1 - consumed))
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > maximum:
                raise CellLineageEvidenceError("G104B6_INPUT_FILE_INVALID")
            chunks.append(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        named_after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        parent_after = os.fstat(parent_fd)
        if (
            _identity(before) != _identity(after)
            or _identity(before) != _identity(named_after)
            or _parent_identity(parent_before) != _parent_identity(parent_after)
            or _parent_identity(parent_before) != _parent_identity(os.stat(parent, follow_symlinks=False))
            or len(raw) != before.st_size
        ):
            raise CellLineageEvidenceError("G104B6_INPUT_CHANGED")
        return raw, hashlib.sha256(raw).hexdigest()
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _write_new_file(path: Path, raw: bytes) -> None:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise CellLineageEvidenceError("G104B6_OUTPUT_PATH_INVALID")
    parent = path.parent.resolve(strict=True)
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    parent_fd = os.open(parent, dir_flags)
    fd = -1
    try:
        parent_before = os.fstat(parent_fd)
        fd = os.open(path.name, file_flags, 0o600, dir_fd=parent_fd)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short_write")
            view = view[written:]
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        readback_chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            readback_chunks.append(chunk)
        if b"".join(readback_chunks) != raw:
            raise CellLineageEvidenceError("G104B6_OUTPUT_VERIFY_FAILED")
        written_state = os.fstat(fd)
        named_state = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(written_state.st_mode)
            or written_state.st_nlink != 1
            or stat.S_IMODE(written_state.st_mode) != 0o600
            or written_state.st_size != len(raw)
            or _identity(written_state) != _identity(named_state)
        ):
            raise CellLineageEvidenceError("G104B6_OUTPUT_VERIFY_FAILED")
        os.fsync(parent_fd)
        if _parent_binding_identity(parent_before) != _parent_binding_identity(os.fstat(parent_fd)):
            raise CellLineageEvidenceError("G104B6_OUTPUT_PARENT_CHANGED")
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build read-only AppsFlyer + Meta exact Cell-lineage evidence."
    )
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--appsflyer-csv", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-request-sha256", required=True)
    parser.add_argument(
        "--allow-env-proxy",
        action="store_true",
        help="Explicitly allow requests to use HTTP(S)_PROXY; never upgrades transport authority.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    session = None
    try:
        request_raw, request_sha = _read_stable_file(
            args.request.absolute(), maximum=MAX_JSON_BYTES, allowed_modes={0o600, 0o644}
        )
        if not re.fullmatch(r"[0-9a-f]{64}", args.expected_request_sha256):
            raise CellLineageEvidenceError("G104B6_REQUEST_SHA_INVALID")
        if request_sha != args.expected_request_sha256:
            raise CellLineageEvidenceError("G104B6_REQUEST_SHA_MISMATCH")
        request = parse_request(request_raw)
        csv_raw, _csv_sha = _read_stable_file(
            args.appsflyer_csv.absolute(), maximum=MAX_CSV_BYTES, allowed_modes={0o600, 0o644}
        )
        token = str(os.getenv("META_ADS_ACCESS_TOKEN") or "").strip()
        if not token:
            raise CellLineageEvidenceError("G104B6_META_TOKEN_MISSING")
        session = requests.Session()
        session.trust_env = bool(args.allow_env_proxy)
        captured_at = datetime.now(timezone.utc).isoformat()
        capture = capture_meta_graph(
            session=session,
            access_token=token,
            request=request,
            captured_at=captured_at,
        )
        evidence = derive_lineage_evidence(
            request=request,
            appsflyer_raw=csv_raw,
            meta_capture=capture,
        )
        raw = json_bytes(evidence)
        _write_new_file(args.output.absolute(), raw)
        print(json_bytes({
            "status": evidence["status"],
            "evidence_hash": evidence["evidence_hash"],
            "output_sha256": hashlib.sha256(raw).hexdigest(),
            "source_content_authority": evidence["ceiling"]["source_content_authority"],
            "gate0_result_effect": evidence["ceiling"]["gate0_result_effect"],
            "snapshot_effect": evidence["ceiling"]["snapshot_effect"],
            "not_gate_receipt": evidence["ceiling"]["not_gate_receipt"],
            "environment_proxy_enabled": bool(args.allow_env_proxy),
        }).decode("utf-8").strip())
        return 2
    except (CellLineageEvidenceError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 64
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
