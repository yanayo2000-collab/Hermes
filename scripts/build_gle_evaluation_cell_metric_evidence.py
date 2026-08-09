#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.common import canonical_json
from app.growth.evaluation_cell_metric_evidence import (
    read_external_canonical_json,
    write_cell_metric_evidence_artifact,
)
from app.growth.gate0_feasibility_assessment import G005ContractError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build an externally anchored, read-only same-cutoff Cell metric evidence "
            "subset. This does not emit a Snapshot or run Replay/Holdout/Gate."
        )
    )
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--expected-request-sha256", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--database-sha256", required=True)
    parser.add_argument("--qualified-transport-manifest", type=Path, required=True)
    parser.add_argument("--expected-qualified-transport-manifest-sha256", required=True)
    parser.add_argument("--qualified-transport-receipt", type=Path, required=True)
    parser.add_argument("--expected-qualified-transport-receipt-sha256", required=True)
    parser.add_argument("--evidence-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        _source_request, source_request_raw = read_external_canonical_json(
            args.request, args.expected_request_sha256,
        )
        manifest = write_cell_metric_evidence_artifact(
            args.output_dir,
            source_request_raw,
            evidence_id=args.evidence_id,
            source_request_sha256=args.expected_request_sha256,
            source_snapshot_path=args.database,
            source_snapshot_sha256=args.database_sha256,
            transport_manifest_path=args.qualified_transport_manifest,
            transport_manifest_sha256=args.expected_qualified_transport_manifest_sha256,
            transport_receipt_path=args.qualified_transport_receipt,
            transport_receipt_sha256=args.expected_qualified_transport_receipt_sha256,
        )
        raw_manifest_sha256 = hashlib.sha256(
            (args.output_dir / "manifest.json").read_bytes()
        ).hexdigest()
    except (G005ContractError, OSError) as exc:
        print(str(exc).split(":", 1)[0], file=sys.stderr)
        return 64

    print(canonical_json({
        "ok": False,
        "status": manifest["status"],
        "metric_effect": manifest["ceiling"]["metric_effect"],
        "snapshot_emitted": manifest["ceiling"]["snapshot_emitted"],
        "replay_executed": manifest["ceiling"]["replay_executed"],
        "golden_eligible": manifest["ceiling"]["golden_eligible"],
        "holdout_status": manifest["ceiling"]["holdout_status"],
        "gate0_result_effect": manifest["ceiling"]["gate0_result_effect"],
        "gate1_effect": manifest["ceiling"]["gate1_effect"],
        "manifest_hash": manifest["manifest_hash"],
        "manifest_sha256": raw_manifest_sha256,
    }))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
