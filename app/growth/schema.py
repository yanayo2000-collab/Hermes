from __future__ import annotations

import sqlite3
import time


GROWTH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS growth_context_snapshot (
    context_snapshot_id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '',
    audience_json TEXT NOT NULL DEFAULT '{}',
    funnel_stage TEXT NOT NULL DEFAULT '',
    business_goal TEXT NOT NULL DEFAULT '',
    creative_type TEXT NOT NULL DEFAULT '',
    creative_angle TEXT NOT NULL DEFAULT '',
    copy_style TEXT NOT NULL DEFAULT '',
    cta TEXT NOT NULL DEFAULT '',
    placement TEXT NOT NULL DEFAULT '',
    budget REAL,
    target_cpa REAL,
    bid_strategy TEXT NOT NULL DEFAULT '',
    market_context_json TEXT NOT NULL DEFAULT '{}',
    snapshot_kind TEXT NOT NULL DEFAULT 'INITIAL'
        CHECK (snapshot_kind IN ('INITIAL', 'ADJUSTMENT')),
    parent_snapshot_id TEXT,
    snapshot_hash TEXT NOT NULL UNIQUE,
    data_origin TEXT NOT NULL DEFAULT 'NATIVE_V2'
        CHECK (data_origin IN ('LEGACY', 'NATIVE_V2')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (parent_snapshot_id) REFERENCES growth_context_snapshot(context_snapshot_id)
);

CREATE TABLE IF NOT EXISTS experiment_context_snapshots (
    experiment_id TEXT NOT NULL,
    context_snapshot_id TEXT NOT NULL,
    relation_type TEXT NOT NULL DEFAULT 'INITIAL'
        CHECK (relation_type IN ('INITIAL', 'ADJUSTMENT')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (experiment_id, context_snapshot_id),
    FOREIGN KEY (context_snapshot_id) REFERENCES growth_context_snapshot(context_snapshot_id)
);

CREATE TABLE IF NOT EXISTS growth_decision (
    decision_id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL,
    context_snapshot_id TEXT NOT NULL,
    selected_action TEXT NOT NULL,
    rejected_actions_json TEXT NOT NULL DEFAULT '[]',
    decision_reason_json TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL DEFAULT 'CREATED'
        CHECK (status IN ('CREATED', 'REJECTED', 'BOUND')),
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    decided_by TEXT NOT NULL DEFAULT '',
    data_origin TEXT NOT NULL DEFAULT 'NATIVE_V2'
        CHECK (data_origin IN ('LEGACY', 'NATIVE_V2')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (context_snapshot_id) REFERENCES growth_context_snapshot(context_snapshot_id)
);

CREATE TABLE IF NOT EXISTS growth_decision_episode (
    episode_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    experiment_id TEXT NOT NULL DEFAULT '',
    context_snapshot_id TEXT NOT NULL,
    observation_json TEXT NOT NULL DEFAULT '{}',
    hypothesis_json TEXT NOT NULL DEFAULT '{}',
    action_json TEXT NOT NULL DEFAULT '{}',
    outcome_json TEXT NOT NULL DEFAULT '{}',
    lesson_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'CREATED'
        CHECK (status IN ('CREATED', 'ACTION_EXECUTING', 'WAITING_OUTCOME', 'OUTCOME_READY', 'LESSON_REVIEW', 'COMPLETED')),
    data_origin TEXT NOT NULL DEFAULT 'NATIVE_V2'
        CHECK (data_origin IN ('LEGACY', 'NATIVE_V2')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (decision_id) REFERENCES growth_decision(decision_id),
    FOREIGN KEY (context_snapshot_id) REFERENCES growth_context_snapshot(context_snapshot_id)
);

CREATE TABLE IF NOT EXISTS growth_operation_action (
    operation_action_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    episode_id TEXT,
    action_type TEXT NOT NULL,
    action_scope TEXT NOT NULL DEFAULT 'BUSINESS_PROTECTION'
        CHECK (action_scope IN ('EXPERIMENT', 'BUSINESS_PROTECTION')),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'CREATED'
        CHECK (status IN ('CREATED', 'QUEUED', 'EXECUTING', 'VERIFIED', 'FAILED', 'MANUAL_REVIEW')),
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES growth_decision(decision_id),
    FOREIGN KEY (episode_id) REFERENCES growth_decision_episode(episode_id)
);

CREATE TABLE IF NOT EXISTS meta_execution_task (
    execution_task_id TEXT PRIMARY KEY,
    operation_action_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED'
        CHECK (status IN ('QUEUED', 'RUNNING', 'VERIFYING', 'SUCCESS', 'MANUAL_REVIEW')),
    current_step TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    meta_object_ids_json TEXT NOT NULL DEFAULT '{}',
    locked_by TEXT NOT NULL DEFAULT '',
    locked_at TEXT NOT NULL DEFAULT '',
    heartbeat_at TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (operation_action_id) REFERENCES growth_operation_action(operation_action_id)
);

CREATE TABLE IF NOT EXISTS meta_execution_task_receipt (
    receipt_id TEXT PRIMARY KEY,
    execution_task_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    step_status TEXT NOT NULL CHECK (step_status IN ('SUCCESS', 'FAILED', 'UNKNOWN', 'VERIFIED')),
    step_result_json TEXT NOT NULL DEFAULT '{}',
    meta_object_ids_json TEXT NOT NULL DEFAULT '{}',
    verification_result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (execution_task_id) REFERENCES meta_execution_task(execution_task_id)
);

CREATE TABLE IF NOT EXISTS growth_operation_approval (
    approval_id TEXT PRIMARY KEY,
    operation_action_id TEXT NOT NULL UNIQUE,
    plan_hash TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PROPOSED'
        CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED')),
    proposed_by TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    consumed_at TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (operation_action_id) REFERENCES growth_operation_action(operation_action_id)
);

CREATE TABLE IF NOT EXISTS ad_experiment (
    experiment_id TEXT PRIMARY KEY,
    experiment_code TEXT NOT NULL UNIQUE,
    target_app TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT 'meta',
    account_id TEXT NOT NULL DEFAULT '',
    source_report_id TEXT NOT NULL DEFAULT '',
    source_recommendation_id TEXT NOT NULL DEFAULT '',
    source_campaign_id TEXT NOT NULL DEFAULT '',
    source_adset_id TEXT NOT NULL DEFAULT '',
    source_ad_id TEXT NOT NULL DEFAULT '',
    source_creative_id TEXT NOT NULL DEFAULT '',
    experiment_type TEXT NOT NULL CHECK (experiment_type IN (
        'NEW_AD_TEST','WINNER_EXTENSION','CREATIVE_REPAIR','CREATIVE_REPLACEMENT',
        'BUDGET_SCALE_UP','BUDGET_REDUCTION','PAUSE_TEST','REACTIVATION_TEST'
    )),
    hypothesis_json TEXT NOT NULL DEFAULT '{}',
    primary_metric TEXT NOT NULL DEFAULT '',
    guardrail_metrics_json TEXT NOT NULL DEFAULT '[]',
    maturity_rule_json TEXT NOT NULL DEFAULT '{}',
    stop_rule_json TEXT NOT NULL DEFAULT '{}',
    control_definition_json TEXT NOT NULL DEFAULT '{}',
    variant_definition_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'DRAFT' CHECK (state IN (
        'DRAFT','CREATIVE_GENERATING','CREATIVE_REVIEW','CREATIVE_REJECTED',
        'WAITING_CREATE_APPROVAL','CREATING_PAUSED_OBJECTS','CREATION_PARTIAL_FAILURE',
        'META_REVIEW_PENDING','READY_FOR_ACTIVATION','RUNNING','MATURING',
        'RECOMMENDATION_READY','WAITING_ADJUSTMENT_APPROVAL','ADJUSTING',
        'EVALUATING_ADJUSTMENT','EFFECTIVE','INEFFECTIVE','INCONCLUSIVE',
        'DATA_INCOMPLETE','MIXED_CHANGE','PAUSED','ARCHIVED'
    )),
    state_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ad_experiment_events (
    event_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES ad_experiment(experiment_id)
);

CREATE TABLE IF NOT EXISTS ad_meta_review_state (
    experiment_id TEXT PRIMARY KEY,
    ad_id TEXT NOT NULL,
    configured_status TEXT NOT NULL DEFAULT '',
    effective_status TEXT NOT NULL DEFAULT '',
    review_feedback_json TEXT NOT NULL DEFAULT '{}',
    remediation_status TEXT NOT NULL DEFAULT 'NONE' CHECK (remediation_status IN (
        'NONE','DETECTED','GENERATING','PLAN_READY','SUBMITTED','RESOLVED','FAILED'
    )),
    replacement_image_id TEXT NOT NULL DEFAULT '',
    replacement_plan_id TEXT NOT NULL DEFAULT '',
    detected_at TEXT NOT NULL DEFAULT '',
    last_checked_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES ad_experiment(experiment_id)
);

CREATE TABLE IF NOT EXISTS ad_creative_revision_window (
    revision_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL DEFAULT '',
    ad_id TEXT NOT NULL,
    creative_id TEXT NOT NULL,
    image_id TEXT NOT NULL DEFAULT '',
    adoption_id TEXT NOT NULL UNIQUE,
    effective_from TEXT NOT NULL,
    effective_to TEXT NOT NULL DEFAULT '',
    replacement_boundary_date TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('CURRENT','HISTORICAL')),
    source TEXT NOT NULL DEFAULT 'VERIFIED_META_RECEIPT',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ad_meta_review_due
    ON ad_meta_review_state(last_checked_at,effective_status);

CREATE TABLE IF NOT EXISTS ad_experiment_evaluation (
    evaluation_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    episode_id TEXT NOT NULL DEFAULT '',
    checkpoint TEXT NOT NULL CHECK (checkpoint IN ('D1','D3','D7')),
    baseline_window_json TEXT NOT NULL DEFAULT '{}',
    post_window_json TEXT NOT NULL DEFAULT '{}',
    baseline_metrics_json TEXT NOT NULL DEFAULT '{}',
    post_metrics_json TEXT NOT NULL DEFAULT '{}',
    data_quality_status TEXT NOT NULL DEFAULT 'PASS',
    dedupe_version TEXT NOT NULL DEFAULT '',
    attribution_version TEXT NOT NULL DEFAULT '',
    evaluation_status TEXT NOT NULL CHECK (evaluation_status IN (
        'EFFECTIVE','INEFFECTIVE','NEUTRAL','INSUFFICIENT_SAMPLE','DATA_INCOMPLETE',
        'NOT_ATTRIBUTABLE','MIXED_CHANGE','NOT_EXECUTED','PENDING'
    )),
    evaluated_at TEXT NOT NULL,
    UNIQUE (experiment_id, checkpoint),
    FOREIGN KEY (experiment_id) REFERENCES ad_experiment(experiment_id)
);

CREATE TABLE IF NOT EXISTS ad_experiment_cycle (
    cycle_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    source_operation_action_id TEXT NOT NULL UNIQUE,
    source_execution_task_id TEXT NOT NULL UNIQUE,
    source_receipt_id TEXT NOT NULL,
    source_plan_hash TEXT NOT NULL,
    source_receipt_hash TEXT NOT NULL,
    evidence_root_hash TEXT NOT NULL UNIQUE,
    action_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    evaluation_subject_json TEXT NOT NULL,
    evaluation_subject_hash TEXT NOT NULL,
    evaluation_checkpoints_json TEXT NOT NULL,
    window_opened_at TEXT NOT NULL,
    first_complete_date TEXT NOT NULL,
    reporting_timezone TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'WAITING_EVIDENCE' CHECK (state IN (
        'WAITING_EVIDENCE','EVALUATING','EVALUATED','NEXT_PLAN_READY','BLOCKED'
    )),
    latest_checkpoint TEXT NOT NULL DEFAULT '' CHECK (
        latest_checkpoint IN ('','D1','D3','D7')
    ),
    latest_evaluation_status TEXT NOT NULL DEFAULT '',
    causal_claim INTEGER NOT NULL DEFAULT 0 CHECK (causal_claim = 0),
    meta_write_allowed INTEGER NOT NULL DEFAULT 0 CHECK (meta_write_allowed = 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES ad_experiment(experiment_id),
    FOREIGN KEY (source_operation_action_id) REFERENCES growth_operation_action(operation_action_id),
    FOREIGN KEY (source_execution_task_id) REFERENCES meta_execution_task(execution_task_id)
);

CREATE TABLE IF NOT EXISTS ad_experiment_cycle_evaluation (
    cycle_evaluation_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    checkpoint TEXT NOT NULL CHECK (checkpoint IN ('D1','D3','D7')),
    window_json TEXT NOT NULL,
    metrics_by_experiment_json TEXT NOT NULL,
    action_candidates_json TEXT NOT NULL DEFAULT '[]',
    evaluation_status TEXT NOT NULL CHECK (evaluation_status IN (
        'OBSERVE','ACTION_RECOMMENDED','CYCLE_COMPLETE_NO_CHANGE'
    )),
    data_quality_status TEXT NOT NULL CHECK (data_quality_status = 'PASS'),
    evidence_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL UNIQUE,
    causal_claim INTEGER NOT NULL DEFAULT 0 CHECK (causal_claim = 0),
    meta_write_allowed INTEGER NOT NULL DEFAULT 0 CHECK (meta_write_allowed = 0),
    evaluated_at TEXT NOT NULL,
    UNIQUE (cycle_id, checkpoint),
    FOREIGN KEY (cycle_id) REFERENCES ad_experiment_cycle(cycle_id)
);

CREATE TABLE IF NOT EXISTS ad_experiment_cycle_next_plan (
    cycle_plan_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    cycle_evaluation_id TEXT NOT NULL UNIQUE,
    checkpoint TEXT NOT NULL CHECK (checkpoint IN ('D1','D3','D7')),
    action_type TEXT NOT NULL CHECK (action_type IN ('OBSERVE','PAUSE_AD')),
    target_experiment_id TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    operation_action_id TEXT NOT NULL DEFAULT '',
    plan_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN (
        'READY','AWAITING_CONFIRMATION','BLOCKED','SUPERSEDED','COMPLETED','DISMISSED'
    )),
    requires_confirmation INTEGER NOT NULL DEFAULT 0 CHECK (requires_confirmation IN (0,1)),
    causal_claim INTEGER NOT NULL DEFAULT 0 CHECK (causal_claim = 0),
    meta_write_allowed INTEGER NOT NULL DEFAULT 0 CHECK (meta_write_allowed = 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (cycle_id) REFERENCES ad_experiment_cycle(cycle_id),
    FOREIGN KEY (cycle_evaluation_id) REFERENCES ad_experiment_cycle_evaluation(cycle_evaluation_id)
);

CREATE TABLE IF NOT EXISTS growth_strategy_knowledge (
    knowledge_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    pattern_type TEXT NOT NULL
        CHECK (pattern_type IN ('SUCCESS_PATTERN', 'FAILURE_PATTERN', 'WARNING_PATTERN')),
    pattern_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RAW'
        CHECK (status IN ('RAW', 'REVIEWED', 'ACTIVE', 'ARCHIVED')),
    reviewed_by TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL DEFAULT '',
    activated_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (episode_id) REFERENCES growth_decision_episode(episode_id)
);

CREATE TABLE IF NOT EXISTS growth_strategy_recommendation (
    strategy_recommendation_id TEXT PRIMARY KEY,
    context_snapshot_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    rationale_json TEXT NOT NULL DEFAULT '{}',
    source_knowledge_ids_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL DEFAULT 'PROPOSED'
        CHECK (status IN ('PROPOSED', 'APPROVED', 'EXECUTED', 'REJECTED')),
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (context_snapshot_id) REFERENCES growth_context_snapshot(context_snapshot_id)
);

CREATE TABLE IF NOT EXISTS growth_autonomy_policy (
    account_id TEXT PRIMARY KEY,
    level TEXT NOT NULL CHECK (level IN (
        'L0_OBSERVE','L1_RECOMMEND','L2_PAUSED_CREATE','L3_BOUNDED_LIVE'
    )),
    allowed_action_types_json TEXT NOT NULL DEFAULT '[]',
    max_daily_budget_usd REAL NOT NULL DEFAULT 0 CHECK (max_daily_budget_usd >= 0),
    max_budget_change_pct REAL NOT NULL DEFAULT 0 CHECK (
        max_budget_change_pct >= 0 AND max_budget_change_pct <= 100
    ),
    minimum_installs INTEGER NOT NULL DEFAULT 100 CHECK (minimum_installs >= 0),
    minimum_real_joins INTEGER NOT NULL DEFAULT 10 CHECK (minimum_real_joins >= 0),
    require_real_join_attribution INTEGER NOT NULL DEFAULT 1 CHECK (
        require_real_join_attribution IN (0,1)
    ),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SUSPENDED')),
    reason TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS growth_next_action (
    next_action_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('EXPERIMENT','CREATIVE_GROUP','AUDIENCE_PAIR')),
    source_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    launch_id TEXT NOT NULL DEFAULT '',
    experiment_id TEXT NOT NULL DEFAULT '',
    checkpoint TEXT NOT NULL CHECK (checkpoint IN ('D1','D3','D7')),
    action_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN (
        'READY','APPROVAL_REQUIRED','BLOCKED','COMPLETED','DISMISSED'
    )),
    block_reason TEXT NOT NULL DEFAULT '',
    meta_write_allowed INTEGER NOT NULL DEFAULT 0 CHECK (meta_write_allowed IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_type, source_id, action_type)
);

CREATE TABLE IF NOT EXISTS ad_audience_pair_evaluation (
    pair_evaluation_id TEXT PRIMARY KEY,
    launch_id TEXT NOT NULL,
    checkpoint TEXT NOT NULL CHECK (checkpoint IN ('D1','D3','D7')),
    baseline_experiment_id TEXT NOT NULL,
    challenger_experiment_id TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    winner_experiment_id TEXT NOT NULL DEFAULT '',
    decision_status TEXT NOT NULL CHECK (decision_status IN (
        'OBSERVE','PROVISIONAL','WINNER','INCONCLUSIVE','DATA_INCOMPLETE'
    )),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    evaluated_at TEXT NOT NULL,
    UNIQUE (launch_id, checkpoint)
);

CREATE TABLE IF NOT EXISTS ad_audience_preflight (
    preflight_id TEXT PRIMARY KEY,
    launch_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    business_id TEXT NOT NULL,
    country TEXT NOT NULL,
    strategy_keys_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    evidence_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('VERIFIED','EXPIRED','INVALID')),
    checked_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ad_audience_generation (
    generation_id TEXT PRIMARY KEY,
    launch_id TEXT NOT NULL,
    parent_generation_id TEXT NOT NULL DEFAULT '',
    source_pair_evaluation_id TEXT NOT NULL,
    winning_strategy_key TEXT NOT NULL,
    candidate_strategy_keys_json TEXT NOT NULL DEFAULT '[]',
    keyword_mutations_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PROPOSED'
        CHECK (status IN ('PROPOSED','APPROVED','ACTIVE','REJECTED','RETIRED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (launch_id, source_pair_evaluation_id)
);

CREATE TABLE IF NOT EXISTS ad_creative_group_evaluation (
    group_evaluation_id TEXT PRIMARY KEY,
    launch_id TEXT NOT NULL,
    checkpoint TEXT NOT NULL CHECK (checkpoint IN ('D1','D3','D7')),
    window_json TEXT NOT NULL DEFAULT '{}',
    metrics_by_experiment_json TEXT NOT NULL DEFAULT '{}',
    ranking_json TEXT NOT NULL DEFAULT '[]',
    winner_experiment_id TEXT NOT NULL DEFAULT '',
    decision_status TEXT NOT NULL CHECK (decision_status IN (
        'OBSERVE','PROVISIONAL','WINNER','TIE','INCONCLUSIVE','DATA_INCOMPLETE'
    )),
    actual_days INTEGER NOT NULL DEFAULT 0,
    data_quality_status TEXT NOT NULL DEFAULT 'PASS',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    evaluated_at TEXT NOT NULL,
    UNIQUE (launch_id, checkpoint)
);

CREATE TABLE IF NOT EXISTS ad_creative_group_evaluation_history (
    history_id TEXT PRIMARY KEY,
    group_evaluation_id TEXT NOT NULL,
    launch_id TEXT NOT NULL,
    checkpoint TEXT NOT NULL,
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    archived_reason TEXT NOT NULL,
    archived_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ad_creative_generation (
    generation_id TEXT PRIMARY KEY,
    launch_id TEXT NOT NULL,
    parent_generation_id TEXT NOT NULL DEFAULT '',
    source_group_evaluation_id TEXT NOT NULL,
    winning_experiment_id TEXT NOT NULL,
    winning_direction_key TEXT NOT NULL,
    prompt_lineage_json TEXT NOT NULL DEFAULT '{}',
    variant_proposals_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PROPOSED'
        CHECK (status IN ('PROPOSED','APPROVED','GENERATING','REVIEW_READY','REJECTED','RETIRED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (launch_id, source_group_evaluation_id)
);

CREATE TABLE IF NOT EXISTS ad_creative_reference_knowledge (
    reference_id TEXT PRIMARY KEY,
    ad_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL DEFAULT '',
    adset_id TEXT NOT NULL DEFAULT '',
    creative_id TEXT NOT NULL DEFAULT '',
    direction_key TEXT NOT NULL DEFAULT '',
    direction_source TEXT NOT NULL DEFAULT 'unmapped',
    source_origin TEXT NOT NULL,
    access_status TEXT NOT NULL,
    original_prompt_available INTEGER NOT NULL DEFAULT 0,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS growth_simulation (
    simulation_id TEXT PRIMARY KEY,
    context_snapshot_id TEXT NOT NULL,
    proposed_action TEXT NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
    expected_success_rate REAL NOT NULL CHECK (expected_success_rate >= 0 AND expected_success_rate <= 1),
    risk_level TEXT NOT NULL CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH', 'INSUFFICIENT_DATA')),
    evidence_episode_ids_json TEXT NOT NULL DEFAULT '[]',
    assumptions_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (context_snapshot_id) REFERENCES growth_context_snapshot(context_snapshot_id)
);

CREATE TABLE IF NOT EXISTS growth_idempotency_record (
    route_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (route_key, idempotency_key)
);

CREATE TABLE IF NOT EXISTS growth_execution_resource_claim (
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    operation_action_id TEXT NOT NULL UNIQUE,
    execution_task_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (resource_type, resource_id),
    FOREIGN KEY (operation_action_id) REFERENCES growth_operation_action(operation_action_id),
    FOREIGN KEY (execution_task_id) REFERENCES meta_execution_task(execution_task_id)
);

CREATE TABLE IF NOT EXISTS growth_state_transition (
    transition_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ad_new_account_launch_archive (
    launch_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'ARCHIVED',
    archived_at TEXT NOT NULL DEFAULT '',
    archived_by TEXT NOT NULL DEFAULT '',
    restored_at TEXT NOT NULL DEFAULT '',
    restored_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ad_new_account_launch_purge_audit (
    purge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    launch_fingerprint TEXT NOT NULL,
    purged_at TEXT NOT NULL,
    purged_by TEXT NOT NULL DEFAULT '',
    purge_reason TEXT NOT NULL DEFAULT '',
    deleted_counts_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_growth_context_created ON growth_context_snapshot(created_at);
CREATE INDEX IF NOT EXISTS idx_experiment_context_experiment ON experiment_context_snapshots(experiment_id, created_at);
CREATE INDEX IF NOT EXISTS idx_growth_decision_recommendation ON growth_decision(recommendation_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_growth_decision_active_recommendation
    ON growth_decision(recommendation_id) WHERE status IN ('CREATED', 'BOUND');
CREATE INDEX IF NOT EXISTS idx_growth_episode_status ON growth_decision_episode(status, created_at);
CREATE INDEX IF NOT EXISTS idx_growth_episode_experiment ON growth_decision_episode(experiment_id, created_at);
CREATE INDEX IF NOT EXISTS idx_growth_operation_status ON growth_operation_action(status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_meta_execution_action_unique ON meta_execution_task(operation_action_id);
CREATE INDEX IF NOT EXISTS idx_meta_execution_claim ON meta_execution_task(status, heartbeat_at, created_at);
CREATE INDEX IF NOT EXISTS idx_meta_receipt_task ON meta_execution_task_receipt(execution_task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_growth_operation_approval_status
    ON growth_operation_approval(status, created_at);
CREATE INDEX IF NOT EXISTS idx_ad_experiment_state ON ad_experiment(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_ad_experiment_source ON ad_experiment(source_recommendation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ad_experiment_event_timeline ON ad_experiment_events(experiment_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ad_experiment_cycle_due
    ON ad_experiment_cycle(state, first_complete_date, created_at);
CREATE INDEX IF NOT EXISTS idx_ad_experiment_cycle_experiment
    ON ad_experiment_cycle(experiment_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ad_experiment_cycle_evaluation
    ON ad_experiment_cycle_evaluation(cycle_id, checkpoint, evaluated_at);
CREATE INDEX IF NOT EXISTS idx_ad_experiment_cycle_plan
    ON ad_experiment_cycle_next_plan(cycle_id, status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ad_experiment_cycle_plan_operation
    ON ad_experiment_cycle_next_plan(operation_action_id)
    WHERE operation_action_id<>'';
CREATE INDEX IF NOT EXISTS idx_ad_creative_revision_ad
    ON ad_creative_revision_window(ad_id, effective_from);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ad_creative_revision_current
    ON ad_creative_revision_window(ad_id) WHERE status='CURRENT';
CREATE INDEX IF NOT EXISTS idx_growth_knowledge_status ON growth_strategy_knowledge(status, created_at);
CREATE INDEX IF NOT EXISTS idx_growth_strategy_recommendation_context
    ON growth_strategy_recommendation(context_snapshot_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_growth_next_action_account
    ON growth_next_action(account_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_growth_simulation_context
    ON growth_simulation(context_snapshot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audience_pair_launch
    ON ad_audience_pair_evaluation(launch_id, evaluated_at);
CREATE INDEX IF NOT EXISTS idx_audience_preflight_launch
    ON ad_audience_preflight(launch_id, checked_at);
CREATE INDEX IF NOT EXISTS idx_audience_generation_status
    ON ad_audience_generation(status, created_at);
CREATE INDEX IF NOT EXISTS idx_creative_group_launch
    ON ad_creative_group_evaluation(launch_id, evaluated_at);
CREATE INDEX IF NOT EXISTS idx_creative_group_history_launch
    ON ad_creative_group_evaluation_history(launch_id, archived_at);
CREATE INDEX IF NOT EXISTS idx_creative_generation_status
    ON ad_creative_generation(status, created_at);
CREATE INDEX IF NOT EXISTS idx_ad_creative_reference_access
    ON ad_creative_reference_knowledge(access_status, account_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_growth_transition_entity ON growth_state_transition(entity_type, entity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_new_account_launch_archive_due
    ON ad_new_account_launch_archive(status, archived_at);

CREATE TRIGGER IF NOT EXISTS trg_growth_context_snapshot_no_update
BEFORE UPDATE ON growth_context_snapshot
BEGIN
    SELECT RAISE(ABORT, 'growth_context_snapshot_immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_growth_context_snapshot_no_delete
BEFORE DELETE ON growth_context_snapshot
BEGIN
    SELECT RAISE(ABORT, 'growth_context_snapshot_immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_growth_episode_completion_requires_evidence_insert
BEFORE INSERT ON growth_decision_episode
WHEN NEW.status = 'COMPLETED'
 AND (NEW.outcome_json = '{}' OR NEW.lesson_json = '{}')
BEGIN
    SELECT RAISE(ABORT, 'episode_completion_requires_outcome_and_lesson');
END;

CREATE TRIGGER IF NOT EXISTS trg_growth_episode_completion_requires_evidence_update
BEFORE UPDATE OF status ON growth_decision_episode
WHEN NEW.status = 'COMPLETED'
 AND (NEW.outcome_json = '{}' OR NEW.lesson_json = '{}')
BEGIN
    SELECT RAISE(ABORT, 'episode_completion_requires_outcome_and_lesson');
END;

CREATE TRIGGER IF NOT EXISTS trg_growth_episode_legal_transition
BEFORE UPDATE OF status ON growth_decision_episode
WHEN NEW.status <> OLD.status AND NOT (
    (OLD.status='CREATED' AND NEW.status='ACTION_EXECUTING') OR
    (OLD.status='ACTION_EXECUTING' AND NEW.status='WAITING_OUTCOME') OR
    (OLD.status='WAITING_OUTCOME' AND NEW.status='OUTCOME_READY') OR
    (OLD.status='OUTCOME_READY' AND NEW.status='LESSON_REVIEW') OR
    (OLD.status='LESSON_REVIEW' AND NEW.status='COMPLETED')
)
BEGIN
    SELECT RAISE(ABORT, 'illegal_growth_episode_transition');
END;

CREATE TRIGGER IF NOT EXISTS trg_growth_knowledge_legal_transition
BEFORE UPDATE OF status ON growth_strategy_knowledge
WHEN NEW.status <> OLD.status AND NOT (
    (OLD.status='RAW' AND NEW.status IN ('REVIEWED','ARCHIVED')) OR
    (OLD.status='REVIEWED' AND NEW.status IN ('ACTIVE','ARCHIVED')) OR
    (OLD.status='ACTIVE' AND NEW.status='ARCHIVED')
)
BEGIN
    SELECT RAISE(ABORT, 'illegal_growth_knowledge_transition');
END;

CREATE TRIGGER IF NOT EXISTS trg_meta_execution_task_legal_transition
BEFORE UPDATE OF status ON meta_execution_task
WHEN NEW.status <> OLD.status AND NOT (
    (OLD.status='QUEUED' AND NEW.status IN ('RUNNING','MANUAL_REVIEW')) OR
    (OLD.status='RUNNING' AND NEW.status IN ('VERIFYING','MANUAL_REVIEW')) OR
    (OLD.status='VERIFYING' AND NEW.status IN ('QUEUED','SUCCESS','MANUAL_REVIEW')) OR
    (OLD.status='MANUAL_REVIEW' AND NEW.status='VERIFYING')
)
BEGIN
    SELECT RAISE(ABORT, 'illegal_meta_execution_task_transition');
END;
"""


GROWTH_SCHEMA_DOWN_SQL = """
DROP TRIGGER IF EXISTS trg_strategy_knowledge_entries_no_insert;
DROP TRIGGER IF EXISTS trg_strategy_knowledge_entries_no_update;
DROP TRIGGER IF EXISTS trg_strategy_knowledge_entries_no_delete;
DROP TRIGGER IF EXISTS trg_meta_execution_task_legal_transition;
DROP TRIGGER IF EXISTS trg_growth_knowledge_legal_transition;
DROP TRIGGER IF EXISTS trg_growth_episode_legal_transition;
DROP TRIGGER IF EXISTS trg_growth_episode_completion_requires_evidence_update;
DROP TRIGGER IF EXISTS trg_growth_episode_completion_requires_evidence_insert;
DROP TRIGGER IF EXISTS trg_growth_context_snapshot_no_delete;
DROP TRIGGER IF EXISTS trg_growth_context_snapshot_no_update;
DROP TABLE IF EXISTS ad_new_account_launch_purge_audit;
DROP TABLE IF EXISTS ad_new_account_launch_archive;
DROP TABLE IF EXISTS growth_state_transition;
DROP TABLE IF EXISTS growth_idempotency_record;
DROP TABLE IF EXISTS growth_execution_resource_claim;
DROP TABLE IF EXISTS growth_simulation;
DROP TABLE IF EXISTS ad_creative_generation;
DROP TABLE IF EXISTS ad_creative_group_evaluation_history;
DROP TABLE IF EXISTS ad_creative_group_evaluation;
DROP TABLE IF EXISTS ad_audience_generation;
DROP TABLE IF EXISTS ad_audience_preflight;
DROP TABLE IF EXISTS ad_audience_pair_evaluation;
DROP TABLE IF EXISTS growth_strategy_recommendation;
DROP TABLE IF EXISTS growth_next_action;
DROP TABLE IF EXISTS growth_autonomy_policy;
DROP TABLE IF EXISTS growth_strategy_knowledge;
DROP TABLE IF EXISTS ad_experiment_cycle_next_plan;
DROP TABLE IF EXISTS ad_experiment_cycle_evaluation;
DROP TABLE IF EXISTS ad_experiment_cycle;
DROP TABLE IF EXISTS ad_experiment_evaluation;
DROP TABLE IF EXISTS ad_creative_revision_window;
DROP TABLE IF EXISTS ad_experiment_events;
DROP TABLE IF EXISTS ad_experiment;
DROP TABLE IF EXISTS growth_operation_approval;
DROP TABLE IF EXISTS meta_execution_task_receipt;
DROP TABLE IF EXISTS meta_execution_task;
DROP TABLE IF EXISTS growth_operation_action;
DROP TABLE IF EXISTS growth_decision_episode;
DROP TABLE IF EXISTS growth_decision;
DROP TABLE IF EXISTS experiment_context_snapshots;
DROP TABLE IF EXISTS growth_context_snapshot;
"""


def ensure_growth_schema(conn: sqlite3.Connection) -> None:
    # Recreate this trigger so existing databases gain bounded GET-only
    # reconciliation without requiring a table migration.
    for attempt in range(3):
        try:
            conn.execute("DROP TRIGGER IF EXISTS trg_meta_execution_task_legal_transition")
            conn.executescript(GROWTH_SCHEMA_SQL)
            break
        except sqlite3.OperationalError as exc:
            if "database schema has changed" not in str(exc).lower() or attempt == 2:
                raise
            conn.rollback()
            time.sleep(0.05 * (attempt + 1))
    approval_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(growth_operation_approval)").fetchall()
    }
    if "expires_at" not in approval_columns:
        conn.execute("ALTER TABLE growth_operation_approval ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''")
    if "consumed_at" not in approval_columns:
        conn.execute("ALTER TABLE growth_operation_approval ADD COLUMN consumed_at TEXT NOT NULL DEFAULT ''")
    adoption_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='creative_adoption_records'"
    ).fetchone()
    revision_count = conn.execute("SELECT COUNT(*) FROM ad_creative_revision_window").fetchone()[0]
    if adoption_table and not revision_count:
        rows = conn.execute(
            """
            SELECT adoption_id,experiment_id,image_id,ad_id,creative_id,adopted_at
            FROM creative_adoption_records
            WHERE ad_id<>'' AND creative_id<>''
              AND binding_status='confirmed'
              AND status IN ('USED_IN_AD','PENDING_CLEANUP')
            ORDER BY ad_id,adopted_at,adoption_id
            """
        ).fetchall()
        by_ad: dict[str, list[tuple[object, ...]]] = {}
        for row in rows:
            bucket = by_ad.setdefault(str(row[3]), [])
            if bucket and str(bucket[-1][4]) == str(row[4]):
                continue
            bucket.append(row)
        for ad_id, revisions in by_ad.items():
            for index, row in enumerate(revisions):
                next_at = str(revisions[index + 1][5] or "") if index + 1 < len(revisions) else ""
                adopted_at = str(row[5] or "")
                adoption_id = str(row[0] or "")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ad_creative_revision_window
                    (revision_id,experiment_id,ad_id,creative_id,image_id,adoption_id,
                     effective_from,effective_to,replacement_boundary_date,status,source,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"crv_{adoption_id}", str(row[1] or ""), ad_id,
                        str(row[4] or ""), str(row[2] or ""), adoption_id,
                        adopted_at, next_at, adopted_at[:10] if index else "",
                        "HISTORICAL" if next_at else "CURRENT", "HISTORY_COMPAT",
                        adopted_at, adopted_at,
                    ),
                )
    conn.execute(
        """INSERT OR IGNORE INTO growth_execution_resource_claim
        (resource_type,resource_id,operation_action_id,execution_task_id,created_at)
        SELECT 'NEW_ACCOUNT_LAUNCH',json_extract(a.payload_json,'$.launch_id'),
               a.operation_action_id,t.execution_task_id,datetime('now')
        FROM growth_operation_action a
        JOIN meta_execution_task t ON t.operation_action_id=a.operation_action_id
        WHERE a.action_type='CREATE_PAUSED_AD' AND t.status='SUCCESS'
          AND COALESCE(json_extract(a.payload_json,'$.launch_id'),'')<>''
          AND COALESCE(json_extract(t.meta_object_ids_json,'$.campaign_id'),'')<>''
          AND EXISTS (
              SELECT 1 FROM ad_experiment e
              WHERE e.source_report_id=json_extract(a.payload_json,'$.launch_id')
                AND e.source_campaign_id=json_extract(t.meta_object_ids_json,'$.campaign_id')
          )
        ORDER BY t.updated_at DESC,t.execution_task_id DESC"""
    )
    conn.commit()
