#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.objective_contract_authority import (  # noqa: E402
    ObjectiveContractAuthorityError,
    write_objective_authority_artifact,
    write_objective_authority_request_artifact,
)


def _source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--expected-proposal-sha256", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--requested-at", required=True)
    parser.add_argument("--evaluated-at", required=True)
    parser.add_argument("--trusted-key-registry", required=True)
    parser.add_argument("--expected-key-registry-sha256", required=True)
    parser.add_argument("--expected-key-registry-hash", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or finalize a signed GLE ObjectiveContract attestation"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="freeze the request before any response is signed")
    _source_arguments(prepare)
    prepare.add_argument("--output-dir", required=True)

    finalize = commands.add_parser("finalize", help="validate a response against a frozen request")
    _source_arguments(finalize)
    finalize.add_argument("--request-dir", required=True)
    finalize.add_argument("--expected-request-manifest-sha256", required=True)
    finalize.add_argument("--response", required=True)
    finalize.add_argument("--expected-response-sha256", required=True)
    finalize.add_argument("--output-dir", required=True)
    return parser


def _source_args(args: argparse.Namespace) -> dict[str, str]:
    return {
        "proposal_file": args.proposal,
        "expected_proposal_sha256": args.expected_proposal_sha256,
        "request_id": args.request_id,
        "requested_at": args.requested_at,
        "evaluated_at": args.evaluated_at,
        "trusted_key_registry_file": args.trusted_key_registry,
        "expected_key_registry_sha256": args.expected_key_registry_sha256,
        "expected_key_registry_hash": args.expected_key_registry_hash,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            manifest = write_objective_authority_request_artifact(
                args.output_dir, **_source_args(args)
            )
        else:
            manifest = write_objective_authority_artifact(
                args.output_dir,
                request_dir=args.request_dir,
                expected_request_manifest_sha256=args.expected_request_manifest_sha256,
                response_file=args.response,
                expected_response_sha256=args.expected_response_sha256,
                **_source_args(args),
            )
    except (ObjectiveContractAuthorityError, OSError, ValueError) as exc:
        print(json.dumps({"status": "INVALID", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 64
    print(json.dumps({
        "status": manifest["status"],
        "authority_effect": manifest["authority_effect"],
        "attestation_effect": manifest.get("attestation_effect", "NONE"),
        "objective_effect": manifest.get("objective_effect", "NONE"),
        "objective_contract_hash": manifest.get("objective_contract_hash"),
        "manifest_hash": manifest["manifest_hash"],
        "snapshot_effect": manifest["snapshot_effect"],
        "replay_eligible": manifest["replay_eligible"],
        "golden_eligible": manifest["golden_eligible"],
        "gate1_effect": manifest["gate1_effect"],
    }, sort_keys=True))
    # Neither stage has governance authority effect. Exit 2 is the stable
    # diagnostic-success code until an independently rooted consumer promotes it.
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
