#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.canonical_evaluation_contracts import canonical_json
from app.growth.immutable_lineage_authority import (
    ImmutableLineageAuthorityError,
    build_authority_request,
    evaluate_authority_response,
    write_authority_artifacts,
)
from app.growth.historical_lineage_candidates import (
    HistoricalLineageCandidateError,
    _canonical_json_document,
    _read_regular,
)


def _json_file(path: str) -> dict:
    try:
        value = _canonical_json_document(
            _read_regular(Path(path)), "G102B2_CLI_JSON_INVALID",
        )
    except HistoricalLineageCandidateError as exc:
        raise ImmutableLineageAuthorityError("G102B2_CLI_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise ImmutableLineageAuthorityError("G102B2_CLI_JSON_INVALID")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build and validate a GLE immutable lineage authority fragment. "
            "This command never assigns DEV, VALIDATION, or HOLDOUT."
        )
    )
    parser.add_argument("--audit-dir", required=True)
    parser.add_argument("--expected-audit-manifest-sha256", required=True)
    parser.add_argument("--candidate-dir", required=True)
    parser.add_argument("--expected-candidate-manifest-sha256", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--requested-at", required=True)
    parser.add_argument("--evaluated-at", required=True)
    parser.add_argument("--response")
    parser.add_argument("--trusted-key-registry")
    parser.add_argument("--expected-key-registry-hash")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if bool(args.response) != bool(args.trusted_key_registry and args.expected_key_registry_hash):
            raise ImmutableLineageAuthorityError("G102B2_CLI_TRUST_INPUTS_INCOMPLETE")
        request = build_authority_request(
            candidate_dir=args.candidate_dir,
            expected_candidate_manifest_sha256=args.expected_candidate_manifest_sha256,
            audit_dir=args.audit_dir,
            expected_audit_manifest_sha256=args.expected_audit_manifest_sha256,
            request_id=args.request_id,
            requested_at=args.requested_at,
            evaluated_at=args.evaluated_at,
        )
        response = _json_file(args.response) if args.response else None
        registry = _json_file(args.trusted_key_registry) if args.trusted_key_registry else None
        fragment = evaluate_authority_response(
            request,
            response,
            trusted_key_registry=registry,
            expected_key_registry_hash=args.expected_key_registry_hash,
            candidate_dir=args.candidate_dir,
            expected_candidate_manifest_sha256=args.expected_candidate_manifest_sha256,
            audit_dir=args.audit_dir,
            expected_audit_manifest_sha256=args.expected_audit_manifest_sha256,
        )
        manifest = write_authority_artifacts(
            request,
            fragment,
            args.output_dir,
            response=response,
            trusted_key_registry=registry,
            expected_key_registry_hash=args.expected_key_registry_hash,
            candidate_dir=args.candidate_dir,
            expected_candidate_manifest_sha256=args.expected_candidate_manifest_sha256,
            audit_dir=args.audit_dir,
            expected_audit_manifest_sha256=args.expected_audit_manifest_sha256,
        )
    except (ImmutableLineageAuthorityError, OSError) as exc:
        print(str(exc).split(":", 1)[0], file=sys.stderr)
        return 64
    print(canonical_json({
        "ok": fragment["status"] == "VERIFIED",
        "status": fragment["status"],
        "authority_effect": fragment["authority_effect"],
        "split_effect": fragment["split_effect"],
        "holdout_status": fragment["holdout_status"],
        "manifest_hash": manifest["manifest_hash"],
    }))
    if fragment["status"] == "VERIFIED":
        return 0
    if fragment["status"] in {"MISSING", "CONFLICT"}:
        return 2
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
