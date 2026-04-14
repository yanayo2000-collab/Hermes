from fastapi.testclient import TestClient

from app.main import create_app


def make_client():
    app = create_app({"DB_PATH": ":memory:"})
    return TestClient(app)


def test_lead_upsert_creates_lead_and_customer_stub():
    client = make_client()

    response = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-1",
            "source_platform": "meta",
            "source_campaign": "camp-a",
            "source_page_id": "LK_ID/fb_general",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81234567890",
            "pendaftaran_group": "MCN-11",
            "app_name": "Linky",
            "dept_name": "Permata",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["is_new"] is True
    assert body["current_status"] == "new"
    assert body["matched_customer_id"] is not None
    assert body["lead_id"] is not None


def test_event_collect_persists_event_and_returns_event_id():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-2",
            "source_platform": "meta",
            "source_page_id": "page-1",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81111111111",
        },
    ).json()

    response = client.post(
        "/api/events/collect",
        json={
            "trace_id": "trace-2",
            "lead_id": lead["lead_id"],
            "event_type": "account_id_submitted",
            "event_source": "landing_page",
            "event_value": "45772164",
            "page_id": "page-1",
            "session_id": "sess-1",
            "happened_at": "2026-02-11T09:00:00Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["event_id"] is not None


def test_task_lifecycle_create_and_report_result():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-3",
            "source_platform": "meta",
            "source_page_id": "page-1",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "82222222222",
        },
    ).json()

    task = client.post(
        "/api/tasks/create",
        json={
            "lead_id": lead["lead_id"],
            "task_type": "crm_sync",
            "priority": "P0",
            "payload": {"mobile": "82222222222"},
            "dedupe_key": "crm-sync-trace-3",
            "created_by": "system",
            "created_at": "2026-02-11T09:05:00Z",
        },
    ).json()

    response = client.post(
        f"/api/tasks/{task['task_id']}/result",
        json={
            "status": "success",
            "result_code": "ok",
            "result_reason": "synced",
            "finished_at": "2026-02-11T09:06:00Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == task["task_id"]
    assert body["crm_sync_status"] == "pending"
    assert body["next_action"] == "sync_customer"


def test_customer_sync_upserts_customer_projection():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-4",
            "source_platform": "meta",
            "source_page_id": "page-1",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "83333333333",
        },
    ).json()

    task = client.post(
        "/api/tasks/create",
        json={
            "lead_id": lead["lead_id"],
            "task_type": "crm_sync",
            "priority": "P0",
            "payload": {},
            "dedupe_key": "crm-sync-trace-4",
            "created_by": "system",
            "created_at": "2026-02-11T09:10:00Z",
        },
    ).json()

    response = client.post(
        "/api/crm/customer-sync",
        json={
            "lead_id": lead["lead_id"],
            "task_id": task["task_id"],
            "mobile": "83333333333",
            "area_code": 62,
            "crm_patch": {
                "pendaftaran_group": "MCN-11",
                "payment_status": "Waiting For Payment Rp30000",
                "user_quality": "优质",
                "remark": "auto synced",
            },
            "sync_mode": "upsert",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] in {"insert", "update"}
    assert body["sync_status"] == "success"
    assert body["customer_id"] is not None


def test_daily_summary_returns_aggregated_counts():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-5",
            "source_platform": "meta",
            "source_page_id": "page-1",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "84444444444",
        },
    ).json()
    client.post(
        "/api/events/collect",
        json={
            "trace_id": "trace-5",
            "lead_id": lead["lead_id"],
            "event_type": "contact_clicked",
            "event_source": "landing_page",
            "event_value": "wa",
            "page_id": "page-1",
            "session_id": "sess-5",
            "happened_at": "2026-02-11T09:00:00Z",
        },
    )
    task = client.post(
        "/api/tasks/create",
        json={
            "lead_id": lead["lead_id"],
            "task_type": "crm_sync",
            "priority": "P0",
            "payload": {},
            "dedupe_key": "crm-sync-trace-5",
            "created_by": "system",
            "created_at": "2026-02-11T09:10:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{task['task_id']}/result",
        json={
            "status": "success",
            "result_code": "ok",
            "result_reason": "done",
            "finished_at": "2026-02-11T09:12:00Z",
        },
    )

    response = client.get("/api/reports/daily-summary")

    assert response.status_code == 200
    body = response.json()
    assert body["lead_count"] == 1
    assert body["engaged_count"] == 1
    assert body["success_count"] == 1
    assert body["failed_count"] == 0
    assert body["pending_count"] == 0
