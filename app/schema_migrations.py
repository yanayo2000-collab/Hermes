from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from app.growth.schema import ensure_growth_schema
from app.newcomer_publication import NEWCOMER_SCHEMA_INDEXES, NEWCOMER_SCHEMA_TABLES


TIMO_ACTIVE_RECOVERY_INDEX_NAME = 'idx_timo_recovery_runs_active_guild'
TIMO_ACTIVE_RECOVERY_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_timo_recovery_runs_active_guild "
    "ON timo_recovery_runs (guild_id) WHERE status IN "
    "('created','chrome_profile_checking','otp_required','otp_request_queued',"
    "'otp_request_created','otp_reading','otp_received','otp_submitting',"
    "'ticket_extracting','guild_verifying','precheck_device_ready',"
    "'station_observation_ready','pre_request_snapshot','timo_send_requested',"
    "'timo_send_accepted','delivery_waiting','evidence_collecting',"
    "'page_refreshing','otp_candidate_found','otp_validated','otp_consuming',"
    "'otp_l4_consumed','ticket_verifying','browser_submit_started',"
    "'browser_submit_accepted','ticket_candidate_collection_started',"
    "'ticket_candidate_captured','ticket_probe_passed','ticket_persisted',"
    "'post_persist_probe_passed')"
)


