#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.canonical_evaluation_contracts import canonical_json
from app.growth.historical_maturing_triage import (
    HistoricalMaturingTriageError,
    derive_maturing_triage_from_audit_directory,
    write_maturing_triage_artifacts,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify captured MATURING current-context records for audit review only.")
    parser.add_argument("--audit-dir", required=True)
    parser.add_argument("--expected-audit-manifest-sha256", required=True)
    parser.add_argument("--triage-id", required=True)
    parser.add_argument("--derived-at", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        bundle = derive_maturing_triage_from_audit_directory(
            args.audit_dir,
            expected_manifest_sha256=args.expected_audit_manifest_sha256,
            triage_id=args.triage_id,
            derived_at=args.derived_at,
        )
        manifest = write_maturing_triage_artifacts(
            bundle,
            args.output_dir,
            audit_dir=args.audit_dir,
            expected_manifest_sha256=args.expected_audit_manifest_sha256,
        )
    except (HistoricalMaturingTriageError, OSError) as exc:
        print(str(exc).split(":", 1)[0], file=sys.stderr)
        return 64
    print(canonical_json({
        "ok": bundle["status"] == "AUDIT_CLASSIFIED",
        "status": bundle["status"],
        "maturing_count": bundle["coverage"]["triage_item_count"],
        "unknown_count": bundle["coverage"]["unknown_count"],
        "manual_review_count": bundle["coverage"]["manual_review_count"],
        "manifest_hash": manifest["manifest_hash"],
    }))
    return 0 if bundle["status"] == "AUDIT_CLASSIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
