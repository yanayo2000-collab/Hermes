from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[1] / "data" / "automation.db")


class LeadUpsertRequest(BaseModel):
    trace_id: str
    source_platform: str
    source_campaign: Optional[str] = None
    source_page_id: str
    country: str
    area_code: int
    mobile: str
    yw_id: Optional[str] = None
    app_name: Optional[str] = None
    dept_name: Optional[str] = None
    pendaftaran_group: Optional[str] = None
    inviter_id: Optional[str] = None
    occurred_at: Optional[str] = None


class EventCollectRequest(BaseModel):
    trace_id: str
    lead_id: Optional[str] = None
    event_type: str
    event_source: str
    event_value: Optional[str] = None
    page_id: Optional[str] = None
    session_id: Optional[str] = None
    operator_id: Optional[str] = None
    operator_name: Optional[str] = None
    raw_payload: Dict[str, Any] = Field(default_factory=dict)
    happened_at: Optional[str] = None


class TaskCreateRequest(BaseModel):
    lead_id: str
    task_type: str
    priority: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str
    created_by: str
    created_at: str


class TaskResultRequest(BaseModel):
    status: str
    result_code: Optional[str] = None
    result_reason: Optional[str] = None
    toast_text: Optional[str] = None
    evidence_url: Optional[str] = None
    retry_count: int = 0
    executor_type: Optional[str] = None
    executor_id: Optional[str] = None
    finished_at: str
    raw_result: Dict[str, Any] = Field(default_factory=dict)