SCHEMA_MIGRATIONS: dict[str, dict[str, tuple[str, ...]]] = {
    'newcomer_daily_publication_v1': {
        'tables': NEWCOMER_SCHEMA_TABLES,
        'indexes': NEWCOMER_SCHEMA_INDEXES,
    },
    'guild_country_contract_v1': {
        'alter': (
            "ALTER TABLE guild_executors ADD COLUMN guild_country TEXT",
            "ALTER TABLE guild_executors ADD COLUMN eligible_user_countries TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE guild_executors ADD COLUMN routing_region TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE leads ADD COLUMN assigned_guild_country TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE leads ADD COLUMN cross_country_fallback INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE leads ADD COLUMN cross_country_fallback_reason TEXT NOT NULL DEFAULT ''",
        ),
    },
    'creative_image_generation_v1': {
        'tables': (
            """
            CREATE TABLE IF NOT EXISTS creative_generation_requests (
                request_id TEXT PRIMARY KEY,
                surface TEXT NOT NULL,
                image_size TEXT NOT NULL,
                market TEXT NOT NULL,
                brand TEXT NOT NULL,
                country TEXT NOT NULL DEFAULT '',
                project TEXT NOT NULL DEFAULT '',
                campaign TEXT NOT NULL DEFAULT '',
                ad_group TEXT NOT NULL DEFAULT '',
                ad TEXT NOT NULL DEFAULT '',
                objective TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL,
                negative_prompt TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                risk_status TEXT NOT NULL,
                risk_tags_json TEXT NOT NULL,
                review_status TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_by TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS creative_generated_images (
                image_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                surface TEXT NOT NULL,
                image_size TEXT NOT NULL,
                market TEXT NOT NULL,
                brand TEXT NOT NULL,
                image_ref TEXT NOT NULL,
                thumbnail_ref TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                risk_status TEXT NOT NULL,
                risk_tags_json TEXT NOT NULL,
                review_status TEXT NOT NULL,
                provider TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                image_hash TEXT NOT NULL DEFAULT '',
                perceptual_hash TEXT NOT NULL DEFAULT '',
                final_delivery_hash TEXT NOT NULL DEFAULT '',
                source_provider TEXT NOT NULL DEFAULT '',
                uploaded_manually INTEGER NOT NULL DEFAULT 0,
                uploaded_final_version INTEGER NOT NULL DEFAULT 0,
                is_exact_generated_asset INTEGER NOT NULL DEFAULT 1
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS creative_generated_image_links (
                link_id TEXT PRIMARY KEY,
                image_id TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT '',
                campaign TEXT NOT NULL DEFAULT '',
                ad_group TEXT NOT NULL DEFAULT '',
                ad TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS creative_review_records (
                review_id TEXT PRIMARY KEY,
                image_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                review_status TEXT NOT NULL,
                review_status_zh TEXT NOT NULL,
                reviewer TEXT NOT NULL DEFAULT '',
                checks_json TEXT NOT NULL,
                decision_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS creative_adoption_records (
                adoption_id TEXT PRIMARY KEY,
                image_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                ad_id TEXT NOT NULL DEFAULT '',
                creative_id TEXT NOT NULL DEFAULT '',
                adset_id TEXT NOT NULL DEFAULT '',
                campaign_id TEXT NOT NULL DEFAULT '',
                adopted_by TEXT NOT NULL DEFAULT '',
                adopted_at TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                experiment_id TEXT NOT NULL DEFAULT '',
                experiment_code TEXT NOT NULL DEFAULT '',
                suggestion_id TEXT NOT NULL DEFAULT '',
                generation_request_id TEXT NOT NULL DEFAULT '',
                generated_image_id TEXT NOT NULL DEFAULT '',
                source_ad_id TEXT NOT NULL DEFAULT '',
                source_creative_id TEXT NOT NULL DEFAULT '',
                adopted_ad_id TEXT NOT NULL DEFAULT '',
                adopted_creative_id TEXT NOT NULL DEFAULT '',
                adopted_adset_id TEXT NOT NULL DEFAULT '',
                adopted_campaign_id TEXT NOT NULL DEFAULT '',
                adoption_type TEXT NOT NULL DEFAULT '',
                binding_method TEXT NOT NULL DEFAULT '',
                binding_confidence TEXT NOT NULL DEFAULT '',
                binding_status TEXT NOT NULL DEFAULT '',
                matched_at TEXT NOT NULL DEFAULT '',
                confirmed_by TEXT NOT NULL DEFAULT '',
                confirmed_at TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                notes TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS creative_experiment_suggestions (
                experiment_id TEXT PRIMARY KEY,
                experiment_code TEXT NOT NULL,
                suggestion_id TEXT NOT NULL DEFAULT '',
                generated_image_id TEXT NOT NULL DEFAULT '',
                generation_request_id TEXT NOT NULL DEFAULT '',
                experiment_mode TEXT NOT NULL,
                source_ad_id TEXT NOT NULL DEFAULT '',
                source_creative_id TEXT NOT NULL DEFAULT '',
                source_campaign_id TEXT NOT NULL DEFAULT '',
                source_adset_id TEXT NOT NULL DEFAULT '',
                recommended_binding_method TEXT NOT NULL DEFAULT '',
                binding_instruction_cn TEXT NOT NULL DEFAULT '',
                requires_manual_upload INTEGER NOT NULL DEFAULT 0,
                requires_experiment_code_in_ad_name INTEGER NOT NULL DEFAULT 0,
                binding_status TEXT NOT NULL DEFAULT 'pending',
                status TEXT NOT NULL DEFAULT 'approved_for_generation',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS creative_generation_review_results (
                review_result_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL DEFAULT '',
                generated_image_id TEXT NOT NULL DEFAULT '',
                review_status TEXT NOT NULL,
                decision_reason TEXT NOT NULL DEFAULT '',
                reviewer TEXT NOT NULL DEFAULT '',
                safe_to_generate INTEGER NOT NULL DEFAULT 0,
                safe_to_use_in_ad INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS creative_pro_work_queue (
                job_id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL DEFAULT 'generation',
                provider_mode TEXT NOT NULL DEFAULT 'chatgpt_pro_manual',
                status TEXT NOT NULL DEFAULT 'pending',
                country TEXT NOT NULL DEFAULT '',
                project TEXT NOT NULL DEFAULT '',
                brand_display_name TEXT NOT NULL DEFAULT '',
                experiment_type TEXT NOT NULL DEFAULT '',
                experiment_id TEXT NOT NULL DEFAULT '',
                experiment_code TEXT NOT NULL DEFAULT '',
                source_ad_ids_json TEXT NOT NULL DEFAULT '[]',
                source_creative_ids_json TEXT NOT NULL DEFAULT '[]',
                source_asset_ids_json TEXT NOT NULL DEFAULT '[]',
                creative_diagnosis_id TEXT NOT NULL DEFAULT '',
                recommendation_id TEXT NOT NULL DEFAULT '',
                metrics_snapshot_json TEXT NOT NULL DEFAULT '{}',
                rules_json TEXT NOT NULL DEFAULT '{}',
                material_refs_json TEXT NOT NULL DEFAULT '{}',
                signed_thumbnail_urls_json TEXT NOT NULL DEFAULT '[]',
                analysis_json TEXT NOT NULL DEFAULT '{}',
                generation_plan_json TEXT NOT NULL DEFAULT '{}',
                created_by TEXT NOT NULL DEFAULT '',
                claimed_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                claimed_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT ''
            )
            """,
        ),
        'alter': (
            "ALTER TABLE creative_generated_images ADD COLUMN image_hash TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_generated_images ADD COLUMN perceptual_hash TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_generated_images ADD COLUMN final_delivery_hash TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_generated_images ADD COLUMN source_provider TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_generated_images ADD COLUMN uploaded_manually INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE creative_generated_images ADD COLUMN uploaded_final_version INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE creative_generated_images ADD COLUMN is_exact_generated_asset INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE creative_adoption_records ADD COLUMN experiment_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN experiment_code TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN suggestion_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN generation_request_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN generated_image_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN source_ad_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN source_creative_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN adopted_ad_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN adopted_creative_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN adopted_adset_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN adopted_campaign_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN adoption_type TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN binding_method TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN binding_confidence TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN binding_status TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN matched_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN confirmed_by TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN confirmed_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_adoption_records ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE creative_adoption_records ADD COLUMN notes TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_pro_work_queue ADD COLUMN analysis_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE creative_pro_work_queue ADD COLUMN generation_plan_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE creative_pro_work_queue ADD COLUMN error_code TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE creative_pro_work_queue ADD COLUMN error_message TEXT NOT NULL DEFAULT ''",
        ),
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_creative_generation_requests_market ON creative_generation_requests(market, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_creative_generated_images_request ON creative_generated_images(request_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_creative_experiment_suggestions_code ON creative_experiment_suggestions(experiment_code, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_creative_adoption_records_experiment ON creative_adoption_records(experiment_id, matched_at)",
            "CREATE INDEX IF NOT EXISTS idx_creative_pro_work_queue_status ON creative_pro_work_queue(status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_creative_pro_work_queue_experiment ON creative_pro_work_queue(experiment_id, created_at)",
        ),
    },
    'ad_dashboard_local_fact_store': {
        'tables': (
            """
            CREATE TABLE IF NOT EXISTS ad_dashboard_fact_rows (
                row_id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                data_source TEXT NOT NULL,
                platform TEXT NOT NULL,
                app_id TEXT NOT NULL DEFAULT '',
                appsflyer_app_id TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                media_source TEXT NOT NULL DEFAULT '',
                campaign TEXT NOT NULL DEFAULT '',
                ad_group TEXT NOT NULL DEFAULT '',
                ad TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT '',
                row_count INTEGER NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0,
                installs REAL NOT NULL DEFAULT 0,
                af_installs REAL NOT NULL DEFAULT 0,
                registrations REAL NOT NULL DEFAULT 0,
                meta_installs REAL NOT NULL DEFAULT 0,
                meta_registrations REAL NOT NULL DEFAULT 0,
                af_registrations REAL NOT NULL DEFAULT 0,
                onsite_registrations REAL NOT NULL DEFAULT 0,
                high_value_users REAL NOT NULL DEFAULT 0,
                im_entries REAL NOT NULL DEFAULT 0,
                im_first_replies REAL NOT NULL DEFAULT 0,
                im_step2_triggers REAL NOT NULL DEFAULT 0,
                im_manual_reply_3 REAL NOT NULL DEFAULT 0,
                im_link_clicks REAL NOT NULL DEFAULT 0,
                guild_joins REAL NOT NULL DEFAULT 0,
                promotion_guild_joins REAL NOT NULL DEFAULT 0,
                organic_guild_joins REAL NOT NULL DEFAULT 0,
                tugao_join_success_users REAL NOT NULL DEFAULT 0,
                tugao_join_success_no_wa_users REAL NOT NULL DEFAULT 0,
                meta_guild_joins REAL NOT NULL DEFAULT 0,
                af_guild_joins REAL NOT NULL DEFAULT 0,
                purchases REAL NOT NULL DEFAULT 0,
                revenue REAL NOT NULL DEFAULT 0,
                clicks REAL NOT NULL DEFAULT 0,
                link_clicks REAL NOT NULL DEFAULT 0,
                impressions REAL NOT NULL DEFAULT 0,
                reach REAL NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ad_dashboard_sync_state (
                source TEXT NOT NULL,
                date TEXT NOT NULL,
                status TEXT NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, date)
            )
            """,
        ),
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_ad_dashboard_fact_date_platform ON ad_dashboard_fact_rows(date, platform)",
            "CREATE INDEX IF NOT EXISTS idx_ad_dashboard_fact_dims ON ad_dashboard_fact_rows(platform, country, app_id, campaign, ad_group, ad)",
            "CREATE INDEX IF NOT EXISTS idx_ad_dashboard_sync_date ON ad_dashboard_sync_state(date, status)",
        ),
    },
    'ad_dashboard_tugao_qualified_join_v1': {
        'alter': (
            "ALTER TABLE ad_dashboard_fact_rows ADD COLUMN tugao_join_success_users REAL NOT NULL DEFAULT 0",
            "ALTER TABLE ad_dashboard_fact_rows ADD COLUMN tugao_join_success_no_wa_users REAL NOT NULL DEFAULT 0",
        ),
    },
    'whatsapp_approval_accounts_runtime': {
        'alter': (
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN area TEXT",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN notify_profile_name TEXT",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN approval_rule TEXT NOT NULL DEFAULT 'count_30'",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN approval_count_threshold INTEGER NOT NULL DEFAULT 30",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN approval_timeout_minutes INTEGER NOT NULL DEFAULT 30",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN auto_recover_worker INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'pending_verification'",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN notes TEXT",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN assigned_customer_service_user_id TEXT",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN assigned_customer_service_username TEXT",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN assigned_customer_service_display_name TEXT",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN created_at TEXT",
        ),
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_whatsapp_approval_accounts_type_updated ON whatsapp_approval_accounts(responsible_type, enabled, updated_at)",
        ),
    },
    'wa_runtime_indexes': {
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_wa_accounts_type_updated ON wa_accounts(responsible_type, provider_mode, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_wa_group_bindings_account_type_updated ON wa_group_bindings(account_key, responsible_type, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_wa_group_bindings_registration_group ON wa_group_bindings(registration_group, provider_mode)",
            "CREATE INDEX IF NOT EXISTS idx_wa_truth_snapshots_binding_type_checked ON wa_truth_snapshots(binding_id, snapshot_type, checked_at)",
            "CREATE INDEX IF NOT EXISTS idx_wa_runtime_actions_account_type_created ON wa_runtime_actions(account_key, action_type, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_wa_identity_map_provider_requester ON wa_identity_map(provider_name, provider_requester_id)",
        ),
    },
    'mcn_truth_snapshot_indexes': {
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_mcn_truth_snapshots_lookup ON mcn_truth_snapshots(object_type, object_key, snapshot_type)",
            "CREATE INDEX IF NOT EXISTS idx_mcn_truth_snapshots_expiry ON mcn_truth_snapshots(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_mcn_truth_snapshots_status_checked ON mcn_truth_snapshots(snapshot_type, truth_status, checked_at)",
        ),
    },
    'truth_acquisition_log_retention_indexes': {
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_truth_acquisition_logs_created_at ON truth_acquisition_logs(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_truth_acquisition_logs_updated_at ON truth_acquisition_logs(updated_at)",
        ),
    },
    'mcn_operation_tasks_runtime': {
        'alter': (
            "ALTER TABLE mcn_operation_tasks ADD COLUMN available_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE mcn_operation_tasks ADD COLUMN lease_owner TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE mcn_operation_tasks ADD COLUMN lease_until TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE mcn_operation_tasks ADD COLUMN timeout_seconds INTEGER NOT NULL DEFAULT 60",
        ),
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_mcn_operation_tasks_status ON mcn_operation_tasks(status, priority, created_at)",
        ),
    },
    'ingress_jobs_leases': {
        'alter': (
            "ALTER TABLE ingress_jobs ADD COLUMN worker_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE ingress_jobs ADD COLUMN lease_until TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE ingress_jobs ADD COLUMN heartbeat_at TEXT NOT NULL DEFAULT ''",
        ),
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_ingress_jobs_status_lease_until ON ingress_jobs (status, lease_until)",
        ),
    },
    'group_atmosphere_scheduler_leases': {
        'alter': (
            "ALTER TABLE whatsapp_group_atmosphere_role_bindings ADD COLUMN scheduler_lease_owner TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE whatsapp_group_atmosphere_role_bindings ADD COLUMN scheduler_lease_until TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE whatsapp_group_atmosphere_configs ADD COLUMN scheduler_lease_owner TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE whatsapp_group_atmosphere_configs ADD COLUMN scheduler_lease_until TEXT NOT NULL DEFAULT ''",
        ),
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_group_atmosphere_role_bindings_scheduler_lease ON whatsapp_group_atmosphere_role_bindings(status, scheduler_lease_until)",
            "CREATE INDEX IF NOT EXISTS idx_group_atmosphere_configs_scheduler_lease ON whatsapp_group_atmosphere_configs(status, scheduler_lease_until)",
        ),
    },
    'group_atmosphere_candidates': {
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_group_atmosphere_candidates_config_role ON whatsapp_group_atmosphere_candidates(config_name, role_positioning, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_group_atmosphere_candidates_language_role ON whatsapp_group_atmosphere_candidates(language, role_positioning, updated_at)",
        ),
    },
    'registration_group_batch_member_name_sources': {
        'alter': (
            "ALTER TABLE registration_group_approval_batch_members ADD COLUMN display_name_source TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE registration_group_approval_batch_members ADD COLUMN display_name_enhanced_at TEXT",
        ),
    },
    'registration_group_batch_member_eligibility_snapshots': {
        'alter': (
            "ALTER TABLE registration_group_approval_batch_members ADD COLUMN lead_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE registration_group_approval_batch_members ADD COLUMN matched_customer_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE registration_group_approval_batch_members ADD COLUMN registration_status_snapshot TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE registration_group_approval_batch_members ADD COLUMN registration_status_label_snapshot TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE registration_group_approval_batch_members ADD COLUMN eligibility_source TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE registration_group_approval_batch_members ADD COLUMN eligibility_snapshot TEXT NOT NULL DEFAULT ''",
        ),
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_rgm_lead ON registration_group_approval_batch_members (lead_id, approved_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_rgm_customer ON registration_group_approval_batch_members (matched_customer_id, approved_at DESC)",
        ),
    },
    'guild_anchor_daily_stats_indexes': {
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_guild_anchor_seen_bj_date_stats ON guild_anchor_seen (created_date_bj, guild_name, is_real_person, last_seen_at)",
        ),
    },
    'timo_auth_station_v1': {
        'tables': (
            """
            CREATE TABLE IF NOT EXISTS timo_session_state (
                guild_id TEXT PRIMARY KEY,
                account_fingerprint TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT '',
                ticket_fingerprint TEXT NOT NULL DEFAULT '',
                ticket_status TEXT NOT NULL DEFAULT '',
                last_verified_at TEXT NOT NULL DEFAULT '',
                last_refresh_attempt_at TEXT NOT NULL DEFAULT '',
                last_refresh_success_at TEXT NOT NULL DEFAULT '',
                recovery_status TEXT NOT NULL DEFAULT '',
                recovery_run_id TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_ticket_versions (
                version_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                account_fingerprint TEXT NOT NULL DEFAULT '',
                ticket_fingerprint TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                verified_result TEXT NOT NULL DEFAULT '',
                activated_at TEXT NOT NULL DEFAULT '',
                retired_at TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_recovery_runs (
                recovery_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                account_fingerprint TEXT NOT NULL DEFAULT '',
                trigger_reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                chrome_profile_result TEXT NOT NULL DEFAULT '',
                otp_required INTEGER NOT NULL DEFAULT 0,
                otp_source TEXT NOT NULL DEFAULT '',
                otp_requested_at TEXT NOT NULL DEFAULT '',
                otp_received_at TEXT NOT NULL DEFAULT '',
                otp_submitted_at TEXT NOT NULL DEFAULT '',
                ticket_fingerprint_before TEXT NOT NULL DEFAULT '',
                ticket_fingerprint_after TEXT NOT NULL DEFAULT '',
                guild_verify_result TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_otp_requests (
                otp_request_id TEXT PRIMARY KEY,
                recovery_id TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                account_fingerprint TEXT NOT NULL DEFAULT '',
                station_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                request_channel TEXT NOT NULL DEFAULT '',
                otp_fingerprint TEXT NOT NULL DEFAULT '',
                otp_code_fingerprint TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL,
                received_at TEXT NOT NULL DEFAULT '',
                used_at TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_auth_stations (
                station_id TEXT PRIMARY KEY,
                station_name TEXT NOT NULL DEFAULT '',
                account_fingerprint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                last_heartbeat_at TEXT NOT NULL DEFAULT '',
                device_id TEXT NOT NULL DEFAULT '',
                device_status TEXT NOT NULL DEFAULT '',
                adb_status TEXT NOT NULL DEFAULT '',
                app_status TEXT NOT NULL DEFAULT '',
                page_status TEXT NOT NULL DEFAULT '',
                battery_level INTEGER,
                charging INTEGER,
                app_version TEXT NOT NULL DEFAULT '',
                relay_version TEXT NOT NULL DEFAULT '',
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_auth_station_device_heartbeats (
                station_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                station_name TEXT NOT NULL DEFAULT '',
                account_fingerprint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                last_heartbeat_at TEXT NOT NULL DEFAULT '',
                device_status TEXT NOT NULL DEFAULT '',
                adb_status TEXT NOT NULL DEFAULT '',
                app_status TEXT NOT NULL DEFAULT '',
                page_status TEXT NOT NULL DEFAULT '',
                battery_level INTEGER,
                charging INTEGER,
                app_version TEXT NOT NULL DEFAULT '',
                relay_version TEXT NOT NULL DEFAULT '',
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (station_id, device_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_auth_station_device_bindings (
                binding_id TEXT PRIMARY KEY,
                station_id TEXT NOT NULL,
                device_serial TEXT NOT NULL,
                guild_id TEXT NOT NULL DEFAULT '',
                guild_name TEXT NOT NULL DEFAULT '',
                account_fingerprint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_by TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(station_id, device_serial)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_data_jobs (
                job_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                job_type TEXT NOT NULL,
                data_date TEXT NOT NULL DEFAULT '',
                data_period_start TEXT NOT NULL DEFAULT '',
                data_period_end TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                requires_timo_session INTEGER NOT NULL DEFAULT 1,
                output_ref TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_daily_id_records (
                record_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                data_date TEXT NOT NULL,
                timo_id TEXT NOT NULL,
                external_user_id TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                is_member INTEGER NOT NULL DEFAULT 0,
                is_real_verified INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_revenue_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                period_type TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                timo_id TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                total_diamonds REAL NOT NULL DEFAULT 0,
                total_income REAL NOT NULL DEFAULT 0,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                source_job_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_anchor_milestones (
                milestone_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                timo_id TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                milestone_type TEXT NOT NULL,
                first_reached_at TEXT NOT NULL DEFAULT '',
                value REAL NOT NULL DEFAULT 0,
                source_snapshot_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_external_sync_runs (
                run_id TEXT PRIMARY KEY,
                snapshot_at TEXT NOT NULL,
                data_date_bj TEXT NOT NULL,
                status TEXT NOT NULL,
                guild_count INTEGER NOT NULL DEFAULT 0,
                streamer_count INTEGER NOT NULL DEFAULT 0,
                revenue_count INTEGER NOT NULL DEFAULT 0,
                task_count INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_external_streamers (
                guild_executor_key TEXT NOT NULL,
                guild_name TEXT NOT NULL,
                country TEXT NOT NULL DEFAULT '',
                guild_country TEXT NOT NULL DEFAULT '',
                timo_country_name TEXT NOT NULL DEFAULT '',
                timo_id TEXT NOT NULL,
                user_uuid TEXT NOT NULL DEFAULT '',
                nickname TEXT NOT NULL DEFAULT '',
                registered_at_bj TEXT NOT NULL DEFAULT '',
                joined_guild_at_bj TEXT NOT NULL DEFAULT '',
                timo_registered_at_bj TEXT NOT NULL DEFAULT '',
                last_active_at_bj TEXT NOT NULL DEFAULT '',
                is_real_person INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '',
                host_role TEXT NOT NULL DEFAULT '',
                source_payload TEXT NOT NULL DEFAULT '{}',
                snapshot_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_executor_key, timo_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_external_revenue_daily (
                guild_executor_key TEXT NOT NULL,
                guild_name TEXT NOT NULL,
                country TEXT NOT NULL DEFAULT '',
                stat_date_bj TEXT NOT NULL,
                timo_id TEXT NOT NULL,
                user_uuid TEXT NOT NULL DEFAULT '',
                nickname TEXT NOT NULL DEFAULT '',
                total_income REAL NOT NULL DEFAULT 0,
                qualified_revenue REAL NOT NULL DEFAULT 0,
                matching_income REAL NOT NULL DEFAULT 0,
                private_message_income REAL NOT NULL DEFAULT 0,
                private_gift_income REAL NOT NULL DEFAULT 0,
                call_income REAL NOT NULL DEFAULT 0,
                online_hours REAL NOT NULL DEFAULT 0,
                call_count INTEGER NOT NULL DEFAULT 0,
                quality_host INTEGER NOT NULL DEFAULT 0,
                quality_revenue REAL NOT NULL DEFAULT 0,
                provisional INTEGER NOT NULL DEFAULT 1,
                source_payload TEXT NOT NULL DEFAULT '{}',
                snapshot_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_executor_key, stat_date_bj, timo_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_external_guild_task_snapshots (
                guild_executor_key TEXT NOT NULL,
                guild_name TEXT NOT NULL,
                country TEXT NOT NULL DEFAULT '',
                snapshot_at TEXT NOT NULL,
                task_type TEXT NOT NULL,
                task_name TEXT NOT NULL DEFAULT '',
                target_diamonds REAL NOT NULL DEFAULT 0,
                progress_diamonds REAL NOT NULL DEFAULT 0,
                reward_diamonds REAL NOT NULL DEFAULT 0,
                task_status TEXT NOT NULL DEFAULT '',
                source_payload TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_executor_key, snapshot_at, task_type)
            )
            """,
        ),
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_timo_ticket_versions_guild_created ON timo_ticket_versions (guild_id, created_at DESC)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_timo_recovery_runs_active_guild ON timo_recovery_runs (guild_id) WHERE status IN ('created','chrome_profile_checking','otp_required','otp_request_created','otp_reading','otp_received','otp_submitting','ticket_extracting','guild_verifying')",
            "CREATE INDEX IF NOT EXISTS idx_timo_recovery_runs_guild_status ON timo_recovery_runs (guild_id, status, started_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_timo_otp_requests_status_expires ON timo_otp_requests (status, expires_at, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_timo_auth_stations_heartbeat ON timo_auth_stations (status, last_heartbeat_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_timo_auth_station_device_heartbeats_status ON timo_auth_station_device_heartbeats (status, last_heartbeat_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_timo_auth_station_device_bindings_guild ON timo_auth_station_device_bindings (guild_name, status, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_timo_auth_station_device_bindings_station ON timo_auth_station_device_bindings (station_id, status, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_timo_data_jobs_status ON timo_data_jobs (status, requires_timo_session, created_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_timo_daily_id_records_unique ON timo_daily_id_records (guild_id, data_date, timo_id)",
            "CREATE INDEX IF NOT EXISTS idx_timo_daily_id_records_date_guild ON timo_daily_id_records (data_date, guild_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_timo_revenue_snapshots_unique ON timo_revenue_snapshots (guild_id, period_type, period_start, period_end, timo_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_timo_anchor_milestones_unique ON timo_anchor_milestones (guild_id, timo_id, milestone_type)",
            "CREATE INDEX IF NOT EXISTS idx_timo_external_streamers_guild ON timo_external_streamers (guild_name, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_timo_external_revenue_date ON timo_external_revenue_daily (stat_date_bj, guild_name)",
            "CREATE INDEX IF NOT EXISTS idx_timo_external_tasks_snapshot ON timo_external_guild_task_snapshots (snapshot_at DESC, guild_name)",
        ),
    },
    'timo_auth_station_evidence_v2': {
        'tables': (
            """
            CREATE TABLE IF NOT EXISTS timo_otp_delivery_evidence (
                evidence_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                recovery_id TEXT NOT NULL DEFAULT '',
                guild_id TEXT NOT NULL DEFAULT '',
                station_id TEXT NOT NULL DEFAULT '',
                device_serial TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                phase TEXT NOT NULL DEFAULT '',
                page_key TEXT NOT NULL DEFAULT '',
                page_fingerprint TEXT NOT NULL DEFAULT '',
                latest_message_hashes_json TEXT NOT NULL DEFAULT '[]',
                message_count INTEGER,
                message_count_delta INTEGER,
                notification_count_delta INTEGER,
                notification_fingerprint TEXT NOT NULL DEFAULT '',
                candidate_count INTEGER NOT NULL DEFAULT 0,
                parse_status TEXT NOT NULL DEFAULT 'not_run',
                parse_miss_reason TEXT NOT NULL DEFAULT '',
                delivery_confidence_level TEXT NOT NULL DEFAULT 'L0',
                final_failure_reason TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                collected_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_app_locator_profiles (
                locator_profile_id TEXT PRIMARY KEY,
                app_version_name TEXT NOT NULL DEFAULT '',
                app_version_code TEXT NOT NULL DEFAULT '',
                locale TEXT NOT NULL DEFAULT '',
                device_resolution_class TEXT NOT NULL DEFAULT '',
                official_assistant_locator_json TEXT NOT NULL DEFAULT '{}',
                system_message_tab_locator_json TEXT NOT NULL DEFAULT '{}',
                otp_template_profile_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'testing',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(app_version_name, app_version_code, locale, device_resolution_class)
            )
            """,
        ),
        'alter': (
            "ALTER TABLE timo_otp_requests ADD COLUMN delivery_state TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_otp_requests ADD COLUMN parse_status TEXT NOT NULL DEFAULT 'not_run'",
            "ALTER TABLE timo_otp_requests ADD COLUMN parse_miss_reason TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_otp_requests ADD COLUMN delivery_confidence_level TEXT NOT NULL DEFAULT 'L0'",
            "ALTER TABLE timo_otp_requests ADD COLUMN final_failure_reason TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_otp_requests ADD COLUMN evidence_summary_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE timo_otp_requests ADD COLUMN cooldown_until TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_recovery_runs ADD COLUMN delivery_state TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_recovery_runs ADD COLUMN final_failure_reason TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_recovery_runs ADD COLUMN cooldown_until TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_recovery_runs ADD COLUMN evidence_summary_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN screen_unlocked INTEGER",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN timo_app_installed INTEGER",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN timo_app_version_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN timo_app_version_code TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN notification_permission_enabled INTEGER",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN notification_listener_enabled INTEGER",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN accessibility_enabled INTEGER",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN battery_optimization_ignored INTEGER",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN network_connected INTEGER",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN official_assistant_page_ready INTEGER",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN last_page_fingerprint TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN last_message_count INTEGER",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN last_successful_ui_dump_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN device_health TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN locator_profile_status TEXT NOT NULL DEFAULT ''",
        ),
        'indexes': (
            "DROP INDEX IF EXISTS idx_timo_recovery_runs_active_guild",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_timo_recovery_runs_active_guild ON timo_recovery_runs (guild_id) WHERE status IN ('created','chrome_profile_checking','otp_required','otp_request_created','otp_reading','otp_received','otp_submitting','ticket_extracting','guild_verifying','precheck_device_ready','pre_request_snapshot','timo_send_requested','timo_send_accepted','delivery_waiting','evidence_collecting','page_refreshing','otp_candidate_found','otp_validated','otp_consuming','ticket_verifying')",
            "CREATE INDEX IF NOT EXISTS idx_timo_otp_delivery_evidence_request ON timo_otp_delivery_evidence (request_id, collected_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_timo_otp_delivery_evidence_guild ON timo_otp_delivery_evidence (guild_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_timo_otp_delivery_evidence_failure ON timo_otp_delivery_evidence (final_failure_reason, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_timo_recovery_runs_cooldown ON timo_recovery_runs (guild_id, cooldown_until)",
        ),
    },
    'timo_streamer_dual_time_contract_v1': {
        'alter': (
            "ALTER TABLE timo_external_streamers ADD COLUMN joined_guild_at_bj TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_external_streamers ADD COLUMN timo_registered_at_bj TEXT NOT NULL DEFAULT ''",
            "UPDATE timo_external_streamers SET joined_guild_at_bj=registered_at_bj WHERE joined_guild_at_bj='' AND registered_at_bj<>''",
        ),
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_timo_external_streamers_joined_guild_at ON timo_external_streamers (joined_guild_at_bj, guild_name)",
        ),
    },
    'timo_streamer_country_contract_v1': {
        'alter': (
            "ALTER TABLE timo_external_streamers ADD COLUMN guild_country TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_external_streamers ADD COLUMN timo_country_name TEXT NOT NULL DEFAULT ''",
            "UPDATE timo_external_streamers SET guild_country=country WHERE guild_country='' AND country<>''",
            """
            UPDATE timo_external_streamers
            SET timo_country_name=TRIM(COALESCE(json_extract(source_payload, '$.countryName'), ''))
            WHERE timo_country_name='' AND json_valid(source_payload)
            """,
        ),
        'indexes': (
            "CREATE INDEX IF NOT EXISTS idx_timo_external_streamers_account_country ON timo_external_streamers (timo_country_name, guild_name)",
        ),
    },
    'timo_auth_station_reliability_v1': {
        'tables': (
            """
            CREATE TABLE IF NOT EXISTS timo_guild_runtime_state (
                guild_id TEXT PRIMARY KEY,
                ticket_status TEXT NOT NULL DEFAULT 'unknown',
                ticket_fingerprint TEXT NOT NULL DEFAULT '',
                ticket_last_verified_at TEXT NOT NULL DEFAULT '',
                ticket_last_probe_result TEXT NOT NULL DEFAULT '',
                ticket_expired_observed_at TEXT NOT NULL DEFAULT '',
                last_ticket_error_code TEXT NOT NULL DEFAULT '',
                station_id TEXT NOT NULL DEFAULT '',
                device_serial TEXT NOT NULL DEFAULT '',
                transport_ready INTEGER NOT NULL DEFAULT 0,
                observation_ready INTEGER NOT NULL DEFAULT 0,
                otp_ready INTEGER NOT NULL DEFAULT 0,
                device_health TEXT NOT NULL DEFAULT 'unknown',
                blocked_reason TEXT NOT NULL DEFAULT '',
                last_successful_observation_at TEXT NOT NULL DEFAULT '',
                last_observation_error_code TEXT NOT NULL DEFAULT '',
                last_observation_error_at TEXT NOT NULL DEFAULT '',
                observation_ready_age_seconds INTEGER,
                active_otp_request_id TEXT NOT NULL DEFAULT '',
                otp_status TEXT NOT NULL DEFAULT 'idle',
                otp_window_deadline_at TEXT NOT NULL DEFAULT '',
                otp_remaining_seconds INTEGER,
                last_otp_result TEXT NOT NULL DEFAULT '',
                recovery_status TEXT NOT NULL DEFAULT 'idle',
                active_recovery_id TEXT NOT NULL DEFAULT '',
                last_recovery_id TEXT NOT NULL DEFAULT '',
                last_recovery_result TEXT NOT NULL DEFAULT '',
                recovery_started_at TEXT NOT NULL DEFAULT '',
                recovery_finished_at TEXT NOT NULL DEFAULT '',
                next_allowed_recovery_at TEXT NOT NULL DEFAULT '',
                can_operate INTEGER NOT NULL DEFAULT 0,
                can_request_otp INTEGER NOT NULL DEFAULT 0,
                can_recover INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_guild_operation_locks (
                guild_id TEXT PRIMARY KEY,
                operation_type TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                locked_by TEXT NOT NULL DEFAULT '',
                locked_at TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS timo_keepalive_jobs (
                job_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                guild_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
        'alter': (
            "ALTER TABLE timo_otp_requests ADD COLUMN otp_requested_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_otp_requests ADD COLUMN otp_provider_accepted_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_otp_requests ADD COLUMN otp_window_deadline_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_otp_requests ADD COLUMN otp_read_deadline_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_otp_requests ADD COLUMN otp_submit_deadline_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_otp_requests ADD COLUMN otp_remaining_seconds INTEGER",
            "ALTER TABLE timo_otp_requests ADD COLUMN min_submit_budget_seconds INTEGER NOT NULL DEFAULT 15",
            "ALTER TABLE timo_otp_requests ADD COLUMN min_ticket_probe_budget_seconds INTEGER NOT NULL DEFAULT 10",
            "ALTER TABLE timo_otp_requests ADD COLUMN window_abort_reason TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_recovery_runs ADD COLUMN otp_l4_consumed_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_recovery_runs ADD COLUMN browser_submit_accepted_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_recovery_runs ADD COLUMN ticket_candidate_captured_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_recovery_runs ADD COLUMN ticket_probe_passed_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_recovery_runs ADD COLUMN ticket_persisted_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_recovery_runs ADD COLUMN post_persist_probe_passed_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN dump_duration_ms INTEGER",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN dump_timeout_count_10m INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN dump_timeout_count_1h INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN last_dump_error TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN last_dump_error_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN last_official_assistant_ready_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN observation_ready INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN observation_ready_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN relay_restart_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE timo_app_locator_profiles ADD COLUMN platform TEXT NOT NULL DEFAULT 'android'",
            "ALTER TABLE timo_app_locator_profiles ADD COLUMN language TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_app_locator_profiles ADD COLUMN brand TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_app_locator_profiles ADD COLUMN model TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_app_locator_profiles ADD COLUMN resolution TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE timo_app_locator_profiles ADD COLUMN orientation TEXT NOT NULL DEFAULT 'portrait'",
            "ALTER TABLE timo_app_locator_profiles ADD COLUMN profile_state TEXT NOT NULL DEFAULT 'profile_learning'",
        ),
        'indexes': (
            "DROP INDEX IF EXISTS idx_timo_recovery_runs_active_guild",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_timo_recovery_runs_active_guild ON timo_recovery_runs (guild_id) WHERE status IN ('created','chrome_profile_checking','otp_required','otp_request_queued','otp_request_created','otp_reading','otp_received','otp_submitting','ticket_extracting','guild_verifying','precheck_device_ready','station_observation_ready','pre_request_snapshot','timo_send_requested','timo_send_accepted','delivery_waiting','evidence_collecting','page_refreshing','otp_candidate_found','otp_validated','otp_consuming','otp_l4_consumed','ticket_verifying','browser_submit_started','browser_submit_accepted','ticket_candidate_collection_started','ticket_candidate_captured','ticket_probe_passed','ticket_persisted','post_persist_probe_passed')",
            "CREATE INDEX IF NOT EXISTS idx_timo_guild_runtime_ready ON timo_guild_runtime_state (can_request_otp, can_recover, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_timo_operation_locks_lease ON timo_guild_operation_locks (lease_expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_timo_keepalive_jobs_status ON timo_keepalive_jobs (status, lease_expires_at, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_timo_otp_window_deadline ON timo_otp_requests (status, otp_window_deadline_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_timo_locator_profile_strict ON timo_app_locator_profiles (platform, app_version_name, app_version_code, language, brand, model, resolution, orientation)",
        ),
    },
}


def _execute_ignore_existing(conn: sqlite3.Connection, statements: Iterable[str]) -> None:
    for statement in statements:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass


def _normalize_index_sql(statement: str) -> str:
    normalized = ' '.join(str(statement or '').strip().rstrip(';').split()).lower()
    return normalized.replace(' index if not exists ', ' index ', 1)


def _ensure_index_definition(
    conn: sqlite3.Connection,
    *,
    index_name: str,
    create_sql: str,
) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    existing_sql = str(row[0] or '') if row else ''
    if _normalize_index_sql(existing_sql) == _normalize_index_sql(create_sql):
        return
    conn.execute('SAVEPOINT ensure_index_definition')
    try:
        conn.execute(f'DROP INDEX IF EXISTS "{index_name}"')
        conn.execute(create_sql)
    except Exception:
        conn.execute('ROLLBACK TO ensure_index_definition')
        raise
    finally:
        conn.execute('RELEASE ensure_index_definition')


def _migrate_timo_locator_profiles_strict_identity(conn: sqlite3.Connection) -> None:
    profile_columns = """
        locator_profile_id, app_version_name, app_version_code, locale,
        device_resolution_class, official_assistant_locator_json,
        system_message_tab_locator_json, otp_template_profile_id, status,
        created_at, updated_at, platform, language, brand, model,
        resolution, orientation, profile_state
    """
    legacy_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='timo_app_locator_profiles_legacy'"
    ).fetchone()
    current_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='timo_app_locator_profiles'"
    ).fetchone()
    if legacy_table and current_table:
        conn.execute(
            f"INSERT OR IGNORE INTO timo_app_locator_profiles ({profile_columns}) "
            f"SELECT {profile_columns} FROM timo_app_locator_profiles_legacy"
        )
        conn.execute('DROP TABLE timo_app_locator_profiles_legacy')
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='timo_app_locator_profiles'"
    ).fetchone()
    table_sql = str(row[0] or '') if row else ''
    legacy_unique = 'UNIQUE(app_version_name, app_version_code, locale, device_resolution_class)'
    if legacy_unique in table_sql:
        conn.execute('ALTER TABLE timo_app_locator_profiles RENAME TO timo_app_locator_profiles_legacy')
        conn.execute(
            """
            CREATE TABLE timo_app_locator_profiles (
                locator_profile_id TEXT PRIMARY KEY,
                app_version_name TEXT NOT NULL DEFAULT '',
                app_version_code TEXT NOT NULL DEFAULT '',
                locale TEXT NOT NULL DEFAULT '',
                device_resolution_class TEXT NOT NULL DEFAULT '',
                official_assistant_locator_json TEXT NOT NULL DEFAULT '{}',
                system_message_tab_locator_json TEXT NOT NULL DEFAULT '{}',
                otp_template_profile_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'testing',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'android',
                language TEXT NOT NULL DEFAULT '',
                brand TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                resolution TEXT NOT NULL DEFAULT '',
                orientation TEXT NOT NULL DEFAULT 'portrait',
                profile_state TEXT NOT NULL DEFAULT 'profile_learning'
            )
            """
        )
        conn.execute(
            f"INSERT INTO timo_app_locator_profiles ({profile_columns}) "
            f"SELECT {profile_columns} FROM timo_app_locator_profiles_legacy"
        )
        conn.execute('DROP TABLE timo_app_locator_profiles_legacy')
    conn.execute(
        """
        DELETE FROM timo_app_locator_profiles AS stale
        WHERE EXISTS (
            SELECT 1 FROM timo_app_locator_profiles AS newer
            WHERE newer.platform=stale.platform
              AND newer.app_version_name=stale.app_version_name
              AND newer.app_version_code=stale.app_version_code
              AND newer.language=stale.language
              AND newer.brand=stale.brand
              AND newer.model=stale.model
              AND newer.resolution=stale.resolution
              AND newer.orientation=stale.orientation
              AND (
                  newer.updated_at > stale.updated_at OR
                  (newer.updated_at = stale.updated_at AND newer.locator_profile_id > stale.locator_profile_id)
              )
        )
        """
    )


def _migrate_guild_country_contract(conn: sqlite3.Connection) -> None:
    guild_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(guild_executors)").fetchall()}
    if {'country', 'guild_country', 'eligible_user_countries', 'routing_region'}.issubset(guild_columns):
        rows = conn.execute(
            "SELECT guild_name, country, guild_country, eligible_user_countries, routing_region FROM guild_executors"
        ).fetchall()
        legacy_spanish = {'Mexico', 'Colombia', 'Venezuela', 'Chile'}
        for guild_name, country, guild_country, eligible_json, routing_region in rows:
            raw_country = str(country or '').strip()
            raw_options = [item.strip() for item in raw_country.replace('，', ',').split(',') if item.strip()]
            is_legacy_spanish = set(raw_options) == legacy_spanish
            canonical_guild_country = str(guild_country or '').strip() or ('Mexico' if is_legacy_spanish else raw_country)
            try:
                eligible = json.loads(str(eligible_json or '[]'))
            except (TypeError, ValueError, json.JSONDecodeError):
                eligible = []
            if not isinstance(eligible, list) or not eligible:
                eligible = raw_options or ([canonical_guild_country] if canonical_guild_country else [])
            canonical_routing_region = str(routing_region or '').strip() or ('ES_LATAM' if is_legacy_spanish else '')
            conn.execute(
                "UPDATE guild_executors SET country=?, guild_country=?, eligible_user_countries=?, routing_region=? WHERE guild_name=?",
                (
                    canonical_guild_country if is_legacy_spanish else raw_country,
                    canonical_guild_country,
                    json.dumps(eligible, ensure_ascii=False),
                    canonical_routing_region,
                    guild_name,
                ),
            )
    lead_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    if {'assigned_guild_country', 'cross_country_fallback', 'cross_country_fallback_reason'}.issubset(lead_columns):
        conn.execute(
            """
            UPDATE leads
            SET assigned_guild_country = COALESCE((
                SELECT NULLIF(COALESCE(NULLIF(guild_country, ''), country), '')
                FROM guild_executors WHERE guild_executors.guild_name = leads.dept_name
            ), assigned_guild_country)
            WHERE COALESCE(assigned_guild_country, '') = '' AND COALESCE(dept_name, '') != ''
            """
        )
        conn.execute(
            """
            UPDATE leads
            SET cross_country_fallback = 1,
                cross_country_fallback_reason = CASE
                    WHEN COALESCE(cross_country_fallback_reason, '') = ''
                    THEN 'historical_compatible_guild_assignment'
                    ELSE cross_country_fallback_reason END
            WHERE COALESCE(assigned_guild_country, '') != ''
              AND lower(trim(COALESCE(country, ''))) != lower(trim(assigned_guild_country))
            """
        )
    for table_name in (
        'streamer_analytics_profile_summary',
        'streamer_analytics_streamer_daily_summary',
        'streamer_analytics_daily_summary',
        'streamer_analytics_newcomer_summary',
    ):
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
        if {'app_name', 'guild_name', 'country'}.issubset(columns):
            conn.execute(
                f"UPDATE {table_name} SET country='Mexico' "
                "WHERE lower(app_name)='linky' AND guild_name IN ('Nova-Spa','Evian✨') "
                "AND country IN ('Colombia,Mexico,Venezuela,Chile','Colombia,M')"
            )
    policy_columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(streamer_roi_policies)").fetchall()]
    if {'app_name', 'country', 'guild_name', 'effective_from'}.issubset(set(policy_columns)):
        cursor = conn.execute(
            "SELECT * FROM streamer_roi_policies WHERE lower(app_name)='linky' "
            "AND guild_name IN ('Nova-Spa','Evian✨') "
            "AND country IN ('Colombia,Mexico,Venezuela,Chile','Colombia,M','Mexico')"
        )
        row_columns = [str(item[0]) for item in cursor.description or ()]
        policy_rows = [dict(zip(row_columns, row)) for row in cursor.fetchall()]
        groups: dict[tuple[str, str], list[dict[str, object]]] = {}
        for row in policy_rows:
            groups.setdefault((str(row['guild_name']), str(row['effective_from'])), []).append(row)
        comparison_columns = [column for column in row_columns if column not in {'country', 'updated_at', 'updated_by'}]
        for (guild_name, effective_from), rows in groups.items():
            if len(rows) == 1 and str(rows[0].get('country') or '') == 'Mexico':
                continue
            variants = {tuple(row.get(column) for column in comparison_columns) for row in rows}
            if len(variants) != 1:
                raise RuntimeError(f'guild_settlement_policy_conflict:{guild_name}:{effective_from}')
            chosen = max(rows, key=lambda row: (str(row.get('country') or '') == 'Mexico', str(row.get('updated_at') or '')))
            chosen['country'] = 'Mexico'
            conn.execute(
                "DELETE FROM streamer_roi_policies WHERE lower(app_name)='linky' "
                "AND guild_name=? AND effective_from=? "
                "AND country IN ('Colombia,Mexico,Venezuela,Chile','Colombia,M','Mexico')",
                (guild_name, effective_from),
            )
            placeholders = ','.join('?' for _ in row_columns)
            conn.execute(
                f"INSERT INTO streamer_roi_policies ({','.join(row_columns)}) VALUES ({placeholders})",
                tuple(chosen.get(column) for column in row_columns),
            )


def apply_schema_migration_registry(conn: sqlite3.Connection) -> None:
    for migration in SCHEMA_MIGRATIONS.values():
        _execute_ignore_existing(conn, migration.get('tables', ()))
        _execute_ignore_existing(conn, migration.get('alter', ()))
        if migration is SCHEMA_MIGRATIONS.get('timo_auth_station_reliability_v1'):
            _migrate_timo_locator_profiles_strict_identity(conn)
        _execute_ignore_existing(
            conn,
            (
                statement
                for statement in migration.get('indexes', ())
                if TIMO_ACTIVE_RECOVERY_INDEX_NAME not in statement
            ),
        )
    _migrate_guild_country_contract(conn)
    _ensure_index_definition(
        conn,
        index_name=TIMO_ACTIVE_RECOVERY_INDEX_NAME,
        create_sql=TIMO_ACTIVE_RECOVERY_INDEX_SQL,
    )
    ensure_growth_schema(conn)
