#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.canonical_evaluation_contracts import canonical_json
from app.growth.golden_label_adjudication import (
    GoldenLabelAdjudicationError,
    build_label_assignment_request,
    evaluate_label_round,
    write_label_round_artifact_domains,
)
from app.growth.historical_lineage_candidates import (
    HistoricalLineageCandidateError,
    _canonical_json_document,
    _read_regular,
)


def _json_file(path: str | None, *, expect: type) -> object | None:
    if path is None:
        return None
    try:
        value = _canonical_json_document(
            _read_regular(Path(path)), "G103A_CLI_JSON_INVALID",
        )
    except HistoricalLineageCandidateError as exc:
        raise GoldenLabelAdjudicationError("G103A_CLI_JSON_INVALID") from exc
    if not isinstance(value, expect):
        raise GoldenLabelAdjudicationError("G103A_CLI_JSON_INVALID")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build source-bound DEV/VALIDATION blind-review tasks and signed label "
            "candidate fragments. This never creates Golden, Replay, Holdout, or Gate evidence."
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
    parser.add_argument("--review-round-id", required=True)
    parser.add_argument("--requested-at", required=True)
    parser.add_argument("--evaluated-at", required=True)
    parser.add_argument("--label-version", required=True)
    parser.add_argument("--reviewer-key-registry")
    parser.add_argument("--expected-reviewer-key-registry-hash")
    parser.add_argument("--expected-reviewer-key-registry-sha256")
    parser.add_argument("--blinding-map")
    parser.add_argument("--expected-blinding-map-sha256")
    parser.add_argument("--review-responses")
    parser.add_argument("--adjudications")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--reviewer-output-dir")
    args = parser.parse_args(argv)

    try:
        pairs = (
            (args.seed_selection_file, args.expected_seed_selection_file_sha256),
            (args.prior_registry_dir, args.expected_prior_manifest_sha256),
        )
        if any(bool(left) != bool(right) for left, right in pairs):
            raise GoldenLabelAdjudicationError("G103A_CLI_INPUTS_INCOMPLETE")
        reviewer_inputs = (
            args.reviewer_key_registry,
            args.expected_reviewer_key_registry_hash,
            args.expected_reviewer_key_registry_sha256,
        )
        if any(reviewer_inputs) and not all(reviewer_inputs):
            raise GoldenLabelAdjudicationError("G103A_CLI_INPUTS_INCOMPLETE")
        if bool(args.blinding_map) != bool(args.expected_blinding_map_sha256):
            raise GoldenLabelAdjudicationError("G103A_CLI_INPUTS_INCOMPLETE")
        if bool(args.prior_registry_dir) != bool(args.expected_prior_devval_key_registry_hash):
            raise GoldenLabelAdjudicationError("G103A_CLI_PRIOR_TRUST_INPUTS_INCOMPLETE")
        if bool(args.review_responses) != bool(args.reviewer_key_registry):
            if args.review_responses is not None:
                raise GoldenLabelAdjudicationError("G103A_CLI_REVIEW_TRUST_INPUTS_INCOMPLETE")
        if args.adjudications is not None and args.review_responses is None:
            raise GoldenLabelAdjudicationError("G103A_CLI_ADJUDICATION_INPUTS_INCOMPLETE")

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
        reviewer_registry = _json_file(args.reviewer_key_registry, expect=dict)
        blinding_map = _json_file(args.blinding_map, expect=dict)
        source_context = {
            "registry_dir": args.registry_dir,
            "expected_registry_manifest_sha256": args.expected_registry_manifest_sha256,
            "expected_devval_key_registry_hash": args.expected_devval_key_registry_hash,
            "source_validation": source_validation,
            "review_round_id": args.review_round_id,
            "requested_at": args.requested_at,
            "evaluated_at": args.evaluated_at,
            "label_version": args.label_version,
            "reviewer_key_registry": reviewer_registry,
            "expected_reviewer_key_registry_hash": args.expected_reviewer_key_registry_hash,
            "expected_reviewer_key_registry_sha256": (
                args.expected_reviewer_key_registry_sha256
            ),
            "blinding_map": blinding_map,
            "expected_blinding_map_sha256": args.expected_blinding_map_sha256,
        }
        review_responses = _json_file(args.review_responses, expect=list) or []
        adjudications = _json_file(args.adjudications, expect=list) or []
        request, tasks = build_label_assignment_request(**source_context)
        if bool(tasks) != bool(args.reviewer_output_dir):
            raise GoldenLabelAdjudicationError("G103A_CLI_REVIEW_PACKET_OUTPUT_INVALID")
        ledger, round_summary = evaluate_label_round(
            request,
            tasks,
            review_responses,
            adjudications,
            reviewer_key_registry=reviewer_registry,
            expected_reviewer_key_registry_hash=args.expected_reviewer_key_registry_hash,
            source_context=source_context,
        )
        manifest, reviewer_manifest = write_label_round_artifact_domains(
            request,
            tasks,
            review_responses,
            adjudications,
            reviewer_registry,
            ledger,
            round_summary,
            args.output_dir,
            args.reviewer_output_dir,
            expected_reviewer_key_registry_hash=args.expected_reviewer_key_registry_hash,
            source_context=source_context,
        )
    except (GoldenLabelAdjudicationError, OSError) as exc:
        print(str(exc).split(":", 1)[0], file=sys.stderr)
        return 64

    print(canonical_json({
        "ok": round_summary["status"] == "ASSIGNED_EVALUATION_SUBSET_LABELS_RESOLVED",
        "status": round_summary["status"],
        "task_count": request["task_count"],
        "fragment_count": round_summary["fragment_count"],
        "label_effect": round_summary["label_effect"],
        "not_golden_case": round_summary["not_golden_case"],
        "holdout_status": round_summary["holdout_status"],
        "manifest_hash": manifest["manifest_hash"],
        "reviewer_packet_manifest_hash": (
            reviewer_manifest["manifest_hash"] if reviewer_manifest is not None else None
        ),
    }))
    if round_summary["status"] == "ASSIGNED_EVALUATION_SUBSET_LABELS_RESOLVED":
        return 0
    if round_summary["status"] == "ADJUDICATION_PENDING":
        return 3
    if round_summary["status"] == "BLOCKED_SOURCE_PARTITION":
        return 4
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
