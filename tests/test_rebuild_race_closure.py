from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from app.growth.common import canonical_json, payload_hash
from app.growth.errors import GrowthValidationError


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "candidate_rebuild_cleanup", ROOT / "app/growth/rebuild_source_ad_cleanup.py"
)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
RebuildSourceAdCleanupService = module.RebuildSourceAdCleanupService

group_spec = importlib.util.spec_from_file_location(
    "candidate_creative_group_evaluator", ROOT / "app/growth/creative_group_evaluator.py"
)
group_module = importlib.util.module_from_spec(group_spec)
assert group_spec and group_spec.loader
group_spec.loader.exec_module(group_module)
CreativeGroupEvaluator = group_module.CreativeGroupEvaluator


EXPERIMENT_ID = "adexp_race"
SOURCE_AD_ID = "120000000000001"
ROOT_PLAN_ID = "operation_source"
CANONICAL_PLAN_ID = "operation_repair_worker"
LATE_PLAN_ID = "operation_repair_browser"


class NoNetworkSession:
    def get(self, *args, **kwargs):  # pragma: no cover - a call fails the test
        raise AssertionError("completed delete reconciliation must not call Meta")

    def delete(self, *args, **kwargs):  # pragma: no cover - a call fails the test
        raise AssertionError("completed delete reconciliation must not call Meta")


