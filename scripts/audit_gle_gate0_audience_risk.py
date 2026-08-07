#!/usr/bin/env python3
"""Publish a bounded GET-only G0-04A audience-risk evidence fragment."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.growth.gate0_audience_risk_audit import (
    G004AAudienceRiskError,
    artifact_manifest,
    build_artifacts,
)
from app.growth.gate0_topology_audit import canonical_json


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise G004AAudienceRiskError("G004A_INPUT_INVALID") from exc
    if not isinstance(value, dict):
        raise G004AAudienceRiskError("G004A_INPUT_INVALID")
    return value


def _publish(path: Path, content: str) -> None:
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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="GLE G0-04A GET-only audience-risk audit")
    result.add_argument("--request", type=Path, required=True)
    result.add_argument("--g004-manifest", type=Path, required=True)
    result.add_argument("--g004-receipt", type=Path, required=True)
    result.add_argument("--g004-evidence", type=Path, required=True)
    result.add_argument("--token-env", default="META_ACCESS_TOKEN")
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--evidence-output", type=Path, required=True)
    result.add_argument("--manifest-output", type=Path, required=True)
    result.add_argument("--execute-read-only", action="store_true")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    outputs = [args.output.resolve(), args.evidence_output.resolve(), args.manifest_output.resolve()]
    if (
        not args.execute_read_only
        or not args.token_env.isidentifier()
        or len(set(outputs)) != 3
        or any(path.exists() for path in outputs)
        or len({path.parent for path in outputs}) != 1
    ):
        print("G004A_INPUT_INVALID", file=sys.stderr)
        return 64
    token = os.environ.get(args.token_env, "")
    if not token:
        print("G004A_TOKEN_ENV_MISSING", file=sys.stderr)
        return 64
    try:
        import requests

        result = build_artifacts(
            request=_read(args.request),
            g004_manifest=_read(args.g004_manifest),
            g004_receipt=_read(args.g004_receipt),
            g004_evidence=_read(args.g004_evidence),
            session=requests.Session(), access_token=token,
        )
        receipt_text = canonical_json(result["receipt"]) + "\n"
        evidence_text = canonical_json(result["evidence"]) + "\n"
        _publish(args.evidence_output, evidence_text)
        _publish(args.output, receipt_text)
        manifest = artifact_manifest(
            result["receipt"], result["evidence"],
            receipt_file=args.output.name, evidence_file=args.evidence_output.name,
        )
        _publish(args.manifest_output, canonical_json(manifest) + "\n")
        sys.stdout.write(receipt_text)
        return 0 if result["receipt"]["outcome"] == "PASS" else 2
    except G004AAudienceRiskError as exc:
        print(str(exc), file=sys.stderr)
        return 64
    except Exception:
        print("G004A_UNEXPECTED_FAILURE", file=sys.stderr)
        return 66


if __name__ == "__main__":
    raise SystemExit(main())
