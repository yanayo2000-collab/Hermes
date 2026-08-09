#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.canonical_evaluation_contracts import canonical_json
from app.growth.frozen_replay_input import (
    FrozenReplayInputError,
    read_canonical_input_file,
    write_frozen_replay_input_artifact,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build an externally anchorable synthetic-only frozen Replay input. "
            "This does not execute Replay or produce Golden, Holdout, or Gate evidence."
        )
    )
    parser.add_argument("--objective-contract", required=True)
    parser.add_argument("--invariant-projection", required=True)
    parser.add_argument("--experiment-spec", required=True)
    parser.add_argument("--input-snapshot", required=True)
    parser.add_argument("--replay-input-id", required=True)
    parser.add_argument("--requested-split", required=True)
    parser.add_argument("--synthetic-clock-at", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    try:
        objective = read_canonical_input_file(args.objective_contract)
        invariant = read_canonical_input_file(args.invariant_projection)
        spec = read_canonical_input_file(args.experiment_spec)
        snapshot = read_canonical_input_file(args.input_snapshot)
        manifest = write_frozen_replay_input_artifact(
            args.output_dir,
            objective,
            invariant,
            spec,
            snapshot,
            replay_input_id=args.replay_input_id,
            requested_split=args.requested_split,
            synthetic_clock_at=args.synthetic_clock_at,
        )
    except (FrozenReplayInputError, OSError) as exc:
        print(str(exc).split(":", 1)[0], file=sys.stderr)
        return 64

    print(canonical_json({
        "ok": False,
        "status": manifest["status"],
        "input_effect": manifest["input_effect"],
        "requested_split": manifest["requested_split"],
        "holdout_status": manifest["holdout_status"],
        "replay_executed": manifest["replay_executed"],
        "replay_eligible": manifest["replay_eligible"],
        "golden_eligible": manifest["golden_eligible"],
        "gate1_effect": manifest["gate1_effect"],
        "input_root": manifest["input_root"],
        "manifest_hash": manifest["manifest_hash"],
    }))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
