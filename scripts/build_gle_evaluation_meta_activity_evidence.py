#!/usr/bin/env python3
"""Capture and publish the bounded S04-01B4 GET-only Meta evidence fragment."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.growth.evaluation_meta_activity_evidence import (  # noqa: E402
    MAX_REGISTRY_BYTES,
    MAX_REQUEST_BYTES,
    MetaActivityEvidenceError,
    read_external_json,
    write_meta_activity_evidence_artifact,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Build the bounded GET-only GLE Meta activity/current-state evidence fragment",
    )
    value.add_argument("--request", type=Path, required=True)
    value.add_argument("--expected-request-sha256", required=True)
    value.add_argument("--actor-registry", type=Path, required=True)
    value.add_argument("--expected-actor-registry-sha256", required=True)
    value.add_argument("--output-dir", type=Path, required=True)
    value.add_argument("--token-env", default="META_ACCESS_TOKEN")
    value.add_argument("--execute-read-only", action="store_true")
    return value


def _exit_code(code: str) -> int:
    if "OUTPUT_EXISTS" in code:
        return 73
    if "DURABILITY_UNCERTAIN" in code:
        return 74
    if "GRAPH_CAPTURE_FAILED" in code:
        return 66
    return 64


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.execute_read_only or not args.token_env.isidentifier():
        print("G104B4_EXPLICIT_READ_ONLY_EXECUTION_REQUIRED", file=sys.stderr)
        return 64
    token = os.environ.get(args.token_env, "")
    if not token:
        print("G104B4_TOKEN_MISSING", file=sys.stderr)
        return 64
    try:
        request, request_raw = read_external_json(
            args.request,
            args.expected_request_sha256,
            maximum=MAX_REQUEST_BYTES,
            code="G104B4_REQUEST_ARTIFACT_INVALID",
        )
        _ = request
        registry, registry_raw = read_external_json(
            args.actor_registry,
            args.expected_actor_registry_sha256,
            maximum=MAX_REGISTRY_BYTES,
            code="G104B4_REGISTRY_ARTIFACT_INVALID",
        )
        _ = registry
        import requests

        session = requests.Session()
        session.trust_env = False
        try:
            result = write_meta_activity_evidence_artifact(
                args.output_dir,
                request_raw=request_raw,
                expected_request_sha256=args.expected_request_sha256,
                actor_registry_raw=registry_raw,
                expected_actor_registry_sha256=args.expected_actor_registry_sha256,
                session=session,
                access_token=token,
                now=datetime.now(timezone.utc),
            )
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        manifest = result["manifest"]
        print(json.dumps({
            "status": manifest["status"],
            "manifest_sha256": result["manifest_sha256"],
            "ceiling": manifest["ceiling"],
            "exit_semantics": "VALID_OBSERVATION_FRAGMENT_NOT_GATE_PASS",
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 2
    except MetaActivityEvidenceError as exc:
        code = str(exc).split(":", 1)[0]
        print(code, file=sys.stderr)
        return _exit_code(code)
    except (OSError, ValueError):
        print("G104B4_UNEXPECTED_IO_FAILURE", file=sys.stderr)
        return 74
    except Exception:
        print("G104B4_UNEXPECTED_FAILURE", file=sys.stderr)
        return 74


if __name__ == "__main__":
    raise SystemExit(main())
