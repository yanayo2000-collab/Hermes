-- PostgreSQL bootstrap schema for MCN intake high-concurrency deployment.
-- This is the first migration target for moving off SQLite.

CREATE TABLE IF NOT EXISTS ingress_events (
    event_id TEXT PRIMARY KEY,
    ingress_type TEXT NOT NULL,
    source_key TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload JSONB NOT NULL,
    status TEXT NOT NULL,
    result_snapshot JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ingress_jobs (
    job_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES ingress_events(event_id),
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS operator_audit_log (
    audit_id TEXT PRIMARY KEY,
    lead_id TEXT,
    ingress_event_id TEXT,
    event_type TEXT NOT NULL,
    event_source TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingress_events_status_created_at ON ingress_events (status, created_at);
CREATE INDEX IF NOT EXISTS idx_ingress_jobs_status_available_at ON ingress_jobs (status, available_at);
CREATE INDEX IF NOT EXISTS idx_operator_audit_log_lead_created_at ON operator_audit_log (lead_id, created_at);
