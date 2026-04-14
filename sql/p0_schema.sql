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
