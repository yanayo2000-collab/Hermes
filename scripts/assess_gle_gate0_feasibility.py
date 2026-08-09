#!/usr/bin/env python3
"""Build an unsigned GLE Gate 0 candidate from immutable local evidence."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.common import canonical_json
from app.growth.gate0_feasibility_assessment import (
    G005ContractError,
    INPUT_VERSION,
    assess_gate0,
    exit_code_for_candidate,
    hash_json,
)
from app.growth.evaluation_cell_metric_evidence import (
    collect_gate0_observations as _collect_observations,
    read_json_artifact as _read_json,
    validate_transport_release as _validate_transport_release,
)


RUN_REQUEST_VERSION = "gle-g0-05-run-request-v1"


def _date(value: Any) -> str:
    text = str(value or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise G005ContractError("G005_DATE_INVALID") from exc


def _publish_new(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Build unsigned GLE Gate 0 feasibility candidate")
    result.add_argument("--request", type=Path, required=True)
    result.add_argument("--governance-config", type=Path, required=True)
    result.add_argument("--g004-manifest", type=Path, required=True)
    result.add_argument("--g004-receipt", type=Path, required=True)
    result.add_argument("--g004-evidence", type=Path, required=True)
    result.add_argument("--g004a-manifest", type=Path, required=True)
    result.add_argument("--g004a-receipt", type=Path, required=True)
    result.add_argument("--g004a-evidence", type=Path, required=True)
    result.add_argument("--g001-input", type=Path, required=True)
    result.add_argument("--g001-report", type=Path, required=True)
    result.add_argument("--database", type=Path, required=True)
    result.add_argument("--database-sha256", required=True)
    result.add_argument("--qualified-transport-manifest", type=Path, required=True)
    result.add_argument("--qualified-transport-receipt", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--manifest-output", type=Path, required=True)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    if (
        args.output.exists()
        or args.manifest_output.exists()
        or args.output.resolve() == args.manifest_output.resolve()
        or args.output.resolve().parent != args.manifest_output.resolve().parent
    ):
        print("G005_OUTPUT_NOT_IMMUTABLE", file=sys.stderr)
        return 64
    try:
        request = _read_json(args.request)
        if request.get("schema_version") != RUN_REQUEST_VERSION or set(request) != {
            "schema_version", "assessment_id", "requested_at", "data_cutoff_at", "subject",
            "policy", "windows", "qualified_transport_evidence",
        }:
            raise G005ContractError("G005_RUN_REQUEST_INVALID")
        natural_start = _date(dict(request["qualified_transport_evidence"])["natural_evidence_not_before_date"])
        if _date(dict(request["windows"])["allocation_start"]) < natural_start:
            raise G005ContractError("G005_NATURAL_EVIDENCE_WINDOW_INVALID")
        source_hash = str(args.database_sha256 or "").lower()
        _validate_transport_release(
            args.qualified_transport_manifest, args.qualified_transport_receipt,
            dict(request["qualified_transport_evidence"]),
        )
        allocation, qualified, baseline, experiment_binding = _collect_observations(
            args.database, request, source_hash,
        )
        raw = {
            "schema_version": INPUT_VERSION,
            "assessment_id": request["assessment_id"], "requested_at": request["requested_at"],
            "data_cutoff_at": request["data_cutoff_at"], "subject": request["subject"],
            "qualified_transport_evidence": request["qualified_transport_evidence"],
            "policy": request["policy"], "source_snapshot_sha256": source_hash,
            "capability_manifest": _read_json(args.g004_manifest),
            "capability_receipt": _read_json(args.g004_receipt),
            "capability_evidence": _read_json(args.g004_evidence),
            "audience_manifest": _read_json(args.g004a_manifest),
            "audience_receipt": _read_json(args.g004a_receipt),
            "audience_evidence": _read_json(args.g004a_evidence),
            "attribution_input_contract": _read_json(args.g001_input),
            "attribution_report": _read_json(args.g001_report),
            "allocation_observation": allocation, "qualified_join_observation": qualified,
            "experiment_binding_observation": experiment_binding,
            "baseline_observation": baseline,
            "governance_contract": _read_json(args.governance_config),
        }
        candidate = assess_gate0(raw)
        serialized = canonical_json(candidate) + "\n"
        _publish_new(args.output, serialized)
        manifest = {
            "schema_version": "gle-g0-05-candidate-manifest-v1",
            "candidate_file": args.output.name,
            "candidate_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "candidate_body_hash": candidate["candidate_body_hash"],
            "source_snapshot_sha256": source_hash,
            "committed": True,
        }
        _publish_new(args.manifest_output, canonical_json(manifest) + "\n")
        sys.stdout.write(serialized)
        return exit_code_for_candidate(candidate)
    except G005ContractError as exc:
        print(str(exc), file=sys.stderr)
        return 64
    except Exception:
        print("G005_UNEXPECTED_FAILURE", file=sys.stderr)
        return 66


if __name__ == "__main__":
    raise SystemExit(main())
