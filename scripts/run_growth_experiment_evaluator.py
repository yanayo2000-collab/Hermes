#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3

from app.growth.ad_experiment_evaluator import AdExperimentEvaluator
from app.growth.ad_experiment_cycle_service import AdExperimentCycleService
from app.growth.audience_experiment_evaluator import AudienceExperimentEvaluator
from app.growth.autonomy_service import GrowthAutonomyService
from app.growth.creative_group_evaluator import CreativeGroupEvaluator
from app.growth.new_account_launch_retention import purge_due_archived_launches
from app.growth.schema import ensure_growth_schema
from app.main_shared import DEFAULT_DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate due Growth ad experiments at D1/D3/D7")
    parser.add_argument("--database-path", default=os.getenv("DB_PATH") or DEFAULT_DB_PATH)
    parser.add_argument("--as-of-date", default="")
    parser.add_argument("--launch-retention-days", type=int, default=7)
    parser.add_argument("--retention-dry-run", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(args.database_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_growth_schema(conn)
    try:
        cycles = AdExperimentCycleService(conn).reconcile_pending(
            actor="growth-experiment-evaluator",
        )
        result = AdExperimentEvaluator(conn).evaluate_due(as_of_date=args.as_of_date)
        creative_result = CreativeGroupEvaluator(conn).evaluate_due(as_of_date=args.as_of_date)
        audience_result = AudienceExperimentEvaluator(conn).evaluate_due(as_of_date=args.as_of_date)
        next_actions = GrowthAutonomyService(conn).sync_evaluations()
        retention = purge_due_archived_launches(
            conn,
            retention_days=max(1, args.launch_retention_days),
            dry_run=args.retention_dry_run,
        )
        print(json.dumps(
            {"ok": True, **result, "cycles": cycles,
             "creative_experiments": creative_result,
             "audience_experiments": audience_result,
             "next_actions": next_actions,
             "new_account_launch_retention": retention},
            ensure_ascii=False,
            sort_keys=True,
        ))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