class CustomerSyncRequest(BaseModel):
    lead_id: str
    task_id: str
    yw_id: Optional[str] = None
    mobile: str
    area_code: int
    crm_patch: Dict[str, Any] = Field(default_factory=dict)
    sync_mode: str = "upsert"


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._memory_conn: Optional[sqlite3.Connection] = None
        self._ensure_parent()
        self._init_schema()

    def _ensure_parent(self) -> None:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._memory_conn.row_factory = sqlite3.Row
            return self._memory_conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        conn = self.connect()
        conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    lead_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    source_platform TEXT NOT NULL,
                    source_campaign TEXT,
                    source_page_id TEXT NOT NULL,
                    country TEXT NOT NULL,
                    area_code INTEGER NOT NULL,
                    mobile TEXT NOT NULL,
                    yw_id TEXT,
                    app_name TEXT,
                    dept_name TEXT,
                    pendaftaran_group TEXT,
                    inviter_id TEXT,
                    current_status TEXT NOT NULL,
                    matched_customer_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(area_code, mobile)
                );

                CREATE TABLE IF NOT EXISTS customer_projection (
                    customer_id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    mobile TEXT NOT NULL,
                    area_code INTEGER NOT NULL,
                    yw_id TEXT,
                    pendaftaran_group TEXT,
                    payment_status TEXT,
                    user_quality TEXT,
                    remark TEXT,
                    join_group TEXT,
                    file_url TEXT,
                    pz_status INTEGER,
                    updated_at TEXT NOT NULL,
                    UNIQUE(area_code, mobile)
                );

                CREATE TABLE IF NOT EXISTS lead_events (
                    event_id TEXT PRIMARY KEY,
                    lead_id TEXT,
                    trace_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_source TEXT NOT NULL,
                    event_value TEXT,
                    page_id TEXT,
                    session_id TEXT,
                    operator_id TEXT,
                    operator_name TEXT,
                    raw_payload TEXT NOT NULL,
                    happened_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS automation_tasks (
                    task_id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_code TEXT,
                    result_reason TEXT,
                    toast_text TEXT,
                    evidence_url TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    executor_type TEXT,
                    executor_id TEXT,
                    finished_at TEXT,
                    raw_result TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS sync_logs (
                    sync_log_id TEXT PRIMARY KEY,
                    lead_id TEXT,
                    task_id TEXT,
                    sync_type TEXT NOT NULL,
                    target_system TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_snapshot TEXT NOT NULL,
                    response_snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
        conn.commit()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Service:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert_lead(self, payload: LeadUpsertRequest) -> Dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT lead_id, matched_customer_id FROM leads WHERE area_code = ? AND mobile = ?",
                (payload.area_code, payload.mobile),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE leads
                    SET trace_id = ?, source_platform = ?, source_campaign = ?, source_page_id = ?, country = ?,
                        yw_id = COALESCE(?, yw_id), app_name = COALESCE(?, app_name), dept_name = COALESCE(?, dept_name),
                        pendaftaran_group = COALESCE(?, pendaftaran_group), inviter_id = COALESCE(?, inviter_id), updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (
                        payload.trace_id,
                        payload.source_platform,
                        payload.source_campaign,
                        payload.source_page_id,
                        payload.country,
                        payload.yw_id,
                        payload.app_name,
                        payload.dept_name,
                        payload.pendaftaran_group,
                        payload.inviter_id,
                        now,
                        row["lead_id"],
                    ),
                )
                return {
                    "lead_id": row["lead_id"],
                    "matched_customer_id": row["matched_customer_id"],
                    "is_new": False,
                    "current_status": "new",
                }

            lead_id = create_id("lead")
            customer_id = create_id("cust")
            conn.execute(
                """
                INSERT INTO leads (
                    lead_id, trace_id, source_platform, source_campaign, source_page_id, country, area_code, mobile,
                    yw_id, app_name, dept_name, pendaftaran_group, inviter_id, current_status, matched_customer_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lead_id,
                    payload.trace_id,
                    payload.source_platform,
                    payload.source_campaign,
                    payload.source_page_id,
                    payload.country,
                    payload.area_code,
                    payload.mobile,
                    payload.yw_id,
                    payload.app_name,
                    payload.dept_name,
                    payload.pendaftaran_group,
                    payload.inviter_id,
                    "new",
                    customer_id,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO customer_projection (customer_id, lead_id, mobile, area_code, yw_id, pendaftaran_group, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (customer_id, lead_id, payload.mobile, payload.area_code, payload.yw_id, payload.pendaftaran_group, now),
            )
            return {
                "lead_id": lead_id,
                "matched_customer_id": customer_id,
                "is_new": True,
                "current_status": "new",
            }

    def collect_event(self, payload: EventCollectRequest) -> Dict[str, Any]:
        event_id = create_id("evt")
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO lead_events (
                    event_id, lead_id, trace_id, event_type, event_source, event_value, page_id, session_id,
                    operator_id, operator_name, raw_payload, happened_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    payload.lead_id,
                    payload.trace_id,
                    payload.event_type,
                    payload.event_source,
                    payload.event_value,
                    payload.page_id,
                    payload.session_id,
                    payload.operator_id,
                    payload.operator_name,
                    json.dumps(payload.raw_payload, ensure_ascii=False),
                    payload.happened_at or now,
                    now,
                ),
            )
            if payload.lead_id and payload.event_type in {"contact_clicked", "account_id_submitted", "wa_redirected"}:
                conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("engaged", now, payload.lead_id))
        return {"event_id": event_id, "accepted": True}

    def create_task(self, payload: TaskCreateRequest) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT task_id, status FROM automation_tasks WHERE dedupe_key = ?", (payload.dedupe_key,)).fetchone()
            if row:
                return {"task_id": row["task_id"], "status": row["status"]}
            task_id = create_id("task")
            conn.execute(
                """
                INSERT INTO automation_tasks (
                    task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    payload.lead_id,
                    payload.task_type,
                    payload.priority,
                    json.dumps(payload.payload, ensure_ascii=False),
                    payload.dedupe_key,
                    payload.created_by,
                    payload.created_at,
                    "pending",
                ),
            )
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("processing", utc_now(), payload.lead_id))
            return {"task_id": task_id, "status": "pending"}

    def task_result(self, task_id: str, payload: TaskResultRequest) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT lead_id FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="task not found")
            conn.execute(
                """
                UPDATE automation_tasks
                SET status = ?, result_code = ?, result_reason = ?, toast_text = ?, evidence_url = ?, retry_count = ?,
                    executor_type = ?, executor_id = ?, finished_at = ?, raw_result = ?
                WHERE task_id = ?
                """,
                (
                    payload.status,
                    payload.result_code,
                    payload.result_reason,
                    payload.toast_text,
                    payload.evidence_url,
                    payload.retry_count,
                    payload.executor_type,
                    payload.executor_id,
                    payload.finished_at,
                    json.dumps(payload.raw_result, ensure_ascii=False),
                    task_id,
                ),
            )
            lead_status = "success" if payload.status == "success" else "failed" if payload.status == "failed" else "manual_review"
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", (lead_status, utc_now(), row["lead_id"]))
            return {"task_id": task_id, "crm_sync_status": "pending", "next_action": "sync_customer"}

    def customer_sync(self, payload: CustomerSyncRequest) -> Dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            lead = conn.execute("SELECT matched_customer_id FROM leads WHERE lead_id = ?", (payload.lead_id,)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail="lead not found")
            customer_id = lead["matched_customer_id"]
            row = conn.execute("SELECT customer_id FROM customer_projection WHERE customer_id = ?", (customer_id,)).fetchone()
            action = "update" if row else "insert"
            patch = payload.crm_patch
            if row:
                conn.execute(
                    """
                    UPDATE customer_projection
                    SET yw_id = COALESCE(?, yw_id), pendaftaran_group = COALESCE(?, pendaftaran_group),
                        payment_status = COALESCE(?, payment_status), user_quality = COALESCE(?, user_quality),
                        remark = COALESCE(?, remark), join_group = COALESCE(?, join_group),
                        file_url = COALESCE(?, file_url), pz_status = COALESCE(?, pz_status), updated_at = ?
                    WHERE customer_id = ?
                    """,
                    (
                        payload.yw_id,
                        patch.get("pendaftaran_group"),
                        patch.get("payment_status"),
                        patch.get("user_quality"),
                        patch.get("remark"),
                        patch.get("join_group"),
                        patch.get("file_url"),
                        patch.get("pz_status"),
                        now,
                        customer_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO customer_projection (
                        customer_id, lead_id, mobile, area_code, yw_id, pendaftaran_group, payment_status,
                        user_quality, remark, join_group, file_url, pz_status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        customer_id,
                        payload.lead_id,
                        payload.mobile,
                        payload.area_code,
                        payload.yw_id,
                        patch.get("pendaftaran_group"),
                        patch.get("payment_status"),
                        patch.get("user_quality"),
                        patch.get("remark"),
                        patch.get("join_group"),
                        patch.get("file_url"),
                        patch.get("pz_status"),
                        now,
                    ),
                )
            conn.execute(
                "INSERT INTO sync_logs (sync_log_id, lead_id, task_id, sync_type, target_system, status, request_snapshot, response_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    create_id("sync"),
                    payload.lead_id,
                    payload.task_id,
                    "customer_sync",
                    "crm_projection",
                    "success",
                    json.dumps(payload.model_dump(), ensure_ascii=False),
                    json.dumps({"customer_id": customer_id, "action": action}, ensure_ascii=False),
                    now,
                ),
            )
            return {"customer_id": customer_id, "action": action, "sync_status": "success"}

    def daily_summary(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            lead_count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            engaged_count = conn.execute("SELECT COUNT(*) FROM lead_events WHERE event_type IN ('contact_clicked', 'wa_redirected', 'account_id_submitted')").fetchone()[0]
            account_submitted_count = conn.execute("SELECT COUNT(*) FROM lead_events WHERE event_type = 'account_id_submitted'").fetchone()[0]
            success_count = conn.execute("SELECT COUNT(*) FROM automation_tasks WHERE status = 'success'").fetchone()[0]
            failed_count = conn.execute("SELECT COUNT(*) FROM automation_tasks WHERE status = 'failed'").fetchone()[0]
            pending_count = conn.execute("SELECT COUNT(*) FROM automation_tasks WHERE status IN ('pending', 'running', 'retry_waiting')").fetchone()[0]
        return {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "lead_count": lead_count,
            "engaged_count": engaged_count,
            "account_submitted_count": account_submitted_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "pending_count": pending_count,
            "top_fail_reasons": [],
            "group_breakdown": [],
            "operator_breakdown": [],
        }


def create_app(settings: Optional[Dict[str, Any]] = None) -> FastAPI:
    cfg = {"DB_PATH": DEFAULT_DB_PATH}
    if settings:
        cfg.update(settings)
    db = Database(cfg["DB_PATH"])
    service = Service(db)
    app = FastAPI(title="MCN AI Automation")

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/leads/upsert")
    def leads_upsert(payload: LeadUpsertRequest) -> Dict[str, Any]:
        return service.upsert_lead(payload)

    @app.post("/api/events/collect")
    def events_collect(payload: EventCollectRequest) -> Dict[str, Any]:
        return service.collect_event(payload)

    @app.post("/api/tasks/create")
    def tasks_create(payload: TaskCreateRequest) -> Dict[str, Any]:
        return service.create_task(payload)

    @app.post("/api/tasks/{task_id}/result")
    def tasks_result(task_id: str, payload: TaskResultRequest) -> Dict[str, Any]:
        return service.task_result(task_id, payload)

    @app.post("/api/crm/customer-sync")
    def customer_sync(payload: CustomerSyncRequest) -> Dict[str, Any]:
        return service.customer_sync(payload)

    @app.get("/api/reports/daily-summary")
    def daily_summary() -> Dict[str, Any]:
        return service.daily_summary()

    return app


app = create_app()
