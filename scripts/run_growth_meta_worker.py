#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, FrozenSet

import requests

from app.growth.execution_service import ExecutionTaskService
from app.growth.errors import GrowthValidationError
from app.growth.meta_execution_worker import MetaExecutionWorker
from app.growth.meta_graph_adapter import MetaGraphExecutionAdapter, MetaGraphWritePolicy
from app.growth.new_account_autopilot import NewAccountLaunchAutopilot
from app.growth.schema import ensure_growth_schema
from app.main_shared import DEFAULT_DB_PATH
from app.meta_api_budget import (
    BudgetedMetaSession,
    MetaApiBudgetManager,
    default_meta_rate_limit_db_path,
)


class DryRunMetaAdapter:
    def execute_step(
        self, step: str, payload: Dict[str, Any], object_ids: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "status": "SUCCESS",
            "meta_object_ids": {f"dry_{step.lower()}_id": f"dry-{step.lower()}"},
        }

    def verify_step(
        self, step: str, payload: Dict[str, Any], object_ids: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {"status": "SUCCESS", "meta_object_ids": object_ids}


def _enabled(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _account_ids(value: str) -> FrozenSet[str]:
    return frozenset(
        item.strip().removeprefix("act_")
        for item in str(value or "").split(",")
        if item.strip()
    )


def _action_types(value: str) -> FrozenSet[str]:
    return frozenset(
        item.strip().upper() for item in str(value or "").split(",") if item.strip()
    )


def _live_adapter(args: argparse.Namespace, database_path: str) -> MetaGraphExecutionAdapter:
    policy = MetaGraphWritePolicy(
        enabled=_enabled(os.getenv("GROWTH_META_WRITES_ENABLED", "")),
        allowed_account_ids=_account_ids(os.getenv("GROWTH_META_ALLOWED_ACCOUNT_IDS", "")),
        allowed_action_types=_action_types(os.getenv("GROWTH_META_ALLOWED_ACTION_TYPES", "")),
        max_budget_change_percent=float(os.getenv("GROWTH_META_MAX_BUDGET_CHANGE_PERCENT", "20")),
        image_root=str(os.getenv("GROWTH_META_IMAGE_ROOT") or "").strip(),
        regional_identity_account_id=str(os.getenv("GROWTH_META_REGIONAL_IDENTITY_ACCOUNT_ID") or "").strip(),
        regional_beneficiary_id=str(os.getenv("GROWTH_META_REGIONAL_BENEFICIARY_ID") or "").strip(),
        regional_payer_id=str(os.getenv("GROWTH_META_REGIONAL_PAYER_ID") or "").strip(),
    )
    rate_limit_manager = MetaApiBudgetManager(
        str(
            os.getenv("META_RATE_LIMIT_DB_PATH")
            or default_meta_rate_limit_db_path(database_path)
        ),
        hard_limit_percent=float(os.getenv("META_RATE_LIMIT_HARD_PERCENT", "85")),
    )
    try:
        adapter = MetaGraphExecutionAdapter(
            session=BudgetedMetaSession(requests, rate_limit_manager),
            access_token=str(os.getenv("GROWTH_META_ACCESS_TOKEN") or "").strip(),
            policy=policy,
            api_version=str(os.getenv("META_ADS_API_VERSION") or "v25.0"),
            base_url=str(os.getenv("META_ADS_BASE_URL") or "https://graph.facebook.com"),
            timeout_seconds=args.network_timeout_seconds,
        )
        adapter.validate_runtime_configuration()
    except GrowthValidationError as exc:
        raise SystemExit(f"live mode configuration invalid:{exc}") from exc
    if not adapter.live_writes_enabled:
        raise SystemExit(
            "live mode is closed: require GROWTH_META_WRITES_ENABLED, a dedicated token, and account allowlist"
        )
    return adapter


def run(args: argparse.Namespace) -> int:
    database_path = str(args.database_path or DEFAULT_DB_PATH)
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_growth_schema(conn)
    tasks = ExecutionTaskService(conn)
    adapter = _live_adapter(args, database_path) if args.mode == "live" else DryRunMetaAdapter()
    worker = MetaExecutionWorker(
        tasks, adapter, worker_id=args.worker_id, execution_mode=args.mode,
        heartbeat_interval_seconds=30.0,
    )
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            try:
                autopilot = NewAccountLaunchAutopilot(
                    conn, meta_adapter=adapter if args.mode == "live" else None,
                )
                autopilot.reconcile_meta_reviews(limit=10)
                autopilot.advance_ready_launches(
                    limit=20, allow_live=args.mode == "live",
                )
                tasks.move_expired_to_reconciliation(stale_after_seconds=args.stale_after_seconds)
                tasks.recover_rate_limited_activation_tasks(actor=args.worker_id)
                reconciled = worker.reconcile_once()
                result = worker.run_once()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                # No Meta write has started while claiming or reconciling work.
                # Release the local transaction and wait for the active writer
                # instead of crashing systemd into an unattributed restart.
                conn.rollback()
                if args.once:
                    return 75
                time.sleep(min(5.0, max(0.1, float(args.poll_seconds))))
                continue
            if args.once:
                return 0
            if reconciled.get("status") == "IDLE" and result.get("status") == "IDLE":
                time.sleep(args.poll_seconds)
    finally:
        conn.close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Growth v2 Meta execution worker")
    parser.add_argument("--database-path", default=os.getenv("DB_PATH") or DEFAULT_DB_PATH)
    parser.add_argument("--mode", choices=("dry_run", "live"), default="dry_run")
    parser.add_argument("--worker-id", default=f"growth-meta-{os.getpid()}")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--stale-after-seconds", type=int, default=90)
    parser.add_argument("--network-timeout-seconds", type=float, default=25.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
