from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.growth.new_account_launch_meta_delete import (
    DELETE_CONFIRMATION,
    LaunchMetaDeleteConflict,
    LaunchMetaDeleteManualReview,
    NewAccountLaunchMetaDeleteService,
    launch_meta_delete_status,
)
from app.growth.new_account_launch_retention import ensure_new_account_launch_retention_tables
from app.growth.schema import ensure_growth_schema


LAUNCH_ID = "newacct_0123456789abcdefghij"
CAMPAIGN_ID = "campaign-1"
ADSET_IDS = ["adset-1", "adset-2", "adset-3"]
AD_IDS = ["ad-1", "ad-2", "ad-3"]


class Response:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.headers = {}

    def json(self) -> dict:
        return dict(self._body)


class MetaSession:
    def __init__(
        self, *, campaign_status: str = "PAUSED", sticky_delete: str = "",
        soft_delete: bool = False,
    ) -> None:
        self.objects = {
            CAMPAIGN_ID: {"id": CAMPAIGN_ID, "name": "campaign", "status": campaign_status, "effective_status": campaign_status},
            **{
                adset_id: {"id": adset_id, "name": adset_id, "status": "PAUSED", "effective_status": "PAUSED", "campaign_id": CAMPAIGN_ID}
                for adset_id in ADSET_IDS
            },
            **{
                ad_id: {"id": ad_id, "name": ad_id, "status": "PAUSED", "effective_status": "PAUSED", "campaign_id": CAMPAIGN_ID, "adset_id": ADSET_IDS[index]}
                for index, ad_id in enumerate(AD_IDS)
            },
        }
        self.sticky_delete = sticky_delete
        self.soft_delete = soft_delete
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _object_id(url: str) -> str:
        return str(url).rstrip("/").rsplit("/", 1)[-1]

    def get(self, url: str, **_kwargs: object) -> Response:
        object_id = self._object_id(url)
        self.calls.append(("GET", object_id))
        if object_id not in self.objects:
            return Response({"error": {"code": 100, "error_subcode": 33, "message": "Unsupported get request"}}, 400)
        return Response(self.objects[object_id])

    def delete(self, url: str, **_kwargs: object) -> Response:
        object_id = self._object_id(url)
        self.calls.append(("DELETE", object_id))
        if object_id != self.sticky_delete:
            if self.soft_delete and object_id in self.objects:
                self.objects[object_id]["status"] = "DELETED"
                self.objects[object_id]["effective_status"] = "DELETED"
            else:
                self.objects.pop(object_id, None)
        return Response({"success": True})


