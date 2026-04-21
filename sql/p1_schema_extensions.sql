CREATE TABLE IF NOT EXISTS lead_status_history (
    history_id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    trigger_event_id TEXT,
    trigger_task_id TEXT,
    operator_id TEXT,
    operator_name TEXT,
    remark TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lead_status_history_lead_id_created_at
ON lead_status_history (lead_id, created_at);

CREATE INDEX IF NOT EXISTS idx_lead_status_history_to_status
ON lead_status_history (to_status);

CREATE TABLE IF NOT EXISTS evidence_files (
    evidence_id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    task_id TEXT,
    file_url TEXT NOT NULL,
    file_type TEXT NOT NULL,
    source_channel TEXT,
    ocr_status TEXT NOT NULL,
    ocr_text TEXT,
    ocr_json TEXT NOT NULL DEFAULT '{}',
    review_status TEXT NOT NULL,
    review_reason TEXT,
    reviewer_id TEXT,
    reviewer_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_files_lead_id
ON evidence_files (lead_id);

CREATE INDEX IF NOT EXISTS idx_evidence_files_review_status
ON evidence_files (review_status);

CREATE TABLE IF NOT EXISTS bind_check_jobs (
    job_id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    evidence_id TEXT,
    account_id TEXT,
    guild_code TEXT,
    check_source TEXT NOT NULL,
    status TEXT NOT NULL,
    result_code TEXT,
    result_reason TEXT,
    raw_result TEXT NOT NULL DEFAULT '{}',
    retry_count INTEGER NOT NULL DEFAULT 0,
    scheduled_at TEXT NOT NULL,
    finished_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bind_check_jobs_lead_id
ON bind_check_jobs (lead_id);

CREATE INDEX IF NOT EXISTS idx_bind_check_jobs_status
ON bind_check_jobs (status);

CREATE TABLE IF NOT EXISTS group_join_jobs (
    job_id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    target_group TEXT NOT NULL,
    join_type TEXT NOT NULL,
    status TEXT NOT NULL,
    result_code TEXT,
    result_reason TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    evidence_url TEXT,
    scheduled_at TEXT NOT NULL,
    finished_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_group_join_jobs_lead_id
ON group_join_jobs (lead_id);

CREATE INDEX IF NOT EXISTS idx_group_join_jobs_status
ON group_join_jobs (status);

CREATE TABLE IF NOT EXISTS reengagement_jobs (
    job_id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    job_type TEXT NOT NULL,
    trigger_reason TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    scheduled_at TEXT NOT NULL,
    finished_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reengagement_jobs_lead_id
ON reengagement_jobs (lead_id);

CREATE INDEX IF NOT EXISTS idx_reengagement_jobs_status
ON reengagement_jobs (status);

CREATE TABLE IF NOT EXISTS daily_funnel_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    snapshot_date TEXT NOT NULL,
    source_platform TEXT NOT NULL,
    source_campaign TEXT,
    country TEXT NOT NULL,
    lead_count INTEGER NOT NULL DEFAULT 0,
    engaged_count INTEGER NOT NULL DEFAULT 0,
    account_submitted_count INTEGER NOT NULL DEFAULT 0,
    bind_success_count INTEGER NOT NULL DEFAULT 0,
    group_join_success_count INTEGER NOT NULL DEFAULT 0,
    cost_amount REAL,
    currency TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(snapshot_date, source_platform, source_campaign, country)
);
