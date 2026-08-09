#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.canonical_evaluation_contracts_v2 import (
    CanonicalEvaluationContractV2Error,
    canonical_json,
    validate_canonical_input_bundle_v2,
)
from app.growth.canonical_evaluation_contracts import CanonicalEvaluationContractError


MAX_BUNDLE_BYTES = 2 * 1024 * 1024


class CanonicalBundleInputError(ValueError):
    pass


def _fail(code: str) -> None:
    raise CanonicalBundleInputError(code)


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IMODE(value.st_mode),
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("G101C_INPUT_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def read_anchored_bundle(path_value: str, expected_sha256: str) -> dict[str, Any]:
    if len(expected_sha256) != 64 or any(char not in "0123456789abcdef" for char in expected_sha256):
        _fail("G101C_INPUT_ANCHOR_INVALID")
    path = Path(path_value)
    if not path.name or path.name in {".", ".."}:
        _fail("G101C_INPUT_PATH_INVALID")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError:
        _fail("G101C_INPUT_PARENT_INVALID")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    grand_fd = os.open(parent.parent, directory_flags)
    parent_fd = -1
    file_fd = -1
    try:
        parent_named_before = os.stat(parent.name, dir_fd=grand_fd, follow_symlinks=False)
        parent_fd = os.open(parent.name, directory_flags, dir_fd=grand_fd)
        parent_before = os.fstat(parent_fd)
        if _identity(parent_named_before) != _identity(parent_before) or not stat.S_ISDIR(parent_before.st_mode):
            _fail("G101C_INPUT_PARENT_IDENTITY_CHANGED")
        file_fd = os.open(path.name, file_flags, dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size > MAX_BUNDLE_BYTES
        ):
            _fail("G101C_INPUT_FILE_UNSAFE")
        chunks: list[bytes] = []
        consumed = 0
        while True:
            chunk = os.read(file_fd, min(65536, MAX_BUNDLE_BYTES + 1 - consumed))
            if not chunk:
                break
            chunks.append(chunk)
            consumed += len(chunk)
            if consumed > MAX_BUNDLE_BYTES:
                _fail("G101C_INPUT_FILE_TOO_LARGE")
        raw = b"".join(chunks)
        after = os.fstat(file_fd)
        named_after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        parent_after = os.fstat(parent_fd)
        parent_named_after = os.stat(parent.name, dir_fd=grand_fd, follow_symlinks=False)
        if (
            _identity(before) != _identity(after)
            or _identity(after) != _identity(named_after)
            or _identity(parent_before) != _identity(parent_after)
            or _identity(parent_after) != _identity(parent_named_after)
        ):
            _fail("G101C_INPUT_IDENTITY_CHANGED")
    except OSError:
        _fail("G101C_INPUT_READ_FAILED")
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(grand_fd)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        _fail("G101C_INPUT_ANCHOR_MISMATCH")
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _: _fail("G101C_INPUT_JSON_INVALID"),
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("G101C_INPUT_JSON_INVALID")
    if not isinstance(value, dict) or raw != (canonical_json(value) + "\n").encode("utf-8"):
        _fail("G101C_INPUT_NOT_CANONICAL")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate an externally SHA-256-anchored canonical v2 synthetic input bundle. "
            "A valid result remains schema-only and is not Snapshot, Replay, Golden, Holdout, or Gate evidence."
        )
    )
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        bundle = read_anchored_bundle(args.bundle, args.expected_sha256)
        validated = validate_canonical_input_bundle_v2(bundle)
    except (
        CanonicalBundleInputError,
        CanonicalEvaluationContractError,
        CanonicalEvaluationContractV2Error,
        OSError,
    ) as exc:
        print(str(exc).split(":", 1)[0], file=sys.stderr)
        return 64
    print(canonical_json({
        "ok": False,
        "status": "SCHEMA_VALIDATED_NO_AUTHORITY_EFFECT",
        "bundle_hash": validated["bundle_hash"],
        "validation_ceiling": validated["validation_ceiling"],
    }))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