def database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ad_experiment (experiment_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL DEFAULT '');
        CREATE TABLE growth_operation_action (
          operation_action_id TEXT PRIMARY KEY, action_type TEXT NOT NULL,
          status TEXT NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE TABLE meta_execution_task (
          execution_task_id TEXT PRIMARY KEY, operation_action_id TEXT NOT NULL,
          status TEXT NOT NULL, meta_object_ids_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE growth_idempotency_record (
          route_key TEXT NOT NULL, idempotency_key TEXT NOT NULL,
          request_hash TEXT NOT NULL, response_status INTEGER NOT NULL,
          response_json TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(route_key,idempotency_key)
        );
        """
    )
    conn.execute("INSERT INTO ad_experiment(experiment_id) VALUES (?)", (EXPERIMENT_ID,))
    return conn


def add_verified_plan(
    conn: sqlite3.Connection, plan_id: str, new_ad_id: str, *, root_plan_id: str = ROOT_PLAN_ID
) -> None:
    payload = {
        "experiment_id": EXPERIMENT_ID,
        "repair_of_operation_action_id": root_plan_id,
        "plan": {
            "experiment_id": EXPERIMENT_ID,
            "before_json": {
                "source_ids": {
                    "ad_id": SOURCE_AD_ID,
                    "campaign_id": "120000000000010",
                    "adset_id": "120000000000011",
                }
            },
            "after_json": {"source_ad_id_to_delete": SOURCE_AD_ID},
        },
    }
    conn.execute(
        "INSERT INTO growth_operation_action VALUES (?,?,?,?)",
        (plan_id, "CREATE_PAUSED_AD", "VERIFIED", canonical_json(payload)),
    )
    conn.execute(
        "INSERT INTO meta_execution_task VALUES (?,?,?,?,?)",
        (
            f"task_{plan_id}", plan_id, "SUCCESS",
            canonical_json({"ad_id": new_ad_id}), "2026-08-20T03:03:00+00:00",
        ),
    )


def add_completed_delete(conn: sqlite3.Connection, *, key: str = "batch-delete") -> dict:
    result = {
        "experiment_id": EXPERIMENT_ID,
        "plan_id": CANONICAL_PLAN_ID,
        "execution_task_id": f"task_{CANONICAL_PLAN_ID}",
        "new_ad_id": "120000000000101",
        "source_ad_id": SOURCE_AD_ID,
        "source_ad_deleted": True,
        "status": "SUCCESS",
        "automatic_retry": False,
    }
    conn.execute(
        "INSERT INTO growth_idempotency_record VALUES (?,?,?,?,?,?)",
        (
            "ad_experiment.rebuild_source_ad_delete", key,
            payload_hash({"experiment_id": EXPERIMENT_ID, "plan_id": CANONICAL_PLAN_ID}),
            200, canonical_json(result), "2026-08-20T03:03:03+00:00",
        ),
    )
    return result


def service(conn: sqlite3.Connection) -> RebuildSourceAdCleanupService:
    return RebuildSourceAdCleanupService(
        conn, session=NoNetworkSession(), access_token="not-used", graph_root="https://graph.invalid"
    )


def test_same_browser_key_reuses_completed_delete_across_equivalent_repair_plan() -> None:
    conn = database()
    add_verified_plan(conn, CANONICAL_PLAN_ID, "120000000000101")
    add_verified_plan(conn, LATE_PLAN_ID, "120000000000102")
    add_completed_delete(conn)

    result = service(conn).execute(
        EXPERIMENT_ID, plan_id=LATE_PLAN_ID,
        idempotency_key="batch-delete", request_id="recheck",
    )

    assert result["status"] == "SUCCESS"
    assert result["source_ad_deleted"] is True
    assert result["new_ad_id"] == "120000000000101"
    assert result["requested_plan_id"] == LATE_PLAN_ID
    assert result["reconciled_delete_fact"] is True
    assert result["duplicate_replacement_ad_id"] == "120000000000102"


def test_new_caller_key_persists_reconciled_delete_without_meta_call() -> None:
    conn = database()
    add_verified_plan(conn, CANONICAL_PLAN_ID, "120000000000101")
    add_verified_plan(conn, LATE_PLAN_ID, "120000000000102")
    add_completed_delete(conn, key="worker-delete")

    result = service(conn).execute(
        EXPERIMENT_ID, plan_id=LATE_PLAN_ID,
        idempotency_key="browser-delete", request_id="recheck-2",
    )

    assert result["reconciled_delete_fact"] is True
    stored = conn.execute(
        "SELECT response_json FROM growth_idempotency_record WHERE idempotency_key='browser-delete'"
    ).fetchone()
    assert json.loads(stored["response_json"])["status"] == "SUCCESS"


def test_completed_delete_is_not_reused_across_unrelated_repair_root() -> None:
    conn = database()
    add_verified_plan(conn, CANONICAL_PLAN_ID, "120000000000101")
    add_verified_plan(conn, LATE_PLAN_ID, "120000000000102", root_plan_id="different_source_plan")
    add_completed_delete(conn)

    with pytest.raises(GrowthValidationError, match="idempotency_key_payload_conflict"):
        service(conn).execute(
            EXPERIMENT_ID, plan_id=LATE_PLAN_ID,
            idempotency_key="batch-delete", request_id="recheck",
        )


def test_page_repair_persistence_identity_is_source_plan_and_page_based() -> None:
    source = (ROOT / "app/growth/api.py").read_text(encoding="utf-8")
    assert 'repair_identity = f"{source_plan_id}:{normalized_page_id}"' in source
    assert 'idempotency_key=f"page-repair-action:{repair_identity}"' in source
    assert 'idempotency_key=f"page-repair-approval:{repair_identity}"' in source
    assert 'f"page-repair-dry:{repair_identity}"' in source
    assert 'idempotency_key=f"page-repair-live:{repair_identity}"' in source
    assert 'repair_anchor_raw = str(source_task.get("finished_at")' in source
    assert 'idempotency_key=f"page-repair-action:{repair_key}"' not in source


def test_creative_group_defers_when_any_cell_is_not_delivery_ready() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE ad_experiment (
           source_report_id TEXT, created_at TEXT, experiment_code TEXT, state TEXT
        )"""
    )
    conn.executemany(
        "INSERT INTO ad_experiment VALUES ('launch_1',?,?,?)",
        [
            ("2026-08-01", "EXP-A", "RUNNING"),
            ("2026-08-01", "EXP-B", "META_REVIEW_PENDING"),
        ],
    )

    evaluator = object.__new__(CreativeGroupEvaluator)
    evaluator.conn = conn
    with pytest.raises(GrowthValidationError, match="creative_experiment_group_not_ready:META_REVIEW_PENDING"):
        evaluator._group("launch_1")
