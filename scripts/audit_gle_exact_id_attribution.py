#!/usr/bin/env python3
"""CLI for the bounded GLE Gate-0 exact-ID attribution audit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.growth.exact_id_attribution_audit import (  # noqa: E402
    AuditContractError,
    AuditInput,
    SourceAuditError,
    audit_snapshot,
    canonical_json,
    exit_code_for_report,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AuditContractError("CLI_ARGUMENT_INVALID")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Read-only GLE G0-01 exact-ID attribution audit")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--expected-db-sha256", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--market", required=True)
    parser.add_argument("--experiment-id", action="append", required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--max-events", type=int, default=10000)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        report = audit_snapshot(
            AuditInput(
                db_path=Path(args.db_path),
                expected_db_sha256=args.expected_db_sha256,
                account_id=args.account_id,
                market=args.market,
                experiment_ids=tuple(args.experiment_id),
                window_start=args.window_start,
                window_end=args.window_end,
                project=args.project,
                max_events=args.max_events,
            )
        )
    except AuditContractError as exc:
        print(str(exc), file=sys.stderr)
        return 64
    except SourceAuditError as exc:
        print(str(exc), file=sys.stderr)
        return 66
    print(canonical_json(report))
    return exit_code_for_report(report)


if __name__ == "__main__":
    raise SystemExit(main())