def _database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_growth_schema(conn)
    ensure_new_account_launch_retention_tables(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_creative_direction_mapping (
            ad_id TEXT PRIMARY KEY,direction_key TEXT NOT NULL,experiment_id TEXT NOT NULL DEFAULT '',
            launch_id TEXT NOT NULL DEFAULT '',source TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
        )
        """
    )
    for index, (adset_id, ad_id) in enumerate(zip(ADSET_IDS, AD_IDS), start=1):
        conn.execute(
            """
            INSERT INTO ad_experiment
            (experiment_id,experiment_code,target_app,country,platform,account_id,source_report_id,
             source_campaign_id,source_adset_id,source_ad_id,source_creative_id,experiment_type,
             state,created_by,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'NEW_AD_TEST','PAUSED','test','2026-08-05T00:00:00+00:00','2026-08-05T00:00:00+00:00')
            """,
            (f"experiment-{index}", f"EXP-{index}", "tugao", "BR", "meta", "123", LAUNCH_ID, CAMPAIGN_ID, adset_id, ad_id, f"creative-{index}"),
        )
        conn.execute(
            """
            INSERT INTO ad_creative_direction_mapping
            (ad_id,direction_key,experiment_id,launch_id,source,created_at,updated_at)
            VALUES (?,?,?,?, 'test','2026-08-05T00:00:00+00:00','2026-08-05T00:00:00+00:00')
            """,
            (ad_id, f"direction-{index}", f"experiment-{index}", LAUNCH_ID),
        )
    conn.execute(
        """
        INSERT INTO ad_new_account_launch_archive
        (launch_id,status,archived_at,archived_by,restored_at,restored_by,updated_at)
        VALUES (?,'ARCHIVED','2026-08-05T00:00:00+00:00','test','','','2026-08-05T00:00:00+00:00')
        """,
        (LAUNCH_ID,),
    )
    conn.commit()
    return conn


def _service(conn: sqlite3.Connection, session: MetaSession) -> NewAccountLaunchMetaDeleteService:
    return NewAccountLaunchMetaDeleteService(
        conn, session=session, access_token="token",
        graph_root="https://graph.example/v25.0", live_delete_enabled=True,
    )


def test_preview_requires_exclusive_owned_meta_objects(tmp_path: Path) -> None:
    conn = _database(tmp_path / "growth.sqlite3")
    preview = _service(conn, MetaSession()).preview(LAUNCH_ID)
    assert preview["eligible"] is True
    assert preview["ownership_verified"] is True
    assert preview["shared_references"] == []
    assert preview["relationships_verified"] is True
    assert preview["all_paused"] is True
    assert preview["active_object_count"] == 0
    assert preview["counts"] == {"ads": 3, "adsets": 3, "campaigns": 1}
    assert [item["object_type"] for item in preview["plan"]["delete_order"]] == [
        "AD", "AD", "AD", "ADSET", "ADSET", "ADSET", "CAMPAIGN",
    ]
    assert preview["plan"]["status_snapshot"][-1] == {
        "object_type": "CAMPAIGN", "object_id": CAMPAIGN_ID,
        "status": "PAUSED", "effective_status": "PAUSED",
    }


def test_preview_accepts_nested_verified_execution_receipt(tmp_path: Path) -> None:
    conn = _database(tmp_path / "growth.sqlite3")
    conn.execute("DELETE FROM ad_creative_direction_mapping")
    conn.execute(
        """
        INSERT INTO growth_operation_action
        (operation_action_id,decision_id,action_type,target_type,target_id,status,
         payload_json,created_at,updated_at)
        VALUES ('action-delete-owner','decision-owner','CREATE_PAUSED_AD','LAUNCH',?,'VERIFIED',?,
                '2026-08-05T00:00:00+00:00','2026-08-05T00:00:00+00:00')
        """,
        (LAUNCH_ID, json.dumps({"launch_id": LAUNCH_ID})),
    )
    conn.execute(
        """
        INSERT INTO meta_execution_task
        (execution_task_id,operation_action_id,idempotency_key,request_hash,status,
         meta_object_ids_json,created_at,updated_at)
        VALUES ('task-delete-owner','action-delete-owner','nested-owner','nested-owner-hash','SUCCESS',?,
                '2026-08-05T00:00:00+00:00','2026-08-05T00:00:00+00:00')
        """,
        (json.dumps({
            "campaign_id": CAMPAIGN_ID,
            "adset_ids": ADSET_IDS,
            "ad_ids": AD_IDS,
        }),),
    )
    conn.commit()

    preview = _service(conn, MetaSession()).preview(LAUNCH_ID)

    assert preview["eligible"] is True
    assert preview["ownership_verified"] is True


def test_execute_deletes_children_first_reads_back_and_then_purges_order(tmp_path: Path) -> None:
    conn = _database(tmp_path / "growth.sqlite3")
    session = MetaSession()
    service = _service(conn, session)
    preview = service.preview(LAUNCH_ID)
    result = service.execute(
        LAUNCH_ID, actor="operator", confirmation=DELETE_CONFIRMATION,
        plan_hash_value=preview["plan_hash"], idempotency_key="delete-launch-meta-1",
    )
    assert result["status"] == "DELETED"
    assert result["meta_delete_status"] == "VERIFIED_DELETED"
    assert result["meta_deleted_counts"] == {"ads": 3, "adsets": 3, "campaigns": 1}
    assert result["meta_writes_performed"] is True
    assert [value for method, value in session.calls if method == "DELETE"] == [*AD_IDS, *ADSET_IDS, CAMPAIGN_ID]
    assert conn.execute("SELECT status FROM ad_new_account_launch_archive WHERE launch_id=?", (LAUNCH_ID,)).fetchone()[0] == "PURGED"
    assert conn.execute("SELECT status FROM ad_new_account_launch_meta_delete_audit").fetchone()[0] == "SUCCESS"
    calls_after_success = list(session.calls)
    replay = service.execute(
        LAUNCH_ID, actor="operator", confirmation=DELETE_CONFIRMATION,
        plan_hash_value=preview["plan_hash"], idempotency_key="delete-launch-meta-1",
    )
    assert replay == result
    assert session.calls == calls_after_success


def test_enqueue_returns_before_meta_delete_and_background_reports_progress(tmp_path: Path) -> None:
    conn = _database(tmp_path / "growth.sqlite3")
    session = MetaSession()
    service = _service(conn, session)
    preview = service.preview(LAUNCH_ID)
    delete_calls_before_enqueue = [item for item in session.calls if item[0] == "DELETE"]

    queued = service.enqueue(
        LAUNCH_ID, actor="operator", confirmation=DELETE_CONFIRMATION,
        plan_hash_value=preview["plan_hash"], idempotency_key="delete-async-1",
    )

    assert queued["status"] == "STARTED"
    assert queued["completed_count"] == 0
    assert queued["target_count"] == 7
    assert queued["can_leave"] is True
    assert [item for item in session.calls if item[0] == "DELETE"] == delete_calls_before_enqueue

    result = service.run_enqueued(queued["delete_id"], LAUNCH_ID, actor="operator")
    status = launch_meta_delete_status(conn, LAUNCH_ID)
    assert result["meta_delete_status"] == "VERIFIED_DELETED"
    assert status["status"] == "SUCCESS"
    assert status["completed_count"] == status["target_count"] == 7
    assert status["progress_percent"] == 100


def test_background_claim_prevents_duplicate_delete_execution(tmp_path: Path) -> None:
    conn = _database(tmp_path / "growth.sqlite3")
    session = MetaSession()
    service = _service(conn, session)
    preview = service.preview(LAUNCH_ID)
    queued = service.enqueue(
        LAUNCH_ID, actor="operator", confirmation=DELETE_CONFIRMATION,
        plan_hash_value=preview["plan_hash"], idempotency_key="delete-async-dedup",
    )
    service.run_enqueued(queued["delete_id"], LAUNCH_ID, actor="operator")
    delete_calls = [item for item in session.calls if item[0] == "DELETE"]

    replay = service.run_enqueued(queued["delete_id"], LAUNCH_ID, actor="operator")

    assert replay["status"] == "SUCCESS"
    assert [item for item in session.calls if item[0] == "DELETE"] == delete_calls


def test_background_uncertainty_stops_in_manual_review_without_retry(tmp_path: Path) -> None:
    conn = _database(tmp_path / "growth.sqlite3")
    session = MetaSession(sticky_delete=AD_IDS[0])
    service = _service(conn, session)
    preview = service.preview(LAUNCH_ID)
    queued = service.enqueue(
        LAUNCH_ID, actor="operator", confirmation=DELETE_CONFIRMATION,
        plan_hash_value=preview["plan_hash"], idempotency_key="delete-async-uncertain",
    )

    with pytest.raises(LaunchMetaDeleteManualReview, match="meta_delete_result_uncertain"):
        service.run_enqueued(queued["delete_id"], LAUNCH_ID, actor="operator")

    status = launch_meta_delete_status(conn, LAUNCH_ID)
    assert status["status"] == "MANUAL_REVIEW"
    assert status["requires_manual_review"] is True
    assert [item for item in session.calls if item[0] == "DELETE"] == [("DELETE", AD_IDS[0])]
    replay = service.run_enqueued(queued["delete_id"], LAUNCH_ID, actor="operator")
    assert replay["status"] == "MANUAL_REVIEW"
    assert [item for item in session.calls if item[0] == "DELETE"] == [("DELETE", AD_IDS[0])]


def test_stale_started_attempt_surfaces_manual_review_instead_of_infinite_spinner(tmp_path: Path) -> None:
    conn = _database(tmp_path / "growth.sqlite3")
    service = _service(conn, MetaSession())
    preview = service.preview(LAUNCH_ID)
    service.enqueue(
        LAUNCH_ID, actor="operator", confirmation=DELETE_CONFIRMATION,
        plan_hash_value=preview["plan_hash"], idempotency_key="delete-stale-started",
    )
    conn.execute(
        "UPDATE ad_new_account_launch_meta_delete_audit SET updated_at='2026-08-01T00:00:00+00:00'",
    )
    conn.commit()

    status = launch_meta_delete_status(conn, LAUNCH_ID)

    assert status["status"] == "MANUAL_REVIEW"
    assert status["stale_started"] is True
    assert status["requires_manual_review"] is True


def test_preview_blocks_shared_objects_but_allows_active_objects(tmp_path: Path) -> None:
    conn = _database(tmp_path / "growth.sqlite3")
    conn.execute(
        """
        INSERT INTO ad_experiment
        (experiment_id,experiment_code,target_app,country,platform,account_id,source_report_id,
         source_campaign_id,experiment_type,state,created_at,updated_at)
        VALUES ('shared-exp','SHARED','tugao','BR','meta','123','newacct_shared0000000000000',?,'NEW_AD_TEST','PAUSED','2026-08-05T00:00:00+00:00','2026-08-05T00:00:00+00:00')
        """,
        (CAMPAIGN_ID,),
    )
    conn.commit()
    shared = _service(conn, MetaSession()).preview(LAUNCH_ID)
    assert shared["eligible"] is False
    assert "meta_objects_shared_by_other_orders" in shared["blocked_reasons"]
    conn.execute("DELETE FROM ad_experiment WHERE experiment_id='shared-exp'")
    conn.commit()
    active = _service(conn, MetaSession(campaign_status="ACTIVE")).preview(LAUNCH_ID)
    assert active["eligible"] is True
    assert active["all_paused"] is False
    assert active["active_object_count"] == 1
    assert active["plan"]["status_snapshot"][-1]["status"] == "ACTIVE"


def test_execute_allows_active_objects_and_keeps_verified_delete_order(tmp_path: Path) -> None:
    conn = _database(tmp_path / "growth.sqlite3")
    session = MetaSession(campaign_status="ACTIVE")
    service = _service(conn, session)
    preview = service.preview(LAUNCH_ID)
    result = service.execute(
        LAUNCH_ID, actor="operator", confirmation=DELETE_CONFIRMATION,
        plan_hash_value=preview["plan_hash"], idempotency_key="delete-active-launch-meta",
    )
    assert result["meta_delete_status"] == "VERIFIED_DELETED"
    assert [value for method, value in session.calls if method == "DELETE"] == [
        *AD_IDS, *ADSET_IDS, CAMPAIGN_ID,
    ]


def test_execute_accepts_meta_soft_deleted_status_as_verified(tmp_path: Path) -> None:
    conn = _database(tmp_path / "growth.sqlite3")
    session = MetaSession(soft_delete=True)
    service = _service(conn, session)
    preview = service.preview(LAUNCH_ID)

    result = service.execute(
        LAUNCH_ID, actor="operator", confirmation=DELETE_CONFIRMATION,
        plan_hash_value=preview["plan_hash"], idempotency_key="delete-soft-status",
    )

    assert result["meta_delete_status"] == "VERIFIED_DELETED"
    assert [value for method, value in session.calls if method == "DELETE"] == [
        *AD_IDS, *ADSET_IDS, CAMPAIGN_ID,
    ]
    audit = conn.execute("SELECT status,results_json FROM ad_new_account_launch_meta_delete_audit").fetchone()
    assert audit["status"] == "SUCCESS"
    assert all(item["verified_deleted"] for item in json.loads(audit["results_json"]))


def test_uncertain_delete_stops_without_retry_or_order_purge(tmp_path: Path) -> None:
    conn = _database(tmp_path / "growth.sqlite3")
    session = MetaSession(sticky_delete=AD_IDS[0])
    service = _service(conn, session)
    preview = service.preview(LAUNCH_ID)
    with pytest.raises(LaunchMetaDeleteManualReview, match="meta_delete_result_uncertain"):
        service.execute(
            LAUNCH_ID, actor="operator", confirmation=DELETE_CONFIRMATION,
            plan_hash_value=preview["plan_hash"], idempotency_key="delete-launch-meta-uncertain",
        )
    assert [value for method, value in session.calls if method == "DELETE"] == [AD_IDS[0]]
    assert conn.execute("SELECT status FROM ad_new_account_launch_archive WHERE launch_id=?", (LAUNCH_ID,)).fetchone()[0] == "ARCHIVED"
    assert conn.execute("SELECT status FROM ad_new_account_launch_meta_delete_audit").fetchone()[0] == "MANUAL_REVIEW"


def test_manual_review_resume_reconciles_attempted_object_without_redelete(tmp_path: Path) -> None:
    conn = _database(tmp_path / "growth.sqlite3")
    session = MetaSession(sticky_delete=AD_IDS[0], soft_delete=True)
    service = _service(conn, session)
    preview = service.preview(LAUNCH_ID)
    kwargs = {
        "actor": "operator",
        "confirmation": DELETE_CONFIRMATION,
        "plan_hash_value": preview["plan_hash"],
        "idempotency_key": "delete-resume-soft-status",
    }
    with pytest.raises(LaunchMetaDeleteManualReview, match="meta_delete_result_uncertain"):
        service.execute(LAUNCH_ID, **kwargs)
    assert [value for method, value in session.calls if method == "DELETE"] == [AD_IDS[0]]

    session.objects[AD_IDS[0]]["status"] = "DELETED"
    session.objects[AD_IDS[0]]["effective_status"] = "DELETED"
    session.sticky_delete = ""
    calls_before_resume = len(session.calls)
    result = service.execute(LAUNCH_ID, **kwargs)

    resumed_calls = session.calls[calls_before_resume:]
    assert ("GET", AD_IDS[0]) in resumed_calls
    assert ("DELETE", AD_IDS[0]) not in resumed_calls
    assert [value for method, value in session.calls if method == "DELETE"] == [
        AD_IDS[0], AD_IDS[1], AD_IDS[2], *ADSET_IDS, CAMPAIGN_ID,
    ]
    assert result["meta_delete_status"] == "VERIFIED_DELETED"
    assert conn.execute("SELECT status FROM ad_new_account_launch_meta_delete_audit").fetchone()[0] == "SUCCESS"


def test_preview_fails_closed_when_live_delete_is_disabled(tmp_path: Path) -> None:
    conn = _database(tmp_path / "growth.sqlite3")
    service = NewAccountLaunchMetaDeleteService(
        conn, session=MetaSession(), access_token="token",
        graph_root="https://graph.example/v25.0", live_delete_enabled=False,
    )
    preview = service.preview(LAUNCH_ID)
    assert preview["eligible"] is False
    assert preview["blocked_reasons"] == ["meta_delete_execution_unavailable"]
    with pytest.raises(LaunchMetaDeleteConflict):
        service.execute(
            LAUNCH_ID, actor="operator", confirmation=DELETE_CONFIRMATION,
            plan_hash_value=preview["plan_hash"], idempotency_key="disabled",
        )
