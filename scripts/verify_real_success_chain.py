#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.success_chain_verifier import build_success_chain_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify one MCN submission's real success chain")
    parser.add_argument("--db-path", default="data/automation.db")
    parser.add_argument("--lead-id")
    parser.add_argument("--mobile")
    parser.add_argument("--account-id")
    parser.add_argument("--invite-code")
    parser.add_argument("--registration-group")
    parser.add_argument("--runtime-health-url", default="http://127.0.0.1:8011/api/ops/runtime-health")
    args = parser.parse_args()

    report = build_success_chain_report(
        db_path=args.db_path,
        lead_id=args.lead_id,
        mobile=args.mobile,
        account_id=args.account_id,
        invite_code=args.invite_code,
        registration_group=args.registration_group,
        runtime_health_url=args.runtime_health_url,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("final_success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
