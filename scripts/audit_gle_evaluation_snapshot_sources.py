#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.canonical_evaluation_contracts import canonical_json
from app.growth.evaluation_snapshot_source_readiness import (
    EvaluationSnapshotSourceReadinessError,
    write_snapshot_source_readiness_artifact,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit externally anchored sources required for a real canonical "
            "EvaluationInputSnapshot. This command never emits a Snapshot or runs Replay."
        )
    )
    parser.add_argument("--audit-dir", required=True)
    parser.add_argument("--expected-audit-manifest-sha256", required=True)
    parser.add_argument("--candidate-dir", required=True)
    parser.add_argument("--expected-candidate-manifest-sha256", required=True)
    parser.add_argument("--authority-dir", required=True)
    parser.add_argument("--expected-authority-manifest-sha256", required=True)
    parser.add_argument("--expected-authority-key-registry-hash")
    parser.add_argument("--registry-dir", required=True)
    parser.add_argument("--expected-registry-manifest-sha256", required=True)
    parser.add_argument("--expected-devval-key-registry-hash")
    parser.add_argument("--seed-selection-file")
    parser.add_argument("--expected-seed-selection-file-sha256")
    parser.add_argument("--prior-registry-dir")
    parser.add_argument("--expected-prior-manifest-sha256")
    parser.add_argument("--expected-prior-devval-key-registry-hash")
    parser.add_argument("--source-observations", required=True)
    parser.add_argument("--expected-source-observations-sha256", required=True)
    parser.add_argument("--readiness-id", required=True)
    parser.add_argument("--requested-at", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    source_validation = {
        "authority_dir": args.authority_dir,
        "expected_authority_manifest_sha256": args.expected_authority_manifest_sha256,
        "expected_authority_key_registry_hash": args.expected_authority_key_registry_hash,
        "candidate_dir": args.candidate_dir,
        "expected_candidate_manifest_sha256": args.expected_candidate_manifest_sha256,
        "audit_dir": args.audit_dir,
        "expected_audit_manifest_sha256": args.expected_audit_manifest_sha256,
        "seed_selection_file": args.seed_selection_file,
        "expected_seed_selection_file_sha256": args.expected_seed_selection_file_sha256,
        "prior_registry_dir": args.prior_registry_dir,
        "expected_prior_manifest_sha256": args.expected_prior_manifest_sha256,
        "expected_prior_devval_key_registry_hash": args.expected_prior_devval_key_registry_hash,
    }
    try:
        manifest = write_snapshot_source_readiness_artifact(
            args.output_dir,
            registry_dir=args.registry_dir,
            expected_registry_manifest_sha256=args.expected_registry_manifest_sha256,
            expected_devval_key_registry_hash=args.expected_devval_key_registry_hash,
            source_validation=source_validation,
            source_observation_file=args.source_observations,
            expected_source_observation_sha256=args.expected_source_observations_sha256,
            readiness_id=args.readiness_id,
            requested_at=args.requested_at,
            checkpoint=args.checkpoint,
        )
    except (EvaluationSnapshotSourceReadinessError, OSError) as exc:
        print(str(exc).split(":", 1)[0], file=sys.stderr)
        return 64

    print(canonical_json({
        "ok": False,
        "status": manifest["status"],
        "subject_count": manifest["subject_count"],
        "gap_count": manifest["gap_count"],
        "snapshot_emitted": manifest["snapshot_emitted"],
        "replay_eligible": manifest["replay_eligible"],
        "golden_eligible": manifest["golden_eligible"],
        "holdout_status": manifest["holdout_status"],
        "gate1_effect": manifest["gate1_effect"],
        "manifest_hash": manifest["manifest_hash"],
    }))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
