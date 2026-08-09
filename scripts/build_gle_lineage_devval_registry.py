#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.canonical_evaluation_contracts import canonical_json
from app.growth.historical_lineage_candidates import (
    HistoricalLineageCandidateError,
    _canonical_json_document,
    _read_regular,
)
from app.growth.lineage_devval_registry import (
    LineageDevvalRegistryError,
    build_registry_request,
    evaluate_registry_response,
    write_registry_artifacts,
)


def _json_file(path: str | None) -> dict | None:
    if path is None:
        return None
    try:
        value = _canonical_json_document(
            _read_regular(Path(path)), "G102B2B_CLI_JSON_INVALID",
        )
    except HistoricalLineageCandidateError as exc:
        raise LineageDevvalRegistryError("G102B2B_CLI_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise LineageDevvalRegistryError("G102B2B_CLI_JSON_INVALID")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a source-bound signed DEV/VALIDATION lineage registry. "
            "HOLDOUT, Replay, Golden, and Gate effects remain disabled."
        )
    )
    parser.add_argument("--audit-dir", required=True)
    parser.add_argument("--expected-audit-manifest-sha256", required=True)
    parser.add_argument("--candidate-dir", required=True)
    parser.add_argument("--expected-candidate-manifest-sha256", required=True)
    parser.add_argument("--authority-dir", required=True)
    parser.add_argument("--expected-authority-manifest-sha256", required=True)
    parser.add_argument("--expected-authority-key-registry-hash")
    parser.add_argument("--registry-id", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--requested-at", required=True)
    parser.add_argument("--evaluated-at", required=True)
    parser.add_argument("--policy")
    parser.add_argument("--seed-selection-file")
    parser.add_argument("--expected-seed-selection-file-sha256")
    parser.add_argument("--prior-registry-dir")
    parser.add_argument("--expected-prior-manifest-sha256")
    parser.add_argument("--expected-prior-devval-key-registry-hash")
    parser.add_argument("--response")
    parser.add_argument("--trusted-key-registry")
    parser.add_argument("--expected-devval-key-registry-hash")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    try:
        paired = (
            (args.seed_selection_file, args.expected_seed_selection_file_sha256),
            (args.prior_registry_dir, args.expected_prior_manifest_sha256),
            (args.response, args.trusted_key_registry),
        )
        if any(bool(left) != bool(right) for left, right in paired):
            raise LineageDevvalRegistryError("G102B2B_CLI_INPUTS_INCOMPLETE")
        if bool(args.prior_registry_dir) != bool(args.expected_prior_devval_key_registry_hash):
            raise LineageDevvalRegistryError("G102B2B_CLI_PRIOR_TRUST_INPUTS_INCOMPLETE")
        if bool(args.response) != bool(args.expected_devval_key_registry_hash):
            raise LineageDevvalRegistryError("G102B2B_CLI_TRUST_INPUTS_INCOMPLETE")

        policy = _json_file(args.policy)
        response = _json_file(args.response)
        key_registry = _json_file(args.trusted_key_registry)
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
            "expected_prior_devval_key_registry_hash": (
                args.expected_prior_devval_key_registry_hash
            ),
        }
        request = build_registry_request(
            authority_dir=args.authority_dir,
            expected_authority_manifest_sha256=args.expected_authority_manifest_sha256,
            expected_authority_key_registry_hash=args.expected_authority_key_registry_hash,
            candidate_dir=args.candidate_dir,
            expected_candidate_manifest_sha256=args.expected_candidate_manifest_sha256,
            audit_dir=args.audit_dir,
            expected_audit_manifest_sha256=args.expected_audit_manifest_sha256,
            registry_id=args.registry_id,
            generation=args.generation,
            requested_at=args.requested_at,
            evaluated_at=args.evaluated_at,
            policy=policy,
            seed_selection_file=args.seed_selection_file,
            expected_seed_selection_file_sha256=args.expected_seed_selection_file_sha256,
            prior_registry_dir=args.prior_registry_dir,
            expected_prior_manifest_sha256=args.expected_prior_manifest_sha256,
            expected_prior_devval_key_registry_hash=(
                args.expected_prior_devval_key_registry_hash
            ),
        )
        registry = evaluate_registry_response(
            request,
            response,
            trusted_key_registry=key_registry,
            expected_devval_key_registry_hash=args.expected_devval_key_registry_hash,
            source_validation=source_validation,
        )
        manifest = write_registry_artifacts(
            request,
            response,
            key_registry,
            registry,
            args.output_dir,
            expected_devval_key_registry_hash=args.expected_devval_key_registry_hash,
            source_validation=source_validation,
        )
    except (LineageDevvalRegistryError, OSError) as exc:
        print(str(exc).split(":", 1)[0], file=sys.stderr)
        return 64

    print(canonical_json({
        "ok": registry["status"] == "SIGNED_DETERMINISTIC_PARTITION",
        "status": registry["status"],
        "assignment_count": len(registry["assignments"]),
        "split_effect": registry["split_effect"],
        "holdout_status": registry["holdout_status"],
        "manifest_hash": manifest["manifest_hash"],
    }))
    return 0 if registry["status"] == "SIGNED_DETERMINISTIC_PARTITION" else 2


if __name__ == "__main__":
    raise SystemExit(main())
