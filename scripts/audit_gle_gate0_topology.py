#!/usr/bin/env python3
"""Run the bounded GLE G0-04 audit. Network execution is explicit and GET-only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.growth.gate0_topology_audit import (
    G004ContractError,
    G004GraphError,
    G004SourceError,
    audit_snapshot_bundle,
    canonical_json,
    exit_code_for_receipt,
)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID") from exc
    if not isinstance(value, dict):
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="GLE G0-04 GET-only topology audit")
    result.add_argument("--request", type=Path, required=True)
    result.add_argument("--actor-registry", type=Path, required=True)
    result.add_argument("--database", type=Path, required=True)
    result.add_argument("--database-sha256", required=True)
    result.add_argument("--token-env", default="META_ACCESS_TOKEN")
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--evidence-output", type=Path, required=True)
    result.add_argument("--manifest-output", type=Path, required=True)
    result.add_argument("--execute-read-only", action="store_true")
    return result


def _publish_new(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    if not args.execute_read_only:
        print("G004_INPUT_SCHEMA_INVALID", file=sys.stderr)
        return 64
    outputs = [args.output.resolve(), args.evidence_output.resolve(), args.manifest_output.resolve()]
    if len(set(outputs)) != 3 or any(path.exists() for path in outputs):
        print("G004_OUTPUT_NOT_IMMUTABLE", file=sys.stderr)
        return 64
    if not args.token_env.isidentifier():
        print("G004_INPUT_SCHEMA_INVALID", file=sys.stderr)
        return 64
    token = os.environ.get(args.token_env, "")
    if not token:
        print("G004_TOKEN_ENV_MISSING", file=sys.stderr)
        return 64
    try:
        import requests

        result = audit_snapshot_bundle(
            request=_read_json(args.request),
            actor_registry=_read_json(args.actor_registry),
            db_path=args.database,
            expected_db_sha256=args.database_sha256,
            session=requests.Session(),
            access_token=token,
        )
        receipt = result["receipt"]
        serialized = canonical_json(receipt) + "\n"
        evidence_serialized = canonical_json(result["evidence_bundle"]) + "\n"
        _publish_new(args.evidence_output, evidence_serialized)
        _publish_new(args.output, serialized)
        manifest = {
            "schema_version": "gle-g0-04-artifact-manifest-v1",
            "receipt_file": args.output.name,
            "receipt_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "evidence_file": args.evidence_output.name,
            "evidence_sha256": hashlib.sha256(evidence_serialized.encode("utf-8")).hexdigest(),
            "committed": True,
        }
        _publish_new(args.manifest_output, canonical_json(manifest) + "\n")
        sys.stdout.write(serialized)
        return exit_code_for_receipt(receipt)
    except G004ContractError as exc:
        print(str(exc), file=sys.stderr)
        return 64
    except (G004SourceError, G004GraphError) as exc:
        print(str(exc), file=sys.stderr)
        return 66
    except Exception:
        print("G004_UNEXPECTED_FAILURE", file=sys.stderr)
        return 66


if __name__ == "__main__":
    raise SystemExit(main())
