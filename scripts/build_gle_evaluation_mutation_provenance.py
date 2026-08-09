#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.common import canonical_json
from app.growth.evaluation_mutation_provenance import (
    MutationProvenanceError,
    read_external_request,
    write_mutation_provenance_artifact,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build bounded, same-cutoff mutation-provenance evidence from externally "
            "pinned immutable SQLite bytes. This never emits a Snapshot or Gate receipt."
        )
    )
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--expected-request-sha256", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--database-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        _request, request_raw = read_external_request(
            args.request, args.expected_request_sha256,
        )
        manifest = write_mutation_provenance_artifact(
            args.output_dir,
            request_raw,
            expected_request_sha256=args.expected_request_sha256,
            source_snapshot_path=args.database,
            expected_source_snapshot_sha256=args.database_sha256,
        )
        manifest_sha = hashlib.sha256(
            (canonical_json(manifest) + "\n").encode("utf-8")
        ).hexdigest()
    except (MutationProvenanceError, OSError, sqlite3.Error) as exc:
        print(str(exc).split(":", 1)[0], file=sys.stderr)
        return 64
    print(canonical_json({
        "ok": False,
        "status": manifest["status"],
        "mutation_effect": manifest["ceiling"]["mutation_effect"],
        "complete_event_journal": manifest["ceiling"]["complete_event_journal"],
        "snapshot_emitted": manifest["ceiling"]["snapshot_emitted"],
        "replay_executed": manifest["ceiling"]["replay_executed"],
        "holdout_status": manifest["ceiling"]["holdout_status"],
        "gate0_result_effect": manifest["ceiling"]["gate0_result_effect"],
        "gate1_effect": manifest["ceiling"]["gate1_effect"],
        "manifest_hash": manifest["manifest_hash"],
        "manifest_sha256": manifest_sha,
    }))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
