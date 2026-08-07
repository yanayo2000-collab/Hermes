#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.historical_lineage_candidates import (
    HistoricalLineageCandidateError,
    derive_lineage_candidates_from_audit_directory,
    write_lineage_candidate_bundle,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive audit-only GLE lineage candidates; DEV/VALIDATION remain blocked and Holdout is forbidden"
    )
    parser.add_argument("--audit-dir", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--derivation-id", required=True)
    parser.add_argument("--derived-at", required=True)
    args = parser.parse_args(argv)
    try:
        candidate = derive_lineage_candidates_from_audit_directory(
            args.audit_dir, expected_manifest_sha256=args.expected_manifest_sha256,
            derivation_id=args.derivation_id, derived_at=args.derived_at,
        )
        manifest = write_lineage_candidate_bundle(
            candidate, args.output_dir, audit_dir=args.audit_dir,
            expected_manifest_sha256=args.expected_manifest_sha256,
        )
    except HistoricalLineageCandidateError as exc:
        print(str(exc).split(":", 1)[0], file=sys.stderr)
        return 64
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
