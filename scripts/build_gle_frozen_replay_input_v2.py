#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.canonical_evaluation_contracts_v2 import canonical_json
from app.growth.frozen_replay_input_v2 import (
    FrozenReplayInputV2Error,
    write_frozen_replay_input_v2_artifact,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise FrozenReplayInputV2Error("G104A2_CLI_ARGUMENT_INVALID")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(
        description=(
            "Build an externally anchorable canonical-v2 synthetic frozen Replay input. "
            "This validates an unverified authority-candidate shape only; it does not execute "
            "Replay or produce Snapshot, Golden, Holdout, or Gate evidence."
        )
    )
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--expected-bundle-sha256", required=True)
    parser.add_argument("--replay-input-id", required=True)
    parser.add_argument("--requested-split", required=True)
    parser.add_argument("--synthetic-clock-at", required=True)
    parser.add_argument("--output-dir", required=True)
    try:
        args = parser.parse_args(argv)
        manifest = write_frozen_replay_input_v2_artifact(
            args.output_dir,
            args.bundle,
            expected_bundle_sha256=args.expected_bundle_sha256,
            replay_input_id=args.replay_input_id,
            requested_split=args.requested_split,
            synthetic_clock_at=args.synthetic_clock_at,
        )
    except (FrozenReplayInputV2Error, OSError) as exc:
        print(str(exc).split(":", 1)[0], file=sys.stderr)
        return 64

    manifest_raw_sha256 = hashlib.sha256(
        (canonical_json(manifest) + "\n").encode("utf-8")
    ).hexdigest()
    ceiling = manifest["validation_ceiling"]
    print(canonical_json({
        "ok": False,
        "status": manifest["status"],
        "contract_effect": ceiling["contract_effect"],
        "input_effect": ceiling["input_effect"],
        "objective_approval_semantics": ceiling["objective_approval_semantics"],
        "spec_status_semantics": ceiling["spec_status_semantics"],
        "authority_reference_content_status": ceiling[
            "authority_reference_content_status"
        ],
        "metric_contract_content_status": ceiling["metric_contract_content_status"],
        "evaluator_implementation_content_status": ceiling[
            "evaluator_implementation_content_status"
        ],
        "policy_implementation_content_status": ceiling[
            "policy_implementation_content_status"
        ],
        "assignment_mechanism_content_status": ceiling[
            "assignment_mechanism_content_status"
        ],
        "capability_assessment_content_status": ceiling[
            "capability_assessment_content_status"
        ],
        "allocation_effect": ceiling["allocation_effect"],
        "requested_split": manifest["requested_split"],
        "requested_split_effect": manifest["requested_split_effect"],
        "holdout_status": ceiling["holdout_status"],
        "snapshot_emitted": ceiling["snapshot_emitted"],
        "replay_executed": ceiling["replay_executed"],
        "replay_eligible": ceiling["replay_eligible"],
        "golden_eligible": ceiling["golden_eligible"],
        "gate0_result_effect": ceiling["gate0_result_effect"],
        "gate1_effect": ceiling["gate1_effect"],
        "input_root": manifest["input_root"],
        "manifest_hash": manifest["manifest_hash"],
        "manifest_raw_sha256": manifest_raw_sha256,
    }))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
