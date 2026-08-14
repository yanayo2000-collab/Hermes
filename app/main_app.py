from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from app.main_shared import *
from app.timo_membership_query_page import TIMO_MEMBERSHIP_QUERY_PAGE_HTML
from app.timo_guild_executor import TIMO_AUTH_EXPIRED_RESULT_CODE
from app.timo_guild_identity import timo_guild_display_name, timo_guild_storage_name
from app.task_control_plane import (
    TASK_CONTROL_PAGE_HTML,
    get_unified_task,
    list_unified_tasks,
    manage_unified_task,
)
from app.growth.api import create_ad_experiment_router, create_growth_router
from app.growth.ad_experiment_service import AdExperimentService
from app.growth.errors import GrowthError
from app.meta_api_budget import (
    BudgetedMetaSession,
    MetaApiBudgetManager,
    MetaRateLimitBlocked,
    default_meta_rate_limit_db_path,
)


class TimoMembershipQueryRequest(BaseModel):
    guild_name: str
    timo_id: str



def create_app(settings: Optional[Dict[str, Any]] = None) -> FastAPI:
    cfg = {"DB_PATH": DEFAULT_DB_PATH}
    if settings:
        cfg.update(settings)
    task_control_db_path = Path(
        cfg.get('MCN_CONTROL_PLANE_DB')
        or os.getenv('MCN_CONTROL_PLANE_DB')
        or '/data/mcn-data/control/mcn_control_plane.db'
    )
    auth_enabled = (
        bool(cfg.get('AUTH_ENABLED'))
        if 'AUTH_ENABLED' in cfg
        else (settings is None and cfg['DB_PATH'] != ':memory:')
    )
    auth_cookie_secure = (
        bool(cfg.get('AUTH_COOKIE_SECURE'))
        if 'AUTH_COOKIE_SECURE' in cfg
        else str(os.getenv('AUTH_COOKIE_SECURE') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    )
    auth_session_ttl_hours = int(cfg.get('AUTH_SESSION_TTL_HOURS') or os.getenv('AUTH_SESSION_TTL_HOURS') or 12)
    auth_internal_token = str(cfg.get('AUTH_INTERNAL_TOKEN') or os.getenv('AUTH_INTERNAL_TOKEN') or '').strip()
    timo_auth_station_token = str(cfg.get('TIMO_AUTH_STATION_TOKEN') or os.getenv('TIMO_AUTH_STATION_TOKEN') or '').strip()
    timo_external_feed_token = str(
        cfg.get('TIMO_EXTERNAL_FEED_TOKEN')
        or cfg.get('TIMO_EXTERNAL_API_TOKEN')
        or os.getenv('TIMO_EXTERNAL_FEED_TOKEN')
        or os.getenv('TIMO_EXTERNAL_API_TOKEN')
        or ''
    ).strip()
    newcomer_external_feed_token = str(
        cfg.get('NEWCOMER_EXTERNAL_FEED_TOKEN')
        or os.getenv('NEWCOMER_EXTERNAL_FEED_TOKEN')
        or timo_external_feed_token
    ).strip()
    db = Database(cfg["DB_PATH"])
    auth_manager = OpsAuthManager(
        db,
        session_ttl_hours=auth_session_ttl_hours,
        cookie_secure=auth_cookie_secure,
    )
    crm_adapter = cfg.get('CRM_ADAPTER')
    ocr_adapter = cfg.get('OCR_ADAPTER')
    lark_media_adapter = cfg.get('LARK_MEDIA_ADAPTER')
    lark_reply_adapter = cfg.get('LARK_REPLY_ADAPTER')
    lark_reply_adapter_by_app_id = cfg.get('LARK_REPLY_ADAPTER_BY_APP_ID') or {}
    registration_group_approval_executor = cfg.get('REGISTRATION_GROUP_APPROVAL_EXECUTOR')
    registration_group_approval_executor_kind = cfg.get('REGISTRATION_GROUP_APPROVAL_EXECUTOR_KIND') or os.getenv('REGISTRATION_GROUP_APPROVAL_EXECUTOR_KIND')
    official_group_approval_executor = cfg.get('OFFICIAL_GROUP_APPROVAL_EXECUTOR')
    official_group_approval_executor_kind = cfg.get('OFFICIAL_GROUP_APPROVAL_EXECUTOR_KIND') or os.getenv('OFFICIAL_GROUP_APPROVAL_EXECUTOR_KIND')
    timo_guild_executor = cfg.get('TIMO_GUILD_EXECUTOR')
    official_group_approval_webhook_url = cfg.get('OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL') or os.getenv('OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL')
    official_group_approval_webhook_token = cfg.get('OFFICIAL_GROUP_APPROVAL_WEBHOOK_TOKEN') or os.getenv('OFFICIAL_GROUP_APPROVAL_WEBHOOK_TOKEN')
    official_group_bridge_token = cfg.get('OFFICIAL_GROUP_BRIDGE_TOKEN') or os.getenv('OFFICIAL_GROUP_BRIDGE_TOKEN')
    official_group_approval_webhook_session = cfg.get('OFFICIAL_GROUP_APPROVAL_WEBHOOK_SESSION')
    official_group_approval_webhook_timeout_seconds = cfg.get('OFFICIAL_GROUP_APPROVAL_WEBHOOK_TIMEOUT_SECONDS') or os.getenv('OFFICIAL_GROUP_APPROVAL_WEBHOOK_TIMEOUT_SECONDS') or 20
    appsflyer_api_token = str(cfg.get('APPSFLYER_API_TOKEN') or os.getenv('APPSFLYER_API_TOKEN') or '').strip()
    appsflyer_app_ids = _parse_config_list(cfg.get('APPSFLYER_APP_IDS') or os.getenv('APPSFLYER_APP_IDS') or cfg.get('APPSFLYER_APP_ID') or os.getenv('APPSFLYER_APP_ID'))
    ad_dashboard_timezone = str(
        cfg.get('AD_DASHBOARD_TIMEZONE')
        or os.getenv('AD_DASHBOARD_TIMEZONE')
        or 'UTC'
    ).strip() or 'UTC'
    ad_dashboard_cache_timezone = str(
        cfg.get('AD_DASHBOARD_CACHE_TIMEZONE')
        or os.getenv('AD_DASHBOARD_CACHE_TIMEZONE')
        or 'Asia/Shanghai'
    ).strip() or 'Asia/Shanghai'
    appsflyer_timezone = ad_dashboard_timezone
    appsflyer_base_url = str(cfg.get('APPSFLYER_BASE_URL') or os.getenv('APPSFLYER_BASE_URL') or 'https://hq1.appsflyer.com').strip() or 'https://hq1.appsflyer.com'
    appsflyer_session = cfg.get('APPSFLYER_SESSION') or requests
    meta_ads_access_token = str(cfg.get('META_ADS_ACCESS_TOKEN') or os.getenv('META_ADS_ACCESS_TOKEN') or '').strip()
    meta_ads_account_ids = _parse_config_list(cfg.get('META_ADS_ACCOUNT_IDS') or os.getenv('META_ADS_ACCOUNT_IDS') or cfg.get('META_ADS_ACCOUNT_ID') or os.getenv('META_ADS_ACCOUNT_ID'))
    meta_ads_business_ids = _parse_config_list(cfg.get('META_ADS_BUSINESS_IDS') or os.getenv('META_ADS_BUSINESS_IDS') or '1525904929069438')
    meta_ads_country_page_ids = {
        'BR': str(cfg.get('META_ADS_PAGE_ID_BR') or os.getenv('META_ADS_PAGE_ID_BR') or '1279714905221405').strip(),
        'ID': str(cfg.get('META_ADS_PAGE_ID_ID') or os.getenv('META_ADS_PAGE_ID_ID') or '1132608379946941').strip(),
        'MX': str(cfg.get('META_ADS_PAGE_ID_MX') or os.getenv('META_ADS_PAGE_ID_MX') or '1188394557692833').strip(),
    }
    meta_ads_api_version = str(cfg.get('META_ADS_API_VERSION') or os.getenv('META_ADS_API_VERSION') or 'v25.0').strip() or 'v25.0'
    meta_ads_base_url = str(cfg.get('META_ADS_BASE_URL') or os.getenv('META_ADS_BASE_URL') or 'https://graph.facebook.com').strip() or 'https://graph.facebook.com'
    raw_meta_ads_session = cfg.get('META_ADS_SESSION') or requests
    meta_rate_limit_manager = cfg.get('META_RATE_LIMIT_MANAGER') or MetaApiBudgetManager(
        str(
            cfg.get('META_RATE_LIMIT_DB_PATH')
            or os.getenv('META_RATE_LIMIT_DB_PATH')
            or default_meta_rate_limit_db_path(str(cfg['DB_PATH']))
        ),
        hard_limit_percent=float(
            cfg.get('META_RATE_LIMIT_HARD_PERCENT')
            or os.getenv('META_RATE_LIMIT_HARD_PERCENT')
            or 85
        ),
    )
    meta_ads_session = (
        raw_meta_ads_session
        if isinstance(raw_meta_ads_session, BudgetedMetaSession)
        else BudgetedMetaSession(raw_meta_ads_session, meta_rate_limit_manager)
    )
    meta_page_eligibility_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
    meta_page_eligibility_cache_ttl_seconds = max(
        60,
        int(cfg.get('META_PAGE_ELIGIBILITY_CACHE_TTL_SECONDS') or 86400),
    )
    ad_creative_image_provider = str(
        cfg.get('AD_CREATIVE_IMAGE_PROVIDER') or os.getenv('AD_CREATIVE_IMAGE_PROVIDER') or PROVIDER_CHATGPT_PRO_MANUAL
    ).strip().lower() or PROVIDER_CHATGPT_PRO_MANUAL
    ad_creative_image_provider_enabled = (
        bool(cfg.get('AD_CREATIVE_IMAGE_PROVIDER_ENABLED'))
        if 'AD_CREATIVE_IMAGE_PROVIDER_ENABLED' in cfg
        else str(
            os.getenv('AD_CREATIVE_IMAGE_PROVIDER_ENABLED')
            or ('true' if ad_creative_image_provider in {PROVIDER_CHATGPT_PRO_MANUAL, PROVIDER_LOCAL_PRODUCTION_PNG, PROVIDER_HERMES_IMAGE2_AGENT} else '')
        ).strip().lower() in {'1', 'true', 'yes', 'on'}
    )
    ad_creative_image_provider_url = str(
        cfg.get('AD_CREATIVE_IMAGE_PROVIDER_URL') or os.getenv('AD_CREATIVE_IMAGE_PROVIDER_URL') or ''
    ).strip()
    ad_creative_image_provider_api_key = str(
        cfg.get('AD_CREATIVE_IMAGE_PROVIDER_API_KEY') or os.getenv('AD_CREATIVE_IMAGE_PROVIDER_API_KEY') or ''
    ).strip()
    ad_creative_image_provider_session = cfg.get('AD_CREATIVE_IMAGE_PROVIDER_SESSION') or requests
    try:
        ad_creative_image_provider_timeout_seconds = int(
            cfg.get('AD_CREATIVE_IMAGE_PROVIDER_TIMEOUT_SECONDS')
            or os.getenv('AD_CREATIVE_IMAGE_PROVIDER_TIMEOUT_SECONDS')
            or 90
        )
    except (TypeError, ValueError):
        ad_creative_image_provider_timeout_seconds = 90
    hermes_image2_agent_webhook_url = str(
        cfg.get('HERMES_IMAGE2_AGENT_WEBHOOK_URL') or os.getenv('HERMES_IMAGE2_AGENT_WEBHOOK_URL') or ''
    ).strip()
    hermes_image2_agent_token = str(
        cfg.get('HERMES_IMAGE2_AGENT_TOKEN') or os.getenv('HERMES_IMAGE2_AGENT_TOKEN') or ''
    ).strip()
    hermes_image2_agent_session = cfg.get('HERMES_IMAGE2_AGENT_SESSION') or requests
    try:
        hermes_image2_agent_timeout_seconds = int(
            cfg.get('HERMES_IMAGE2_AGENT_TIMEOUT_SECONDS')
            or os.getenv('HERMES_IMAGE2_AGENT_TIMEOUT_SECONDS')
            or 10
        )
    except (TypeError, ValueError):
        hermes_image2_agent_timeout_seconds = 10
    ad_creative_pro_workbench_enabled = (
        bool(cfg.get('AD_CREATIVE_PRO_WORKBENCH_ENABLED'))
        if 'AD_CREATIVE_PRO_WORKBENCH_ENABLED' in cfg
        else str(
            os.getenv('AD_CREATIVE_PRO_WORKBENCH_ENABLED')
            or ('true' if ad_creative_image_provider == PROVIDER_CHATGPT_PRO_MANUAL else '')
        ).strip().lower() in {'1', 'true', 'yes', 'on'}
    )
    bind_success_api_token = str(
        cfg.get('BIND_SUCCESS_API_TOKEN')
        or cfg.get('BI_BIND_SUCCESS_API_TOKEN')
        or os.getenv('BIND_SUCCESS_API_TOKEN')
        or os.getenv('BI_BIND_SUCCESS_API_TOKEN')
        or ''
    ).strip()
    bind_success_base_url = str(
        cfg.get('BIND_SUCCESS_API_BASE_URL')
        or cfg.get('BI_BIND_SUCCESS_API_BASE_URL')
        or os.getenv('BIND_SUCCESS_API_BASE_URL')
        or os.getenv('BI_BIND_SUCCESS_API_BASE_URL')
        or 'https://servertest.timetrade.club'
    ).strip() or 'https://servertest.timetrade.club'
    bind_success_project = str(
        cfg.get('BIND_SUCCESS_PROJECT')
        or cfg.get('BI_BIND_SUCCESS_PROJECT')
        or os.getenv('BIND_SUCCESS_PROJECT')
        or os.getenv('BI_BIND_SUCCESS_PROJECT')
        or 'TUGAO'
    ).strip() or 'TUGAO'
    bind_success_session = cfg.get('BIND_SUCCESS_SESSION') or cfg.get('BI_BIND_SUCCESS_SESSION') or requests
    try:
        ad_dashboard_cache_ttl_seconds = max(
            0,
            int(cfg.get('AD_DASHBOARD_CACHE_TTL_SECONDS') or os.getenv('AD_DASHBOARD_CACHE_TTL_SECONDS') or 0),
        )
    except (TypeError, ValueError):
        ad_dashboard_cache_ttl_seconds = 0
    ad_dashboard_cache: Dict[str, Dict[str, Any]] = {}
    ad_dashboard_cache_lock = threading.Lock()
    ad_daily_report_enabled = (
        bool(cfg.get('AD_DAILY_REPORT_ENABLED'))
        if 'AD_DAILY_REPORT_ENABLED' in cfg
        else str(os.getenv('AD_DAILY_REPORT_ENABLED') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'}
    )
    ad_recommendation_mode = str(
        cfg.get('AD_RECOMMENDATION_MODE') or os.getenv('AD_RECOMMENDATION_MODE') or 'shadow'
    ).strip().lower() or 'shadow'
    real_bind_provider_kind = str(
        cfg.get('REAL_BIND_PROVIDER') or os.getenv('REAL_BIND_PROVIDER') or 'fixture'
    ).strip().lower() or 'fixture'
    ad_review_enabled = (
        bool(cfg.get('AD_REVIEW_ENABLED'))
        if 'AD_REVIEW_ENABLED' in cfg
        else str(os.getenv('AD_REVIEW_ENABLED') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'}
    )
    ad_creative_flags = normalize_feature_flags({
        key: cfg.get(key) if key in cfg else os.getenv(key)
        for key in CREATIVE_FEATURE_FLAGS
    })
    ad_creative_analysis_enabled = ad_creative_flags.get('AD_CREATIVE_IMAGE_ANALYSIS_ENABLED', False)
    try:
        meta_creative_sync_page_size = int(cfg.get('META_CREATIVE_SYNC_PAGE_SIZE') or os.getenv('META_CREATIVE_SYNC_PAGE_SIZE') or 100)
    except (TypeError, ValueError):
        meta_creative_sync_page_size = 100
    meta_activity_sync_enabled = (
        bool(cfg.get('META_ACTIVITY_SYNC_ENABLED'))
        if 'META_ACTIVITY_SYNC_ENABLED' in cfg
        else str(os.getenv('META_ACTIVITY_SYNC_ENABLED') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    )
    try:
        meta_activity_sync_page_size = int(cfg.get('META_ACTIVITY_SYNC_PAGE_SIZE') or os.getenv('META_ACTIVITY_SYNC_PAGE_SIZE') or 100)
    except (TypeError, ValueError):
        meta_activity_sync_page_size = 100
    official_group_target_map_raw = cfg.get('OFFICIAL_GROUP_TARGET_MAP') or os.getenv('OFFICIAL_GROUP_TARGET_MAP') or '{}'
    official_group_target_map = {}
    if isinstance(official_group_target_map_raw, dict):
        official_group_target_map = {
            str(k).strip(): str(v).strip()
            for k, v in official_group_target_map_raw.items()
            if str(k).strip() and str(v).strip()
        }
    else:
        try:
            parsed_official_group_target_map = json.loads(official_group_target_map_raw)
            if isinstance(parsed_official_group_target_map, dict):
                official_group_target_map = {
                    str(k).strip(): str(v).strip()
                    for k, v in parsed_official_group_target_map.items()
                    if str(k).strip() and str(v).strip()
                }
        except Exception:
            official_group_target_map = {}
    media_cache_dir = cfg.get('MEDIA_CACHE_DIR')
    proxy_region_urls_raw = cfg.get('GUILD_EXECUTOR_PROXY_REGION_URLS') or os.getenv('GUILD_EXECUTOR_PROXY_REGION_URLS') or '{}'
    guild_executor_proxy_region_urls: Dict[str, str] = {}
    if isinstance(proxy_region_urls_raw, dict):
        guild_executor_proxy_region_urls = {
            str(k).strip(): str(v).strip()
            for k, v in proxy_region_urls_raw.items()
            if str(k).strip() and str(v).strip()
        }
    else:
        try:
            parsed_proxy_region_urls = json.loads(str(proxy_region_urls_raw or '{}'))
            if isinstance(parsed_proxy_region_urls, dict):
                guild_executor_proxy_region_urls = {
                    str(k).strip(): str(v).strip()
                    for k, v in parsed_proxy_region_urls.items()
                    if str(k).strip() and str(v).strip()
                }
        except Exception:
            guild_executor_proxy_region_urls = {}
    lark_default_app_name = cfg.get('LARK_DEFAULT_APP_NAME') or os.getenv('LARK_DEFAULT_APP_NAME')
    lark_default_dept_name = cfg.get('LARK_DEFAULT_DEPT_NAME') or os.getenv('LARK_DEFAULT_DEPT_NAME')
    app_id = cfg.get('LARK_APP_ID') or cfg.get('FEISHU_APP_ID') or os.getenv('LARK_APP_ID') or os.getenv('FEISHU_APP_ID')
    app_secret = cfg.get('LARK_APP_SECRET') or cfg.get('FEISHU_APP_SECRET') or os.getenv('LARK_APP_SECRET') or os.getenv('FEISHU_APP_SECRET')
    app_domain = cfg.get('LARK_DOMAIN') or cfg.get('FEISHU_DOMAIN') or os.getenv('LARK_DOMAIN') or os.getenv('FEISHU_DOMAIN') or 'lark'
    lark_reply_adapter_kind = str(cfg.get('LARK_REPLY_ADAPTER_KIND') or os.getenv('LARK_REPLY_ADAPTER_KIND') or '').strip().lower()
    crm_base_url = cfg.get('CRM_BASE_URL') or os.getenv('CRM_BASE_URL')
    crm_username = cfg.get('CRM_USERNAME') or os.getenv('CRM_USERNAME')
    crm_password = cfg.get('CRM_PASSWORD') or os.getenv('CRM_PASSWORD')
    crm_automation_token = cfg.get('CRM_AUTOMATION_TOKEN') or os.getenv('CRM_AUTOMATION_TOKEN')
    auto_bind_simulation = bool(cfg.get('AUTO_BIND_SIMULATION') or str(os.getenv('AUTO_BIND_SIMULATION') or '').strip().lower() in {'1', 'true', 'yes', 'on'})
    allow_live_bind_simulation = bool(cfg.get('ALLOW_LIVE_BIND_SIMULATION') or str(os.getenv('ALLOW_LIVE_BIND_SIMULATION') or '').strip().lower() in {'1', 'true', 'yes', 'on'})
    if auto_bind_simulation and cfg["DB_PATH"] != ':memory:' and not allow_live_bind_simulation:
        auto_bind_simulation = False
    bind_simulator = cfg.get('BIND_SIMULATOR')
    real_bind_executor = cfg.get('REAL_BIND_EXECUTOR')
    enable_chrome_bind_executor = bool(cfg.get('ENABLE_CHROME_BIND_EXECUTOR') or str(os.getenv('ENABLE_CHROME_BIND_EXECUTOR') or '').strip().lower() in {'1', 'true', 'yes', 'on'})
    chrome_profile_map_raw = cfg.get('BIND_CHROME_PROFILE_MAP') or os.getenv('BIND_CHROME_PROFILE_MAP') or '{}'
    chrome_profile_map = {}
    if not real_bind_executor and enable_chrome_bind_executor:
        try:
            parsed_profile_map = json.loads(chrome_profile_map_raw)
            if isinstance(parsed_profile_map, dict):
                chrome_profile_map = {str(k): str(v) for k, v in parsed_profile_map.items()}
        except Exception:
            chrome_profile_map = {}
        from app.live_bind_executor import LiveChromeBindExecutor
        real_bind_executor = LiveChromeBindExecutor(
            profile_map=chrome_profile_map,
            chrome_binary=cfg.get('CHROME_BINARY') or os.getenv('CHROME_BINARY'),
            chrome_user_data_root=cfg.get('CHROME_USER_DATA_ROOT') or os.getenv('CHROME_USER_DATA_ROOT'),
        )
    auto_bind_simulation_success_rate = cfg.get('AUTO_BIND_SIMULATION_SUCCESS_RATE') or os.getenv('AUTO_BIND_SIMULATION_SUCCESS_RATE') or 0.5
    auto_bind_simulation_seed = cfg.get('AUTO_BIND_SIMULATION_SEED')
    if auto_bind_simulation_seed is None and os.getenv('AUTO_BIND_SIMULATION_SEED'):
        auto_bind_simulation_seed = int(os.getenv('AUTO_BIND_SIMULATION_SEED'))
    ingress_async_default = (
        bool(cfg.get('INGRESS_ASYNC_DEFAULT'))
        if 'INGRESS_ASYNC_DEFAULT' in cfg
        else (cfg["DB_PATH"] != ':memory:' and str(os.getenv('INGRESS_ASYNC_DEFAULT') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'})
    )
    ingress_worker_enabled = (
        bool(cfg.get('INGRESS_WORKER_ENABLED'))
        if 'INGRESS_WORKER_ENABLED' in cfg
        else (ingress_async_default and str(os.getenv('INGRESS_WORKER_ENABLED') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'})
    )
    ingress_worker_poll_interval = cfg.get('INGRESS_WORKER_POLL_INTERVAL') or os.getenv('INGRESS_WORKER_POLL_INTERVAL') or 1.0
    ingress_worker_count = cfg.get('INGRESS_WORKER_COUNT') or os.getenv('INGRESS_WORKER_COUNT') or 8
    ingress_rate_limit_per_minute = cfg.get('INGRESS_RATE_LIMIT_PER_MINUTE') or os.getenv('INGRESS_RATE_LIMIT_PER_MINUTE') or 600
    external_call_rate_limit_per_minute = cfg.get('EXTERNAL_CALL_RATE_LIMIT_PER_MINUTE') or os.getenv('EXTERNAL_CALL_RATE_LIMIT_PER_MINUTE') or 300
    group_atmosphere_scheduler_enabled = (
        bool(cfg.get('GROUP_ATMOSPHERE_SCHEDULER_ENABLED'))
        if 'GROUP_ATMOSPHERE_SCHEDULER_ENABLED' in cfg
        else (cfg["DB_PATH"] != ':memory:' and str(os.getenv('GROUP_ATMOSPHERE_SCHEDULER_ENABLED') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'})
    )
    group_atmosphere_scheduler_poll_interval_seconds = cfg.get('GROUP_ATMOSPHERE_SCHEDULER_POLL_INTERVAL_SECONDS') or os.getenv('GROUP_ATMOSPHERE_SCHEDULER_POLL_INTERVAL_SECONDS') or 30
    if 'OPS_INTAKE_AUTO_CLEAR_STALE_FEEDBACK_ENABLED' in cfg:
        raw_auto_clear = cfg.get('OPS_INTAKE_AUTO_CLEAR_STALE_FEEDBACK_ENABLED')
        ops_intake_auto_clear_stale_feedback_enabled = raw_auto_clear if isinstance(raw_auto_clear, bool) else str(raw_auto_clear or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    else:
        ops_intake_auto_clear_stale_feedback_enabled = cfg["DB_PATH"] != ':memory:' and str(os.getenv('OPS_INTAKE_AUTO_CLEAR_STALE_FEEDBACK_ENABLED') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'}
    ops_intake_auto_clear_stale_feedback_poll_interval_seconds = (
        cfg.get('OPS_INTAKE_AUTO_CLEAR_STALE_FEEDBACK_POLL_INTERVAL_SECONDS')
        or os.getenv('OPS_INTAKE_AUTO_CLEAR_STALE_FEEDBACK_POLL_INTERVAL_SECONDS')
        or 300
    )
    ops_intake_auto_clear_stale_feedback_threshold_minutes = (
        cfg.get('OPS_INTAKE_AUTO_CLEAR_STALE_FEEDBACK_THRESHOLD_MINUTES')
        or os.getenv('OPS_INTAKE_AUTO_CLEAR_STALE_FEEDBACK_THRESHOLD_MINUTES')
        or 120
    )
    require_invite_code = (
        bool(cfg.get('REQUIRE_INVITE_CODE'))
        if 'REQUIRE_INVITE_CODE' in cfg
        else cfg["DB_PATH"] != ':memory:'
    )
    crm_retry_delays_seconds = cfg.get('CRM_RETRY_DELAYS_SECONDS')
    if crm_retry_delays_seconds is None:
        raw_retry_delays = os.getenv('CRM_RETRY_DELAYS_SECONDS')
        if raw_retry_delays:
            crm_retry_delays_seconds = [part.strip() for part in str(raw_retry_delays).split(',') if str(part).strip()]
    crm_retry_max_attempts = cfg.get('CRM_RETRY_MAX_ATTEMPTS') or os.getenv('CRM_RETRY_MAX_ATTEMPTS') or 3
    bind_retry_max_attempts = cfg.get('BIND_RETRY_MAX_ATTEMPTS') or os.getenv('BIND_RETRY_MAX_ATTEMPTS') or 2
    crm_login_error = None
    if crm_adapter is None and crm_base_url and crm_username and crm_password:
        live_crm_kwargs = {
            'base_url': crm_base_url,
            'username': crm_username,
            'password': crm_password,
        }
        if crm_automation_token:
            live_crm_kwargs['automation_token'] = crm_automation_token
        try:
            candidate_crm_adapter = LiveCrmAdapter(**live_crm_kwargs)
        except TypeError:
            live_crm_kwargs.pop('automation_token', None)
            candidate_crm_adapter = LiveCrmAdapter(**live_crm_kwargs)
        crm_adapter = candidate_crm_adapter
        try:
            candidate_crm_adapter.login()
        except Exception as exc:
            crm_login_error = str(exc)
            print(f'CRM login degraded at startup: {crm_login_error}')
    if ocr_adapter is None and ((cfg.get('ENABLE_RAPIDOCR') is True) or str(cfg.get('ENABLE_RAPIDOCR') or os.getenv('ENABLE_RAPIDOCR') or '').strip().lower() in {'1', 'true', 'yes', 'on'}):
        ocr_adapter = RapidOcrAdapter()
    normalized_registration_group_executor_kind = str(registration_group_approval_executor_kind or '').strip().lower()
    if registration_group_approval_executor is None and normalized_registration_group_executor_kind == 'live_whatsapp':
        from app.registration_group_executor import LiveWarmWhatsAppRegistrationGroupApprovalExecutor
        registration_group_initial_wait_ms = int(cfg.get('WHATSAPP_INITIAL_WAIT_MS') or os.getenv('WHATSAPP_INITIAL_WAIT_MS') or 500)
        registration_group_navigation_wait_ms = int(cfg.get('WHATSAPP_NAVIGATION_WAIT_MS') or os.getenv('WHATSAPP_NAVIGATION_WAIT_MS') or 120)
        registration_group_post_click_wait_ms = int(cfg.get('WHATSAPP_POST_CLICK_WAIT_MS') or os.getenv('WHATSAPP_POST_CLICK_WAIT_MS') or 80)
        registration_group_verify_timeout_ms = int(cfg.get('WHATSAPP_VERIFY_TIMEOUT_MS') or os.getenv('WHATSAPP_VERIFY_TIMEOUT_MS') or 1200)
        registration_group_verify_poll_ms = int(cfg.get('WHATSAPP_VERIFY_POLL_MS') or os.getenv('WHATSAPP_VERIFY_POLL_MS') or 80)
        registration_group_strict_reload_verify = str(cfg.get('WHATSAPP_STRICT_RELOAD_VERIFY') or os.getenv('WHATSAPP_STRICT_RELOAD_VERIFY') or 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
        registration_group_approval_executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(
            chrome_user_data_root=cfg.get('WHATSAPP_CHROME_USER_DATA_ROOT') or os.getenv('WHATSAPP_CHROME_USER_DATA_ROOT') or cfg.get('CHROME_USER_DATA_ROOT') or os.getenv('CHROME_USER_DATA_ROOT'),
            profile_dir=cfg.get('WHATSAPP_PROFILE_DIR') or os.getenv('WHATSAPP_PROFILE_DIR') or 'Profile 25',
            registration_list_item_index=int(cfg.get('WHATSAPP_REGISTRATION_LIST_ITEM_INDEX') or os.getenv('WHATSAPP_REGISTRATION_LIST_ITEM_INDEX') or 0),
            registration_group_name=cfg.get('WHATSAPP_REGISTRATION_GROUP_NAME') or os.getenv('WHATSAPP_REGISTRATION_GROUP_NAME') or '8️⃣5️⃣',
            temp_user_data_dir=cfg.get('WHATSAPP_REGISTRATION_APPROVAL_TEMP_DIR') or os.getenv('WHATSAPP_REGISTRATION_APPROVAL_TEMP_DIR') or '/tmp/chrome-whatsapp-registration-group-approval',
            initial_wait_ms=registration_group_initial_wait_ms,
            navigation_wait_ms=registration_group_navigation_wait_ms,
            post_click_wait_ms=registration_group_post_click_wait_ms,
            verify_timeout_ms=registration_group_verify_timeout_ms,
            verify_poll_ms=registration_group_verify_poll_ms,
            strict_reload_verify=registration_group_strict_reload_verify,
        )
    if registration_group_approval_executor is None and normalized_registration_group_executor_kind == 'webjs_bridge':
        from app.registration_group_webjs_executor import WebjsBridgeRegistrationGroupApprovalExecutor
        registration_group_approval_executor = WebjsBridgeRegistrationGroupApprovalExecutor(
            base_url=cfg.get('REGISTRATION_GROUP_APPROVAL_WEBJS_BASE_URL') or os.getenv('REGISTRATION_GROUP_APPROVAL_WEBJS_BASE_URL') or '',
            token=cfg.get('REGISTRATION_GROUP_APPROVAL_WEBJS_TOKEN') or os.getenv('REGISTRATION_GROUP_APPROVAL_WEBJS_TOKEN'),
            timeout_seconds=float(cfg.get('REGISTRATION_GROUP_APPROVAL_WEBJS_TIMEOUT_SECONDS') or os.getenv('REGISTRATION_GROUP_APPROVAL_WEBJS_TIMEOUT_SECONDS') or 35),
        )
    if official_group_approval_executor is None and str(official_group_approval_executor_kind or '').strip().lower() == 'webhook' and official_group_approval_webhook_url:
        from app.official_group_executor import WebhookOfficialGroupApprovalExecutor
        official_group_approval_executor = WebhookOfficialGroupApprovalExecutor(
            webhook_url=official_group_approval_webhook_url,
            token=official_group_approval_webhook_token,
            session=official_group_approval_webhook_session,
            timeout_seconds=float(official_group_approval_webhook_timeout_seconds or 20),
        )
    auto_lark_reply = cfg.get('AUTO_LARK_REPLY', True)
    if auto_lark_reply and lark_reply_adapter is None:
        if lark_reply_adapter_kind in {'cli', 'lark-cli', 'lark_cli'}:
            lark_reply_adapter = LarkCliReplyAdapter(
                cli_bin=cfg.get('LARK_CLI_BIN') or os.getenv('LARK_CLI_BIN') or 'lark-cli',
                as_identity=cfg.get('LARK_CLI_AS') or os.getenv('LARK_CLI_AS') or 'bot',
                timeout_seconds=float(cfg.get('LARK_CLI_TIMEOUT_SECONDS') or os.getenv('LARK_CLI_TIMEOUT_SECONDS') or 15),
            )
        elif app_id and app_secret:
            lark_reply_adapter = LiveLarkReplyAdapter(app_id=app_id, app_secret=app_secret, domain=app_domain)
    group_atmosphere_candidate_translator = cfg.get('GROUP_ATMOSPHERE_CANDIDATE_TRANSLATOR')
    if group_atmosphere_candidate_translator is None:
        translator_provider = str(cfg.get('GROUP_ATMOSPHERE_TRANSLATOR_PROVIDER') or os.getenv('GROUP_ATMOSPHERE_TRANSLATOR_PROVIDER') or '').strip().lower()
        if translator_provider == 'google':
            group_atmosphere_candidate_translator = GoogleTranslateCandidateTranslator(
                base_url=str(cfg.get('GROUP_ATMOSPHERE_TRANSLATOR_BASE_URL') or os.getenv('GROUP_ATMOSPHERE_TRANSLATOR_BASE_URL') or '').strip(),
                timeout_seconds=float(cfg.get('GROUP_ATMOSPHERE_TRANSLATOR_TIMEOUT_SECONDS') or os.getenv('GROUP_ATMOSPHERE_TRANSLATOR_TIMEOUT_SECONDS') or 20),
            )
        elif translator_provider == 'libretranslate':
            group_atmosphere_candidate_translator = LibreTranslateCandidateTranslator(
                base_url=str(cfg.get('LIBRETRANSLATE_BASE_URL') or os.getenv('LIBRETRANSLATE_BASE_URL') or '').strip(),
                api_key=str(cfg.get('LIBRETRANSLATE_API_KEY') or os.getenv('LIBRETRANSLATE_API_KEY') or '').strip(),
                timeout_seconds=float(cfg.get('GROUP_ATMOSPHERE_TRANSLATOR_TIMEOUT_SECONDS') or os.getenv('GROUP_ATMOSPHERE_TRANSLATOR_TIMEOUT_SECONDS') or 20),
            )
        else:
            translator_api_key = str(cfg.get('GROUP_ATMOSPHERE_TRANSLATOR_API_KEY') or os.getenv('GROUP_ATMOSPHERE_TRANSLATOR_API_KEY') or os.getenv('OPENAI_API_KEY') or '').strip()
            if translator_api_key:
                group_atmosphere_candidate_translator = GroupAtmosphereAiTranslator(
                    api_key=translator_api_key,
                    base_url=str(cfg.get('GROUP_ATMOSPHERE_TRANSLATOR_BASE_URL') or os.getenv('GROUP_ATMOSPHERE_TRANSLATOR_BASE_URL') or os.getenv('OPENAI_BASE_URL') or '').strip(),
                    model=str(cfg.get('GROUP_ATMOSPHERE_TRANSLATOR_MODEL') or os.getenv('GROUP_ATMOSPHERE_TRANSLATOR_MODEL') or '').strip(),
                    timeout_seconds=float(cfg.get('GROUP_ATMOSPHERE_TRANSLATOR_TIMEOUT_SECONDS') or os.getenv('GROUP_ATMOSPHERE_TRANSLATOR_TIMEOUT_SECONDS') or 20),
                )
    translation_background_setting = cfg.get('GROUP_ATMOSPHERE_TRANSLATION_BACKGROUND_ENABLED')
    if translation_background_setting is None:
        translation_background_setting = os.getenv('GROUP_ATMOSPHERE_TRANSLATION_BACKGROUND_ENABLED') or 'false'
    service = Service(
        db,
        crm_adapter=crm_adapter,
        ocr_adapter=ocr_adapter,
        lark_media_adapter=lark_media_adapter,
        lark_reply_adapter=lark_reply_adapter,
        lark_reply_adapter_by_app_id=lark_reply_adapter_by_app_id,
        media_cache_dir=media_cache_dir,
        lark_default_app_name=lark_default_app_name,
        lark_default_dept_name=lark_default_dept_name,
        current_lark_app_id=app_id,
        auto_bind_simulation=auto_bind_simulation,
        bind_simulator=bind_simulator,
        real_bind_executor=real_bind_executor,
        registration_group_approval_executor=registration_group_approval_executor,
        official_group_approval_executor=official_group_approval_executor,
        timo_guild_executor=timo_guild_executor,
        official_group_target_map=official_group_target_map,
        auto_bind_simulation_success_rate=auto_bind_simulation_success_rate,
        auto_bind_simulation_seed=auto_bind_simulation_seed,
        crm_base_url=crm_base_url,
        crm_username=crm_username,
        crm_login_error=crm_login_error,
        ingress_async_default=ingress_async_default,
        ingress_worker_enabled=ingress_worker_enabled,
        ingress_worker_poll_interval=ingress_worker_poll_interval,
        ingress_worker_count=ingress_worker_count,
        ingress_rate_limit_per_minute=ingress_rate_limit_per_minute,
        external_call_rate_limit_per_minute=external_call_rate_limit_per_minute,
        require_invite_code=require_invite_code,
        crm_retry_delays_seconds=crm_retry_delays_seconds,
        crm_retry_max_attempts=int(crm_retry_max_attempts or 3),
        bind_retry_max_attempts=int(bind_retry_max_attempts or 2),
        official_group_approval_webhook_url=official_group_approval_webhook_url,
        official_group_bridge_token=official_group_bridge_token,
        group_atmosphere_scheduler_enabled=group_atmosphere_scheduler_enabled,
        group_atmosphere_scheduler_poll_interval_seconds=float(group_atmosphere_scheduler_poll_interval_seconds or 30),
        group_atmosphere_candidate_translator=group_atmosphere_candidate_translator,
        group_atmosphere_translation_background_enabled=str(translation_background_setting).strip().lower() in {'1', 'true', 'yes', 'on'},
        guild_executor_proxy_region_urls=guild_executor_proxy_region_urls,
        group_atmosphere_media_dir=cfg.get('GROUP_ATMOSPHERE_MEDIA_DIR') or os.getenv('GROUP_ATMOSPHERE_MEDIA_DIR'),
        ops_intake_auto_clear_stale_feedback_enabled=ops_intake_auto_clear_stale_feedback_enabled,
        ops_intake_auto_clear_stale_feedback_poll_interval_seconds=float(ops_intake_auto_clear_stale_feedback_poll_interval_seconds or 300),
        ops_intake_auto_clear_stale_feedback_threshold_minutes=int(ops_intake_auto_clear_stale_feedback_threshold_minutes or 120),
    )
    if real_bind_executor is not None and hasattr(real_bind_executor, 'set_executor_resolver'):
        real_bind_executor.set_executor_resolver(service.resolve_guild_executor)
    try:
        service.reconcile_task_residue(force=True)
    except Exception as exc:
        print(f'Task residue reconcile degraded at startup: {exc}')
    _schedule_registration_group_executor_warmup(registration_group_approval_executor)
    print(
        json.dumps(
            {
                'startup_health': _summarize_startup_health_payload(service.runtime_health()),
            },
            ensure_ascii=False,
        )
    )
    service.ensure_current_intake_preset()
    app = FastAPI(title="MCN AI Automation")
    static_dir = Path(__file__).resolve().parent / 'static'
    if static_dir.exists():
        app.mount('/static', StaticFiles(directory=str(static_dir)), name='static')
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            'http://47.236.9.71:7819',
            'http://127.0.0.1:7819',
            'http://localhost:7819',
            'http://127.0.0.1:8011',
            'http://localhost:8011',
        ],
        allow_credentials=True,
        allow_methods=['GET', 'POST', 'PATCH', 'DELETE', 'OPTIONS'],
        allow_headers=['*'],
    )
    app.state.service = service
    app.state.approval_realtime_store = RealtimeApprovalStateStore()
    app.state.creative_pro_event_id = 0
    app.state.creative_pro_event_lock = threading.Lock()
    app.state.creative_pro_event_subscribers = set()
    app.state.auth_enabled = auth_enabled
    app.state.auth_manager = auth_manager
    app.state.auth_internal_token = auth_internal_token
    external_app_tokens_raw = cfg.get('EXTERNAL_APP_INTAKE_TOKENS') or os.getenv('EXTERNAL_APP_INTAKE_TOKENS') or {}
    external_app_default_guilds_raw = cfg.get('EXTERNAL_APP_INTAKE_DEFAULT_GUILDS') or os.getenv('EXTERNAL_APP_INTAKE_DEFAULT_GUILDS') or {}
    external_app_allowed_guilds_raw = cfg.get('EXTERNAL_APP_INTAKE_ALLOWED_GUILDS') or os.getenv('EXTERNAL_APP_INTAKE_ALLOWED_GUILDS') or {}
    external_app_allowed_app_guilds_raw = cfg.get('EXTERNAL_APP_INTAKE_ALLOWED_APP_GUILDS') or os.getenv('EXTERNAL_APP_INTAKE_ALLOWED_APP_GUILDS') or {}
    def _coerce_external_mapping(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        try:
            parsed = json.loads(str(value or '{}'))
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    external_app_tokens = _coerce_external_mapping(external_app_tokens_raw)
    external_app_default_guilds = _coerce_external_mapping(external_app_default_guilds_raw)
    external_app_allowed_guilds = _coerce_external_mapping(external_app_allowed_guilds_raw)
    external_app_allowed_app_guilds = _coerce_external_mapping(external_app_allowed_app_guilds_raw)
    def _coerce_external_guild_list(value: Any) -> List[str]:
        if isinstance(value, str):
            return [part.strip() for part in value.split(',') if part.strip()]
        return [str(part or '').strip() for part in list(value or []) if str(part or '').strip()]

    external_app_sources: Dict[str, Dict[str, Any]] = {}
    for source_name, token_value in external_app_tokens.items():
        source_key = str(source_name or '').strip()
        token = str(token_value or '').strip()
        if not source_key or not token:
            continue
        allowed_raw = external_app_allowed_guilds.get(source_key) or []
        allowed_guilds = _coerce_external_guild_list(allowed_raw)
        app_guilds: Dict[str, List[str]] = {}
        app_guilds_raw = external_app_allowed_app_guilds.get(source_key) or {}
        if isinstance(app_guilds_raw, str):
            app_guilds_raw = _coerce_external_mapping(app_guilds_raw)
        if isinstance(app_guilds_raw, dict):
            for app_name, guild_values in app_guilds_raw.items():
                try:
                    app_slug = service._normalize_external_product_app(str(app_name or '').strip())
                except HTTPException:
                    continue
                guild_values_list = _coerce_external_guild_list(guild_values)
                if guild_values_list:
                    app_guilds[app_slug] = guild_values_list
        external_app_sources[source_key] = {
            'source': source_key,
            'token': token,
            'default_guild': str(external_app_default_guilds.get(source_key) or 'Carote').strip() or 'Carote',
            'allowed_guilds': allowed_guilds,
            'app_guilds': app_guilds,
        }
    app.state.external_app_sources = external_app_sources

    def _external_app_http_exception(exc: HTTPException) -> HTTPException:
        if isinstance(exc.detail, dict):
            return exc
        detail = {'ok': False, 'reason': str(exc.detail or 'external_app_error'), 'message': str(exc.detail or 'external_app_error')}
        return HTTPException(status_code=exc.status_code, detail=detail)

    def _require_external_app_source(request: Request, payload_source: Optional[str] = None) -> Dict[str, Any]:
        source = str(payload_source or request.headers.get('X-Source') or '').strip()
        if not source:
            raise HTTPException(status_code=401, detail={'ok': False, 'reason': 'missing_source', 'message': 'missing source'})
        config = external_app_sources.get(source)
        if not config:
            raise HTTPException(status_code=401, detail={'ok': False, 'reason': 'unknown_source', 'message': 'unknown source'})
        auth_header = str(request.headers.get('Authorization') or '').strip()
        prefix = 'Bearer '
        provided = auth_header[len(prefix):].strip() if auth_header.startswith(prefix) else ''
        if not provided or not hmac.compare_digest(provided, str(config.get('token') or '')):
            raise HTTPException(status_code=401, detail={'ok': False, 'reason': 'unauthorized', 'message': 'unauthorized'})
        return config

    def _require_timo_external_feed(request: Request) -> None:
        if not timo_external_feed_token:
            raise HTTPException(status_code=503, detail={'ok': False, 'reason': 'timo_external_feed_token_not_configured'})
        auth_header = str(request.headers.get('Authorization') or '').strip()
        prefix = 'Bearer '
        provided = auth_header[len(prefix):].strip() if auth_header.startswith(prefix) else ''
        if not provided or not hmac.compare_digest(provided, timo_external_feed_token):
            raise HTTPException(status_code=401, detail={'ok': False, 'reason': 'unauthorized'})

    def _require_newcomer_external_feed(request: Request) -> None:
        if not newcomer_external_feed_token:
            raise HTTPException(
                status_code=503,
                detail={'ok': False, 'reason': 'newcomer_external_feed_token_not_configured'},
            )
        auth_header = str(request.headers.get('Authorization') or '').strip()
        provided = auth_header[7:].strip() if auth_header.startswith('Bearer ') else ''
        if not provided or not hmac.compare_digest(provided, newcomer_external_feed_token):
            raise HTTPException(status_code=401, detail={'ok': False, 'reason': 'unauthorized'})

    def _external_response_or_raise(fn):
        try:
            return fn()
        except HTTPException as exc:
            raise _external_app_http_exception(exc) from exc

    def _is_internal_request(request: Request) -> bool:
        if not auth_enabled:
            return False
        if not auth_internal_token:
            return False
        provided = str(request.headers.get(OPS_AUTH_INTERNAL_HEADER) or '').strip()
        return bool(provided) and hmac.compare_digest(provided, auth_internal_token)

    def _request_session_user(
        request: Request,
        *,
        refresh_activity: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if not auth_enabled:
            return {
                'user_id': 'local-dev',
                'username': 'local-dev',
                'display_name': 'local-dev',
                'role': OPS_AUTH_ROLE_ADMIN,
                'enabled': True,
            }
        if _is_internal_request(request):
            return {
                'user_id': 'internal-system',
                'username': 'internal-system',
                'display_name': 'internal-system',
                'role': OPS_AUTH_ROLE_INTERNAL,
                'enabled': True,
            }
        cached = getattr(request.state, 'ops_user', None)
        if cached is not None:
            return cached
        raw_token = request.cookies.get(auth_manager.cookie_name)
        effective_refresh = refresh_activity and not bool(
            getattr(request.state, 'suppress_ops_session_activity', False)
        )
        user = auth_manager.session_user(
            raw_token,
            refresh_activity=effective_refresh,
        )
        request.state.ops_user = user
        return user

    ops_hot_read_cache_lock = threading.Lock()
    ops_hot_read_cache: Dict[str, Tuple[float, Any]] = {}
    ops_hot_read_cache_inflight: Dict[str, threading.Event] = {}
    ops_hot_read_cache_enabled = (
        service.db.db_path != ':memory:'
        and str(os.getenv('OPS_HOT_READ_CACHE_DISABLED') or '').strip().lower() not in {'1', 'true', 'yes', 'on'}
    )

    def _ops_hot_cache_user_suffix(user: Optional[Dict[str, Any]]) -> str:
        if not user:
            return 'anonymous'
        role = str(user.get('role') or '').strip().lower() or 'unknown'
        user_id = str(user.get('user_id') or user.get('username') or '').strip() or 'unknown'
        return f'{role}:{user_id}'

    def _ops_hot_read_cache_get(key: str) -> Optional[Any]:
        if not ops_hot_read_cache_enabled:
            return None
        now = time.monotonic()
        with ops_hot_read_cache_lock:
            cached = ops_hot_read_cache.get(key)
            if not cached:
                return None
            expires_at, payload = cached
            if expires_at <= now:
                ops_hot_read_cache.pop(key, None)
                return None
            return copy.deepcopy(payload)

    def _ops_hot_read_cache_set(key: str, ttl_seconds: float, payload: Any) -> Any:
        if not ops_hot_read_cache_enabled:
            return payload
        ttl = max(float(ttl_seconds or 0), 0.1)
        with ops_hot_read_cache_lock:
            ops_hot_read_cache[key] = (time.monotonic() + ttl, copy.deepcopy(payload))
        return payload

    def _ops_hot_read_cache_get_or_set(
        key: str,
        ttl_seconds: float,
        builder,
        *,
        stale_ttl_seconds: float = 60.0,
    ):
        if not ops_hot_read_cache_enabled:
            return builder()
        ttl = max(float(ttl_seconds or 0), 0.1)
        stale_ttl = max(float(stale_ttl_seconds or 0), ttl)
        while True:
            now = time.monotonic()
            with ops_hot_read_cache_lock:
                cached = ops_hot_read_cache.get(key)
                stale_payload = None
                if cached:
                    expires_at, payload = cached
                    if expires_at > now:
                        return copy.deepcopy(payload)
                    if expires_at + stale_ttl > now:
                        stale_payload = copy.deepcopy(payload)
                event = ops_hot_read_cache_inflight.get(key)
                if event is None:
                    event = threading.Event()
                    ops_hot_read_cache_inflight[key] = event
                    owner = True
                    break
                owner = False
            if stale_payload is not None:
                return stale_payload
            event.wait(timeout=30.0)
            if not event.is_set():
                continue
        if not owner:
            cached = _ops_hot_read_cache_get(key)
            if cached is not None:
                return cached
            return builder()
        try:
            payload = builder()
            return _ops_hot_read_cache_set(key, ttl, payload)
        finally:
            with ops_hot_read_cache_lock:
                event = ops_hot_read_cache_inflight.pop(key, None)
                if event is not None:
                    event.set()

    def _ops_hot_read_cache_invalidate(prefix: str = '') -> None:
        if not ops_hot_read_cache_enabled:
            return
        with ops_hot_read_cache_lock:
            if not prefix:
                ops_hot_read_cache.clear()
                return
            stale_keys = [key for key in ops_hot_read_cache if key.startswith(prefix)]
            for key in stale_keys:
                ops_hot_read_cache.pop(key, None)
            stale_inflight_keys = [key for key in ops_hot_read_cache_inflight if key.startswith(prefix)]
            for key in stale_inflight_keys:
                event = ops_hot_read_cache_inflight.pop(key, None)
                if event is not None:
                    event.set()

    def _ops_request_timing_threshold_ms() -> float:
        raw = settings.get('OPS_REQUEST_TIMING_THRESHOLD_MS') if isinstance(settings, dict) else None
        if raw is None:
            raw = os.getenv('OPS_REQUEST_TIMING_THRESHOLD_MS')
        try:
            return max(float(raw), 0.0)
        except (TypeError, ValueError):
            return 500.0

    ops_request_timing_enabled = str(
        (settings.get('OPS_REQUEST_TIMING_ENABLED') if isinstance(settings, dict) else None)
        or os.getenv('OPS_REQUEST_TIMING_ENABLED')
        or 'true'
    ).strip().lower() not in {'0', 'false', 'no', 'off'}
    ops_request_timing_threshold_ms = _ops_request_timing_threshold_ms()

    def _ops_should_time_request(path: str) -> bool:
        normalized_path = str(path or '')
        return (
            normalized_path.startswith('/api/ops/')
            or normalized_path.startswith('/api/internal/')
            or normalized_path == '/ops'
            or normalized_path.startswith('/ops/')
        )

    async def _ops_timed_call_next(request: Request, call_next):
        if not ops_request_timing_enabled:
            return await call_next(request)
        path = str(request.url.path or '/')
        if not _ops_should_time_request(path):
            return await call_next(request)
        started_at = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started_at) * 1000
        if duration_ms >= ops_request_timing_threshold_ms:
            print(json.dumps({
                'event': 'ops_slow_request',
                'method': str(request.method or 'GET').upper(),
                'path': path,
                'status_code': getattr(response, 'status_code', None),
                'duration_ms': round(duration_ms, 1),
            }, ensure_ascii=False))
        return response

    def _refresh_ops_session_cookie(request: Request, response: Response) -> Response:
        if not auth_enabled:
            return response
        if bool(getattr(request.state, 'suppress_ops_session_activity', False)):
            return response
        raw_token = str(request.cookies.get(auth_manager.cookie_name) or '').strip()
        if not raw_token:
            return response
        user = getattr(request.state, 'ops_user', None)
        if not user or str((user or {}).get('role') or '').strip() == OPS_AUTH_ROLE_INTERNAL:
            return response
        auth_manager.apply_session_cookie(response, raw_token)
        return response

    async def _ops_timed_call_next_with_session_refresh(request: Request, call_next):
        response = await _ops_timed_call_next(request, call_next)
        return _refresh_ops_session_cookie(request, response)

    def _require_ops_user(
        request: Request,
        *,
        role: Optional[str] = None,
        refresh_session_activity: bool = True,
    ) -> Dict[str, Any]:
        user = _request_session_user(
            request,
            refresh_activity=refresh_session_activity,
        )
        if not user:
            raise HTTPException(status_code=401, detail='ops_auth_required')
        current_role = str(user.get('role') or '').strip().lower()
        if role == OPS_AUTH_ROLE_INTERNAL:
            if current_role != OPS_AUTH_ROLE_INTERNAL:
                raise HTTPException(status_code=403, detail='ops_internal_required')
            return user
        if current_role == OPS_AUTH_ROLE_INTERNAL:
            return user
        if role == 'ops_user':
            if current_role not in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_CUSTOMER_SERVICE, OPS_AUTH_ROLE_OPERATOR}:
                raise HTTPException(status_code=403, detail='ops_user_required')
            return user
        if role == OPS_AUTH_ROLE_SUPER_ADMIN and current_role != OPS_AUTH_ROLE_SUPER_ADMIN:
            raise HTTPException(status_code=403, detail='ops_super_admin_required')
        if role == OPS_AUTH_ROLE_ADMIN and current_role not in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN}:
            raise HTTPException(status_code=403, detail='ops_admin_required')
        if role == OPS_AUTH_ROLE_CUSTOMER_SERVICE and current_role not in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, *OPS_AUTH_BUSINESS_ROLES}:
            raise HTTPException(status_code=403, detail='ops_customer_service_required')
        if role == OPS_AUTH_ROLE_OPERATOR and current_role not in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, *OPS_AUTH_BUSINESS_ROLES}:
            raise HTTPException(status_code=403, detail='ops_operator_required')
        return user

    @app.exception_handler(MetaRateLimitBlocked)
    async def meta_rate_limit_exception_handler(
        request: Request, exc: MetaRateLimitBlocked,
    ) -> Response:
        return JSONResponse(
            status_code=429,
            headers={'Retry-After': str(exc.retry_after_seconds)},
            content={
                'detail': {
                    'code': 'META_RATE_LIMITED',
                    'message': 'Meta 调用额度正在恢复，请稍后再试。',
                    'account_id': exc.account_id,
                    'retry_after_seconds': exc.retry_after_seconds,
                }
            },
        )

    @app.exception_handler(HTTPException)
    async def ops_html_auth_exception_handler(request: Request, exc: HTTPException) -> Response:
        detail = getattr(exc, 'detail', None)
        path = str(request.url.path or '')
        if path.startswith('/ops') and not path.startswith('/api/'):
            if exc.status_code == 401 and detail == 'ops_auth_required':
                return RedirectResponse(url=f"/login?next={quote(path)}", status_code=303)
            if exc.status_code == 403 and detail == 'ops_admin_required':
                return RedirectResponse(url='/ops', status_code=303)
            if exc.status_code == 403 and detail in {'ops_customer_service_required', 'ops_operator_required'}:
                return RedirectResponse(url='/ops/group-atmosphere', status_code=303)
        if path.startswith('/api/external/app-intake/') and isinstance(detail, dict):
            return JSONResponse(status_code=exc.status_code, content=detail)
        return JSONResponse(status_code=exc.status_code, content={'detail': detail})

    def _group_atmosphere_api_required_role(path: str, method: str) -> str:
        normalized_method = str(method or 'GET').upper()
        if path == '/api/ops/group-atmosphere/scheduler/run-due':
            return OPS_AUTH_ROLE_INTERNAL
        if path.startswith('/api/ops/group-atmosphere/accounts/'):
            return OPS_AUTH_ROLE_OPERATOR
        if path == '/api/ops/group-atmosphere/accounts' and normalized_method in {'GET', 'POST'}:
            return OPS_AUTH_ROLE_OPERATOR
        return OPS_AUTH_ROLE_OPERATOR

    def _ops_api_is_public_route(path: str, method: str) -> bool:
        normalized_method = str(method or 'GET').upper()
        if path.startswith('/api/ops/auth/'):
            return True
        if path == '/api/ops/client-version' and normalized_method == 'GET':
            return True
        if path == '/api/ops/creative-pro-actions/openapi.json' and normalized_method == 'GET':
            return True
        if path in {
            '/api/ops/ad-data-dashboard/tiktok/oauth/callback',
            '/api/ops/ad-data-dashboard/tiktok/account-holder/oauth/callback',
        } and normalized_method == 'GET':
            return True
        return False

    def _ops_api_required_role(path: str, method: str) -> Optional[str]:
        normalized_method = str(method or 'GET').upper()
        if _ops_api_is_public_route(path, normalized_method):
            return None
        if path == '/api/ops/ad-data-dashboard/meta-accounts/page-eligibility' and normalized_method == 'POST':
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/task-control/'):
            return OPS_AUTH_ROLE_ADMIN
        if path in {
            '/api/ops/ad-data-dashboard/summary',
            '/api/ops/ad-data-dashboard/daily-report',
            '/api/ops/ad-data-dashboard/daily-report/export.xlsx',
            '/api/ops/ad-data-dashboard/creative-insights',
            '/api/ops/ad-data-dashboard/creative-provider-status',
            '/api/ops/ad-data-dashboard/gle-ad-coverage',
            '/api/ops/ad-data-dashboard/meta-accounts',
            '/api/ops/ad-data-dashboard/meta-rate-limit',
            '/api/ops/ad-data-dashboard/creative-images',
            '/api/ops/ad-data-dashboard/creative-preview-proxy',
            '/api/ops/ad-data-dashboard/recommendations/history',
            '/api/ops/ad-data-dashboard/recommendations/{recommendation_id}/review',
        } and normalized_method == 'GET':
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/ad-data-dashboard/creative-assets/') and normalized_method == 'GET':
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/ad-data-dashboard/creative-images/') and normalized_method == 'GET':
            return OPS_AUTH_ROLE_ADMIN
        if path == '/api/ops/ad-data-dashboard/creative-sync' and normalized_method == 'POST':
            return OPS_AUTH_ROLE_ADMIN
        if path == '/api/ops/ad-data-dashboard/creative-experiment-performance/refresh' and normalized_method == 'POST':
            return OPS_AUTH_ROLE_ADMIN
        if path == '/api/ops/ad-data-dashboard/meta-activity-sync' and normalized_method == 'POST':
            return OPS_AUTH_ROLE_ADMIN
        if path == '/api/ops/ad-data-dashboard/creative-images/generate' and normalized_method == 'POST':
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/ad-data-dashboard/creative-images/') and normalized_method == 'POST':
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/ad-data-dashboard/creative-experiments/') and normalized_method in {'GET', 'POST'}:
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/creative-pro-jobs') and normalized_method in {'GET', 'POST'}:
            return OPS_AUTH_ROLE_ADMIN
        if path == '/api/ops/creative-pro-events' and normalized_method == 'GET':
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/creative-generation-tasks') and normalized_method in {'GET', 'POST'}:
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/im-diagnostics/') and normalized_method in {'GET', 'POST'}:
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/im-diagnosis-tasks') and normalized_method in {'GET', 'POST'}:
            return OPS_AUTH_ROLE_ADMIN
        if path == '/api/ops/sqlite-observability' and normalized_method == 'GET':
            return OPS_AUTH_ROLE_ADMIN
        if path == '/api/ops/ad-data-dashboard/tugao-bind-success/sync' and normalized_method == 'POST':
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/ad-data-dashboard/recommendations/') and path.endswith('/review') and normalized_method == 'GET':
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/ad-data-dashboard/experiments') and normalized_method in {'GET', 'POST'}:
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/ad-data-dashboard/new-account-launches') and normalized_method in {'GET', 'POST'}:
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/ad-data-dashboard/meta-plans/') and normalized_method in {'GET', 'POST'}:
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/ad-data-dashboard/autonomy/') and normalized_method == 'GET':
            return OPS_AUTH_ROLE_ADMIN
        if path == '/api/ops/ad-data-dashboard/next-actions' and normalized_method == 'GET':
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/growth/') and normalized_method in {'GET', 'POST', 'PATCH', 'PUT'}:
            return OPS_AUTH_ROLE_ADMIN
        if path in {'/api/ops/mcn-region-options', '/api/ops/whatsapp-approval-area-options'} and normalized_method == 'GET':
            return 'ops_user'
        if path in {
            '/api/ops/official-group-approval-executor-health',
            '/api/ops/whatsapp-approval-accounts/overview',
            '/api/ops/whatsapp-approval-candidates/summary',
            '/api/ops/runtime-health/summary',
            '/api/ops/official-group-approval-summary/summary',
            '/api/ops/official-group-bridge-summary/summary',
            '/api/ops/whatsapp-approval-area-options',
            '/api/ops/mcn-region-options',
            '/api/ops/approval-batch-queue/summary',
            '/api/ops/next-bind-task/summary',
            '/api/ops/next-group-task/summary',
            '/api/ops/next-action/summary',
            '/api/ops/operator-notifications',
            '/api/ops/parser-quality-summary',
            '/api/ops/manual-review-queue',
            '/api/ops/bind-queue',
            '/api/ops/group-queue',
            '/api/ops/dashboard/summary',
        } and normalized_method == 'GET':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path in {
            '/api/ops/registration-group-approval-executor-health',
            '/api/ops/group-approvals/executor/health',
            '/api/ops/registration-group-approval-executor-group-state',
            '/api/ops/group-approvals/executor/target-state',
            '/api/ops/group-approvals/executor/group-metadata',
            '/api/ops/group-approvals/executor/member-lookup',
            '/api/ops/production-ops-daemon/monitor-target',
            '/api/ops/whatsapp-approval-accounts/runtime-directory',
            '/api/ops/whatsapp-approval-accounts/registration-runtime-directory',
            '/api/ops/whatsapp-approval-accounts/binding-directory',
            '/api/ops/whatsapp-approval-accounts/official-binding-directory',
        } and normalized_method == 'GET':
            return OPS_AUTH_ROLE_INTERNAL
        if path.startswith('/api/ops/group-atmosphere/'):
            return _group_atmosphere_api_required_role(path, normalized_method)
        if path.startswith('/api/ops/whatsapp-approval-accounts/') and '/runtime/internal' in path:
            return OPS_AUTH_ROLE_INTERNAL
        if path.startswith('/api/ops/whatsapp-approval-accounts/') and '/session/internal' in path:
            return OPS_AUTH_ROLE_INTERNAL
        if path.startswith('/api/ops/whatsapp-approval-accounts/') and '/truth-refresh/internal' in path:
            return OPS_AUTH_ROLE_INTERNAL
        if path.startswith('/api/ops/whatsapp-approval-accounts/') and normalized_method == 'GET':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path in {
            '/api/ops/ingress-queue',
            '/api/ops/operator-audit-log',
            '/api/ops/intake-bot-presets',
            '/api/ops/intake-bot-presets/resolve',
            '/api/ops/guild-executors',
            '/api/ops/timo-guild-executors',
            '/api/ops/sogo-guild-executors',
            '/api/ops/sugo-guild-executors',
            '/api/ops/production-ops-daemon',
            '/api/ops/whatsapp-approval-accounts',
            '/api/ops/whatsapp-approval-candidates',
            '/api/ops/registration-group-approval-batch-members',
            '/api/ops/registration-group-approval-batch-members/summary',
            '/api/ops/registration-group-approval-batch-members/export',
            '/api/ops/guild-anchor-daily-stats',
            '/api/ops/guild-executors/health',
            '/api/ops/exception-queue',
            '/api/ops/sla-summary',
        } and normalized_method == 'GET':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/guild-executors/') and normalized_method == 'GET':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/timo-guild-executors/') and normalized_method == 'GET':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path == '/api/ops/timo-guild-executors/refresh-status' and normalized_method == 'POST':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/sogo-guild-executors/') and normalized_method == 'GET':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/sugo-guild-executors/') and normalized_method == 'GET':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/timo-auth-station/') and normalized_method in {'GET', 'POST', 'DELETE'}:
            return OPS_AUTH_ROLE_SUPER_ADMIN
        if path.startswith('/api/ops/streamer-analytics/') and normalized_method in {'GET', 'POST'}:
            return OPS_AUTH_ROLE_SUPER_ADMIN
        if path.startswith('/api/ops/timo-membership-query/') and normalized_method in {'GET', 'POST'}:
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/guild-streamer-history/') and normalized_method == 'GET':
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/accounts'):
            if normalized_method == 'DELETE':
                return OPS_AUTH_ROLE_SUPER_ADMIN
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/local-intake-bot-gateway/') and normalized_method == 'POST':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/intake-bot-presets/') and normalized_method in {'POST', 'DELETE'}:
            return OPS_AUTH_ROLE_ADMIN
        if path in {
            '/api/ops/registration-group-approval-executor-warmup',
            '/api/ops/group-approvals/executor/warmup',
            '/api/ops/official-group-approval-batches/run-ready',
            '/api/ops/group-approvals/batches/run-ready',
        }:
            return OPS_AUTH_ROLE_INTERNAL
        if path == '/api/ops/ingress-queue/run-next':
            return OPS_AUTH_ROLE_INTERNAL
        if path == '/api/ops/approval-batches/evaluate':
            return OPS_AUTH_ROLE_INTERNAL
        if path in {
            '/api/ops/runtime-health',
            '/api/ops/official-group-approval-summary',
            '/api/ops/official-group-bridge-summary',
            '/api/ops/production-ops-daemon',
            '/api/ops/approval-batch-queue',
            '/api/ops/next-bind-task',
            '/api/ops/next-group-task',
            '/api/ops/next-action',
            '/api/ops/whatsapp-approval-area-options',
        }:
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path == '/api/ops/mcn-region-options':
            if normalized_method in {'PUT', 'POST', 'DELETE'}:
                return OPS_AUTH_ROLE_ADMIN
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/operator-notifications/') and normalized_method == 'POST':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/manual-review/') and normalized_method == 'POST':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/guild-executors/') and normalized_method in {'POST', 'DELETE'}:
            return OPS_AUTH_ROLE_ADMIN
        if path == '/api/ops/guild-anchor-daily-stats' and normalized_method in {'GET', 'POST'}:
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/timo-guild-executors/') and normalized_method in {'POST', 'DELETE'}:
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/sogo-guild-executors/') and normalized_method in {'POST', 'DELETE'}:
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/sugo-guild-executors/') and normalized_method in {'POST', 'DELETE'}:
            return OPS_AUTH_ROLE_ADMIN
        if path.startswith('/api/ops/whatsapp-approval-accounts/') and normalized_method in {'POST', 'DELETE'}:
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/operation-tasks/') and normalized_method == 'GET':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/intake-workbench/'):
            if path.endswith('/assignees') and normalized_method == 'POST':
                return OPS_AUTH_ROLE_ADMIN
            if path.endswith('/bind-failed-items/clear') and normalized_method == 'POST':
                return OPS_AUTH_ROLE_ADMIN
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/timo-intake/'):
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/sogo-intake/'):
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/sugo-intake/'):
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path == '/api/ops/intake-submit' and normalized_method == 'POST':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/submissions/') and normalized_method == 'POST':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        if path.startswith('/api/ops/leads/') and normalized_method == 'POST':
            return OPS_AUTH_ROLE_CUSTOMER_SERVICE
        return None

    def _assert_ops_api_permissions_complete(app: FastAPI) -> None:
        unmapped: List[str] = []
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            path = str(route.path or '')
            if not path.startswith('/api/ops/') or path.startswith('/api/ops/auth/'):
                continue
            methods = sorted(str(method).upper() for method in (route.methods or set()) if str(method).upper() not in {'HEAD', 'OPTIONS'})
            for method in methods:
                if _ops_api_is_public_route(path, method):
                    continue
                required_role = _ops_api_required_role(path, method)
                if required_role is None:
                    unmapped.append(f'{method} {path}')
        if unmapped:
            raise RuntimeError('unmapped_ops_api_routes: ' + ', '.join(sorted(unmapped)))

    def _login_redirect_target(request: Request) -> str:
        path = str(request.url.path or '/ops')
        query = str(request.url.query or '').strip()
        if query:
            path = f'{path}?{query}'
        return f'/login?next={quote(path, safe="/?=&")}'

    def _same_origin_request(request: Request) -> bool:
        origin = str(request.headers.get('origin') or '').strip()
        if not origin:
            return True
        try:
            parsed_origin = urlparse(origin)
        except Exception:
            return False
        request_host = str(request.headers.get('host') or request.url.netloc or '').strip().lower()
        origin_host = str(parsed_origin.netloc or '').strip().lower()
        return bool(origin_host) and origin_host == request_host

    def _requires_internal_api_token(path: str, method: str) -> bool:
        normalized_method = str(method or 'GET').upper()
        if path.startswith('/api/internal/'):
            return True
        if normalized_method not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
            return False
        exact_internal_paths = {
            '/api/leads/upsert',
            '/api/events/collect',
            '/api/tasks/create',
            '/api/crm/customer-sync',
            '/api/account-submissions',
        }
        if path in exact_internal_paths:
            return True
        internal_prefixes = (
            '/api/tasks/',
            '/api/intake/',
            '/api/leads/',
            '/api/group-approvals/',
            '/api/registration-groups/',
            '/api/official-groups/',
        )
        return any(path.startswith(prefix) for prefix in internal_prefixes)

    def _allow_ops_session_for_internal_protected_api(request: Request) -> bool:
        session_user = _request_session_user(request)
        if not session_user:
            return False
        role = normalize_ops_role(session_user.get('role'))
        if role not in OPS_AUTH_ALLOWED_ROLES:
            return False
        if request.method.upper() in {'POST', 'PUT', 'PATCH', 'DELETE'} and not _same_origin_request(request):
            return False
        return True

    @app.middleware('http')
    async def ops_auth_middleware(request: Request, call_next):
        path = request.url.path or '/'
        request.state.suppress_ops_session_activity = (
            request.method.upper() == 'GET'
            and path in {
                '/api/ops/streamer-analytics/summary',
                '/api/ops/streamer-analytics/weekly-roi',
                '/api/ops/streamer-analytics/roi-policies',
            }
        )
        public_paths = {'/health', '/login', '/api/ops/auth/login', '/api/ops/auth/logout', '/api/ops/auth/status', '/api/ops/auth/bootstrap'}
        if (not auth_enabled) or path in public_paths or _ops_api_is_public_route(path, request.method):
            return await _ops_timed_call_next(request, call_next)
        if path.startswith('/api/internal/'):
            try:
                _require_ops_user(request, role=OPS_AUTH_ROLE_INTERNAL)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})
            return await _ops_timed_call_next_with_session_refresh(request, call_next)
        if _requires_internal_api_token(path, request.method):
            try:
                _require_ops_user(request, role=OPS_AUTH_ROLE_INTERNAL)
            except HTTPException as exc:
                if _allow_ops_session_for_internal_protected_api(request):
                    return await _ops_timed_call_next_with_session_refresh(request, call_next)
                return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})
            return await _ops_timed_call_next_with_session_refresh(request, call_next)
        if path.startswith('/api/ops/'):
            if path.startswith('/api/ops/auth/'):
                return await _ops_timed_call_next(request, call_next)
            sensitive_methods = {'POST', 'PUT', 'PATCH', 'DELETE'}
            required_role = _ops_api_required_role(path, request.method)
            if required_role is None:
                return JSONResponse(status_code=403, content={'detail': 'ops_api_permission_unmapped'})
            try:
                user = _require_ops_user(request, role=required_role)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})
            if request.method.upper() in sensitive_methods and str(user.get('role') or '') != OPS_AUTH_ROLE_INTERNAL and not _same_origin_request(request):
                return JSONResponse(status_code=403, content={'detail': 'ops_csrf_origin_forbidden'})
            return await _ops_timed_call_next_with_session_refresh(request, call_next)
        if path == '/ops' or path.startswith('/ops/'):
            user = _request_session_user(request)
            if not user:
                return RedirectResponse(url=_login_redirect_target(request), status_code=307)
        return await _ops_timed_call_next_with_session_refresh(request, call_next)

    def _ops_login_page_html() -> str:
        if auth_manager.has_users():
            return """<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>后台登录</title>
  <style>
    body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; background:#f4f7fb; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; color:#142033; }
    .card { width:min(420px, calc(100vw - 32px)); background:#fff; border:1px solid #dbe4f0; border-radius:18px; padding:24px; box-shadow:0 10px 28px rgba(15,23,42,.08); }
    h1 { margin:0 0 10px; font-size:28px; }
    p { margin:0 0 16px; color:#5d6b82; font-size:13px; line-height:1.6; }
    label { display:block; margin-bottom:12px; }
    .label { font-size:12px; color:#475569; margin-bottom:6px; }
    input { width:100%; box-sizing:border-box; min-height:42px; padding:10px 12px; border:1px solid #cbd5e1; border-radius:10px; font-size:14px; }
    button { width:100%; min-height:42px; border:none; border-radius:10px; background:#2563eb; color:#fff; font-weight:600; cursor:pointer; }
    .error { color:#b91c1c; font-size:13px; min-height:20px; margin-top:10px; }
  </style>
</head>
<body>
  <div class=\"card\">
    <h1>后台登录</h1>
    <p id=\"loginHint\">请输入后台账号和密码。</p>
    <form id=\"loginForm\" onsubmit=\"submitLogin(event)\">
      <label><div class=\"label\">账号</div><input id=\"loginUsername\" autocomplete=\"username\" /></label>
      <label><div class=\"label\">密码</div><input id=\"loginPassword\" type=\"password\" autocomplete=\"current-password\" /></label>
      <button type=\"submit\">登录</button>
    </form>
    <div id=\"authError\" class=\"error\"></div>
  </div>
  <script>
    const nextUrl = new URLSearchParams(window.location.search).get('next') || '/ops';
    const adminOnlyNextTargets = [];
    function safeNextUrlForRole(role) {
      const normalizedRole = String(role || '').trim().toLowerCase();
      const target = String(nextUrl || '/ops');
      if (['operator', 'customer_service'].includes(normalizedRole)) {
        const allowedBusinessTargets = ['/ops/intake-submit', '/ops/timo-membership-query', '/ops/production-ops', '/ops/registration-group-approval-batch-members', '/ops/group-atmosphere', '/ops/accounts'];
        return allowedBusinessTargets.some((path) => target === path || target.startsWith(`${path}?`)) ? target : '/ops/intake-submit';
      }
      if (!['admin', 'super_admin'].includes(normalizedRole) && adminOnlyNextTargets.some((path) => target === path || target.startsWith(`${path}?`))) {
        return '/ops';
      }
      return target || '/ops';
    }
    async function fetchStatus() {
      const res = await fetch('/api/ops/auth/status', { credentials: 'same-origin' });
      const data = await res.json();
      if (data.authenticated) {
        window.location.replace(safeNextUrlForRole(data.user && data.user.role));
      }
    }
    async function submitLogin(event) {
      event.preventDefault();
      const res = await fetch('/api/ops/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          username: document.getElementById('loginUsername').value,
          password: document.getElementById('loginPassword').value,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        document.getElementById('authError').textContent = data.detail || '登录失败';
        return;
      }
      window.location.replace(safeNextUrlForRole(data.user && data.user.role));
    }
    fetchStatus();
  </script>
</body>
</html>"""
        return """<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>后台初始化</title>
  <style>
    body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; background:#f4f7fb; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; color:#142033; }
    .card { width:min(420px, calc(100vw - 32px)); background:#fff; border:1px solid #dbe4f0; border-radius:18px; padding:24px; box-shadow:0 10px 28px rgba(15,23,42,.08); }
    h1 { margin:0 0 10px; font-size:28px; }
    p { margin:0 0 16px; color:#5d6b82; font-size:13px; line-height:1.6; }
    label { display:block; margin-bottom:12px; }
    .label { font-size:12px; color:#475569; margin-bottom:6px; }
    input { width:100%; box-sizing:border-box; min-height:42px; padding:10px 12px; border:1px solid #cbd5e1; border-radius:10px; font-size:14px; }
    button { width:100%; min-height:42px; border:none; border-radius:10px; background:#2563eb; color:#fff; font-weight:600; cursor:pointer; }
    .error { color:#b91c1c; font-size:13px; min-height:20px; margin-top:10px; }
  </style>
</head>
<body>
  <div class=\"card\">
    <h1>后台初始化</h1>
    <p id=\"loginHint\">当前还没有后台账号，请创建第一个超级管理员账号。</p>
    <form id=\"bootstrapForm\" onsubmit=\"submitBootstrap(event)\">
      <label><div class=\"label\">账号</div><input id=\"bootstrapUsername\" autocomplete=\"username\" /></label>
      <label><div class=\"label\">显示名</div><input id=\"bootstrapDisplayName\" /></label>
      <label><div class=\"label\">密码</div><input id=\"bootstrapPassword\" type=\"password\" autocomplete=\"new-password\" /></label>
      <button type=\"submit\">创建并登录</button>
    </form>
    <div id=\"authError\" class=\"error\"></div>
  </div>
  <script>
    const nextUrl = new URLSearchParams(window.location.search).get('next') || '/ops';
    async function submitBootstrap(event) {
      event.preventDefault();
      const res = await fetch('/api/ops/auth/bootstrap', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          username: document.getElementById('bootstrapUsername').value,
          display_name: document.getElementById('bootstrapDisplayName').value,
          password: document.getElementById('bootstrapPassword').value,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        document.getElementById('authError').textContent = data.detail || '初始化失败';
        return;
      }
      window.location.replace(nextUrl || '/ops');
    }
  </script>
</body>
</html>"""


    def _ops_accounts_page_html(role: str) -> str:
        if str(role or '').strip() not in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN}:
            return """<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\" /><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" /><title>账号设置</title>
<style>
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; margin:0; padding:24px; background:#f4f7fb; color:#142033; }
.page { max-width:1280px; margin:0 auto; display:grid; gap:16px; }
.nav { position:sticky; top:0; z-index:20; display:flex; gap:10px; flex-wrap:wrap; margin:0 0 18px 0; padding:12px 0 14px; background:rgba(244,247,251,.96); backdrop-filter:blur(10px); }
.nav a { color:#2563eb; text-decoration:none; font-size:13px; padding:8px 12px; border-radius:999px; background:#eef4ff; border:1px solid #d8e5ff; }
.card { background:#fff; border:1px solid #dbe4f0; border-radius:18px; padding:18px; box-shadow:0 10px 28px rgba(15,23,42,.06); margin-bottom:16px; }
h1 { margin:0 0 8px 0; font-size:30px; letter-spacing:-.02em; }
label { display:block; margin-top:12px; }
.label { color:#475569; font-size:12px; font-weight:700; margin-bottom:6px; }
input { width:100%; min-height:42px; padding:10px 12px; box-sizing:border-box; border:1px solid #cbd5e1; border-radius:10px; font-size:14px; background:#fff; }
button { min-height:40px; padding:10px 14px; border:none; border-radius:10px; background:#2563eb; color:#fff; font-weight:700; cursor:pointer; margin-top:14px; }
button.secondary { background:#334155; }
button.ghost { background:#e2e8f0; color:#334155; }
.account-actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:12px; }
.account-actions button { margin-top:0; }
.password-visibility-toggle { display:flex; align-items:center; gap:8px; margin-top:12px; color:#475569; font-size:13px; font-weight:700; }
.password-visibility-toggle input { width:auto; min-height:auto; }
.toast { position:fixed; right:24px; bottom:24px; min-width:240px; max-width:420px; background:#065f46; color:#fff; padding:12px 14px; border-radius:12px; display:none; box-shadow:0 18px 45px rgba(15,23,42,.24); z-index:50; font-size:14px; }
.toast.error { background:#991b1b; }
.status-line { min-height:20px; color:#64748b; font-size:13px; margin-top:8px; }
.status-line:empty { display:none; }
.status-line.success { color:#166534; }
.status-line.error { color:#b91c1c; }
</style></head>
<body><div class=\"page\">
  <div class=\"nav\"><a href=\"/ops\">管理员看板</a><a href=\"/ops/intake-bot-presets\">收口配置中心</a><a href=\"/ops/production-ops\">群审批控制台</a><a href=\"/ops/registration-group-approval-batch-members\">群审批留存页</a><a href=\"/ops/accounts\">账号设置</a></div>
  <div class=\"card\"><h1>账号设置</h1><div class=\"account-actions\"><button class=\"secondary\" type=\"button\" onclick=\"openChangeOwnPassword()\">修改我的密码</button><button class=\"ghost\" type=\"button\" onclick=\"logoutCurrentAccount()\">退出登录</button></div></div>
  <div class=\"card\" id=\"passwordPanel\">
    <h2>修改我的密码</h2>
    <label><div class=\"label\">当前密码</div><input id=\"currentPassword\" type=\"password\" autocomplete=\"current-password\" /></label>
    <label><div class=\"label\">新密码</div><input id=\"newPassword\" type=\"password\" autocomplete=\"new-password\" /></label>
    <label><div class=\"label\">确认新密码</div><input id=\"confirmPassword\" type=\"password\" autocomplete=\"new-password\" /></label>
    <label class=\"password-visibility-toggle\"><input id=\"passwordVisibleToggle\" type=\"checkbox\" onchange=\"togglePasswordVisibility(this.checked)\" />显示密码</label>
    <button id=\"passwordSubmitBtn\" type=\"button\" onclick=\"submitPasswordChange()\">保存新密码</button>
    <div id=\"passwordMessage\" class=\"status-line\"></div>
  </div>
</div>
<div id=\"toast\" class=\"toast\"></div>
<script>
const errorText = { invalid_current_password:'当前密码不正确', password_too_short:'密码至少 8 位', ops_auth_required:'请先登录' };
function detailText(detail, fallback='操作失败') { return errorText[String(detail || '')] || String(detail || fallback); }
function showToast(message, type='success') { const toast=document.getElementById('toast'); toast.textContent=message; toast.className=`toast ${type}`; toast.style.display='block'; clearTimeout(window.__accountToastTimer); window.__accountToastTimer=setTimeout(()=>{toast.style.display='none';},2600); }
function togglePasswordVisibility(visible) { ['currentPassword','newPassword','confirmPassword'].forEach(id => { const el=document.getElementById(id); if (el) el.type = visible ? 'text' : 'password'; }); }
function setStatus(message, type='') { const el=document.getElementById('passwordMessage'); el.textContent=message || ''; el.className=`status-line ${type}`.trim(); }
function openChangeOwnPassword() { document.getElementById('passwordPanel').scrollIntoView({behavior:'smooth', block:'start'}); }
async function logoutCurrentAccount() {
  const btn = document.activeElement && document.activeElement.tagName === 'BUTTON' ? document.activeElement : null;
  if (btn) { btn.disabled = true; btn.textContent = '退出中...'; }
  try {
    await fetch('/api/ops/auth/logout', { method:'POST', credentials:'same-origin' });
  } catch (_) {}
  window.location.replace('/login');
}
async function submitPasswordChange() {
  const current=document.getElementById('currentPassword').value;
  const next=document.getElementById('newPassword').value;
  const confirm=document.getElementById('confirmPassword').value;
  if (next.length < 8) { setStatus('新密码至少 8 位','error'); return; }
  if (next !== confirm) { setStatus('两次输入的新密码不一致','error'); return; }
  if (!current) { setStatus('请输入当前密码','error'); return; }
  setStatus('保存中...');
  const res=await fetch('/api/ops/auth/password', { method:'POST', headers:{'Content-Type':'application/json'}, credentials:'same-origin', body: JSON.stringify({current_password: current, new_password: next}) });
  let data={}; try { data=await res.json(); } catch (_) {}
  if (!res.ok) { setStatus(detailText(data.detail, '保存失败'), 'error'); showToast(detailText(data.detail, '保存失败'), 'error'); return; }
  document.getElementById('currentPassword').value=''; document.getElementById('newPassword').value=''; document.getElementById('confirmPassword').value=''; const toggle=document.getElementById('passwordVisibleToggle'); if (toggle) { toggle.checked=false; togglePasswordVisibility(false); } setStatus('密码修改成功', 'success'); showToast('密码修改成功');
}
</script></body></html>"""
        html = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>后台账号设置</title>
<style>
:root { --bg:#f4f7fb; --card:#fff; --line:#dbe4f0; --text:#142033; --muted:#64748b; --blue:#2563eb; --green:#166534; --red:#b91c1c; --amber:#92400e; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; margin:0; padding:24px; background:var(--bg); color:var(--text); }
.page { max-width:1280px; margin:0 auto; display:grid; gap:16px; }
.nav { position:sticky; top:0; z-index:20; display:flex; gap:10px; flex-wrap:wrap; margin:0 0 18px 0; padding:12px 0 14px; background:rgba(244,247,251,.96); backdrop-filter:blur(10px); }
.nav a { color:var(--blue); text-decoration:none; font-size:13px; padding:8px 12px; border-radius:999px; background:#eef4ff; border:1px solid #d8e5ff; }
.card { background:var(--card); border:1px solid var(--line); border-radius:18px; padding:14px 16px; box-shadow:0 10px 28px rgba(15,23,42,.06); margin-bottom:0; }
.page > .card { padding:14px 16px!important; margin-bottom:0!important; }
.page > .card.hero { padding:12px 16px!important; }
.card.compact-card { padding:14px 16px; margin-bottom:0; }
.hero { display:flex; justify-content:space-between; align-items:center; gap:14px; }
.hero h1 { margin:0; }
.accounts-hero { display:flex!important; justify-content:space-between!important; align-items:center!important; gap:16px!important; flex-wrap:nowrap!important; }
.accounts-hero-title { min-width:0; }
.accounts-hero-actions { margin-left:auto; display:flex; align-items:center; justify-content:flex-end; gap:10px; flex:0 0 auto; }
.accounts-hero-actions button { min-height:42px!important; height:42px!important; margin:0!important; display:inline-flex!important; align-items:center!important; justify-content:center!important; }
h1 { margin:0; font-size:28px; letter-spacing:-.02em; }
h2 { margin:0; font-size:18px; }
.muted { color:var(--muted); font-size:13px; line-height:1.45; }
.summary { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; margin:0; }
.summary-item { background:#f8fbff; border:1px solid #e2e8f0; border-radius:14px; padding:9px 12px; }
.summary-item .label { margin:0 0 3px; }
.summary-item .value { font-size:21px; font-weight:800; line-height:1.05; }
.grid { display:grid; grid-template-columns: 1.1fr 1.1fr .9fr 1.1fr auto; gap:16px!important; align-items:end; }
.page .grid { gap:16px!important; }
.page .grid input,.page .grid select { min-height:42px!important; height:42px!important; margin:0!important; }
.page .grid > div { display:flex; align-items:flex-end; }
.page .grid > div > button { min-height:42px!important; height:42px!important; margin:0!important; display:inline-flex!important; align-items:center!important; justify-content:center!important; }
label { display:block; }
.label { color:#475569; font-size:12px; font-weight:700; margin-bottom:6px; }
.hint { color:#64748b; font-size:12px; margin-top:6px; line-height:1.5; }
input, select { width:100%; min-height:42px; padding:10px 12px; box-sizing:border-box; border:1px solid #cbd5e1; border-radius:10px; font-size:14px; background:#fff; }
input:focus, select:focus { outline:none; border-color:var(--blue); box-shadow:0 0 0 3px rgba(37,99,235,.12); }
button { min-height:40px; padding:10px 14px; border:none; border-radius:10px; background:var(--blue); color:#fff; font-weight:700; cursor:pointer; white-space:nowrap; }
button.secondary { background:#334155; }
button.ghost { background:#e2e8f0; color:#334155; }
button.danger { background:#dc2626; }
button:disabled { opacity:.58; cursor:not-allowed; }
.toolbar { display:flex; gap:10px; align-items:center; justify-content:space-between; flex-wrap:wrap; margin-bottom:10px; }
.toolbar h2 { margin:0; }
.toolbar-left { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.toolbar-left input.search { min-height:42px!important; height:42px!important; margin:0!important; }
.toolbar-left button { min-height:42px!important; height:42px!important; margin:0!important; display:inline-flex!important; align-items:center!important; justify-content:center!important; }
.search { width:260px; }
.table-wrap { overflow-x:auto; padding-bottom:2px; }
table { width:100%; min-width:980px; border-collapse:collapse; font-size:13px; table-layout:fixed; }
col.col-account { width:24%; }
col.col-role { width:16%; }
col.col-status { width:12%; }
col.col-login { width:16%; }
col.col-actions { width:32%; }
th, td { padding:4px 10px; border-bottom:1px solid #e5edf6; text-align:left; vertical-align:middle; white-space:nowrap; height:36px; line-height:1; }
th { color:#475569; background:#f8fbff; font-weight:800; white-space:nowrap; }
tr:hover td { background:#fbfdff; }
.account-main, .account-sub { display:flex; align-items:center; height:32px; min-height:32px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.account-main { font-weight:800; font-size:14px; gap:8px; }
.account-name-text { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.account-sub { color:#64748b; font-size:13px; }
.current-cell, .role-cell, .status-cell, .login-cell, .actions { display:flex; align-items:center; height:32px; min-height:32px; width:100%; }
.current-cell { justify-content:center; }
.role-cell, .status-cell { justify-content:center; gap:8px; }
.role-cell select { width:126px; min-width:126px; height:32px!important; min-height:32px!important; padding:5px 8px!important; text-align:center; text-align-last:center; line-height:18px!important; transform:translateY(3px); }
.status-cell select { width:86px; min-width:86px; height:32px!important; min-height:32px!important; padding:5px 8px!important; text-align:center; text-align-last:center; line-height:18px!important; transform:translateY(3px); }
.login-cell { color:#334155; justify-content:flex-start; }
table th:nth-child(2), table td:nth-child(2), table th:nth-child(3), table td:nth-child(3) { text-align:center; }
.actions { gap:6px; flex-wrap:nowrap; justify-content:flex-start; }
.actions button { height:32px!important; min-height:32px!important; padding:5px 9px!important; margin-top:0; line-height:18px!important; display:inline-flex; align-items:center; justify-content:center; }
.badge { display:inline-flex; align-items:center; justify-content:center; gap:5px; min-height:24px; padding:3px 8px; border-radius:999px; font-size:12px; font-weight:800; line-height:18px; vertical-align:middle; }
.badge.super_admin { background:#fde68a; color:#92400e; }
.badge.admin { background:#dbeafe; color:#1d4ed8; }
.badge.customer_service { background:#dcfce7; color:#166534; }
.badge.operator { background:#fef3c7; color:#92400e; }
.badge.off { background:#fee2e2; color:#991b1b; }
.badge.pending { background:#fef3c7; color:#92400e; }
.inline-edit { display:flex; gap:8px; align-items:center; }
.inline-edit input { min-width:180px; }
.status-line { min-height:20px; color:#64748b; font-size:13px; margin-top:8px; }
.status-line:empty { display:none; }
.status-line.success { color:#166534; }
.status-line.error { color:#b91c1c; }
.toast { position:fixed; right:24px; bottom:24px; min-width:260px; max-width:420px; background:#111827; color:#fff; padding:12px 14px; border-radius:12px; display:none; box-shadow:0 18px 45px rgba(15,23,42,.24); z-index:50; font-size:14px; }
.toast.success { background:#065f46; }
.toast.error { background:#991b1b; }
.modal-backdrop { position:fixed; inset:0; background:rgba(15,23,42,.42); display:none; align-items:center; justify-content:center; padding:20px; z-index:40; }
.modal { width:min(520px,100%); background:#fff; border-radius:18px; padding:20px; box-shadow:0 24px 70px rgba(15,23,42,.28); }
.account-modal-card { width:min(520px,100%); max-height:calc(100vh - 96px); max-height:calc(100dvh - 96px); overflow:hidden; display:grid; grid-template-rows:auto minmax(0,1fr) auto; background:#fff!important; border:1px solid rgba(219,228,240,.95)!important; border-radius:20px; padding:0; box-shadow:0 24px 64px rgba(15,23,42,.24); color:var(--text); }
.account-modal-head { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; padding:18px 20px 14px; border-bottom:1px solid var(--line); background:#fff; border-radius:20px 20px 0 0; }
.account-modal-body { min-height:0; overflow-y:auto; padding:16px 20px 12px; }
.account-modal-card h2 { margin:0 0 4px 0; }
.modal-grid { display:grid; gap:12px; }
.generated-password-panel { display:none; border:1px solid #bfdbfe; background:#eff6ff; color:#1e3a8a; border-radius:12px; padding:10px 12px; font-size:13px; line-height:1.5; }
.generated-password-panel code { display:block; margin-top:6px; font-size:18px; color:#111827; user-select:all; word-break:break-all; }
.region-card { padding:18px!important; }
.region-toolbar { margin-bottom:12px!important; align-items:flex-start!important; }
.region-toolbar h2 { margin-bottom:4px!important; }
.region-table { border:1px solid #dbe4f0; border-radius:14px; overflow:hidden; background:#fff; }
.region-head, .region-row { display:grid; grid-template-columns:minmax(220px,1fr) 110px 92px 82px; align-items:center; column-gap:0; }
.region-head { min-height:38px; background:#f1f5fb; color:#475569; font-size:12px; font-weight:800; letter-spacing:0; border-bottom:1px solid #dbe4f0; }
.region-head span, .region-cell { padding:0 12px; min-width:0; }
.region-head span { display:flex; align-items:center; height:100%; transform:translateX(5px); }
.region-head span:not(:first-child) { justify-content:center; text-align:center; }
.region-head span:first-child { justify-content:flex-start; text-align:left; transform:translateX(5px); }
.region-list { display:grid; gap:0; margin-top:0; }
.region-row { min-height:52px; border-bottom:1px solid #e8eef6; background:#fff; font-size:13px; }
.region-row:last-child { border-bottom:0; }
.region-row:hover { background:#fbfdff; }
.region-main { display:flex; align-items:center; gap:10px; min-width:0; }
.region-code { width:34px; height:24px; display:inline-flex; align-items:center; justify-content:center; border-radius:8px; background:#eef4ff; color:#1d4ed8; font-size:12px; font-weight:800; flex:0 0 auto; }
.region-title { min-width:0; }
.region-name { font-weight:800; line-height:1.2; color:#0f172a; }
.region-meta { color:#64748b; font-size:12px; line-height:1.2; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.region-center { text-align:center; }
.region-toggle { justify-content:center; display:flex; align-items:center; gap:6px; color:#475569; font-size:12px; font-weight:700; }
.region-toggle input[type="checkbox"] { width:16px; min-height:16px; margin:0; }
@media (max-width: 980px) { .region-head, .region-row { grid-template-columns:minmax(190px,1fr) 86px 72px 70px; } .region-head span, .region-cell { padding:0 8px; } }
.password-visibility-toggle { display:flex; align-items:center; gap:8px; margin-top:2px; color:#475569; font-size:13px; font-weight:700; }
.password-visibility-toggle input { width:auto; min-height:auto; }
.modal-actions { display:flex; justify-content:flex-end; gap:10px; margin:0; padding:14px 20px 20px; border-top:1px solid var(--line); background:linear-gradient(180deg,rgba(255,255,255,.94),#fff 42%); border-radius:0 0 20px 20px; box-shadow:0 -12px 28px rgba(15,23,42,.06); }
@media (max-width: 900px) { .summary { grid-template-columns:repeat(2,minmax(0,1fr)); } .grid { grid-template-columns:1fr; } .hero { flex-direction:column; } .search { width:100%; } }
</style></head>
<body><div class="page">
  <div class="nav"><a href="/ops">管理员看板</a><a href="/ops/intake-bot-presets">收口配置中心</a><a href="/ops/production-ops">群审批控制台</a><a href="/ops/registration-group-approval-batch-members">群审批留存页</a><a href="/ops/group-atmosphere" data-admin-only-nav="true">群聊天助手</a><a href="/ops/accounts">账号设置</a></div>
  <div class="card hero compact-card accounts-hero">
    <div class="accounts-hero-title"><h1>后台账号设置</h1></div>
    <div class="accounts-hero-actions"><button class="secondary" type="button" onclick="openChangeOwnPassword()">修改我的密码</button><button class="ghost" type="button" onclick="logoutCurrentAccount()">退出登录</button></div>
  </div>
  <div class="summary" id="summaryCards">
    <div class="summary-item"><div class="label">总账号</div><div class="value" id="totalCount">-</div></div>
    <div class="summary-item"><div class="label">超级管理员</div><div class="value" id="superAdminCount">-</div></div>
    <div class="summary-item"><div class="label">管理员/运营</div><div class="value" id="adminCount">-</div></div>
    <div class="summary-item"><div class="label">停用</div><div class="value" id="disabledCount">-</div></div>
  </div>
  <div class="card">
    <h2>新增账号</h2>
    <div class="grid">
      <label><div class="label">账号</div><input id="username" autocomplete="off" placeholder="例如 ops01" /></label>
      <label><div class="label">显示名</div><input id="displayName" autocomplete="off" placeholder="例如 印尼运营A" /></label>
      <label><div class="label">角色</div><select id="role"><option value="operator">运营</option><option value="admin">管理员</option><option value="super_admin">超级管理员</option></select></label>
      <label><div class="label">初始密码</div><input id="password" type="password" autocomplete="new-password" placeholder="至少 8 位" /></label>
      <div><button id="createBtn" type="button" onclick="createAccount()">创建账号</button></div>
    </div>
    <div id="createMessage" class="status-line"></div>
  </div>
  <div class="card">
    <div class="toolbar">
      <div><h2>账号列表</h2></div>
      <div class="toolbar-left"><input id="accountSearch" class="search" placeholder="搜索账号/显示名" oninput="renderAccounts()" /><button class="ghost" type="button" onclick="loadAccounts()">刷新</button></div>
    </div>
    <div id="tableMessage" class="status-line"></div>
    <div class="table-wrap"><table><colgroup><col class="col-account"><col class="col-role"><col class="col-status"><col class="col-login"><col class="col-actions"></colgroup><thead><tr><th>账号</th><th>角色</th><th>状态</th><th>最近登录</th><th>操作</th></tr></thead><tbody id="rows"><tr><td colspan="5" class="muted">加载中...</td></tr></tbody></table></div>
  </div>
  <div class="card region-card" id="mcnRegionManagementCard" data-admin-region-management="true">
    <div class="toolbar region-toolbar">
      <div><h2>地区管理</h2><div class="muted">统一管理绑定中心、群审批、群聊天助手可用地区；仅管理员/超级管理员可配置。</div></div>
      <div class="toolbar-left"><button class="ghost" type="button" onclick="loadRegionOptions()">刷新</button><button id="saveRegionBtn" type="button" onclick="saveRegionOptions()">保存配置</button></div>
    </div>
    <div class="region-table" aria-label="地区管理列表">
      <div class="region-head"><span>地区</span><span>电话区号</span><span>语言</span><span>启用</span></div>
      <div id="regionRows" class="region-list"></div>
    </div>
    <div id="regionMessage" class="status-line"></div>
  </div>
</div>
<div id="displayNameModal" class="modal-backdrop" onclick="closeModalOnBackdrop(event)">
  <div class="account-modal-card ops-modal-card">
    <div class="account-modal-head ops-modal-head"><div><h2>修改显示名</h2><div class="muted">修改后立即在后台账号列表生效。</div></div><button class="ghost" type="button" onclick="closeDisplayNameModal()">关闭</button></div>
    <div class="account-modal-body ops-modal-body">
      <label><div class="label">显示名</div><input id="displayNameInput" autocomplete="off" /></label>
      <div id="displayNameMessage" class="status-line"></div>
    </div>
    <div class="modal-actions ops-modal-actions"><button class="ghost" type="button" onclick="closeDisplayNameModal()">取消</button><button id="displayNameSubmitBtn" type="button" onclick="submitDisplayNameModal()">保存显示名</button></div>
  </div>
</div>
<div id="passwordModal" class="modal-backdrop" onclick="closeModalOnBackdrop(event)">
  <div class="account-modal-card ops-modal-card">
    <div class="account-modal-head ops-modal-head"><div><h2 id="passwordModalTitle">修改密码</h2><div id="passwordModalHint" class="muted"></div></div><button class="ghost" type="button" onclick="closePasswordModal()">关闭</button></div>
    <div class="account-modal-body ops-modal-body">
      <div class="modal-grid">
        <label id="currentPasswordWrap"><div class="label">当前密码</div><input id="currentPassword" type="password" autocomplete="current-password" /></label>
        <label><div class="label">新密码</div><input id="newPassword" type="password" autocomplete="new-password" placeholder="至少 8 位" /></label>
        <label><div class="label">确认新密码</div><input id="confirmPassword" type="password" autocomplete="new-password" /></label>
        <div id="generatedPasswordPanel" class="generated-password-panel">已生成新密码<code id="generatedPasswordText"></code><button class="ghost" type="button" onclick="copyGeneratedPassword()">复制密码</button></div>
        <label class="password-visibility-toggle"><input id="passwordVisibleToggle" type="checkbox" onchange="togglePasswordVisibility(this.checked)" />显示密码</label>
      </div>
      <div id="passwordMessage" class="status-line"></div>
    </div>
    <div class="modal-actions ops-modal-actions"><button class="ghost" type="button" onclick="closePasswordModal()">取消</button><button class="ghost" id="generatePasswordBtn" type="button" onclick="generateTemporaryPassword()">生成临时密码</button><button id="passwordSubmitBtn" type="button" onclick="submitPasswordModal()">保存密码</button></div>
  </div>
</div>
<div id="toast" class="toast"></div>
<script>
let accounts = [];
let regionOptions = [];
let currentUser = null;
let passwordMode = null;
let passwordTargetUserId = null;
let displayNameTargetUserId = null;
let displayNameSourceButton = null;
const errorText = { invalid_username:'账号格式不正确', password_too_short:'密码至少 8 位', username_taken:'账号已存在', user_not_found:'账号不存在', invalid_current_password:'当前密码不正确', ops_admin_required:'需要管理员权限', ops_super_admin_required:'需要超级管理员权限', super_admin_password_protected:'管理员不能重置超级管理员密码', cannot_delete_self:'不能删除当前登录账号', ops_auth_required:'请先登录' };
function escapeHtml(value) { return window.OpsCommon ? window.OpsCommon.escapeHtml(value) : String(value ?? '').replace(/[&<>\"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char])); }
function detailText(detail, fallback='操作失败') { return errorText[String(detail || '')] || String(detail || fallback); }
function isSuperAdmin() { return currentUser && currentUser.role === 'super_admin'; }
function roleText(role) { if (role === 'super_admin') return '超级管理员'; if (role === 'admin') return '管理员'; if (role === 'customer_service') return '运营'; if (role === 'operator') return '运营'; return '运营'; }
function badgeRole(role) { return `<span class="badge ${escapeHtml(role)}">${roleText(role)}</span>`; }
function badgeEnabled(enabled) { return enabled ? '<span class="badge operator">启用</span>' : '<span class="badge off">停用</span>'; }
function formatLoginTime(value) { const raw=String(value || '').trim(); if (!raw) return '-'; return raw.replace('T',' ').replace(/\.\d+/, '').replace(/(?:Z|[+-]\d{2}:?\d{2})$/, '').slice(0,19); }
function showToast(message, type='success') { if (window.OpsCommon) return window.OpsCommon.showToast(message, type, 'toast'); const toast=document.getElementById('toast'); toast.textContent=message; toast.className=`toast ${type}`; toast.style.display='block'; clearTimeout(window.__accountToastTimer); window.__accountToastTimer=setTimeout(()=>{toast.style.display='none';},2600); }
function setStatus(id, message, type='') { const el=document.getElementById(id); el.textContent=message || ''; el.className=`status-line ${type}`.trim(); }
function setBusy(btn, busy, text) { if (!btn) return; if (busy) { btn.dataset.originalText=btn.textContent; btn.textContent=text || '处理中...'; btn.disabled=true; } else { btn.textContent=btn.dataset.originalText || btn.textContent; btn.disabled=false; } }
function togglePasswordVisibility(visible) { ['currentPassword','newPassword','confirmPassword'].forEach(id => { const el=document.getElementById(id); if (el) el.type = visible ? 'text' : 'password'; }); }
function makeTemporaryPassword() { const chars='ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'; const array=new Uint32Array(14); window.crypto.getRandomValues(array); return Array.from(array, value => chars[value % chars.length]).join(''); }
function showGeneratedPassword(value) { const panel=document.getElementById('generatedPasswordPanel'); const text=document.getElementById('generatedPasswordText'); if (!panel || !text) return; text.textContent=value || ''; panel.style.display=value ? 'block' : 'none'; }
function generateTemporaryPassword() { const value=makeTemporaryPassword(); document.getElementById('newPassword').value=value; document.getElementById('confirmPassword').value=value; const toggle=document.getElementById('passwordVisibleToggle'); if (toggle) toggle.checked=true; togglePasswordVisibility(true); showGeneratedPassword(value); setStatus('passwordMessage','已生成新密码，请复制后再保存。','success'); }
async function copyGeneratedPassword() { const text=document.getElementById('generatedPasswordText'); const value=String(text && text.textContent || ''); if (!value) return; try { await navigator.clipboard.writeText(value); showToast('密码已复制'); } catch (_) { showToast('复制失败，请手动选择复制', 'error'); } }
async function fetchJson(url, options={}) { if (window.OpsCommon) { try { return await window.OpsCommon.loadJson(url, options); } catch (err) { err.message=detailText(err.message, '请求失败'); throw err; } } const res=await fetch(url, {credentials:'same-origin', ...options}); let data={}; try { data=await res.json(); } catch (_) {} if (!res.ok) { const err=new Error(detailText(data.detail, '请求失败')); err.detail=data.detail; err.status=res.status; throw err; } return data; }
async function logoutCurrentAccount() {
  const btn = document.activeElement && document.activeElement.tagName === 'BUTTON' ? document.activeElement : null;
  if (btn) { btn.disabled = true; btn.textContent = '退出中...'; }
  try { await fetchJson('/api/ops/auth/logout', {method:'POST'}); } catch (_) {}
  window.location.replace('/login');
}
async function loadCurrentUser() { const data=await fetchJson('/api/ops/auth/status'); currentUser=data.user || null; }
async function loadAccounts() {
  setStatus('tableMessage','');
  try {
    await loadCurrentUser();
    const data = await fetchJson('/api/ops/accounts');
    accounts = Array.isArray(data.rows) ? data.rows : [];
    updateSummary(); renderAccounts(); setStatus('tableMessage', '');
  } catch (err) {
    document.getElementById('rows').innerHTML = `<tr><td colspan="5" class="status-line error">${escapeHtml(err.message || '加载失败')}</td></tr>`;
    setStatus('tableMessage', err.message || '加载失败', 'error');
  }
}
function updateSummary() {
  const superAdmin=accounts.filter(x=>x.role==='super_admin').length;
  const admin=accounts.filter(x=>x.role==='admin').length;
  const operator=accounts.filter(x=>['customer_service','operator'].includes(String(x.role||''))).length;
  const disabled=accounts.filter(x=>!x.enabled).length;
  document.getElementById('totalCount').textContent=accounts.length;
  document.getElementById('superAdminCount').textContent=superAdmin;
  document.getElementById('adminCount').textContent=`${admin}/${operator}`;
  document.getElementById('disabledCount').textContent=disabled;
}
function renderAccounts() {
  const q=String(document.getElementById('accountSearch').value || '').trim().toLowerCase();
  const rows=accounts.filter(row => !q || String(row.username || '').toLowerCase().includes(q) || String(row.display_name || '').toLowerCase().includes(q));
  if (!rows.length) { document.getElementById('rows').innerHTML = '<tr><td colspan="5" class="muted">暂无匹配账号</td></tr>'; return; }
  document.getElementById('rows').innerHTML = rows.map((row) => {
    const userIdJson=escapeHtml(JSON.stringify(row.user_id));
    const usernameJson=escapeHtml(JSON.stringify(row.username || ''));
    const displayNameJson=escapeHtml(JSON.stringify(row.display_name || ''));
    const isMe=currentUser && currentUser.user_id === row.user_id;
    const roleOptions = `<option value="operator" ${['customer_service','operator'].includes(String(row.role||'')) ? 'selected' : ''}>运营</option><option value="admin" ${row.role === 'admin' ? 'selected' : ''}>管理员</option><option value="super_admin" ${row.role === 'super_admin' ? 'selected' : ''}>超级管理员</option>`;
    const actions = [`<button class="ghost" type="button" onclick="openDisplayNameEditor(${userIdJson}, ${displayNameJson}, this)">显示名</button>`];
    if (!(row.role === 'super_admin' && !isSuperAdmin())) actions.push(`<button class="secondary" type="button" onclick="openAdminResetPassword(${userIdJson}, ${usernameJson})">重置密码</button>`);
    if (isSuperAdmin() && !isMe) actions.push(`<button class="danger" type="button" onclick="deleteAccount(${userIdJson}, ${usernameJson}, this)">删除</button>`);
    return `<tr>
      <td><div class="account-main" title="${escapeHtml(row.display_name || row.username)}"><span class="account-name-text">${escapeHtml(row.username)}</span>${isMe ? '<span class="badge pending">当前</span>' : ''}</div></td>
      <td><div class="role-cell"><select aria-label="修改角色" onchange="updateAccount(${userIdJson}, {role:this.value}, this, '角色已保存')">${roleOptions}</select></div></td>
      <td><div class="status-cell"><select aria-label="修改状态" onchange="updateAccount(${userIdJson}, {enabled:this.value==='enabled'}, this, this.value==='enabled'?'账号已启用':'账号已停用')"><option value="enabled" ${row.enabled ? 'selected' : ''}>启用</option><option value="disabled" ${!row.enabled ? 'selected' : ''}>停用</option></select></div></td>
      <td><div class="login-cell">${escapeHtml(formatLoginTime(row.last_login_at))}</div></td>
      <td><div class="actions">${actions.join('')}</div></td>
    </tr>`;
  }).join('');
}
async function createAccount() {
  const btn=document.getElementById('createBtn'); setBusy(btn,true,'创建中...'); setStatus('createMessage','创建中...');
  const payload = { username: document.getElementById('username').value.trim(), display_name: document.getElementById('displayName').value.trim(), role: document.getElementById('role').value, password: document.getElementById('password').value, enabled: true };
  try {
    await fetchJson('/api/ops/accounts', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    document.getElementById('username').value=''; document.getElementById('displayName').value=''; document.getElementById('password').value='';
    setStatus('createMessage','创建成功', 'success'); showToast('账号创建成功'); await loadAccounts();
  } catch (err) { setStatus('createMessage', err.message || '创建失败', 'error'); showToast(err.message || '创建失败', 'error'); }
  finally { setBusy(btn,false); }
}
async function updateAccount(userId, patch, control=null, successText='已保存') {
  if (control) control.disabled=true;
  setStatus('tableMessage','保存中...');
  try { await fetchJson(`/api/ops/accounts/${encodeURIComponent(userId)}`, { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(patch) }); showToast(successText); await loadAccounts(); }
  catch (err) { showToast(err.message || '更新失败', 'error'); setStatus('tableMessage', err.message || '更新失败', 'error'); await loadAccounts(); }
  finally { if (control) control.disabled=false; }
}
async function loadRegionOptions() {
  setStatus('regionMessage','');
  try {
    const data = await fetchJson('/api/ops/mcn-region-options?include_disabled=true');
    regionOptions = Array.isArray(data.options) ? data.options : [];
    renderRegionOptions();
    setStatus('regionMessage', '');
  } catch (err) {
    setStatus('regionMessage', err.message || '地区加载失败', 'error');
    document.getElementById('regionRows').innerHTML = `<div class="muted">${escapeHtml(err.message || '地区加载失败')}</div>`;
  }
}
function renderRegionOptions() {
  const rows = regionOptions.map((row, index) => {
    const code = escapeHtml(row.code || '');
    const label = escapeHtml(row.label_zh || row.label || row.value || row.code || '-');
    const en = escapeHtml(row.label || row.value || '');
    const phone = escapeHtml(row.phone_code ? `+${row.phone_code}` : '-');
    const language = escapeHtml(row.language || '-');
    const checked = row.enabled ? 'checked' : '';
    return `<div class="region-row" data-region-code="${code}"><div class="region-cell region-main"><span class="region-code">${code}</span><div class="region-title"><div class="region-name">${label}</div><div class="region-meta">${en}</div></div></div><div class="region-cell region-center">${phone}</div><div class="region-cell region-center">${language}</div><label class="region-cell region-toggle"><input type="checkbox" data-region-enabled ${checked}> 启用</label></div>`;
  }).join('');
  document.getElementById('regionRows').innerHTML = rows || '<div class="muted">暂无地区</div>';
}
async function saveRegionOptions() {
  const btn = document.getElementById('saveRegionBtn');
  const options = Array.from(document.querySelectorAll('[data-region-code]')).map((row) => ({
    code: row.dataset.regionCode,
    enabled: !!row.querySelector('[data-region-enabled]')?.checked,
  }));
  setBusy(btn, true, '保存中...');
  setStatus('regionMessage','保存中...');
  try {
    const data = await fetchJson('/api/ops/mcn-region-options', { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify({options}) });
    regionOptions = Array.isArray(data.options) ? data.options : regionOptions;
    renderRegionOptions();
    setStatus('regionMessage','地区配置已保存', 'success');
    showToast('地区配置已保存');
  } catch (err) {
    setStatus('regionMessage', err.message || '地区保存失败', 'error');
    showToast(err.message || '地区保存失败', 'error');
  } finally { setBusy(btn, false); }
}
async function deleteAccount(userId, username, control=null) {
  if (!window.confirm(`确认删除账号 ${username}？该操作不可恢复。`)) return;
  if (control) control.disabled=true;
  setStatus('tableMessage','删除中...');
  try { await fetchJson(`/api/ops/accounts/${encodeURIComponent(userId)}`, { method:'DELETE' }); showToast('账号已删除'); await loadAccounts(); }
  catch (err) { showToast(err.message || '删除失败', 'error'); setStatus('tableMessage', err.message || '删除失败', 'error'); await loadAccounts(); }
  finally { if (control) control.disabled=false; }
}
function openDisplayNameEditor(userId, currentName, btn) {
  displayNameTargetUserId = userId;
  displayNameSourceButton = btn || null;
  const input = document.getElementById('displayNameInput');
  input.value = currentName || '';
  setStatus('displayNameMessage','');
  document.getElementById('displayNameModal').style.display='flex';
  setTimeout(()=>input.focus(), 0);
}
function closeDisplayNameModal() { document.getElementById('displayNameModal').style.display='none'; displayNameTargetUserId=null; displayNameSourceButton=null; setStatus('displayNameMessage',''); }
async function submitDisplayNameModal() {
  const btn=document.getElementById('displayNameSubmitBtn');
  const next=document.getElementById('displayNameInput').value.trim();
  if (!next) { setStatus('displayNameMessage','显示名不能为空','error'); return; }
  setBusy(btn,true,'保存中...');
  setStatus('displayNameMessage','保存中...');
  try {
    await updateAccount(displayNameTargetUserId, {display_name: next}, displayNameSourceButton, '显示名已保存');
    closeDisplayNameModal();
  } catch (err) {
    setStatus('displayNameMessage', err.message || '保存失败', 'error');
  } finally {
    setBusy(btn,false);
  }
}
function clearPasswordForm() { ['currentPassword','newPassword','confirmPassword'].forEach(id => { const el=document.getElementById(id); if (el) { el.value=''; el.type='password'; } }); const toggle=document.getElementById('passwordVisibleToggle'); if (toggle) toggle.checked=false; showGeneratedPassword(''); setStatus('passwordMessage',''); }
function openChangeOwnPassword() { passwordMode='self'; passwordTargetUserId=null; clearPasswordForm(); document.getElementById('passwordModalTitle').textContent='修改我的密码'; document.getElementById('passwordModalHint').textContent='需要输入当前密码，保存后下次登录使用新密码。'; document.getElementById('currentPasswordWrap').style.display='block'; document.getElementById('generatePasswordBtn').style.display='none'; document.getElementById('passwordSubmitBtn').textContent='保存新密码'; document.getElementById('passwordModal').style.display='flex'; }
function openAdminResetPassword(userId, username) { passwordMode='admin-reset'; passwordTargetUserId=userId; clearPasswordForm(); document.getElementById('passwordModalTitle').textContent='管理员重置密码'; document.getElementById('passwordModalHint').textContent=`目标账号：${username}。弹窗会直接生成新密码，请复制给使用人后保存。`; document.getElementById('currentPasswordWrap').style.display='none'; document.getElementById('generatePasswordBtn').style.display='inline-flex'; document.getElementById('passwordSubmitBtn').textContent='重置密码'; document.getElementById('passwordModal').style.display='flex'; generateTemporaryPassword(); }
function closePasswordModal() { document.getElementById('passwordModal').style.display='none'; clearPasswordForm(); }
function closeModalOnBackdrop(event) { if (event.target && event.target.id === 'passwordModal') closePasswordModal(); if (event.target && event.target.id === 'displayNameModal') closeDisplayNameModal(); }
async function submitPasswordModal() {
  const btn=document.getElementById('passwordSubmitBtn');
  const current=document.getElementById('currentPassword').value;
  const next=document.getElementById('newPassword').value;
  const confirm=document.getElementById('confirmPassword').value;
  if (next.length < 8) { setStatus('passwordMessage','新密码至少 8 位','error'); return; }
  if (next !== confirm) { setStatus('passwordMessage','两次输入的新密码不一致','error'); return; }
  if (passwordMode === 'self' && !current) { setStatus('passwordMessage','请输入当前密码','error'); return; }
  setBusy(btn,true,'保存中...'); setStatus('passwordMessage','保存中...');
  try {
    if (passwordMode === 'self') {
      await fetchJson('/api/ops/auth/password', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({current_password: current, new_password: next}) });
      showToast('密码修改成功');
    } else {
      await fetchJson(`/api/ops/accounts/${encodeURIComponent(passwordTargetUserId)}`, { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: next}) });
      showToast('密码已重置');
    }
    closePasswordModal(); await loadAccounts();
  } catch (err) { setStatus('passwordMessage', err.message || '保存失败', 'error'); showToast(err.message || '保存失败', 'error'); }
  finally { setBusy(btn,false); }
}
Promise.all([loadAccounts(), loadRegionOptions()]);
</script></body></html>"""
        if str(role or '').strip() != OPS_AUTH_ROLE_SUPER_ADMIN:
            html = html.replace('<option value="super_admin">超级管理员</option>', '')
            html = re.sub(r"<option value=\\\"super_admin\\\"[^`]*?超级管理员</option>", "", html)
            html = re.sub(r"\n    if \(isSuperAdmin\(\) && !isMe\).*?deleteAccount.*?;", "", html)
            html = re.sub(r"\nasync function deleteAccount\(userId, username, control=null\) \{.*?\n\}\nfunction openDisplayNameEditor", "\nfunction openDisplayNameEditor", html, flags=re.S)
        return html

    def _official_group_bridge_console_base_url() -> Optional[str]:
        webhook_url = str(official_group_approval_webhook_url or '').strip()
        if not webhook_url:
            return None
        return webhook_url.replace('/official-group/approve', '')

    def _official_group_bridge_summary_payload() -> Dict[str, Any]:
        base_url = _official_group_bridge_console_base_url()
        if not base_url:
            return {
                'configured': False,
                'health': {},
                'summary': {},
            }

        def _get_json(url: str) -> Dict[str, Any]:
            headers = _official_group_bridge_auth_headers()
            response = requests.get(url, headers=headers or None, timeout=10.0)
            response.raise_for_status()
            return response.json()

        try:
            health = _get_json(f"{base_url}/ops/official-group-bridge/health")
        except Exception as exc:
            health = {'status': 'unreachable', 'error': str(exc)}
        try:
            summary = _get_json(f"{base_url}/ops/official-group-bridge/summary")
        except Exception as exc:
            summary = {'status': 'unreachable', 'error': str(exc)}
        return {
            'configured': True,
            'base_url': base_url,
            'health': health,
            'summary': summary,
        }

    def _registration_group_approval_batch_members_page_html() -> str:
        return """<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>群审批留存页</title>
  <link rel=\"stylesheet\" href=\"https://cdn.jsdelivr.net/npm/flatpickr/dist/flatpickr.min.css\">
  <style>
    .batch-members-summary { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:12px; }
    .batch-members-summary .item { border:1px solid var(--ops-border); border-radius:var(--ops-r-lg); background:linear-gradient(180deg,#fff 0%,#fbfdff 100%); padding:14px 16px; box-shadow:var(--ops-shadow-card); }
    .batch-members-summary .value { font-size:21px; font-weight:760; color:var(--ops-text); line-height:1.25; }
    .batch-members-filter-card { padding:14px 16px!important; }
    .batch-members-filters { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px 12px; align-items:end; }
    .batch-members-filters label,.batch-members-range-picker { min-width:0; display:flex; flex-direction:column; gap:0; }
    .batch-members-filters .label { min-height:18px; margin:0 0 5px; display:flex; align-items:center; font-size:12px; line-height:18px; }
    .batch-members-filters input,.batch-members-filters select { min-height:38px!important; height:38px!important; margin:0!important; padding-top:0!important; padding-bottom:0!important; }
    .batch-members-range-display { display:flex; align-items:center; gap:8px; }
    .batch-members-range-display .range-picker-input { flex:1; min-height:38px!important; height:38px!important; margin:0!important; }
    .batch-members-range-display .range-picker-clear { min-width:64px; min-height:38px!important; height:38px!important; margin:0!important; padding:0 12px!important; display:inline-flex!important; align-items:center!important; justify-content:center!important; background:#e2e8f0!important; color:#334155!important; border-color:#cbd5e1!important; box-shadow:none!important; }
    .range-picker-hidden { display:none; }
    .filter-actions { min-width:0; display:flex; gap:8px; align-items:flex-end; justify-content:flex-end; flex-wrap:wrap; align-self:end; padding-top:0; }
    .filter-actions button { flex:0 1 auto; min-width:72px; min-height:38px!important; height:38px!important; margin:0!important; padding:0 14px!important; display:inline-flex!important; align-items:center!important; justify-content:center!important; white-space:nowrap!important; }
    .filter-actions button:nth-child(n+2) { min-width:84px; }
    .filter-actions button:nth-child(n+2) { background:#f2f6ff!important; color:#1f55d9!important; border-color:#d7e5ff!important; box-shadow:none!important; }
    .batch-members-notice { grid-column:1 / -1; display:none; margin-top:0; padding:8px 10px; border-radius:var(--ops-r-md); background:var(--ops-amber-soft); color:#92400e; border:1px solid #fed7aa; font-size:13px; font-weight:650; }
    .batch-members-notice.is-visible { display:block; }
    .table-wrap { overflow-x:auto; }
    #batchMembersTable { min-width:1060px; }
    #batchMembersTable th { position:relative; white-space:nowrap; text-align:left!important; vertical-align:middle!important; }
    #batchMembersTable td { vertical-align:middle!important; }
    .batch-member-phone-cell{white-space:nowrap;overflow:visible;text-overflow:clip;min-width:170px;font-variant-numeric:tabular-nums;}
    th.resizable-column { user-select:none; }
    .batch-member-resize-handle { position:absolute; top:0; right:0; width:10px; height:100%; cursor:col-resize; z-index:3; }
    .batch-member-resize-handle::after { content:''; position:absolute; top:10px; bottom:10px; left:4px; width:2px; border-radius:999px; background:#d7e3f4; }
    th.resizable-column:hover .batch-member-resize-handle::after, body.batch-member-column-resizing .batch-member-resize-handle::after { background:#93c5fd; }
    .badge.registered { background:#dcfce7!important; color:#166534!important; border-color:#bbf7d0!important; }
    .badge.in_progress { background:#fef3c7!important; color:#92400e!important; border-color:#fde68a!important; }
    .badge.not_found { background:#fee2e2!important; color:#991b1b!important; border-color:#fecaca!important; }
    .pager { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:14px; flex-wrap:wrap; }
    .pager-meta { color:var(--ops-muted); font-size:13px; }
    .pager-actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    .pager-jump { display:flex; gap:8px; align-items:center; flex-wrap:wrap; color:#475569; font-size:13px; }
    .pager-jump input { width:84px; }
    .pager-jump-total { color:#64748b; font-size:13px; }
    .pager-actions button[disabled],.filter-actions button[disabled] { background:#e2e8f0!important; border-color:#cbd5e1!important; color:#64748b!important; cursor:not-allowed!important; box-shadow:none!important; transform:none!important; }
    .selection-bar { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-bottom:10px; color:#475569; font-size:13px; }
    .selection-actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    .selection-actions button { background:#e2e8f0!important; color:#334155!important; border-color:#cbd5e1!important; box-shadow:none!important; }
    .select-col { text-align:center!important; width:48px; padding-left:0!important; padding-right:0!important; vertical-align:middle!important; }
    .select-col input { width:18px!important; height:18px!important; min-height:18px!important; padding:0!important; margin:0 auto!important; cursor:pointer; display:block!important; }
    @media (max-width:1100px) { .batch-members-summary { grid-template-columns:repeat(3,minmax(0,1fr)); } .batch-members-filters { grid-template-columns:repeat(2,minmax(180px,1fr)); } .filter-actions { grid-column:1 / -1; justify-content:flex-start; } }
    @media (max-width:720px) { .batch-members-filters,.batch-members-summary { grid-template-columns:1fr; } }
  </style>
</head>
<body data-layout-guard=\"batch-member-phone-column\">
  <div class=\"page-shell\">
    <div class=\"shell-nav\">
      <a href=\"/ops\">管理员看板</a>
      <a href=\"/ops/intake-bot-presets\">收口配置中心</a>
      <a href=\"/ops/production-ops\">群审批控制台</a>
      <a href=\"/ops/registration-group-approval-batch-members\">群审批留存页</a>
      <a href=\"/ops/group-atmosphere\" data-admin-only-nav=\"true\">群聊天助手</a>
      <a href=\"/ops/accounts\">账号设置</a>
    </div>
    <div class=\"hero\">
      <div>
        <h1>群审批留存页</h1>
      </div>
    </div>
    <div class=\"batch-members-summary\" id=\"summary\"></div>
    <div class=\"card batch-members-filter-card\">
      <div class=\"batch-members-filters\">
        <div class=\"batch-members-range-picker\">
          <div class=\"label\">时间周期</div>
          <div class=\"batch-members-range-display\">
            <input id=\"rangePickerInput\" class=\"range-picker-input\" type=\"text\" placeholder=\"选择时间周期\" readonly />
            <button id=\"rangePickerClearBtn\" class=\"range-picker-clear\" type=\"button\" onclick=\"clearBatchMembersDateRange()\">清空</button>
          </div>
          <input id=\"approved_date_start\" class=\"range-picker-hidden\" type=\"hidden\" />
          <input id=\"approved_date_end\" class=\"range-picker-hidden\" type=\"hidden\" />
        </div>
        <label><div class=\"label\">批次ID</div><input id=\"approval_run_id\" placeholder=\"例如 2026050712\" /></label>
        <label><div class=\"label\">群类型</div><select id=\"group_type\" onchange=\"reloadBatchMembers(true)\"><option value=\"\">全部类型</option><option value=\"registration_group\">注册群</option><option value=\"official_group\">官方群</option></select></label>
        <label><div class=\"label\">群组</div><select id=\"registration_group\"><option value=\"\">全部群组</option></select></label>
        <label><div class=\"label\">地区</div><select id=\"area\"><option value=\"\">全部地区</option></select></label>
        <label><div class=\"label\">关键词</div><input id=\"keyword\" placeholder=\"WA 号码 / 批次 / 群组\" /></label>
        <label><div class=\"label\">注册状态</div><select id=\"registration_status\"><option value=\"\">全部</option><option value=\"registered\">已注册</option><option value=\"in_progress\">引导注册中</option><option value=\"not_found\">未注册</option></select></label>
        <div class=\"filter-actions\"><button onclick=\"reloadBatchMembers(true)\">查询</button><button id=\"exportXlsxBtn\" onclick=\"exportBatchMembers('xlsx')\" disabled>导出 xlsx</button><button id=\"exportCsvBtn\" onclick=\"exportBatchMembers('csv')\" disabled>导出 CSV</button></div>
        <div id=\"batchMembersSelectionNotice\" class=\"batch-members-notice\">请先选择要导出的成员</div>
      </div>
    </div>
    <div class=\"card\">
      <div class=\"selection-bar\">
        <div class=\"selection-actions\">
          <button id=\"batchMembersSelectAllBtn\" type=\"button\" onclick=\"toggleBatchMembersSelectAll()\">全选当前页</button>
          <button type=\"button\" onclick=\"clearBatchMemberSelection()\">清空选择</button>
        </div>
        <div id=\"batchMembersSelectionMeta\">未选择成员时显示全部统计</div>
      </div>
      <div class=\"table-wrap\">
      <table id=\"batchMembersTable\">
        <colgroup id=\"batchMembersColgroup\">
          <col style=\"width:48px\" />
          <col style=\"width:120px\" />
          <col style=\"width:120px\" />
          <col style=\"width:120px\" />
          <col style=\"width:240px\" />
          <col style=\"width:120px\" />
          <col style=\"width:190px\" />
          <col style=\"width:130px\" />
        </colgroup>
        <thead><tr><th class=\"select-col\"><input id=\"batchMembersSelectAll\" type=\"checkbox\" onchange=\"toggleBatchMembersSelectAll(this.checked)\" aria-label=\"全选当前页\" /></th><th class=\"resizable-column\">审批时间<span class=\"batch-member-resize-handle\" data-column-index=\"1\"></span></th><th class=\"resizable-column\">批次<span class=\"batch-member-resize-handle\" data-column-index=\"2\"></span></th><th class=\"resizable-column\">群类型<span class=\"batch-member-resize-handle\" data-column-index=\"3\"></span></th><th class=\"resizable-column\">群组<span class=\"batch-member-resize-handle\" data-column-index=\"4\"></span></th><th class=\"resizable-column\">地区<span class=\"batch-member-resize-handle\" data-column-index=\"5\"></span></th><th class=\"resizable-column\">WA 号码<span class=\"batch-member-resize-handle\" data-column-index=\"6\"></span></th><th class=\"resizable-column\">注册状态<span class=\"batch-member-resize-handle\" data-column-index=\"7\"></span></th></tr></thead>
        <tbody id=\"rows\"><tr><td colspan=\"8\" class=\"muted\">加载中...</td></tr></tbody>
      </table>
      </div>
      <div class="pager" id="pager">
        <div class="pager-meta" id="pagerMeta">每页 30 条</div>
        <div class="pager-actions">
          <button id="prevPageBtn" type="button" onclick="changeBatchMembersPage(-1)">上一页</button>
          <div class="pager-jump">
            <span>第</span>
            <input id="pagerPageInput" type="number" min="1" step="1" inputmode="numeric" onkeydown="if (event.key === 'Enter') submitBatchMembersPageJump()" />
            <span id="pagerPageTotal" class="pager-jump-total">/ 1 页</span>
            <button type="button" onclick="submitBatchMembersPageJump()">跳转</button>
          </div>
          <button id="nextPageBtn" type="button" onclick="changeBatchMembersPage(1)">下一页</button>
        </div>
      </div>
    </div>
  </div>
  <script src=\"https://cdn.jsdelivr.net/npm/flatpickr\"></script>
  <script>
    function escapeHtml(value) {
      if (window.OpsCommon) return window.OpsCommon.escapeHtml(value);
      return String(value ?? '').replace(/[&<>\"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
    }
    function statusBadge(row) {
      const status = String(row.registration_status || 'not_found');
      const label = String(row.registration_status_label || status);
      return `<span class=\"badge ${status}\">${escapeHtml(label)}</span>`;
    }
    function currentBeijingDateString() {
      const now = new Date();
      const utcMs = now.getTime() + now.getTimezoneOffset() * 60000;
      const beijing = new Date(utcMs + 8 * 60 * 60000);
      const year = beijing.getUTCFullYear();
      const month = String(beijing.getUTCMonth() + 1).padStart(2, '0');
      const day = String(beijing.getUTCDate()).padStart(2, '0');
      return `${year}-${month}-${day}`;
    }
    function formatBatchMembersDateRange(start, end) {
      const normalizedStart = String(start || '').trim();
      const normalizedEnd = String(end || '').trim();
      if (!normalizedStart && !normalizedEnd) return '';
      if (normalizedStart && normalizedEnd) {
        return normalizedStart === normalizedEnd ? normalizedStart : `${normalizedStart} ~ ${normalizedEnd}`;
      }
      return normalizedStart || normalizedEnd;
    }
    function currentBatchMembersDateRange() {
      return {
        start: String(document.getElementById('approved_date_start')?.value || '').trim(),
        end: String(document.getElementById('approved_date_end')?.value || '').trim(),
      };
    }
    function setBatchMembersDateRange(start, end, options = {}) {
      const startInput = document.getElementById('approved_date_start');
      const endInput = document.getElementById('approved_date_end');
      const rangeInput = document.getElementById('rangePickerInput');
      const normalizedStart = String(start || '').trim();
      const normalizedEnd = String(end || '').trim();
      if (!options.preserveAutoState) {
        window.__batchMembersAutoDateRange = options.source === 'auto';
      }
      startInput.value = normalizedStart;
      endInput.value = normalizedEnd;
      if (rangeInput) {
        rangeInput.value = formatBatchMembersDateRange(normalizedStart, normalizedEnd);
      }
      if (window.__batchMembersFlatpickr) {
        if (!normalizedStart && !normalizedEnd) {
          window.__batchMembersFlatpickr.clear();
        } else {
          const selectedDates = normalizedStart === normalizedEnd
            ? [normalizedStart, normalizedEnd]
            : [normalizedStart, normalizedEnd];
          window.__batchMembersFlatpickr.setDate(selectedDates, false);
        }
      }
    }
    function clearBatchMembersDateRange() {
      setBatchMembersDateRange('', '', { source: 'manual' });
    }
    function syncBatchMembersDefaultDateRange() {
      if (window.__batchMembersAutoDateRange !== true) return false;
      const today = currentBeijingDateString();
      const current = currentBatchMembersDateRange();
      if (current.start === today && current.end === today) return false;
      setBatchMembersDateRange(today, today, { source: 'auto' });
      window.__batchMembersPage = 1;
      return true;
    }
    function refreshBatchMembersDefaultDateOnResume() {
      if (document.visibilityState && document.visibilityState !== 'visible') return;
      if (syncBatchMembersDefaultDateRange()) {
        reloadBatchMembers(true);
      }
    }
    function initBatchMembersRangePicker() {
      const rangeInput = document.getElementById('rangePickerInput');
      if (!rangeInput || typeof window.flatpickr !== 'function') return;
      if (window.__batchMembersFlatpickr) return;
      window.__batchMembersFlatpickr = window.flatpickr(rangeInput, {
        mode: 'range',
        dateFormat: 'Y-m-d',
        allowInput: false,
        clickOpens: true,
        locale: {
          rangeSeparator: ' ~ ',
        },
        onChange(selectedDates, dateStr, instance) {
          if (!Array.isArray(selectedDates) || selectedDates.length === 0) {
            window.__batchMembersAutoDateRange = false;
            document.getElementById('approved_date_start').value = '';
            document.getElementById('approved_date_end').value = '';
            instance.input.value = '';
            return;
          }
          const values = selectedDates.map((item) => instance.formatDate(item, 'Y-m-d'));
          const start = values[0] || '';
          const end = values[1] || values[0] || '';
          window.__batchMembersAutoDateRange = false;
          document.getElementById('approved_date_start').value = start;
          document.getElementById('approved_date_end').value = end;
          instance.input.value = formatBatchMembersDateRange(start, end);
        },
      });
    }
    function currentBatchMemberParams() {
      const params = new URLSearchParams();
      for (const key of ['approved_date_start', 'approved_date_end', 'approval_run_id', 'group_type', 'registration_group', 'area', 'keyword', 'registration_status']) {
        const element = document.getElementById(key);
        const value = String(element?.value || '').trim();
        if (value) params.set(key, value);
      }
      return params;
    }
    function currentBatchMembersPage() {
      return Math.max(Number(window.__batchMembersPage || 1), 1);
    }
    function batchMemberColumnStorageKey() {
      return 'registration-group-approval-batch-members:column-widths';
    }
    function defaultBatchMemberColumnWidths() {
      return [48, 120, 120, 120, 260, 120, 190, 130];
    }
    function currentBatchMemberColumnWidths() {
      try {
        const parsed = JSON.parse(window.localStorage.getItem(batchMemberColumnStorageKey()) || 'null');
        if (Array.isArray(parsed) && parsed.length === defaultBatchMemberColumnWidths().length) {
          return parsed.map((value, index) => Math.max(Number(value) || defaultBatchMemberColumnWidths()[index], 90));
        }
      } catch (error) {
      }
      return defaultBatchMemberColumnWidths().slice();
    }
    function applyBatchMemberColumnWidths(widths) {
      const cols = Array.from(document.querySelectorAll('#batchMembersColgroup col'));
      if (!cols.length) return;
      cols.forEach((col, index) => {
        const fallback = defaultBatchMemberColumnWidths()[index] || 140;
        const width = Math.max(Number(widths?.[index]) || fallback, 90);
        col.style.width = `${width}px`;
      });
    }
    function persistBatchMemberColumnWidths(widths) {
      window.localStorage.setItem(batchMemberColumnStorageKey(), JSON.stringify(widths.map((value, index) => Math.max(Number(value) || defaultBatchMemberColumnWidths()[index], 90))));
    }
    function initBatchMemberColumnResize() {
      if (window.__batchMemberColumnResizeInitialized) return;
      window.__batchMemberColumnResizeInitialized = true;
      applyBatchMemberColumnWidths(currentBatchMemberColumnWidths());
      const handles = Array.from(document.querySelectorAll('.batch-member-resize-handle'));
      handles.forEach((handle) => {
        handle.addEventListener('mousedown', (event) => {
          event.preventDefault();
          event.stopPropagation();
          const columnIndex = Number(handle.dataset.columnIndex || '-1');
          const widths = currentBatchMemberColumnWidths();
          const startX = event.clientX;
          const startWidth = widths[columnIndex] || defaultBatchMemberColumnWidths()[columnIndex] || 140;
          document.body.classList.add('batch-member-column-resizing');
          const onMove = (moveEvent) => {
            const nextWidths = widths.slice();
            nextWidths[columnIndex] = Math.max(startWidth + (moveEvent.clientX - startX), 90);
            applyBatchMemberColumnWidths(nextWidths);
          };
          const onUp = (upEvent) => {
            const nextWidths = widths.slice();
            nextWidths[columnIndex] = Math.max(startWidth + (upEvent.clientX - startX), 90);
            persistBatchMemberColumnWidths(nextWidths);
            applyBatchMemberColumnWidths(nextWidths);
            document.body.classList.remove('batch-member-column-resizing');
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
          };
          window.addEventListener('mousemove', onMove);
          window.addEventListener('mouseup', onUp);
        });
      });
    }
    function goToBatchMembersPage(page) {
      const totalPages = Math.max(Number(window.__batchMembersTotalPages || 1), 1);
      const nextPage = Math.min(Math.max(Number(page || 1), 1), totalPages);
      window.__batchMembersPage = nextPage;
      reloadBatchMembers();
    }
    function submitBatchMembersPageJump() {
      const input = document.getElementById('pagerPageInput');
      const requestedPage = Number(input?.value || currentBatchMembersPage());
      goToBatchMembersPage(requestedPage);
    }
    function renderPagination(pagination) {
      const pagerMeta = document.getElementById('pagerMeta');
      const prevBtn = document.getElementById('prevPageBtn');
      const nextBtn = document.getElementById('nextPageBtn');
      const pageInput = document.getElementById('pagerPageInput');
      const pageTotal = document.getElementById('pagerPageTotal');
      const page = Number(pagination?.page || 1);
      const totalPages = Math.max(Number(pagination?.total_pages || 1), 1);
      const totalRows = Math.max(Number(pagination?.total_rows || 0), 0);
      const pageSize = Math.max(Number(pagination?.page_size || 30), 1);
      const startRow = totalRows === 0 ? 0 : ((page - 1) * pageSize) + 1;
      const endRow = totalRows === 0 ? 0 : Math.min(page * pageSize, totalRows);
      pagerMeta.textContent = totalRows ? `第 ${page}/${totalPages} 页 · 第 ${startRow}-${endRow} 条，共 ${totalRows} 条` : `第 ${page}/${totalPages} 页 · 共 0 条`;
      prevBtn.disabled = !Boolean(pagination?.has_prev);
      nextBtn.disabled = !Boolean(pagination?.has_next);
      window.__batchMembersTotalPages = totalPages;
      if (pageInput) {
        pageInput.value = String(page);
        pageInput.min = '1';
        pageInput.max = String(totalPages);
      }
      if (pageTotal) {
        pageTotal.textContent = `/ ${totalPages} 页`;
      }
    }
    function changeBatchMembersPage(delta) {
      goToBatchMembersPage(currentBatchMembersPage() + Number(delta || 0));
    }
    function syncGroupTypeFilter(selectedValue) {
      const select = document.getElementById('group_type');
      const value = String(selectedValue || '').trim();
      select.value = ['registration_group', 'official_group'].includes(value) ? value : '';
    }
    function syncRegistrationGroupOptions(options, selectedValue) {
      const select = document.getElementById('registration_group');
      const currentValue = String(selectedValue ?? select.value ?? '').trim();
      const normalizedOptions = Array.isArray(options) ? options : [];
      const optionHtml = ['<option value="">全部群组</option>'].concat(
        normalizedOptions.map((item) => {
          const value = escapeHtml(item?.value || '');
          const label = escapeHtml(item?.label || item?.value || '');
          return `<option value="${value}">${label}</option>`;
        })
      ).join('');
      select.innerHTML = optionHtml;
      const availableValues = new Set(normalizedOptions.map((item) => String(item?.value || '').trim()));
      select.value = availableValues.has(currentValue) ? currentValue : '';
    }
    function syncAreaOptions(options, selectedValue) {
      const select = document.getElementById('area');
      const currentValue = String(selectedValue ?? select.value ?? '').trim();
      const normalizedOptions = Array.isArray(options) ? options : [];
      const optionHtml = ['<option value="">全部地区</option>'].concat(
        normalizedOptions.map((item) => {
          const value = escapeHtml(item?.value || '');
          const label = escapeHtml(item?.label || item?.value || '');
          return `<option value="${value}">${label}</option>`;
        })
      ).join('');
      select.innerHTML = optionHtml;
      const availableValues = new Set(normalizedOptions.map((item) => String(item?.value || '').trim()));
      select.value = availableValues.has(currentValue) ? currentValue : '';
    }
    function selectedBatchMemberIdsParam() {
      const selected = Array.from(window.__batchMembersSelectedIds || []).filter(Boolean);
      return selected.join(',');
    }
    function selectedBatchMemberCount() {
      return Array.from(window.__batchMembersSelectedIds || []).filter(Boolean).length;
    }
    function showBatchMembersSelectionNotice(message) {
      const notice = document.getElementById('batchMembersSelectionNotice');
      if (!notice) return;
      notice.textContent = message || '';
      notice.classList.toggle('is-visible', Boolean(message));
    }
    function syncBatchMemberExportControls() {
      const selectedIds = selectedBatchMemberIdsParam();
      const disabled = !selectedIds;
      ['exportXlsxBtn', 'exportCsvBtn'].forEach((id) => {
        const button = document.getElementById(id);
        if (button) button.disabled = disabled;
      });
      if (selectedIds) showBatchMembersSelectionNotice('');
    }
    function exportBatchMembers(format) {
      const params = currentBatchMemberParams();
      const selectedIds = selectedBatchMemberIdsParam();
      if (!selectedIds) {
        showBatchMembersSelectionNotice('请先选择要导出的成员');
        syncBatchMemberExportControls();
        return false;
      }
      params.set('format', format || 'xlsx');
      params.set('limit', '5000');
      params.set('member_ids', selectedIds);
      window.open(`/api/ops/registration-group-approval-batch-members/export?${params.toString()}`, '_blank');
      return true;
    }
    function batchMemberRowId(row) {
      return String(row?.member_id || `${row?.approval_run_id || ''}:${row?.requester_id || ''}:${row?.batch_index ?? ''}`);
    }
    function ensureBatchMemberSelectionStores() {
      if (!window.__batchMembersSelectedIds) window.__batchMembersSelectedIds = new Set();
      if (!window.__batchMembersSelectedRowsById) window.__batchMembersSelectedRowsById = new Map();
    }
    function rememberVisibleSelectedBatchMemberRows() {
      ensureBatchMemberSelectionStores();
      const selected = window.__batchMembersSelectedIds;
      const rowsById = window.__batchMembersSelectedRowsById;
      (window.__batchMembersRows || []).forEach((row) => {
        const rowId = batchMemberRowId(row);
        if (rowId && selected.has(rowId)) rowsById.set(rowId, row);
      });
    }
    function selectedBatchMemberRows() {
      ensureBatchMemberSelectionStores();
      const rowsById = window.__batchMembersSelectedRowsById;
      return Array.from(window.__batchMembersSelectedIds || []).map((id) => rowsById.get(id)).filter(Boolean);
    }
    function formatBatchMembersRate(value) {
      const numeric = Number(value || 0);
      if (!Number.isFinite(numeric)) return '0%';
      return `${(numeric * 100).toFixed(1).replace(/\.0$/, '')}%`;
    }
    function renderBatchMembersSummary(summary, selectedRows = null) {
      const rows = Array.isArray(selectedRows) ? selectedRows : [];
      const selectedCount = selectedBatchMemberCount();
      const useSelection = selectedCount > 0 && rows.length === selectedCount;
      const emptySummary = { total_members: 0, registration_group_members: 0, official_group_members: 0, registered_members: 0, in_progress_members: 0, not_registered_members: 0, registration_rate: 0 };
      const computed = useSelection ? rows.reduce((acc, row) => {
        acc.total_members += 1;
        if (row.group_type === 'official_group') acc.official_group_members += 1;
        else acc.registration_group_members += 1;
        if (row.registration_status === 'registered') acc.registered_members += 1;
        else if (row.registration_status === 'in_progress') acc.in_progress_members += 1;
        else acc.not_registered_members += 1;
        return acc;
      }, { ...emptySummary }) : ({ ...emptySummary, ...(summary || {}) });
      computed.registration_rate = computed.total_members ? computed.registered_members / computed.total_members : 0;
      document.getElementById('summary').innerHTML = [
        [useSelection ? '已选注册群人数' : '注册群人数', computed.registration_group_members || 0],
        [useSelection ? '已选官方群人数' : '官方群人数', computed.official_group_members || 0],
        ['已注册', computed.registered_members || 0],
        ['注册率', formatBatchMembersRate(computed.registration_rate)],
        ['未注册', computed.not_registered_members || 0],
      ].map(([label, value]) => `<div class=\"item\"><div class=\"label\">${label}</div><div class=\"value\">${value}</div></div>`).join('');
      const meta = document.getElementById('batchMembersSelectionMeta');
      if (meta) meta.textContent = selectedCount ? `当前已选中 ${selectedCount} 条用户信息；导出时所有已选用户会写入同一个 Excel 工作表` : '当前已选中 0 条用户信息；请先选择要导出的成员';
      syncBatchMemberExportControls();
    }
    function syncBatchMemberSelectionControls() {
      const rows = window.__batchMembersRows || [];
      const selected = window.__batchMembersSelectedIds || new Set();
      const visibleIds = rows.map(batchMemberRowId).filter(Boolean);
      const selectedVisibleCount = visibleIds.filter((id) => selected.has(id)).length;
      const selectAll = document.getElementById('batchMembersSelectAll');
      if (selectAll) {
        selectAll.checked = visibleIds.length > 0 && selectedVisibleCount === visibleIds.length;
        selectAll.indeterminate = selectedVisibleCount > 0 && selectedVisibleCount < visibleIds.length;
      }
      const button = document.getElementById('batchMembersSelectAllBtn');
      if (button) button.textContent = visibleIds.length > 0 && selectedVisibleCount === visibleIds.length ? '取消全选当前页' : '全选当前页';
    }
    function refreshBatchMemberSelectionSummary() {
      renderBatchMembersSummary(window.__batchMembersSummary || {}, selectedBatchMemberRows());
      syncBatchMemberSelectionControls();
    }
    function toggleBatchMemberSelection(rowId, checked) {
      ensureBatchMemberSelectionStores();
      const normalizedId = String(rowId || '');
      if (!normalizedId) return;
      const row = (window.__batchMembersRows || []).find((item) => batchMemberRowId(item) === normalizedId);
      if (checked) {
        window.__batchMembersSelectedIds.add(normalizedId);
        if (row) window.__batchMembersSelectedRowsById.set(normalizedId, row);
      } else {
        window.__batchMembersSelectedIds.delete(normalizedId);
        window.__batchMembersSelectedRowsById.delete(normalizedId);
      }
      refreshBatchMemberSelectionSummary();
    }
    function toggleBatchMembersSelectAll(forceChecked = null) {
      ensureBatchMemberSelectionStores();
      const rows = window.__batchMembersRows || [];
      const ids = rows.map(batchMemberRowId).filter(Boolean);
      const shouldSelect = forceChecked === null ? ids.some((id) => !window.__batchMembersSelectedIds.has(id)) : Boolean(forceChecked);
      rows.forEach((row) => {
        const id = batchMemberRowId(row);
        if (!id) return;
        if (shouldSelect) {
          window.__batchMembersSelectedIds.add(id);
          window.__batchMembersSelectedRowsById.set(id, row);
        } else {
          window.__batchMembersSelectedIds.delete(id);
          window.__batchMembersSelectedRowsById.delete(id);
        }
      });
      document.querySelectorAll('.batch-member-row-checkbox').forEach((checkbox) => { checkbox.checked = shouldSelect; });
      refreshBatchMemberSelectionSummary();
    }
    function clearBatchMemberSelection() {
      window.__batchMembersSelectedIds = new Set();
      window.__batchMembersSelectedRowsById = new Map();
      document.querySelectorAll('.batch-member-row-checkbox').forEach((checkbox) => { checkbox.checked = false; });
      refreshBatchMemberSelectionSummary();
    }
    async function reloadBatchMembers(resetPage = false) {
      if (syncBatchMembersDefaultDateRange()) resetPage = true;
      if (resetPage) {
        window.__batchMembersPage = 1;
        window.__batchMembersSelectedIds = new Set();
        window.__batchMembersSelectedRowsById = new Map();
      }
      const params = currentBatchMemberParams();
      params.set('limit', '30');
      params.set('page', String(currentBatchMembersPage()));
      const currentGroupValue = String(document.getElementById('registration_group').value || '').trim();
      const currentAreaValue = String(document.getElementById('area').value || '').trim();
      const response = await fetch(`/api/ops/registration-group-approval-batch-members?${params.toString()}`, { cache: 'no-store' });
      const data = await response.json();
      const summary = data.summary || {};
      window.__batchMembersSummary = summary;
      const pagination = data.pagination || {};
      window.__batchMembersPage = Number((data.filters || {}).page || pagination.page || 1);
      setBatchMembersDateRange(
        (data.filters || {}).approved_date_start || (data.filters || {}).approved_date || '',
        (data.filters || {}).approved_date_end || (data.filters || {}).approved_date || '',
        { preserveAutoState: true },
      );
      syncGroupTypeFilter((data.filters || {}).group_type || '');
      syncRegistrationGroupOptions(data.registration_group_options || [], currentGroupValue || (data.filters || {}).registration_group || '');
      syncAreaOptions(data.area_options || [], currentAreaValue || (data.filters || {}).area || '');
      const rows = data.rows || [];
      window.__batchMembersRows = rows;
      ensureBatchMemberSelectionStores();
      rememberVisibleSelectedBatchMemberRows();
      renderBatchMembersSummary(summary, selectedBatchMemberRows());
      document.getElementById('rows').innerHTML = rows.length ? rows.map((row) => {
        const rowId = batchMemberRowId(row);
        const checked = (window.__batchMembersSelectedIds || new Set()).has(rowId) ? 'checked' : '';
        return `
        <tr>
          <td class=\"select-col\"><input class=\"batch-member-row-checkbox\" type=\"checkbox\" data-member-id=\"${escapeHtml(rowId)}\" ${checked} onchange=\"toggleBatchMemberSelection(this.dataset.memberId, this.checked)\" aria-label=\"选择成员\" /></td>
          <td>${escapeHtml(row.approved_time_display || '')}</td>
          <td>${escapeHtml(row.approval_batch_display_id || row.approval_run_id || '')}</td>
          <td>${escapeHtml(row.group_type_label || '')}</td>
          <td>${escapeHtml(row.registration_group_name || row.registration_group || '')}</td>
          <td>${escapeHtml(row.area || '')}</td>
          <td class=\"batch-member-phone-cell\">${escapeHtml(row.wa_phone_raw || row.wa_phone_normalized || '')}</td>
          <td>${statusBadge(row)}</td>
        </tr>`;
      }).join('') : '<tr><td colspan=\"8\" class=\"muted\">暂无数据</td></tr>';
      refreshBatchMemberSelectionSummary();
      renderPagination(pagination);
    }
    const initialBatchMembersDate = currentBeijingDateString();
    initBatchMembersRangePicker();
    window.__batchMembersAutoDateRange = true;
    setBatchMembersDateRange(initialBatchMembersDate, initialBatchMembersDate, { source: 'auto' });
    window.__batchMembersPage = 1;
    window.__batchMembersRows = [];
    window.__batchMembersSummary = {};
    window.__batchMembersSelectedIds = new Set();
    window.__batchMembersSelectedRowsById = new Map();
    initBatchMemberColumnResize();
    document.addEventListener('visibilitychange', refreshBatchMembersDefaultDateOnResume);
    window.addEventListener('focus', refreshBatchMembersDefaultDateOnResume);
    window.setInterval(refreshBatchMembersDefaultDateOnResume, 60000);
    reloadBatchMembers(true);
  </script>
</body>
</html>"""

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get('/api/ops/group-atmosphere/roles')
    def group_atmosphere_roles() -> Dict[str, Any]:
        return service.list_group_atmosphere_roles()

    @app.post('/api/ops/group-atmosphere/roles/manual-phrases')
    def group_atmosphere_roles_manual_phrases(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.upsert_group_atmosphere_manual_phrases(payload)

    @app.get('/api/ops/group-atmosphere/phrase-types')
    def group_atmosphere_phrase_types(include_disabled: bool = False) -> Dict[str, Any]:
        return service.list_group_atmosphere_phrase_types(include_disabled=include_disabled)

    @app.post('/api/ops/group-atmosphere/phrase-types')
    def group_atmosphere_phrase_type_upsert(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.upsert_group_atmosphere_phrase_type(payload)

    @app.post('/api/ops/group-atmosphere/phrase-types/{type_key}')
    def group_atmosphere_phrase_type_rename(type_key: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.rename_group_atmosphere_phrase_type(type_key, payload)

    @app.get('/api/ops/group-atmosphere/phrase-types/{type_key}/usage')
    def group_atmosphere_phrase_type_usage(type_key: str) -> Dict[str, Any]:
        return service.group_atmosphere_phrase_type_usage(type_key)

    @app.delete('/api/ops/group-atmosphere/phrase-types/{type_key}')
    def group_atmosphere_phrase_type_delete(type_key: str) -> Dict[str, Any]:
        return service.delete_group_atmosphere_phrase_type(type_key)

    @app.get('/api/ops/group-atmosphere/media-assets')
    def group_atmosphere_media_assets() -> Dict[str, Any]:
        return service.list_group_atmosphere_media_assets()

    GROUP_ATMOSPHERE_MEDIA_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
    GROUP_ATMOSPHERE_PHRASE_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
    GROUP_ATMOSPHERE_PHRASE_UPLOAD_MAX_LINES = 5000

    async def _read_limited_upload_file(file: UploadFile, *, max_bytes: int, detail: str) -> bytes:
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = await file.read(min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(status_code=413, detail=detail)
            chunks.append(chunk)
        return b''.join(chunks)

    def _assert_upload_line_limit(content: str, *, max_lines: int, detail: str) -> None:
        if len(str(content or '').splitlines()) > max_lines:
            raise HTTPException(status_code=413, detail=detail)

    async def _read_limited_json_request(request: Request, *, max_bytes: int, detail: str) -> Dict[str, Any]:
        raw_length = str(request.headers.get('content-length') or '').strip()
        if raw_length:
            try:
                if int(raw_length) > max_bytes:
                    raise HTTPException(status_code=413, detail=detail)
            except ValueError:
                pass
        raw = await request.body()
        if len(raw) > max_bytes:
            raise HTTPException(status_code=413, detail=detail)
        try:
            payload = json.loads(raw.decode('utf-8') if raw else '{}')
        except Exception:
            raise HTTPException(status_code=400, detail='invalid_json_payload')
        return payload if isinstance(payload, dict) else {}

    async def _extract_ops_upload_ocr_text(file: UploadFile, *, too_large_detail: str) -> Dict[str, Any]:
        if service.ocr_adapter is None:
            raise HTTPException(status_code=503, detail='ocr_adapter_not_configured')
        if getattr(service.ocr_adapter, 'available', True) is False:
            raise HTTPException(status_code=503, detail=getattr(service.ocr_adapter, 'unavailable_reason', None) or 'ocr_adapter_unavailable')
        content_type = str(file.content_type or '').strip().lower()
        filename = str(file.filename or '').strip()
        suffix = Path(filename).suffix.lower()
        if content_type and not content_type.startswith('image/'):
            raise HTTPException(status_code=400, detail='unsupported_ocr_file_type')
        if suffix not in {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}:
            suffix = '.png'
        raw = await _read_limited_upload_file(file, max_bytes=8 * 1024 * 1024, detail=too_large_detail)
        if not raw:
            raise HTTPException(status_code=400, detail='empty_ocr_image')
        cache_path = service.media_cache_dir / f"{create_id('ops_ocr')}{suffix}"
        cache_path.write_bytes(raw)
        try:
            extracted = service.ocr_adapter.extract_text(str(cache_path))
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc) or 'ocr_adapter_unavailable')
        except Exception:
            raise HTTPException(status_code=500, detail='ops_ocr_failed')
        finally:
            try:
                cache_path.unlink()
            except Exception:
                pass
        raw_text = str((extracted or {}).get('raw_text') or '').strip() if isinstance(extracted, dict) else str(extracted or '').strip()
        return {
            'ok': True,
            'raw_text': raw_text,
            'normalized': normalize_native_ocr_fields(raw_text) if raw_text else {},
            'ocr': {
                'engine': (extracted or {}).get('engine') if isinstance(extracted, dict) else '',
                'line_count': (extracted or {}).get('line_count') if isinstance(extracted, dict) else None,
            },
        }

    @app.post('/api/ops/group-atmosphere/media-assets')
    async def group_atmosphere_media_asset_upload(file: UploadFile = File(...)) -> Dict[str, Any]:
        raw = await _read_limited_upload_file(
            file,
            max_bytes=GROUP_ATMOSPHERE_MEDIA_UPLOAD_MAX_BYTES,
            detail='media_upload_file_too_large',
        )
        return service.create_group_atmosphere_media_asset(
            filename=file.filename or 'image',
            content=raw,
            mime_type=file.content_type or '',
        )

    @app.get('/api/ops/group-atmosphere/media-assets/{media_id}/preview')
    def group_atmosphere_media_asset_preview(media_id: str) -> Response:
        media = service.get_group_atmosphere_media_asset(media_id)
        path = Path(str(media.get('media_path') or ''))
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail='media_file_not_found')
        return Response(content=path.read_bytes(), media_type=str(media.get('mime_type') or 'application/octet-stream'))

    def _normalize_group_atmosphere_upload_header(value: Any) -> str:
        text = str(value or '').strip().lower()
        text = re.sub(r'[\s_\-/:：|｜（）()\[\]【】]+', '', text)
        return text

    def _normalize_group_atmosphere_upload_region_value(value: Any) -> str:
        raw = str(value or '').strip()
        if not raw:
            return ''
        enriched = _enrich_mcn_region_option(raw)
        return str(enriched.get('label_zh') or enriched.get('value') or raw).strip()

    def _group_atmosphere_upload_store_embedded_media(filename: str, content: bytes, mime_type: str) -> Dict[str, Any]:
        if not content:
            return {}
        try:
            result = service.create_group_atmosphere_media_asset(
                filename=filename or 'embedded-image.png',
                content=content,
                mime_type=mime_type or '',
                created_by='manual_upload_template',
            )
            media = dict(result.get('media') or {})
            if not media:
                return {}
            return {
                'asset_type': 'image_caption',
                'media_id': str(media.get('media_id') or ''),
                'media_path': str(media.get('media_path') or ''),
                'media_mime_type': str(media.get('mime_type') or mime_type or ''),
                'media_filename': str(media.get('filename') or filename or ''),
                'media_preview_url': str(media.get('preview_url') or (f"/api/ops/group-atmosphere/media-assets/{media.get('media_id')}/preview" if media.get('media_id') else '')),
            }
        except HTTPException:
            raise
        except Exception:
            return {}

    def _group_atmosphere_upload_media_for_target(target: str, archive: zipfile.ZipFile, *, base_dir: str = 'xl') -> Dict[str, Any]:
        value = str(target or '').strip().replace('\\', '/')
        if not value:
            return {}
        normalized_base = str(base_dir or '').strip('/ ')
        path = value.lstrip('/') if value.startswith('/') else f"{normalized_base}/{value}" if normalized_base and not value.startswith(f'{normalized_base}/') else value
        try:
            content = archive.read(path)
        except KeyError:
            return {}
        suffix = Path(path).suffix.lower()
        mime = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.webp': 'image/webp',
        }.get(suffix, '')
        return _group_atmosphere_upload_store_embedded_media(Path(path).name, content, mime)

    def _extract_group_atmosphere_dispimg_media_by_id(raw: bytes) -> Dict[str, Dict[str, Any]]:
        media_by_id: Dict[str, Dict[str, Any]] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                rel_xml = archive.read('xl/_rels/cellimages.xml.rels')
                image_xml = archive.read('xl/cellimages.xml')
                rel_root = ET.fromstring(rel_xml)
                rel_targets = {
                    str(rel.attrib.get('Id') or ''): str(rel.attrib.get('Target') or '')
                    for rel in rel_root.findall('{http://schemas.openxmlformats.org/package/2006/relationships}Relationship')
                }
                image_root = ET.fromstring(image_xml)
                ns_pic = '{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}'
                ns_a = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
                ns_r_embed = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed'
                for pic in image_root.findall(f'.//{ns_pic}pic'):
                    prop = pic.find(f'.//{ns_pic}cNvPr')
                    blip = pic.find(f'.//{ns_a}blip')
                    image_id = str((prop.attrib.get('name') if prop is not None else '') or '').strip()
                    rel_id = str((blip.attrib.get(ns_r_embed) if blip is not None else '') or '').strip()
                    target = rel_targets.get(rel_id, '')
                    if image_id and target:
                        media = _group_atmosphere_upload_media_for_target(target, archive)
                        if media.get('media_id'):
                            media_by_id[image_id] = media
        except KeyError:
            return {}
        except (zipfile.BadZipFile, ET.ParseError):
            return {}
        return media_by_id

    def _extract_group_atmosphere_dispimg_id(value: Any) -> str:
        text = str(value or '').strip()
        match = re.search(r'DISPIMG\(\s*"([^"]+)"', text, flags=re.I)
        return match.group(1).strip() if match else ''

    def _extract_group_atmosphere_openpyxl_anchored_media(sheet: Any) -> Dict[Tuple[int, int], Dict[str, Any]]:
        media_by_cell: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for index, image in enumerate(list(getattr(sheet, '_images', []) or []), start=1):
            try:
                anchor = image.anchor
                marker = anchor._from
                row = int(marker.row) + 1
                col = int(marker.col) + 1
                content = image._data()
            except Exception:
                continue
            image_format = str(getattr(image, 'format', '') or 'png').lower()
            suffix = '.jpg' if image_format in {'jpg', 'jpeg'} else f'.{image_format}'
            mime = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.webp': 'image/webp',
            }.get(suffix, '')
            media = _group_atmosphere_upload_store_embedded_media(f'embedded-row-{row}-{index}{suffix}', content, mime)
            if media.get('media_id'):
                media_by_cell[(row, col)] = media
        return media_by_cell

    def _group_atmosphere_upload_media_for_cell(
        *,
        row_number: int,
        image_col: Optional[int],
        row: List[Any],
        anchored_media_by_cell: Dict[Tuple[int, int], Dict[str, Any]],
        dispimg_media_by_id: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        if image_col is None:
            return {}
        cell_value = row[image_col] if image_col < len(row) else ''
        dispimg_id = _extract_group_atmosphere_dispimg_id(cell_value)
        if dispimg_id and dispimg_media_by_id.get(dispimg_id):
            return dict(dispimg_media_by_id[dispimg_id])
        media = anchored_media_by_cell.get((row_number, image_col + 1))
        return dict(media or {})

    def _extract_group_atmosphere_docx_rows_and_media(raw: bytes) -> tuple[List[List[str]], Dict[Tuple[int, int], Dict[str, Any]]]:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                document_xml = archive.read('word/document.xml')
                try:
                    rel_xml = archive.read('word/_rels/document.xml.rels')
                except KeyError:
                    rel_xml = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
                rel_root = ET.fromstring(rel_xml)
                rel_targets = {
                    str(rel.attrib.get('Id') or ''): str(rel.attrib.get('Target') or '')
                    for rel in rel_root.findall('{http://schemas.openxmlformats.org/package/2006/relationships}Relationship')
                }
                root = ET.fromstring(document_xml)
                ns_w = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
                ns_a = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
                ns_r_embed = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed'
                table = root.find(f'.//{ns_w}tbl')
                if table is None:
                    return [], {}
                rows: List[List[str]] = []
                media_by_cell: Dict[Tuple[int, int], Dict[str, Any]] = {}
                for row_index, tr in enumerate(table.findall(f'{ns_w}tr'), start=1):
                    row_values: List[str] = []
                    for col_index, tc in enumerate(tr.findall(f'{ns_w}tc'), start=1):
                        text = ''.join(str(node.text or '') for node in tc.findall(f'.//{ns_w}t')).strip()
                        row_values.append(text)
                        for blip in tc.findall(f'.//{ns_a}blip'):
                            rel_id = str(blip.attrib.get(ns_r_embed) or '').strip()
                            target = rel_targets.get(rel_id, '')
                            if not target:
                                continue
                            media = _group_atmosphere_upload_media_for_target(target, archive, base_dir='word')
                            if media.get('media_id'):
                                media_by_cell[(row_index, col_index)] = media
                                break
                    rows.append(row_values)
                return rows, media_by_cell
        except KeyError:
            raise HTTPException(status_code=400, detail='docx_parse_failed')
        except (zipfile.BadZipFile, ET.ParseError):
            raise HTTPException(status_code=400, detail='docx_parse_failed')

    def _xml_text(value: Any) -> str:
        return html.escape(str(value or ''), quote=False)

    def _xml_attr(value: Any) -> str:
        return html.escape(str(value or ''), quote=True)

    def _build_docx_paragraph_xml(text: str = '', *, bold: bool = False, align: str = 'left') -> str:
        align_xml = f'<w:pPr><w:jc w:val="{_xml_attr(align)}"/></w:pPr>' if align else ''
        bold_xml = '<w:rPr><w:b/></w:rPr>' if bold else ''
        value = _xml_text(text)
        space_attr = ' xml:space="preserve"' if value.startswith(' ') or value.endswith(' ') or not value else ''
        return f'<w:p>{align_xml}<w:r>{bold_xml}<w:t{space_attr}>{value}</w:t></w:r></w:p>'

    def _build_docx_cell_xml(text: str = '', *, width: int = 3000, bold: bool = False, shade: str = '', align: str = 'left', combo_items: Optional[List[str]] = None) -> str:
        shade_xml = f'<w:shd w:fill="{_xml_attr(shade)}"/>' if shade else ''
        tc_pr = f'<w:tcPr><w:tcW w:w="{int(width)}" w:type="dxa"/>{shade_xml}<w:vAlign w:val="center"/></w:tcPr>'
        if combo_items:
            items_xml = ''.join(f'<w:listItem w:displayText="{_xml_attr(item)}" w:value="{_xml_attr(item)}"/>' for item in combo_items)
            content = (
                '<w:sdt><w:sdtPr><w:alias w:val="可选地区"/><w:tag w:val="manual_upload_region"/>'
                f'<w:comboBox>{items_xml}</w:comboBox></w:sdtPr><w:sdtContent>'
                f'{_build_docx_paragraph_xml(text, bold=bold, align=align)}'
                '</w:sdtContent></w:sdt>'
            )
        else:
            content = _build_docx_paragraph_xml(text, bold=bold, align=align)
        return f'<w:tc>{tc_pr}{content}</w:tc>'

    def _build_docx_row_xml(cells: List[str]) -> str:
        return '<w:tr>' + ''.join(cells) + '</w:tr>'

    def _extract_group_atmosphere_phrase_rows_from_rows(
        rows: List[List[Any]],
        *,
        anchored_media_by_cell: Optional[Dict[Tuple[int, int], Dict[str, Any]]] = None,
        dispimg_media_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        if not rows:
            return []
        phrase_headers = {
            '话术', '话术内容', '话术列表', '具体话术', '文案', '文案内容', '内容',
            'caption', 'text', 'phrase', 'message', 'messagetext', 'content',
        }
        region_headers = {'地区', '地区选择', '国家地区', '国家', '区域', '市场', 'region', 'country', 'area'}
        role_headers = {'话术类型', '话术类型名字', '话术类型名称', '类型名字', '类型名称', '话术分类', 'phrasetype', 'rolepositioning', 'role', 'category'}
        image_headers = {'对应图片', '图片', '配图', '附图', 'image', 'images', 'media', 'picture', 'photo'}
        anchored_media_by_cell = anchored_media_by_cell or {}
        dispimg_media_by_id = dispimg_media_by_id or {}
        first_row = [_normalize_group_atmosphere_upload_header(value) for value in (rows[0] if rows else [])]
        if len(rows) >= 4 and len(first_row) >= 2 and first_row[0] in role_headers and first_row[1] in {'可选地区', '地区', '地区选择', '国家地区', '国家'}:
            role_value = str(rows[1][0] if len(rows[1]) >= 1 else '').strip()
            region_value = _normalize_group_atmosphere_upload_region_value(rows[1][1] if len(rows[1]) >= 2 else '')
            header_values = [_normalize_group_atmosphere_upload_header(value) for value in rows[2]]
            phrase_col = next((idx for idx, value in enumerate(header_values) if value in phrase_headers), 0)
            image_col = next((idx for idx, value in enumerate(header_values) if value in image_headers), 1 if len(header_values) > 1 else None)
            phrase_rows: List[Dict[str, Any]] = []
            for offset, row in enumerate(rows[3:], start=4):
                if phrase_col >= len(row):
                    continue
                text = str(row[phrase_col] or '').strip()
                if not text:
                    continue
                media = _group_atmosphere_upload_media_for_cell(
                    row_number=offset,
                    image_col=image_col,
                    row=row,
                    anchored_media_by_cell=anchored_media_by_cell,
                    dispimg_media_by_id=dispimg_media_by_id,
                )
                for line in [line.strip() for line in text.splitlines() if line.strip()]:
                    phrase_rows.append({
                        'text': line,
                        'region': region_value,
                        'role_positioning': role_value,
                        **media,
                    })
            return phrase_rows
        header_index = None
        phrase_col = None
        region_col = None
        role_col = None
        image_col = None
        for row_index, row in enumerate(rows[:5]):
            header_values = [_normalize_group_atmosphere_upload_header(value) for value in row]
            candidate_phrase_col = next((idx for idx, value in enumerate(header_values) if value in phrase_headers), None)
            if candidate_phrase_col is None:
                continue
            header_index = row_index
            phrase_col = candidate_phrase_col
            region_col = next((idx for idx, value in enumerate(header_values) if value in region_headers), None)
            role_col = next((idx for idx, value in enumerate(header_values) if value in role_headers), None)
            image_col = next((idx for idx, value in enumerate(header_values) if value in image_headers), None)
            break
        data_rows = rows[(header_index + 1):] if header_index is not None else rows
        data_start_row_number = (header_index + 2) if header_index is not None else 1
        if phrase_col is None:
            max_cols = max((len(row) for row in data_rows), default=0)
            def _score_cell(value: Any) -> int:
                text = str(value or '').strip()
                if not text:
                    return 0
                if re.fullmatch(r'\d{1,5}', text):
                    return -20
                if len(text) <= 3:
                    return -5
                score = min(len(text), 160)
                if re.search(r'[.!?。！？,，]\s*|\s', text):
                    score += 20
                if re.search(r'[A-Za-z]{3,}|[\u4e00-\u9fff]{3,}', text):
                    score += 20
                if re.search(r'(kak|admin|grup|daftar|bonus|aktif|tanya|share|cerita|hola|oi|amiga)', text, flags=re.I):
                    score += 30
                return score
            col_scores = []
            for idx in range(max_cols):
                values = [row[idx] for row in data_rows if idx < len(row)]
                non_empty = [str(v or '').strip() for v in values if str(v or '').strip()]
                score = sum(_score_cell(v) for v in values) + len(non_empty)
                col_scores.append((score, idx))
            phrase_col = max(col_scores, default=(0, 0))[1]
        if image_col is None and phrase_col is not None and phrase_col + 1 < max((len(row) for row in data_rows), default=0):
            image_col = phrase_col + 1
        phrase_rows: List[Dict[str, Any]] = []
        for offset, row in enumerate(data_rows, start=data_start_row_number):
            if phrase_col >= len(row):
                continue
            text = str(row[phrase_col] or '').strip()
            if not text:
                continue
            row_region = _normalize_group_atmosphere_upload_region_value(row[region_col]) if region_col is not None and region_col < len(row) else ''
            row_role = str(row[role_col] or '').strip() if role_col is not None and role_col < len(row) else ''
            media = _group_atmosphere_upload_media_for_cell(
                row_number=offset,
                image_col=image_col,
                row=row,
                anchored_media_by_cell=anchored_media_by_cell,
                dispimg_media_by_id=dispimg_media_by_id,
            )
            for line in [line.strip() for line in text.splitlines() if line.strip()]:
                phrase_rows.append({
                    'text': line,
                    'region': row_region,
                    'role_positioning': row_role,
                    **media,
                })
        return phrase_rows

    def _extract_group_atmosphere_phrase_lines_from_rows(rows: List[List[Any]]) -> List[str]:
        return [item['text'] for item in _extract_group_atmosphere_phrase_rows_from_rows(rows) if str(item.get('text') or '').strip()]

    def _group_atmosphere_phrase_payload_from_rows(
        rows: List[List[Any]],
        *,
        anchored_media_by_cell: Optional[Dict[Tuple[int, int], Dict[str, Any]]] = None,
        dispimg_media_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        phrases = _extract_group_atmosphere_phrase_rows_from_rows(
            rows,
            anchored_media_by_cell=anchored_media_by_cell,
            dispimg_media_by_id=dispimg_media_by_id,
        )
        return {
            'content': '\n'.join(str(item.get('text') or '').strip() for item in phrases if str(item.get('text') or '').strip()),
            'phrases': phrases,
        }

    def _parse_group_atmosphere_phrase_upload_file_payload(filename: str, raw: bytes) -> Dict[str, Any]:
        name = str(filename or '')
        if re.search(r'\.docx$', name, flags=re.I):
            rows, media_by_cell = _extract_group_atmosphere_docx_rows_and_media(raw)
            return _group_atmosphere_phrase_payload_from_rows(rows, anchored_media_by_cell=media_by_cell)
        if re.search(r'\.xlsx$', name, flags=re.I):
            try:
                workbook = load_workbook(io.BytesIO(raw), read_only=False, data_only=False)
            except Exception:
                raise HTTPException(status_code=400, detail='xlsx_parse_failed')
            sheet = workbook.active
            rows = [list(row) for row in sheet.iter_rows(values_only=True)]
            return _group_atmosphere_phrase_payload_from_rows(
                rows,
                anchored_media_by_cell=_extract_group_atmosphere_openpyxl_anchored_media(sheet),
                dispimg_media_by_id=_extract_group_atmosphere_dispimg_media_by_id(raw),
            )
        if re.search(r'\.csv$', name, flags=re.I):
            content = raw.decode('utf-8-sig', errors='ignore')
            rows = [row for row in csv.reader(io.StringIO(content))]
            return _group_atmosphere_phrase_payload_from_rows(rows)
        if re.search(r'\.xls$', name, flags=re.I):
            content = raw.decode('utf-8-sig', errors='ignore')
            if content.strip() and not content.lstrip().startswith('<') and not raw.startswith(b'\xd0\xcf\x11\xe0'):
                rows = [line.split('\t') if '\t' in line else line.split(',') for line in content.splitlines()]
                return _group_atmosphere_phrase_payload_from_rows(rows)
            try:
                import xlrd  # type: ignore
                workbook = xlrd.open_workbook(file_contents=raw)
                sheet = workbook.sheet_by_index(0)
                rows = [[sheet.row(r)[c].value for c in range(sheet.ncols)] for r in range(sheet.nrows)]
                return _group_atmosphere_phrase_payload_from_rows(rows)
            except ImportError:
                raise HTTPException(status_code=400, detail='xls_parser_not_installed')
            except Exception:
                raise HTTPException(status_code=400, detail='xls_parse_failed')
        if re.search(r'\.txt$', name, flags=re.I):
            return {'content': raw.decode('utf-8-sig', errors='ignore'), 'phrases': []}
        raise HTTPException(status_code=400, detail='unsupported_phrase_file_type')

    def _parse_group_atmosphere_phrase_upload_file(filename: str, raw: bytes) -> str:
        return str(_parse_group_atmosphere_phrase_upload_file_payload(filename, raw).get('content') or '')

    def _group_atmosphere_upload_phrases_have_metadata(phrases: List[Dict[str, Any]]) -> bool:
        return any(
            str((item or {}).get('region') or '').strip()
            or str((item or {}).get('language') or '').strip()
            or str((item or {}).get('role_positioning') or '').strip()
            or str((item or {}).get('media_id') or '').strip()
            for item in phrases
        )

    def _build_group_atmosphere_manual_upload_template_xlsx() -> bytes:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = '模板'
        sheet.append(['话术类型', '可选地区'])
        sheet.append(['', ''])
        sheet.append(['话术内容', '对应图片'])
        for _ in range(97):
            sheet.append(['', ''])
        sheet.freeze_panes = 'A4'
        sheet.column_dimensions['A'].width = 54
        sheet.column_dimensions['B'].width = 42
        sheet.row_dimensions[2].height = 22
        sheet.row_dimensions[3].height = 24
        for row_index in range(4, 101):
            sheet.row_dimensions[row_index].height = 72
        header_fill = PatternFill('solid', fgColor='DBEAFE')
        section_fill = PatternFill('solid', fgColor='E2E8F0')
        for cell in sheet[1]:
            cell.font = Font(bold=True, color='0F172A')
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center', vertical='center')
        for cell in sheet[3]:
            cell.font = Font(bold=True, color='0F172A')
            cell.fill = section_fill
            cell.alignment = Alignment(horizontal='center', vertical='center')
        for row in sheet.iter_rows(min_row=2, max_row=100, max_col=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical='top')
        options = workbook.create_sheet('可选值')
        try:
            region_options = list(service.list_mcn_region_options(include_disabled=False).get('enabled_options') or [])
        except Exception:
            region_options = []
        region_labels = [
            str((item or {}).get('label_zh') or (item or {}).get('value') or (item or {}).get('label') or '').strip()
            for item in region_options
            if str((item or {}).get('label_zh') or (item or {}).get('value') or (item or {}).get('label') or '').strip()
        ]
        if not region_labels:
            region_labels = ['印尼', '墨西哥', '巴西']
        sheet['A2'] = ''
        sheet['B2'] = region_labels[0]
        options.append(['可选地区'])
        for region_label in region_labels:
            options.append([region_label])
        for cell in options[1]:
            cell.font = Font(bold=True, color='0F172A')
            cell.fill = PatternFill('solid', fgColor='E2E8F0')
        options.column_dimensions['A'].width = 18
        region_last_row = max(2, len(region_labels) + 1)
        region_validation = DataValidation(type='list', formula1=f"'可选值'!$A$2:$A${region_last_row}", allow_blank=False)
        sheet.add_data_validation(region_validation)
        region_validation.add('B2')
        options.sheet_state = 'hidden'
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def _build_group_atmosphere_manual_upload_template_docx() -> bytes:
        try:
            region_options = list(service.list_mcn_region_options(include_disabled=False).get('enabled_options') or [])
        except Exception:
            region_options = []
        region_labels = [
            str((item or {}).get('label_zh') or (item or {}).get('value') or (item or {}).get('label') or '').strip()
            for item in region_options
            if str((item or {}).get('label_zh') or (item or {}).get('value') or (item or {}).get('label') or '').strip()
        ]
        if not region_labels:
            region_labels = ['印尼', '墨西哥', '巴西']
        default_region = region_labels[0]
        rows = [
            _build_docx_row_xml([
                _build_docx_cell_xml('话术类型', width=5300, bold=True, shade='DBEAFE', align='center'),
                _build_docx_cell_xml('可选地区', width=5300, bold=True, shade='DBEAFE', align='center'),
            ]),
            _build_docx_row_xml([
                _build_docx_cell_xml('', width=5300, align='center'),
                _build_docx_cell_xml(default_region, width=5300, align='center', combo_items=region_labels),
            ]),
            _build_docx_row_xml([
                _build_docx_cell_xml('话术内容', width=5300, bold=True, shade='E2E8F0'),
                _build_docx_cell_xml('对应图片', width=5300, bold=True, shade='E2E8F0'),
            ]),
        ]
        for _ in range(18):
            rows.append(_build_docx_row_xml([
                _build_docx_cell_xml('', width=5300),
                _build_docx_cell_xml('', width=5300),
            ]))
        document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <w:p><w:pPr><w:spacing w:after="160"/></w:pPr><w:r><w:rPr><w:b/><w:sz w:val="32"/></w:rPr><w:t>人工上传图文话术模板</w:t></w:r></w:p>
    <w:p><w:pPr><w:spacing w:after="200"/></w:pPr><w:r><w:rPr><w:color w:val="475569"/><w:sz w:val="20"/></w:rPr><w:t>填写 A2 话术类型，B2 选择地区；同名归入已有类型，未匹配则确认导入时自动新建。从第 4 行开始，每行一条话术，右侧单元格直接粘贴图片。</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr><w:tblW w:w="10600" w:type="dxa"/><w:tblBorders><w:top w:val="single" w:sz="6" w:color="CBD5E1"/><w:left w:val="single" w:sz="6" w:color="CBD5E1"/><w:bottom w:val="single" w:sz="6" w:color="CBD5E1"/><w:right w:val="single" w:sz="6" w:color="CBD5E1"/><w:insideH w:val="single" w:sz="6" w:color="CBD5E1"/><w:insideV w:val="single" w:sz="6" w:color="CBD5E1"/></w:tblBorders><w:tblCellMar><w:top w:w="120" w:type="dxa"/><w:left w:w="120" w:type="dxa"/><w:bottom w:w="120" w:type="dxa"/><w:right w:w="120" w:type="dxa"/></w:tblCellMar></w:tblPr>
      <w:tblGrid><w:gridCol w:w="5300"/><w:gridCol w:w="5300"/></w:tblGrid>
      {''.join(rows)}
    </w:tbl>
    <w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1080" w:right="900" w:bottom="1080" w:left="900" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>
  </w:body>
</w:document>'''
        content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'''
        package_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'''
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('[Content_Types].xml', content_types)
            archive.writestr('_rels/.rels', package_rels)
            archive.writestr('word/document.xml', document_xml)
        return buffer.getvalue()

    @app.get('/api/ops/group-atmosphere/phrases/manual-upload-template.xlsx')
    def group_atmosphere_phrases_manual_upload_template() -> Response:
        return Response(
            content=_build_group_atmosphere_manual_upload_template_xlsx(),
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': 'attachment; filename="group_atmosphere_manual_phrases_template.xlsx"'},
        )

    @app.get('/api/ops/group-atmosphere/phrases/manual-upload-template.docx')
    def group_atmosphere_phrases_manual_upload_template_docx() -> Response:
        return Response(
            content=_build_group_atmosphere_manual_upload_template_docx(),
            media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            headers={'Content-Disposition': 'attachment; filename="group_atmosphere_manual_phrases_template.docx"'},
        )

    @app.post('/api/ops/group-atmosphere/phrases/manual-upload-preview-file')
    async def group_atmosphere_phrases_manual_upload_preview_file(
        file: UploadFile = File(...),
        region: str = Form('印尼'),
        language: str = Form('id'),
        role_positioning: str = Form(''),
    ) -> Dict[str, Any]:
        raw = await _read_limited_upload_file(
            file,
            max_bytes=GROUP_ATMOSPHERE_PHRASE_UPLOAD_MAX_BYTES,
            detail='phrase_upload_file_too_large',
        )
        parsed = _parse_group_atmosphere_phrase_upload_file_payload(str(file.filename or ''), raw)
        content = str(parsed.get('content') or '')
        _assert_upload_line_limit(
            content,
            max_lines=GROUP_ATMOSPHERE_PHRASE_UPLOAD_MAX_LINES,
            detail='phrase_upload_line_count_exceeded',
        )
        phrases = [dict(item or {}) for item in list(parsed.get('phrases') or []) if str((item or {}).get('text') or '').strip()]
        payload = {
            'region': region,
            'language': language,
            'role_positioning': role_positioning,
            'content': '' if phrases else content,
        }
        if phrases:
            payload['phrases'] = phrases
        return service.preview_manual_upload_group_atmosphere_phrases(payload)

    @app.post('/api/ops/group-atmosphere/phrases/manual-upload-preview')
    def group_atmosphere_phrases_manual_upload_preview(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.preview_manual_upload_group_atmosphere_phrases(payload)

    @app.post('/api/ops/group-atmosphere/phrases/manual-upload-translate')
    def group_atmosphere_phrases_manual_upload_translate(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.translate_group_atmosphere_manual_upload_text(payload)

    @app.post('/api/ops/group-atmosphere/phrases/manual-upload-confirm')
    def group_atmosphere_phrases_manual_upload_confirm(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.confirm_manual_upload_group_atmosphere_phrases(payload)

    @app.post('/api/ops/group-atmosphere/phrases/manual-upload-file')
    async def group_atmosphere_phrases_manual_upload_file(
        file: UploadFile = File(...),
        role_key: str = Form(''),
        role_name: str = Form(''),
        region: str = Form('印尼'),
        language: str = Form('id'),
        role_positioning: str = Form(''),
    ) -> Dict[str, Any]:
        raw = await _read_limited_upload_file(
            file,
            max_bytes=GROUP_ATMOSPHERE_PHRASE_UPLOAD_MAX_BYTES,
            detail='phrase_upload_file_too_large',
        )
        parsed = _parse_group_atmosphere_phrase_upload_file_payload(str(file.filename or ''), raw)
        content = str(parsed.get('content') or '')
        _assert_upload_line_limit(
            content,
            max_lines=GROUP_ATMOSPHERE_PHRASE_UPLOAD_MAX_LINES,
            detail='phrase_upload_line_count_exceeded',
        )
        phrases = [dict(item or {}) for item in list(parsed.get('phrases') or []) if str((item or {}).get('text') or '').strip()]
        if phrases and _group_atmosphere_upload_phrases_have_metadata(phrases):
            preview = service.preview_manual_upload_group_atmosphere_phrases({
                'region': region,
                'language': language,
                'role_positioning': role_positioning,
                'content': '',
                'phrases': phrases,
            })
            selected_items = [dict(item or {}) for item in list(preview.get('items') or []) if (item or {}).get('selected', True) is not False]
            result = service.confirm_manual_upload_group_atmosphere_phrases({'items': selected_items})
            result['preview_summary'] = preview.get('summary') or {}
            return result
        return service.manual_upload_group_atmosphere_phrases({
            'role_key': role_key,
            'role_name': role_name,
            'region': region,
            'language': language,
            'role_positioning': role_positioning,
            'content': content,
        })

    @app.post('/api/ops/group-atmosphere/phrases/manual-upload')
    def group_atmosphere_phrases_manual_upload(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.manual_upload_group_atmosphere_phrases(payload)

    @app.post('/api/ops/group-atmosphere/phrases/move')
    def group_atmosphere_phrases_move(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.move_group_atmosphere_phrases(payload)

    @app.delete('/api/ops/group-atmosphere/roles/{role_key}')
    def group_atmosphere_role_delete(role_key: str) -> Dict[str, Any]:
        return service.delete_group_atmosphere_role(role_key)

    @app.get('/api/ops/group-atmosphere/roles/{role_key}/usage')
    def group_atmosphere_role_usage(role_key: str) -> Dict[str, Any]:
        return service.group_atmosphere_role_usage(role_key)

    @app.get('/api/ops/group-atmosphere/role-bindings')
    def group_atmosphere_role_bindings() -> Dict[str, Any]:
        return service.list_group_atmosphere_role_bindings()

    @app.get('/api/ops/group-atmosphere/trigger-rules')
    def group_atmosphere_trigger_rules(relationship_key: str = '') -> Dict[str, Any]:
        return service.list_group_atmosphere_trigger_rules(relationship_key)

    @app.post('/api/ops/group-atmosphere/trigger-rules')
    def group_atmosphere_trigger_rules_upsert(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.upsert_group_atmosphere_trigger_rule(payload)

    @app.delete('/api/ops/group-atmosphere/trigger-rules/{rule_id}')
    def group_atmosphere_trigger_rules_delete(rule_id: str) -> Dict[str, Any]:
        return service.delete_group_atmosphere_trigger_rule(rule_id)


    @app.post('/api/ops/group-atmosphere/role-bindings/{binding_id}/trigger-rules')
    def group_atmosphere_role_binding_trigger_rules_upsert(binding_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        binding = service.get_group_atmosphere_role_binding(binding_id)
        data = dict(payload or {})
        data['relationship_key'] = str(binding.get('role_key') or '')
        return service.upsert_group_atmosphere_trigger_rule(data)

    @app.post('/api/ops/group-atmosphere/role-bindings')
    def group_atmosphere_role_bindings_upsert(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.upsert_group_atmosphere_role_bindings(payload)

    @app.post('/api/ops/group-atmosphere/role-bindings/{binding_id}')
    def group_atmosphere_role_binding_update(binding_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.update_group_atmosphere_role_binding(binding_id, payload)

    @app.delete('/api/ops/group-atmosphere/role-bindings/{binding_id}')
    def group_atmosphere_role_binding_delete(binding_id: str) -> Dict[str, Any]:
        return service.delete_group_atmosphere_role_binding(binding_id)

    @app.post('/api/ops/group-atmosphere/role-bindings/{binding_id}/trigger')
    def group_atmosphere_role_binding_trigger(binding_id: str) -> Dict[str, Any]:
        return service.trigger_group_atmosphere_role_binding(binding_id)

    @app.get('/api/ops/group-atmosphere/learning-accounts')
    def group_atmosphere_learning_accounts() -> Dict[str, Any]:
        return service.list_group_atmosphere_learning_accounts()

    @app.post('/api/ops/group-atmosphere/learning-accounts')
    def group_atmosphere_learning_accounts_upsert(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.upsert_group_atmosphere_learning_account(payload)

    @app.delete('/api/ops/group-atmosphere/learning-accounts/{learning_account_key}')
    def group_atmosphere_learning_accounts_delete(learning_account_key: str) -> Dict[str, Any]:
        return service.delete_group_atmosphere_learning_account(learning_account_key)

    @app.post('/api/ops/group-atmosphere/learning-accounts/{learning_account_key}/learn-once')
    def group_atmosphere_learning_account_learn_once(learning_account_key: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.learn_once_group_atmosphere_learning_account(learning_account_key, payload)

    @app.post('/api/ops/group-atmosphere/accounts')
    def group_atmosphere_account_upsert(payload: GroupAtmosphereWhatsAppAccountRequest) -> Dict[str, Any]:
        return service.upsert_group_atmosphere_whatsapp_account(payload)

    @app.get('/api/ops/group-atmosphere/accounts')
    def group_atmosphere_accounts() -> Dict[str, Any]:
        return service.get_group_atmosphere_whatsapp_accounts()

    @app.get('/api/ops/group-atmosphere/summary')
    def group_atmosphere_summary() -> Dict[str, Any]:
        return service.group_atmosphere_summary_snapshot()

    @app.get('/api/ops/group-atmosphere/accounts/{account_key}/session')
    def group_atmosphere_account_session(account_key: str) -> Dict[str, Any]:
        return service.get_group_atmosphere_whatsapp_account_session(account_key)

    @app.post('/api/ops/group-atmosphere/accounts/{account_key}/groups/refresh-names')
    def group_atmosphere_account_group_names_refresh(account_key: str) -> Dict[str, Any]:
        return service.refresh_group_atmosphere_whatsapp_account_group_names(account_key)

    @app.post('/api/ops/group-atmosphere/accounts/{account_key}/session/start')
    def group_atmosphere_account_session_start(account_key: str) -> Dict[str, Any]:
        return service.start_group_atmosphere_whatsapp_account_session(account_key)

    @app.post('/api/ops/group-atmosphere/accounts/{account_key}/session/reset')
    def group_atmosphere_account_session_reset(account_key: str) -> Dict[str, Any]:
        return service.start_group_atmosphere_whatsapp_account_session(account_key, reset=True)

    @app.post('/api/ops/group-atmosphere/accounts/{account_key}/runtime/stop')
    def group_atmosphere_account_runtime_stop(account_key: str) -> Dict[str, Any]:
        return service.stop_group_atmosphere_whatsapp_account_runtime(account_key)

    @app.delete('/api/ops/group-atmosphere/accounts/{account_key}')
    def group_atmosphere_account_delete(account_key: str) -> Dict[str, Any]:
        return service.delete_whatsapp_approval_account(account_key)

    @app.post('/api/ops/group-atmosphere/accounts/{account_key}/chat-records/upload')
    async def group_atmosphere_account_chat_records_upload(account_key: str, request: Request) -> Dict[str, Any]:
        payload = await _read_limited_json_request(
            request,
            max_bytes=GROUP_ATMOSPHERE_CHAT_RECORD_JSON_MAX_BYTES,
            detail='chat_records_payload_too_large',
        )
        content = str((payload or {}).get('content') or '')
        _assert_upload_line_limit(content, max_lines=GROUP_ATMOSPHERE_CHAT_RECORD_MAX_LINES, detail='chat_records_line_count_exceeded')
        records = service._parse_group_atmosphere_chat_export(content)
        return service.import_group_atmosphere_chat_records_for_account(account_key, records)

    @app.post('/api/ops/group-atmosphere/chat-records/auto-learn')
    async def group_atmosphere_chat_records_auto_learn(request: Request) -> Dict[str, Any]:
        payload = await _read_limited_json_request(
            request,
            max_bytes=GROUP_ATMOSPHERE_CHAT_RECORD_JSON_MAX_BYTES,
            detail='chat_records_payload_too_large',
        )
        content = str((payload or {}).get('content') or '')
        _assert_upload_line_limit(content, max_lines=GROUP_ATMOSPHERE_CHAT_RECORD_MAX_LINES, detail='chat_records_line_count_exceeded')
        return service.auto_learn_group_atmosphere_chat_records(
            filename=str((payload or {}).get('filename') or ''),
            content=content,
            files=(payload or {}).get('files') if isinstance((payload or {}).get('files'), list) else None,
            role_positioning=str((payload or {}).get('role_positioning') or ''),
        )

    @app.post('/api/ops/group-atmosphere/accounts/{account_key}/groups/{group_index}/send')
    def group_atmosphere_account_group_send(account_key: str, group_index: int, payload: GroupAtmosphereManualSendRequest) -> Dict[str, Any]:
        return service.send_group_atmosphere_account_group_message(account_key, group_index, payload)

    @app.get('/api/ops/group-atmosphere/candidate-pool')
    def group_atmosphere_candidate_pool() -> Dict[str, Any]:
        return service.list_group_atmosphere_candidate_pool()

    @app.post('/api/ops/group-atmosphere/candidate-pool/enable')
    def group_atmosphere_candidate_pool_enable(payload: GroupAtmosphereCandidateEnableRequest) -> Dict[str, Any]:
        return service.enable_group_atmosphere_candidates(payload)

    @app.post('/api/ops/group-atmosphere/candidate-pool/add-to-role')
    def group_atmosphere_candidate_pool_add_to_role(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.add_group_atmosphere_candidates_to_role(payload)

    @app.post('/api/ops/group-atmosphere/candidate-pool/reorder')
    def group_atmosphere_candidate_pool_reorder(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.reorder_group_atmosphere_candidates(payload)

    @app.post('/api/ops/group-atmosphere/candidate-pool/move-type')
    def group_atmosphere_candidate_pool_move_type(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.move_group_atmosphere_candidates_to_type(payload)

    @app.delete('/api/ops/group-atmosphere/candidate-pool/{config_name}/{candidate_id}')
    def group_atmosphere_candidate_pool_delete(config_name: str, candidate_id: str) -> Dict[str, Any]:
        return service.delete_group_atmosphere_candidate(config_name, candidate_id)

    @app.post('/api/ops/group-atmosphere/candidate-pool/custom')
    def group_atmosphere_candidate_pool_custom(payload: GroupAtmosphereCandidateCustomRequest) -> Dict[str, Any]:
        return service.save_group_atmosphere_custom_candidate(payload)

    @app.post('/api/ops/group-atmosphere/candidate-pool/translate')
    def group_atmosphere_candidate_pool_translate(payload: GroupAtmosphereCandidateTranslateRequest) -> Dict[str, Any]:
        return service.translate_group_atmosphere_candidate(payload)

    @app.post('/api/ops/group-atmosphere/candidate-pool/translations/preprocess')
    def group_atmosphere_candidate_pool_translation_preprocess(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
        return service.preprocess_group_atmosphere_candidate_translations(limit=int((payload or {}).get('limit') or 50), retry_failed=bool((payload or {}).get('retry_failed')))

    @app.post('/api/ops/group-atmosphere/candidate-pool/translations/retry-failed')
    def group_atmosphere_candidate_pool_translation_retry_failed(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
        return service.retry_failed_group_atmosphere_candidate_translations(limit=int((payload or {}).get('limit') or 100))

    @app.post('/api/ops/group-atmosphere/speech-plans/{config_name}/rename')
    def group_atmosphere_speech_plan_rename(config_name: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return service.rename_group_atmosphere_speech_plan(config_name, payload)

    @app.delete('/api/ops/group-atmosphere/speech-plans/{config_name}')
    def group_atmosphere_speech_plan_delete(config_name: str) -> Dict[str, Any]:
        return service.delete_group_atmosphere_speech_plan(config_name)

    @app.get('/api/ops/group-atmosphere/scheduler/status')
    def group_atmosphere_scheduler_status() -> Dict[str, Any]:
        return service.group_atmosphere_scheduler_status()

    @app.post('/api/ops/group-atmosphere/scheduler/run-due')
    def group_atmosphere_scheduler_run_due(payload: GroupAtmosphereSchedulerRunRequest) -> Dict[str, Any]:
        return service.run_due_group_atmosphere_scheduler(payload)

    @app.post('/api/ops/group-atmosphere/configs')
    def group_atmosphere_config_upsert(payload: GroupAtmosphereConfigRequest) -> Dict[str, Any]:
        return {'ok': True, 'config': service.upsert_group_atmosphere_config(payload)}

    @app.get('/api/ops/group-atmosphere/configs')
    def group_atmosphere_config_list() -> Dict[str, Any]:
        return {'rows': service.list_group_atmosphere_configs()}

    @app.post('/api/ops/group-atmosphere/dispatch-once')
    def group_atmosphere_dispatch_once(payload: GroupAtmosphereDispatchRequest) -> Dict[str, Any]:
        return service.dispatch_group_atmosphere_once(payload)

    @app.post('/api/ops/group-atmosphere/inbound-message')
    def group_atmosphere_inbound_message(payload: GroupAtmosphereInboundMessageRequest) -> Dict[str, Any]:
        return service.handle_group_atmosphere_inbound_message(payload)

    @app.post('/api/internal/group-atmosphere/inbound-message')
    def internal_group_atmosphere_inbound_message(payload: GroupAtmosphereInboundMessageRequest) -> Dict[str, Any]:
        return service.handle_group_atmosphere_inbound_message(payload)

    @app.post('/api/internal/group-atmosphere/trigger-event')
    def internal_group_atmosphere_trigger_event(payload: GroupAtmosphereTriggerEventRequest) -> Dict[str, Any]:
        return service.handle_group_atmosphere_trigger_event(payload)

    @app.post('/api/ops/group-atmosphere/import-chat-records')
    def group_atmosphere_import_chat_records(payload: GroupAtmosphereImportChatRecordsRequest) -> Dict[str, Any]:
        return service.import_group_atmosphere_chat_records(payload)

    @app.post('/api/ops/group-atmosphere/ai-candidates')
    def group_atmosphere_ai_candidates(payload: GroupAtmosphereAiCandidateRequest) -> Dict[str, Any]:
        return service.generate_group_atmosphere_ai_candidates(payload)

    @app.post('/api/ops/group-atmosphere/simulate')
    def group_atmosphere_simulate(payload: GroupAtmosphereSimulationRequest) -> Dict[str, Any]:
        return service.simulate_group_atmosphere(payload)

    @app.get('/api/ops/group-atmosphere/logs')
    def group_atmosphere_logs(limit: int = 50) -> Dict[str, Any]:
        return {'rows': service.list_group_atmosphere_logs(limit)}

    @app.get('/login', response_class=HTMLResponse)
    def ops_login_page() -> str:
        if not auth_enabled:
            return '<html><body>auth disabled</body></html>'
        return _ops_login_page_html()

    @app.get('/api/ops/auth/status')
    def ops_auth_status(request: Request, response: Response) -> Dict[str, Any]:
        user = _request_session_user(request) if auth_enabled else _request_session_user(request)
        if user:
            _refresh_ops_session_cookie(request, response)
        return {
            'auth_enabled': auth_enabled,
            'authenticated': bool(user),
            'bootstrap_open': auth_enabled and (not auth_manager.has_users()),
            'user': user,
        }

    @app.post('/api/ops/auth/bootstrap')
    def ops_auth_bootstrap(request: Request, response: Response, payload: OpsAuthBootstrapRequest) -> Dict[str, Any]:
        if not auth_enabled:
            raise HTTPException(status_code=400, detail='ops_auth_disabled')
        try:
            user = auth_manager.bootstrap_admin(
                username=payload.username,
                password=payload.password,
                display_name=payload.display_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raw_token = auth_manager.create_session(
            user,
            ip_address=str(request.client.host if request.client else ''),
            user_agent=request.headers.get('user-agent'),
        )
        auth_manager.apply_session_cookie(response, raw_token)
        return {'ok': True, 'user': user}

    @app.post('/api/ops/auth/login')
    def ops_auth_login(request: Request, response: Response, payload: OpsAuthLoginRequest) -> Dict[str, Any]:
        if not auth_enabled:
            raise HTTPException(status_code=400, detail='ops_auth_disabled')
        user = auth_manager.authenticate(payload.username, payload.password)
        if not user:
            raise HTTPException(status_code=401, detail='invalid_credentials')
        raw_token = auth_manager.create_session(
            user,
            ip_address=str(request.client.host if request.client else ''),
            user_agent=request.headers.get('user-agent'),
        )
        auth_manager.apply_session_cookie(response, raw_token)
        return {'ok': True, 'user': user}

    @app.post('/api/ops/auth/logout')
    def ops_auth_logout(request: Request, response: Response) -> Dict[str, Any]:
        raw_token = request.cookies.get(auth_manager.cookie_name)
        auth_manager.revoke_session(raw_token)
        auth_manager.clear_session_cookie(response)
        return {'ok': True}

    @app.post('/api/ops/auth/password')
    def ops_auth_change_password(request: Request, payload: OpsPasswordChangeRequest) -> Dict[str, Any]:
        user = _require_ops_user(request)
        if str(user.get('role') or '').strip() == OPS_AUTH_ROLE_INTERNAL:
            raise HTTPException(status_code=403, detail='ops_browser_session_required')
        try:
            updated_user = auth_manager.change_user_password(
                str(user.get('user_id') or ''),
                current_password=payload.current_password,
                new_password=payload.new_password,
            )
        except ValueError as exc:
            status_code = 401 if str(exc) == 'invalid_current_password' else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return {'ok': True, 'user': updated_user}

    def _ops_nav_html(role: Optional[str] = None, *, nav_class: str = 'shell-nav') -> str:
        normalized_role = str(role or '').strip().lower()
        admin_items = [
            ('/ops', '管理员看板'),
            ('/ops/ad-data-dashboard', '广告数据看板'),
            ('/ops/intake-submit', '绑定中心'),
            ('/ops/timo-membership-query', 'Timo 入会查询'),
            ('/ops/production-ops', '群审批控制台'),
            ('/ops/group-atmosphere', '群聊天助手'),
            ('/ops/accounts', '账号设置'),
        ]
        super_admin_items = [
            ('/ops', '管理员看板'),
            ('/ops/ad-data-dashboard', '广告数据看板'),
            ('/ops/streamer-analytics', '主播数据分析'),
            ('/ops/intake-submit', '绑定中心'),
            ('/ops/timo-membership-query', 'Timo 入会查询'),
            ('/ops/production-ops', '群审批控制台'),
            ('/ops/group-atmosphere', '群聊天助手'),
            ('/ops/accounts', '账号设置'),
        ]
        business_items = [
            ('/ops/intake-submit', '绑定中心'),
            ('/ops/timo-membership-query', 'Timo 入会查询'),
            ('/ops/production-ops', '群审批控制台'),
            ('/ops/group-atmosphere', '群聊天助手'),
            ('/ops/accounts', '账号设置'),
        ]
        if normalized_role == OPS_AUTH_ROLE_SUPER_ADMIN:
            items = super_admin_items
        elif ops_role_is_business(normalized_role):
            items = business_items
        else:
            items = admin_items
        links = ''.join(f'<a href="{href}">{label}</a>' for href, label in items)
        return f'<div class="{nav_class}">{links}</div>'

    def _normalize_ops_nav(html: str, role: Optional[str] = None) -> str:
        def repl(match: re.Match) -> str:
            nav_class = 'nav' if re.search(r"class=[\"']nav[\"']", match.group(0)) else 'shell-nav'
            return _ops_nav_html(role, nav_class=nav_class)
        return re.sub(r"<div\s+class=[\"'](?:shell-nav|nav)[\"'][^>]*>.*?</div>", repl, html, count=1, flags=re.S)

    OPS_LOCAL_BASE_SELECTOR_RE = re.compile(
        r'(^|[,\s])(?:\.page-shell|\.shell-nav|\.nav|\.hero|\.card|\.toolbar|table|th|td)(?:\s*(?:[,{:+>#.~]|\[)|\s*$)',
        re.I,
    )

    def _strip_ops_local_base_layout_css(html: str) -> str:
        """Keep page-specific CSS, but remove local redeclarations of shared shell/card/table primitives."""
        def strip_style(match: re.Match) -> str:
            attrs = match.group(1) or ''
            css = match.group(2) or ''
            if 'data-ops-shell-normalized' in attrs:
                return match.group(0)
            css = re.sub(
                r'([^{}]+)\{([^{}]*)\}',
                lambda rule: '' if OPS_LOCAL_BASE_SELECTOR_RE.search(rule.group(1)) else rule.group(0),
                css,
                flags=re.S,
            )
            return f'<style{attrs}>{css}</style>'
        return re.sub(r'<style([^>]*)>(.*?)</style>', strip_style, html, flags=re.S | re.I)

    def _with_ops_shell_style(html: str, role: Optional[str] = None, page: Optional[str] = None) -> str:
        """Apply one shared backend shell layout and one shared backend navigation across independently authored ops pages."""
        html = _strip_ops_local_base_layout_css(html)
        html = _normalize_ops_nav(html, role)
        if page and 'data-ops-shell-page=' not in html:
            safe_page = re.sub(r'[^a-zA-Z0-9_-]', '', str(page))
            html = re.sub(r'(<(?:div|main)\s+class=["\'](?:page-shell|page)["\'])', rf'\1 data-ops-shell-page="{safe_page}"', html, count=1)
        if 'data-ops-shell-normalized="true"' in html:
            return html
        shell_style = '''
<style data-ops-shell-normalized="true">
/* CRM UI system v2: unified light dashboard tokens, typography, spacing */
:root{
  --ops-font:Inter,-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,"Helvetica Neue",Arial,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  --ops-mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono",monospace;
  --ops-nav-width:248px;
  --ops-content-left-gap:32px;
  --ops-card-gap:16px;
  --ops-hero-min-height:72px;
  --ops-table-row-padding-y:11px;
  --ops-bg:#eef3f9;
  --ops-bg-2:#f6f8fc;
  --ops-panel:#ffffff;
  --ops-surface:#f8fbff;
  --ops-surface-2:#f2f6fc;
  --ops-border:#e5ebf3;
  --ops-border-strong:#d7e0ec;
  --ops-text:#172033;
  --ops-text-2:#334155;
  --ops-muted:#718095;
  --ops-faint:#9aa7b7;
  --ops-blue:#2f6bff;
  --ops-blue-hover:#1f55d9;
  --ops-blue-soft:#edf4ff;
  --ops-green:#16a34a;
  --ops-green-soft:#dcfce7;
  --ops-amber:#d97706;
  --ops-amber-soft:#fff7ed;
  --ops-red:#dc2626;
  --ops-red-soft:#fff1f2;
  --ops-r-sm:10px;
  --ops-r-md:14px;
  --ops-r-lg:18px;
  --ops-r-xl:22px;
  --ops-r-2xl:28px;
  --ops-space-1:4px;
  --ops-space-2:8px;
  --ops-space-3:12px;
  --ops-space-4:16px;
  --ops-space-5:20px;
  --ops-space-6:24px;
  --ops-space-8:32px;
  --ops-shadow:0 14px 36px rgba(38,55,91,.075);
  --ops-shadow-soft:0 6px 18px rgba(38,55,91,.05);
  --ops-shadow-card:0 1px 0 rgba(15,23,42,.02),0 8px 22px rgba(38,55,91,.045);
}
html{scrollbar-gutter:stable;overflow-x:hidden!important;background:var(--ops-bg)!important;font-size:14px!important;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;}
body{margin:0!important;padding:24px!important;min-height:100vh!important;overflow-x:hidden!important;background:radial-gradient(circle at 12% -8%,rgba(47,107,255,.10),transparent 31%),linear-gradient(180deg,#f7f9fd 0%,#eef3f9 100%)!important;color:var(--ops-text)!important;font-family:var(--ops-font)!important;font-size:14px!important;line-height:1.5!important;letter-spacing:0!important;}
body *{box-sizing:border-box;}
.page-shell,body>.page{width:min(1480px,calc(100vw - 48px))!important;max-width:1480px!important;margin:0 auto!important;padding:0 0 32px!important;display:grid!important;grid-template-columns:var(--ops-nav-width) minmax(0,1fr)!important;gap:var(--ops-card-gap) var(--ops-content-left-gap)!important;align-items:start!important;}
.shell-nav,.nav{grid-column:1!important;grid-row:1 / span 160!important;position:fixed!important;top:24px!important;left:max(24px,calc((100vw - 1480px)/2))!important;width:var(--ops-nav-width)!important;height:calc(100vh - 48px)!important;min-height:0!important;overflow-y:auto!important;z-index:30!important;display:flex!important;flex-direction:column!important;align-items:stretch!important;gap:6px!important;flex-wrap:nowrap!important;margin:0!important;padding:22px 16px!important;background:rgba(255,255,255,.96)!important;backdrop-filter:blur(14px) saturate(140%)!important;border:1px solid var(--ops-border)!important;border-radius:var(--ops-r-2xl)!important;box-shadow:var(--ops-shadow)!important;}
.shell-nav::before,.nav::before{content:'MCN 客服工具';display:block;margin:0 8px 18px;padding:0 0 16px;border-bottom:1px solid var(--ops-border);color:#111827;font-size:18px;line-height:1.12;font-weight:760;letter-spacing:-.035em;}
.shell-nav a,.nav a{display:flex!important;align-items:center!important;justify-content:flex-start!important;gap:10px!important;min-height:40px!important;padding:11px 14px!important;border-radius:15px!important;background:transparent!important;border:0!important;color:var(--ops-muted)!important;text-decoration:none!important;font-size:14px!important;line-height:18px!important;font-weight:620!important;letter-spacing:-.01em!important;white-space:nowrap!important;box-shadow:none!important;transition:background .16s ease,color .16s ease,transform .16s ease;}
.shell-nav a::before,.nav a::before{content:'';width:8px;height:8px;border-radius:999px;background:#cbd5e1;flex:0 0 auto;transition:background .16s ease;}
.shell-nav a:hover,.nav a:hover{color:var(--ops-blue-hover)!important;background:var(--ops-blue-soft)!important;transform:translateX(1px);}
.shell-nav a:hover::before,.nav a:hover::before{background:var(--ops-blue)!important;}
.shell-nav a.is-active,.nav a.is-active{background:var(--ops-blue)!important;color:#fff!important;box-shadow:0 12px 24px rgba(47,107,255,.22)!important;}
.shell-nav a.is-active::before,.nav a.is-active::before{background:#fff!important;}
.page-shell>:not(.shell-nav),body>.page>:not(.nav){grid-column:2!important;min-width:0!important;}
.hero{padding:20px 24px!important;margin:0!important;min-height:var(--ops-hero-min-height)!important;border-radius:24px!important;background:var(--ops-panel)!important;border:1px solid var(--ops-border)!important;box-shadow:var(--ops-shadow-card)!important;color:var(--ops-text)!important;}
.hero h1{margin-bottom:4px!important;}
.hero p,.hero .subtitle,.hero .muted,.hero .hint,.hero .help,.hero small{color:#475569!important;font-size:13px!important;line-height:1.5!important;font-weight:500!important;max-width:920px!important;}
.hero + .card,.hero + .summary-grid,.hero + .grid,.hero + .stats-grid,.hero + .config-workspace,.hero + .mini-tools,.hero + .ga-notice,.hero + .toolbar,.hero + .filter-card,body>.page>.hero + *,.page-shell>.hero + *{margin-top:0!important;}
.page-shell>.card:first-of-type,body>.page>.card:first-of-type{margin-top:0!important;}
.page-shell>.hero:first-of-type,body>.page>.hero:first-of-type{margin-top:0!important;}
.card,.summary-item,.executor-card,.account-card,.binding-card,.advanced-fields,.qr-modal-card,.modal-card,.status-card,.group-card,.mini-note,fieldset{background:var(--ops-panel)!important;border:1px solid var(--ops-border)!important;color:var(--ops-text)!important;border-radius:var(--ops-r-xl)!important;box-shadow:var(--ops-shadow-card)!important;}
.card{padding:20px!important;margin-top:0!important;}
.card + .card,.section-card + .section-card,.group-card + .group-card,.account-card + .account-card,.executor-card + .executor-card,.guild-card + .guild-card{margin-top:16px!important;}
.summary-item,.status-card,.account-status-item{padding:16px!important;border-radius:var(--ops-r-lg)!important;background:linear-gradient(180deg,#fff 0%,#fbfdff 100%)!important;}
.account-card,.binding-card{padding:18px!important;}
.group-card,.mini-note{padding:14px!important;}
.grid,.mini-tools,.editor-grid,.field-grid,.compact-grid,.account-card-grid,.account-status-grid,.group-card-grid,.toolbar-actions,.inline-actions{gap:12px!important;}
h1,h2,h3,p{margin-top:0;}
h1{font-size:28px!important;line-height:1.16!important;letter-spacing:0!important;color:#111827!important;font-weight:780!important;margin-bottom:6px!important;}
h2{font-size:19px!important;line-height:1.28!important;letter-spacing:0!important;color:var(--ops-text)!important;font-weight:740!important;margin-bottom:12px!important;}
h3{font-size:16px!important;line-height:1.32!important;letter-spacing:0!important;color:var(--ops-text)!important;font-weight:720!important;margin-bottom:8px!important;}
.section-title{display:flex!important;align-items:center!important;justify-content:space-between!important;gap:12px!important;margin-bottom:14px!important;}
.section-title h2{margin:0!important;}
.label,label,.k,.muted,.hint,.account-sub,small,.field-hint,.help{color:var(--ops-muted)!important;font-size:13px!important;line-height:1.45!important;}
strong,.value{color:var(--ops-text)!important;font-weight:720!important;}
input,select,textarea{width:100%;min-height:40px!important;padding:10px 12px!important;background:#fff!important;color:var(--ops-text)!important;border:1px solid var(--ops-border-strong)!important;border-radius:var(--ops-r-md)!important;box-shadow:0 1px 0 rgba(17,24,39,.02)!important;font:500 14px/1.45 var(--ops-font)!important;margin:6px 0 12px!important;}
textarea{min-height:88px!important;resize:vertical;}
input::placeholder,textarea::placeholder{color:var(--ops-faint)!important;}
input:focus,select:focus,textarea:focus{border-color:#9bbcff!important;box-shadow:0 0 0 4px rgba(47,107,255,.13)!important;outline:none!important;}
button,.button{min-height:38px!important;padding:9px 14px!important;background:var(--ops-blue)!important;color:#fff!important;border:1px solid var(--ops-blue)!important;border-radius:var(--ops-r-md)!important;font:700 14px/18px var(--ops-font)!important;letter-spacing:0!important;box-shadow:0 8px 18px rgba(47,107,255,.18)!important;cursor:pointer!important;transition:background .16s ease,border-color .16s ease,transform .16s ease,box-shadow .16s ease;}
button:hover,.button:hover{background:var(--ops-blue-hover)!important;border-color:var(--ops-blue-hover)!important;transform:translateY(-1px);}
button.secondary,button.ghost,.button.secondary,.button.ghost{background:#f2f6ff!important;border-color:#d7e5ff!important;color:var(--ops-blue-hover)!important;box-shadow:none!important;}
button.danger{background:var(--ops-red-soft)!important;border-color:#fecdd3!important;color:#be123c!important;box-shadow:none!important;}
button.switch-on{background:var(--ops-green-soft)!important;border-color:#bbf7d0!important;color:#166534!important;box-shadow:none!important;}
button.switch-off{background:var(--ops-red-soft)!important;border-color:#fecdd3!important;color:#be123c!important;box-shadow:none!important;}
button:disabled{opacity:.58!important;cursor:not-allowed!important;transform:none!important;}
table{width:100%!important;background:#fff!important;border-collapse:separate!important;border-spacing:0!important;border-radius:18px!important;overflow:hidden!important;box-shadow:var(--ops-shadow-soft)!important;font-size:13px!important;}
th{background:#f5f8ff!important;color:#526178!important;border-bottom:1px solid var(--ops-border)!important;font-size:12px!important;line-height:1.35!important;font-weight:720!important;text-align:left!important;padding:11px 12px!important;}
td{color:var(--ops-text-2)!important;border-bottom:1px solid var(--ops-border-soft)!important;padding:var(--ops-table-row-padding-y) 12px!important;vertical-align:top!important;}
tr:hover td{background:#f8fbff!important;}
#batchMembersTable th{text-align:left!important;vertical-align:middle!important;}
#batchMembersTable td{vertical-align:middle!important;}
#batchMembersTable .select-col{padding-left:0!important;padding-right:0!important;text-align:center!important;vertical-align:middle!important;}
#batchMembersTable .select-col input{width:18px!important;height:18px!important;min-height:18px!important;margin:0 auto!important;padding:0!important;display:block!important;}
.badge,.pill,.binding-badge,.card-monitor-toggle{display:inline-flex;align-items:center;gap:6px;min-height:24px;padding:4px 10px!important;border-radius:999px!important;border:1px solid var(--ops-border)!important;background:#f6f8fc!important;color:var(--ops-text-2)!important;font-size:12px!important;font-weight:720!important;line-height:1.2!important;box-shadow:none!important;}
.badge.operator,.badge.customer_service,.badge.admin,.badge.super_admin{background:var(--ops-blue-soft)!important;color:var(--ops-blue-hover)!important;border-color:#d7e5ff!important;}
.badge.off,.pill.red{background:var(--ops-red-soft)!important;color:#be123c!important;border-color:#fecdd3!important;}
.badge.pending,.pill.orange,.pill.yellow,.pill.amber{background:var(--ops-amber-soft)!important;color:#c2410c!important;border-color:#fed7aa!important;}
.status-line.success,.pill.green,.badge.green{color:#166534!important;background:var(--ops-green-soft)!important;border-color:#bbf7d0!important;}
.status-line.error{color:#be123c!important;}
.toolbar{display:flex!important;align-items:center!important;justify-content:space-between!important;gap:12px!important;margin-bottom:12px!important;}
:root{--crm-card-gap:var(--ops-card-gap);--crm-layout-gap:var(--ops-content-left-gap);}
.page-shell,body>.page{gap:var(--crm-card-gap) var(--crm-layout-gap)!important;}
.page-shell>.hero,.page-shell>.card,.page-shell>.section-card,.page-shell>.summary,.page-shell>.summary-grid,.page-shell>.stats-grid,.page-shell>.top-overview-grid,.page-shell>.ga-stats,.page-shell>.ga-prototype-board,body>.page>.hero,body>.page>.card,body>.page>.section-card,body>.page>.summary,body>.page>.summary-grid,body>.page>.stats-grid,body>.page>.top-overview-grid{margin-top:0!important;}
.page-shell>.card + .card,body>.page>.card + .card,.page-shell>.section-card + .section-card,body>.page>.section-card + .section-card{margin-top:0!important;}
.page-shell .top-overview-grid > .card.card,body>.page .top-overview-grid > .card.card{margin-top:0!important;}
.grid > .card,.grid-2 > .card,.grid-4 > .card,.page-grid > .card,.top-overview-grid > .card,.summary-grid > .summary-item,.summary-grid > .summary-card,.stats-grid > .summary-item,.stats-grid > .card,.status-grid > .status-card,.ga-stats > .card,.ga-stats > .ga-stat,.ga-workbench-stats > .ga-stat,.ga-proto-card-grid > .card,.ga-proto-card-grid > .account-card,.ga-proto-card-grid > .group-card,.ga-proto-script-grid > .ga-script-card,.ga-learn-grid > .ga-script-card,.ga-generation-grid > .card,.account-grid > .account-card,.account-card-grid > .account-card,.account-status-grid > .account-card,.group-card-grid > .group-card,.executor-card-grid > .executor-card,.account-card-grid > .account-card,.guild-grid > .guild-card,.executor-grid > .executor-card,.preset-card-grid > .preset-card{margin-top:0!important;}
.summary,.summary-grid,.stats-grid,.top-overview-grid,.account-grid,.guild-grid,.executor-grid,.account-card-grid,.ga-stats,.ga-board-grid,.ga-board-col,.ga-prototype-board{gap:var(--crm-card-gap)!important;}
body[data-ops-shell-page="production-ops"]>.page-shell>.hero,body[data-ops-shell-page="intake-submit"]>.page-shell>.hero,body[data-ops-shell-page="registration-group-approval-batch-members"]>.page-shell>.hero,body[data-ops-shell-page="bind-failed-users"]>.page-shell>.hero,body[data-ops-shell-page="accounts"]>.page-shell>.hero,.page-shell[data-ops-shell-page="production-ops"]>.hero,.page-shell[data-ops-shell-page="intake-submit"]>.hero,.page-shell[data-ops-shell-page="registration-group-approval-batch-members"]>.hero,.page-shell[data-ops-shell-page="bind-failed-users"]>.hero,.page-shell[data-ops-shell-page="accounts"]>.hero{border-bottom-left-radius:0!important;border-bottom-right-radius:0!important;border-bottom-color:transparent!important;box-shadow:none!important;}
body[data-ops-shell-page="production-ops"]>.page-shell>.top-overview-grid,body[data-ops-shell-page="intake-submit"]>.page-shell>.summary,body[data-ops-shell-page="registration-group-approval-batch-members"]>.page-shell>.batch-members-summary,body[data-ops-shell-page="bind-failed-users"]>.page-shell>.summary,body[data-ops-shell-page="accounts"]>.page-shell>.summary,.page-shell[data-ops-shell-page="production-ops"]>.top-overview-grid,.page-shell[data-ops-shell-page="intake-submit"]>.summary,.page-shell[data-ops-shell-page="registration-group-approval-batch-members"]>.batch-members-summary,.page-shell[data-ops-shell-page="bind-failed-users"]>.summary,.page-shell[data-ops-shell-page="accounts"]>.summary{margin-top:calc(-1 * var(--ops-card-gap))!important;padding:0 20px 20px!important;background:var(--ops-panel)!important;border:1px solid var(--ops-border)!important;border-top:0!important;border-radius:0 0 24px 24px!important;box-shadow:var(--ops-shadow-card)!important;}
body[data-ops-shell-page="production-ops"]>.page-shell>.top-overview-grid>.card,body[data-ops-shell-page="intake-submit"]>.page-shell>.summary>.summary-card,body[data-ops-shell-page="registration-group-approval-batch-members"]>.page-shell>.batch-members-summary>.item,body[data-ops-shell-page="accounts"]>.page-shell>.summary>.summary-item,.page-shell[data-ops-shell-page="production-ops"]>.top-overview-grid>.card,.page-shell[data-ops-shell-page="intake-submit"]>.summary>.summary-card,.page-shell[data-ops-shell-page="registration-group-approval-batch-members"]>.batch-members-summary>.item,.page-shell[data-ops-shell-page="accounts"]>.summary>.summary-item{box-shadow:none!important;}
/* The intake summary lives inside its hero, unlike the sibling summary grids above. */
body[data-ops-shell-page="intake-submit"]>.page-shell>.hero.intake-hero,.page-shell[data-ops-shell-page="intake-submit"]>.hero.intake-hero{border-bottom-left-radius:24px!important;border-bottom-right-radius:24px!important;border-bottom-color:var(--ops-border)!important;box-shadow:var(--ops-shadow-card)!important;}
.ga-proto-page .ga-page-head{border-bottom-left-radius:0!important;border-bottom-right-radius:0!important;border-bottom-color:transparent!important;box-shadow:none!important;margin-bottom:0!important;}
.ga-proto-page .ga-page-head ~ .ga-workbench-stats{margin:0 0 16px!important;padding:0 20px 20px!important;background:var(--ops-panel)!important;border:1px solid var(--ops-border)!important;border-top:0!important;border-radius:0 0 24px 24px!important;box-shadow:var(--ops-shadow-card)!important;}
.ga-proto-page .ga-workbench-stats ~ .ga-proto-stack{margin-top:var(--crm-card-gap,16px)!important;}
.ga-proto-page .ga-page-head ~ .ga-workbench-stats>.card{box-shadow:none!important;}
details{border-radius:var(--ops-r-lg)!important;border:1px solid var(--ops-border)!important;background:#fbfdff!important;padding:12px 14px!important;}
summary{cursor:pointer;color:var(--ops-text)!important;font-weight:700!important;}
pre,code{font-family:var(--ops-mono)!important;font-size:12px!important;line-height:1.55!important;}
.qr-modal,.modal{background:rgba(18,31,54,.46)!important;backdrop-filter:blur(8px)!important;}
.ops-modal-card{max-height:calc(100vh - 96px)!important;max-height:calc(100dvh - 96px)!important;overflow:hidden!important;display:grid!important;grid-template-rows:auto minmax(0,1fr) auto!important;padding:0!important;background:#fff!important;border:1px solid var(--ops-border)!important;border-radius:var(--ops-r-xl)!important;box-shadow:0 24px 64px rgba(15,23,42,.24)!important;color:var(--ops-text)!important;}
.ops-modal-head{flex:0 0 auto!important;margin:0!important;padding:18px 20px 14px!important;border-bottom:1px solid var(--ops-border)!important;background:#fff!important;border-radius:var(--ops-r-xl) var(--ops-r-xl) 0 0!important;}
.ops-modal-body{min-height:0!important;overflow-y:auto!important;padding:18px 20px 12px!important;}
.ops-modal-actions{flex:0 0 auto!important;display:flex!important;gap:10px!important;align-items:center!important;justify-content:flex-end!important;flex-wrap:wrap!important;margin:0!important;padding:14px 20px 20px!important;border-top:1px solid var(--ops-border)!important;background:linear-gradient(180deg,rgba(255,255,255,.94),#fff 42%)!important;border-radius:0 0 var(--ops-r-xl) var(--ops-r-xl)!important;box-shadow:0 -12px 28px rgba(15,23,42,.06)!important;}
.toast{border:1px solid var(--ops-border)!important;background:#111827!important;color:#fff!important;box-shadow:var(--ops-shadow)!important;border-radius:var(--ops-r-lg)!important;}
@media (max-width:980px){body{padding:16px!important}.page-shell,body>.page{display:block!important;width:min(100vw - 32px,1480px)!important;max-width:calc(100vw - 32px)!important;padding-bottom:24px!important}.shell-nav,.nav{position:sticky!important;top:0!important;left:auto!important;right:auto!important;width:auto!important;max-width:none!important;height:auto!important;max-height:none!important;min-height:0!important;grid-column:auto!important;grid-row:auto!important;flex-direction:row!important;align-items:center!important;overflow-x:auto!important;overflow-y:hidden!important;border-radius:0 0 18px 18px!important;margin:-16px -16px 18px!important;padding:10px 14px!important;box-shadow:0 10px 24px rgba(38,55,91,.08)!important}.shell-nav::before,.nav::before{display:none!important}.shell-nav a,.nav a{min-height:34px!important;padding:8px 12px!important;border-radius:999px!important;white-space:nowrap!important;flex:0 0 auto!important}.page-shell>:not(.shell-nav),body>.page>:not(.nav){grid-column:auto!important;width:100%!important;max-width:100%!important;min-width:0!important}.card{padding:16px!important}.hero{padding:18px!important;border-radius:18px!important}.grid,.editor-grid,.mini-tools,.compact-grid,.account-card-grid,.account-status-grid{grid-template-columns:1fr!important}}

</style>
<script data-ops-shell-active="true">
(function(){
function cleanOpsPath(path){path=(path||location.pathname||'/ops').split('?')[0].split('#')[0].replace(/\/$/,'')||'/ops';return path;}
function canonicalOpsPath(path){return cleanOpsPath(path);}
function markActive(){try{var path=canonicalOpsPath(location.pathname);var matched=false;document.querySelectorAll('.shell-nav a,.nav a').forEach(function(a){a.classList.remove('is-active');var href=canonicalOpsPath(a.getAttribute('href')||'');if(href===path){a.classList.add('is-active');matched=true;}});document.documentElement.setAttribute('data-current-ops-path',path);document.documentElement.setAttribute('data-ops-nav-matched',matched?'true':'false');}catch(e){}}
function bindOpsNavFallback(){try{if(window.__opsNavFallbackBound)return;window.__opsNavFallbackBound=true;document.addEventListener('click',function(event){var anchor=event.target&&event.target.closest?event.target.closest('.shell-nav a,.nav a'):null;if(!anchor)return;if(event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;var href=anchor.getAttribute('href')||'';if(!href||href.charAt(0)!=='/')return;event.preventDefault();window.location.assign(href);},true);}catch(e){}}
if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',markActive);}else{markActive();}
if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',bindOpsNavFallback);}else{bindOpsNavFallback();}
})();
</script>
<script>window.__opsClientVersion = "__OPS_CLIENT_VERSION__";</script>
<script src="/static/ops/common.js"></script>

'''
        shell_style = shell_style.replace(
            '__OPS_CLIENT_VERSION__',
            re.sub(r'[^a-zA-Z0-9_.-]', '', OPS_CLIENT_VERSION),
        )
        shell_style = shell_style.replace(
            '/static/ops/common.js',
            f"/static/ops/common.js?v={re.sub(r'[^a-zA-Z0-9_.-]', '', OPS_CLIENT_VERSION)}",
        )
        if '</head>' in html:
            return html.replace('</head>', shell_style + '</head>', 1)
        return shell_style + html

    def _ops_page_html(role: str) -> str:
        html = OPS_PAGE_HTML
        normalized_role = str(role or '').strip()
        if normalized_role in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN}:
            return html
        html = re.sub(
            r'\s*<a[^>]*href="/ops/intake-bot-presets"[^>]*>.*?</a>',
            '',
            html,
            flags=re.S,
        )
        return re.sub(
            r'\s*<a[^>]*href="/ops/group-atmosphere"[^>]*data-admin-only-nav="true"[^>]*>.*?</a>',
            '',
            html,
            flags=re.S,
        )

    def _group_atmosphere_page_html(role: str) -> str:
        html = GROUP_ATMOSPHERE_PAGE_HTML.replace('__GROUP_ATMOSPHERE_PAGE_VERSION__', GROUP_ATMOSPHERE_PAGE_VERSION)
        normalized_role = str(role or '').strip()
        if not ops_role_is_business(normalized_role):
            return html
        operator_nav = _ops_nav_html(normalized_role)
        if '<div class="shell-nav">' in html:
            return re.sub(
                r'<div class="shell-nav">.*?</div>',
                operator_nav,
                html,
                count=1,
                flags=re.S,
            )
        if '<div class="page-shell">' in html:
            return html.replace('<div class="page-shell">', f'<div class="page-shell">{operator_nav}', 1)
        return operator_nav + html

    def _render_ops_home_page(request: Request) -> Response:
        user = _require_ops_user(request)
        current_role = str(user.get('role') or '').strip()
        if ops_role_is_business(current_role):
            return RedirectResponse(url='/ops/intake-submit', status_code=303)
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        return HTMLResponse(
            _with_ops_shell_style(_ops_page_html(current_role), current_role, page='dashboard'),
            headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma': 'no-cache',
            },
        )

    @app.get('/ops', response_class=HTMLResponse)
    def ops_page(request: Request) -> str:
        return _render_ops_home_page(request)

    @app.get('/ops/', response_class=HTMLResponse)
    def ops_page_slash(request: Request) -> str:
        return _render_ops_home_page(request)

    @app.get('/ops/task-control', response_class=HTMLResponse)
    def ops_task_control_page(request: Request) -> HTMLResponse:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        return HTMLResponse(
            _with_ops_shell_style(
                TASK_CONTROL_PAGE_HTML.replace(
                    '<div class="page-shell">',
                    '<div class="page-shell"><div class="shell-nav"></div>',
                    1,
                ),
                str(user.get('role') or '').strip(),
                page='task-control',
            ),
            headers={'Cache-Control': 'no-store'},
        )

    @app.get('/api/ops/task-control/tasks')
    def ops_task_control_tasks(
        request: Request,
        status: str = '',
        source: str = '',
        q: str = '',
        blocked_only: bool = False,
        view: str = '',
        limit: int = 200,
        offset: int = 0,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            return list_unified_tasks(
                db_path=task_control_db_path,
                normalized_status=status,
                source_system=source,
                query=q,
                blocked_only=blocked_only,
                view=view,
                limit=limit,
                offset=offset,
            )
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail=f'task_control_unavailable:{str(exc)[:160]}') from exc

    @app.get('/api/ops/task-control/tasks/{task_key}')
    def ops_task_control_task(request: Request, task_key: str) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            return get_unified_task(task_key, db_path=task_control_db_path)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/ops/task-control/tasks/{task_key}/actions')
    async def ops_task_control_action(request: Request, task_key: str) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        payload = await request.json()
        try:
            return manage_unified_task(
                task_key,
                action=str(payload.get('action') or ''),
                expected_version=int(payload.get('expected_version')),
                reason=str(payload.get('reason') or ''),
                actor=str(user.get('username') or user.get('name') or user.get('user_id') or ''),
                db_path=task_control_db_path,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get('/ops/ad-data-dashboard', response_class=HTMLResponse)
    def ops_ad_data_dashboard_page(request: Request) -> HTMLResponse:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        return HTMLResponse(
            _with_ops_shell_style(
                AD_DATA_DASHBOARD_PAGE_HTML,
                str(user.get('role') or '').strip(),
                page='ad-data-dashboard',
            ),
            headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma': 'no-cache',
                'Expires': '0',
            },
        )

    def _ad_dashboard_query_context(request: Request, *, days: int = 30, date_from: Optional[str] = None, date_to: Optional[str] = None, top_limit: int = 8) -> Dict[str, Any]:
        def _query_values(*names: str) -> List[str]:
            values: List[str] = []
            for name in names:
                values.extend(request.query_params.getlist(name))
            return _normalize_ad_filter_values(values)

        filters = {
            'data_source': _query_values('data_source', 'source'),
            # account_id is a report-scope numeric Meta identifier, not the
            # dashboard app/account label dimension.
            'app_id': _query_values('app_id', 'account'),
            'media_source': _query_values('media_source', 'channel'),
        }
        target_app = _normalize_ad_dashboard_target_app(
            request.query_params.get('target_app')
            or request.query_params.get('business_app')
            or request.query_params.get('app')
            or 'all'
        ) or 'all'
        platform_filters: Dict[str, Dict[str, List[str]]] = {}
        for platform, prefix in AD_DASHBOARD_PLATFORM_PARAM_PREFIXES.items():
            platform_filters[platform] = {
                'target_app': _query_values(f'{prefix}_target_app', f'{platform}_target_app'),
                'app_id': _query_values(f'{prefix}_app_id', f'{prefix}_account', f'{platform}_app_id', f'{platform}_account'),
                'country': _query_values(f'{prefix}_country', f'{platform}_country'),
                'campaign': _query_values(f'{prefix}_campaign', f'{platform}_campaign'),
                'ad_group': _query_values(f'{prefix}_ad_group', f'{platform}_ad_group', f'{prefix}_adset', f'{platform}_adset'),
                'ad': _query_values(f'{prefix}_ad', f'{platform}_ad'),
            }
        platform_date_windows = {
            platform: {
                'date_from': request.query_params.get(f'{prefix}_date_from') or request.query_params.get(f'{prefix}_from') or '',
                'date_to': request.query_params.get(f'{prefix}_date_to') or request.query_params.get(f'{prefix}_to') or '',
            }
            for platform, prefix in AD_DASHBOARD_PLATFORM_PARAM_PREFIXES.items()
        }
        return {
            'days': days,
            'date_from': date_from or request.query_params.get('from'),
            'date_to': date_to or request.query_params.get('to'),
            'top_limit': top_limit,
            'target_app': target_app,
            'filters': filters,
            'platform_filters': platform_filters,
            'platform_date_windows': platform_date_windows,
        }

    def _ad_dashboard_summary_cache_key(context: Dict[str, Any]) -> str:
        key_payload = {
            'schema': 'ad_dashboard_summary_v9_app_window',
            'days': int(context.get('days') or 30),
            'date_from': str(context.get('date_from') or ''),
            'date_to': str(context.get('date_to') or ''),
            'top_limit': int(context.get('top_limit') or 8),
            'target_app': str(context.get('target_app') or 'all'),
            'filters': context.get('filters') or {},
            'platform_filters': context.get('platform_filters') or {},
            'app_ids': appsflyer_app_ids,
            'meta_accounts': meta_ads_account_ids,
            'af_base': appsflyer_base_url,
            'dashboard_timezone': ad_dashboard_timezone,
            'meta_base': meta_ads_base_url,
            'meta_version': meta_ads_api_version,
            'bind_success_configured': bool(bind_success_api_token),
            'bind_success_base': bind_success_base_url,
            'bind_success_project': bind_success_project,
        }
        platform_date_windows = {
            platform: {
                'date_from': str((window or {}).get('date_from') or ''),
                'date_to': str((window or {}).get('date_to') or ''),
            }
            for platform, window in (context.get('platform_date_windows') or {}).items()
        }
        non_empty_platform_windows = {
            platform: window
            for platform, window in platform_date_windows.items()
            if window.get('date_from') or window.get('date_to')
        }
        global_window = {
            'date_from': str(context.get('date_from') or ''),
            'date_to': str(context.get('date_to') or ''),
        }
        all_platforms_match_global = (
            non_empty_platform_windows
            and set(non_empty_platform_windows.keys()) == set(AD_DASHBOARD_PLATFORM_NAMES)
            and all(window == global_window for window in non_empty_platform_windows.values())
        )
        if non_empty_platform_windows and not all_platforms_match_global:
            key_payload['platform_date_windows'] = non_empty_platform_windows
        return json.dumps(
            key_payload,
            ensure_ascii=False,
            sort_keys=True,
        )

    def _build_ad_dashboard_snapshot_for_request(request: Request, *, days: int = 30, date_from: Optional[str] = None, date_to: Optional[str] = None, top_limit: int = 8) -> Dict[str, Any]:
        context = _ad_dashboard_query_context(
            request,
            days=days,
            date_from=date_from,
            date_to=date_to,
            top_limit=top_limit,
        )
        return build_ad_data_dashboard_snapshot(
            token=appsflyer_api_token,
            app_ids=appsflyer_app_ids,
            timezone_name=appsflyer_timezone,
            base_url=appsflyer_base_url,
            session=appsflyer_session,
            meta_token=meta_ads_access_token,
            meta_ad_account_ids=meta_ads_account_ids,
            meta_api_version=meta_ads_api_version,
            meta_base_url=meta_ads_base_url,
            meta_session=meta_ads_session,
            bind_success_token=bind_success_api_token,
            bind_success_base_url=bind_success_base_url,
            bind_success_project=bind_success_project,
            bind_success_session=bind_success_session,
            days=int(context['days'] or 30),
            date_from=context.get('date_from'),
            date_to=context.get('date_to'),
            top_limit=int(context['top_limit'] or 8),
            target_app=str(context.get('target_app') or 'all'),
            filters=context['filters'],
            platform_filters=context['platform_filters'],
            platform_date_windows=context.get('platform_date_windows') or {},
        )

    def _ensure_ad_dashboard_snapshot_cache_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ad_dashboard_snapshot_cache (
                cache_key TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                created_at_utc TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )

    def _read_persistent_ad_dashboard_cache(
        cache_key: str,
        *,
        now_ts: float,
        cache_window_start: float,
        cache_next_refresh: float,
        cache_max_age_seconds: int,
        cache_timezone: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            with db.connect() as conn:
                _ensure_ad_dashboard_snapshot_cache_table(conn)
                row = conn.execute(
                    "SELECT created_at, payload_json FROM ad_dashboard_snapshot_cache WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
        except Exception:
            return None
        if not row:
            return None
        try:
            cached_at = float(row['created_at'] or 0.0)
        except Exception:
            cached_at = 0.0
        cache_fresh = (
            cached_at > 0
            and (
                now_ts - cached_at < ad_dashboard_cache_ttl_seconds
                if ad_dashboard_cache_ttl_seconds > 0
                else cached_at >= cache_window_start
            )
        )
        if not cache_fresh:
            return None
        try:
            payload = json.loads(row['payload_json'])
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        payload = copy.deepcopy(payload)
        payload['cache'] = {
            'hit': True,
            'layer': 'persistent',
            'ttl_seconds': cache_max_age_seconds,
            'cached_at': datetime.fromtimestamp(cached_at, timezone.utc).isoformat(),
            'next_refresh_at': datetime.fromtimestamp(cache_next_refresh, ZoneInfo(cache_timezone)).isoformat(),
            'schedule': f'{cache_timezone} 09:20 daily',
        }
        return payload

    def _read_any_persistent_ad_dashboard_cache(cache_key: str, *, now_ts: float, cache_timezone: str) -> Optional[Dict[str, Any]]:
        try:
            with db.connect() as conn:
                _ensure_ad_dashboard_snapshot_cache_table(conn)
                row = conn.execute(
                    "SELECT created_at, payload_json FROM ad_dashboard_snapshot_cache WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
        except Exception:
            return None
        if not row:
            return None
        try:
            cached_at = float(row['created_at'] or 0.0)
            payload = json.loads(row['payload_json'])
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        payload = copy.deepcopy(payload)
        payload['cache'] = {
            'hit': True,
            'layer': 'stale_persistent',
            'stale': True,
            'cached_at': datetime.fromtimestamp(cached_at, timezone.utc).isoformat(),
            'age_seconds': max(0, int(now_ts - cached_at)) if cached_at else None,
            'schedule': f'{cache_timezone} 09:20 daily',
        }
        return payload

    def _write_persistent_ad_dashboard_cache(cache_key: str, payload: Dict[str, Any], *, created_at: float) -> None:
        try:
            with db.connect() as conn:
                _ensure_ad_dashboard_snapshot_cache_table(conn)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ad_dashboard_snapshot_cache
                    (cache_key, created_at, created_at_utc, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        cache_key,
                        created_at,
                        datetime.fromtimestamp(created_at, timezone.utc).isoformat(),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                conn.commit()
        except Exception:
            return

    def _real_conversion_provider_for_request(data_mode: str) -> Any:
        normalized_mode = str(data_mode or 'fixture').strip().lower()
        provider_kind = real_bind_provider_kind
        if normalized_mode == 'real':
            provider_kind = 'tugao'
        if provider_kind == 'fixture':
            return FixtureRealConversionProvider(
                random_count=int(cfg.get('AD_FIXTURE_RANDOM_BIND_OBJECTS') or os.getenv('AD_FIXTURE_RANDOM_BIND_OBJECTS') or 0),
            )
        if provider_kind == 'tugao':
            return TugaoRealConversionProvider(db_path=cfg["DB_PATH"])
        raise HTTPException(status_code=400, detail='unsupported_real_bind_provider')

    meta_creative_auto_sync_lock = threading.Lock()

    def _ad_creative_preview_cache_dir() -> Path:
        db_path = str(cfg.get("DB_PATH") or '').strip()
        base_dir = Path(__file__).resolve().parents[1] / 'data'
        if db_path and db_path != ':memory:':
            try:
                base_dir = Path(db_path).expanduser().resolve().parent
            except Exception:
                pass
        path = base_dir / 'ad_creative_previews'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _ad_creative_source_cache_dir() -> Path:
        db_path = str(cfg.get("DB_PATH") or '').strip()
        base_dir = Path(__file__).resolve().parents[1] / 'data'
        if db_path and db_path != ':memory:':
            try:
                base_dir = Path(db_path).expanduser().resolve().parent
            except Exception:
                pass
        path = base_dir / 'ad_creative_sources'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _ad_creative_preview_route(asset_id: str) -> str:
        return f"/api/ops/ad-data-dashboard/creative-assets/{quote(str(asset_id or ''))}/preview"

    def _ad_creative_source_route(asset_id: str) -> str:
        return f"/api/ops/ad-data-dashboard/creative-assets/{quote(str(asset_id or ''))}/source"

    def _ad_creative_preview_filename(asset_id: str, content_type: str) -> str:
        safe_id = re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(asset_id or '').strip())[:120] or create_id('aci_preview')
        ext = {
            'image/jpeg': '.jpg',
            'image/jpg': '.jpg',
            'image/png': '.png',
            'image/webp': '.webp',
            'image/gif': '.gif',
            'image/svg+xml': '.svg',
        }.get(str(content_type or '').split(';', 1)[0].strip().lower(), '.img')
        return f'{safe_id}{ext}'

    def _ad_creative_preview_source_allowed(raw_url: str) -> bool:
        parsed = urlparse(str(raw_url or '').strip())
        host = str(parsed.hostname or '').strip().lower()
        allowed_suffixes = ('facebook.com', 'fbcdn.net', 'fbsbx.com')
        return parsed.scheme == 'https' and bool(host) and any(host == suffix or host.endswith(f'.{suffix}') for suffix in allowed_suffixes)

    def _image_dimensions_from_bytes(content: bytes) -> Tuple[int, int]:
        try:
            from PIL import Image
            import io
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
            return int(width or 0), int(height or 0)
        except Exception:
            return 0, 0

    def _localize_creative_asset_sources(assets: Iterable[Any]) -> List[Any]:
        localized: List[Any] = []
        cache_dir = _ad_creative_source_cache_dir()
        for asset in assets or []:
            try:
                existing_ref = str(getattr(asset, 'source_image_local_ref', '') or '')
                if existing_ref.startswith('/api/ops/ad-data-dashboard/creative-assets/'):
                    localized.append(asset)
                    continue
                source_url = str(getattr(asset, 'source_image_url', '') or '').strip()
                if not source_url or not _ad_creative_preview_source_allowed(source_url):
                    localized.append(asset)
                    continue
                safe_id = re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(getattr(asset, 'asset_id', '') or '').strip())[:120]
                existing = next(cache_dir.glob(f"{safe_id}.*"), None)
                if existing and existing.is_file():
                    content = existing.read_bytes()
                    width, height = _image_dimensions_from_bytes(content)
                    source_quality = 'high_res' if max(
                        width,
                        height,
                        int(getattr(asset, 'source_image_width', 0) or 0),
                        int(getattr(asset, 'source_image_height', 0) or 0),
                    ) >= 600 else 'thumbnail'
                    localized.append(replace(
                        asset,
                        source_image_local_ref=_ad_creative_source_route(getattr(asset, 'asset_id', '')),
                        source_image_hash=str(getattr(asset, 'source_image_hash', '') or '').strip() or hashlib.sha256(content).hexdigest(),
                        source_image_width=width or int(getattr(asset, 'source_image_width', 0) or 0),
                        source_image_height=height or int(getattr(asset, 'source_image_height', 0) or 0),
                        source_image_quality=str(getattr(asset, 'source_image_quality', '') or '').strip() or source_quality,
                    ))
                    continue
                upstream = requests.get(
                    source_url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36',
                        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                    },
                    timeout=20.0,
                )
                if upstream.status_code >= 400:
                    localized.append(asset)
                    continue
                content = upstream.content or b''
                content_type = str(upstream.headers.get('content-type') or '').split(';', 1)[0].strip().lower()
                if not content or len(content) > 12 * 1024 * 1024 or not content_type.startswith('image/'):
                    localized.append(asset)
                    continue
                width, height = _image_dimensions_from_bytes(content)
                source_quality = 'high_res' if max(width, height, int(getattr(asset, 'source_image_width', 0) or 0), int(getattr(asset, 'source_image_height', 0) or 0)) >= 600 else 'thumbnail'
                target = cache_dir / _ad_creative_preview_filename(str(getattr(asset, 'asset_id', '') or ''), content_type)
                tmp = target.with_suffix(target.suffix + '.tmp')
                tmp.write_bytes(content)
                tmp.replace(target)
                localized.append(replace(
                    asset,
                    source_image_local_ref=_ad_creative_source_route(getattr(asset, 'asset_id', '')),
                    source_image_hash=hashlib.sha256(content).hexdigest(),
                    source_image_width=width or int(getattr(asset, 'source_image_width', 0) or 0),
                    source_image_height=height or int(getattr(asset, 'source_image_height', 0) or 0),
                    source_image_quality=source_quality,
                ))
            except Exception:
                localized.append(asset)
        return localized

    def _localize_creative_asset_previews(assets: Iterable[Any]) -> List[Any]:
        localized: List[Any] = []
        cache_dir = _ad_creative_preview_cache_dir()
        for asset in assets or []:
            try:
                if str(getattr(asset, 'local_media_ref', '') or '').startswith('/api/ops/ad-data-dashboard/creative-assets/'):
                    localized.append(asset)
                    continue
                source_url = str(getattr(asset, 'thumbnail_url', '') or '').strip()
                if not source_url or not _ad_creative_preview_source_allowed(source_url):
                    localized.append(asset)
                    continue
                existing = next(cache_dir.glob(f"{re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(getattr(asset, 'asset_id', '') or '').strip())[:120]}.*"), None)
                if existing and existing.is_file():
                    localized.append(replace(asset, local_media_ref=_ad_creative_preview_route(getattr(asset, 'asset_id', ''))))
                    continue
                upstream = requests.get(
                    source_url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36',
                        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                    },
                    timeout=12.0,
                )
                if upstream.status_code >= 400:
                    localized.append(asset)
                    continue
                content = upstream.content or b''
                content_type = str(upstream.headers.get('content-type') or '').split(';', 1)[0].strip().lower()
                if not content or len(content) > 5 * 1024 * 1024 or not content_type.startswith('image/'):
                    localized.append(asset)
                    continue
                target = cache_dir / _ad_creative_preview_filename(str(getattr(asset, 'asset_id', '') or ''), content_type)
                tmp = target.with_suffix(target.suffix + '.tmp')
                tmp.write_bytes(content)
                tmp.replace(target)
                localized.append(replace(asset, local_media_ref=_ad_creative_preview_route(getattr(asset, 'asset_id', ''))))
            except Exception:
                localized.append(asset)
        return localized

    def _repair_preview_asset_local_ref(conn: sqlite3.Connection, asset: Dict[str, Any]) -> Dict[str, Any]:
        if str(asset.get('local_media_ref') or '').startswith('/api/ops/ad-data-dashboard/creative-assets/'):
            return asset
        asset_id = str(asset.get('asset_id') or '').strip()
        account_id = str(asset.get('account_id') or '').strip()
        image_hash = str(asset.get('image_hash') or '').strip()
        thumbnail_url = str(asset.get('thumbnail_url') or '').strip()
        source_url = thumbnail_url
        if image_hash and meta_ads_access_token and account_id and (
            not source_url or 'external-' in source_url or '/emg1/' in source_url
        ):
            try:
                response = meta_ads_session.get(
                    f'{meta_ads_base_url.rstrip("/")}/{meta_ads_api_version}/act_{account_id}/adimages',
                    params={
                        'fields': 'hash,url,url_128',
                        'hashes': json.dumps([image_hash]),
                        'access_token': meta_ads_access_token,
                    },
                    timeout=20,
                ) if meta_ads_session is not None else None
                if response is not None and getattr(response, 'status_code', 200) < 400:
                    body = response.json()
                    for row in (body.get('data') or []) if isinstance(body, dict) else []:
                        if str(row.get('hash') or '').strip() != image_hash:
                            continue
                        source_url = str(row.get('url_128') or row.get('url') or source_url or '').strip()
                        break
            except Exception:
                pass
        if not asset_id or not source_url or not _ad_creative_preview_source_allowed(source_url):
            return asset
        try:
            cache_dir = _ad_creative_preview_cache_dir()
            safe_id = re.sub(r'[^a-zA-Z0-9_.-]+', '_', asset_id)[:120]
            existing = next(cache_dir.glob(f'{safe_id}.*'), None)
            if not existing or not existing.is_file():
                upstream = requests.get(
                    source_url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36',
                        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                    },
                    timeout=12.0,
                )
                if upstream.status_code >= 400:
                    return asset
                content = upstream.content or b''
                content_type = str(upstream.headers.get('content-type') or '').split(';', 1)[0].strip().lower()
                if not content or len(content) > 5 * 1024 * 1024 or not content_type.startswith('image/'):
                    return asset
                target = cache_dir / _ad_creative_preview_filename(asset_id, content_type)
                tmp = target.with_suffix(target.suffix + '.tmp')
                tmp.write_bytes(content)
                tmp.replace(target)
            local_ref = _ad_creative_preview_route(asset_id)
            conn.execute(
                """
                UPDATE ad_creative_asset
                SET thumbnail_url = ?, local_media_ref = ?, updated_at = ?
                WHERE asset_id = ?
                """,
                (source_url, local_ref, datetime.now(timezone.utc).isoformat(), asset_id),
            )
            conn.commit()
            repaired = dict(asset)
            repaired['thumbnail_url'] = source_url
            repaired['local_media_ref'] = local_ref
            repaired['preview_url'] = local_ref
            return repaired
        except Exception:
            return asset

    def _fetch_preview_asset_from_meta_ad_name(conn: sqlite3.Connection, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        ad_name = str(row.get('ad') or row.get('object_name') or '').strip()
        if not ad_name or not meta_ads_access_token or not meta_ads_account_ids or meta_ads_session is None:
            return None
        fields = 'id,name,account_id,campaign_id,adset_id,creative{id,name,title,body,thumbnail_url,image_url,image_hash,video_id}'
        for account_id in meta_ads_account_ids:
            try:
                response = meta_ads_session.get(
                    f'{meta_ads_base_url.rstrip("/")}/{meta_ads_api_version}/act_{account_id}/ads',
                    params={
                        'fields': fields,
                        'limit': 5,
                        'filtering': json.dumps([{'field': 'name', 'operator': 'CONTAIN', 'value': ad_name}]),
                        'access_token': meta_ads_access_token,
                    },
                    timeout=20,
                )
                if getattr(response, 'status_code', 200) >= 400:
                    continue
                body = response.json()
                for item in (body.get('data') or []) if isinstance(body, dict) else []:
                    if str(item.get('name') or '').strip() != ad_name:
                        continue
                    creative = item.get('creative') or {}
                    if not isinstance(creative, dict):
                        creative = {}
                    asset = creative_asset_from_meta_payload({
                        'platform': 'meta',
                        'account_id': item.get('account_id') or account_id,
                        'campaign_id': item.get('campaign_id'),
                        'adset_id': item.get('adset_id'),
                        'ad_id': item.get('id'),
                        'ad_name': item.get('name'),
                        'creative_id': creative.get('id'),
                        'body_text': creative.get('body'),
                        'title_text': item.get('name') or creative.get('title') or creative.get('name'),
                        'thumbnail_url': creative.get('thumbnail_url') or creative.get('image_url'),
                        'image_url': creative.get('image_url'),
                        'image_hash': creative.get('image_hash'),
                        'video_id': creative.get('video_id'),
                    })
                    localized = _localize_creative_asset_previews([asset])
                    persisted = persist_creative_assets(conn, localized)
                    if not persisted:
                        continue
                    loaded = _load_daily_payload_preview_assets(conn, {'recommendations': [row], 'ad_objects': [row]})
                    for loaded_asset in loaded:
                        if str(loaded_asset.get('ad_id') or '') == str(item.get('id') or '') or str(loaded_asset.get('title_text') or '') == ad_name:
                            return loaded_asset
            except Exception:
                continue
        return None

    def _meta_creative_assets_need_refresh(conn: sqlite3.Connection, *, now_ts: Optional[float] = None) -> bool:
        now_ts = float(now_ts or time.time())
        try:
            ensure_creative_intelligence_tables(conn)
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN COALESCE(thumbnail_url, '') <> '' OR COALESCE(local_media_ref, '') <> '' THEN 1 ELSE 0 END) AS with_preview,
                       SUM(CASE WHEN COALESCE(copy_fragments_json, '') NOT IN ('', '[]') THEN 1 ELSE 0 END) AS with_copy_fragments,
                       MAX(updated_at) AS latest_updated_at
                FROM ad_creative_asset
                """
            ).fetchone()
        except Exception:
            return True
        total = int((row or {}).get('total') or 0) if isinstance(row, dict) else int(row['total'] or 0)
        with_preview = int((row or {}).get('with_preview') or 0) if isinstance(row, dict) else int(row['with_preview'] or 0)
        with_copy_fragments = int((row or {}).get('with_copy_fragments') or 0) if isinstance(row, dict) else int(row['with_copy_fragments'] or 0)
        latest = str(((row or {}).get('latest_updated_at') if isinstance(row, dict) else row['latest_updated_at']) or '').strip()
        if total <= 0 or with_preview <= 0:
            return True
        try:
            latest_ts = datetime.fromisoformat(latest).timestamp()
        except Exception:
            latest_ts = 0.0
        return latest_ts <= 0 or now_ts - latest_ts > 6 * 3600

    def _sync_meta_creative_assets_if_stale(conn: sqlite3.Connection) -> Dict[str, Any]:
        if not ad_creative_flags.get('AD_CREATIVE_SYNC_ENABLED', False):
            return {'synced': False, 'reason': 'creative_sync_disabled'}
        if not meta_ads_access_token or not meta_ads_account_ids or meta_ads_session is None:
            return {'synced': False, 'reason': 'meta_creative_sync_not_configured'}
        now_ts = time.time()
        if not _meta_creative_assets_need_refresh(conn, now_ts=now_ts):
            return {'synced': False, 'reason': 'fresh'}
        if not meta_creative_auto_sync_lock.acquire(blocking=False):
            return {'synced': False, 'reason': 'sync_in_progress'}
        try:
            if not _meta_creative_assets_need_refresh(conn, now_ts=now_ts):
                return {'synced': False, 'reason': 'fresh'}
            service = MetaCreativeSyncService(
                token=meta_ads_access_token,
                account_ids=meta_ads_account_ids,
                api_version=meta_ads_api_version,
                base_url=meta_ads_base_url,
                session=meta_ads_session,
                page_size=meta_creative_sync_page_size,
                enabled=True,
            )
            result = service.sync()
            persisted = persist_creative_assets(conn, _localize_creative_asset_previews(result.get('assets') or []))
            return {
                'synced': bool(result.get('ok')),
                'persisted': persisted,
                'synced_count': int(result.get('synced_count') or 0),
                'errors': len(result.get('errors') or []),
            }
        except Exception as exc:
            return {'synced': False, 'reason': exc.__class__.__name__}
        finally:
            meta_creative_auto_sync_lock.release()

    def _creative_generation_is_old_image_request(body: Dict[str, Any]) -> bool:
        task = body.get('production_task') if isinstance(body.get('production_task'), dict) else {}
        mode = str(body.get('experiment_mode') or task.get('mode') or task.get('experiment_mode') or '').strip()
        return mode == 'replacement'

    def _sync_meta_source_asset_for_creative_generation(conn: sqlite3.Connection, body: Dict[str, Any]) -> Dict[str, Any]:
        if not _creative_generation_is_old_image_request(body):
            return {'synced': False, 'reason': 'not_old_image_request'}
        task = body.get('production_task') if isinstance(body.get('production_task'), dict) else {}
        source_url_for_body = str(body.get('source_image_signed_url') or body.get('source_image_url') or task.get('source_image_signed_url') or task.get('source_image_url') or '').strip()
        source_looks_like_preview = (
            '/api/ops/ad-data-dashboard/creative-assets/' in source_url_for_body
            and source_url_for_body.rstrip('/').endswith('/preview')
        )
        has_resolved_source = (
            not source_looks_like_preview
            and all(str(body.get(key) or task.get(key) or '').strip() for key in ('source_image_signed_url', 'source_image_hash', 'source_image_id'))
        )
        if has_resolved_source:
            return {'synced': False, 'reason': 'source_image_already_resolved'}
        source_ad_id = str(
            body.get('source_ad_id')
            or task.get('source_ad_id')
            or task.get('ad_id')
            or (task.get('object_id') if str(task.get('object_level') or '') == 'ad' else '')
            or ''
        ).strip()
        source_account_id = str(body.get('account_id') or task.get('account_id') or '').strip()
        source_asset_id = str(
            body.get('source_image_id')
            or task.get('source_image_id')
            or body.get('source_preview_asset_id')
            or task.get('source_preview_asset_id')
            or ''
        ).strip()
        if source_asset_id and (not source_ad_id or not source_ad_id.isdigit()):
            try:
                asset_row = conn.execute(
                    """
                    SELECT account_id, ad_id
                    FROM ad_creative_asset
                    WHERE asset_id = ? OR ad_id = ? OR creative_id = ?
                    ORDER BY last_seen_at DESC, updated_at DESC
                    LIMIT 1
                    """,
                    (source_asset_id, source_asset_id, source_asset_id),
                ).fetchone()
            except Exception:
                asset_row = None
            if asset_row:
                source_ad_id = str(asset_row['ad_id'] or source_ad_id or '').strip()
                source_account_id = str(source_account_id or asset_row['account_id'] or '').strip()
        if not source_ad_id:
            return {'synced': False, 'reason': 'source_ad_id_missing'}
        if not meta_ads_access_token or not meta_ads_session:
            return {'synced': False, 'reason': 'meta_creative_sync_not_configured'}
        if not meta_creative_auto_sync_lock.acquire(blocking=False):
            return {'synced': False, 'reason': 'source_sync_in_progress'}
        try:
            service = MetaCreativeSyncService(
                token=meta_ads_access_token,
                account_ids=meta_ads_account_ids,
                api_version=meta_ads_api_version,
                base_url=meta_ads_base_url,
                session=meta_ads_session,
                page_size=1,
                enabled=True,
            )
            asset = service.fetch_ad_asset(source_ad_id, account_id=source_account_id)
            if not asset:
                return {'synced': False, 'reason': 'meta_ad_asset_not_found', 'source_ad_id': source_ad_id}
            localized = _localize_creative_asset_previews(_localize_creative_asset_sources([asset]))
            persisted = persist_creative_assets(conn, localized)
            localized_asset = localized[0] if localized else None
            return {
                'synced': bool(persisted),
                'reason': 'on_demand_source_sync',
                'source_ad_id': source_ad_id,
                'source_creative_id': str(getattr(localized_asset, 'creative_id', '') or ''),
                'asset_id': str(getattr(localized_asset, 'asset_id', '') or ''),
                'source_image_id': str(getattr(localized_asset, 'asset_id', '') or ''),
                'source_image_signed_url': str(getattr(localized_asset, 'source_image_local_ref', '') or ''),
                'source_image_hash': str(getattr(localized_asset, 'source_image_hash', '') or ''),
                'source_image_width': int(getattr(localized_asset, 'source_image_width', 0) or 0),
                'source_image_height': int(getattr(localized_asset, 'source_image_height', 0) or 0),
                'source_image_quality': str(getattr(localized_asset, 'source_image_quality', '') or ''),
                'source_image_resolution_status': 'on_demand_source_sync',
                'has_source_image': bool(str(getattr(localized_asset, 'source_image_local_ref', '') or '').strip()),
                'persisted': persisted,
            }
        except Exception as exc:
            return {'synced': False, 'reason': exc.__class__.__name__, 'source_ad_id': source_ad_id}
        finally:
            meta_creative_auto_sync_lock.release()

    def _creative_preview_keys(value: Any) -> List[str]:
        raw = str(value or '').strip().lower()
        if not raw:
            return []
        compact = re.sub(r'[\s\-_—–·•:：|/\\]+', '', raw)
        return list(dict.fromkeys([raw, compact] if compact and compact != raw else [raw]))

    def _load_daily_payload_preview_assets(conn: sqlite3.Connection, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        identity_values: List[str] = []
        title_values: List[str] = []
        for collection_name in ('recommendations', 'ad_objects'):
            for row in payload.get(collection_name) or []:
                if not isinstance(row, dict):
                    continue
                for field in ('object_id', 'ad_id', 'creative_id', 'asset_id'):
                    value = str(row.get(field) or '').strip()
                    if value:
                        identity_values.append(value)
                for field in ('ad', 'object_name'):
                    value = str(row.get(field) or '').strip()
                    if value:
                        title_values.append(value)
        identity_values = list(dict.fromkeys(identity_values))[:120]
        title_values = list(dict.fromkeys(title_values))[:120]
        clauses: List[str] = []
        params: List[Any] = []
        if identity_values:
            placeholders = ','.join('?' for _ in identity_values)
            clauses.append(f"(ad_id IN ({placeholders}) OR creative_id IN ({placeholders}) OR asset_id IN ({placeholders}))")
            params.extend(identity_values)
            params.extend(identity_values)
            params.extend(identity_values)
        if title_values:
            placeholders = ','.join('?' for _ in title_values)
            clauses.append(f"(ad_name IN ({placeholders}) OR title_text IN ({placeholders}))")
            params.extend(title_values)
            params.extend(title_values)
        if not clauses:
            return []
        try:
            rows = conn.execute(
                f"""
                SELECT
                    asset_id,
                    ad_id,
                    ad_name,
                    creative_id,
                    title_text,
                    thumbnail_url,
                    local_media_ref,
                    image_hash,
                    country,
                    project,
                    account_id,
                    campaign_id,
                    adset_id,
                    last_seen_at,
                    updated_at
                FROM ad_creative_asset
                WHERE (COALESCE(local_media_ref, '') <> '' OR COALESCE(thumbnail_url, '') <> '')
                  AND ({' OR '.join(clauses)})
                ORDER BY last_seen_at DESC, updated_at DESC
                LIMIT 240
                """,
                tuple(params),
            ).fetchall()
        except Exception:
            return []
        return [dict(row) for row in rows]

    def _enrich_daily_payload_creative_previews(
        payload: Dict[str, Any],
        conn: Optional[sqlite3.Connection] = None,
        *,
        allow_network: bool = True,
    ) -> Dict[str, Any]:
        insights = payload.get('creative_insights') or {}
        assets = list(insights.get('assets') or [])
        if conn is not None:
            assets = _load_daily_payload_preview_assets(conn, payload) + assets
        exact_index: Dict[str, Dict[str, Any]] = {}
        compact_index: Dict[str, Dict[str, Any]] = {}
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            if conn is not None and allow_network:
                asset = _repair_preview_asset_local_ref(conn, asset)
            preview = str(asset.get('preview_url') or asset.get('local_media_ref') or asset.get('thumbnail_url') or '').strip()
            if not preview:
                continue
            for field in ('ad_id', 'creative_id', 'asset_id', 'ad_name', 'title_text'):
                for key in _creative_preview_keys(asset.get(field)):
                    if not key:
                        continue
                    exact_index.setdefault(key, asset)
                    compact_index.setdefault(key, asset)

        def _match(row: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
            candidates: List[str] = []
            for field in ('object_id', 'ad_id', 'creative_id', 'ad', 'object_name'):
                candidates.extend(_creative_preview_keys(row.get(field)))
            for key in candidates:
                if key in exact_index:
                    return exact_index[key], 'exact'
            for key in candidates:
                if key in compact_index:
                    return compact_index[key], 'normalized'
            for key in candidates:
                if len(key) < 3:
                    continue
                for asset_key, asset in compact_index.items():
                    if len(asset_key) >= 3 and (key in asset_key or asset_key in key):
                        return asset, 'fuzzy_name'
            return None, ''

        for collection_name in ('recommendations', 'ad_objects'):
            rows = payload.get(collection_name) or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                asset, match_method = _match(row)
                if not asset and conn is not None and allow_network:
                    asset = _fetch_preview_asset_from_meta_ad_name(conn, row)
                    match_method = 'meta_name_lookup' if asset else ''
                if not asset:
                    row['creative_preview_status'] = 'unavailable'
                    continue
                row['creative_preview_url'] = str(asset.get('preview_url') or asset.get('local_media_ref') or asset.get('thumbnail_url') or '').strip()
                row['creative_preview_title'] = str(asset.get('ad_name') or asset.get('title_text') or asset.get('ad_id') or asset.get('creative_id') or '').strip()
                row['creative_preview_asset_id'] = str(asset.get('asset_id') or '').strip()
                row['source_ad_id'] = str(row.get('source_ad_id') or asset.get('ad_id') or '').strip()
                row['source_creative_id'] = str(row.get('source_creative_id') or asset.get('creative_id') or '').strip()
                row['source_image_id'] = str(row.get('source_image_id') or asset.get('asset_id') or '').strip()
                row['source_image_signed_url'] = str(row.get('source_image_signed_url') or asset.get('source_image_local_ref') or asset.get('source_image_url') or '').strip()
                row['source_image_hash'] = str(row.get('source_image_hash') or asset.get('source_image_hash') or asset.get('image_hash') or '').strip()
                row['creative_preview_match'] = match_method
                row['creative_preview_status'] = 'matched'
        _reconcile_daily_payload_actions_by_creative_asset(payload)
        return payload

    def _reconcile_daily_payload_actions_by_creative_asset(payload: Dict[str, Any]) -> Dict[str, Any]:
        rows = [row for row in list((payload or {}).get('recommendations') or []) if isinstance(row, dict)]
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            asset_key = str(row.get('creative_preview_url') or row.get('creative_preview_asset_id') or '').strip()
            if not asset_key:
                continue
            groups[asset_key].append(row)

        generative_diagnoses = {'front_funnel_weak', 'low_quality_traffic', 'creative_fatigue', 'audience_mismatch'}
        for asset_key, asset_rows in groups.items():
            if len(asset_rows) < 2:
                continue
            spend = 0.0
            im_entries = 0.0
            user_engaged = 0.0
            for row in asset_rows:
                evidence = row.get('evidence') if isinstance(row.get('evidence'), dict) else {}
                funnel = evidence.get('funnel_metrics') if isinstance(evidence.get('funnel_metrics'), dict) else {}
                spend += float(evidence.get('spend') or 0.0)
                im_entries += float(funnel.get('im_entries') or 0.0)
                user_engaged += float(funnel.get('user_engaged_im_users') or 0.0)
            if im_entries < 20 or user_engaged < 10:
                continue
            user_engaged_rate = user_engaged / im_entries if im_entries else 0.0
            user_engaged_cost = spend / user_engaged if user_engaged else None
            if user_engaged_rate < 0.20:
                continue
            asset_has_effective_post_im_row = any(
                str(row.get('diagnosis_type') or '') in {'creative_effective_post_im_failed', 'business_result_anomaly', 'scale_opportunity'}
                or str(row.get('action_type') or '') == 'inspect_post_im_funnel'
                for row in asset_rows
            )
            for row in asset_rows:
                if row.get('allow_generate_creative') is not True:
                    continue
                if str(row.get('diagnosis_type') or '') not in generative_diagnoses:
                    continue
                replacement_diagnosis = 'creative_effective_post_im_failed' if asset_has_effective_post_im_row else 'continue_observe'
                replacement_action = 'inspect_post_im_funnel' if asset_has_effective_post_im_row else 'observe'
                row['allow_generate_creative'] = False
                row['allow_scale'] = False
                row['allow_pause'] = False
                row['diagnosis_type'] = replacement_diagnosis
                row['diagnosis_type_zh'] = '同素材汇总有效'
                row['action_type'] = replacement_action
                row['action_type_zh'] = '检查im链路' if asset_has_effective_post_im_row else '继续观察'
                row['primary_layer'] = 'creative_asset_group'
                row['status_tag'] = 'same_asset_effective'
                row['reason_zh'] = (
                    '同一素材在其他广告对象已累计带来足够的用户行为型有效 IM，'
                    '当前单条弱表现优先排查投放人群、广告组或继续观察，不直接重画素材。'
                )
                row['creative_asset_group_diagnosis'] = {
                    'status': 'asset_effective_mixed_delivery',
                    'asset_key': asset_key,
                    'sampled_ad_count': len(asset_rows),
                    'im_entries': round(im_entries, 4),
                    'user_engaged_im_users': round(user_engaged, 4),
                    'user_engaged_im_rate': round(user_engaged_rate, 4),
                    'user_engaged_im_cost': round(user_engaged_cost, 4) if user_engaged_cost is not None else None,
                    'action': 'suppress_repair_creative',
                }
        for asset_key, asset_rows in groups.items():
            if len(asset_rows) < 2:
                continue
            action_flags = {bool(row.get('allow_generate_creative')) for row in asset_rows}
            if len(action_flags) <= 1:
                continue
            spend = 0.0
            im_entries = 0.0
            user_engaged = 0.0
            for row in asset_rows:
                evidence = row.get('evidence') if isinstance(row.get('evidence'), dict) else {}
                funnel = evidence.get('funnel_metrics') if isinstance(evidence.get('funnel_metrics'), dict) else {}
                spend += float(evidence.get('spend') or 0.0)
                im_entries += float(funnel.get('im_entries') or 0.0)
                user_engaged += float(funnel.get('user_engaged_im_users') or 0.0)
            user_engaged_rate = user_engaged / im_entries if im_entries else 0.0
            user_engaged_cost = spend / user_engaged if user_engaged else None
            if im_entries >= 20 and (user_engaged < 10 or user_engaged_rate < 0.20):
                group_status = 'asset_weak'
                group_action = 'generate_repair_creative'
                allow_generate = True
            else:
                group_status = 'asset_effective_or_insufficient'
                group_action = 'observe'
                allow_generate = False
            for row in asset_rows:
                row['allow_generate_creative'] = allow_generate
                row['allow_scale'] = False if not allow_generate else bool(row.get('allow_scale') or False)
                row['allow_pause'] = False
                row['action_type'] = group_action
                row['action_type_zh'] = '生成修正素材' if allow_generate else '继续观察'
                row['diagnosis_type'] = 'low_quality_traffic' if allow_generate else 'continue_observe'
                row['diagnosis_type_zh'] = '同素材汇总偏弱' if allow_generate else '同素材汇总有效或样本不足'
                row['primary_layer'] = 'creative_asset_group'
                row['status_tag'] = 'same_asset_weak' if allow_generate else 'same_asset_observe'
                row['reason_zh'] = (
                    '同一素材按预览图汇总后统一判断，避免同图在不同广告对象上出现一部分修、一部分不修。'
                )
                row['creative_asset_group_diagnosis'] = {
                    'status': group_status,
                    'asset_key': asset_key,
                    'sampled_ad_count': len(asset_rows),
                    'im_entries': round(im_entries, 4),
                    'user_engaged_im_users': round(user_engaged, 4),
                    'user_engaged_im_rate': round(user_engaged_rate, 4),
                    'user_engaged_im_cost': round(user_engaged_cost, 4) if user_engaged_cost is not None else None,
                    'action': group_action,
                }
        return payload

    def _strip_daily_payload_runtime_preview_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = copy.deepcopy(payload)
        runtime_keys = {
            'creative_preview_url',
            'creative_preview_title',
            'creative_preview_asset_id',
            'creative_preview_match',
            'creative_preview_status',
        }
        for collection_name in ('recommendations', 'ad_objects'):
            for row in cleaned.get(collection_name) or []:
                if isinstance(row, dict):
                    for key in runtime_keys:
                        row.pop(key, None)
        return cleaned

    def _lite_daily_report_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Return the compact payload used by the dashboard recommendation panel."""
        cleaned = dict(payload or {})
        cleaned.pop('creative_insights', None)
        cleaned.pop('review_skeleton', None)
        cleaned.pop('creative_test_plan', None)
        cleaned['lite'] = True
        return cleaned

    def _enrich_daily_payload_decision_states(
        payload: Dict[str, Any], *, conn: sqlite3.Connection,
    ) -> Dict[str, Any]:
        recommendations = [
            item for item in payload.get('recommendations') or [] if isinstance(item, dict)
        ]
        recommendation_ids = sorted({
            str(item.get('recommendation_id') or '').strip()
            for item in recommendations
            if str(item.get('recommendation_id') or '').strip()
        })
        if not recommendation_ids:
            return payload
        placeholders = ','.join('?' for _ in recommendation_ids)
        try:
            rows = conn.execute(
                f"""
                SELECT d.recommendation_id, d.decision_id, d.selected_action, d.status,
                       d.target_type, d.target_id, d.created_at, d.updated_at,
                       e.episode_id, e.status AS episode_status
                FROM growth_decision d
                LEFT JOIN growth_decision_episode e ON e.decision_id=d.decision_id
                WHERE d.recommendation_id IN ({placeholders})
                ORDER BY d.created_at DESC, e.created_at DESC
                """,
                recommendation_ids,
            ).fetchall()
        except sqlite3.OperationalError:
            return payload
        by_recommendation: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            recommendation_id = str(row['recommendation_id'] or '')
            if recommendation_id and recommendation_id not in by_recommendation:
                by_recommendation[recommendation_id] = dict(row)
        for item in recommendations:
            decision = by_recommendation.get(str(item.get('recommendation_id') or ''))
            if decision:
                item['decision_state'] = decision
        from app.growth.recommendation_management import enrich_system_managed_recommendations
        enrich_system_managed_recommendations(conn, payload)
        return payload

    def _build_daily_ad_report_for_request(
        request: Request,
        *,
        report_date: Optional[str] = None,
        country: Optional[str] = None,
        project: Optional[str] = None,
        account_id: Optional[str] = None,
        platform: Optional[str] = None,
        data_mode: str = 'fixture',
        days: int = 30,
        top_limit: int = 25,
        window_days: int = 1,
        fast_cached: bool = False,
    ) -> Dict[str, Any]:
        if not ad_daily_report_enabled or ad_recommendation_mode == 'off':
            raise HTTPException(status_code=404, detail='ad_daily_report_disabled')
        normalized_data_mode = str(data_mode or 'fixture').strip().lower()
        if normalized_data_mode not in {'fixture', 'real'}:
            raise HTTPException(status_code=400, detail='unsupported_data_mode')
        normalized_window_days = min(max(int(window_days or 1), 1), 31)
        report_cache_date = str(report_date or '').strip()
        if normalized_window_days > 1 and report_cache_date:
            report_cache_date = f'{report_cache_date}__last{normalized_window_days}d'
        normalized_target_app = _normalize_ad_dashboard_target_app(request.query_params.get('target_app') or 'all') or 'all'
        payload = None
        if normalized_target_app == 'all':
            with db.connect() as conn:
                payload = load_persisted_daily_report(
                    conn,
                    report_date=report_cache_date or report_date,
                    data_mode=normalized_data_mode,
                )
                if payload and str(payload.get('rule_version') or '') != RECOMMENDATION_RULE_VERSION:
                    payload = None
        if payload and not _ad_daily_report_payload_has_unknown_country(payload):
            payload = _ad_daily_report_apply_funnel_caps(payload)
            if normalized_window_days > 1:
                payload['report_cache_date'] = payload.get('report_date')
                payload['report_date'] = str(report_date or payload.get('report_date') or '').split('__', 1)[0]
            if not fast_cached:
                try:
                    with db.connect() as conn:
                        payload['creative_insights'] = build_creative_intelligence_payload(
                            report_from_dict(payload),
                            conn=conn,
                            feature_flags=ad_creative_flags,
                        )
                        payload = _enrich_daily_payload_creative_previews(payload, conn=conn)
                except Exception:
                    pass
            payload['feature_flags'] = {
                'AD_DAILY_REPORT_ENABLED': ad_daily_report_enabled,
                'AD_RECOMMENDATION_MODE': ad_recommendation_mode,
                'REAL_BIND_PROVIDER': real_bind_provider_kind,
                'AD_REVIEW_ENABLED': ad_review_enabled,
                'AD_CREATIVE_ANALYSIS_ENABLED': ad_creative_analysis_enabled,
                **ad_creative_flags,
            }
            if account_id:
                payload['ad_objects'] = [item for item in payload.get('ad_objects') or [] if str(item.get('account_id') or '') == str(account_id)]
            if platform:
                payload['platform_filter'] = str(platform)
            if fast_cached:
                with db.connect() as conn:
                    payload = _enrich_daily_payload_creative_previews(payload, conn=conn, allow_network=False)
                return payload
            with db.connect() as conn:
                return _enrich_daily_payload_creative_previews(payload, conn=conn)
        request_date_to = request.query_params.get('date_to') or request.query_params.get('to') or report_date
        request_date_from = request.query_params.get('date_from') or request.query_params.get('from')
        if normalized_window_days > 1 and request_date_to and not request_date_from:
            try:
                request_date_from = (
                    datetime.strptime(str(request_date_to), '%Y-%m-%d').date()
                    - timedelta(days=normalized_window_days - 1)
                ).isoformat()
            except Exception:
                request_date_from = None
        daily_cache_context = _ad_dashboard_query_context(
            request,
            days=days,
            date_from=request_date_from,
            date_to=request_date_to,
            top_limit=top_limit,
        )
        provider = _real_conversion_provider_for_request(normalized_data_mode)
        snapshot = None
        now_ts = time.time()
        if fast_cached:
            fact_start_date, fact_end_date = ad_dashboard_fact_window_for_context(daily_cache_context)
            try:
                with db.connect() as conn:
                    fact_rows = read_ad_dashboard_fact_rows(
                        conn,
                        start_date=fact_start_date,
                        end_date=fact_end_date,
                    )
            except Exception:
                fact_rows = []
            requested_target_app = str(daily_cache_context.get('target_app') or 'all')
            if requested_target_app not in {'', 'all'} and fact_rows and not any(
                _ad_dashboard_row_target_app(row) == requested_target_app
                for row in fact_rows
            ):
                fact_rows = []
            if fact_rows:
                fact_completeness = ad_dashboard_fact_rows_completeness(
                    fact_rows,
                    start_date=fact_start_date,
                    end_date=fact_end_date,
                    appsflyer_required=bool(appsflyer_api_token),
                )
                use_local_fact_rows = not bool(fact_completeness.get('missing_dates'))
            else:
                use_local_fact_rows = False
            if fact_rows and use_local_fact_rows:
                snapshot = build_ad_data_dashboard_snapshot_from_rows(
                    fact_rows,
                    timezone_name=appsflyer_timezone,
                    days=int(daily_cache_context['days'] or 30),
                    date_from=daily_cache_context.get('date_from'),
                    date_to=daily_cache_context.get('date_to'),
                    top_limit=int(daily_cache_context['top_limit'] or 8),
                    target_app=str(daily_cache_context.get('target_app') or 'all'),
                    filters=daily_cache_context['filters'],
                    platform_filters=daily_cache_context['platform_filters'],
                    platform_date_windows=daily_cache_context.get('platform_date_windows') or {},
                )
                if not fact_completeness.get('complete'):
                    message = ad_dashboard_sync_error_user_message(str(fact_completeness.get('error_message') or ''))
                    snapshot.setdefault('errors', []).append({
                        'source': '本地事实表',
                        'app_id': 'ad_dashboard_fact_rows',
                        'message': message,
                    })
                    snapshot.setdefault('insights', []).append(message)
                snapshot['cache'] = {
                    'hit': True,
                    'layer': 'local_fact' if fact_completeness.get('complete') else 'local_fact_partial',
                    'cached_at': datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
                }
        if snapshot is None:
            snapshot = _build_ad_dashboard_snapshot_for_request(
                request,
                days=days,
                date_from=request_date_from,
                date_to=request_date_to,
                top_limit=top_limit,
            )
        _write_persistent_ad_dashboard_cache(
            _ad_dashboard_summary_cache_key(daily_cache_context),
            snapshot,
            created_at=now_ts,
        )
        report = build_daily_report_from_dashboard_snapshot(
            snapshot,
            report_date=report_date,
            data_mode=normalized_data_mode,
            provider=provider,
            project=project,
            country=country,
            window_days=normalized_window_days,
        )
        if fast_cached:
            payload = report_to_dict(report)
            with db.connect() as conn:
                payload = _enrich_daily_payload_creative_previews(payload, conn=conn, allow_network=False)
        else:
            with db.connect() as conn:
                _sync_meta_creative_assets_if_stale(conn)
                report = report_from_dict({
                    **report_to_dict(report),
                    'creative_insights': build_creative_intelligence_payload(
                        report,
                        conn=conn,
                        feature_flags=ad_creative_flags,
                    ),
                })
                payload = _enrich_daily_payload_creative_previews(report_to_dict(report), conn=conn)
        payload['feature_flags'] = {
            'AD_DAILY_REPORT_ENABLED': ad_daily_report_enabled,
            'AD_RECOMMENDATION_MODE': ad_recommendation_mode,
            'REAL_BIND_PROVIDER': real_bind_provider_kind,
            'AD_REVIEW_ENABLED': ad_review_enabled,
            'AD_CREATIVE_ANALYSIS_ENABLED': ad_creative_analysis_enabled,
            **ad_creative_flags,
        }
        if account_id:
            payload['ad_objects'] = [item for item in payload.get('ad_objects') or [] if str(item.get('account_id') or '') == str(account_id)]
        if platform:
            payload['platform_filter'] = str(platform)
        if not fast_cached:
            with db.connect() as conn:
                payload = _enrich_daily_payload_creative_previews(payload, conn=conn)
        if normalized_target_app == 'all':
            with db.connect() as conn:
                persist_daily_report(
                    conn,
                    replace(report, report_date=report_cache_date) if report_cache_date and report_cache_date != report.report_date else report,
                )
        return payload

    @app.post('/api/ops/ad-data-dashboard/tugao-bind-success/sync')
    def ops_sync_tugao_bind_success_events(
        request: Request,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        if not bind_success_api_token:
            raise HTTPException(status_code=400, detail='bind_success_token_not_configured')
        body = dict(payload or {})
        start_time = str(body.get('start_time') or '').strip()
        end_time = str(body.get('end_time') or '').strip()
        if not start_time or not end_time:
            raise HTTPException(status_code=400, detail='start_time_and_end_time_required')
        try:
            client = TugaoBindSuccessClient(
                token=bind_success_api_token,
                base_url=bind_success_base_url,
                session=bind_success_session,
                timeout=float(body.get('timeout') or 30.0),
                max_retries=int(body.get('max_retries') or 2),
            )
            with db.connect() as conn:
                result = sync_tugao_bind_success_events(
                    conn,
                    client,
                    start_time=start_time,
                    end_time=end_time,
                    project=str(body.get('project') or bind_success_project or 'TUGAO').strip() or 'TUGAO',
                    updated_after=str(body.get('updated_after') or '').strip() or None,
                    page_size=int(body.get('page_size') or 500),
                    max_pages=int(body.get('max_pages') or 20),
                )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f'tugao_bind_success_sync_failed:{exc.__class__.__name__}') from exc
        return {
            'ok': True,
            'provider': 'tugao',
            'mode': 'shadow',
            **result,
        }

    @app.get('/api/ops/ad-data-dashboard/summary')
    def ops_ad_data_dashboard_summary(
        request: Request,
        days: int = 30,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        top_limit: int = 8,
        refresh: bool = False,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        context = _ad_dashboard_query_context(
            request,
            days=days,
            date_from=date_from,
            date_to=date_to,
            top_limit=top_limit,
        )
        filters = context['filters']
        platform_filters = context['platform_filters']
        cache_key = _ad_dashboard_summary_cache_key(context)
        now_ts = time.time()
        cache_window_start, cache_next_refresh = _ad_dashboard_cache_window(now_ts, ad_dashboard_cache_timezone)
        cache_max_age_seconds = ad_dashboard_cache_ttl_seconds if ad_dashboard_cache_ttl_seconds > 0 else max(int(cache_next_refresh - cache_window_start), 1)
        expected_latest_fact_date = _ad_dashboard_latest_complete_utc_date().isoformat()
        latest_fact_date = ''
        try:
            with db.connect() as conn:
                latest_fact_row = conn.execute(
                    """
                    SELECT MAX(state.date)
                    FROM ad_dashboard_sync_state AS state
                    WHERE state.source = 'all'
                      AND state.status = 'ok'
                      AND state.row_count > 0
                      AND EXISTS (
                          SELECT 1
                          FROM ad_dashboard_fact_rows AS fact
                          WHERE fact.date = state.date
                      )
                    """
                ).fetchone()
                latest_fact_date = str((latest_fact_row[0] if latest_fact_row else '') or '').strip()
        except Exception:
            latest_fact_date = ''

        def attach_data_freshness(payload: Dict[str, Any]) -> Dict[str, Any]:
            selected_date_end = str((payload or {}).get('date_end') or '').strip()
            actual_latest_date = latest_fact_date
            if not actual_latest_date:
                freshness_status = 'no_data'
            elif actual_latest_date < expected_latest_fact_date:
                freshness_status = 'delayed'
            else:
                freshness_status = 'current'
            payload['data_freshness'] = {
                'latest_fact_date': actual_latest_date,
                'expected_latest_date': expected_latest_fact_date,
                'selected_date_end': selected_date_end,
                'is_historical_view': bool(
                    actual_latest_date and selected_date_end and selected_date_end < actual_latest_date
                ),
                'status': freshness_status,
                'watermark_source': 'ad_dashboard_sync_state:all:ok',
            }
            return payload

        def read_fresh_cached_payload() -> Optional[Dict[str, Any]]:
            with ad_dashboard_cache_lock:
                cached = ad_dashboard_cache.get(cache_key)
                cached_at = float(cached.get('created_at') or 0.0) if cached else 0.0
                cache_fresh = (
                    cached_at > 0
                    and (
                        now_ts - cached_at < ad_dashboard_cache_ttl_seconds
                        if ad_dashboard_cache_ttl_seconds > 0
                        else cached_at >= cache_window_start
                    )
                )
                if cached and cache_fresh:
                    payload = copy.deepcopy(cached.get('payload') or {})
                    payload['cache'] = {
                        'hit': True,
                        'layer': 'memory',
                        'ttl_seconds': cache_max_age_seconds,
                        'cached_at': datetime.fromtimestamp(cached_at, timezone.utc).isoformat(),
                        'next_refresh_at': datetime.fromtimestamp(cache_next_refresh, ZoneInfo(ad_dashboard_cache_timezone)).isoformat(),
                        'schedule': f'{ad_dashboard_cache_timezone} 09:20 daily',
                    }
                    return payload
            persistent_payload = _read_persistent_ad_dashboard_cache(
                cache_key,
                now_ts=now_ts,
                cache_window_start=cache_window_start,
                cache_next_refresh=cache_next_refresh,
                cache_max_age_seconds=cache_max_age_seconds,
                cache_timezone=ad_dashboard_cache_timezone,
            )
            if persistent_payload is not None:
                persistent_cached_at = datetime.fromisoformat(persistent_payload['cache']['cached_at']).timestamp()
                with ad_dashboard_cache_lock:
                    ad_dashboard_cache[cache_key] = {
                        'created_at': persistent_cached_at,
                        'payload': copy.deepcopy(persistent_payload),
                    }
            return persistent_payload

        if cache_max_age_seconds > 0 and not refresh:
            cached_payload = read_fresh_cached_payload()
            if cached_payload is not None:
                return attach_data_freshness(cached_payload)

        if cache_max_age_seconds > 0 and not refresh:
            stale_payload = _read_any_persistent_ad_dashboard_cache(
                cache_key,
                now_ts=now_ts,
                cache_timezone=ad_dashboard_cache_timezone,
            )
            if stale_payload is not None:
                stale_payload['cache']['serving_reason'] = 'fresh_cache_expired'
                stale_payload['cache']['refresh_mode'] = 'scheduled_or_manual'
                stale_payload.setdefault('insights', []).append(
                    '当前先展示最近一次完整缓存；后台定时任务会继续更新，页面无需等待媒体接口。'
                )
                return attach_data_freshness(stale_payload)

        force_live_for_missing_fact_dates = False
        if cache_max_age_seconds > 0 and not refresh:
            fact_start_date, fact_end_date = ad_dashboard_fact_window_for_context(context)
            try:
                with db.connect() as conn:
                    fact_rows = read_ad_dashboard_fact_rows(
                        conn,
                        start_date=fact_start_date,
                        end_date=fact_end_date,
                    )
            except Exception:
                fact_rows = []
            requested_target_app = str(context.get('target_app') or 'all')
            if requested_target_app not in {'', 'all'} and fact_rows and not any(
                _ad_dashboard_row_target_app(row) == requested_target_app
                for row in fact_rows
            ):
                fact_rows = []
            if fact_rows:
                fact_completeness = ad_dashboard_fact_rows_completeness(
                    fact_rows,
                    start_date=fact_start_date,
                    end_date=fact_end_date,
                    appsflyer_required=bool(appsflyer_api_token),
                )
                missing_fact_dates = set(fact_completeness.get('missing_dates') or [])
                force_live_for_missing_fact_dates = fact_end_date.isoformat() in missing_fact_dates
            if fact_rows and not force_live_for_missing_fact_dates:
                available_fact_dates: List[datetime.date] = []
                for row in fact_rows:
                    raw_fact_date = str((row or {}).get('date') or '').strip()
                    if not raw_fact_date:
                        continue
                    try:
                        available_fact_dates.append(datetime.fromisoformat(raw_fact_date).date())
                    except Exception:
                        continue
                effective_date_to = context.get('date_to')
                if force_live_for_missing_fact_dates and available_fact_dates:
                    effective_date_to = max(available_fact_dates).isoformat()
                effective_date_from = context.get('date_from')
                if effective_date_to and effective_date_from and effective_date_from > effective_date_to:
                    effective_date_from = effective_date_to
                payload = build_ad_data_dashboard_snapshot_from_rows(
                    fact_rows,
                    timezone_name=appsflyer_timezone,
                    days=int(context['days'] or 30),
                    date_from=effective_date_from,
                    date_to=effective_date_to,
                    top_limit=int(context['top_limit'] or 8),
                    target_app=str(context.get('target_app') or 'all'),
                    filters=filters,
                    platform_filters=platform_filters,
                    platform_date_windows=context.get('platform_date_windows') or {},
                )
                payload['cache'] = {
                    'hit': True,
                    'layer': 'local_fact' if fact_completeness.get('complete') else 'local_fact_partial',
                    'ttl_seconds': cache_max_age_seconds,
                    'cached_at': datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
                    'next_refresh_at': datetime.fromtimestamp(cache_next_refresh, ZoneInfo(ad_dashboard_cache_timezone)).isoformat(),
                    'schedule': f'{ad_dashboard_cache_timezone} 09:20 daily',
                }
                with ad_dashboard_cache_lock:
                    ad_dashboard_cache[cache_key] = {
                        'created_at': now_ts,
                        'payload': copy.deepcopy(payload),
                    }
                return attach_data_freshness(payload)

        try:
            payload = build_ad_data_dashboard_snapshot(
                token=appsflyer_api_token,
                app_ids=appsflyer_app_ids,
                timezone_name=appsflyer_timezone,
                base_url=appsflyer_base_url,
                session=appsflyer_session,
                meta_token=meta_ads_access_token,
                meta_ad_account_ids=meta_ads_account_ids,
                meta_api_version=meta_ads_api_version,
                meta_base_url=meta_ads_base_url,
                meta_session=meta_ads_session,
                bind_success_token=bind_success_api_token,
                bind_success_base_url=bind_success_base_url,
                bind_success_project=bind_success_project,
                bind_success_session=bind_success_session,
                days=days,
                date_from=date_from or request.query_params.get('from'),
                date_to=date_to or request.query_params.get('to'),
                top_limit=top_limit,
                target_app=str(context.get('target_app') or 'all'),
                filters=filters,
                platform_filters=platform_filters,
                platform_date_windows=context.get('platform_date_windows') or {},
                include_fact_rows=True,
            )
        except Exception as exc:
            stale_payload = _read_any_persistent_ad_dashboard_cache(
                cache_key,
                now_ts=now_ts,
                cache_timezone=ad_dashboard_cache_timezone,
            )
            if stale_payload is not None:
                message = ad_dashboard_sync_error_user_message(str(exc))
                stale_payload.setdefault('errors', []).append({
                    'source': '广告看板',
                    'app_id': 'ad-data-dashboard-summary',
                    'message': f'最新数据读取失败，已显示最近缓存：{message}',
                    'cls': 'is-cache',
                })
                stale_payload.setdefault('insights', []).append('最新数据读取失败，当前为最近一次可用缓存。')
                return attach_data_freshness(stale_payload)
            raise
        fact_rows = payload.pop('_fact_rows', [])
        if fact_rows:
            fact_dates: List[datetime.date] = []
            for row in fact_rows:
                raw_date = str((row or {}).get('date') or '').strip()
                if not raw_date:
                    continue
                try:
                    fact_dates.append(datetime.fromisoformat(raw_date).date())
                except Exception:
                    continue
            if fact_dates:
                synced_at = datetime.fromtimestamp(now_ts, timezone.utc).isoformat()
                try:
                    with db.connect() as conn:
                        stored_count = replace_ad_dashboard_fact_rows_for_dates(
                            conn,
                            fact_rows,
                            start_date=min(fact_dates),
                            end_date=max(fact_dates),
                            synced_at=synced_at,
                        )
                        fact_completeness = ad_dashboard_fact_rows_completeness(
                            fact_rows,
                            start_date=min(fact_dates),
                            end_date=max(fact_dates),
                            appsflyer_required=bool(appsflyer_api_token),
                        )
                        mark_ad_dashboard_sync_state(
                            conn,
                            source='all',
                            start_date=min(fact_dates),
                            end_date=max(fact_dates),
                            status=str(fact_completeness.get('status') or 'partial'),
                            row_count=stored_count,
                            error_message=str(fact_completeness.get('error_message') or ''),
                            synced_at=synced_at,
                        )
                        conn.commit()
                        if str(fact_completeness.get('status') or '') == 'ok':
                            latest_fact_date = max(latest_fact_date, max(fact_dates).isoformat())
                except Exception:
                    pass
        payload['cache'] = {
            'hit': False,
            'layer': 'live',
            'ttl_seconds': cache_max_age_seconds,
            'cached_at': datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
            'next_refresh_at': datetime.fromtimestamp(cache_next_refresh, ZoneInfo(ad_dashboard_cache_timezone)).isoformat(),
            'schedule': f'{ad_dashboard_cache_timezone} 09:20 daily',
        }
        if cache_max_age_seconds > 0:
            with ad_dashboard_cache_lock:
                ad_dashboard_cache[cache_key] = {
                    'created_at': now_ts,
                    'payload': copy.deepcopy(payload),
                }
            _write_persistent_ad_dashboard_cache(cache_key, payload, created_at=now_ts)
        return attach_data_freshness(payload)

    @app.get('/api/ops/ad-data-dashboard/daily-report')
    def ops_ad_data_dashboard_daily_report(
        request: Request,
        report_date: Optional[str] = None,
        country: Optional[str] = None,
        project: Optional[str] = None,
        account_id: Optional[str] = None,
        platform: Optional[str] = None,
        data_mode: str = 'fixture',
        window_days: int = 1,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        payload = _build_daily_ad_report_for_request(
            request,
            report_date=report_date,
            country=country,
            project=project,
            account_id=account_id,
            platform=platform,
            data_mode=data_mode,
            window_days=window_days,
            fast_cached=True,
        )
        with db.connect() as conn:
            payload = _enrich_daily_payload_decision_states(payload, conn=conn)
        if str(request.query_params.get('lite') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
            return _lite_daily_report_payload(payload)
        return payload

    @app.get('/api/ops/ad-data-dashboard/daily-report/export.xlsx')
    def ops_ad_data_dashboard_daily_report_export_xlsx(
        request: Request,
        report_date: Optional[str] = None,
        country: Optional[str] = None,
        project: Optional[str] = None,
        account_id: Optional[str] = None,
        platform: Optional[str] = None,
        data_mode: str = 'fixture',
        window_days: int = 1,
    ) -> StreamingResponse:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        payload = _build_daily_ad_report_for_request(
            request,
            report_date=report_date,
            country=country,
            project=project,
            account_id=account_id,
            platform=platform,
            data_mode=data_mode,
            window_days=window_days,
        )
        report = report_from_dict(_strip_daily_payload_runtime_preview_fields(payload))
        content = export_daily_report_xlsx(report)
        window_suffix = f"-last{int(window_days or 1)}d" if int(window_days or 1) > 1 else ""
        filename = f"ad-daily-report-{payload.get('report_date') or 'latest'}{window_suffix}.xlsx"
        return StreamingResponse(
            iter([content]),
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )

    @app.get('/api/ops/ad-data-dashboard/creative-insights')
    def ops_ad_data_dashboard_creative_insights(
        request: Request,
        report_date: Optional[str] = None,
        country: Optional[str] = None,
        project: Optional[str] = None,
        account_id: Optional[str] = None,
        platform: Optional[str] = None,
        data_mode: str = 'fixture',
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        payload = _build_daily_ad_report_for_request(
            request,
            report_date=report_date,
            country=country,
            project=project,
            account_id=account_id,
            platform=platform,
            data_mode=data_mode,
        )
        return {
            'report_id': payload.get('report_id'),
            'report_date': payload.get('report_date'),
            'creative_insights': payload.get('creative_insights') or {},
            'feature_flags': payload.get('feature_flags') or {},
        }

    @app.get('/api/ops/im-diagnostics/summary')
    def ops_im_diagnostics_summary(
        request: Request,
        diagnosis_run_id: str = '',
        ad_id: str = '',
        region: str = '',
        target_app: str = 'all',
        limit: int = 20,
        script_limit: int = 80,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        normalized_target_app = _normalize_ad_dashboard_target_app(target_app) or 'all'
        if normalized_target_app == 'timo':
            return _empty_im_diagnostics_summary_payload(target_app=normalized_target_app, region=region)
        normalized_limit = max(1, min(int(limit or 20), 100))
        normalized_script_limit = max(1, min(int(script_limit or limit or 20), 200))
        cache_key = '|'.join((
            'im_diagnostics_summary',
            str(diagnosis_run_id or ''), str(ad_id or ''), str(region or ''),
            normalized_target_app, str(normalized_limit), str(normalized_script_limit),
        ))

        def build_summary() -> Dict[str, Any]:
            with db.connect() as conn:
                return im_diagnostics_summary(
                    conn,
                    diagnosis_run_id=str(diagnosis_run_id or ''),
                    ad_id=str(ad_id or ''),
                    region=str(region or ''),
                    limit=normalized_limit,
                    script_limit=normalized_script_limit,
                )

        return _ops_hot_read_cache_get_or_set(cache_key, 20.0, build_summary, stale_ttl_seconds=90.0)

    @app.get('/api/ops/im-diagnostics/ad/{ad_id}/summary')
    def ops_im_diagnostics_ad_summary(
        request: Request,
        ad_id: str,
        diagnosis_run_id: str = '',
        region: str = '',
        target_app: str = 'all',
        limit: int = 20,
        script_limit: int = 80,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        normalized_target_app = _normalize_ad_dashboard_target_app(target_app) or 'all'
        if normalized_target_app == 'timo':
            return _empty_im_diagnostics_summary_payload(target_app=normalized_target_app, region=region)
        with db.connect() as conn:
            return im_diagnostics_summary(
                conn,
                diagnosis_run_id=str(diagnosis_run_id or ''),
                ad_id=str(ad_id or ''),
                region=str(region or ''),
                limit=max(1, min(int(limit or 20), 100)),
                script_limit=max(1, min(int(script_limit or limit or 20), 200)),
            )

    @app.get('/api/ops/im-diagnostics/conversations')
    def ops_im_diagnostics_conversations(
        request: Request,
        diagnosis_run_id: str = '',
        ad_id: str = '',
        diagnosis: str = '',
        dropoff_stage: str = '',
        region: str = '',
        target_app: str = 'all',
        limit: int = 20,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        normalized_target_app = _normalize_ad_dashboard_target_app(target_app) or 'all'
        if normalized_target_app == 'timo':
            return {'ok': True, 'diagnosis_run_id': '', 'target_app': normalized_target_app, 'conversations': []}
        with db.connect() as conn:
            return im_conversations_payload(
                conn,
                diagnosis_run_id=str(diagnosis_run_id or ''),
                ad_id=str(ad_id or ''),
                diagnosis=str(diagnosis or ''),
                dropoff_stage=str(dropoff_stage or ''),
                region=str(region or ''),
                limit=max(1, min(int(limit or 20), 100)),
            )

    @app.get('/api/ops/im-diagnostics/conversations/{conversation_id}')
    def ops_im_diagnostics_conversation_detail(
        request: Request,
        conversation_id: str,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        with db.connect() as conn:
            payload = im_conversation_detail(conn, str(conversation_id or ''))
        if not payload.get('ok'):
            raise HTTPException(status_code=404, detail=payload.get('detail') or 'conversation_not_found')
        return payload

    @app.post('/api/ops/im-diagnostics/conversations/{conversation_id}/review')
    def ops_im_diagnostics_conversation_review(
        request: Request,
        conversation_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        with db.connect() as conn:
            result = review_im_conversation_diagnosis(
                conn,
                conversation_id=str(conversation_id or ''),
                review_status=str(body.get('human_review_status') or body.get('review_status') or ''),
                comment=str(body.get('comment') or ''),
            )
        if not result.get('ok'):
            raise HTTPException(status_code=400, detail=result.get('detail') or 'review_failed')
        result['reviewed_by'] = str(user.get('username') or user.get('display_name') or '')
        return result

    @app.post('/api/ops/im-diagnostics/script-suggestions/{script_suggestion_id}/approve')
    def ops_im_diagnostics_script_suggestion_approve(
        request: Request,
        script_suggestion_id: str,
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        with db.connect() as conn:
            result = update_im_script_suggestion_status(
                conn,
                script_suggestion_id=str(script_suggestion_id or ''),
                approval_status='approved',
                approved_by=str(user.get('username') or user.get('display_name') or ''),
            )
        if not result.get('ok'):
            raise HTTPException(status_code=400, detail=result.get('detail') or 'script_suggestion_update_failed')
        return result

    @app.post('/api/ops/im-diagnostics/script-suggestions/{script_suggestion_id}/reject')
    def ops_im_diagnostics_script_suggestion_reject(
        request: Request,
        script_suggestion_id: str,
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        with db.connect() as conn:
            result = update_im_script_suggestion_status(
                conn,
                script_suggestion_id=str(script_suggestion_id or ''),
                approval_status='rejected',
                approved_by=str(user.get('username') or user.get('display_name') or ''),
            )
        if not result.get('ok'):
            raise HTTPException(status_code=400, detail=result.get('detail') or 'script_suggestion_update_failed')
        return result

    @app.post('/api/ops/im-diagnostics/mock')
    def ops_im_diagnostics_mock(
        request: Request,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        count = max(1, min(int(body.get('count') or 80), 500))
        start_date = str(body.get('start_date') or datetime.now(timezone.utc).date().isoformat()).strip()
        fixtures = generate_im_diagnosis_fixtures(count=count, start_date=start_date)
        with db.connect() as conn:
            persisted = persist_im_diagnostics_payload(
                conn,
                conversations=fixtures['conversations'],
                messages=fixtures['messages'],
                events=fixtures['events'],
                replace_existing=bool(body.get('replace_existing', True)),
            )
            result: Dict[str, Any] = {
                'ok': True,
                'mode': 'mock',
                'persisted': persisted,
            }
            if body.get('run_diagnosis', True):
                result['diagnosis'] = run_im_diagnosis(conn)
        return result

    @app.post('/api/ops/im-diagnostics/import')
    def ops_im_diagnostics_import(
        request: Request,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        with db.connect() as conn:
            persisted = persist_im_diagnostics_payload(
                conn,
                conversations=list(body.get('conversations') or []),
                messages=list(body.get('messages') or []),
                events=list(body.get('events') or []),
                replace_existing=bool(body.get('replace_existing', True)),
            )
            result: Dict[str, Any] = {'ok': True, 'mode': 'import', 'persisted': persisted}
            if body.get('run_diagnosis', True):
                conversation_ids = [
                    str(row.get('conversation_id') or '').strip()
                    for row in list(body.get('conversations') or [])
                    if str(row.get('conversation_id') or '').strip()
                ]
                result['diagnosis'] = run_im_diagnosis(conn, conversation_ids=conversation_ids or None)
        return result

    @app.get('/api/ops/im-diagnostics/reception-mode-daily')
    def ops_im_diagnostics_reception_mode_daily(
        request: Request,
        start_date: str,
        end_date: str,
        country: str = '',
        external_app: str = '',
        ab_group_at_entry: str = '',
        reception_mode: str = '',
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            start = datetime.strptime(str(start_date or '').strip()[:10], '%Y-%m-%d').date()
            end = datetime.strptime(str(end_date or '').strip()[:10], '%Y-%m-%d').date()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail='invalid_date_range') from exc
        if start > end or (end - start).days > 92:
            raise HTTPException(status_code=400, detail='invalid_date_range')
        token = str(os.getenv('IM_DIAGNOSTICS_API_TOKEN') or '').strip()
        if not token:
            raise HTTPException(status_code=400, detail='im_diagnostics_api_token_not_configured')
        try:
            client = TimeTradeImDiagnosticsClient(
                token=token,
                base_url=str(os.getenv('IM_DIAGNOSTICS_API_BASE_URL') or DEFAULT_IM_DIAGNOSTICS_BASE_URL),
                auth_header=str(os.getenv('IM_DIAGNOSTICS_AUTH_HEADER') or 'authorization'),
            )
            page = client.fetch_reception_mode_daily(
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                country=str(country or '').strip(),
                external_app=str(external_app or '').strip(),
                ab_group_at_entry=str(ab_group_at_entry or '').strip(),
                reception_mode=str(reception_mode or '').strip(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f'im_diagnostics_reception_mode_daily_failed:{exc.__class__.__name__}') from exc
        return {
            'ok': True,
            'source': 'timetrade_im_diagnostics_reception_mode_daily',
            'start_date': start.isoformat(),
            'end_date': end.isoformat(),
            'filters': {
                'country': str(country or '').strip(),
                'external_app': str(external_app or '').strip(),
                'ab_group_at_entry': str(ab_group_at_entry or '').strip(),
                'reception_mode': str(reception_mode or '').strip(),
            },
            'coverage_status': 'available' if page.rows else 'unavailable',
            'raw_row_count': len(page.rows),
            'pages': page.pages,
            'truncated': bool(page.next_cursor),
            'rows': aggregate_reception_mode_daily(page.rows),
        }

    def _cached_result_message_rows(
        *,
        kind: str,
        start_date: str,
        end_date: str,
        step_code: str,
        country: str,
        external_app: str,
        guild_name: str,
        limit: int,
    ) -> Dict[str, Any]:
        try:
            start = datetime.strptime(str(start_date or '').strip()[:10], '%Y-%m-%d').date()
            end = datetime.strptime(str(end_date or '').strip()[:10], '%Y-%m-%d').date()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail='invalid_date_range') from exc
        if start > end or (end - start).days >= 120:
            raise HTTPException(status_code=400, detail='invalid_date_range')
        with db.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = im_result_message_detail_rows(
                conn,
                kind=kind,
                start_date_utc=start.isoformat(),
                end_date_utc=end.isoformat(),
                step_code=step_code,
                country=country,
                external_app=external_app,
                guild_name=guild_name,
                limit=limit,
            )
        return {
            'ok': True,
            'source': 'im_result_message_facts_v1',
            'timezone': 'UTC+0',
            'start_date_utc': start.isoformat(),
            'end_date_utc': end.isoformat(),
            'coverage_status': 'available' if rows else 'missing',
            'rows': rows,
        }

    @app.get('/api/ops/im-diagnostics/result-message-daily')
    def ops_im_diagnostics_result_message_daily(
        request: Request,
        start_date: str,
        end_date: str,
        step_code: str = '',
        country: str = '',
        external_app: str = '',
        guild_name: str = '',
        limit: int = 500,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        return _cached_result_message_rows(
            kind='daily', start_date=start_date, end_date=end_date, step_code=step_code,
            country=country, external_app=external_app, guild_name=guild_name, limit=limit,
        )

    @app.get('/api/ops/im-diagnostics/result-message-deliveries')
    def ops_im_diagnostics_result_message_deliveries(
        request: Request,
        start_date: str,
        end_date: str,
        step_code: str = '',
        country: str = '',
        external_app: str = '',
        guild_name: str = '',
        limit: int = 100,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        return _cached_result_message_rows(
            kind='deliveries', start_date=start_date, end_date=end_date, step_code=step_code,
            country=country, external_app=external_app, guild_name=guild_name, limit=limit,
        )

    @app.get('/api/ops/im-diagnostics/result-message-interactions')
    def ops_im_diagnostics_result_message_interactions(
        request: Request,
        start_date: str,
        end_date: str,
        step_code: str = '',
        country: str = '',
        external_app: str = '',
        guild_name: str = '',
        limit: int = 100,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        return _cached_result_message_rows(
            kind='interactions', start_date=start_date, end_date=end_date, step_code=step_code,
            country=country, external_app=external_app, guild_name=guild_name, limit=limit,
        )

    @app.post('/api/ops/im-diagnostics/sync-timetrade')
    def ops_im_diagnostics_sync_timetrade(
        request: Request,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        token = str(os.getenv('IM_DIAGNOSTICS_API_TOKEN') or '').strip()
        if not token:
            raise HTTPException(status_code=400, detail='im_diagnostics_api_token_not_configured')
        snapshot_date = str(body.get('snapshot_date') or '').strip()
        start_date = str(body.get('start_date') or '').strip()
        end_date = str(body.get('end_date') or '').strip()
        if not snapshot_date and not (start_date and end_date):
            snapshot_date = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        page_size = max(1, min(int(body.get('page_size') or 100), 500))
        max_pages = max(1, min(int(body.get('max_pages') or 100), 500))
        run_after_sync = bool(body.get('run_diagnosis', True))
        replace_existing = bool(body.get('replace_existing', True))
        diagnosis_run_id = str(body.get('diagnosis_run_id') or '').strip()
        if not diagnosis_run_id:
            scope = snapshot_date or f'{start_date}_{end_date}'
            diagnosis_run_id = f'timetrade_im_api_{scope}'.replace('-', '')
        try:
            client = TimeTradeImDiagnosticsClient(
                token=token,
                base_url=str(os.getenv('IM_DIAGNOSTICS_API_BASE_URL') or DEFAULT_IM_DIAGNOSTICS_BASE_URL),
                auth_header=str(os.getenv('IM_DIAGNOSTICS_AUTH_HEADER') or 'authorization'),
            )
            link_click_token = str(
                os.getenv('BI_MARKETING_DIAGNOSTICS_TOKEN')
                or os.getenv('MARKETING_DIAGNOSTICS_API_TOKEN')
                or os.getenv('BI_MARKETING_DIAGNOSTICS_API_TOKEN')
                or os.getenv('TIMETRADE_MARKETING_DIAGNOSTICS_API_TOKEN')
                or ''
            ).strip()
            link_click_client = TimeTradeImDiagnosticsClient(
                token=link_click_token,
                base_url=str(os.getenv('TIMETRADE_IM_LINK_CLICK_API_BASE_URL') or DEFAULT_IM_DIAGNOSTICS_BASE_URL),
                auth_header='authorization',
            ) if link_click_token else None
            api_payload = fetch_im_diagnostics_payload(
                client,
                link_click_client=link_click_client,
                include_link_click_details=bool(link_click_client),
                snapshot_date=snapshot_date,
                start_date=start_date,
                end_date=end_date,
                page_size=page_size,
                max_pages=max_pages,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f'im_diagnostics_api_sync_failed:{exc.__class__.__name__}') from exc
        with db.connect() as conn:
            persisted = persist_im_diagnostics_payload(
                conn,
                conversations=list(api_payload.get('conversations') or []),
                messages=list(api_payload.get('messages') or []),
                events=list(api_payload.get('events') or []),
                replace_existing=replace_existing,
            )
            result: Dict[str, Any] = {
                'ok': True,
                'mode': 'timetrade_im_diagnostics_api',
                'diagnosis_run_id': diagnosis_run_id,
                'snapshot_date': snapshot_date,
                'start_date': start_date,
                'end_date': end_date,
                'raw_counts': api_payload.get('raw_counts') or {},
                'pages': api_payload.get('pages') or {},
                'truncated_endpoints': sorted((api_payload.get('next_cursors') or {}).keys()),
                'pii_key_path_counts': {
                    endpoint: len(paths)
                    for endpoint, paths in (api_payload.get('pii_key_paths') or {}).items()
                },
                'persisted': persisted,
            }
            if run_after_sync:
                conversation_ids = [
                    str(row.get('conversation_id') or '').strip()
                    for row in list(api_payload.get('conversations') or [])
                    if str(row.get('conversation_id') or '').strip()
                ]
                result['diagnosis'] = run_im_diagnosis(
                    conn,
                    conversation_ids=conversation_ids or None,
                    diagnosis_run_id=diagnosis_run_id,
                )
        return result

    @app.post('/api/ops/im-diagnostics/runs')
    def ops_im_diagnostics_runs(
        request: Request,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        conversation_ids = [
            str(item or '').strip()
            for item in list(body.get('conversation_ids') or [])
            if str(item or '').strip()
        ]
        with db.connect() as conn:
            return run_im_diagnosis(
                conn,
                conversation_ids=conversation_ids or None,
                diagnosis_run_id=str(body.get('diagnosis_run_id') or '') or None,
            )

    @app.post('/api/ops/im-diagnosis-tasks')
    def ops_im_diagnosis_tasks_create(
        request: Request,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                if body.get('conversation_id'):
                    return create_im_llm_diagnosis_task(
                        conn,
                        conversation_id=str(body.get('conversation_id') or ''),
                        diagnosis_run_id=str(body.get('diagnosis_run_id') or ''),
                        max_attempts=int(body.get('max_attempts') or 3),
                        force=bool(body.get('force')),
                    )
                return create_im_llm_diagnosis_tasks_for_latest_run(
                    conn,
                    diagnosis_run_id=str(body.get('diagnosis_run_id') or ''),
                    primary_diagnosis=str(body.get('primary_diagnosis') or ''),
                    dropoff_stage=str(body.get('dropoff_stage') or ''),
                    limit=max(1, min(int(body.get('limit') or 50), 500)),
                    force=bool(body.get('force')),
                )
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if detail == 'conversation_not_found' else 400
            raise HTTPException(status_code=status_code, detail=detail) from exc

    @app.get('/api/ops/im-diagnosis-tasks/next')
    def ops_im_diagnosis_tasks_next(
        request: Request,
        claim: bool = False,
        lease_owner: str = 'hermes-llm-agent',
        lease_seconds: int = 900,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        normalized_owner = str(lease_owner or 'hermes-llm-agent')
        normalized_seconds = int(lease_seconds or 900)
        if claim and db_writer_enabled():
            result = submit_sqlite_write_job({
                'type': 'im_llm_claim_next',
                'lease_owner': normalized_owner,
                'lease_seconds': normalized_seconds,
            }, timeout=20.0)
            task = result.get('task')
        else:
            with db.connect() as conn:
                task = next_im_llm_diagnosis_task(
                    conn,
                    claim=claim,
                    lease_owner=normalized_owner,
                    lease_seconds=normalized_seconds,
                )
        return {'ok': True, 'provider_mode': 'hermes_llm', 'task': task, 'external_write_performed': False}

    @app.post('/api/ops/im-diagnosis-tasks/{task_id}/claim')
    def ops_im_diagnosis_tasks_claim(
        request: Request,
        task_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            normalized_owner = str(body.get('lease_owner') or 'hermes-llm-agent')
            normalized_seconds = int(body.get('lease_seconds') or 900)
            if db_writer_enabled():
                result = submit_sqlite_write_job({
                    'type': 'im_llm_claim',
                    'task_id': str(task_id or ''),
                    'lease_owner': normalized_owner,
                    'lease_seconds': normalized_seconds,
                }, timeout=20.0)
                task = result.get('task')
            else:
                with db.connect() as conn:
                    task = claim_im_llm_diagnosis_task(
                        conn,
                        str(task_id or ''),
                        lease_owner=normalized_owner,
                        lease_seconds=normalized_seconds,
                    )
            return {'ok': True, 'provider_mode': 'hermes_llm', 'task': task, 'external_write_performed': False}
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if detail == 'im_llm_diagnosis_task_not_found' else 400
            raise HTTPException(status_code=status_code, detail=detail) from exc

    @app.get('/api/ops/im-diagnosis-tasks/{task_id}/status')
    def ops_im_diagnosis_tasks_status(request: Request, task_id: str) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            with db.connect() as conn:
                return {'ok': True, 'task': get_im_llm_diagnosis_task(conn, str(task_id or '')), 'external_write_performed': False}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/ops/im-diagnosis-tasks/{task_id}/fail')
    def ops_im_diagnosis_tasks_fail(
        request: Request,
        task_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                task = fail_im_llm_diagnosis_task(
                    conn,
                    str(task_id or ''),
                    error_code=str(body.get('error_code') or 'hermes_llm_diagnosis_failed'),
                    error_message=str(body.get('error_message') or ''),
                    retryable=body.get('retryable') is not False,
                    provider_response=body.get('provider_response') if isinstance(body.get('provider_response'), dict) else {},
                )
            return {'ok': True, 'provider_mode': 'hermes_llm', 'task': task, 'external_write_performed': False}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/ops/im-diagnosis-tasks/{task_id}/result')
    def ops_im_diagnosis_tasks_result(
        request: Request,
        task_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        result = body.get('result') if isinstance(body.get('result'), dict) else body
        provider_response = body.get('provider_response') if isinstance(body.get('provider_response'), dict) else {}
        try:
            with db.connect() as conn:
                return complete_im_llm_diagnosis_task(
                    conn,
                    str(task_id or ''),
                    result=result,
                    provider_response=provider_response,
                )
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if detail == 'im_llm_diagnosis_task_not_found' else 400
            raise HTTPException(status_code=status_code, detail=detail) from exc

    @app.get('/api/ops/ad-data-dashboard/creative-provider-status')
    def ops_ad_data_dashboard_creative_provider_status(
        request: Request,
        probe: bool = False,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        meta_service = MetaCreativeSyncService(
            token=meta_ads_access_token,
            account_ids=meta_ads_account_ids,
            api_version=meta_ads_api_version,
            base_url=meta_ads_base_url,
            session=meta_ads_session,
            page_size=meta_creative_sync_page_size,
            enabled=ad_creative_flags.get('AD_CREATIVE_SYNC_ENABLED', False),
        )
        meta_status = meta_service.probe_readonly_access() if probe else meta_service.readiness()
        activity_service = MetaActivityReadonlyService(
            token=meta_ads_access_token,
            account_ids=meta_ads_account_ids,
            api_version=meta_ads_api_version,
            base_url=meta_ads_base_url,
            session=meta_ads_session,
            page_size=meta_activity_sync_page_size,
            enabled=meta_activity_sync_enabled,
        )
        vision_provider = str(
            cfg.get('AD_CREATIVE_VISION_PROVIDER') or os.getenv('AD_CREATIVE_VISION_PROVIDER') or 'fixture'
        ).strip().lower() or 'fixture'
        ocr_provider = str(
            cfg.get('AD_CREATIVE_OCR_PROVIDER') or os.getenv('AD_CREATIVE_OCR_PROVIDER') or 'fixture'
        ).strip().lower() or 'fixture'
        image_provider_status = external_image_provider_readiness(
            ExternalImageProviderConfig(
                provider=ad_creative_image_provider,
                enabled=ad_creative_image_provider_enabled,
                url=ad_creative_image_provider_url,
                api_key=ad_creative_image_provider_api_key,
                session=ad_creative_image_provider_session,
                timeout_seconds=ad_creative_image_provider_timeout_seconds,
            )
        )
        with db.connect() as conn:
            pro_workbench = chatgpt_pro_workbench_status(conn, enabled=ad_creative_pro_workbench_enabled)
        external_wrapper = {
            'enabled': bool(ad_creative_image_provider_enabled and image_provider_status.get('mode') == 'external_wrapper'),
            'configured': bool(image_provider_status.get('mode') == 'external_wrapper' and image_provider_status.get('ready')),
            'status': 'ready' if image_provider_status.get('mode') == 'external_wrapper' and image_provider_status.get('ready') else 'not_configured',
            'message_cn': '自动图片生成 Provider 可用' if image_provider_status.get('mode') == 'external_wrapper' and image_provider_status.get('ready') else '自动图片生成 Provider 未配置',
        }
        local_production = {
            'enabled': bool(ad_creative_image_provider_enabled and image_provider_status.get('mode') == PROVIDER_LOCAL_PRODUCTION_PNG),
            'configured': bool(image_provider_status.get('mode') == PROVIDER_LOCAL_PRODUCTION_PNG and image_provider_status.get('ready')),
            'status': 'ready' if image_provider_status.get('mode') == PROVIDER_LOCAL_PRODUCTION_PNG and image_provider_status.get('ready') else 'not_configured',
            'message_cn': '本地 1024×1024 PNG 生成可用' if image_provider_status.get('mode') == PROVIDER_LOCAL_PRODUCTION_PNG and image_provider_status.get('ready') else '本地 PNG 生成未启用',
        }
        hermes_image2_agent = {
            'enabled': bool(ad_creative_image_provider_enabled and image_provider_status.get('mode') == PROVIDER_HERMES_IMAGE2_AGENT),
            'configured': bool(image_provider_status.get('mode') == PROVIDER_HERMES_IMAGE2_AGENT and image_provider_status.get('ready')),
            'status': 'ready' if image_provider_status.get('mode') == PROVIDER_HERMES_IMAGE2_AGENT and image_provider_status.get('ready') else 'not_configured',
            'message_cn': 'Hermes image2 Agent 生产链路可用：后端派发任务，Hermes 用 multipart 上传成图，合格后进入待审核。' if image_provider_status.get('mode') == PROVIDER_HERMES_IMAGE2_AGENT and image_provider_status.get('ready') else 'Hermes image2 Agent 未启用',
        }
        recommended_next_action = {
            'mode': image_provider_status.get('mode') if image_provider_status.get('ready') else (
                PROVIDER_CHATGPT_PRO_MANUAL if pro_workbench.get('configured') else image_provider_status.get('mode')
            ),
            'message_cn': (
                '当前使用 Hermes image2 Agent：系统创建生产任务，Agent 上传真实图片文件，质量门禁通过后进入人工审核。'
                if hermes_image2_agent.get('configured')
                else
                '当前可直接生成 1024×1024 信息流 PNG，运营可预览、下载、审核后建实验。'
                if local_production.get('configured')
                else
                '当前未配置自动图片生成服务，已启用人工制图待办模式：只创建任务，需上传可投成图。'
                if pro_workbench.get('configured') and not external_wrapper.get('configured') and not hermes_image2_agent.get('configured')
                else '当前可使用自动图片生成 Provider。'
                if external_wrapper.get('configured')
                else '请启用人工制图待办或配置自动图片生成 Provider。'
            ),
        }
        return {
            'ok': True,
            'probe': bool(probe),
            'runtime': _ops_runtime_version_state(),
            'feature_flags': ad_creative_flags,
            'meta_creative_sync': meta_status,
            'meta_activity_sync': activity_service.readiness(),
            'analysis_providers': {
                'vision': {
                    'provider': vision_provider,
                    'enabled': ad_creative_flags.get('AD_CREATIVE_IMAGE_ANALYSIS_ENABLED', False),
                    'external_provider_configured': vision_provider not in {'', 'fixture', 'local'},
                    'blocking_reasons': [
                        reason for reason, blocked in {
                            'image_analysis_disabled': not ad_creative_flags.get('AD_CREATIVE_IMAGE_ANALYSIS_ENABLED', False),
                            'vision_provider_not_configured': vision_provider in {'', 'fixture', 'local'},
                        }.items() if blocked
                    ],
                },
                'ocr': {
                    'provider': ocr_provider,
                    'enabled': ad_creative_flags.get('AD_CREATIVE_OCR_ENABLED', False),
                    'external_provider_configured': ocr_provider not in {'', 'fixture', 'local'},
                    'blocking_reasons': [
                        reason for reason, blocked in {
                            'ocr_disabled': not ad_creative_flags.get('AD_CREATIVE_OCR_ENABLED', False),
                            'ocr_provider_not_configured': ocr_provider in {'', 'fixture', 'local'},
                        }.items() if blocked
                    ],
                },
            },
            'image_generation_provider': {
                **image_provider_status,
                'output_size': '1024x1024',
                'surface': 'feed_static_ad',
                'external_provider_configured': bool(
                    image_provider_status.get('mode') == 'external_wrapper'
                    and image_provider_status.get('ready')
                ),
            },
            'external_wrapper': external_wrapper,
            'local_production': local_production,
            'hermes_image2_agent': hermes_image2_agent,
            'chatgpt_pro_manual': pro_workbench,
            'recommended_next_action': recommended_next_action,
            'guardrails': [
                'status API never returns tokens or secrets',
                'Meta probe uses readonly ads creative fields only',
                'no external ad write is performed',
            ],
        }

    @app.get('/api/ops/ad-data-dashboard/meta-rate-limit')
    def ops_ad_data_dashboard_meta_rate_limit(
        request: Request, account_id: str = '',
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        guard = meta_rate_limit_manager.guard_state(account_id) if str(account_id or '').strip() else None
        if str(account_id or '').strip():
            states = [meta_rate_limit_manager.snapshot(account_id).as_dict()]
        else:
            states = meta_rate_limit_manager.list_snapshots()
        return {
            'ok': True,
            'guard': guard,
            'states': states,
            'meta_writes_performed': False,
        }

    @app.get('/api/ops/ad-data-dashboard/meta-accounts')
    def ops_ad_data_dashboard_meta_accounts(request: Request) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        if not meta_ads_access_token:
            return {'ok': True, 'accounts': [], 'pages': [], 'available_count': 0, 'available_page_count': 0, 'message': 'meta_access_token_missing', 'meta_writes_performed': False}
        graph_root = f'{meta_ads_base_url.rstrip("/")}/{_normalize_meta_api_version(meta_ads_api_version)}'
        granted_scopes = set()
        try:
            permission_response = meta_ads_session.get(f'{graph_root}/me/permissions', headers={'Authorization': f'Bearer {meta_ads_access_token}'}, timeout=20.0)
            permission_response.raise_for_status()
            granted_scopes = {str(item.get('permission') or '').strip().lower() for item in permission_response.json().get('data') or [] if str(item.get('status') or '').strip().lower() == 'granted'}
        except MetaRateLimitBlocked:
            raise
        except Exception:
            granted_scopes = set()
        has_global_ads_management = 'ads_management' in granted_scopes
        has_global_page_ads_management = 'pages_manage_ads' in granted_scopes
        has_business_asset_management = {'ads_management', 'business_management'}.issubset(granted_scopes)
        url = f'{graph_root}/me/adaccounts'
        params: Optional[Dict[str, Any]] = {'fields': 'id,account_id,name,account_status,disable_reason,permissions,tasks', 'limit': 200}
        next_url = url
        rows: List[Dict[str, Any]] = []
        while next_url:
            response = meta_ads_session.get(next_url, params=params if next_url == url else None, headers={'Authorization': f'Bearer {meta_ads_access_token}'}, timeout=20.0)
            try:
                response.raise_for_status()
            except Exception as exc:
                raise HTTPException(status_code=502, detail='meta_account_discovery_failed') from exc
            payload = response.json()
            for raw in payload.get('data') or []:
                item = dict(raw or {})
                account_id = str(item.get('account_id') or item.get('id') or '').strip().removeprefix('act_')
                if not account_id:
                    continue
                permissions = {str(value or '').strip().upper() for value in [*(item.get('permissions') or []), *(item.get('tasks') or [])] if str(value or '').strip()}
                account_status = int(item.get('account_status') or 0)
                can_manage = has_global_ads_management or bool(permissions.intersection({'ADVERTISE', 'MANAGE', 'MANAGE_CAMPAIGNS'}))
                selectable = account_status == 1 and can_manage
                reason = '账户不可用或已停用' if account_status != 1 else ('当前 Token 缺少广告管理权限' if not can_manage else '')
                rows.append({'account_id': account_id, 'name': str(item.get('name') or '未命名账户').strip() or '未命名账户', 'account_status': account_status, 'status_label': '可用' if selectable else '不可用', 'selectable': selectable, 'disabled_reason': reason})
            next_url = str(((payload.get('paging') or {}).get('next')) or '').strip()
            params = None
        configured = {str(value or '').strip().removeprefix('act_') for value in meta_ads_account_ids}
        rows.sort(key=lambda item: (not item['selectable'], item['account_id'] not in configured, item['name'].lower()))
        available_count = sum(1 for item in rows if item['selectable'])
        page_by_id: Dict[str, Dict[str, Any]] = {}
        page_discovery_succeeded = False

        def collect_pages(first_url: str, *, fields: str, discovery_source: str, business_asset: bool) -> None:
            nonlocal page_discovery_succeeded
            next_page_url = first_url
            page_params: Optional[Dict[str, Any]] = {'fields': fields, 'limit': 200}
            while next_page_url:
                response = meta_ads_session.get(next_page_url, params=page_params if next_page_url == first_url else None, headers={'Authorization': f'Bearer {meta_ads_access_token}'}, timeout=20.0)
                try:
                    response.raise_for_status()
                except Exception:
                    return
                page_discovery_succeeded = True
                payload = response.json()
                for raw in payload.get('data') or []:
                    item = dict(raw or {})
                    page_id = str(item.get('id') or '').strip()
                    if not page_id:
                        continue
                    tasks = {str(value or '').strip().upper() for value in item.get('tasks') or [] if str(value or '').strip()}
                    published = item.get('is_published') is not False
                    eligible = published and ((business_asset and has_business_asset_management) or has_global_page_ads_management or bool(tasks.intersection({'ADVERTISE', 'MANAGE'})))
                    reason = '' if eligible else ('公共主页未发布' if not published else '当前 Token 缺少主页广告权限')
                    candidate = {'page_id': page_id, 'name': str(item.get('name') or '未命名主页').strip() or '未命名主页', 'status_label': '可用' if eligible else '不可用', 'eligible': eligible, 'disabled_reason': reason, 'discovery_source': discovery_source}
                    current = page_by_id.get(page_id)
                    if current is None or (not current['eligible'] and eligible):
                        page_by_id[page_id] = candidate
                next_page_url = str(((payload.get('paging') or {}).get('next')) or '').strip()
                page_params = None

        for business_id in meta_ads_business_ids:
            collect_pages(f'{graph_root}/{business_id}/owned_pages', fields='id,name', discovery_source='business_owned_pages', business_asset=True)
            collect_pages(f'{graph_root}/{business_id}/client_pages', fields='id,name', discovery_source='business_client_pages', business_asset=True)
        collect_pages(f'{graph_root}/me/accounts', fields='id,name,tasks,is_published', discovery_source='me_accounts', business_asset=False)
        if not page_discovery_succeeded:
            raise HTTPException(status_code=502, detail='meta_page_discovery_failed')
        page_rows = list(page_by_id.values())
        page_rows.sort(key=lambda item: (not item['eligible'], item['name'].lower()))
        eligible_page_ids = {str(item['page_id']) for item in page_rows if item['eligible']}
        account_page_ids: Dict[str, str] = {}
        for account in rows:
            account_id = str(account.get('account_id') or '').strip()
            if not account.get('selectable') or not account_id:
                continue
            try:
                ads_url = f'{graph_root}/act_{account_id}/ads'
                ads_params: Optional[Dict[str, Any]] = {'fields': 'creative{object_story_spec}', 'limit': 100}
                page_counts: Dict[str, int] = {}
                page_number = 0
                while ads_url and page_number < 3:
                    response = meta_ads_session.get(
                        ads_url,
                        params=ads_params if page_number == 0 else None,
                        headers={'Authorization': f'Bearer {meta_ads_access_token}'},
                        timeout=20.0,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    for raw_ad in payload.get('data') or []:
                        creative = dict(dict(raw_ad or {}).get('creative') or {})
                        story = dict(creative.get('object_story_spec') or {})
                        page_id = str(story.get('page_id') or '').strip()
                        if page_id:
                            page_counts[page_id] = page_counts.get(page_id, 0) + 1
                    ads_url = str(((payload.get('paging') or {}).get('next')) or '').strip()
                    ads_params = None
                    page_number += 1
                if not page_counts:
                    continue
                mapped_page_id = max(page_counts, key=lambda page_id: (page_counts[page_id], page_id))
                account_page_ids[account_id] = mapped_page_id
                if mapped_page_id not in page_by_id:
                    page_by_id[mapped_page_id] = {
                        'page_id': mapped_page_id,
                        'name': '账户历史主页',
                        'status_label': '账户已使用',
                        'eligible': True,
                        'disabled_reason': '',
                        'discovery_source': 'account_ad_history',
                    }
                    eligible_page_ids.add(mapped_page_id)
            except MetaRateLimitBlocked:
                raise
            except Exception:
                continue
        page_rows = list(page_by_id.values())
        page_rows.sort(key=lambda item: (not item['eligible'], item['name'].lower()))
        available_country_page_ids = {country: page_id for country, page_id in meta_ads_country_page_ids.items() if page_id in eligible_page_ids}
        country_account_ids = {
            country: next((str(item.get('account_id') or '') for item in rows if item.get('selectable') and account_page_ids.get(str(item.get('account_id') or '')) == page_id), '')
            for country, page_id in available_country_page_ids.items()
        }
        country_account_ids = {country: account_id for country, account_id in country_account_ids.items() if account_id}
        return {'ok': True, 'accounts': rows, 'pages': page_rows, 'available_count': available_count, 'available_page_count': sum(1 for item in page_rows if item['eligible']), 'default_account_id': next((item['account_id'] for item in rows if item['selectable'] and item['account_id'] in configured), next((item['account_id'] for item in rows if item['selectable']), '')), 'default_page_id': available_country_page_ids.get('BR', next((item['page_id'] for item in page_rows if item['eligible']), '')), 'country_page_ids': available_country_page_ids, 'country_account_ids': country_account_ids, 'account_page_ids': account_page_ids, 'meta_writes_performed': False}

    @app.post('/api/ops/ad-data-dashboard/meta-accounts/page-eligibility')
    def ops_ad_data_dashboard_meta_page_eligibility(
        request: Request,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        """Validate account/Page ad-creation eligibility without creating an object.

        Page visibility, Business ownership and historical use are only candidate
        discovery signals. The dropdown may call a Page usable only after this
        endpoint has passed a Meta ``validate_only`` Creative request for the
        selected ad account.
        """
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        account_id = str(body.get('account_id') or '').strip().removeprefix('act_')
        country = str(body.get('country') or '').strip().upper()
        force = bool(body.get('force'))
        if force and not meta_rate_limit_manager.force_refresh_allowed(account_id):
            state = meta_rate_limit_manager.snapshot(account_id).as_dict()
            retry_after = max(60, int(state.get('retry_after_seconds') or 300))
            raise MetaRateLimitBlocked(account_id, retry_after, 'meta_force_refresh_guarded')
        discovery = ops_ad_data_dashboard_meta_accounts(request)
        account = next((
            dict(item)
            for item in list(discovery.get('accounts') or [])
            if str(dict(item).get('account_id') or '') == account_id
        ), None)
        if not account or not account.get('selectable'):
            raise HTTPException(status_code=400, detail='meta_account_not_selectable')

        graph_root = f'{meta_ads_base_url.rstrip("/")}/{_normalize_meta_api_version(meta_ads_api_version)}'
        auth_headers = {'Authorization': f'Bearer {meta_ads_access_token}'}
        fixture: Dict[str, Any] = {}
        ads_url = f'{graph_root}/act_{account_id}/ads'
        ads_params: Optional[Dict[str, Any]] = {
            'fields': 'adset_id,creative{id,image_hash,object_story_spec}',
            'limit': 100,
        }
        page_number = 0
        while ads_url and page_number < 3 and not fixture:
            try:
                response = meta_ads_session.get(
                    ads_url,
                    params=ads_params if page_number == 0 else None,
                    headers=auth_headers,
                    timeout=20.0,
                )
                response.raise_for_status()
                response_payload = response.json()
            except MetaRateLimitBlocked:
                raise
            except Exception:
                break
            for raw_ad in response_payload.get('data') or []:
                creative = dict(dict(raw_ad or {}).get('creative') or {})
                story = dict(creative.get('object_story_spec') or {})
                link_data = dict(story.get('link_data') or {})
                adset_id = str(dict(raw_ad or {}).get('adset_id') or '').strip()
                if adset_id and creative.get('image_hash') and link_data.get('link'):
                    fixture = {
                        'adset_id': adset_id,
                        'image_hash': str(creative['image_hash']),
                        'link': str(link_data['link']),
                        'call_to_action': dict(link_data.get('call_to_action') or {'type': 'INSTALL_MOBILE_APP'}),
                    }
                    break
            ads_url = str(((response_payload.get('paging') or {}).get('next')) or '').strip()
            ads_params = None
            page_number += 1

        now_ts = time.time()
        page_results: List[Dict[str, Any]] = []
        validation_requests = 0
        historical_page_id = str(dict(discovery.get('account_page_ids') or {}).get(account_id) or '')
        preferred_country_page_id = str(meta_ads_country_page_ids.get(country) or '')
        for raw_page in list(discovery.get('pages') or []):
            page = dict(raw_page or {})
            page_id = str(page.get('page_id') or '').strip()
            if not page_id:
                continue
            cache_key = (account_id, page_id)
            cached = meta_page_eligibility_cache.get(cache_key)
            if (
                not force
                and cached
                and now_ts - float(cached.get('cached_at') or 0) < meta_page_eligibility_cache_ttl_seconds
            ):
                result = dict(cached)
            elif not fixture:
                result = {
                    'eligible': False,
                    'verification_status': 'NO_FIXTURE',
                    'disabled_reason': '该账户缺少可用于权限校验的历史素材',
                    'error_code': None,
                    'error_subcode': None,
                    'cached_at': now_ts,
                }
                meta_page_eligibility_cache[cache_key] = dict(result)
            else:
                validation_requests += 1
                object_story_spec = {
                    'page_id': page_id,
                    'link_data': {
                        'link': fixture['link'],
                        'image_hash': fixture['image_hash'],
                        'message': 'Tugao page eligibility validation',
                        'name': 'Tugao',
                        'call_to_action': fixture['call_to_action'],
                    },
                }
                try:
                    response = meta_ads_session.post(
                        f'{graph_root}/act_{account_id}/ads',
                        data={
                            'name': f'GLE_PAGE_ELIGIBILITY_VALIDATE_ONLY_{page_id[-6:]}',
                            'adset_id': fixture['adset_id'],
                            'creative': json.dumps({'object_story_spec': object_story_spec}, ensure_ascii=False, separators=(',', ':')),
                            'status': 'PAUSED',
                            'execution_options': json.dumps(['validate_only']),
                        },
                        headers=auth_headers,
                        timeout=20.0,
                    )
                    response_payload = dict(response.json() or {})
                    error = dict(response_payload.get('error') or {})
                    status_code = int(getattr(response, 'status_code', 200) or 200)
                    eligible = status_code < 400 and not error
                    denied = int(error.get('code') or 0) in {10, 100, 190, 200}
                    result = {
                        'eligible': eligible,
                        'verification_status': 'PASSED' if eligible else ('DENIED' if denied else 'ERROR'),
                        'disabled_reason': '' if eligible else (
                            '当前账户无法使用此主页创建并投放广告'
                            if denied
                            else '主页投放权限校验暂时失败'
                        ),
                        'error_code': error.get('code'),
                        'error_subcode': error.get('error_subcode'),
                        'cached_at': now_ts,
                    }
                except MetaRateLimitBlocked:
                    raise
                except Exception:
                    result = {
                        'eligible': False,
                        'verification_status': 'ERROR',
                        'disabled_reason': '主页完整投放权限校验暂时失败',
                        'error_code': None,
                        'error_subcode': None,
                        'cached_at': now_ts,
                    }
                meta_page_eligibility_cache[cache_key] = dict(result)
            page.update({
                'eligible': bool(result.get('eligible')),
                'permission_verified': result.get('verification_status') == 'PASSED',
                'verification_status': str(result.get('verification_status') or 'ERROR'),
                'status_label': '可投放' if result.get('eligible') else '不可投放',
                'disabled_reason': str(result.get('disabled_reason') or ''),
                'error_code': result.get('error_code'),
                'error_subcode': result.get('error_subcode'),
            })
            page_results.append(page)

        eligible_page_ids = [
            str(item['page_id'])
            for item in page_results
            if item.get('permission_verified') and item.get('eligible')
        ]
        # Country configuration and account history only rank Pages. The full
        # validate_only App-promotion chain is the sole eligibility gate.
        preferred_ids = [preferred_country_page_id, historical_page_id]
        default_page_id = next(
            (page_id for page_id in preferred_ids if page_id in eligible_page_ids),
            eligible_page_ids[0] if eligible_page_ids else '',
        )
        return {
            'ok': True,
            'account_id': account_id,
            'pages': page_results,
            'available_page_count': len(eligible_page_ids),
            'account_page_ids': {account_id: default_page_id} if default_page_id else {},
            'account_page_options': {account_id: eligible_page_ids},
            'default_page_id': default_page_id,
            'validation_only': True,
            'validation_requests_performed': validation_requests,
            'meta_objects_created': 0,
            'meta_writes_performed': False,
        }

    @app.post('/api/ops/ad-data-dashboard/creative-sync')
    def ops_ad_data_dashboard_creative_sync(
        request: Request,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        payloads = body.get('creative_payloads')
        creative_account_ids = list(meta_ads_account_ids)
        if not creative_account_ids and meta_ads_access_token:
            try:
                creative_account_ids = _fetch_meta_ad_accounts(
                    token=meta_ads_access_token,
                    api_version=meta_ads_api_version,
                    base_url=meta_ads_base_url,
                    session=meta_ads_session,
                )
            except MetaRateLimitBlocked:
                raise
            except Exception:
                creative_account_ids = []
        service = MetaCreativeSyncService(
            token=meta_ads_access_token,
            account_ids=creative_account_ids,
            api_version=meta_ads_api_version,
            base_url=meta_ads_base_url,
            session=meta_ads_session,
            page_size=meta_creative_sync_page_size,
            enabled=ad_creative_flags.get('AD_CREATIVE_SYNC_ENABLED', False),
        )
        if isinstance(payloads, list):
            result = service.sync_payloads([dict(item or {}) for item in payloads])
        else:
            result = service.sync()
        assets = _localize_creative_asset_previews(result.get('assets') or [])
        with db.connect() as conn:
            persisted = persist_creative_assets(conn, assets)
        return {
            'ok': bool(result.get('ok')),
            'mode': result.get('mode'),
            'synced_count': int(result.get('synced_count') or 0),
            'persisted_count': persisted,
            'errors': result.get('errors') or [],
            'feature_flags': ad_creative_flags,
        }

    @app.post('/api/ops/ad-data-dashboard/meta-activity-sync')
    def ops_ad_data_dashboard_meta_activity_sync(
        request: Request,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        service = MetaActivityReadonlyService(
            token=meta_ads_access_token,
            account_ids=meta_ads_account_ids,
            api_version=meta_ads_api_version,
            base_url=meta_ads_base_url,
            session=meta_ads_session,
            page_size=meta_activity_sync_page_size,
            enabled=meta_activity_sync_enabled,
        )
        result = service.sync(
            since=str(body.get('since') or body.get('date_from') or '').strip(),
            until=str(body.get('until') or body.get('date_to') or '').strip(),
        )
        changes = result.get('changes') or []
        with db.connect() as conn:
            persisted = persist_activity_changes(conn, changes)
        return {
            'ok': bool(result.get('ok')),
            'mode': result.get('mode'),
            'synced_count': int(result.get('synced_count') or 0),
            'persisted_count': persisted,
            'errors': result.get('errors') or [],
        }

    def _notify_hermes_image2_agent(task_payload: Dict[str, Any]) -> Dict[str, Any]:
        webhook_url = str(hermes_image2_agent_webhook_url or '').strip()
        if not webhook_url:
            return {'configured': False, 'sent': False, 'reason': 'webhook_not_configured'}
        task = dict((task_payload or {}).get('task') or task_payload or {})
        job = dict((task_payload or {}).get('job') or task.get('job') or {})
        payload = {
            'event': 'creative_generation_task_queued',
            'provider_mode': PROVIDER_HERMES_IMAGE2_AGENT,
            'task': task,
            'job': {
                'job_id': job.get('job_id'),
                'status': job.get('status'),
                'country': job.get('country'),
                'brand_display_name': job.get('brand_display_name'),
                'experiment_code': job.get('experiment_code'),
            },
            'external_write_performed': False,
        }
        headers = {'content-type': 'application/json'}
        if hermes_image2_agent_token:
            headers['authorization'] = f'Bearer {hermes_image2_agent_token}'
        try:
            response = hermes_image2_agent_session.post(
                webhook_url,
                json=payload,
                headers=headers,
                timeout=max(1, int(hermes_image2_agent_timeout_seconds or 10)),
            )
            status_code = int(getattr(response, 'status_code', 0) or 0)
            if status_code >= 400:
                return {'configured': True, 'sent': False, 'status_code': status_code, 'reason': 'webhook_http_error'}
            return {'configured': True, 'sent': True, 'status_code': status_code or 200}
        except Exception as exc:
            return {'configured': True, 'sent': False, 'reason': 'webhook_request_failed', 'error': str(exc)[:160]}

    def _creative_pro_broadcast(event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        event_payload = {
            'type': str(event_type or 'creative_pro_updated').strip() or 'creative_pro_updated',
            'payload': dict(payload or {}),
            'server_emit_at': datetime.now(timezone.utc).isoformat(),
        }
        with app.state.creative_pro_event_lock:
            app.state.creative_pro_event_id = int(getattr(app.state, 'creative_pro_event_id', 0) or 0) + 1
            event_payload['event_id'] = app.state.creative_pro_event_id
            subscribers = list(getattr(app.state, 'creative_pro_event_subscribers', set()) or set())
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event_payload)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except Exception:
                    pass
                try:
                    subscriber.put_nowait(event_payload)
                except Exception:
                    pass
            except Exception:
                pass

    def _creative_pro_event_signature() -> str:
        try:
            with db.connect() as conn:
                jobs = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT job_id, status, provider_mode, error_code, error_message, completed_at
                        FROM creative_pro_work_queue
                        ORDER BY created_at DESC LIMIT 20
                        """
                    ).fetchall()
                ]
                tasks = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT task_id, job_id, status, error_code, error_message, finished_at, updated_at
                        FROM creative_generation_tasks
                        ORDER BY created_at DESC LIMIT 20
                        """
                    ).fetchall()
                ]
                images = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT image_id, request_id, review_status, provider, created_at
                        FROM creative_generated_images
                        ORDER BY created_at DESC LIMIT 20
                        """
                    ).fetchall()
                ]
            raw = json.dumps({'jobs': jobs, 'tasks': tasks, 'images': images}, ensure_ascii=False, sort_keys=True)
            return hashlib.sha256(raw.encode('utf-8')).hexdigest()
        except Exception:
            return ''

    @app.get('/api/ops/creative-pro-events')
    async def ops_creative_pro_events(request: Request) -> StreamingResponse:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        subscriber: queue.Queue = queue.Queue(maxsize=100)
        with app.state.creative_pro_event_lock:
            app.state.creative_pro_event_subscribers.add(subscriber)
            current_event_id = int(getattr(app.state, 'creative_pro_event_id', 0) or 0)
        last_signature = _creative_pro_event_signature()

        async def event_stream():
            nonlocal last_signature
            try:
                hello = {
                    'type': 'hello',
                    'event_id': current_event_id,
                    'server_emit_at': datetime.now(timezone.utc).isoformat(),
                }
                yield f"event: creative_pro\ndata: {json.dumps(hello, ensure_ascii=False)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.to_thread(subscriber.get, True, 5)
                        yield f"event: creative_pro\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    except queue.Empty:
                        current_signature = _creative_pro_event_signature()
                        if current_signature and current_signature != last_signature:
                            last_signature = current_signature
                            event = {
                                'type': 'creative_pro_changed',
                                'event_id': int(getattr(app.state, 'creative_pro_event_id', 0) or 0),
                                'server_emit_at': datetime.now(timezone.utc).isoformat(),
                            }
                            yield f"event: creative_pro\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                            continue
                        heartbeat = {
                            'type': 'heartbeat',
                            'event_id': int(getattr(app.state, 'creative_pro_event_id', 0) or 0),
                            'server_emit_at': datetime.now(timezone.utc).isoformat(),
                        }
                        yield f"event: heartbeat\ndata: {json.dumps(heartbeat, ensure_ascii=False)}\n\n"
            finally:
                with app.state.creative_pro_event_lock:
                    app.state.creative_pro_event_subscribers.discard(subscriber)

        return StreamingResponse(
            event_stream(),
            media_type='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )

    @app.post('/api/ops/ad-data-dashboard/creative-images/generate')
    def ops_ad_data_dashboard_generate_creative_image(
        request: Request,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        production_task_payload = body.get('production_task') if isinstance(body.get('production_task'), dict) else {}
        source_performance_payload = body.get('source_performance') if isinstance(body.get('source_performance'), dict) else {}
        brief = CreativeImageGenerationBrief(
            country=str(body.get('country') or ''),
            project=str(body.get('project') or ''),
            campaign=str(body.get('campaign') or ''),
            ad_group=str(body.get('ad_group') or body.get('adset') or ''),
            ad=str(body.get('ad') or ''),
            objective=str(body.get('objective') or '真实入会'),
            audience=str(body.get('audience') or '想开始内容创作并需要本地支持的新用户'),
            core_offer=str(body.get('core_offer') or '公会支持、申请流程清晰、本地语言指导'),
            source_performance=dict(source_performance_payload),
            source_preview_url=str(body.get('source_preview_url') or production_task_payload.get('source_preview_url') or ''),
            source_preview_asset_id=str(body.get('source_preview_asset_id') or production_task_payload.get('source_preview_asset_id') or ''),
            source_preview_title=str(body.get('source_preview_title') or production_task_payload.get('source_preview_title') or ''),
            source_diagnosis=str(body.get('source_diagnosis') or production_task_payload.get('diagnosis') or source_performance_payload.get('evidence') or ''),
            revision_goal=str(body.get('revision_goal') or production_task_payload.get('revision_goal') or production_task_payload.get('action') or ''),
            requested_by=str(user.get('username') or user.get('display_name') or ''),
        )
        with db.connect() as conn:
            source_sync = _sync_meta_source_asset_for_creative_generation(conn, body)
            if source_sync.get('synced') or source_sync.get('reason') not in {'not_old_image_request', 'source_image_already_resolved'}:
                body['_source_image_on_demand_sync'] = source_sync
            if source_sync.get('synced'):
                source_fields = {
                    key: source_sync.get(key)
                    for key in (
                        'source_image_id',
                        'source_image_signed_url',
                        'source_image_hash',
                        'source_image_width',
                        'source_image_height',
                        'source_image_quality',
                        'source_image_resolution_status',
                    )
                }
                source_url = str(source_fields.get('source_image_signed_url') or '').strip()
                source_hash = str(source_fields.get('source_image_hash') or '').strip()
                source_ready = bool(
                    str(source_fields.get('source_image_id') or '').strip()
                    and source_url
                    and not urlparse(source_url).path.rstrip('/').lower().endswith('/preview')
                    and re.fullmatch(r'[0-9a-fA-F]{64}', source_hash)
                )
                if source_ready:
                    production_task_payload = dict(production_task_payload)
                    body.update(source_fields)
                    production_task_payload.update(source_fields)
                    for identity_key in ('source_ad_id', 'source_creative_id'):
                        identity_value = str(source_sync.get(identity_key) or '').strip()
                        if identity_value:
                            body[identity_key] = identity_value
                            production_task_payload[identity_key] = identity_value
                    body['production_task'] = production_task_payload
            if ad_creative_image_provider == PROVIDER_HERMES_IMAGE2_AGENT and ad_creative_image_provider_enabled:
                generation_count = max(1, min(int(body.get('generation_count') or 1), 6))
                results: List[Dict[str, Any]] = []
                for generation_index in range(generation_count):
                    generation_body = dict(body)
                    generation_body.pop('generation_count', None)
                    generation_body['candidate_count'] = int(body.get('candidate_count') or 1) if generation_count == 1 else 1
                    generation_body['generation_batch_index'] = generation_index
                    generation_body['generation_batch_count'] = generation_count
                    result = create_hermes_image2_generation_job(
                        conn,
                        brief=brief,
                        payload=generation_body,
                        created_by=str(user.get('username') or user.get('display_name') or ''),
                        image_size=str(body.get('image_size') or '1024x1024'),
                        candidate_count=int(generation_body.get('candidate_count') or 1),
                    )
                    result['hermes_notify'] = _notify_hermes_image2_agent(result)
                    _creative_pro_broadcast('creative_task_queued', {
                        'job_id': (result.get('job') or {}).get('job_id'),
                        'task_id': (result.get('task') or {}).get('task_id'),
                        'status': (result.get('task') or {}).get('status'),
                    })
                    results.append(result)
                first_result = dict(results[0])
                if generation_count > 1:
                    first_result.update({
                        'generation_count': generation_count,
                        'created_count': len(results),
                        'jobs': [result.get('job') for result in results],
                        'tasks': [result.get('task') for result in results],
                        'results': results,
                    })
                return first_result
            if ad_creative_image_provider == PROVIDER_CHATGPT_PRO_MANUAL and ad_creative_pro_workbench_enabled:
                result = create_chatgpt_pro_job(
                    conn,
                    brief=brief,
                    payload=body,
                    created_by=str(user.get('username') or user.get('display_name') or ''),
                )
                _creative_pro_broadcast('creative_job_created', {
                    'job_id': (result.get('job') or {}).get('job_id'),
                    'status': (result.get('job') or {}).get('status'),
                })
                return result
            return create_feed_image_generation(
                conn,
                brief,
                image_provider_config=ExternalImageProviderConfig(
                    provider=ad_creative_image_provider,
                    enabled=ad_creative_image_provider_enabled,
                    url=ad_creative_image_provider_url,
                    api_key=ad_creative_image_provider_api_key,
                    session=ad_creative_image_provider_session,
                    timeout_seconds=ad_creative_image_provider_timeout_seconds,
                ),
            )

    @app.get('/api/ops/ad-data-dashboard/creative-images')
    def ops_ad_data_dashboard_generated_creative_images(request: Request, limit: int = 12, target_app: str = 'all') -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        with db.connect() as conn:
            return {'images': latest_generated_images(conn, limit=limit, target_app=target_app)}

    @app.get('/api/ops/ad-data-dashboard/creative-images/{image_id}/experiment-tracking')
    def ops_ad_data_dashboard_generated_creative_image_experiment_tracking(request: Request, image_id: str) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            with db.connect() as conn:
                return generated_image_experiment_tracking(conn, image_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/ops/ad-data-dashboard/creative-experiment-performance/refresh')
    def ops_ad_data_dashboard_refresh_creative_experiment_performance(
        request: Request,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        days = max(1, min(int(body.get('days') or 7), 30))
        today = datetime.now(timezone.utc).date() - timedelta(days=1)
        date_to = _parse_dashboard_date(body.get('date_to')) or today
        date_from = _parse_dashboard_date(body.get('date_from')) or (date_to - timedelta(days=days - 1))
        if date_from > date_to:
            date_from, date_to = date_to, date_from
        explicit_ad_ids = [
            str(item or '').strip()
            for item in (body.get('ad_ids') or ([body.get('ad_id')] if body.get('ad_id') else []))
            if str(item or '').strip()
        ]
        if not meta_ads_access_token or not meta_ads_session:
            raise HTTPException(status_code=400, detail='meta_ads_not_configured')
        with db.connect() as conn:
            ensure_creative_intelligence_tables(conn)
            if explicit_ad_ids:
                ad_ids = sorted(set(explicit_ad_ids))
            else:
                ad_ids = sorted({
                    str(row['ad_id'] or '').strip()
                    for row in conn.execute(
                        """
                        SELECT DISTINCT ad_id
                        FROM creative_adoption_records
                        WHERE COALESCE(ad_id, '') <> ''
                          AND COALESCE(experiment_id, '') <> ''
                        """
                    ).fetchall()
                    if str(row['ad_id'] or '').strip()
                })
            if not ad_ids:
                return {'ok': True, 'ad_ids': [], 'fetched_rows': 0, 'stored_rows': 0, 'message_cn': '暂无已绑定实验广告需要刷新。'}
            service = MetaCreativeSyncService(
                token=meta_ads_access_token,
                account_ids=meta_ads_account_ids,
                api_version=meta_ads_api_version,
                base_url=meta_ads_base_url,
                session=meta_ads_session,
                page_size=50,
                enabled=True,
            )
            for ad_id in ad_ids:
                asset_exists = conn.execute(
                    "SELECT 1 FROM ad_creative_asset WHERE ad_id = ? LIMIT 1",
                    (ad_id,),
                ).fetchone()
                if asset_exists:
                    continue
                asset = service.fetch_ad_asset(ad_id)
                if asset:
                    persist_creative_assets(conn, [asset])
            account_rows = conn.execute(
                f"""
                SELECT DISTINCT account_id
                FROM ad_creative_asset
                WHERE ad_id IN ({','.join('?' for _ in ad_ids)})
                  AND COALESCE(account_id, '') <> ''
                """,
                tuple(ad_ids),
            ).fetchall()
            account_ids = [str(row['account_id'] or '').strip() for row in account_rows if str(row['account_id'] or '').strip()]
            if not account_ids:
                account_ids = list(meta_ads_account_ids or [])
            account_ids = list(dict.fromkeys([normalize_meta_ad_account_id(item) for item in account_ids if normalize_meta_ad_account_id(item)]))
            if not account_ids:
                raise HTTPException(status_code=400, detail='meta_account_ids_missing')
            target_ad_ids = set(ad_ids)
            fetched_rows: List[Dict[str, Any]] = []
            errors: List[Dict[str, Any]] = []
            for account_id in account_ids:
                try:
                    rows = _fetch_meta_insight_rows(
                        token=meta_ads_access_token,
                        ad_account_id=account_id,
                        api_version=meta_ads_api_version,
                        base_url=meta_ads_base_url,
                        from_date=date_from,
                        to_date=date_to,
                        session=meta_ads_session,
                        account_timezone=_fetch_meta_ad_account_timezone(
                            token=meta_ads_access_token,
                            ad_account_id=account_id,
                            api_version=meta_ads_api_version,
                            base_url=meta_ads_base_url,
                            session=meta_ads_session,
                        ),
                        hourly=False,
                        include_actions=True,
                    )
                    fetched_rows.extend([row for row in rows if str(row.get('ad_id') or '').strip() in target_ad_ids])
                except Exception as exc:
                    errors.append({'account_id': account_id, 'error': _dashboard_error_message(exc)})
            performance_rows = build_ad_creative_performance_rows_from_meta_rows(conn, fetched_rows)
            stored_count = upsert_ad_creative_performance_daily_rows(conn, performance_rows)
            conn.commit()
        return {
            'ok': True,
            'ad_ids': ad_ids,
            'account_ids': account_ids,
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'fetched_rows': len(fetched_rows),
            'stored_rows': stored_count,
            'errors': errors,
            'message_cn': f'已写入 {stored_count} 条 ad_id 级素材表现。',
        }

    @app.get('/api/ops/ad-data-dashboard/creative-assets/{asset_id}/preview')
    def ops_ad_data_dashboard_creative_asset_preview(request: Request, asset_id: str) -> Response:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        safe_id = re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(asset_id or '').strip())[:120]
        if not safe_id:
            raise HTTPException(status_code=404, detail='creative_asset_preview_not_found')
        cache_dir = _ad_creative_preview_cache_dir().resolve()
        candidates = [path for path in cache_dir.glob(f'{safe_id}.*') if path.is_file() and not path.name.endswith('.tmp')]
        if not candidates:
            raise HTTPException(status_code=404, detail='creative_asset_preview_not_found')
        image_path = candidates[0].resolve()
        try:
            image_path.relative_to(cache_dir)
        except ValueError:
            raise HTTPException(status_code=403, detail='creative_asset_preview_path_blocked')
        media_type = {
            '.svg': 'image/svg+xml',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.webp': 'image/webp',
            '.gif': 'image/gif',
        }.get(image_path.suffix.lower(), 'application/octet-stream')
        return Response(image_path.read_bytes(), media_type=media_type, headers={'Cache-Control': 'private, max-age=86400'})

    @app.get('/api/ops/ad-data-dashboard/creative-assets/{asset_id}/source')
    def ops_ad_data_dashboard_creative_asset_source(request: Request, asset_id: str) -> Response:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        safe_id = re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(asset_id or '').strip())[:120]
        if not safe_id:
            raise HTTPException(status_code=404, detail='creative_asset_source_not_found')
        cache_dir = _ad_creative_source_cache_dir().resolve()
        candidates = [path for path in cache_dir.glob(f'{safe_id}.*') if path.is_file() and not path.name.endswith('.tmp')]
        if not candidates:
            raise HTTPException(status_code=404, detail='creative_asset_source_not_found')
        image_path = candidates[0].resolve()
        try:
            image_path.relative_to(cache_dir)
        except ValueError:
            raise HTTPException(status_code=403, detail='creative_asset_source_path_blocked')
        media_type = {
            '.svg': 'image/svg+xml',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.webp': 'image/webp',
            '.gif': 'image/gif',
        }.get(image_path.suffix.lower(), 'application/octet-stream')
        return Response(image_path.read_bytes(), media_type=media_type, headers={'Cache-Control': 'private, max-age=86400'})

    @app.get('/api/ops/ad-data-dashboard/creative-preview-proxy')
    def ops_ad_data_dashboard_creative_preview_proxy(request: Request, url: str) -> Response:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        raw_url = str(url or '').strip()
        if not _ad_creative_preview_source_allowed(raw_url):
            raise HTTPException(status_code=400, detail='creative_preview_url_not_allowed')
        try:
            upstream = requests.get(
                raw_url,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36',
                    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                },
                timeout=12.0,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail='creative_preview_fetch_failed') from exc
        if upstream.status_code >= 400:
            raise HTTPException(status_code=502, detail='creative_preview_fetch_failed')
        content = upstream.content or b''
        if not content:
            raise HTTPException(status_code=502, detail='creative_preview_empty')
        if len(content) > 5 * 1024 * 1024:
            raise HTTPException(status_code=502, detail='creative_preview_too_large')
        content_type = str(upstream.headers.get('content-type') or '').split(';', 1)[0].strip().lower()
        if not content_type.startswith('image/'):
            raise HTTPException(status_code=502, detail='creative_preview_not_image')
        return Response(
            content,
            media_type=content_type,
            headers={'Cache-Control': 'private, max-age=300'},
        )

    @app.post('/api/ops/ad-data-dashboard/creative-images/{image_id}/review')
    def ops_ad_data_dashboard_review_creative_image(
        request: Request,
        image_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                result = create_review_record(
                    conn,
                    image_id=image_id,
                    review_status=str(body.get('review_status') or body.get('status') or 'NEEDS_REVIEW'),
                    reviewer=str(user.get('username') or user.get('display_name') or ''),
                    checks=dict(body.get('checks') or {}),
                    decision_reason=str(body.get('decision_reason') or ''),
                )
                linked_job_id = str(result.get('job_id') or result.get('completed_job_id') or '').strip()
                if linked_job_id:
                    job_row = conn.execute(
                        'SELECT experiment_id, material_refs_json FROM creative_pro_work_queue WHERE job_id = ?',
                        (linked_job_id,),
                    ).fetchone()
                    material_refs = json.loads(job_row['material_refs_json'] or '{}') if job_row else {}
                    growth_experiment_id = str(
                        material_refs.get('growth_experiment_id')
                        or (job_row['experiment_id'] if job_row else '')
                        or ''
                    ).strip()
                    if growth_experiment_id:
                        try:
                            service = AdExperimentService(conn)
                            experiment = service.record_creative_review(
                                growth_experiment_id,
                                str(result.get('review_status') or ''),
                                actor=str(user.get('user_id') or user.get('username') or ''),
                                image_id=image_id,
                                job_id=linked_job_id,
                                image_hash=str(result.get('image_hash') or ''),
                            )
                            result['growth_experiment_id'] = growth_experiment_id
                            result['growth_experiment_state'] = experiment.get('state')
                            if result.get('review_status') == 'APPROVED':
                                result['next_step'] = 'CREATE_PAUSED_AD_PLAN'
                        except GrowthError:
                            pass
            _creative_pro_broadcast('creative_image_reviewed', {
                'image_id': image_id,
                'review_status': result.get('review_status'),
            })
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post('/api/ops/ad-data-dashboard/creative-images/batch-download')
    def ops_ad_data_dashboard_batch_download_creative_images(
        request: Request,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> StreamingResponse:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        requested_ids = list(dict.fromkeys(
            str(value or '').strip()
            for value in list((payload or {}).get('image_ids') or [])
            if str(value or '').strip()
        ))
        if not requested_ids:
            raise HTTPException(status_code=400, detail='creative_image_ids_required')
        if len(requested_ids) > 60:
            raise HTTPException(status_code=400, detail='creative_image_batch_limit_exceeded')
        placeholders = ','.join('?' for _ in requested_ids)
        with db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT image_id, image_ref, market, brand, creative_direction,
                       created_at, metadata_json
                FROM creative_generated_images
                WHERE image_id IN ({placeholders})
                  AND COALESCE(LOWER(review_status), '') != 'deleted'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM creative_pro_work_queue q
                      WHERE q.job_id = json_extract(creative_generated_images.metadata_json, '$.job_id')
                        AND q.status = 'deleted'
                  )
                """,
                requested_ids,
            ).fetchall()
            images = [dict(row) for row in rows]
            for image in images:
                image['metadata'] = json.loads(image.pop('metadata_json') or '{}')
            enrich_creative_image_names(conn, images)
        by_id = {str(image['image_id']): image for image in images}
        allowed_root = (Path(__file__).resolve().parents[1] / 'data' / 'ad_creative_generated_images').resolve()
        archive = io.BytesIO()
        downloaded_count = 0
        used_names: set[str] = set()
        with zipfile.ZipFile(archive, mode='w', compression=zipfile.ZIP_DEFLATED) as bundle:
            for image_id in requested_ids:
                image = by_id.get(image_id)
                if not image:
                    continue
                image_path = Path(str(image.get('image_ref') or '')).resolve()
                try:
                    image_path.relative_to(allowed_root)
                except ValueError:
                    continue
                if not image_path.is_file():
                    continue
                filename = creative_image_download_filename(image, image_path.suffix.lower() or '.png')
                if filename in used_names:
                    stem = Path(filename).stem
                    suffix = Path(filename).suffix
                    serial = 2
                    while f'{stem}-{serial}{suffix}' in used_names:
                        serial += 1
                    filename = f'{stem}-{serial}{suffix}'
                used_names.add(filename)
                bundle.write(image_path, arcname=filename)
                downloaded_count += 1
        if not downloaded_count:
            raise HTTPException(status_code=404, detail='creative_image_files_not_found')
        archive.seek(0)
        skipped_count = len(requested_ids) - downloaded_count
        archive_name = f'creative-materials-{datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")}.zip'
        return StreamingResponse(
            archive,
            media_type='application/zip',
            headers={
                'Content-Disposition': f'attachment; filename="{archive_name}"',
                'Cache-Control': 'private, no-store',
                'X-Creative-Downloaded-Count': str(downloaded_count),
                'X-Creative-Skipped-Count': str(skipped_count),
            },
        )

    @app.post('/api/ops/ad-data-dashboard/creative-images/{image_id}/adopt')
    def ops_ad_data_dashboard_adopt_creative_image(
        request: Request,
        image_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                raw_ad_id = str(body.get('ad_id') or '').strip()
                link_context = _extract_meta_ads_manager_context(raw_ad_id)
                ad_id = str(link_context.get('ad_id') or raw_ad_id).strip()
                route_business_id = str(body.get('business_id') or link_context.get('business_id') or '').strip()
                creative_id = str(body.get('creative_id') or '').strip()
                adset_id = str(body.get('adset_id') or link_context.get('adset_id') or '').strip()
                campaign_id = str(body.get('campaign_id') or link_context.get('campaign_id') or '').strip()
                account_candidates = _meta_ad_account_candidates_for_context(
                    meta_ads_account_ids,
                    link_context=link_context,
                    body=body,
                )
                evidence: Dict[str, Any] = {}
                if ad_id and meta_ads_access_token and meta_ads_session:
                    try:
                        service = MetaCreativeSyncService(
                            token=meta_ads_access_token,
                            account_ids=account_candidates or meta_ads_account_ids,
                            api_version=meta_ads_api_version,
                            base_url=meta_ads_base_url,
                            session=meta_ads_session,
                            page_size=1,
                            enabled=True,
                        )
                        account_id = account_candidates[0] if account_candidates else ''
                        asset = service.fetch_ad_asset(ad_id, account_id=account_id)
                        if asset:
                            localized = _localize_creative_asset_previews(_localize_creative_asset_sources([asset]))
                            persist_creative_assets(conn, localized)
                            loaded_asset = localized[0] if localized else asset
                            creative_id = creative_id or str(getattr(loaded_asset, 'creative_id', '') or '')
                            adset_id = adset_id or str(getattr(loaded_asset, 'adset_id', '') or '')
                            campaign_id = campaign_id or str(getattr(loaded_asset, 'campaign_id', '') or '')
                            evidence = {
                                'meta_asset_synced': True,
                                'link_account_id': str(link_context.get('account_id') or ''),
                                'link_business_id': route_business_id,
                                'route_account_id': account_id,
                                'route_account_candidates': account_candidates[:5],
                                'configured_account_count': len(meta_ads_account_ids or []),
                                'account_id': str(getattr(loaded_asset, 'account_id', '') or ''),
                                'asset_id': str(getattr(loaded_asset, 'asset_id', '') or ''),
                                'image_hash': str(getattr(loaded_asset, 'image_hash', '') or ''),
                                'source_image_hash': str(getattr(loaded_asset, 'source_image_hash', '') or ''),
                                'source_image_quality': str(getattr(loaded_asset, 'source_image_quality', '') or ''),
                                'source_image_width': int(getattr(loaded_asset, 'source_image_width', 0) or 0),
                                'source_image_height': int(getattr(loaded_asset, 'source_image_height', 0) or 0),
                            }
                        else:
                            evidence = {
                                'meta_asset_synced': False,
                                'reason': 'meta_ad_asset_not_found',
                                'link_account_id': str(link_context.get('account_id') or ''),
                                'link_business_id': route_business_id,
                                'route_account_id': account_id,
                                'route_account_candidates': account_candidates[:5],
                                'configured_account_count': len(meta_ads_account_ids or []),
                            }
                    except Exception as exc:
                        evidence = {'meta_asset_synced': False, 'reason': exc.__class__.__name__}
                return mark_generated_image_adopted(
                    conn,
                    image_id=image_id,
                    ad_id=ad_id,
                    creative_id=creative_id,
                    adset_id=adset_id,
                    campaign_id=campaign_id,
                    adopted_by=str(user.get('username') or user.get('display_name') or ''),
                    evidence=evidence,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post('/api/ops/ad-data-dashboard/creative-experiments/{suggestion_id}/approve-generate')
    def ops_ad_data_dashboard_approve_creative_experiment_generation(
        request: Request,
        suggestion_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                experiment = approve_creative_experiment_generation(
                    conn,
                    suggestion_id=suggestion_id,
                    generated_image_id=str(body.get('generated_image_id') or body.get('image_id') or ''),
                    experiment_mode=str(body.get('experiment_mode') or body.get('mode') or 'replacement'),
                    source_ad_id=str(body.get('source_ad_id') or body.get('ad_id') or ''),
                    source_creative_id=str(body.get('source_creative_id') or body.get('creative_id') or ''),
                    source_campaign_id=str(body.get('source_campaign_id') or body.get('campaign_id') or ''),
                    source_adset_id=str(body.get('source_adset_id') or body.get('adset_id') or ''),
                    country=str(body.get('country') or ''),
                    created_by=str(user.get('username') or user.get('display_name') or ''),
                    payload=body,
                )
            return {'ok': True, **experiment}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get('/api/ops/ad-data-dashboard/creative-experiments/{experiment_id}/binding-status')
    def ops_ad_data_dashboard_creative_experiment_binding_status(
        request: Request,
        experiment_id: str,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            with db.connect() as conn:
                return creative_experiment_binding_status(conn, experiment_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/ops/ad-data-dashboard/creative-experiments/{experiment_id}/detect-binding')
    def ops_ad_data_dashboard_detect_creative_experiment_binding(
        request: Request,
        experiment_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                return detect_creative_experiment_binding(
                    conn,
                    experiment_id=experiment_id,
                    meta_ads=list(body.get('meta_ads') or body.get('creative_snapshots') or []),
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post('/api/ops/ad-data-dashboard/creative-experiments/{experiment_id}/confirm-binding')
    def ops_ad_data_dashboard_confirm_creative_experiment_binding(
        request: Request,
        experiment_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                return confirm_creative_experiment_binding(
                    conn,
                    experiment_id=experiment_id,
                    ad_id=str(body.get('ad_id') or ''),
                    creative_id=str(body.get('creative_id') or ''),
                    adset_id=str(body.get('adset_id') or ''),
                    campaign_id=str(body.get('campaign_id') or ''),
                    confirmed_by=str(user.get('username') or user.get('display_name') or ''),
                    notes=str(body.get('notes') or body.get('reason') or ''),
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post('/api/ops/ad-data-dashboard/creative-experiments/{experiment_id}/reject-binding')
    def ops_ad_data_dashboard_reject_creative_experiment_binding(
        request: Request,
        experiment_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                return reject_creative_experiment_binding(
                    conn,
                    experiment_id=experiment_id,
                    rejected_by=str(user.get('username') or user.get('display_name') or ''),
                    reason=str(body.get('reason') or ''),
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post('/api/ops/ad-data-dashboard/creative-experiments/{experiment_id}/upload-final-asset')
    def ops_ad_data_dashboard_upload_final_creative_asset(
        request: Request,
        experiment_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                return upload_final_creative_asset_hash(
                    conn,
                    experiment_id=experiment_id,
                    generated_image_id=str(body.get('generated_image_id') or body.get('image_id') or ''),
                    final_delivery_hash=str(body.get('final_delivery_hash') or body.get('image_hash') or ''),
                    uploaded_by=str(user.get('username') or user.get('display_name') or ''),
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get('/api/ops/creative-pro-actions/openapi.json')
    def ops_creative_pro_actions_openapi(request: Request) -> Dict[str, Any]:
        schema_path = Path(__file__).resolve().parents[1] / 'docs' / 'chatgpt_pro_creative_actions.openapi.json'
        try:
            schema = json.loads(schema_path.read_text(encoding='utf-8'))
        except Exception as exc:
            raise HTTPException(status_code=500, detail='creative_actions_schema_unavailable') from exc
        configured_base = str(os.getenv('OPS_PUBLIC_BASE_URL') or '').strip().rstrip('/')
        forwarded_host = str(request.headers.get('x-forwarded-host') or '').strip()
        forwarded_proto = str(request.headers.get('x-forwarded-proto') or '').split(',', 1)[0].strip()
        host = forwarded_host or str(request.headers.get('host') or '').strip()
        proto = forwarded_proto or str(request.url.scheme or '').strip() or 'https'
        derived_base = f'{proto}://{host}'.rstrip('/') if host else ''
        public_base = configured_base or derived_base
        if public_base and not any(local in public_base for local in ('127.0.0.1', 'localhost', '0.0.0.0')):
            schema['servers'] = [{
                'url': public_base,
                'description': 'Public HTTPS ops host for ChatGPT Pro GPT Actions.',
            }]
        return schema

    @app.get('/api/ops/creative-pro-jobs')
    def ops_creative_pro_jobs_list(
        request: Request,
        status: str = '',
        target_app: str = 'all',
        limit: int = 20,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        with db.connect() as conn:
            return {
                'ok': True,
                'provider_mode': PROVIDER_CHATGPT_PRO_MANUAL,
                'target_app': _normalize_ad_dashboard_target_app(target_app) or 'all',
                'jobs': list_chatgpt_pro_jobs(conn, status=status, limit=limit, target_app=target_app),
            }

    @app.get('/api/ops/creative-pro-jobs/next')
    def ops_creative_pro_jobs_next(
        request: Request,
        claim: bool = True,
        claimed_by: str = 'chatgpt_pro',
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        with db.connect() as conn:
            job = claim_next_chatgpt_pro_job(conn, claimed_by=claimed_by, claim=claim)
        return {
            'ok': True,
            'provider_mode': PROVIDER_CHATGPT_PRO_MANUAL,
            'job': job,
            'external_write_performed': False,
        }

    @app.get('/api/ops/creative-pro-jobs/{job_id}')
    def ops_creative_pro_jobs_detail(request: Request, job_id: str) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            with db.connect() as conn:
                return {'ok': True, 'job': get_chatgpt_pro_job(conn, job_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get('/api/ops/creative-pro-jobs/{job_id}/action-context')
    def ops_creative_pro_jobs_action_context(request: Request, job_id: str) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            with db.connect() as conn:
                job = get_chatgpt_pro_job(conn, job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            'ok': True,
            'provider_mode': PROVIDER_CHATGPT_PRO_MANUAL,
            'job_id': job.get('job_id'),
            'status': job.get('status'),
            'country': job.get('country'),
            'brand_display_name': job.get('brand_display_name'),
            'experiment_code': job.get('experiment_code'),
            'generation_plan': job.get('generation_plan') or {},
            'rules': job.get('rules') or {},
            'analysis': job.get('analysis') or {},
            'manual_image': job.get('manual_image') or None,
            'external_write_performed': False,
        }

    @app.post('/api/ops/creative-pro-jobs/{job_id}/analysis')
    def ops_creative_pro_jobs_analysis(
        request: Request,
        job_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            body = dict(payload or {})
            analysis = body.get('analysis') if isinstance(body.get('analysis'), dict) else body
            with db.connect() as conn:
                job = update_chatgpt_pro_job_analysis(conn, job_id, dict(analysis or {}))
            return {'ok': True, 'job': job, 'external_write_performed': False}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/ops/creative-pro-jobs/{job_id}/generation-plan')
    def ops_creative_pro_jobs_generation_plan(
        request: Request,
        job_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            body = dict(payload or {})
            plan = body.get('generation_plan') if isinstance(body.get('generation_plan'), dict) else body
            with db.connect() as conn:
                job = update_chatgpt_pro_job_generation_plan(conn, job_id, dict(plan or {}))
            _creative_pro_broadcast('creative_job_plan_updated', {
                'job_id': job.get('job_id'),
                'status': job.get('status'),
            })
            return {
                'ok': bool((job.get('generation_plan') or {}).get('validation_ok')),
                'job': job,
                'external_write_performed': False,
            }
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/ops/creative-pro-jobs/{job_id}/start-generation')
    def ops_creative_pro_jobs_start_generation(
        request: Request,
        job_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                result = start_hermes_image2_generation_task(
                    conn,
                    job_id=job_id,
                    image_size=str(body.get('image_size') or '1024x1024'),
                    candidate_count=int(body.get('candidate_count') or 1),
                    max_attempts=int(body.get('max_attempts') or 3),
                    created_by=str(user.get('username') or user.get('display_name') or ''),
                    force_regenerate=bool(body.get('force_regenerate')),
                )
            result['hermes_notify'] = _notify_hermes_image2_agent(result)
            _creative_pro_broadcast('creative_task_queued', {
                'job_id': (result.get('job') or {}).get('job_id'),
                'task_id': (result.get('task') or {}).get('task_id'),
                'status': (result.get('task') or {}).get('status'),
            })
            return result
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if detail == 'creative_pro_job_not_found' else 400
            raise HTTPException(status_code=status_code, detail=detail) from exc

    @app.get('/api/ops/creative-pro-jobs/{job_id}/generation-status')
    def ops_creative_pro_jobs_generation_status(request: Request, job_id: str) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            with db.connect() as conn:
                return get_creative_pro_generation_status(conn, job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get('/api/ops/creative-generation-tasks/next')
    def ops_creative_generation_tasks_next(
        request: Request,
        claim: bool = False,
        lease_owner: str = 'hermes_image2_agent',
        lease_seconds: int = 900,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        normalized_owner = str(lease_owner or 'hermes_image2_agent')
        normalized_seconds = int(lease_seconds or 900)
        if claim and db_writer_enabled():
            result = submit_sqlite_write_job({
                'type': 'creative_image2_claim_next',
                'lease_owner': normalized_owner,
                'lease_seconds': normalized_seconds,
            }, timeout=20.0)
            task = result.get('task')
        else:
            with db.connect() as conn:
                task = next_hermes_image2_generation_task(
                    conn,
                    claim=claim,
                    lease_owner=normalized_owner,
                    lease_seconds=normalized_seconds,
                )
        return {'ok': True, 'provider_mode': PROVIDER_HERMES_IMAGE2_AGENT, 'task': task, 'external_write_performed': False}

    @app.post('/api/ops/creative-generation-tasks/claim-next')
    def ops_creative_generation_tasks_claim_next(
        request: Request,
        lease_owner: str = 'hermes_image2_agent',
        lease_seconds: int = 900,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        with db.connect() as conn:
            task = next_hermes_image2_generation_task(
                conn,
                claim=True,
                lease_owner=lease_owner,
                lease_seconds=lease_seconds,
            )
        return {
            'ok': True,
            'provider_mode': PROVIDER_HERMES_IMAGE2_AGENT,
            'task': task,
            'external_write_performed': bool(task),
        }

    @app.get('/api/ops/creative-generation-tasks')
    def ops_creative_generation_tasks_list(
        request: Request,
        job_id: str = '',
        status: str = '',
        limit: int = 50,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        with db.connect() as conn:
            tasks = list_hermes_image2_generation_tasks(
                conn,
                job_id=str(job_id or '').strip(),
                status=str(status or '').strip(),
                limit=int(limit or 50),
            )
        return {
            'ok': True,
            'provider_mode': PROVIDER_HERMES_IMAGE2_AGENT,
            'tasks': tasks,
            'external_write_performed': False,
        }

    @app.post('/api/ops/creative-generation-tasks/{task_id}/claim')
    def ops_creative_generation_tasks_claim(
        request: Request,
        task_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            normalized_owner = str(body.get('lease_owner') or 'hermes_image2_agent')
            normalized_seconds = int(body.get('lease_seconds') or 900)
            if db_writer_enabled():
                result = submit_sqlite_write_job({
                    'type': 'creative_image2_claim',
                    'task_id': task_id,
                    'lease_owner': normalized_owner,
                    'lease_seconds': normalized_seconds,
                }, timeout=20.0)
                task = result.get('task')
            else:
                with db.connect() as conn:
                    task = claim_hermes_image2_generation_task(
                        conn,
                        task_id,
                        lease_owner=normalized_owner,
                        lease_seconds=normalized_seconds,
                    )
            return {'ok': True, 'provider_mode': PROVIDER_HERMES_IMAGE2_AGENT, 'task': task, 'external_write_performed': False}
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if detail == 'creative_generation_task_not_found' else 400
            raise HTTPException(status_code=status_code, detail=detail) from exc

    @app.post('/api/ops/creative-generation-tasks/{task_id}/heartbeat')
    def ops_creative_generation_tasks_heartbeat(
        request: Request,
        task_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                task = heartbeat_hermes_image2_generation_task(
                    conn,
                    task_id,
                    lease_owner=str(body.get('lease_owner') or 'hermes_image2_agent'),
                    lease_seconds=int(body.get('lease_seconds') or 900),
                    provider_response=body.get('provider_response') if isinstance(body.get('provider_response'), dict) else {},
                )
            return {'ok': True, 'provider_mode': PROVIDER_HERMES_IMAGE2_AGENT, 'task': task, 'external_write_performed': False}
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if detail == 'creative_generation_task_not_found' else 400
            raise HTTPException(status_code=status_code, detail=detail) from exc

    @app.get('/api/ops/creative-generation-tasks/{task_id}/status')
    def ops_creative_generation_tasks_status(request: Request, task_id: str) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            with db.connect() as conn:
                return {'ok': True, 'task': get_creative_generation_task(conn, task_id), 'external_write_performed': False}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/ops/creative-generation-tasks/{task_id}/fail')
    def ops_creative_generation_tasks_fail(
        request: Request,
        task_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                task = fail_hermes_image2_generation_task(
                    conn,
                    task_id,
                    error_code=str(body.get('error_code') or 'hermes_image2_generation_failed'),
                    error_message=str(body.get('error_message') or ''),
                    retryable=body.get('retryable') is not False,
                    provider_response=body.get('provider_response') if isinstance(body.get('provider_response'), dict) else {},
                    expected_attempt_count=body.get('expected_attempt_count'),
                    expected_lease_owner=str(body.get('expected_lease_owner') or ''),
                )
            _creative_pro_broadcast('creative_task_failed', {
                'job_id': task.get('job_id'),
                'task_id': task.get('task_id'),
                'status': task.get('status'),
                'error_code': task.get('error_code'),
            })
            return {'ok': True, 'provider_mode': PROVIDER_HERMES_IMAGE2_AGENT, 'task': task, 'external_write_performed': False}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/ops/creative-generation-tasks/{task_id}/cancel')
    def ops_creative_generation_tasks_cancel(
        request: Request,
        task_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                task = cancel_hermes_image2_generation_task(
                    conn,
                    task_id,
                    cancelled_by=str(user.get('username') or user.get('display_name') or ''),
                    reason=str(body.get('reason') or 'cancelled_by_operator'),
                )
            _creative_pro_broadcast('creative_task_cancelled', {
                'job_id': task.get('job_id'),
                'task_id': task.get('task_id'),
                'status': task.get('status'),
            })
            return {'ok': True, 'provider_mode': PROVIDER_HERMES_IMAGE2_AGENT, 'task': task, 'external_write_performed': False}
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if detail == 'creative_generation_task_not_found' else 400
            raise HTTPException(status_code=status_code, detail=detail) from exc

    @app.post('/api/ops/creative-generation-tasks/{task_id}/retry')
    def ops_creative_generation_tasks_retry(
        request: Request,
        task_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        try:
            with db.connect() as conn:
                task = retry_hermes_image2_generation_task(
                    conn,
                    task_id,
                    retry_by=str(user.get('username') or user.get('display_name') or ''),
                    reset_attempts=body.get('reset_attempts') is not False,
                )
            _creative_pro_broadcast('creative_task_retried', {
                'job_id': task.get('job_id'),
                'task_id': task.get('task_id'),
                'status': task.get('status'),
            })
            return {'ok': True, 'provider_mode': PROVIDER_HERMES_IMAGE2_AGENT, 'task': task, 'external_write_performed': False}
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if detail == 'creative_generation_task_not_found' else 400
            raise HTTPException(status_code=status_code, detail=detail) from exc

    @app.post('/api/ops/creative-generation-tasks/{task_id}/upload-image')
    async def ops_creative_generation_tasks_upload_image(
        request: Request,
        task_id: str,
        file: UploadFile = File(...),
        provider_session_id: str = Form(default=''),
        candidate_index: int = Form(default=0),
        quality_evaluation_json: str = Form(default=''),
        source_image_used: bool = Form(default=False),
        source_image_hash: str = Form(default=''),
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        content = await file.read()
        try:
            quality_evaluation = json.loads(quality_evaluation_json) if str(quality_evaluation_json or '').strip() else {}
            if not isinstance(quality_evaluation, dict):
                quality_evaluation = {}
        except Exception as exc:
            raise HTTPException(status_code=400, detail='invalid_quality_evaluation_json') from exc
        try:
            with db.connect() as conn:
                result = save_hermes_image2_uploaded_image(
                    conn,
                    task_id=task_id,
                    filename=str(file.filename or ''),
                    content=content,
                    content_type=str(file.content_type or ''),
                    provider_session_id=provider_session_id,
                    candidate_index=candidate_index,
                    quality_evaluation=quality_evaluation,
                    source_image_used=source_image_used,
                    source_image_hash=source_image_hash,
                )
                quality = dict(result.get('quality_summary') or {})
                auto_review_ready = creative_image_auto_approval_eligible(quality)
                image_id = str(dict(result.get('image') or {}).get('image_id') or '').strip()
                if auto_review_ready and image_id:
                    existing_review = conn.execute(
                        "SELECT review_status FROM creative_review_records WHERE image_id=? ORDER BY created_at DESC LIMIT 1",
                        (image_id,),
                    ).fetchone()
                    if not existing_review or str(existing_review['review_status'] or '').upper() != 'APPROVED':
                        review = create_review_record(
                            conn,
                            image_id=image_id,
                            review_status='APPROVED',
                            reviewer='ai-creative-review-v1',
                            checks={
                                'file_quality': True,
                                'ocr_text_risk': True,
                                'currency_reward': True,
                                'direction_fit': True,
                                'public_positioning_fit': True,
                                'l3_visual_review': True,
                            },
                            decision_reason='strict_machine_checks_and_l3_visual_review_passed',
                        )
                        linked_job_id = str(review.get('job_id') or review.get('completed_job_id') or '').strip()
                        if linked_job_id:
                            job_row = conn.execute(
                                'SELECT experiment_id,material_refs_json FROM creative_pro_work_queue WHERE job_id=?',
                                (linked_job_id,),
                            ).fetchone()
                            refs = json.loads(job_row['material_refs_json'] or '{}') if job_row else {}
                            growth_experiment_id = str(refs.get('growth_experiment_id') or (job_row['experiment_id'] if job_row else '') or '').strip()
                            if growth_experiment_id:
                                AdExperimentService(conn).record_creative_review(
                                    growth_experiment_id,
                                    'APPROVED',
                                    actor='ai-creative-review-v1',
                                    image_id=image_id,
                                    job_id=linked_job_id,
                                    image_hash=str(review.get('image_hash') or ''),
                                )
                        result['auto_review'] = {
                            'status': 'APPROVED',
                            'reviewer': 'ai-creative-review-v1',
                            'reason': 'strict_machine_checks_and_l3_visual_review_passed',
                        }
                    else:
                        result['auto_review'] = {'status': 'APPROVED', 'replayed': True}
                elif image_id:
                    result['auto_review'] = {
                        'status': 'NEEDS_HUMAN_REVIEW',
                        'reason': 'strict_auto_review_evidence_incomplete',
                    }
            _creative_pro_broadcast('creative_image_uploaded', {
                'job_id': (result.get('job') or {}).get('job_id'),
                'task_id': (result.get('task') or {}).get('task_id') or task_id,
                'image_id': (result.get('image') or {}).get('image_id'),
                'status': (result.get('task') or {}).get('status'),
            })
            return result
        except InvalidCreativeImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/ops/creative-pro-jobs/{job_id}/mark-completed')
    def ops_creative_pro_jobs_mark_completed(request: Request, job_id: str) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            with db.connect() as conn:
                job = mark_chatgpt_pro_job_completed(conn, job_id)
            _creative_pro_broadcast('creative_job_completed', {
                'job_id': job.get('job_id'),
                'status': job.get('status'),
            })
            return {'ok': True, 'job': job, 'external_write_performed': False}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/ops/creative-pro-jobs/{job_id}/delete')
    def ops_creative_pro_jobs_delete(request: Request, job_id: str) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        try:
            with db.connect() as conn:
                job = delete_chatgpt_pro_job(conn, job_id)
            _creative_pro_broadcast('creative_job_deleted', {
                'job_id': job.get('job_id'),
                'status': job.get('status'),
            })
            return {'ok': True, 'job': job, 'external_write_performed': False}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def _decode_creative_pro_image_payload(
        *,
        content_type: str = '',
        image_url: str = '',
        data_url: str = '',
        image_base64: str = '',
    ) -> Tuple[bytes, str]:
        content_type = str(content_type or '').strip().lower()
        image_url = str(image_url or '').strip()
        data_url = str(data_url or '').strip()
        image_base64 = str(image_base64 or '').strip()
        content = b''
        if data_url:
            if not data_url.startswith('data:image/'):
                raise HTTPException(status_code=400, detail='image_data_url_must_be_image')
            header, _, encoded = data_url.partition(',')
            content_type = content_type or header.split(';', 1)[0].replace('data:', '').strip().lower()
            image_base64 = encoded.strip()
        if image_base64:
            if image_base64.lower().startswith('data:image/'):
                header, _, encoded = image_base64.partition(',')
                content_type = content_type or header.split(';', 1)[0].replace('data:', '').strip().lower()
                image_base64 = encoded
            normalized_base64 = re.sub(r'\s+', '', image_base64)
            if len(normalized_base64) % 4:
                normalized_base64 += '=' * (4 - (len(normalized_base64) % 4))
            try:
                content = base64.b64decode(normalized_base64, validate=True)
            except Exception as exc:
                try:
                    content = base64.b64decode(normalized_base64, altchars=b'-_', validate=True)
                except Exception:
                    raise HTTPException(status_code=400, detail='invalid_image_base64') from exc
        elif image_url:
            if image_url.startswith('/mnt/data/') or image_url.startswith('file:') or image_url.startswith('/'):
                raise HTTPException(status_code=400, detail='local_image_path_must_be_converted_to_image_data_url_or_image_base64')
            parsed = urlparse(image_url)
            if parsed.scheme != 'https':
                raise HTTPException(status_code=400, detail='image_url_must_be_https')
            try:
                response = requests.get(image_url, timeout=30)
            except Exception as exc:
                raise HTTPException(status_code=400, detail='image_url_fetch_failed') from exc
            if response.status_code >= 400:
                raise HTTPException(status_code=400, detail='image_url_fetch_failed')
            content = response.content or b''
            content_type = content_type or str(response.headers.get('content-type') or '').split(';', 1)[0].strip().lower()
        else:
            raise HTTPException(status_code=400, detail='image_url_or_base64_required')
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail='image_too_large')
        if content_type and content_type not in {'image/png', 'image/jpeg', 'image/jpg', 'image/webp'}:
            raise HTTPException(status_code=400, detail='unsupported_image_type')
        return content, content_type

    def _normalize_creative_pro_filename(filename: str, content_type: str) -> str:
        filename = str(filename or '').strip() or 'generated-image.png'
        if not Path(filename).suffix:
            suffix = {
                'image/jpeg': '.jpg',
                'image/jpg': '.jpg',
                'image/webp': '.webp',
            }.get(str(content_type or '').strip().lower(), '.png')
            filename = f'{filename}{suffix}'
        return filename

    def _creative_pro_chunk_dir(job_id: str, upload_id: str) -> Path:
        clean_job_id = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(job_id or ''))[:160]
        clean_upload_id = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(upload_id or ''))[:120]
        if not clean_job_id or not clean_upload_id or clean_upload_id != str(upload_id or ''):
            raise HTTPException(status_code=400, detail='invalid_upload_id')
        return Path(__file__).resolve().parents[1] / 'data' / 'ad_creative_generated_images' / '.chunks' / clean_job_id / clean_upload_id

    @app.post('/api/ops/creative-pro-jobs/{job_id}/manual-image-upload')
    async def ops_creative_pro_jobs_manual_image_upload(
        request: Request,
        job_id: str,
        file: UploadFile = File(...),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        content = await file.read()
        try:
            with db.connect() as conn:
                result = save_chatgpt_pro_uploaded_image(
                    conn,
                    job_id=job_id,
                    filename=str(file.filename or ''),
                    content=content,
                    uploaded_by=str(user.get('username') or user.get('display_name') or ''),
                )
            _creative_pro_broadcast('creative_image_uploaded', {
                'job_id': (result.get('job') or {}).get('job_id') or job_id,
                'image_id': (result.get('image') or {}).get('image_id'),
                'status': (result.get('job') or {}).get('status'),
            })
            return result
        except InvalidCreativeImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/ops/creative-pro-jobs/{job_id}/generated-image')
    def ops_creative_pro_jobs_generated_image(
        request: Request,
        job_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        filename = str(body.get('filename') or f'{job_id}.png').strip()
        content_type = str(body.get('content_type') or '').strip().lower()
        content, content_type = _decode_creative_pro_image_payload(
            content_type=content_type,
            image_url=str(body.get('image_url') or '').strip(),
            data_url=str(body.get('image_data_url') or '').strip(),
            image_base64=str(body.get('image_base64') or '').strip(),
        )
        filename = _normalize_creative_pro_filename(filename, content_type)
        try:
            with db.connect() as conn:
                result = save_chatgpt_pro_uploaded_image(
                    conn,
                    job_id=job_id,
                    filename=filename,
                    content=content,
                    uploaded_by=str(user.get('username') or user.get('display_name') or 'chatgpt_pro_action'),
                )
            _creative_pro_broadcast('creative_image_uploaded', {
                'job_id': (result.get('job') or {}).get('job_id') or job_id,
                'image_id': (result.get('image') or {}).get('image_id'),
                'status': (result.get('job') or {}).get('status'),
            })
            return result
        except InvalidCreativeImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/ops/creative-pro-jobs/{job_id}/generated-image-chunk')
    def ops_creative_pro_jobs_generated_image_chunk(
        request: Request,
        job_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        body = dict(payload or {})
        upload_id = str(body.get('upload_id') or '').strip()
        try:
            chunk_index = int(body.get('chunk_index'))
            total_chunks = int(body.get('total_chunks'))
        except Exception as exc:
            raise HTTPException(status_code=400, detail='invalid_chunk_index') from exc
        if chunk_index < 0 or total_chunks < 1 or chunk_index >= total_chunks or total_chunks > 500:
            raise HTTPException(status_code=400, detail='invalid_chunk_index')
        chunk = str(body.get('image_base64_chunk') or body.get('chunk') or '')
        if not chunk:
            raise HTTPException(status_code=400, detail='image_chunk_required')
        if len(chunk) > 200_000:
            raise HTTPException(status_code=400, detail='image_chunk_too_large')
        filename = _normalize_creative_pro_filename(str(body.get('filename') or f'{job_id}.png').strip(), str(body.get('content_type') or '').strip().lower())
        content_type = str(body.get('content_type') or '').strip().lower()
        chunk_dir = _creative_pro_chunk_dir(job_id, upload_id)
        chunk_dir.mkdir(parents=True, exist_ok=True)
        (chunk_dir / 'meta.json').write_text(json.dumps({
            'job_id': job_id,
            'upload_id': upload_id,
            'filename': filename,
            'content_type': content_type,
            'total_chunks': total_chunks,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, sort_keys=True), encoding='utf-8')
        (chunk_dir / f'{chunk_index:04d}.txt').write_text(chunk, encoding='ascii')
        commit = bool(body.get('commit') or body.get('is_final') or chunk_index == total_chunks - 1)
        if not commit:
            return {
                'ok': True,
                'upload_id': upload_id,
                'chunk_index': chunk_index,
                'total_chunks': total_chunks,
                'committed': False,
            }
        missing = [idx for idx in range(total_chunks) if not (chunk_dir / f'{idx:04d}.txt').exists()]
        if missing:
            raise HTTPException(status_code=400, detail=f'image_chunks_missing:{",".join(str(i) for i in missing[:20])}')
        image_base64 = ''.join((chunk_dir / f'{idx:04d}.txt').read_text(encoding='ascii') for idx in range(total_chunks))
        content, content_type = _decode_creative_pro_image_payload(
            content_type=content_type,
            image_base64=image_base64,
        )
        filename = _normalize_creative_pro_filename(filename, content_type)
        try:
            with db.connect() as conn:
                result = save_chatgpt_pro_uploaded_image(
                    conn,
                    job_id=job_id,
                    filename=filename,
                    content=content,
                    uploaded_by=str(user.get('username') or user.get('display_name') or 'chatgpt_pro_action'),
                )
            shutil.rmtree(chunk_dir, ignore_errors=True)
            result['upload_id'] = upload_id
            result['committed'] = True
            _creative_pro_broadcast('creative_image_uploaded', {
                'job_id': (result.get('job') or {}).get('job_id') or job_id,
                'image_id': (result.get('image') or {}).get('image_id'),
                'status': (result.get('job') or {}).get('status'),
                'upload_id': upload_id,
            })
            return result
        except InvalidCreativeImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get('/api/ops/ad-data-dashboard/creative-images/{image_id}')
    def ops_ad_data_dashboard_creative_image_asset(request: Request, image_id: str) -> Response:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        with db.connect() as conn:
            image = conn.execute(
                """
                SELECT image_id, image_ref, thumbnail_ref, market, brand, creative_direction,
                       created_at, metadata_json
                FROM creative_generated_images
                WHERE image_id = ?
                  AND COALESCE(LOWER(review_status), '') != 'deleted'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM creative_pro_work_queue q
                      WHERE q.job_id = json_extract(creative_generated_images.metadata_json, '$.job_id')
                        AND q.status = 'deleted'
                  )
                LIMIT 1
                """,
                (image_id,),
            ).fetchone()
            image = dict(image) if image else None
            if image:
                image['metadata'] = json.loads(image.pop('metadata_json') or '{}')
                enrich_creative_image_names(conn, [image])
        if not image:
            raise HTTPException(status_code=404, detail='creative_image_not_found')
        download_requested = str(request.query_params.get('download') or '').strip().lower() in {'1', 'true', 'yes'}
        selected_ref = image.get('image_ref') if download_requested else (image.get('thumbnail_ref') or image.get('image_ref'))
        image_path = Path(str(selected_ref or '')).resolve()
        allowed_root = Path(__file__).resolve().parents[1] / 'data' / 'ad_creative_generated_images'
        try:
            image_path.relative_to(allowed_root.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail='creative_image_path_blocked')
        if not image_path.exists():
            raise HTTPException(status_code=404, detail='creative_image_file_not_found')
        media_type = {
            '.svg': 'image/svg+xml',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.webp': 'image/webp',
        }.get(image_path.suffix.lower(), 'application/octet-stream')
        headers = {'Cache-Control': 'private, max-age=86400, immutable'}
        if download_requested:
            safe_name = creative_image_download_filename(image, image_path.suffix.lower() or '.png')
            ascii_fallback = f"creative-image{image_path.suffix.lower() or '.png'}"
            headers['Content-Disposition'] = f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quote(safe_name)}'
        return FileResponse(path=str(image_path), media_type=media_type, headers=headers)

    @app.get('/api/ops/ad-data-dashboard/recommendations/history')
    def ops_ad_data_dashboard_recommendations_history(
        request: Request,
        limit: int = 100,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        with db.connect() as conn:
            return recommendation_history_payload(conn, limit=limit)

    @app.get('/api/ops/ad-data-dashboard/recommendations/{recommendation_id}/review')
    def ops_ad_data_dashboard_recommendation_review(
        recommendation_id: str,
        request: Request,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        with db.connect() as conn:
            payload = recommendation_review_payload(conn, recommendation_id)
        if payload.get('detail') == 'recommendation_not_found':
            raise HTTPException(status_code=404, detail='recommendation_not_found')
        return payload

    def _tiktok_oauth_callback_response(kind: str, request: Request) -> HTMLResponse:
        code = str(request.query_params.get('code') or '').strip()
        state = str(request.query_params.get('state') or '').strip()
        error = str(request.query_params.get('error') or request.query_params.get('error_code') or '').strip()
        detail = str(request.query_params.get('message') or request.query_params.get('error_description') or '').strip()
        ok = bool(code and not error)
        title = 'TikTok 授权回调已收到' if ok else 'TikTok 授权回调异常'
        status_text = '已收到授权 code。请回到广告数据看板继续完成 token 接入。' if ok else '没有收到有效授权 code，请回到 TikTok 后台重新授权。'
        error_html = f'<p>错误：<code>{html.escape(error)}</code> {html.escape(detail)}</p>' if error else ''
        page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f6f7f9;color:#101828;margin:0;padding:32px}}.card{{max-width:720px;margin:60px auto;background:#fff;border:1px solid #e3e8ef;border-radius:12px;padding:24px;box-shadow:0 12px 30px rgba(16,24,40,.08)}}h1{{font-size:24px;margin:0 0 12px}}p{{line-height:1.7;color:#475467}}code{{background:#f2f4f7;border-radius:6px;padding:2px 6px}}</style></head>
<body><main class="card"><h1>{html.escape(title)}</h1><p>{html.escape(status_text)}</p><p>类型：<code>{html.escape(kind)}</code></p><p>state：<code>{html.escape(state or '-')}</code></p>{error_html}</main></body></html>"""
        return HTMLResponse(
            page,
            status_code=200 if ok else 400,
            headers={'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0'},
        )

    @app.get('/api/ops/ad-data-dashboard/tiktok/oauth/callback', response_class=HTMLResponse)
    def ops_ad_data_dashboard_tiktok_oauth_callback(request: Request) -> HTMLResponse:
        return _tiktok_oauth_callback_response('advertiser', request)

    @app.get('/api/ops/ad-data-dashboard/tiktok/account-holder/oauth/callback', response_class=HTMLResponse)
    def ops_ad_data_dashboard_tiktok_account_holder_oauth_callback(request: Request) -> HTMLResponse:
        return _tiktok_oauth_callback_response('account_holder', request)

    @app.get('/ops/accounts', response_class=HTMLResponse)
    def ops_accounts_page(request: Request) -> str:
        user = _require_ops_user(request, role='ops_user')
        role = normalize_ops_role(user.get('role'))
        return _with_ops_shell_style(
            _ops_accounts_page_html(role),
            role,
            page='accounts',
        )

    @app.get('/api/ops/accounts')
    def ops_accounts_list(request: Request) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        return {'rows': auth_manager.list_users()}

    @app.post('/api/ops/accounts')
    def ops_accounts_create(request: Request, payload: OpsAccountCreateRequest) -> Dict[str, Any]:
        current_user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        if payload.role == OPS_AUTH_ROLE_SUPER_ADMIN and str(current_user.get('role') or '').strip() != OPS_AUTH_ROLE_SUPER_ADMIN:
            raise HTTPException(status_code=403, detail='ops_super_admin_required')
        try:
            user = auth_manager.create_user(
                username=payload.username,
                password=payload.password,
                role=payload.role,
                display_name=payload.display_name,
                enabled=payload.enabled,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _ops_hot_read_cache_invalidate('approval_accounts:options')
        return {'ok': True, 'user': user}

    @app.put('/api/ops/accounts/{user_id}')
    def ops_accounts_update(request: Request, user_id: str, payload: OpsAccountUpdateRequest) -> Dict[str, Any]:
        current_user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        target_user = auth_manager.get_user_by_id(user_id)
        if target_user is None:
            raise HTTPException(status_code=404, detail='user_not_found')
        current_role = str(current_user.get('role') or '').strip()
        target_role = str(target_user.get('role') or '').strip()
        if payload.password is not None and target_role == OPS_AUTH_ROLE_SUPER_ADMIN and current_role != OPS_AUTH_ROLE_SUPER_ADMIN:
            raise HTTPException(status_code=403, detail='super_admin_password_protected')
        if payload.role == OPS_AUTH_ROLE_SUPER_ADMIN and current_role != OPS_AUTH_ROLE_SUPER_ADMIN:
            raise HTTPException(status_code=403, detail='ops_super_admin_required')
        try:
            user = auth_manager.update_user(
                user_id,
                role=payload.role,
                display_name=payload.display_name,
                enabled=payload.enabled,
                password=payload.password,
            )
        except ValueError as exc:
            status_code = 404 if str(exc) == 'user_not_found' else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        _ops_hot_read_cache_invalidate('approval_accounts:options')
        return {'ok': True, 'user': user}

    @app.delete('/api/ops/accounts/{user_id}')
    def ops_accounts_delete(request: Request, user_id: str) -> Dict[str, Any]:
        current_user = _require_ops_user(request, role=OPS_AUTH_ROLE_SUPER_ADMIN)
        normalized_user_id = str(user_id or '').strip()
        if normalized_user_id == str(current_user.get('user_id') or '').strip():
            raise HTTPException(status_code=400, detail='cannot_delete_self')
        try:
            deleted = auth_manager.delete_user(normalized_user_id)
        except ValueError as exc:
            status_code = 404 if str(exc) == 'user_not_found' else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        _ops_hot_read_cache_invalidate('approval_accounts:options')
        return {'ok': True, 'deleted': bool(deleted)}

    @app.get('/api/external/newcomers/daily')
    def external_newcomer_daily(
        request: Request,
        platform: str,
        business_date: str,
        revision: int = 0,
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        _require_newcomer_external_feed(request)
        return _external_response_or_raise(
            lambda: service.list_newcomer_daily_publication(
                platform=platform,
                business_date=business_date,
                revision=revision,
                limit=limit,
                offset=offset,
            )
        )

    @app.get('/api/external/fan-conversions/daily')
    def external_fan_conversions_daily(
        request: Request,
        updated_since: str = '',
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        _require_newcomer_external_feed(request)
        return _external_response_or_raise(
            lambda: service.list_external_fan_conversions(
                updated_since=updated_since,
                limit=limit,
                offset=offset,
            )
        )

    @app.get('/api/external/timo/v1/countries')
    def external_timo_countries(request: Request) -> Dict[str, Any]:
        _require_timo_external_feed(request)
        return service.list_timo_external_countries()

    @app.get('/api/external/timo/v1/streamers')
    def external_timo_streamers(
        request: Request,
        country: str = '',
        guild_name: str = '',
        guild_id: str = '',
        guild_sid: str = '',
        updated_since: str = '',
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        _require_timo_external_feed(request)
        return service.list_timo_external_streamers(
            country=country,
            guild_name=guild_name,
            guild_id=guild_id,
            guild_sid=guild_sid,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        )

    @app.get('/api/external/timo/v1/revenue-daily')
    def external_timo_revenue_daily(
        request: Request,
        stat_date: str = '',
        country: str = '',
        guild_name: str = '',
        guild_id: str = '',
        guild_sid: str = '',
        updated_since: str = '',
        include_provisional: bool = True,
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        _require_timo_external_feed(request)
        if 'date' in request.query_params:
            raise HTTPException(status_code=400, detail={'ok': False, 'reason': 'unsupported_query_param_date_use_stat_date'})
        return service.list_timo_external_revenue_daily(
            stat_date_bj=stat_date,
            country=country,
            guild_name=guild_name,
            guild_id=guild_id,
            guild_sid=guild_sid,
            updated_since=updated_since,
            include_provisional=include_provisional,
            limit=limit,
            offset=offset,
        )

    @app.get('/api/external/timo/v1/live-revenue-aggregate')
    def external_timo_live_revenue_aggregate(
        request: Request,
        stat_date: str = '',
        country: str = '',
        guild_name: str = '',
        guild_id: str = '',
        guild_sid: str = '',
    ) -> Dict[str, Any]:
        _require_timo_external_feed(request)
        return service.list_timo_live_revenue_aggregate(
            stat_date_bj=stat_date,
            country=country,
            guild_name=guild_name,
            guild_id=guild_id,
            guild_sid=guild_sid,
        )

    @app.get('/api/external/timo/v1/live-revenue-aggregate-runs')
    def external_timo_live_revenue_aggregate_runs(
        request: Request,
        status: str = 'all',
        data_date: str = '',
        country: str = '',
        guild_name: str = '',
        guild_id: str = '',
        guild_sid: str = '',
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        _require_timo_external_feed(request)
        return service.list_timo_live_revenue_aggregate_runs(
            status=status,
            data_date_bj=data_date,
            country=country,
            guild_name=guild_name,
            guild_id=guild_id,
            guild_sid=guild_sid,
            limit=limit,
            offset=offset,
        )

    @app.get('/api/external/timo/v1/guild-tasks')
    def external_timo_guild_tasks(
        request: Request,
        country: str = '',
        guild_name: str = '',
        guild_id: str = '',
        guild_sid: str = '',
        snapshot_since: str = '',
        include_history: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        _require_timo_external_feed(request)
        return service.list_timo_external_guild_tasks(
            country=country,
            guild_name=guild_name,
            guild_id=guild_id,
            guild_sid=guild_sid,
            snapshot_since=snapshot_since,
            include_history=include_history,
            limit=limit,
            offset=offset,
        )

    @app.get('/api/external/timo/v1/sync-runs')
    def external_timo_sync_runs(
        request: Request,
        status: str = 'success',
        data_date: str = '',
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        _require_timo_external_feed(request)
        return service.list_timo_external_sync_runs(status=status, data_date_bj=data_date, limit=limit, offset=offset)

    @app.get('/api/external/timo/v1/materialization-runs')
    def external_timo_materialization_runs(
        request: Request,
        status: str = 'all',
        data_date: str = '',
        guild_name: str = '',
        guild_id: str = '',
        guild_sid: str = '',
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        _require_timo_external_feed(request)
        return service.list_timo_incremental_sync_runs(
            status=status,
            data_date_bj=data_date,
            guild_name=guild_name,
            guild_id=guild_id,
            guild_sid=guild_sid,
            limit=limit,
            offset=offset,
        )

    @app.get('/api/external/timo/v1/bi-revenue-daily')
    def external_timo_bi_revenue_daily(
        request: Request,
        stat_date: str = '',
        country: str = '',
        guild_name: str = '',
        guild_id: str = '',
        guild_sid: str = '',
        updated_since: str = '',
        include_provisional: bool = True,
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        _require_timo_external_feed(request)
        return service.list_timo_bi_revenue_daily(
            stat_date_bj=stat_date,
            country=country,
            guild_name=guild_name,
            guild_id=guild_id,
            guild_sid=guild_sid,
            updated_since=updated_since,
            include_provisional=include_provisional,
            limit=limit,
            offset=offset,
        )

    @app.post('/api/external/app-intake/submissions')
    def external_app_intake_submit(request: Request, payload: ExternalAppIntakeSubmissionRequest) -> Dict[str, Any]:
        source_config = _require_external_app_source(request, payload.source)
        return _external_response_or_raise(lambda: service.submit_external_app_intake(payload=payload, source_config=source_config))

    @app.post('/api/external/app-intake/phone-backfill')
    def external_app_intake_phone_backfill(request: Request, payload: ExternalAppPhoneBackfillRequest) -> Dict[str, Any]:
        source_config = _require_external_app_source(request, payload.source)
        audit_request_id = service.record_external_app_phone_backfill_request(payload=payload, source_config=source_config)
        try:
            response = service.backfill_external_app_phone(payload=payload, source_config=source_config)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {'reason': str(exc.detail or ''), 'message': str(exc.detail or '')}
            service.update_external_app_phone_backfill_request(
                request_id=audit_request_id,
                status='failed',
                result_code=str(detail.get('reason') or exc.status_code or '').strip(),
                result_reason=str(detail.get('message') or detail.get('reason') or exc.detail or '').strip(),
                result_snapshot={'http_status': exc.status_code, 'detail': detail},
            )
            raise _external_app_http_exception(exc) from exc
        except Exception as exc:
            service.update_external_app_phone_backfill_request(
                request_id=audit_request_id,
                status='error',
                result_code=type(exc).__name__,
                result_reason=str(exc),
                result_snapshot={'error_type': type(exc).__name__, 'error': str(exc)},
            )
            raise
        service.update_external_app_phone_backfill_request(
            request_id=audit_request_id,
            status='succeeded',
            result_code=str(response.get('phone_backfill_status') or response.get('result_code') or 'backfilled'),
            result_reason=str(response.get('message') or '手机号已回补'),
            submission_id=str(response.get('submission_id') or response.get('item_id') or ''),
            result_snapshot={'response': response},
        )
        return response

    @app.get('/api/external/app-intake/submissions/{submission_id}')
    def external_app_intake_submission(request: Request, submission_id: str) -> Dict[str, Any]:
        source_config = _require_external_app_source(request)
        product_app = str(request.query_params.get('app') or request.headers.get('X-App') or '').strip()
        return _external_response_or_raise(lambda: service.get_external_app_intake_submission(source=str(source_config.get('source') or ''), submission_id=submission_id, app_name=product_app))

    @app.get('/api/external/app-intake/users/{external_user_id}/latest')
    def external_app_intake_latest(request: Request, external_user_id: str) -> Dict[str, Any]:
        source_config = _require_external_app_source(request)
        product_app = str(request.query_params.get('app') or request.headers.get('X-App') or '').strip()
        return _external_response_or_raise(lambda: service.get_external_app_latest_submission(source=str(source_config.get('source') or ''), external_user_id=external_user_id, app_name=product_app))

    @app.post('/api/external/app-intake/submissions/{submission_id}/template-copied')
    def external_app_intake_template_copied(request: Request, submission_id: str, payload: ExternalAppIntakeFeedbackActionRequest) -> Dict[str, Any]:
        source_config = _require_external_app_source(request)
        product_app = str(request.query_params.get('app') or request.headers.get('X-App') or '').strip()
        return _external_response_or_raise(lambda: service.mark_external_app_template_copied(
            source=str(source_config.get('source') or ''),
            item_id=submission_id,
            customer_service_id=payload.customer_service_id,
            customer_service_name=payload.customer_service_name,
            app_name=product_app,
        ))

    @app.post('/api/external/app-intake/submissions/{submission_id}/feedback-done')
    def external_app_intake_feedback_done(request: Request, submission_id: str, payload: ExternalAppIntakeFeedbackActionRequest) -> Dict[str, Any]:
        source_config = _require_external_app_source(request)
        product_app = str(request.query_params.get('app') or request.headers.get('X-App') or '').strip()
        return _external_response_or_raise(lambda: service.mark_external_app_feedback_done(
            source=str(source_config.get('source') or ''),
            item_id=submission_id,
            customer_service_id=payload.customer_service_id,
            customer_service_name=payload.customer_service_name,
            app_name=product_app,
        ))

    @app.get('/ops/intake-submit', response_class=HTMLResponse)
    def ops_intake_submit_page(request: Request) -> HTMLResponse:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        html = OPS_INTAKE_SUBMIT_PAGE_HTML.replace('__OPS_USER_ROLE__', str(user.get('role') or '').strip())
        return HTMLResponse(
            _with_ops_shell_style(html, str(user.get('role') or '').strip(), page='intake-submit'),
            headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma': 'no-cache',
                'Expires': '0',
            },
        )

    @app.get('/ops/timo-membership-query', response_class=HTMLResponse)
    def ops_timo_membership_query_page(request: Request) -> HTMLResponse:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return HTMLResponse(
            _with_ops_shell_style(
                TIMO_MEMBERSHIP_QUERY_PAGE_HTML,
                str(user.get('role') or '').strip(),
                page='timo-membership-query',
            ),
            headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma': 'no-cache',
                'Expires': '0',
            },
        )

    @app.get('/ops/timo-intake', response_class=HTMLResponse)
    def ops_timo_intake_page(request: Request) -> HTMLResponse:
        _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return RedirectResponse(url='/ops/intake-submit?app=timo', status_code=303)

    @app.get('/ops/timo-intake-history', response_class=HTMLResponse)
    def ops_timo_intake_history_page(request: Request) -> HTMLResponse:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        html = _with_ops_shell_style(OPS_TIMO_INTAKE_PAGE_HTML, str(user.get('role') or '').strip(), page='timo-intake-history')
        return HTMLResponse(
            html,
            headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma': 'no-cache',
                'Expires': '0',
            },
        )

    @app.get('/ops/bind-failed-users', response_class=HTMLResponse)
    def ops_bind_failed_users_page(request: Request) -> HTMLResponse:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        html = _with_ops_shell_style(OPS_BIND_FAILED_USERS_PAGE_HTML, str(user.get('role') or '').strip(), page='bind-failed-users')
        return HTMLResponse(
            html,
            headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma': 'no-cache',
                'Expires': '0',
            },
        )

    @app.post('/api/ops/intake-submit')
    def ops_intake_submit(request: Request, payload: OpsIntakeSubmitRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        display_name = str(user.get('username') or user.get('display_name') or user.get('user_id') or 'ops_user').strip()
        return service.submit_ops_intake_text(text=payload.text, profile_name=payload.profile_name, submitted_by=display_name)

    @app.get('/api/ops/timo-intake/guilds')
    def ops_timo_intake_guilds(request: Request) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        cache_key = f'timo_intake_guilds:{_ops_hot_cache_user_suffix(user)}'
        return _ops_hot_read_cache_get_or_set(
            cache_key,
            20.0,
            lambda: service.list_timo_intake_guilds(user=user),
        )

    @app.post('/api/ops/timo-intake/guilds/{guild_name}/parse')
    def ops_timo_intake_parse(request: Request, guild_name: str, payload: OpsIntakeParseRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        if not service._ops_intake_user_can_access_guild(user, guild_name):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        return service.parse_timo_intake_text(text=payload.text, fields=payload.fields)

    @app.post('/api/ops/timo-intake/guilds/{guild_name}/ocr-image')
    async def ops_timo_intake_ocr_image(request: Request, guild_name: str, file: UploadFile = File(...)) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        if not service._ops_intake_user_can_access_guild(user, guild_name):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        ocr_result = await _extract_ops_upload_ocr_text(file, too_large_detail='timo_ocr_image_too_large')
        raw_text = str(ocr_result.get('raw_text') or '').strip()
        parsed = service.parse_timo_intake_text(text=raw_text, fields={})
        parsed_fields = parsed.get('fields') or {}
        normalized = ocr_result.get('normalized') or {}
        timo_id = str(
            parsed_fields.get('timo_id')
            or normalized.get('timo_id')
            or normalized.get('account_id')
            or normalized.get('profile_id')
            or normalized.get('sid')
            or ''
        ).strip()
        return {
            **ocr_result,
            'fields': {'timo_id': timo_id} if timo_id else {},
            'validation': {'timo_id': bool(timo_id)},
            'errors': [] if timo_id else ['missing_timo_id'],
            'can_submit': False,
        }

    @app.get('/api/ops/sogo-intake/guilds')
    @app.get('/api/ops/sugo-intake/guilds')
    def ops_sogo_intake_guilds(request: Request) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return service.list_sogo_intake_guilds(user=user)

    @app.post('/api/ops/sogo-intake/guilds/{guild_name}/parse')
    @app.post('/api/ops/sugo-intake/guilds/{guild_name}/parse')
    def ops_sogo_intake_parse(request: Request, guild_name: str, payload: OpsIntakeParseRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        if not service._ops_intake_user_can_access_guild(user, guild_name):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        return service.parse_sogo_intake_text(text=payload.text, fields=payload.fields)

    @app.post('/api/ops/sogo-intake/verify')
    @app.post('/api/ops/sugo-intake/verify')
    def ops_sogo_intake_verify(request: Request, payload: OpsSugoIntakeVerifyRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return service.verify_sogo_intake_member(guild_name=payload.guild_name, sogo_id=payload.sogo_id, user=user)

    @app.get('/api/ops/timo-intake/items')
    def ops_timo_intake_items(
        request: Request,
        page: int = 1,
        page_size: int = 30,
        status: Optional[str] = None,
        q: Optional[str] = None,
        guild_name: Optional[str] = None,
        date: Optional[str] = None,
        submitted_by: Optional[str] = None,
        view: str = 'all',
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        user_scope = str(user.get('user_id') or user.get('username') or user.get('role') or '')
        cache_key = '|'.join((
            'timo_intake_items', user_scope, str(page), str(page_size), str(status or ''),
            str(q or ''), str(guild_name or ''), str(date or ''), str(submitted_by or ''), str(view or 'all'),
        ))
        return _ops_hot_read_cache_get_or_set(
            cache_key,
            10.0,
            lambda: service.list_timo_intake_items(
                page=page, page_size=page_size, status=status, q=q, guild_name=guild_name,
                date=date, submitted_by=submitted_by, user=user, view=view,
            ),
            stale_ttl_seconds=30.0,
        )

    @app.post('/api/ops/timo-intake/guilds/{guild_name}/clear-stale-feedback')
    def ops_timo_intake_clear_stale_feedback(request: Request, guild_name: str) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return service.clear_timo_intake_stale_feedback_items(guild_name=guild_name, user=user)

    @app.get('/api/ops/timo-intake/guilds/{guild_name}/yesterday-id-export')
    def ops_timo_intake_yesterday_id_export(request: Request, guild_name: str, date: Optional[str] = None):
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        content = service.export_timo_yesterday_ids_xlsx(guild_name=guild_name, user=user, date_bj=date)
        filename = service.timo_yesterday_ids_export_filename(guild_name=guild_name, date_bj=date)
        headers = {'Content-Disposition': f'attachment; filename="{filename}"'}
        return StreamingResponse(iter([content]), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers=headers)

    @app.get('/api/ops/timo-intake/guilds/{guild_name}/revenue-export')
    def ops_timo_intake_revenue_export(
        request: Request,
        guild_name: str,
        period: str = 'yesterday',
        export_type: Optional[str] = None,
        date: Optional[str] = None,
    ):
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        content, filename = service.export_timo_guild_revenue_xlsx(
            guild_name=guild_name,
            user=user,
            period=period,
            export_type=export_type,
            date_bj=date,
        )
        headers = {'Content-Disposition': f'attachment; filename="{filename}"'}
        return StreamingResponse(iter([content]), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers=headers)

    @app.get('/api/ops/timo-intake/guilds/{guild_name}/real-person-id-export')
    def ops_timo_intake_real_person_id_export(request: Request, guild_name: str, as_of_date: Optional[str] = None):
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        content, filename = service.export_timo_real_person_ids_xlsx(guild_name=guild_name, user=user, as_of_date_bj=as_of_date)
        headers = {'Content-Disposition': f'attachment; filename="{filename}"'}
        return StreamingResponse(iter([content]), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers=headers)

    @app.get('/api/ops/timo-intake/guilds/{guild_name}/first-20k-diamond-export')
    def ops_timo_intake_first_20k_diamond_export(request: Request, guild_name: str, as_of_date: Optional[str] = None):
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        content, filename = service.export_timo_first_20k_diamonds_xlsx(guild_name=guild_name, user=user, as_of_date_bj=as_of_date)
        headers = {'Content-Disposition': f'attachment; filename="{filename}"'}
        return StreamingResponse(iter([content]), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers=headers)

    @app.post('/api/ops/timo-intake/items')
    def ops_timo_intake_submit(request: Request, payload: OpsTimoIntakeSubmitRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        result = service.submit_timo_intake_item(payload=payload, user=user)
        _ops_hot_read_cache_invalidate('timo_intake_items|')
        return result

    @app.post('/api/ops/timo-intake/items/{item_id}/verify')
    def ops_timo_intake_verify(request: Request, item_id: str, payload: OpsTimoIntakeVerifyRequest = Body(default_factory=OpsTimoIntakeVerifyRequest)) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        item = service._get_timo_intake_item(item_id)
        if not service._ops_timo_intake_user_can_access_item(user, item):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        result = service.verify_timo_intake_item(item_id=item_id, force_crm_sync=payload.force_crm_sync)
        _ops_hot_read_cache_invalidate('timo_intake_items|')
        return result

    @app.post('/api/ops/timo-intake/items/{item_id}/feedback-done')
    def ops_timo_intake_feedback_done(request: Request, item_id: str) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        item = service._get_timo_intake_item(item_id)
        if not service._ops_timo_intake_user_can_access_item(user, item):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        result = service.mark_external_app_feedback_done(
            source=str(item.get('source') or 'ops_timo_intake'),
            item_id=item_id,
            customer_service_id=str(user.get('user_id') or user.get('username') or ''),
            customer_service_name=str(user.get('display_name') or user.get('username') or ''),
            app_name='timo',
        )
        _ops_hot_read_cache_invalidate('timo_intake_items|')
        return result

    @app.post('/api/ops/timo-intake/items/{item_id}/clear')
    def ops_timo_intake_clear_item(request: Request, item_id: str) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        result = service.clear_timo_intake_item_card(item_id=item_id, user=user)
        _ops_hot_read_cache_invalidate('timo_intake_items|')
        return result

    @app.get('/api/ops/intake-workbench/guilds')
    def ops_intake_workbench_guilds(request: Request) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return service.list_ops_intake_guilds(user=user)

    @app.post('/api/ops/intake-workbench/guilds/refresh-health')
    def ops_intake_workbench_refresh_guild_health(request: Request, payload: OpsIntakeGuildHealthRefreshRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return service.refresh_ops_intake_guild_health(user=user, guild_names=payload.guild_names, only_if_unknown_or_stale=payload.only_if_unknown_or_stale)

    @app.get('/api/ops/intake-workbench/filter-guilds')
    def ops_intake_workbench_filter_guilds(request: Request) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return service.list_ops_intake_filter_guilds(user=user)

    @app.post('/api/ops/intake-workbench/guilds/{guild_name}/assignees')
    def ops_intake_workbench_guild_assignees(request: Request, guild_name: str, payload: OpsIntakeGuildAssigneesRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        result = service.set_ops_intake_guild_assignees(guild_name=guild_name, user_ids=payload.user_ids, assigned_by=str(user.get('username') or user.get('user_id') or 'admin'))
        _ops_hot_read_cache_invalidate('timo_intake_guilds:')
        _ops_hot_read_cache_invalidate('timo_guild_executors:')
        return result

    @app.post('/api/ops/intake-workbench/guilds/{guild_name}/parse')
    def ops_intake_workbench_parse(request: Request, guild_name: str, payload: OpsIntakeParseRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        if not service._ops_intake_user_can_access_guild(user, guild_name):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        return service.parse_ops_intake_text(guild_name=guild_name, text=payload.text, fields=payload.fields)

    @app.post('/api/ops/intake-workbench/guilds/{guild_name}/ocr-image')
    async def ops_intake_workbench_ocr_image(request: Request, guild_name: str, file: UploadFile = File(...)) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        if not service._ops_intake_user_can_access_guild(user, guild_name):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        ocr_result = await _extract_ops_upload_ocr_text(file, too_large_detail='ops_intake_ocr_image_too_large')
        raw_text = str(ocr_result.get('raw_text') or '').strip()
        parsed = service.parse_ops_intake_text(guild_name=guild_name, text=raw_text, fields={})
        parsed_fields = parsed.get('fields') or {}
        normalized = ocr_result.get('normalized') or {}
        account_id = str(
            parsed_fields.get('account_id')
            or normalized.get('account_id')
            or normalized.get('profile_id')
            or normalized.get('sid')
            or normalized.get('timo_id')
            or ''
        ).strip()
        return {
            **ocr_result,
            'guild_name': guild_name,
            'fields': {'account_id': account_id} if account_id else {},
            'validation': {'account_id': bool(account_id)},
            'errors': [] if account_id else ['missing_account_id'],
            'can_submit': False,
            'code_required': False,
        }

    @app.post('/api/ops/intake-workbench/guilds/{guild_name}/submit')
    def ops_intake_workbench_submit_guild_item(request: Request, guild_name: str, payload: OpsIntakeParseRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        result = service.submit_ops_intake_guild_item(guild_name=guild_name, text=payload.text, fields=payload.fields, user=user)
        _ops_hot_read_cache_invalidate('ops_intake_items:')
        return result

    @app.post('/api/ops/intake-workbench/guilds/{guild_name}/clear-stale-feedback')
    def ops_intake_workbench_clear_stale_feedback(request: Request, guild_name: str) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        result = service.clear_ops_intake_stale_feedback_items(guild_name=guild_name, user=user)
        _ops_hot_read_cache_invalidate('ops_intake_items:')
        return result

    @app.get('/api/ops/intake-workbench/items')
    def ops_intake_workbench_items(request: Request, guild_name: Optional[str] = None, limit: int = 100, include_done: bool = False) -> JSONResponse:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        cache_key = ':'.join([
            'ops_intake_items',
            _ops_hot_cache_user_suffix(user),
            str(guild_name or '').strip(),
            str(max(1, min(int(limit or 100), 1000))),
            'include_done' if include_done else 'active',
        ])
        payload = _ops_hot_read_cache_get_or_set(
            cache_key,
            20.0,
            lambda: service.list_ops_intake_items(guild_name=guild_name, user=user, limit=limit, include_done=include_done),
        )
        return JSONResponse(
            payload,
            headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma': 'no-cache',
                'Expires': '0',
            },
        )

    @app.get('/api/ops/intake-workbench/binding-history-items')
    def ops_intake_workbench_binding_history_items(
        request: Request,
        limit: int = 100,
        offset: int = 0,
        guild_name: Optional[str] = None,
        date: Optional[str] = None,
        submitted_by: Optional[str] = None,
        view: str = 'all',
        q: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return service.list_ops_intake_binding_history_items(
            user=user,
            limit=limit,
            offset=offset,
            guild_name=guild_name,
            date=date,
            submitted_by=submitted_by,
            view=view,
            q=q,
            status=status,
        )

    @app.get('/api/ops/intake-workbench/bind-failed-items')
    def ops_intake_workbench_bind_failed_items(
        request: Request,
        limit: int = 100,
        guild_name: Optional[str] = None,
        date: Optional[str] = None,
        submitted_by: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return service.list_ops_intake_bind_failed_items(
            user=user,
            limit=limit,
            guild_name=guild_name,
            date=date,
            submitted_by=submitted_by,
        )

    @app.post('/api/ops/intake-workbench/bind-failed-items/clear')
    def ops_intake_workbench_clear_bind_failed_items(request: Request, payload: OpsBindFailedClearRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        return service.clear_ops_intake_bind_failed_items(
            user=user,
            guild_name=payload.guild_name,
            date=payload.date,
            submitted_by=payload.submitted_by,
            item_ids=payload.item_ids,
            limit=payload.limit,
        )

    @app.patch('/api/ops/intake-workbench/items/{item_id}/fields')
    def ops_intake_workbench_update_item_fields(request: Request, item_id: str, payload: OpsIntakeParseRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        result = service.update_ops_intake_item_fields(item_id=item_id, fields=payload.fields, user=user)
        _ops_hot_read_cache_invalidate('ops_intake_items:')
        return result

    @app.post('/api/ops/intake-workbench/items/{item_id}/recheck-cms')
    def ops_intake_workbench_recheck_cms_item(request: Request, item_id: str, payload: OpsIntakeParseRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        result = service.recheck_ops_intake_bind_failed_item(item_id=item_id, fields=payload.fields, user=user)
        _ops_hot_read_cache_invalidate('ops_intake_items:')
        return result

    @app.post('/api/ops/intake-workbench/items/{item_id}/verify-current-truth')
    def ops_intake_workbench_verify_current_truth(request: Request, item_id: str, payload: OpsIntakeParseRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        result = service.create_verify_binding_current_truth_task(item_id=item_id, fields=payload.fields, user=user)
        _ops_hot_read_cache_invalidate('ops_intake_items:')
        return result

    @app.get('/api/ops/operation-tasks/{task_id}')
    def ops_operation_task_status(request: Request, task_id: str) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return {'ok': True, 'task': service.get_operation_task(task_id)}

    @app.post('/api/ops/intake-workbench/items/{item_id}/resubmit')
    def ops_intake_workbench_resubmit_item(request: Request, item_id: str, payload: OpsIntakeParseRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        result = service.resubmit_ops_intake_item(item_id=item_id, text=payload.text, fields=payload.fields, user=user)
        _ops_hot_read_cache_invalidate('ops_intake_items:')
        return result

    @app.post('/api/ops/intake-workbench/items/{item_id}/resolve')
    def ops_intake_workbench_resolve_item(request: Request, item_id: str, payload: OpsIntakeResolveRequest) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        result = service.resolve_ops_intake_history_item(item_id=item_id, action=payload.action, reason=payload.reason, note=payload.note, user=user)
        _ops_hot_read_cache_invalidate('ops_intake_items:')
        return result

    @app.post('/api/ops/intake-workbench/items/{item_id}/clear')
    def ops_intake_workbench_clear_item(request: Request, item_id: str) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        result = service.clear_ops_intake_item_card(item_id=item_id, user=user)
        _ops_hot_read_cache_invalidate('ops_intake_items:')
        return result

    @app.post('/api/ops/intake-workbench/items/{item_id}/template-copied')
    def ops_intake_workbench_template_copied(request: Request, item_id: str) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        result = service.mark_ops_intake_template_copied(item_id=item_id, user=user)
        _ops_hot_read_cache_invalidate('ops_intake_items:')
        return result

    @app.post('/api/ops/intake-workbench/items/{item_id}/feedback-done')
    def ops_intake_workbench_feedback_done(request: Request, item_id: str, payload: OpsIntakeFeedbackDoneRequest = Body(default_factory=OpsIntakeFeedbackDoneRequest)) -> Dict[str, Any]:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        result = service.mark_ops_intake_feedback_done(item_id=item_id, user=user, force=payload.force, reason=payload.reason)
        _ops_hot_read_cache_invalidate('ops_intake_items:')
        return result

    @app.get('/api/ops/client-version')
    def ops_client_version(response: Response) -> Dict[str, Any]:
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        return _ops_runtime_version_state()

    @app.get('/api/ops/sqlite-observability')
    def ops_sqlite_observability(request: Request, response: Response) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        return sqlite_observability_snapshot()

    @app.get('/ops/intake-bot-presets', response_class=HTMLResponse)
    def intake_bot_presets_page(request: Request) -> str:
        _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return RedirectResponse(url='/ops/intake-submit', status_code=303)

    @app.get('/ops/production-ops', response_class=HTMLResponse)
    def production_ops_page(request: Request) -> str:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return _with_ops_shell_style(PRODUCTION_OPS_PAGE_HTML, str(user.get('role') or '').strip(), page='production-ops')

    @app.get('/ops/group-atmosphere', response_class=HTMLResponse)
    def group_atmosphere_page(request: Request) -> HTMLResponse:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_OPERATOR)
        html = _with_ops_shell_style(_group_atmosphere_page_html(str(user.get('role') or '').strip()), str(user.get('role') or '').strip(), page='group-atmosphere')
        return HTMLResponse(
            content=html,
            headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma': 'no-cache',
            },
        )

    @app.get('/ops/registration-group-approval-batch-members', response_class=HTMLResponse)
    def registration_group_approval_batch_members_page(request: Request) -> str:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return _with_ops_shell_style(
            _registration_group_approval_batch_members_page_html(),
            str(user.get('role') or '').strip(),
            page='registration-group-approval-batch-members',
        )

    @app.get('/api/ops/group-atmosphere/page-version')
    def group_atmosphere_page_version(request: Request) -> Dict[str, str]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_OPERATOR)
        return {'version': GROUP_ATMOSPHERE_PAGE_VERSION}

    def _official_group_bridge_base_url() -> str:
        bridge_url = str(official_group_approval_webhook_url or '').strip()
        if not bridge_url:
            raise HTTPException(status_code=404, detail='official_group_bridge_not_configured')
        return bridge_url.replace('/official-group/approve', '').rstrip('/')

    def _official_group_bridge_public_ops_base(request: Request) -> str:
        forwarded_proto = str(request.headers.get('x-forwarded-proto') or '').split(',')[0].strip()
        scheme = forwarded_proto or request.url.scheme
        forwarded_host = str(request.headers.get('x-forwarded-host') or '').split(',')[0].strip()
        host = forwarded_host or str(request.headers.get('host') or request.url.netloc)
        return f'{scheme}://{host}'

    def _official_group_bridge_auth_headers() -> Dict[str, str]:
        token = str(official_group_bridge_token or '').strip()
        if not token:
            return {}
        return {'Authorization': f'Bearer {token}'}

    def _bridge_get_json(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f'{_official_group_bridge_base_url()}{path}'
        headers = _official_group_bridge_auth_headers()
        if headers:
            response = requests.get(url, params=params or {}, headers=headers, timeout=15.0)
        else:
            response = requests.get(url, params=params or {}, timeout=15.0)
        response.raise_for_status()
        return response.json()

    def _bridge_post_json(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f'{_official_group_bridge_base_url()}{path}'
        headers = _official_group_bridge_auth_headers()
        if headers:
            response = requests.post(url, json=payload or {}, headers=headers, timeout=20.0)
        else:
            response = requests.post(url, json=payload or {}, timeout=20.0)
        response.raise_for_status()
        return response.json()

    def _official_group_bridge_fallback_page_html() -> str:
        return """<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><title>官方群审批桥接台</title></head>
<body><div class=\"page\"><div class=\"nav\"><a href=\"/ops\">管理员看板</a><a href=\"/ops/production-ops\">群审批控制台</a><a href=\"/ops/accounts\">账号设置</a></div><div class=\"card hero\"><h1>官方群审批桥接台</h1></div></div></body></html>"""

    @app.get('/ops/official-group-bridge', response_class=HTMLResponse)
    def official_group_bridge_page(request: Request) -> str:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        headers = _official_group_bridge_auth_headers()
        html = _official_group_bridge_fallback_page_html()
        if headers:
            try:
                response = requests.get(
                    f'{_official_group_bridge_base_url()}/ops/official-group-bridge',
                    headers=headers,
                    timeout=15.0,
                    allow_redirects=False,
                )
                if response.status_code < 400:
                    html = response.text
            except requests.RequestException:
                html = _official_group_bridge_fallback_page_html()
        public_base = _official_group_bridge_public_ops_base(request).rstrip('/')
        # The bridge service is internal-only on 127.0.0.1:55801. Render it through the main ops origin
        # so browser refresh/navigation stays on the public 7819 backend and uses the same shell sizing.
        html = html.replace('http://127.0.0.1:8011', public_base)
        html = html.replace('http://127.0.0.1:55801', public_base)
        if '官方群审批桥接台' not in html:
            html = _official_group_bridge_fallback_page_html()
        return _with_ops_shell_style(html, str(user.get('role') or '').strip())

    @app.get('/ops/official-group-bridge/health')
    def official_group_bridge_health_proxy(request: Request) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return _bridge_get_json('/ops/official-group-bridge/health')

    @app.get('/ops/official-group-bridge/summary')
    def official_group_bridge_summary_proxy(request: Request) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return _bridge_get_json('/ops/official-group-bridge/summary')

    @app.get('/ops/official-group-bridge/requests')
    def official_group_bridge_requests_proxy(
        request: Request,
        status: Optional[str] = None,
        target_group: Optional[str] = None,
        lead_id: Optional[str] = None,
        request_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = 'updated_at',
        sort_order: str = 'desc',
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return _bridge_get_json('/ops/official-group-bridge/requests', {
            'status': status,
            'target_group': target_group,
            'lead_id': lead_id,
            'request_id': request_id,
            'limit': limit,
            'offset': offset,
            'sort_by': sort_by,
            'sort_order': sort_order,
        })

    @app.get('/ops/official-group-bridge/requests/{request_id}')
    def official_group_bridge_request_detail_proxy(request: Request, request_id: str) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return _bridge_get_json(f'/ops/official-group-bridge/requests/{quote(str(request_id), safe="")}')

    @app.post('/ops/official-group-bridge/requests/{request_id}/resolve')
    def official_group_bridge_request_resolve_proxy(request: Request, request_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return _bridge_post_json(f'/ops/official-group-bridge/requests/{quote(str(request_id), safe="")}/resolve', payload)

    @app.get('/api/ops/runtime-health')
    def ops_runtime_health() -> Dict[str, Any]:
        return service.runtime_health()

    @app.get('/api/ops/runtime-health/summary')
    def ops_runtime_health_summary() -> Dict[str, Any]:
        payload = service.runtime_health() or {}
        crm = payload.get('crm') or {}
        lark = payload.get('lark') or {}
        simulation = payload.get('simulation') or {}
        registration_group_approval = payload.get('registration_group_approval') or {}
        official_group_approval = payload.get('official_group_approval') or {}
        ingress = payload.get('ingress') or {}
        return {
            'crm': {
                'enabled': crm.get('enabled'),
                'status': crm.get('status'),
                'token_ready': crm.get('token_ready'),
            },
            'lark': {
                'default_app': lark.get('default_app'),
                'default_guild': lark.get('default_guild'),
                'current_app_id': lark.get('current_app_id'),
            },
            'simulation': {
                'mode': simulation.get('mode'),
                'auto_bind_simulation': simulation.get('auto_bind_simulation'),
            },
            'registration_group_approval': {
                'configured': registration_group_approval.get('configured'),
                'provider': registration_group_approval.get('provider'),
                'status': registration_group_approval.get('status'),
                'ready': registration_group_approval.get('ready'),
                'authenticated': registration_group_approval.get('authenticated'),
            },
            'official_group_approval': {
                'configured': official_group_approval.get('configured'),
                'provider': official_group_approval.get('provider'),
                'status': official_group_approval.get('status'),
                'ready': official_group_approval.get('ready'),
                'authenticated': official_group_approval.get('authenticated'),
            },
            'ingress': {
                'async_default': ingress.get('async_default'),
                'worker_enabled': ingress.get('worker_enabled'),
                'worker_count': ingress.get('worker_count'),
                'worker_alive': ingress.get('worker_alive'),
                'active_worker_threads': ingress.get('active_worker_threads'),
                'queued_jobs': ingress.get('queued_jobs'),
                'processing_jobs': ingress.get('processing_jobs'),
                'pending_bind_tasks': ingress.get('pending_bind_tasks'),
                'processing_bind_tasks': ingress.get('processing_bind_tasks'),
                'pending_bind_human_action_count': ingress.get('pending_bind_human_action_count'),
            },
        }

    @app.get('/api/ops/registration-group-approval-executor-health')
    def ops_registration_group_approval_executor_health() -> Dict[str, Any]:
        return service.registration_group_approval_executor_health()

    @app.get('/api/ops/group-approvals/executor/health')
    def ops_group_approval_executor_health(approval_scope: str) -> Dict[str, Any]:
        return service.group_approval_executor_health(approval_scope)

    @app.post('/api/ops/registration-group-approval-executor-warmup')
    def ops_registration_group_approval_executor_warmup() -> Dict[str, Any]:
        return service.registration_group_approval_executor_warmup()

    @app.post('/api/ops/group-approvals/executor/warmup')
    def ops_group_approval_executor_warmup(payload: GroupApprovalExecutorWarmupRequest) -> Dict[str, Any]:
        return service.group_approval_executor_warmup(payload.approval_scope)

    @app.get('/api/ops/registration-group-approval-executor-group-state')
    def ops_registration_group_approval_executor_group_state(registration_group: str) -> Dict[str, Any]:
        return service.registration_group_approval_executor_group_state(registration_group)

    @app.get('/api/ops/group-approvals/executor/target-state')
    def ops_group_approval_executor_target_state(approval_scope: str, target_group: str) -> Dict[str, Any]:
        return service.group_approval_executor_target_state(approval_scope, target_group)

    @app.get('/api/ops/group-approvals/executor/group-metadata')
    def ops_group_approval_executor_group_metadata(approval_scope: str, target_group: str) -> Dict[str, Any]:
        return service.group_approval_executor_group_metadata(approval_scope, target_group)

    @app.get('/api/ops/group-approvals/executor/member-lookup')
    def ops_group_approval_executor_member_lookup(
        approval_scope: str,
        target_group: str,
        requester_id: Optional[str] = None,
        phone_hint: Optional[str] = None,
        name_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        return service.group_approval_executor_member_lookup(
            approval_scope,
            target_group,
            requester_id=requester_id,
            phone_hint=phone_hint,
            name_hint=name_hint,
        )

    @app.get('/api/ops/ingress-queue')
    def ops_ingress_queue() -> Dict[str, Any]:
        return service.list_ingress_queue()

    @app.post('/api/ops/ingress-queue/run-next')
    def ops_ingress_queue_run_next() -> Dict[str, Any]:
        processed = service.process_next_worker_tick()
        if not processed:
            return {'processed': False}
        if 'status' in processed:
            return processed
        if 'task_id' in processed:
            normalized = dict(processed)
            normalized.setdefault('status', 'success')
            return normalized
        return processed

    @app.get('/api/ops/operator-audit-log')
    def ops_operator_audit_log(limit: int = 200) -> Dict[str, Any]:
        return service.operator_audit_log(limit=limit)

    @app.post('/api/leads/upsert')
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
    def customer_sync(payload: CustomerSyncRequest):
        return service.customer_sync(payload)

    @app.post("/api/account-submissions")
    def account_submissions(payload: AccountSubmissionRequest):
        return service.submit_account(payload)

    @app.post("/api/intake/manual-cs-submissions")
    def manual_cs_submissions(payload: ManualCsSubmissionRequest):
        return service.submit_manual_cs(payload)

    @app.post("/api/intake/lark/events")
    def lark_events(payload: Dict[str, Any] = Body(...)):
        return service.handle_lark_event(payload)

    @app.post("/api/tasks/{task_id}/recognition-result")
    def recognition_result(task_id: str, payload: RecognitionResultRequest):
        return service.recognition_result(task_id, payload)

    @app.post("/api/tasks/{task_id}/native-ocr-run")
    def native_ocr_run(task_id: str):
        return service.run_native_ocr(task_id)

    @app.post("/api/tasks/{task_id}/bind-check-result")
    def bind_check_result(task_id: str, payload: BindCheckResultRequest):
        return service.bind_check_result(task_id, payload)

    @app.post("/api/tasks/{task_id}/group-join-result")
    def group_join_result(task_id: str, payload: GroupJoinResultRequest):
        return service.group_join_result(task_id, payload)

    @app.get("/api/leads/{lead_id}/timeline")
    def lead_timeline(lead_id: str):
        return service.lead_timeline(lead_id)

    @app.post("/api/leads/{lead_id}/voucher-attach")
    def voucher_attach(lead_id: str, payload: VoucherAttachRequest):
        return service.attach_voucher_for_lead(lead_id, payload.image_path, payload.remark_suffix)

    @app.post("/api/registration-groups/approval-batches")
    def registration_group_approval_batches(payload: RegistrationGroupApprovalBatchRequest):
        return service.create_registration_group_approval_batch(payload)

    @app.post("/api/registration-groups/approval-decisions")
    def registration_group_approval_decisions(payload: RegistrationGroupApprovalDecisionRequest):
        return service.registration_group_approval_decision(payload)

    @app.get("/api/registration-groups/approval-decisions/{approval_run_id}")
    def registration_group_approval_decision_status(approval_run_id: str):
        return service.registration_group_approval_decision_status(approval_run_id)

    @app.post("/api/official-groups/approval-checks")
    def official_group_approval_checks(payload: OfficialGroupApprovalCheckRequest):
        return service.official_group_approval_check(payload)

    @app.post("/api/official-groups/approval-decisions")
    def official_group_approval_decisions(payload: OfficialGroupApprovalDecisionRequest):
        return service.official_group_approval_decision(payload)

    @app.post('/api/group-approvals/checks')
    def group_approval_checks(payload: GroupApprovalCheckRequest):
        approval_scope = str(payload.approval_scope or '').strip()
        if approval_scope != 'official_group':
            raise HTTPException(status_code=400, detail='group approval checks currently support official_group only')
        result = service.official_group_approval_check(OfficialGroupApprovalCheckRequest(
            lead_id=str(payload.lead_id or '').strip(),
            target_group=str(payload.target_group or '').strip(),
            checked_at=payload.checked_at,
            checked_by=payload.checked_by,
            checked_by_name=payload.checked_by_name,
            source_platform=payload.source_platform,
            source_campaign=payload.source_campaign,
            source_adset=payload.source_adset,
            source_ad=payload.source_ad,
            target_phone_hint=payload.target_phone_hint,
            target_requester_id=payload.target_requester_id,
            remark=payload.remark,
        ))
        return _with_shared_group_approval_result(result, approval_scope='official_group', target_group=payload.target_group)

    @app.post('/api/group-approvals/decisions')
    def group_approval_decisions(payload: GroupApprovalDecisionRequest):
        approval_scope = str(payload.approval_scope or '').strip()
        if approval_scope == 'registration_group':
            result = service.registration_group_approval_decision(RegistrationGroupApprovalDecisionRequest(
                registration_group=str(payload.registration_group or '').strip(),
                decision=payload.decision,
                decided_at=payload.decided_at,
                decided_by=payload.decided_by,
                decided_by_name=payload.decided_by_name,
                source_platform=payload.source_platform,
                source_campaign=payload.source_campaign,
                source_adset=payload.source_adset,
                source_ad=payload.source_ad,
                target_name_hint=payload.target_name_hint,
                target_phone_hint=payload.target_phone_hint,
                approved_count=payload.approved_count,
                area=payload.area,
                remark=payload.remark,
                force_immediate=payload.force_immediate,
                expected_pending_count=payload.expected_pending_count,
                expected_member_count=payload.expected_member_count,
                expected_requester_ids=payload.expected_requester_ids,
                expected_requesters=payload.expected_requesters,
            ))
            return _with_shared_group_approval_result(result, approval_scope='registration_group', registration_group=payload.registration_group)
        if approval_scope == 'official_group':
            result = service.official_group_approval_decision(OfficialGroupApprovalDecisionRequest(
                lead_id=str(payload.lead_id or '').strip(),
                target_group=str(payload.target_group or '').strip(),
                decision=payload.decision,
                decided_at=payload.decided_at,
                decided_by=payload.decided_by,
                decided_by_name=payload.decided_by_name,
                source_platform=payload.source_platform,
                source_campaign=payload.source_campaign,
                source_adset=payload.source_adset,
                source_ad=payload.source_ad,
                target_name_hint=payload.target_name_hint,
                target_phone_hint=payload.target_phone_hint,
                target_requester_id=payload.target_requester_id,
                remark=payload.remark,
            ))
            return _with_shared_group_approval_result(result, approval_scope='official_group', target_group=payload.target_group)
        raise HTTPException(status_code=400, detail='unsupported approval_scope')

    @app.post('/api/ops/leads/{lead_id}/retry-official-group-approval')
    def ops_retry_official_group_approval(lead_id: str, payload: OfficialGroupApprovalRetryRequest):
        return service.retry_official_group_approval(lead_id, payload)

    @app.get('/api/ops/official-group-approval-executor-health')
    def ops_official_group_approval_executor_health():
        return service.official_group_approval_executor_health()

    @app.get('/api/ops/official-group-approval-summary')
    def ops_official_group_approval_summary():
        return service.official_group_approval_summary()

    @app.get('/api/ops/official-group-approval-summary/summary')
    def ops_official_group_approval_summary_summary():
        payload = service.official_group_approval_summary() or {}
        by_target_group = payload.get('by_target_group') or {}
        return {
            'view_scope': payload.get('view_scope'),
            'pending_count': payload.get('pending_count'),
            'approved_count': payload.get('approved_count'),
            'failed_count': payload.get('failed_count'),
            'skipped_duplicate_count': payload.get('skipped_duplicate_count'),
            'retryable_failed_count': payload.get('retryable_failed_count'),
            'manual_required_count': payload.get('manual_required_count'),
            'target_group_count': len(by_target_group) if isinstance(by_target_group, dict) else 0,
        }

    @app.post('/api/ops/official-group-approval-batches/run-ready')
    def ops_run_ready_official_group_batches(payload: OfficialGroupBatchRunRequest):
        return service.run_ready_official_group_batches(payload)

    @app.post('/api/ops/group-approvals/batches/run-ready')
    def ops_group_approval_batches_run_ready(payload: GroupApprovalBatchRunRequest):
        approval_scope = str(payload.approval_scope or '').strip()
        if approval_scope != 'official_group':
            raise HTTPException(status_code=400, detail='group approval batch run-ready currently supports official_group only')
        result = service.run_ready_official_group_batches(OfficialGroupBatchRunRequest(
            decided_at=payload.decided_at,
            decided_by=payload.decided_by,
            decided_by_name=payload.decided_by_name,
            source_platform=payload.source_platform,
            source_campaign=payload.source_campaign,
            source_adset=payload.source_adset,
            source_ad=payload.source_ad,
            remark=payload.remark,
            limit_groups=payload.limit_groups,
            limit_leads_per_group=payload.limit_leads_per_group,
            allow_live_crm_phone_match=payload.allow_live_crm_phone_match,
            allow_crm_only_test_match=payload.allow_crm_only_test_match,
            suppress_success_notifications=payload.suppress_success_notifications,
        ))
        return _with_shared_group_approval_result(result, approval_scope='official_group')

    @app.get("/api/ops/manual-review-queue")
    def ops_manual_review_queue():
        return service.ops_manual_review_queue()

    @app.post("/api/ops/manual-review/{lead_id}/resolve")
    def ops_manual_review_resolve(lead_id: str, payload: ManualReviewResolveRequest):
        return service.resolve_manual_review(lead_id, payload)

    @app.get("/api/ops/bind-queue")
    def ops_bind_queue():
        return service.ops_bind_queue()

    @app.get("/api/ops/group-queue")
    def ops_group_queue():
        return service.ops_group_queue()

    @app.get("/api/ops/dashboard/summary")
    def ops_dashboard_summary():
        return service.ops_dashboard_summary()

    @app.get('/api/ops/intake-bot-presets')
    def ops_intake_bot_presets():
        return service.list_intake_bot_presets()

    @app.get('/api/ops/intake-bot-presets/resolve')
    def ops_resolve_intake_bot_preset(app_id: Optional[str] = None, profile_name: Optional[str] = None):
        return service.resolve_intake_bot_preset(app_id=app_id, profile_name=profile_name)

    @app.post('/api/ops/intake-bot-presets/{profile_name}')
    def ops_intake_bot_preset_update(profile_name: str, payload: IntakeBotPresetUpdateRequest):
        return service.update_intake_bot_preset(profile_name, payload)

    @app.post('/api/ops/local-intake-bot-gateway/{profile_name}/activate')
    def ops_local_intake_bot_gateway_activate(request: Request, profile_name: str, payload: LocalIntakeBotGatewayActivationRequest):
        client_host = (request.client.host if request.client else '') or ''
        if client_host not in {'127.0.0.1', '::1', 'localhost', 'testclient'}:
            raise HTTPException(status_code=403, detail='local_gateway_activation_only')
        return service.activate_local_intake_bot_gateway(profile_name, payload)

    @app.delete('/api/ops/intake-bot-presets/{profile_name}')
    def ops_intake_bot_preset_delete(profile_name: str):
        return service.delete_intake_bot_preset(profile_name)

    def _timo_live_streamer_history_profile(guild_name: str, streamer_id: str) -> Dict[str, Any]:
        executor_config = service.resolve_timo_guild_executor(guild_name)
        if not executor_config or not executor_config.get('enabled'):
            raise HTTPException(status_code=404, detail='Timo 公会执行器不存在或未启用')
        verifier = service._build_timo_executor_for_item({'guild_name': guild_name})
        result = verifier.verify_host_membership(timo_id=streamer_id)
        if not result.get('ok'):
            raise HTTPException(status_code=502, detail='Timo 平台实时查询失败，请稍后重试')
        if result.get('verified') is not True:
            raise HTTPException(status_code=404, detail='Timo 平台实时查询未找到该主播')
        member = dict(result.get('member') or {})
        registered_at_bj = service._timo_epoch_ms_to_bj_text(member.get('joinTime'))
        if not registered_at_bj:
            raise HTTPException(status_code=502, detail='Timo 平台未返回主播加入时间')
        return {
            'app_name': 'timo',
            'guild_executor_key': service._guild_anchor_executor_key(executor_config),
            'guild_name': str(guild_name or '').strip(),
            'timo_id': streamer_id,
            'canonical_streamer_id': streamer_id,
            'requested_streamer_id': streamer_id,
            'nickname': str(member.get('nickName') or ''),
            'user_uuid': str(member.get('userUuid') or ''),
            'registered_at_bj': registered_at_bj,
            'first_join_date': registered_at_bj[:10],
            'updated_at': utc_now(),
            'lookup_source': 'timo_live',
        }

    def _guild_streamer_history_profile(app_name: str, guild_name: str, streamer_id: str) -> Dict[str, Any]:
        try:
            normalized_app = normalize_history_app(app_name)
            normalized_streamer_id = normalize_streamer_id(streamer_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if normalized_app == 'timo':
            return _timo_live_streamer_history_profile(guild_name, normalized_streamer_id)
        executor = service.resolve_guild_executor(guild_name, app_name='linky')
        if executor and executor.get('enabled') and executor.get('oauth_configured'):
            try:
                profile = fetch_linky_streamer_profile(
                    executor=executor,
                    streamer_id=normalized_streamer_id,
                )
            except Exception as exc:
                raise HTTPException(status_code=502, detail='Linky 平台实时查询失败，请稍后重试') from exc
            if not profile:
                raise HTTPException(status_code=404, detail='Linky 平台实时查询未找到该主播')
            profile['guild_executor_key'] = service._guild_anchor_executor_key(executor)
            return profile
        with service.db.connect() as conn:
            profile = lookup_streamer_first_join(
                conn,
                app_name=normalized_app,
                guild_name=guild_name,
                streamer_id=normalized_streamer_id,
            )
        if not profile:
            raise HTTPException(status_code=404, detail='未找到该主播在目标公会的加入记录')
        profile['lookup_source'] = 'local_cache'
        return profile

    def _timo_live_streamer_revenue_rows(
        *,
        profile: Dict[str, Any],
        date_to: str,
        covered_dates: set[str],
        user: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        missing_dates = uncovered_dates(
            date_from=profile['first_join_date'],
            date_to=date_to,
            covered_dates=covered_dates,
        )
        target_dates = list(missing_dates)
        if profile['first_join_date'] <= date_to and date_to not in target_dates:
            target_dates.append(date_to)
        live_rows: List[Dict[str, Any]] = []
        for stat_date in target_dates:
            try:
                content, _ = service.export_timo_guild_revenue_xlsx(
                    guild_name=profile['guild_name'],
                    user=user,
                    export_type='day',
                    date_bj=stat_date,
                    use_cache=False,
                )
                detail_rows = service._parse_timo_revenue_detail_rows(content)
            except Exception as exc:
                print(json.dumps({
                    'event': 'timo_streamer_history_live_revenue_fallback',
                    'guild_name': profile['guild_name'],
                    'streamer_id': profile['requested_streamer_id'],
                    'stat_date': stat_date,
                    'error': str(getattr(exc, 'detail', exc))[:180],
                }, ensure_ascii=False))
                continue
            matched = next(
                (row for row in detail_rows if str(row.get('timo_id') or '').strip() == profile['requested_streamer_id']),
                None,
            )
            if matched:
                live_rows.append(normalize_timo_revenue_export_row(
                    profile=profile,
                    stat_date=stat_date,
                    row=matched,
                ))
            covered_dates.add(stat_date)
        return live_rows

    @app.get('/api/ops/guild-streamer-history/{app_name}/{guild_name}')
    def ops_guild_streamer_history(
        request: Request,
        app_name: str,
        guild_name: str,
        streamer_id: str,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        profile = _guild_streamer_history_profile(app_name, guild_name, streamer_id)
        return {
            'ok': True,
            'app_name': profile['app_name'],
            'guild_name': profile['guild_name'],
            'guild_display_name': (
                timo_guild_display_name(profile['guild_name'])
                if profile['app_name'] == 'timo'
                else profile['guild_name']
            ),
            'streamer_id': profile['requested_streamer_id'],
            'nickname': profile.get('nickname') or '',
            'first_join_date': profile['first_join_date'],
            'updated_at': profile.get('updated_at') or '',
            'source': profile.get('lookup_source') or 'local_cache',
        }

    @app.get('/api/ops/timo-membership-query/guilds')
    def ops_timo_membership_query_guilds(request: Request) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        rows = service.list_guild_executors(app_name='timo').get('rows', [])
        return {
            'ok': True,
            'rows': [
                {
                    'guild_name': str(row.get('guild_name') or '').strip(),
                    'guild_display_name': timo_guild_display_name(
                        row.get('guild_name'),
                        guild_id=row.get('cms_guild_id'),
                        guild_sid=row.get('cms_guild_sid'),
                    ),
                    'country': str(row.get('country') or '').strip(),
                }
                for row in rows
                if bool(row.get('enabled')) and str(row.get('guild_name') or '').strip()
            ],
        }

    @app.post('/api/ops/timo-membership-query/query')
    def ops_timo_membership_query(
        request: Request,
        payload: TimoMembershipQueryRequest,
    ) -> Dict[str, Any]:
        _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        guild_name = timo_guild_storage_name(payload.guild_name)
        timo_id = ''.join(ch for ch in str(payload.timo_id or '').strip() if ch.isdigit())
        if not guild_name:
            raise HTTPException(status_code=400, detail={'code': 'guild_required', 'message': '请选择目标公会'})
        if not timo_id or not str(payload.timo_id or '').strip().isdigit() or not 6 <= len(timo_id) <= 20:
            raise HTTPException(status_code=400, detail={'code': 'invalid_timo_id', 'message': '请输入正确的 Timo SID'})
        executor_config = service.resolve_timo_guild_executor(guild_name)
        if not executor_config or not bool(executor_config.get('enabled')):
            raise HTTPException(status_code=404, detail={'code': 'guild_unavailable', 'message': '该 Timo 公会当前不可查询'})
        verifier = service._build_timo_executor_for_item({'guild_name': guild_name})
        result = verifier.verify_host_membership(timo_id=timo_id)
        queried_at = utc_now()
        if result.get('ok') is not True:
            result_code = str(result.get('result_code') or '').strip()
            message = '该公会实时查询暂不可用，请稍后重试'
            if result_code in {TIMO_AUTH_EXPIRED_RESULT_CODE, 'timo_ticket_not_configured'}:
                message = '该公会授权正在维护，请稍后重试'
            raise HTTPException(
                status_code=503,
                detail={'code': 'timo_query_unavailable', 'message': message},
            )
        if result.get('verified') is not True:
            return {
                'ok': True,
                'status': 'not_joined',
                'guild_name': guild_name,
                'guild_display_name': timo_guild_display_name(guild_name),
                'timo_id': timo_id,
                'queried_at': queried_at,
            }
        member = dict(result.get('member') or {})
        joined_at = service._timo_epoch_ms_to_bj_text(member.get('joinTime'))
        return {
            'ok': True,
            'status': 'joined',
            'guild_name': guild_name,
            'guild_display_name': timo_guild_display_name(guild_name),
            'timo_id': timo_id,
            'nickname': str(member.get('nickName') or '').strip(),
            'join_date': joined_at[:10] if joined_at else '',
            'queried_at': queried_at,
        }

    @app.get('/api/ops/guild-streamer-history/{app_name}/{guild_name}/export.xlsx')
    def ops_guild_streamer_history_export(
        request: Request,
        app_name: str,
        guild_name: str,
        streamer_id: str,
    ) -> StreamingResponse:
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        profile = _guild_streamer_history_profile(app_name, guild_name, streamer_id)
        if profile['app_name'] == 'timo':
            date_to = service._timo_revenue_latest_complete_day_bj().isoformat()
        else:
            date_to = (datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=1)).isoformat()
        with service.db.connect() as conn:
            local_rows = load_local_revenue_rows(conn, profile=profile, date_to=date_to)
            covered_dates = load_covered_dates(conn, profile=profile, date_to=date_to)
        live_rows: List[Dict[str, Any]] = []
        live_full_range = False
        if profile['app_name'] == 'timo':
            live_rows = _timo_live_streamer_revenue_rows(
                profile=profile,
                date_to=date_to,
                covered_dates=covered_dates,
                user=user,
            )
        elif profile['app_name'] == 'linky' and profile['first_join_date'] <= date_to:
            executor = service.resolve_guild_executor(profile['guild_name'], app_name='linky')
            if executor and executor.get('oauth_configured'):
                try:
                    live_rows = fetch_linky_streamer_history(
                        executor=executor,
                        streamer_id=profile.get('platform_character_id') or profile['canonical_streamer_id'],
                        date_from=profile['first_join_date'],
                        date_to=date_to,
                    )
                    live_full_range = True
                except Exception as exc:
                    print(json.dumps({
                        'event': 'linky_streamer_history_live_fallback',
                        'guild_name': profile['guild_name'],
                        'streamer_id': profile['requested_streamer_id'],
                        'error': str(exc)[:180],
                    }, ensure_ascii=False))
        rows = merge_revenue_calendar(
            profile=profile,
            date_to=date_to,
            local_rows=local_rows,
            live_rows=live_rows,
            covered_dates=covered_dates,
            live_full_range=live_full_range,
        )
        content = build_streamer_history_xlsx(profile=profile, date_to=date_to, rows=rows)
        exported_guild_name = (
            timo_guild_display_name(profile['guild_name'])
            if profile['app_name'] == 'timo'
            else profile['guild_name']
        )
        display_name = f"{profile['app_name']}_{exported_guild_name}_{profile['requested_streamer_id']}_收益.xlsx"
        fallback_name = f"streamer-history-{profile['app_name']}-{profile['requested_streamer_id']}.xlsx"
        headers = {
            'Content-Disposition': f"attachment; filename=\"{fallback_name}\"; filename*=UTF-8''{quote(display_name)}",
            'X-Data-Coverage': 'full' if live_full_range or all(row.get('data_status') == '已覆盖' for row in rows) else 'partial',
        }
        return StreamingResponse(
            iter([content]),
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers=headers,
        )

    @app.get('/api/ops/guild-executors')
    def ops_guild_executors():
        return service.list_guild_executors()

    @app.get('/api/ops/timo-guild-executors')
    def ops_timo_guild_executors(request: Request):
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        cache_key = f'timo_guild_executors:{_ops_hot_cache_user_suffix(user)}'
        return _ops_hot_read_cache_get_or_set(
            cache_key,
            20.0,
            lambda: service.list_timo_guild_executors(user=user),
        )

    @app.post('/api/ops/timo-guild-executors/refresh-status')
    def ops_timo_guild_executors_refresh_status(request: Request):
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        result = service.trigger_timo_guild_executor_health_refresh(user=user)
        _ops_hot_read_cache_invalidate('timo_intake_guilds:')
        _ops_hot_read_cache_invalidate('timo_guild_executors:')
        return result

    @app.get('/api/ops/sogo-guild-executors')
    @app.get('/api/ops/sugo-guild-executors')
    def ops_sogo_guild_executors(request: Request):
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return service.list_sogo_guild_executors(user=user)

    @app.get('/api/ops/timo-auth-station/bootstrap')
    def ops_timo_auth_station_bootstrap(request: Request):
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_SUPER_ADMIN)
        station_service = TimoAuthStationService(service.db.db_path)
        try:
            guild_payload = service.list_timo_guild_executors(user=user, include_reward_tracks=False)
        except Exception:
            guild_payload = {'rows': []}
        try:
            ops_status = station_service.ops_status_summary()
        except Exception as exc:
            ops_status = {'ok': False, 'error': str(exc)}
        return {
            'ok': True,
            'user': user,
            'station_api_prefix': '/api/timo/auth-station',
            'station_token_configured': bool(str(timo_auth_station_token or '').strip()),
            'station_token': str(timo_auth_station_token or '').strip(),
            'guilds': guild_payload.get('rows') if isinstance(guild_payload, dict) else [],
            'device_bindings': station_service.list_device_bindings().get('rows', []),
            'ops_status': ops_status,
        }

    @app.get('/api/ops/timo-auth-station/ops-status')
    def ops_timo_auth_station_ops_status(request: Request):
        _require_ops_user(request, role=OPS_AUTH_ROLE_SUPER_ADMIN)
        return TimoAuthStationService(service.db.db_path).ops_status_summary()

    @app.get('/api/ops/timo-auth-station/device-bindings')
    def ops_timo_auth_station_device_bindings(request: Request, station_id: str = ''):
        _require_ops_user(request, role=OPS_AUTH_ROLE_SUPER_ADMIN)
        return TimoAuthStationService(service.db.db_path).list_device_bindings(station_id=station_id)

    @app.delete('/api/ops/timo-auth-station/device-bindings/{binding_id}')
    def ops_timo_auth_station_device_binding_delete(request: Request, binding_id: str):
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_SUPER_ADMIN)
        updated_by = str(user.get('username') or user.get('display_name') or user.get('user_id') or '').strip()
        try:
            return TimoAuthStationService(service.db.db_path).disable_device_binding(binding_id, updated_by=updated_by)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _timo_auth_station_update_dir() -> Path:
        configured = str(cfg.get('TIMO_AUTH_STATION_UPDATE_DIR') or os.getenv('TIMO_AUTH_STATION_UPDATE_DIR') or '').strip()
        if configured:
            return Path(configured)
        return Path(service.db.db_path).resolve().parent / 'timo_auth_station_updates'

    def _timo_auth_station_velopack_dir() -> Path:
        configured = str(cfg.get('TIMO_AUTH_STATION_VELOPACK_DIR') or os.getenv('TIMO_AUTH_STATION_VELOPACK_DIR') or '').strip()
        if configured:
            return Path(configured)
        return Path(service.db.db_path).resolve().parent / 'timo_auth_station_velopack'

    def _load_timo_auth_station_update_manifest() -> Tuple[Path, Dict[str, Any]]:
        update_dir = _timo_auth_station_update_dir().resolve()
        manifest_path = update_dir / 'manifest.json'
        if not manifest_path.exists():
            return update_dir, {}
        try:
            payload = json.loads(manifest_path.read_text(encoding='utf-8'))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f'timo_auth_station_update_manifest_invalid:{exc}') from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=500, detail='timo_auth_station_update_manifest_invalid')
        return update_dir, payload

    @app.get('/api/ops/timo-auth-station/windows-update/manifest')
    def ops_timo_auth_station_windows_update_manifest(request: Request, current_version: str = ''):
        _require_ops_user(request, role=OPS_AUTH_ROLE_SUPER_ADMIN)
        update_dir, manifest = _load_timo_auth_station_update_manifest()
        latest_version = str(manifest.get('version') or manifest.get('latest_version') or '').strip()
        requested_version = str(current_version or '').strip()
        filename = str(manifest.get('filename') or '').strip()
        file_path = (update_dir / filename).resolve() if filename else update_dir / ''
        file_ready = bool(filename and file_path.exists() and update_dir in file_path.parents)
        legacy_manifest_for_velopack_client = bool('-build.' in requested_version and latest_version.isdigit())
        update_available = bool(
            file_ready
            and latest_version
            and latest_version != requested_version
            and not legacy_manifest_for_velopack_client
        )
        public_download_url = f'/api/public/timo-auth-station/windows-update/files/{filename}' if file_ready else ''
        ops_download_url = '/api/ops/timo-auth-station/windows-update/download' if file_ready else ''
        return {
            'ok': True,
            'current_version': requested_version,
            'latest_version': latest_version,
            'update_available': update_available,
            'file_ready': file_ready,
            'filename': filename if file_ready else '',
            'size_bytes': int(file_path.stat().st_size) if file_ready else 0,
            'sha256': str(manifest.get('sha256') or '').strip() if file_ready else '',
            'notes': str(manifest.get('notes') or '').strip(),
            'download_url': public_download_url,
            'static_download_url': public_download_url,
            'public_download_url': public_download_url,
            'download_urls': [
                public_download_url,
                ops_download_url,
            ] if file_ready else [],
        }

    @app.get('/api/ops/timo-auth-station/windows-update/download')
    def ops_timo_auth_station_windows_update_download(request: Request):
        _require_ops_user(request, role=OPS_AUTH_ROLE_SUPER_ADMIN)
        update_dir, manifest = _load_timo_auth_station_update_manifest()
        filename = str(manifest.get('filename') or '').strip()
        if not filename:
            raise HTTPException(status_code=404, detail='timo_auth_station_update_file_not_configured')
        file_path = (update_dir / filename).resolve()
        if update_dir not in file_path.parents or not file_path.exists():
            raise HTTPException(status_code=404, detail='timo_auth_station_update_file_not_found')
        return FileResponse(
            path=str(file_path),
            filename=filename,
            media_type='application/vnd.microsoft.portable-executable',
        )

    @app.get('/api/public/timo-auth-station/windows-update/files/{filename}')
    def public_timo_auth_station_windows_update_download(filename: str):
        update_dir, manifest = _load_timo_auth_station_update_manifest()
        configured_filename = str(manifest.get('filename') or '').strip()
        requested_filename = str(filename or '').strip()
        if not configured_filename or requested_filename != configured_filename:
            raise HTTPException(status_code=404, detail='timo_auth_station_update_file_not_found')
        file_path = (update_dir / configured_filename).resolve()
        if update_dir not in file_path.parents or not file_path.exists():
            raise HTTPException(status_code=404, detail='timo_auth_station_update_file_not_found')
        return FileResponse(
            path=str(file_path),
            filename=configured_filename,
            media_type='application/vnd.microsoft.portable-executable',
        )

    @app.get('/api/public/timo-auth-station/velopack/{file_path:path}')
    def public_timo_auth_station_velopack_feed(file_path: str, request: Request):
        velopack_dir = _timo_auth_station_velopack_dir().resolve()
        requested_path = str(file_path or '').strip().replace('\\', '/')
        if not requested_path or requested_path.startswith('/') or '..' in Path(requested_path).parts:
            raise HTTPException(status_code=404, detail='timo_auth_station_velopack_file_not_found')
        local_version = str(request.query_params.get('localVersion') or '').strip()
        if requested_path == 'releases.win.json' and local_version in {
            '2026.7.13-build.192655',
            '2026.7.20-build.163132',
            '2026.7.20-build.173540',
        }:
            # This build downloaded Velopack deltas successfully but its detached
            # launcher could restart the unchanged current directory. Force its
            # existing authenticated Setup fallback for the one-time migration;
            # fixed clients continue to consume the normal Velopack feed.
            raise HTTPException(
                status_code=409,
                detail='timo_auth_station_velopack_setup_migration_required',
            )
        resolved_path = (velopack_dir / requested_path).resolve()
        if velopack_dir not in resolved_path.parents or not resolved_path.exists() or not resolved_path.is_file():
            raise HTTPException(status_code=404, detail='timo_auth_station_velopack_file_not_found')
        media_type = 'application/octet-stream'
        if resolved_path.name.endswith('.json'):
            media_type = 'application/json'
        elif resolved_path.suffix.lower() == '.exe':
            media_type = 'application/vnd.microsoft.portable-executable'
        return FileResponse(path=str(resolved_path), filename=resolved_path.name, media_type=media_type)

    @app.get('/api/public/timo-auth-station/relay-script')
    def public_timo_auth_station_relay_script(request: Request):
        expected_token = str(timo_auth_station_token or '').strip()
        if not expected_token:
            raise HTTPException(status_code=503, detail='timo_auth_station_token_not_configured')
        provided_token = str(request.headers.get('x-timo-auth-station-token') or '').strip()
        if not provided_token or not hmac.compare_digest(provided_token, expected_token):
            raise HTTPException(status_code=403, detail='timo_auth_station_token_required')
        relay_path = (Path(__file__).resolve().parents[1] / 'scripts' / 'timo_auth_station_relay.py').resolve()
        if not relay_path.exists() or not relay_path.is_file():
            raise HTTPException(status_code=404, detail='timo_auth_station_relay_script_not_found')
        relay_text = relay_path.read_text(encoding='utf-8')
        version = ''
        for relay_line in relay_text.splitlines():
            if relay_line.strip().startswith('RELAY_VERSION') and '=' in relay_line:
                version = relay_line.split('=', 1)[1].strip().strip('"').strip("'")
                break
        return Response(
            content=relay_text,
            media_type='text/x-python; charset=utf-8',
            headers={
                'x-timo-auth-station-relay-version': version,
                'cache-control': 'no-store',
            },
        )

    @app.get('/api/public/timo-auth-station/accessibility-bridge.apk')
    def public_timo_auth_station_accessibility_bridge(request: Request):
        expected_token = str(timo_auth_station_token or '').strip()
        if not expected_token:
            raise HTTPException(status_code=503, detail='timo_auth_station_token_not_configured')
        provided_token = str(request.headers.get('x-timo-auth-station-token') or '').strip()
        if not provided_token or not hmac.compare_digest(provided_token, expected_token):
            raise HTTPException(status_code=403, detail='timo_auth_station_token_required')
        apk_path = (
            Path(__file__).resolve().parents[1]
            / 'outputs' / 'timo_accessibility_bridge' / 'timo-auth-bridge.apk'
        ).resolve()
        if not apk_path.exists() or not apk_path.is_file():
            raise HTTPException(status_code=404, detail='timo_accessibility_bridge_apk_not_found')
        return FileResponse(
            path=str(apk_path),
            filename='timo-auth-bridge.apk',
            media_type='application/vnd.android.package-archive',
            headers={'cache-control': 'no-store'},
        )

    @app.post('/api/ops/timo-auth-station/device-bindings')
    def ops_timo_auth_station_device_binding_upsert(request: Request, payload: AuthStationDeviceBindingRequest):
        user = _require_ops_user(request, role=OPS_AUTH_ROLE_SUPER_ADMIN)
        created_by = str(user.get('username') or user.get('display_name') or user.get('user_id') or '').strip()
        try:
            return TimoAuthStationService(service.db.db_path).upsert_device_binding(payload, created_by=created_by)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get('/api/ops/production-ops-daemon')
    def ops_production_ops_daemon(view: Optional[str] = None):
        def with_current_probe_overlay(payload: Dict[str, Any]) -> Dict[str, Any]:
            result = dict(payload or {})
            runtime = dict(result.get('runtime') or {})
            status = dict(runtime.get('status') or {})
            current_probe: Dict[str, Any] = {}
            try:
                health = service.registration_group_approval_executor_health() or {}
            except Exception:
                health = {}
            provider = str(health.get('provider') or health.get('provider_name') or '').strip().lower()
            health_ready = bool(
                provider == 'baileys'
                and (health.get('ready') or health.get('authenticated') or str(health.get('status') or '').strip() in {'running', 'ready', 'warm'})
            )
            if health_ready:
                current_probe = {
                    'provider': 'baileys',
                    'account_key': health.get('account_key'),
                    'source': health.get('source') or health.get('routed_via') or 'baileys_approval_account_runtime',
                    'runtime_status': (health.get('runtime') or {}).get('status') if isinstance(health.get('runtime'), dict) else health.get('status'),
                    'monitor_target': health.get('monitor_target') if isinstance(health.get('monitor_target'), dict) else {},
                }
            if not current_probe:
                try:
                    rows = _ops_whatsapp_approval_account_directory_rows('registration_group')
                except Exception:
                    rows = []
                for row in rows:
                    if not isinstance(row, dict) or row.get('enabled') is False:
                        continue
                    runtime_state = row.get('runtime_state') if isinstance(row.get('runtime_state'), dict) else {}
                    session_state = row.get('session_state') if isinstance(row.get('session_state'), dict) else {}
                    provider_name = str(row.get('provider_name') or runtime_state.get('provider_name') or '').strip().lower()
                    provider_mode = str(row.get('provider_mode') or runtime_state.get('provider_mode') or '').strip().lower()
                    if provider_name != 'baileys' and not provider_mode.startswith('baileys'):
                        continue
                    runtime_ready = bool(
                        runtime_state.get('ready')
                        or runtime_state.get('authenticated')
                        or str(runtime_state.get('status') or '').strip().lower() in {'running', 'ready', 'warm'}
                    )
                    session_ready = bool(session_state.get('login_verified') or session_state.get('can_probe'))
                    if not (runtime_ready or session_ready):
                        continue
                    current_probe = {
                        'provider': 'baileys',
                        'account_key': row.get('account_key'),
                        'source': runtime_state.get('source') or 'whatsapp_approval_account_directory',
                        'runtime_status': runtime_state.get('status'),
                        'monitor_target': {
                            'account_key': row.get('account_key'),
                            'worker_base_url': '',
                            'provider_base_url': str(runtime_state.get('base_url') or '').strip().rstrip('/') or None,
                            'source': 'account_binding',
                            'provider_name': 'baileys',
                            'provider_mode': provider_mode,
                        },
                    }
                    break
            if current_probe:
                status['worker_state'] = {
                    'ok': True,
                    'status': 'baileys_provider_ready',
                    'status_text': 'Baileys 账号已登录，审批探针可用',
                    'login_state': 'logged_in',
                    'login_state_label': '已登录',
                    'can_probe': True,
                    'provider': 'baileys',
                    'source': current_probe.get('source') or 'baileys_approval_account_runtime',
                }
                if isinstance(current_probe.get('monitor_target'), dict) and current_probe.get('monitor_target'):
                    status['monitor_target'] = current_probe.get('monitor_target')
                overlay = {
                    'applied': True,
                    'source': 'registration_group_approval_executor_health',
                    'provider': 'baileys',
                    'account_key': current_probe.get('account_key'),
                    'runtime_status': current_probe.get('runtime_status'),
                }
                if current_probe.get('source'):
                    overlay['runtime_source'] = current_probe.get('source')
                status['current_probe_overlay'] = overlay
                result['current_probe_overlay'] = overlay
                runtime['status'] = status
                result['runtime'] = runtime
            return result

        if str(view or '').strip().lower() in {'debug', 'full'}:
            payload = service.get_production_ops_daemon_config()
            payload['payload_mode'] = 'debug'
            return with_current_probe_overlay(payload)
        return _ops_hot_read_cache_get_or_set(
            'production_ops_daemon:light',
            12.0,
            lambda: service.get_production_ops_daemon_config_light(),
        )

    @app.get('/api/ops/production-ops-daemon/monitor-target')
    def ops_production_ops_daemon_monitor_target():
        snapshot = service.get_production_ops_daemon_config() or {}
        config = snapshot.get('config') or {}
        runtime = snapshot.get('runtime') or {}
        status = runtime.get('status') or {}
        current_candidates: list[dict[str, Any]] = []
        current_configured: list[dict[str, Any]] = []
        try:
            for row in _ops_whatsapp_approval_account_directory_rows('registration_group'):
                if not isinstance(row, dict) or not row.get('enabled'):
                    continue
                runtime_state = row.get('runtime_state') if isinstance(row.get('runtime_state'), dict) else {}
                session_state = row.get('session_state') if isinstance(row.get('session_state'), dict) else {}
                runtime_active = bool(runtime_state.get('active') or runtime_state.get('configured'))
                login_ready = bool(session_state.get('login_verified') or session_state.get('can_probe') or runtime_state.get('authenticated') or runtime_state.get('ready'))
                account_provider_mode = str(row.get('provider_mode') or runtime_state.get('provider_mode') or '').strip()
                account_provider_name = str(row.get('provider_name') or runtime_state.get('provider_name') or '').strip()
                for binding in row.get('group_binding_runtimes') or row.get('group_link_bindings') or []:
                    if not isinstance(binding, dict) or binding.get('enabled') is False:
                        continue
                    provider_mode = str(binding.get('provider_mode') or account_provider_mode or '').strip()
                    provider_name = str(binding.get('provider_name') or account_provider_name or '').strip()
                    binding_uses_baileys = bool(
                        provider_name.lower() == 'baileys'
                        or provider_mode.lower().startswith('baileys')
                        or binding.get('baileys_enabled') is True
                        or row.get('baileys_enabled') is True
                    )
                    registration_group = str(
                        binding.get('group_id')
                        or binding.get('registration_group')
                        or binding.get('runtime_probe_group_id')
                        or binding.get('link')
                        or binding.get('group_name')
                        or ''
                    ).strip()
                    if not registration_group:
                        continue
                    target = {
                        'registration_group': registration_group,
                        'group_name': str(binding.get('group_name') or registration_group).strip(),
                        'binding_link': str(binding.get('link') or '').strip() or None,
                        'account_key': row.get('account_key'),
                        'account_name': row.get('account_name'),
                        'area': binding.get('area') or row.get('area'),
                        'worker_base_url': '' if binding_uses_baileys else str(runtime_state.get('base_url') or '').strip().rstrip('/'),
                        'provider_base_url': str(runtime_state.get('base_url') or '').strip().rstrip('/') or None,
                        'provider_name': provider_name,
                        'provider_mode': provider_mode,
                        'baileys_enabled': bool(binding_uses_baileys),
                        'baileys_account_id': str(binding.get('baileys_account_id') or row.get('baileys_account_id') or runtime_state.get('baileys_account_id') or '').strip(),
                        'runtime_state': runtime_state,
                        'session_state': session_state,
                        'source': 'account_binding',
                    }
                    current_configured.append(target)
                    if runtime_active and login_ready:
                        current_candidates.append(target)
        except Exception:
            current_candidates = []
            current_configured = []
        selected_current = current_candidates[0] if current_candidates else (current_configured[0] if current_configured else {})
        if selected_current:
            selection_reason = 'account_binding_active' if current_candidates else 'configured_binding_runtime_unavailable'
            return {
                'registration_group': str(selected_current.get('registration_group') or status.get('registration_group') or config.get('registration_group') or '').strip(),
                'monitor_target': selected_current,
                'monitor_targets': {
                    'selection_reason': selection_reason,
                    'candidates': current_candidates or current_configured,
                    'active_count': len(current_candidates),
                    'allow_fallback': False if current_configured and not current_candidates else True,
                },
                'runtime_status': status,
            }
        return {
            'registration_group': str(status.get('registration_group') or config.get('registration_group') or '').strip(),
            'monitor_target': status.get('monitor_target') or {},
            'runtime_status': status,
        }

    @app.get('/api/ops/whatsapp-approval-accounts')
    def ops_whatsapp_approval_accounts(request: Request):
        current_user = _request_session_user(request)
        cache_key = f'approval_accounts:{_ops_hot_cache_user_suffix(current_user)}'
        return _ops_hot_read_cache_get_or_set(
            cache_key,
            12.0,
            lambda: service.list_whatsapp_approval_accounts(current_user=current_user, lightweight=True),
        )

    @app.get('/api/ops/whatsapp-approval-accounts/options')
    def ops_whatsapp_approval_account_options(request: Request):
        _request_session_user(request)
        return _ops_hot_read_cache_get_or_set(
            'approval_accounts:options',
            30.0,
            lambda: service.list_whatsapp_approval_account_options(),
            stale_ttl_seconds=300.0,
        )

    @app.get('/api/ops/whatsapp-approval-accounts/live')
    def ops_whatsapp_approval_accounts_live(request: Request):
        return service.list_whatsapp_approval_accounts(current_user=_request_session_user(request), lightweight=False)

    @app.get('/api/ops/whatsapp-approval-accounts/overview')
    def ops_whatsapp_approval_accounts_overview(request: Request):
        current_user = _request_session_user(request)
        cache_key = f'approval_accounts:{_ops_hot_cache_user_suffix(current_user)}'
        payload = _ops_hot_read_cache_get_or_set(
            cache_key,
            12.0,
            lambda: service.list_whatsapp_approval_accounts(current_user=current_user, lightweight=True, include_options=False),
        ) or {}
        rows = payload.get('rows') or []
        trimmed_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            trimmed_rows.append({
                'account_key': row.get('account_key'),
                'account_name': row.get('account_name'),
                'responsible_type': row.get('responsible_type'),
                'enabled': row.get('enabled'),
                'area': row.get('area'),
                'group_count': row.get('group_count'),
                'status_text': row.get('status_text'),
                'status_color': row.get('status_color'),
                'runtime_status': row.get('runtime_status'),
                'verification_status': row.get('verification_status'),
                'verification_status_label': row.get('verification_status_label'),
                'verification_checks': row.get('verification_checks') or [],
                'service_scope': row.get('service_scope') or {},
                'session_state': row.get('session_state') or {},
                'assigned_customer_service_user_id': row.get('assigned_customer_service_user_id'),
                'assigned_customer_service_user_ids': row.get('assigned_customer_service_user_ids') or [],
                'assigned_customer_service_username': row.get('assigned_customer_service_username'),
                'assigned_customer_service_display_name': row.get('assigned_customer_service_display_name'),
                'group_link_bindings': row.get('group_link_bindings') or [],
                'group_binding_runtimes': row.get('group_binding_runtimes') or [],
            })
        return {
            'rows': trimmed_rows,
            'summary': payload.get('summary') or {},
        }

    def _approval_realtime_store() -> RealtimeApprovalStateStore:
        return app.state.approval_realtime_store

    def _approval_realtime_seed_full_snapshot_before_partial_update() -> None:
        store = _approval_realtime_store()
        snapshot = store.snapshot()
        if int(snapshot.get('snapshot_version') or 0) > 0:
            return
        store.ingest_snapshot(
            {'rows': [], 'account_set_complete': False},
            source='cold_seed_pending',
        )
        _schedule_approval_realtime_full_seed()

    def _approval_operation_realtime_callback(
        *,
        account_key: str,
        binding_index: int,
        operation: str,
        task_id: str,
        result: Dict[str, Any],
    ) -> None:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            return
        row = service._get_whatsapp_approval_account_runtime_row_lightweight(normalized_key)
        if not isinstance(row, dict) or not row:
            return
        realtime_source = {
            'truth_refresh': 'manual_truth_refresh',
            'full_sync': 'manual_full_sync',
            'manual_approve': 'manual_approve',
            'probe_refresh': 'manual_probe',
            'rebuild_identity': 'manual_rebuild_identity',
        }.get(str(operation or '').strip(), 'manual_truth_refresh')
        _approval_realtime_seed_full_snapshot_before_partial_update()
        _approval_realtime_store().ingest_snapshot(
            {'rows': [row]},
            source=realtime_source,
        )
        _ops_hot_read_cache_invalidate('approval_realtime:')
        _ops_hot_read_cache_invalidate('approval_accounts:')
        _ops_hot_read_cache_invalidate('approval_batch_queue:')
        _ops_hot_read_cache_invalidate('production_ops_daemon:')
        _ops_hot_read_cache_invalidate('official_group_bridge_summary:')

    service.approval_operation_realtime_callback = _approval_operation_realtime_callback

    def _approval_realtime_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        safe_rows = [row for row in rows if isinstance(row, dict)]
        return {
            'total_accounts': len(safe_rows),
            'enabled_accounts': sum(1 for row in safe_rows if row.get('enabled')),
            'registration_group_accounts': sum(1 for row in safe_rows if row.get('responsible_type') == 'registration_group'),
            'official_group_accounts': sum(1 for row in safe_rows if row.get('responsible_type') == 'official_group'),
            'active_now_accounts': sum(1 for row in safe_rows if row.get('runtime_status') == 'active'),
            'ready_accounts': sum(1 for row in safe_rows if row.get('verification_status') == 'ready'),
            'verification_pending_accounts': sum(1 for row in safe_rows if row.get('verification_status') != 'ready'),
        }

    def _approval_realtime_user_allowed_account_keys(snapshot: Dict[str, Any], current_user: Optional[Dict[str, Any]]) -> Optional[set[str]]:
        if not auth_enabled:
            return None
        if not current_user:
            return set()
        role = str(current_user.get('role') or '').strip().lower()
        if role in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_INTERNAL}:
            return None
        if not ops_role_is_business(role):
            return set()
        user_id = str(current_user.get('user_id') or '').strip()
        allowed_keys: set[str] = set()
        for row in (snapshot.get('rows') or []):
            if not isinstance(row, dict):
                continue
            account_key = str(row.get('account_key') or '').strip()
            if not account_key:
                continue
            assigned_user_ids = service._whatsapp_approval_assigned_customer_service_ids_from_row(row)
            if user_id in assigned_user_ids:
                allowed_keys.add(account_key)
                continue
            if str(row.get('responsible_type') or '').strip() == 'official_group' and not assigned_user_ids:
                allowed_keys.add(account_key)
        return allowed_keys

    def _filter_approval_realtime_snapshot_for_user(snapshot: Dict[str, Any], current_user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        allowed_keys = _approval_realtime_user_allowed_account_keys(snapshot, current_user)
        if allowed_keys is None:
            return snapshot
        filtered = copy.deepcopy(snapshot or {})
        rows = [
            copy.deepcopy(row)
            for row in (snapshot.get('rows') or [])
            if isinstance(row, dict) and str(row.get('account_key') or '').strip() in allowed_keys
        ]
        filtered['rows'] = rows
        filtered['summary'] = _approval_realtime_summary(rows)
        return filtered

    def _approval_realtime_row_has_ready_session(row: Dict[str, Any]) -> bool:
        if not isinstance(row, dict):
            return False
        session = row.get('session_state') if isinstance(row.get('session_state'), dict) else {}
        return any(value is True for value in (
            session.get('login_verified'),
            session.get('can_probe'),
            session.get('ready'),
            session.get('authenticated'),
            row.get('login_verified'),
            row.get('can_probe'),
        )) or (
            str(row.get('runtime_status') or '').strip() == 'active'
            and str(row.get('verification_status') or '').strip() == 'ready'
        )

    def _approval_realtime_row_has_explicit_unready_session(row: Dict[str, Any]) -> bool:
        if not isinstance(row, dict) or row.get('enabled') is False:
            return False
        if _approval_realtime_row_has_ready_session(row):
            return False
        session = row.get('session_state') if isinstance(row.get('session_state'), dict) else {}
        login_state = str(session.get('login_state') or row.get('login_state') or '').strip()
        login_status = str(session.get('login_check_status') or row.get('login_check_status') or '').strip()
        runtime_status = str(row.get('runtime_status') or '').strip()
        verification_status = str(row.get('verification_status') or '').strip()
        status_text = str(row.get('status_text') or '').strip()
        return (
            verification_status == 'pending_login'
            or runtime_status in {'inactive', 'starting', 'error'}
            or login_state in {'not_logged_in', 'not_started', 'waiting_for_scan_qr_ready', 'waiting_for_scan_qr_pending', 'runtime_starting', 'initializing'}
            or login_status in {'pending_runtime', 'not_logged_in', 'not_started', 'waiting_for_scan', 'qr_pending', 'needs_scan', 'runtime_recovering'}
            or status_text in {'待登录', '待扫码', '登录异常'}
        )

    def _approval_realtime_row_needs_session_refresh(row: Dict[str, Any]) -> bool:
        if not isinstance(row, dict) or row.get('enabled') is False:
            return False
        if _approval_realtime_row_has_explicit_unready_session(row):
            return True
        session = row.get('session_state') if isinstance(row.get('session_state'), dict) else {}
        runtime = row.get('runtime_state') if isinstance(row.get('runtime_state'), dict) else {}
        provider_markers = {
            str(row.get('provider_name') or '').strip().lower(),
            str(row.get('provider_mode') or '').strip().lower(),
            str(runtime.get('provider_name') or '').strip().lower(),
            str(runtime.get('source') or '').strip().lower(),
            str(runtime.get('mode') or '').strip().lower(),
            str(session.get('auth_strategy') or '').strip().lower(),
            str(session.get('mode') or '').strip().lower(),
        }
        return bool(
            row.get('baileys_account_id')
            or runtime.get('baileys_account_id')
            or session.get('baileys_account_id')
            or any('baileys' in marker for marker in provider_markers if marker)
        )

    approval_session_refresh_lock = threading.Lock()
    approval_session_refresh_state: Dict[str, Any] = {
        'running_keys': set(),
        'last_started_by_key': {},
    }

    def _schedule_approval_realtime_session_refresh(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        refresh_keys = sorted({
            str(row.get('account_key') or '').strip()
            for row in (rows or [])
            if _approval_realtime_row_needs_session_refresh(row) and str(row.get('account_key') or '').strip()
        })
        if not refresh_keys:
            return {'ok': True, 'skipped': True, 'reason': 'no_stale_sessions'}
        now_monotonic = time.monotonic()
        scheduled_keys: List[str] = []
        with approval_session_refresh_lock:
            running_keys = approval_session_refresh_state['running_keys']
            last_started_by_key = approval_session_refresh_state['last_started_by_key']
            for account_key in refresh_keys:
                if account_key in running_keys:
                    continue
                last_started = float(last_started_by_key.get(account_key) or 0.0)
                if last_started > 0.0 and now_monotonic - last_started < 30.0:
                    continue
                running_keys.add(account_key)
                last_started_by_key[account_key] = now_monotonic
                scheduled_keys.append(account_key)

        def _worker(account_keys: List[str]) -> None:
            for account_key in account_keys:
                try:
                    refreshed_row = service._get_whatsapp_approval_account_runtime_row_provider_snapshot(account_key)
                    if isinstance(refreshed_row, dict) and refreshed_row:
                        _approval_realtime_store().ingest_snapshot(
                            {'rows': [refreshed_row]},
                            source='session_recovery_refresh',
                        )
                except Exception as exc:
                    try:
                        service.write_event_ledger(
                            event_type='approval_session_background_refresh_failed',
                            object_type='whatsapp_approval_account',
                            object_key=account_key,
                            status='failed',
                            evidence_level='warning',
                            payload={'error': str(exc)[:500]},
                        )
                    except Exception:
                        pass
                finally:
                    with approval_session_refresh_lock:
                        approval_session_refresh_state['running_keys'].discard(account_key)

        if scheduled_keys:
            threading.Thread(
                target=_worker,
                args=(scheduled_keys,),
                name='approval-session-refresh-sampler',
                daemon=True,
            ).start()
        return {
            'ok': True,
            'scheduled': bool(scheduled_keys),
            'mode': 'async_per_account_singleflight',
            'scheduled_count': len(scheduled_keys),
        }

    def _approval_realtime_event_allowed(event: Dict[str, Any], allowed_keys: Optional[set[str]]) -> bool:
        if allowed_keys is None:
            return True
        account_key = str((event or {}).get('account_key') or '').strip()
        if not account_key:
            return True
        return account_key in allowed_keys

    def _approval_realtime_account_snapshot_patch(row: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(row, dict):
            return {}
        account_key = str(row.get('account_key') or '').strip()
        if not account_key:
            return {}
        patch: Dict[str, Any] = {}
        for field in ('runtime_status', 'verification_status', 'status_text'):
            if field in row:
                patch[field] = row.get(field)
        session_state = row.get('session_state') if isinstance(row.get('session_state'), dict) else {}
        for field in (
            'login_state',
            'ready',
            'authenticated',
            'login_verified',
            'can_probe',
            'login_check_status',
            'login_check_message',
            'qr_available',
            'can_show_qr',
            'qr_stale',
        ):
            if field in session_state:
                patch[field] = session_state.get(field)
        return {
            'type': 'account_state_patch',
            'account_key': account_key,
            'group_id': '',
            'patch': patch,
            'source': 'websocket_snapshot_sync',
            'server_emit_at': datetime.now(timezone.utc).isoformat(),
            'delivery_target_ms': _approval_realtime_store().delivery_target_ms,
        } if patch else {}

    def _websocket_session_user(websocket: WebSocket) -> Optional[Dict[str, Any]]:
        if not auth_enabled:
            return {
                'user_id': 'local-dev',
                'username': 'local-dev',
                'display_name': 'local-dev',
                'role': OPS_AUTH_ROLE_ADMIN,
                'enabled': True,
            }
        raw_token = websocket.cookies.get(auth_manager.cookie_name)
        return auth_manager.session_user(raw_token)

    approval_truth_self_heal_lock = threading.Lock()
    approval_truth_self_heal_state = {'running': False, 'last_started_monotonic': 0.0}

    approval_realtime_full_seed_lock = threading.Lock()
    approval_realtime_full_seed_state = {'running': False}

    def _schedule_approval_realtime_full_seed() -> Dict[str, Any]:
        store = _approval_realtime_store()
        with approval_realtime_full_seed_lock:
            if bool(approval_realtime_full_seed_state.get('running')):
                return {'ok': True, 'scheduled': False, 'reason': 'seed_already_running'}
            approval_realtime_full_seed_state['running'] = True
        seed_version = int(store.snapshot().get('snapshot_version') or 0)

        def _worker() -> None:
            retry = False
            try:
                payload = service.list_whatsapp_approval_accounts(lightweight=True, include_options=False) or {}
                if not isinstance(payload, dict):
                    payload = {}
                current = store.snapshot()
                if int(current.get('snapshot_version') or 0) != seed_version:
                    retry = not bool(current.get('account_set_complete'))
                    return
                payload['account_set_complete'] = True
                store.ingest_snapshot(payload, source='lightweight_snapshot_refresh')
                _ops_hot_read_cache_invalidate('approval_realtime:')
            except Exception as exc:
                try:
                    service.write_event_ledger(
                        event_type='approval_realtime_full_seed_failed',
                        object_type='whatsapp_approval_account',
                        object_key='realtime_snapshot',
                        status='failed',
                        evidence_level='warning',
                        payload={'error': str(exc)[:500]},
                    )
                except Exception:
                    pass
            finally:
                with approval_realtime_full_seed_lock:
                    approval_realtime_full_seed_state['running'] = False
                if retry:
                    _schedule_approval_realtime_full_seed()

        threading.Thread(
            target=_worker,
            name='approval-realtime-full-seed',
            daemon=True,
        ).start()
        return {'ok': True, 'scheduled': True, 'mode': 'async_singleflight'}

    def _schedule_approval_truth_self_heal(
        rows: List[Dict[str, Any]],
        *,
        created_by: str = 'realtime_snapshot_refresh',
    ) -> Dict[str, Any]:
        safe_rows = copy.deepcopy(rows or [])
        if not safe_rows:
            return {'ok': True, 'skipped': True, 'reason': 'no_rows'}
        now_monotonic = time.monotonic()
        with approval_truth_self_heal_lock:
            if bool(approval_truth_self_heal_state.get('running')):
                return {'ok': True, 'skipped': True, 'reason': 'self_heal_already_running'}
            last_started = float(approval_truth_self_heal_state.get('last_started_monotonic') or 0.0)
            elapsed = now_monotonic - last_started
            if last_started > 0.0 and elapsed < 10.0:
                return {'ok': True, 'skipped': True, 'reason': 'self_heal_schedule_cooldown', 'retry_after_seconds': round(10.0 - elapsed, 3)}
            approval_truth_self_heal_state['running'] = True
            approval_truth_self_heal_state['last_started_monotonic'] = now_monotonic

        def _worker() -> None:
            try:
                service.maybe_enqueue_expired_approval_queue_self_heal(
                    safe_rows,
                    created_by=created_by,
                )
            except Exception as exc:
                try:
                    service.write_event_ledger(
                        event_type='approval_truth_self_heal_enqueue_failed',
                        object_type='registration_group_binding',
                        object_key='realtime_snapshot_refresh',
                        status='failed',
                        evidence_level='warning',
                        payload={'error': str(exc)[:500]},
                    )
                except Exception:
                    pass
            finally:
                with approval_truth_self_heal_lock:
                    approval_truth_self_heal_state['running'] = False

        threading.Thread(target=_worker, name='approval-truth-self-heal', daemon=True).start()
        return {'ok': True, 'scheduled': True, 'mode': 'async_background'}

    def _approval_realtime_seed_snapshot_if_empty(current_user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        store = _approval_realtime_store()
        snapshot = store.snapshot()
        if int(snapshot.get('snapshot_version') or 0) <= 0:
            snapshot = store.ingest_snapshot(
                {'rows': [], 'account_set_complete': False},
                source='cold_seed_pending',
            )['snapshot']
        if not bool(snapshot.get('account_set_complete')):
            snapshot = dict(snapshot)
            snapshot['approval_full_seed'] = _schedule_approval_realtime_full_seed()
        rows = snapshot.get('rows') if isinstance(snapshot.get('rows'), list) else []
        try:
            snapshot = dict(snapshot)
            snapshot['approval_session_refresh'] = _schedule_approval_realtime_session_refresh(rows)
            snapshot['approval_truth_self_heal'] = _schedule_approval_truth_self_heal(rows)
        except Exception as exc:
            snapshot = dict(snapshot)
            snapshot['approval_truth_self_heal'] = {
                'ok': False,
                'error': str(exc),
                'source': 'realtime_snapshot_refresh',
            }
        return _filter_approval_realtime_snapshot_for_user(snapshot, current_user)

    @app.get('/api/ops/whatsapp-approval-accounts/realtime-snapshot')
    def ops_whatsapp_approval_accounts_realtime_snapshot(request: Request):
        current_user = _request_session_user(request)
        return _approval_realtime_seed_snapshot_if_empty(current_user)

    @app.post('/api/internal/whatsapp-approval/realtime-state')
    def internal_whatsapp_approval_realtime_state(payload: Dict[str, Any] = Body(default_factory=dict)):
        source = str(payload.get('source') or 'internal').strip() if isinstance(payload, dict) else 'internal'
        snapshot_payload = payload.get('snapshot') if isinstance(payload.get('snapshot'), dict) else payload
        store = _approval_realtime_store()
        current_snapshot = store.snapshot()
        try:
            incoming_snapshot_version = int(snapshot_payload.get('snapshot_version') or 0)
        except (TypeError, ValueError):
            incoming_snapshot_version = 0
        current_snapshot_version = int(current_snapshot.get('snapshot_version') or 0)
        if (
            source == 'production_ops_daemon'
            and incoming_snapshot_version > 0
            and current_snapshot_version >= incoming_snapshot_version
        ):
            return {
                'ok': True,
                'ignored': True,
                'ignore_reason': 'stale_snapshot_feedback',
                'incoming_snapshot_version': incoming_snapshot_version,
                'snapshot_version': current_snapshot_version,
                'event_id': current_snapshot.get('event_id'),
                'event_count': 0,
                'events': [],
            }
        if RealtimeApprovalStateStore._is_partial_account_snapshot_source(source):
            _approval_realtime_seed_full_snapshot_before_partial_update()
        result = store.ingest_snapshot(snapshot_payload, source=source)
        _ops_hot_read_cache_invalidate('approval_realtime:')
        return {
            'ok': True,
            'snapshot_version': result['snapshot'].get('snapshot_version'),
            'event_id': result['snapshot'].get('event_id'),
            'event_count': len(result.get('events') or []),
            'events': result.get('events') or [],
        }

    @app.websocket('/api/ops/whatsapp-approval-accounts/realtime-ws')
    async def ops_whatsapp_approval_accounts_realtime_ws(websocket: WebSocket):
        await websocket.accept()
        store = _approval_realtime_store()
        current_user = _websocket_session_user(websocket)
        snapshot = await asyncio.to_thread(_approval_realtime_seed_snapshot_if_empty, current_user)
        allowed_keys = _approval_realtime_user_allowed_account_keys(store.snapshot(), current_user)
        raw_last_event_id = websocket.query_params.get('last_event_id') if websocket.query_params else None
        try:
            last_event_id = int(raw_last_event_id or 0)
        except (TypeError, ValueError):
            last_event_id = 0
        await websocket.send_json({
            'type': 'hello',
            'snapshot_version': snapshot.get('snapshot_version'),
            'event_id': snapshot.get('event_id'),
            'delivery_target_ms': store.delivery_target_ms,
        })
        await websocket.send_json({
            'type': 'snapshot',
            'snapshot': snapshot,
            'snapshot_version': snapshot.get('snapshot_version'),
            'event_id': snapshot.get('event_id'),
            'server_emit_at': datetime.now(timezone.utc).isoformat(),
            'delivery_target_ms': store.delivery_target_ms,
        })
        for row in snapshot.get('rows') or []:
            account_snapshot_event = _approval_realtime_account_snapshot_patch(row)
            if account_snapshot_event and _approval_realtime_event_allowed(account_snapshot_event, allowed_keys):
                await websocket.send_json(account_snapshot_event)
        for event in store.events_since(last_event_id):
            if _approval_realtime_event_allowed(event, allowed_keys):
                await websocket.send_json(event)
        queue = store.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    if _approval_realtime_event_allowed(event, allowed_keys):
                        await websocket.send_json(event)
                except asyncio.TimeoutError:
                    await websocket.send_json({
                        'type': 'heartbeat',
                        'snapshot_version': store.snapshot().get('snapshot_version'),
                        'event_id': store.snapshot().get('event_id'),
                        'server_emit_at': datetime.now(timezone.utc).isoformat(),
                        'delivery_target_ms': store.delivery_target_ms,
                    })
        except WebSocketDisconnect:
            pass
        finally:
            store.unsubscribe(queue)

    def _ops_whatsapp_approval_account_directory_rows(responsible_type_filter: str = '') -> list[dict[str, Any]]:
        def build_rows() -> list[dict[str, Any]]:
            account_payload = service._approval_batch_queue_accounts_payload(
                production_ops=service._production_ops_daemon_snapshot_light()
            )
            directory_rows: list[dict[str, Any]] = []
            for raw_row in account_payload.get('rows') or []:
                if not isinstance(raw_row, dict):
                    continue
                responsible_type = str(raw_row.get('responsible_type') or '').strip()
                if responsible_type_filter and responsible_type != responsible_type_filter:
                    continue
                group_link_bindings = [
                    dict(item or {})
                    for item in (raw_row.get('group_link_bindings') or [])
                    if isinstance(item, dict)
                ]
                group_binding_runtimes = [
                    dict(item or {})
                    for item in (raw_row.get('group_binding_runtimes') or group_link_bindings)
                    if isinstance(item, dict)
                ]
                directory_rows.append({
                    'account_key': str(raw_row.get('account_key') or '').strip(),
                    'account_name': raw_row.get('account_name'),
                    'responsible_type': responsible_type,
                    'enabled': bool(raw_row.get('enabled')),
                    'area': raw_row.get('area'),
                    'provider_name': raw_row.get('provider_name'),
                    'provider_mode': raw_row.get('provider_mode'),
                    'provider_capabilities': raw_row.get('provider_capabilities') or {},
                    'provider_decision': raw_row.get('provider_decision') or {},
                    'runtime_state': raw_row.get('runtime_state') if isinstance(raw_row.get('runtime_state'), dict) else {},
                    'session_state': raw_row.get('session_state') if isinstance(raw_row.get('session_state'), dict) else {},
                    'group_link_bindings': group_link_bindings,
                    'group_binding_runtimes': group_binding_runtimes,
                    'group_links': [
                        str(item.get('link') or '').strip()
                        for item in group_link_bindings
                        if str(item.get('link') or '').strip()
                    ],
                    'runtime_status': raw_row.get('runtime_status'),
                    'verification_status': raw_row.get('verification_status'),
                    'monitor_runtime_active': bool(raw_row.get('monitor_runtime_active')),
                    'service_scope': raw_row.get('service_scope') or {},
                })
            return directory_rows

        cache_scope = str(responsible_type_filter or 'all').strip() or 'all'
        return _ops_hot_read_cache_get_or_set(f'approval_accounts:directory:{cache_scope}', 12.0, build_rows)

    @app.get('/api/ops/whatsapp-approval-accounts/runtime-directory')
    def ops_whatsapp_approval_accounts_runtime_directory():
        trimmed_rows = _ops_whatsapp_approval_account_directory_rows()
        return {'rows': trimmed_rows}

    @app.get('/api/ops/whatsapp-approval-accounts/registration-runtime-directory')
    def ops_whatsapp_approval_accounts_registration_runtime_directory():
        trimmed_rows = _ops_whatsapp_approval_account_directory_rows('registration_group')
        return {'rows': trimmed_rows}

    @app.get('/api/ops/whatsapp-approval-accounts/binding-directory')
    def ops_whatsapp_approval_accounts_binding_directory():
        trimmed_rows = _ops_whatsapp_approval_account_directory_rows()
        return {'rows': trimmed_rows}

    @app.get('/api/ops/whatsapp-approval-accounts/official-binding-directory')
    def ops_whatsapp_approval_accounts_official_binding_directory():
        trimmed_rows = _ops_whatsapp_approval_account_directory_rows('official_group')
        return {'rows': trimmed_rows}

    @app.get('/api/ops/whatsapp-approval-accounts/{account_key}/runtime')
    def ops_whatsapp_approval_account_runtime(account_key: str, request: Request):
        service._require_whatsapp_approval_account_access(account_key, _request_session_user(request))
        return service.get_whatsapp_approval_account_runtime(account_key)

    @app.get('/api/ops/whatsapp-approval-accounts/{account_key}/runtime/internal')
    def ops_whatsapp_approval_account_runtime_internal(account_key: str):
        return service.get_whatsapp_approval_account_runtime(account_key)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/runtime/start')
    def ops_whatsapp_approval_account_runtime_start(account_key: str, request: Request):
        service._require_whatsapp_approval_account_access(account_key, _request_session_user(request))
        return service.start_whatsapp_approval_account_runtime(account_key)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/runtime/internal/start')
    def ops_whatsapp_approval_account_runtime_internal_start(account_key: str):
        return service.start_whatsapp_approval_account_runtime(account_key)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/runtime/stop')
    def ops_whatsapp_approval_account_runtime_stop(account_key: str, request: Request):
        service._require_whatsapp_approval_account_access(account_key, _request_session_user(request))
        return service.stop_whatsapp_approval_account_runtime(account_key)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/runtime/recover')
    def ops_whatsapp_approval_account_runtime_recover(account_key: str, request: Request):
        service._require_whatsapp_approval_account_access(account_key, _request_session_user(request))
        return service.recover_whatsapp_approval_account_runtime(account_key)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/runtime/internal/stop')
    def ops_whatsapp_approval_account_runtime_internal_stop(account_key: str):
        return service.stop_whatsapp_approval_account_runtime(account_key)

    @app.get('/api/ops/whatsapp-approval-accounts/{account_key}/session')
    def ops_whatsapp_approval_account_session(account_key: str, request: Request, include_qr_ascii: bool = True):
        service._require_whatsapp_approval_account_access(account_key, _request_session_user(request))
        return service.get_whatsapp_approval_account_session(account_key, include_qr_ascii=include_qr_ascii)

    @app.get('/api/ops/whatsapp-approval-accounts/{account_key}/session/internal')
    def ops_whatsapp_approval_account_session_internal(account_key: str, include_qr_ascii: bool = False):
        return service.get_whatsapp_approval_account_session(account_key, include_qr_ascii=include_qr_ascii)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/session/start')
    def ops_whatsapp_approval_account_session_start(account_key: str, request: Request):
        service._require_whatsapp_approval_account_access(account_key, _request_session_user(request))
        return service.start_whatsapp_approval_account_session(account_key)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/session/pairing-code')
    def ops_whatsapp_approval_account_pairing_code(
        account_key: str,
        payload: WhatsAppApprovalPairingCodeRequest,
        request: Request,
    ):
        service._require_whatsapp_approval_account_access(account_key, _request_session_user(request))
        result = service.request_whatsapp_approval_account_pairing_code(account_key, payload.phone_number)
        return JSONResponse(
            content=result,
            headers={'Cache-Control': 'no-store, max-age=0', 'Pragma': 'no-cache'},
        )

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/session/internal/start')
    def ops_whatsapp_approval_account_session_internal_start(account_key: str):
        return service.start_whatsapp_approval_account_session(account_key)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/session/reset')
    def ops_whatsapp_approval_account_session_reset(account_key: str, request: Request):
        service._require_whatsapp_approval_account_access(account_key, _request_session_user(request))
        return service.reset_whatsapp_approval_account_session(account_key)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/session/internal/reset')
    def ops_whatsapp_approval_account_session_internal_reset(account_key: str):
        return service.reset_whatsapp_approval_account_session(account_key)

    def _merge_binding_runtime_result(result: Dict[str, Any], binding_runtime: Any) -> Dict[str, Any]:
        if not isinstance(result, dict) or not isinstance(binding_runtime, dict):
            return result
        merged_runtime = dict(result.get('binding_runtime') or {})
        for key, value in dict(binding_runtime).items():
            if value in (None, '', [], {}):
                continue
            existing_value = merged_runtime.get(key)
            if isinstance(value, dict) and isinstance(existing_value, dict):
                merged_nested = dict(existing_value or {})
                for nested_key, nested_value in value.items():
                    if nested_value in (None, '', [], {}):
                        continue
                    existing_nested_value = merged_nested.get(nested_key)
                    if isinstance(nested_value, dict) and isinstance(existing_nested_value, dict):
                        merged_nested[nested_key] = {**dict(nested_value), **dict(existing_nested_value)}
                    elif existing_nested_value in (None, '', [], {}):
                        merged_nested[nested_key] = nested_value
                merged_runtime[key] = merged_nested
            elif existing_value in (None, '', [], {}):
                merged_runtime[key] = value
        return {**result, 'binding_runtime': merged_runtime}

    def _record_whatsapp_approval_route_warning(event_type: str, *, account_key: str, binding_index: int, source: str, exc: Exception) -> None:
        try:
            service.write_event_ledger(
                event_type=event_type,
                object_type='registration_group_binding',
                object_key=f'{str(account_key or "").strip()}:{int(binding_index)}',
                status='failed',
                evidence_level='runtime',
                payload={
                    'account_key': str(account_key or '').strip(),
                    'binding_index': int(binding_index),
                    'source': source,
                    'error': str(exc),
                },
            )
        except Exception:
            pass

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/bindings/{binding_index}/full-sync')
    def ops_whatsapp_approval_binding_full_sync(account_key: str, binding_index: int, request: Request):
        service._require_whatsapp_approval_account_access(account_key, _request_session_user(request))
        request_id = str(request.headers.get('X-Request-ID') or request.headers.get('X-Correlation-ID') or '').strip() or create_id('approval_op')
        result = service.run_whatsapp_approval_task_sync(
            account_key=account_key,
            binding_index=binding_index,
            operation='full_sync',
            input_payload={
                'source': 'manual_full_sync',
                'timeout_seconds': 45.0,
                'request_id': request_id,
            },
            timeout_seconds=45,
            max_retries=2,
            created_by=str((_request_session_user(request) or {}).get('username') or '').strip(),
            wait_timeout_seconds=90.0,
        )
        try:
            binding_runtime = service._get_whatsapp_approval_binding_runtime_snapshot(account_key, int(binding_index))
            if isinstance(binding_runtime, dict):
                result = _merge_binding_runtime_result(result, binding_runtime)
            _approval_realtime_store().ingest_snapshot({'rows': [service._get_whatsapp_approval_account_runtime_row_lightweight(account_key)]}, source='manual_full_sync')
        except Exception as exc:
            _record_whatsapp_approval_route_warning('approval_realtime_ingest_failed', account_key=account_key, binding_index=binding_index, source='manual_full_sync', exc=exc)
        return result

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/bindings/{binding_index}/manual-approve')
    def ops_whatsapp_approval_binding_manual_approve(account_key: str, binding_index: int, request: Request):
        current_user = _request_session_user(request)
        service._require_whatsapp_approval_account_access(account_key, current_user)
        client_ip = ''
        forwarded_for = str(request.headers.get('X-Forwarded-For') or '').strip()
        if forwarded_for:
            client_ip = forwarded_for.split(',')[0].strip()
        elif request.client is not None:
            client_ip = str(request.client.host or '').strip()
        operator = {
            'user_id': str((current_user or {}).get('user_id') or '').strip() or None,
            'username': str((current_user or {}).get('username') or '').strip() or None,
            'display_name': str((current_user or {}).get('display_name') or '').strip() or None,
            'role': str((current_user or {}).get('role') or '').strip() or None,
            'session_id': str((current_user or {}).get('session_id') or '').strip() or None,
        }
        request_context = {
            'request_id': str(request.headers.get('X-Request-ID') or request.headers.get('X-Correlation-ID') or '').strip() or create_id('approval_op'),
            'client_ip': client_ip or None,
            'user_agent': str(request.headers.get('User-Agent') or '').strip() or None,
            'path': str(request.url.path or ''),
            'method': str(request.method or 'POST').upper(),
        }
        account_runtime = service._get_whatsapp_approval_account_runtime_row(account_key)
        is_official_group = str(account_runtime.get('responsible_type') or '').strip() == 'official_group'
        operation_started = False
        try:
            service._mark_whatsapp_binding_operation_started(
                account_key,
                int(binding_index),
                operation='manual_approve',
                detail='人工审批任务已提交，等待后台执行',
                stage_code='queued',
                stage_label='后台排队',
                request_id=str(request_context.get('request_id') or '').strip(),
            )
            operation_started = True
            if is_official_group:
                result = service.enqueue_whatsapp_approval_task(
                    account_key=account_key,
                    binding_index=binding_index,
                    operation='manual_approve',
                    input_payload={'operator': operator, 'request': request_context},
                    timeout_seconds=90,
                    max_retries=1,
                    created_by=str(operator.get('username') or operator.get('user_id') or '').strip(),
                )
                result = service.kick_whatsapp_approval_operation_task(result, force=True)
            else:
                result = service.run_whatsapp_approval_task_sync(
                    account_key=account_key,
                    binding_index=binding_index,
                    operation='manual_approve',
                    input_payload={'operator': operator, 'request': request_context},
                    timeout_seconds=90,
                    max_retries=1,
                    created_by=str(operator.get('username') or operator.get('user_id') or '').strip(),
                    wait_timeout_seconds=135.0,
                )
        except Exception:
            if operation_started:
                service._clear_whatsapp_binding_operation(account_key, int(binding_index))
            raise
        result.update({
            'ok': True,
            'accepted': True,
            'async': bool(is_official_group),
            'operation': 'manual_approve',
            'task_status': str(result.get('status') or '').strip(),
            'message': '人工审批任务已提交，后台执行中。' if is_official_group else '人工审批已执行完成。',
        })
        try:
            _approval_realtime_store().ingest_snapshot({'rows': [service._get_whatsapp_approval_account_runtime_row_lightweight(account_key)]}, source='manual_approve')
        except Exception as exc:
            _record_whatsapp_approval_route_warning('approval_realtime_ingest_failed', account_key=account_key, binding_index=binding_index, source='manual_approve', exc=exc)
        return result

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/bindings/{binding_index}/truth-refresh')
    def ops_whatsapp_approval_binding_truth_refresh(account_key: str, binding_index: int, request: Request):
        service._require_whatsapp_approval_account_access(account_key, _request_session_user(request))
        service._ensure_registration_group_cutover_enabled(account_key, binding_index, 'truth_refresh')
        request_id = str(request.headers.get('X-Request-ID') or request.headers.get('X-Correlation-ID') or '').strip() or create_id('approval_op')
        current_user = _request_session_user(request) or {}
        try:
            account_runtime = service._get_whatsapp_approval_account_runtime_row_lightweight(account_key)
        except Exception:
            account_runtime = {}
        is_official_group = str((account_runtime or {}).get('responsible_type') or '').strip() == 'official_group'
        foreground_timeout_seconds = 4.0

        def _enqueue_deferred_truth_refresh(reason: str, *, fallback: bool = False, recover_identity: bool = False) -> Dict[str, Any]:
            operation = 'probe_refresh' if recover_identity else ('truth_refresh' if is_official_group else 'full_sync')
            input_timeout_seconds = 5.0 if is_official_group else 30.0
            task_timeout_seconds = 75 if recover_identity else (8 if is_official_group else 45)
            queued_task: Dict[str, Any] = {}
            try:
                queued_task = service.enqueue_whatsapp_approval_task(
                    account_key=account_key,
                    binding_index=int(binding_index),
                    operation=operation,
                    input_payload={
                        'source': 'manual_truth_refresh',
                        'timeout_seconds': input_timeout_seconds,
                        'request_id': request_id,
                        'reason': str(reason or 'manual_truth_refresh_deferred').strip(),
                        'probe_mode': 'strict' if recover_identity else None,
                        'followup_truth_refresh': bool(recover_identity),
                        'followup_source': 'background_identity_probe_recovery' if recover_identity else None,
                        'followup_timeout_seconds': 30.0 if recover_identity else None,
                    },
                    timeout_seconds=task_timeout_seconds,
                    max_retries=2 if recover_identity else 1,
                    created_by=str(current_user.get('username') or '').strip(),
                )
            except Exception as enqueue_exc:
                _record_whatsapp_approval_route_warning(
                    'approval_truth_refresh_background_enqueue_failed',
                    account_key=account_key,
                    binding_index=binding_index,
                    source='manual_truth_refresh_fallback' if fallback else 'manual_truth_refresh',
                    exc=enqueue_exc,
                )
            return queued_task

        try:
            result = service.refresh_whatsapp_approval_binding_truth(
                account_key,
                int(binding_index),
                source='manual_truth_refresh',
                timeout_seconds=foreground_timeout_seconds,
                request_id=request_id,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            reason = str(detail.get('reason') or '').strip()
            foreground_defer_reasons = {
                'runtime_actor_busy',
                'truth_acquisition_in_progress',
                'binding_operation_in_progress',
                'official_fast_truth_unavailable',
                'foreground_truth_unavailable',
            }
            if exc.status_code != 409 or reason not in foreground_defer_reasons:
                raise
            should_enqueue = reason not in {'truth_acquisition_in_progress', 'binding_operation_in_progress'}
            queued_task = _enqueue_deferred_truth_refresh(reason or 'manual_truth_refresh_deferred', fallback=True) if should_enqueue else {}
            result = {
                'account_key': str(account_key or '').strip(),
                'binding_index': int(binding_index),
                'probe_mode': 'foreground_deferred_background',
                'ok': bool(queued_task) or not should_enqueue,
                'refresh_deferred_in_progress': reason in {'truth_acquisition_in_progress', 'binding_operation_in_progress'},
                'refresh_pending_background': bool(queued_task),
                'queued_refresh': queued_task,
                'direct_probe_error': reason or str(exc.detail or exc),
                'active_operation': detail.get('active_operation'),
                'active_binding_index': detail.get('active_binding_index'),
                'foreground_timeout_seconds': foreground_timeout_seconds,
            }
        except Exception as exc:
            queued_task = _enqueue_deferred_truth_refresh('sync_exception')
            result = {
                'account_key': str(account_key or '').strip(),
                'binding_index': int(binding_index),
                'probe_mode': 'fast_cached_background',
                'ok': bool(queued_task),
                'refresh_pending_background': bool(queued_task),
                'queued_refresh': queued_task,
                'direct_probe_error': str(exc),
            }
        result = dict(result or {})
        result.setdefault('account_key', str(account_key or '').strip())
        result.setdefault('binding_index', int(binding_index))
        committed_pending_count = service._approval_truth_committed_pending_count(result)
        permission_reason = str(result.get('reason_code') or result.get('permission_status') or '').strip()
        permission_truth_written = bool(
            result.get('current_truth_written')
            and str(result.get('trust_status') or '').strip() == 'PERMISSION_DENIED'
            and permission_reason in {'not_group_member', 'not_group_admin'}
        )
        group_banned_truth_written = bool(
            result.get('current_truth_written')
            and str(result.get('trust_status') or '').strip() == 'GROUP_BANNED'
            and permission_reason == 'group_banned'
        )
        authoritative_truth_written = bool(
            (bool(result.get('current_truth_written')) and committed_pending_count is not None)
            or permission_truth_written
            or group_banned_truth_written
        )
        if not authoritative_truth_written and not result.get('refresh_pending_background') and not result.get('refresh_deferred_in_progress'):
            failure_class = str(result.get('failure_class') or service._approval_truth_failure_class(result) or '').strip()
            result['ok'] = False
            result['authoritative_truth_written'] = False
            recover_identity = failure_class in {'BUDGET_EXHAUSTED', 'IDENTITY_UNRESOLVED', 'IDENTITY_MISMATCH'}
            recoverable_failure = failure_class in {
                'BUDGET_EXHAUSTED',
                'IDENTITY_UNRESOLVED',
                'IDENTITY_MISMATCH',
                'INTERNAL_ERROR',
                'INDEPENDENT_VERIFY_UNAVAILABLE',
                'SYNC_INCONCLUSIVE',
                'EMPTY_UNVERIFIED',
                'RUNTIME_UNHEALTHY',
                'SOFT_RELOAD_FAILED',
            }
            if recoverable_failure:
                queued_task = _enqueue_deferred_truth_refresh(failure_class.lower(), fallback=True, recover_identity=recover_identity)
                result['refresh_pending_background'] = bool(queued_task)
                result['queued_refresh'] = queued_task
                result['recovery_mode'] = 'identity_probe_then_truth_refresh' if recover_identity else 'background_truth_refresh'
                if queued_task:
                    result['ok'] = True
                    result['accepted'] = True
                    result['message'] = '已转后台刷新，当前读数保持不变；取得新真值后页面会自动更新。'
        elif authoritative_truth_written:
            result['authoritative_truth_written'] = True
        if bool(result.get('background_refresh_skipped')):
            return result
        try:
            _ops_hot_read_cache_invalidate('approval_realtime:')
            _ops_hot_read_cache_invalidate('approval_accounts:')
            _ops_hot_read_cache_invalidate('approval_batch_queue:')
            _ops_hot_read_cache_invalidate('production_ops_daemon:')
            _ops_hot_read_cache_invalidate('official_group_bridge_summary:')
            refreshed_truth = dict(result.get('approval_queue_truth') or {}) if isinstance(result.get('approval_queue_truth'), dict) else None
            runtime_snapshot_started = time.perf_counter()
            binding_runtime = service._get_whatsapp_approval_binding_runtime_snapshot(account_key, int(binding_index))
            result['runtime_snapshot_ms'] = int(max((time.perf_counter() - runtime_snapshot_started) * 1000.0, 0.0))
            if isinstance(binding_runtime, dict):
                result = _merge_binding_runtime_result(result, binding_runtime)
                if refreshed_truth is not None:
                    result['approval_queue_truth'] = refreshed_truth
                    merged_runtime = dict(result.get('binding_runtime') or {})
                    merged_runtime['approval_queue_truth'] = refreshed_truth
                    result['binding_runtime'] = merged_runtime
                elif not isinstance(result.get('approval_queue_truth'), dict) and isinstance(binding_runtime.get('approval_queue_truth'), dict):
                    result['approval_queue_truth'] = dict(binding_runtime.get('approval_queue_truth') or {})
            realtime_row = (
                dict(account_runtime)
                if result.get('refresh_pending_background') and isinstance(account_runtime, dict) and account_runtime
                else service._get_whatsapp_approval_account_runtime_row_lightweight(account_key)
            )
            realtime_ingest_started = time.perf_counter()
            _approval_realtime_store().ingest_snapshot({'rows': [realtime_row]}, source='manual_truth_refresh')
            result['realtime_ingest_ms'] = int(max((time.perf_counter() - realtime_ingest_started) * 1000.0, 0.0))
            merged_runtime = dict(result.get('binding_runtime') or {})
            operation_state = merged_runtime.get('operation_state') if isinstance(merged_runtime.get('operation_state'), dict) else None
            if isinstance(operation_state, dict) and operation_state.get('active') and str(operation_state.get('operation') or '').strip() in {'truth_refresh', 'probe_refresh'}:
                merged_runtime.pop('operation_state', None)
                result = {**result, 'binding_runtime': merged_runtime}
        except Exception as exc:
            _record_whatsapp_approval_route_warning('approval_realtime_ingest_failed', account_key=account_key, binding_index=binding_index, source='manual_truth_refresh', exc=exc)
        return result

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/bindings/{binding_index}/truth-refresh/internal')
    def ops_whatsapp_approval_binding_truth_refresh_internal(account_key: str, binding_index: int):
        service._ensure_registration_group_cutover_enabled(account_key, binding_index, 'truth_refresh')
        internal_source = 'production_ops_daemon_official_truth_refresh'
        preflight_skip = service._background_approval_truth_refresh_preflight(
            account_key=account_key,
            binding_index=int(binding_index),
            source=internal_source,
            cooldown_seconds=60.0,
            fresh_seconds=APPROVAL_TRUTH_PENDING_TTL_SECONDS,
            reserve=True,
        )
        if preflight_skip:
            result = preflight_skip
        else:
            queued = service.enqueue_whatsapp_approval_task(
                account_key=account_key,
                binding_index=int(binding_index),
                operation='truth_refresh',
                input_payload={
                    'request_id': create_id('approval_op'),
                    'source': internal_source,
                    'timeout_seconds': 5.0,
                },
                priority=80,
                timeout_seconds=8,
                max_retries=1,
                created_by=internal_source,
            )
            result = {
                'ok': True,
                'queued': True,
                'refresh_pending_background': True,
                'task_id': queued.get('task_id'),
                'status': queued.get('status') or 'pending',
                'queued_refresh': queued,
            }
        result = dict(result or {})
        result.setdefault('account_key', str(account_key or '').strip())
        result.setdefault('binding_index', int(binding_index))
        if bool(result.get('background_refresh_skipped')):
            return result
        return result

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/bindings/{binding_index}/probe-refresh')
    def ops_whatsapp_approval_binding_probe_refresh(account_key: str, binding_index: int, request: Request):
        service._require_whatsapp_approval_account_access(account_key, _request_session_user(request))
        service._ensure_registration_group_cutover_enabled(account_key, binding_index, 'probe_refresh')
        probe_mode = 'fast'
        try:
            result = service.refresh_whatsapp_approval_binding_probe(account_key, int(binding_index), probe_mode=probe_mode)
        except HTTPException:
            raise
        except Exception as exc:
            queued_task: Dict[str, Any] = {}
            try:
                queued_task = service.enqueue_whatsapp_approval_task(
                    account_key=account_key,
                    binding_index=int(binding_index),
                    operation='probe_refresh',
                    input_payload={
                        'request_id': str(request.headers.get('X-Request-ID') or request.headers.get('X-Correlation-ID') or '').strip() or create_id('approval_op'),
                        'probe_mode': 'strict',
                        'source': 'probe_refresh_fast_fallback',
                    },
                    timeout_seconds=45,
                    max_retries=2,
                    created_by=str((_request_session_user(request) or {}).get('username') or '').strip(),
                )
            except Exception as enqueue_exc:
                _record_whatsapp_approval_route_warning('approval_probe_background_enqueue_failed', account_key=account_key, binding_index=binding_index, source='manual_probe', exc=enqueue_exc)
            result = {
                'account_key': str(account_key or '').strip(),
                'binding_index': int(binding_index),
                'probe_mode': probe_mode,
                'ok': False,
                'refresh_deferred': True,
                'background_queued': bool(queued_task),
                'background_task': queued_task,
                'probe': {
                    'ok': False,
                    'status': 'probe_refresh_deferred',
                    'error_message': str(exc),
                    'queue_readable': None,
                },
            }
        binding_runtime = dict(result.get('binding_runtime') or {})
        operation_state = binding_runtime.get('operation_state') if isinstance(binding_runtime.get('operation_state'), dict) else None
        if isinstance(operation_state, dict) and operation_state.get('active') and str(operation_state.get('operation') or '').strip() == 'probe_refresh':
            binding_runtime.pop('operation_state', None)
            result = {**result, 'binding_runtime': binding_runtime}
        try:
            latest_binding_runtime = service._get_whatsapp_approval_binding_runtime_snapshot(account_key, int(binding_index))
            if isinstance(latest_binding_runtime, dict):
                result = _merge_binding_runtime_result(result, latest_binding_runtime)
            _approval_realtime_store().ingest_snapshot({'rows': [service._get_whatsapp_approval_account_runtime_row_lightweight(account_key)]}, source='manual_probe')
        except Exception as exc:
            _record_whatsapp_approval_route_warning('approval_realtime_ingest_failed', account_key=account_key, binding_index=binding_index, source='manual_probe', exc=exc)
        return result

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/bindings/{binding_index}/rebuild-identity')
    def ops_whatsapp_approval_binding_rebuild_identity(account_key: str, binding_index: int, request: Request):
        current_user = _request_session_user(request)
        request_context = {
            'request_id': str(request.headers.get('X-Request-ID') or request.headers.get('X-Correlation-ID') or '').strip() or create_id('approval_op'),
            'path': str(request.url.path or ''),
            'method': str(request.method or 'POST').upper(),
        }
        result = service.run_whatsapp_approval_task_sync(
            account_key=account_key,
            binding_index=binding_index,
            operation='rebuild_identity',
            input_payload={'current_user': current_user, 'request_context': request_context},
            timeout_seconds=60,
            max_retries=1,
            created_by=str((current_user or {}).get('username') or (current_user or {}).get('user_id') or '').strip(),
            wait_timeout_seconds=90.0,
        )
        try:
            binding_runtime = service._get_whatsapp_approval_binding_runtime_snapshot(account_key, int(binding_index))
            if isinstance(binding_runtime, dict):
                result = _merge_binding_runtime_result(result, binding_runtime)
            _approval_realtime_store().ingest_snapshot({'rows': [service._get_whatsapp_approval_account_runtime_row_lightweight(account_key)]}, source='manual_rebuild_identity')
        except Exception as exc:
            _record_whatsapp_approval_route_warning('approval_realtime_ingest_failed', account_key=account_key, binding_index=binding_index, source='manual_rebuild_identity', exc=exc)
        return result

    @app.delete('/api/ops/whatsapp-approval-accounts/{account_key}/bindings/{binding_index}')
    def ops_whatsapp_approval_binding_delete(account_key: str, binding_index: int, request: Request):
        result = service.delete_whatsapp_approval_account_binding(
            account_key,
            int(binding_index),
            current_user=_request_session_user(request),
        )
        try:
            account = result.get('account') if isinstance(result, dict) else None
            if isinstance(account, dict):
                _approval_realtime_seed_full_snapshot_before_partial_update()
                _approval_realtime_store().ingest_snapshot({'rows': [account]}, source='approval_binding_delete')
        except Exception as exc:
            _record_whatsapp_approval_route_warning('approval_realtime_ingest_failed', account_key=account_key, binding_index=binding_index, source='approval_binding_delete', exc=exc)
        _ops_hot_read_cache_invalidate('approval_realtime:')
        _ops_hot_read_cache_invalidate('approval_accounts:')
        _ops_hot_read_cache_invalidate('approval_batch_queue:')
        _ops_hot_read_cache_invalidate('production_ops_daemon:')
        _ops_hot_read_cache_invalidate('official_group_bridge_summary:')
        return result

    @app.get('/api/ops/mcn-region-options')
    def ops_mcn_region_options(include_disabled: bool = False):
        return service.list_mcn_region_options(include_disabled=include_disabled)

    @app.put('/api/ops/mcn-region-options')
    def ops_mcn_region_options_update(payload: McnRegionOptionsUpdateRequest, request: Request):
        _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN)
        return service.update_mcn_region_options(payload)

    @app.get('/api/ops/whatsapp-approval-area-options')
    def ops_whatsapp_approval_area_options():
        return service.list_whatsapp_approval_area_options()

    @app.post('/api/ops/whatsapp-approval-area-options')
    def ops_whatsapp_approval_area_options_update(payload: WhatsAppApprovalAreaOptionsUpdateRequest):
        return service.update_whatsapp_approval_area_options(payload)

    @app.get('/api/ops/whatsapp-approval-candidates')
    def ops_whatsapp_approval_candidates(request: Request):
        return service.list_whatsapp_approval_candidates(current_user=_request_session_user(request))

    @app.get('/api/ops/whatsapp-approval-candidates/summary')
    def ops_whatsapp_approval_candidates_summary(request: Request):
        payload = service.list_whatsapp_approval_candidates(current_user=_request_session_user(request)) or {}
        return {
            'summary': payload.get('summary') or {},
            'verifier_framework': payload.get('verifier_framework') or {},
        }

    @app.get('/api/ops/registration-group-approval-batch-members')
    def ops_registration_group_approval_batch_members(
        approval_run_id: Optional[str] = None,
        registration_group: Optional[str] = None,
        group_type: Optional[str] = None,
        area: Optional[str] = None,
        keyword: Optional[str] = None,
        registration_status: Optional[str] = None,
        approved_date: Optional[str] = None,
        approved_date_start: Optional[str] = None,
        approved_date_end: Optional[str] = None,
        member_ids: Optional[str] = None,
        limit: int = 30,
        page: int = 1,
    ):
        return service.list_registration_group_approval_batch_members(
            approval_run_id=approval_run_id,
            registration_group=registration_group,
            group_type=group_type,
            area=area,
            keyword=keyword,
            registration_status=registration_status,
            approved_date=approved_date,
            approved_date_start=approved_date_start,
            approved_date_end=approved_date_end,
            member_ids=member_ids,
            limit=limit,
            page=page,
        )

    @app.get('/api/ops/registration-group-approval-batch-members/summary')
    def ops_registration_group_approval_batch_members_summary(
        group_type: Optional[str] = None,
        approved_date: Optional[str] = None,
        approved_date_start: Optional[str] = None,
        approved_date_end: Optional[str] = None,
    ):
        return service.registration_group_approval_batch_members_summary(
            group_type=group_type,
            approved_date=approved_date,
            approved_date_start=approved_date_start,
            approved_date_end=approved_date_end,
        )

    @app.get('/api/ops/registration-group-approval-batch-members/export')
    def ops_registration_group_approval_batch_members_export(
        format: str = 'xlsx',
        approval_run_id: Optional[str] = None,
        registration_group: Optional[str] = None,
        group_type: Optional[str] = None,
        area: Optional[str] = None,
        keyword: Optional[str] = None,
        registration_status: Optional[str] = None,
        approved_date: Optional[str] = None,
        approved_date_start: Optional[str] = None,
        approved_date_end: Optional[str] = None,
        member_ids: Optional[str] = None,
        limit: int = 5000,
    ):
        normalized_format = str(format or 'xlsx').strip().lower()
        if normalized_format == 'csv':
            content = service.export_registration_group_approval_batch_members_csv(
                approval_run_id=approval_run_id,
                registration_group=registration_group,
                group_type=group_type,
                area=area,
                keyword=keyword,
                registration_status=registration_status,
                approved_date=approved_date,
                approved_date_start=approved_date_start,
                approved_date_end=approved_date_end,
                member_ids=member_ids,
                limit=limit,
            )
            filename = service.registration_group_approval_batch_members_export_filename(
                extension='csv',
                approval_run_id=approval_run_id,
                registration_group=registration_group,
                group_type=group_type,
                area=area,
                keyword=keyword,
                registration_status=registration_status,
                approved_date=approved_date,
                approved_date_start=approved_date_start,
                approved_date_end=approved_date_end,
                member_ids=member_ids,
                limit=limit,
            )
            headers = {'Content-Disposition': f'attachment; filename="{filename}"'}
            return StreamingResponse(iter([content]), media_type='text/csv; charset=utf-8', headers=headers)
        if normalized_format != 'xlsx':
            raise HTTPException(status_code=400, detail='unsupported export format')
        content = service.export_registration_group_approval_batch_members_xlsx(
            approval_run_id=approval_run_id,
            registration_group=registration_group,
            group_type=group_type,
            area=area,
            keyword=keyword,
            registration_status=registration_status,
            approved_date=approved_date,
            approved_date_start=approved_date_start,
            approved_date_end=approved_date_end,
            member_ids=member_ids,
            limit=limit,
        )
        filename = service.registration_group_approval_batch_members_export_filename(
            extension='xlsx',
            approval_run_id=approval_run_id,
            registration_group=registration_group,
            group_type=group_type,
            area=area,
            keyword=keyword,
            registration_status=registration_status,
            approved_date=approved_date,
            approved_date_start=approved_date_start,
            approved_date_end=approved_date_end,
            member_ids=member_ids,
            limit=limit,
        )
        headers = {'Content-Disposition': f'attachment; filename="{filename}"'}
        return StreamingResponse(iter([content]), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers=headers)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}')
    def ops_whatsapp_approval_account_update(account_key: str, payload: WhatsAppApprovalAccountUpdateRequest, request: Request):
        result = service.update_whatsapp_approval_account(account_key, payload, current_user=_request_session_user(request))
        try:
            account = result.get('account') if isinstance(result, dict) else None
            if isinstance(account, dict):
                _approval_realtime_seed_full_snapshot_before_partial_update()
                _approval_realtime_store().ingest_snapshot({'rows': [account]}, source='approval_account_update')
        except Exception as exc:
            _record_whatsapp_approval_route_warning('approval_realtime_ingest_failed', account_key=account_key, binding_index=-1, source='approval_account_update', exc=exc)
        _ops_hot_read_cache_invalidate('approval_realtime:')
        _ops_hot_read_cache_invalidate('approval_accounts:')
        _ops_hot_read_cache_invalidate('approval_batch_queue:')
        _ops_hot_read_cache_invalidate('production_ops_daemon:')
        _ops_hot_read_cache_invalidate('official_group_bridge_summary:')
        return result

    @app.delete('/api/ops/whatsapp-approval-accounts/{account_key}')
    def ops_whatsapp_approval_account_delete(account_key: str, request: Request):
        result = service.delete_whatsapp_approval_account(account_key, current_user=_request_session_user(request))
        try:
            payload = service.list_whatsapp_approval_accounts(lightweight=True, include_options=False) or {}
            _approval_realtime_store().ingest_snapshot(payload, source='approval_account_delete')
        except Exception as exc:
            _record_whatsapp_approval_route_warning('approval_realtime_ingest_failed', account_key=account_key, binding_index=-1, source='approval_account_delete', exc=exc)
        _ops_hot_read_cache_invalidate('approval_realtime:')
        _ops_hot_read_cache_invalidate('approval_accounts:')
        _ops_hot_read_cache_invalidate('approval_batch_queue:')
        _ops_hot_read_cache_invalidate('production_ops_daemon:')
        _ops_hot_read_cache_invalidate('official_group_bridge_summary:')
        return result

    @app.get('/api/ops/official-group-bridge-summary')
    def ops_official_group_bridge_summary():
        return _official_group_bridge_summary_payload()

    @app.get('/api/ops/official-group-bridge-summary/summary')
    def ops_official_group_bridge_summary_summary():
        def build_summary() -> Dict[str, Any]:
            payload = _official_group_bridge_summary_payload() or {}
            health = payload.get('health') or {}
            summary = payload.get('summary') or {}
            by_target_group = summary.get('by_target_group') or {}
            today_approved_counts = service._approval_batch_member_today_counts()
            official_today_approved_count = int(today_approved_counts.get('official_group') or 0)
            return {
                'configured': payload.get('configured'),
                'health': {
                    'status': health.get('status'),
                    'mode': health.get('mode'),
                },
                'summary': {
                    'pending_count': summary.get('pending_count'),
                    'resolved_count': summary.get('resolved_count'),
                    'approved_count': summary.get('approved_count'),
                    'today_approved_count': official_today_approved_count,
                    'approved_today_count': official_today_approved_count,
                    'today_approved_count_source': 'registration_group_approval_batch_members',
                    'today_approved_counts': today_approved_counts,
                    'bridge_today_approved_count': summary.get('today_approved_count'),
                    'bridge_approved_today_count': summary.get('approved_today_count'),
                    'today_created_count': summary.get('today_created_count'),
                    'pending_timeout_over_1h_count': summary.get('pending_timeout_over_1h_count'),
                    'target_group_count': len(by_target_group) if isinstance(by_target_group, dict) else 0,
                    'checked_at': summary.get('checked_at'),
                    'by_target_group': by_target_group if isinstance(by_target_group, dict) else {},
                },
            }

        return _ops_hot_read_cache_get_or_set(
            'official_group_bridge_summary:summary',
            12.0,
            build_summary,
        )

    @app.post('/api/ops/production-ops-daemon')
    def ops_production_ops_daemon_update(payload: ProductionOpsDaemonConfigUpdateRequest):
        result = service.update_production_ops_daemon_config(payload)
        _ops_hot_read_cache_invalidate('production_ops_daemon:')
        _ops_hot_read_cache_invalidate('approval_batch_queue:')
        return result

    @app.get('/api/ops/guild-executors/health')
    def ops_guild_executors_health():
        return service.guild_executor_health()

    @app.get('/api/ops/guild-anchor-daily-stats')
    def ops_guild_anchor_daily_stats(
        request: Request,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        app: Optional[str] = None,
    ):
        _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return service.list_guild_anchor_daily_stats(date_from=date_from, date_to=date_to, app_name=app)

    @app.post('/api/ops/guild-anchor-daily-stats')
    def ops_guild_anchor_daily_stats_refresh(
        request: Request,
        stat_date: Optional[str] = None,
        force: bool = False,
    ):
        _require_ops_user(request, role=OPS_AUTH_ROLE_CUSTOMER_SERVICE)
        return service.start_guild_anchor_daily_stats_refresh(stat_date=stat_date, force=force, source='manual')

    @app.post('/api/ops/submissions/{submission_id}/retry-bind')
    def ops_retry_bind_submission(submission_id: str):
        return service.retry_bind_submission(submission_id)

    @app.post('/api/ops/submissions/{submission_id}/retry-crm')
    def ops_retry_crm_submission(submission_id: str):
        return service.retry_crm_submission(submission_id)

    @app.post('/api/ops/submissions/{submission_id}/resubmit')
    def ops_resubmit_submission(submission_id: str, payload: SubmissionResubmitRequest):
        return service.resubmit_corrected_submission(submission_id, payload)

    @app.get('/api/ops/exception-queue')
    def ops_exception_queue():
        return service.exception_queue()

    @app.get('/api/ops/sla-summary')
    def ops_sla_summary():
        return service.sla_summary()

    @app.get('/api/ops/guild-executors/{guild_name}')
    def ops_guild_executor(guild_name: str):
        return service.get_guild_executor(guild_name)

    @app.delete('/api/ops/guild-executors/{guild_name}')
    def ops_guild_executor_delete(guild_name: str):
        return service.delete_guild_executor(guild_name)

    @app.post('/api/ops/guild-executors/{guild_name}')
    def ops_guild_executor_update(guild_name: str, payload: GuildExecutorUpdateRequest):
        return service.update_guild_executor(guild_name, payload)

    @app.get('/api/ops/timo-guild-executors/{guild_name}')
    def ops_timo_guild_executor(guild_name: str):
        return service.get_timo_guild_executor(guild_name)

    @app.delete('/api/ops/timo-guild-executors/{guild_name}')
    def ops_timo_guild_executor_delete(guild_name: str):
        result = service.delete_timo_guild_executor(guild_name)
        _ops_hot_read_cache_invalidate('timo_intake_guilds:')
        _ops_hot_read_cache_invalidate('timo_guild_executors:')
        return result

    @app.post('/api/ops/timo-guild-executors/{guild_name}')
    def ops_timo_guild_executor_update(guild_name: str, payload: TimoGuildExecutorUpdateRequest):
        result = service.update_timo_guild_executor(guild_name, payload)
        _ops_hot_read_cache_invalidate('timo_intake_guilds:')
        _ops_hot_read_cache_invalidate('timo_guild_executors:')
        return result

    @app.get('/api/ops/sogo-guild-executors/{guild_name}')
    @app.get('/api/ops/sugo-guild-executors/{guild_name}')
    def ops_sogo_guild_executor(guild_name: str):
        return service.get_sogo_guild_executor(guild_name)

    @app.delete('/api/ops/sogo-guild-executors/{guild_name}')
    @app.delete('/api/ops/sugo-guild-executors/{guild_name}')
    def ops_sogo_guild_executor_delete(guild_name: str):
        return service.delete_sogo_guild_executor(guild_name)

    @app.post('/api/ops/sogo-guild-executors/{guild_name}')
    @app.post('/api/ops/sugo-guild-executors/{guild_name}')
    def ops_sogo_guild_executor_update(guild_name: str, payload: SugoGuildExecutorUpdateRequest):
        return service.update_sogo_guild_executor(guild_name, payload)

    @app.get("/api/ops/next-bind-task")
    def ops_next_bind_task():
        return service.ops_next_bind_task()

    @app.get("/api/ops/next-bind-task/summary")
    def ops_next_bind_task_summary():
        payload = service.ops_next_bind_task() or {}
        row = payload.get('row') or {}
        return {
            'kind': payload.get('kind'),
            'row': {
                'lead_id': row.get('lead_id'),
                'task_id': row.get('task_id'),
                'current_status': row.get('current_status'),
                'updated_at': row.get('updated_at'),
                'app_name': row.get('app_name'),
                'dept_name': row.get('dept_name'),
                'registration_group': row.get('pendaftaran_group'),
            } if row else None,
        }

    @app.get("/api/ops/next-group-task")
    def ops_next_group_task():
        return service.ops_next_group_task()

    @app.get("/api/ops/next-group-task/summary")
    def ops_next_group_task_summary():
        payload = service.ops_next_group_task() or {}
        row = payload.get('row') or {}
        return {
            'kind': payload.get('kind'),
            'row': {
                'lead_id': row.get('lead_id'),
                'task_id': row.get('task_id'),
                'current_status': row.get('current_status'),
                'updated_at': row.get('updated_at'),
                'app_name': row.get('app_name'),
                'dept_name': row.get('dept_name'),
                'registration_group': row.get('pendaftaran_group'),
            } if row else None,
        }

    @app.get("/api/ops/next-action")
    def ops_next_action():
        return service.ops_next_action()

    @app.get("/api/ops/next-action/summary")
    def ops_next_action_summary():
        payload = service.ops_next_action() or {}
        row = payload.get('row') or {}
        return {
            'kind': payload.get('kind'),
            'score': payload.get('score'),
            'reason': payload.get('reason'),
            'row': {
                'lead_id': row.get('lead_id'),
                'task_id': row.get('task_id'),
                'current_status': row.get('current_status'),
                'updated_at': row.get('updated_at'),
                'app_name': row.get('app_name'),
                'dept_name': row.get('dept_name'),
                'registration_group': row.get('pendaftaran_group'),
            } if row else None,
        }

    @app.get("/api/ops/operator-notifications")
    def ops_operator_notifications(status: Optional[str] = None, query: Optional[str] = None):
        return service.operator_notifications(status=status, query=query)

    @app.post("/api/ops/operator-notifications/{notification_id}/read")
    def ops_operator_notification_read(notification_id: str, payload: NotificationReadRequest = Body(...)):
        return service.mark_operator_notification_read(notification_id, read_by=payload.read_by)

    @app.get("/api/ops/parser-quality-summary")
    def ops_parser_quality_summary():
        return service.parser_quality_summary()

    @app.post("/api/ops/approval-batches/evaluate")
    def ops_approval_batches_evaluate(payload: ApprovalBatchEvaluateRequest):
        return service.evaluate_approval_batch(payload)

    @app.get("/api/ops/approval-batch-queue")
    def ops_approval_batch_queue():
        def build_payload() -> Dict[str, Any]:
            payload = service.approval_batch_queue()
            payload['approval_truth_self_heal'] = {
                'ok': True,
                'skipped': True,
                'reason': 'read_endpoint_snapshot_only',
            }
            return payload

        return _ops_hot_read_cache_get_or_set('approval_batch_queue:full', 12.0, build_payload)

    @app.get("/api/ops/approval-batch-queue/summary")
    def ops_approval_batch_queue_summary():
        def _numeric(value: Any, default: int = 0) -> int:
            try:
                if value is None:
                    return default
                return int(value)
            except (TypeError, ValueError):
                return default

        def _summary_target_key(row: Dict[str, Any]) -> str:
            for key in ('target_group', 'group_id', 'binding_link', 'registration_group', 'group_name', 'target_group_label'):
                value = str(row.get(key) or '').strip().lower()
                if value:
                    return value
            return ''

        def _trim_rows(rows: Any) -> list[Dict[str, Any]]:
            by_key: Dict[str, Dict[str, Any]] = {}
            sequence = 0
            for row in list(rows or []):
                if not isinstance(row, dict):
                    continue
                pending_count = _numeric(row.get('pending_count'), _numeric(row.get('release_count')))
                trimmed_row = {
                    'approval_scope': row.get('approval_scope'),
                    'registration_group': row.get('registration_group'),
                    'group_name': row.get('group_name'),
                    'target_group_label': row.get('target_group_label'),
                    'target_group': row.get('target_group'),
                    'group_id': row.get('group_id'),
                    'binding_link': row.get('binding_link'),
                    'pending_count': pending_count,
                    'configured_binding_count': 1,
                    '_sequence': sequence,
                }
                sequence += 1
                key = _summary_target_key(trimmed_row) or f'row:{sequence}'
                existing = by_key.get(key)
                if existing is None:
                    by_key[key] = trimmed_row
                    continue
                existing['pending_count'] = max(_numeric(existing.get('pending_count')), pending_count)
                existing['configured_binding_count'] = _numeric(existing.get('configured_binding_count')) + 1
                for display_key in ('registration_group', 'group_name', 'target_group_label', 'target_group', 'group_id', 'binding_link'):
                    if not existing.get(display_key) and trimmed_row.get(display_key):
                        existing[display_key] = trimmed_row.get(display_key)
            deduped = sorted(by_key.values(), key=lambda item: int(item.get('_sequence') or 0))
            for item in deduped:
                item.pop('_sequence', None)
            return deduped

        def _summary(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
            configured_binding_count = sum(_numeric(row.get('configured_binding_count'), 1) for row in rows)
            unique_group_count = len(rows)
            return {
                'monitored_group_count': unique_group_count,
                'unique_group_count': unique_group_count,
                'configured_binding_count': configured_binding_count,
                'pending_count': sum(_numeric(row.get('pending_count')) for row in rows),
            }

        def _truthy_enabled(value: Any, default: bool = True) -> bool:
            if value is None:
                return default
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            text = str(value).strip().lower()
            if text in {'0', 'false', 'no', 'off', 'disabled'}:
                return False
            if text in {'1', 'true', 'yes', 'on', 'enabled'}:
                return True
            return default

        def _target_tokens(*values: Any) -> set[str]:
            tokens: set[str] = set()
            for value in values:
                text = str(value or '').strip()
                if text:
                    tokens.add(text.lower())
            return tokens

        def _current_monitor_targets() -> tuple[set[str], Dict[str, set[str]]]:
            configured_scopes: set[str] = set()
            active_targets: Dict[str, set[str]] = {'registration_group': set(), 'official_group': set()}
            try:
                current_accounts_payload = service._approval_batch_queue_accounts_payload(
                    production_ops=service._production_ops_daemon_snapshot_light()
                )
            except Exception:
                return configured_scopes, active_targets
            current_account_rows = (current_accounts_payload.get('rows') or current_accounts_payload.get('accounts')) if isinstance(current_accounts_payload, dict) else None
            if not isinstance(current_account_rows, list):
                return configured_scopes, active_targets
            for account in current_account_rows:
                if not isinstance(account, dict):
                    continue
                scope = str(account.get('responsible_type') or '').strip()
                if scope not in active_targets:
                    continue
                configured_scopes.add(scope)
                if not _truthy_enabled(account.get('enabled'), True):
                    continue
                bindings = account.get('group_link_bindings')
                if not isinstance(bindings, list):
                    bindings = []
                for binding in bindings:
                    if not isinstance(binding, dict):
                        continue
                    if not _truthy_enabled(binding.get('enabled'), True):
                        continue
                    active_targets[scope].update(_target_tokens(
                        binding.get('group_name'),
                        binding.get('registration_group'),
                        binding.get('target_group_label'),
                        binding.get('target_group'),
                        binding.get('group_id'),
                        binding.get('link'),
                    ))
            return configured_scopes, active_targets

        def _row_matches_active_target(row: Dict[str, Any], active_targets: set[str]) -> bool:
            if not active_targets:
                return False
            return bool(active_targets & _target_tokens(
                row.get('registration_group'),
                row.get('group_name'),
                row.get('target_group_label'),
                row.get('target_group'),
                row.get('group_id'),
                row.get('binding_link'),
            ))

        def _filter_rows_to_active_targets(rows: Any, active_targets: set[str]) -> list[Dict[str, Any]]:
            filtered: list[Dict[str, Any]] = []
            for row in list(rows or []):
                if isinstance(row, dict) and _row_matches_active_target(row, active_targets):
                    filtered.append(row)
            return filtered

        def _merge_preferred_rows(preferred_rows: Any, fallback_rows: Any) -> list[Dict[str, Any]]:
            preferred = [row for row in list(preferred_rows or []) if isinstance(row, dict)]
            preferred_keys = {_summary_target_key(row) for row in preferred}
            preferred_keys.discard('')
            fallback = [
                row
                for row in list(fallback_rows or [])
                if isinstance(row, dict) and (_summary_target_key(row) not in preferred_keys)
            ]
            return [*fallback, *preferred]

        def _rows_from_daemon_cycles(cycles: Any, approval_scope: str, active_targets: set[str]) -> list[Dict[str, Any]]:
            def _dict_value(value: Any) -> Dict[str, Any]:
                return value if isinstance(value, dict) else {}

            def _numeric_or_none(value: Any) -> Optional[int]:
                try:
                    if value is None:
                        return None
                    return int(value)
                except (TypeError, ValueError):
                    return None

            def _cycle_pending_count(cycle: Dict[str, Any], payload: Dict[str, Any]) -> int:
                approval_queue_truth = _dict_value(cycle.get('approval_queue_truth'))
                current_truth = _dict_value(approval_queue_truth.get('current_truth'))
                for candidate in (
                    current_truth.get('pending_count'),
                    approval_queue_truth.get('pending_count') if current_truth else None,
                ):
                    value = _numeric_or_none(candidate)
                    if value is not None:
                        return value

                truth_state = _dict_value(cycle.get('truth_state'))
                truth_payload = _dict_value(truth_state.get('payload'))
                truth_status = str(truth_state.get('status') or '').strip().lower()
                if truth_status in {'confirmed_pending', 'confirmed_empty'}:
                    for candidate in (truth_state.get('pending_count'), truth_payload.get('pending_count')):
                        value = _numeric_or_none(candidate)
                        if value is not None:
                            return value

                for candidate in (cycle.get('pending_count'), payload.get('pending_count')):
                    value = _numeric_or_none(candidate)
                    if value is not None:
                        return value
                return 0

            rows: list[Dict[str, Any]] = []
            if not active_targets:
                return rows
            for cycle in list(cycles or []):
                if not isinstance(cycle, dict):
                    continue
                monitor_target = _dict_value(cycle.get('monitor_target'))
                decision_group_state = _dict_value(cycle.get('decision_group_state'))
                payload = _dict_value(decision_group_state.get('payload'))
                truth_state = _dict_value(cycle.get('truth_state'))
                truth_payload = _dict_value(truth_state.get('payload'))
                approval_queue_truth = _dict_value(cycle.get('approval_queue_truth'))
                current_truth = _dict_value(approval_queue_truth.get('current_truth'))
                group_name = (
                    monitor_target.get('group_name')
                    or truth_payload.get('group_name')
                    or current_truth.get('group_name')
                    or payload.get('group_name')
                    or monitor_target.get('registration_group')
                    or monitor_target.get('target_group')
                    or truth_payload.get('group_id')
                    or current_truth.get('group_id')
                    or payload.get('group_id')
                )
                row = {
                    'approval_scope': approval_scope,
                    'registration_group': monitor_target.get('registration_group') or group_name,
                    'group_name': group_name,
                    'target_group_label': group_name,
                    'target_group': monitor_target.get('target_group'),
                    'group_id': truth_payload.get('group_id') or current_truth.get('group_id') or payload.get('group_id') or monitor_target.get('group_id'),
                    'pending_count': _cycle_pending_count(cycle, payload),
                }
                if _row_matches_active_target(row, active_targets):
                    rows.append(row)
            return rows

        def build_summary_payload() -> Dict[str, Any]:
            payload: Dict[str, Any] = {}
            try:
                queue_payload = service.approval_batch_queue() or {}
                payload = {
                    'registration_groups': queue_payload.get('registration_groups') or [],
                    'official_groups': queue_payload.get('official_groups') or [],
                }
            except Exception:
                payload = {'registration_groups': [], 'official_groups': []}

            registration_groups = _trim_rows(payload.get('registration_groups'))
            official_groups = _trim_rows(payload.get('official_groups'))
            return {
                'registration_groups': registration_groups,
                'official_groups': official_groups,
                'registration_summary': _summary(registration_groups),
                'official_summary': _summary(official_groups),
            }

        return _ops_hot_read_cache_get_or_set('approval_batch_queue:summary', 12.0, build_summary_payload)

    app.include_router(
        create_streamer_analytics_router(
            db=db,
            require_ops_user=_require_ops_user,
            with_ops_shell_style=_with_ops_shell_style,
            super_admin_role=OPS_AUTH_ROLE_SUPER_ADMIN,
        )
    )
    app.include_router(create_report_router(service))

    app.include_router(
        create_timo_auth_station_public_router(
            db_path=service.db.db_path,
            station_token=timo_auth_station_token,
        )
    )
    app.include_router(
        create_timo_auth_station_router(
            db_path=service.db.db_path,
            station_token=timo_auth_station_token,
        )
    )

    app.include_router(
        create_growth_router(
            db=db,
            require_admin=lambda request: _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN),
        )
    )
    app.include_router(
        create_ad_experiment_router(
            db=db,
            require_admin=lambda request: _require_ops_user(request, role=OPS_AUTH_ROLE_ADMIN),
            meta_session=meta_ads_session,
            meta_access_token=meta_ads_access_token,
            meta_graph_root=f'{meta_ads_base_url.rstrip("/")}/{_normalize_meta_api_version(meta_ads_api_version)}',
            meta_business_ids=meta_ads_business_ids,
            meta_application_id=str(cfg.get('GROWTH_META_TUGAO_APPLICATION_ID') or os.getenv('GROWTH_META_TUGAO_APPLICATION_ID') or '1684703062404662'),
            meta_store_url=str(cfg.get('GROWTH_META_TUGAO_STORE_URL') or os.getenv('GROWTH_META_TUGAO_STORE_URL') or 'http://play.google.com/store/apps/details?id=com.timetrade.duitan'),
            meta_regional_identity_account_id=str(cfg.get('GROWTH_META_REGIONAL_IDENTITY_ACCOUNT_ID') or os.getenv('GROWTH_META_REGIONAL_IDENTITY_ACCOUNT_ID') or ''),
            meta_regional_beneficiary_id=str(cfg.get('GROWTH_META_REGIONAL_BENEFICIARY_ID') or os.getenv('GROWTH_META_REGIONAL_BENEFICIARY_ID') or ''),
            meta_regional_payer_id=str(cfg.get('GROWTH_META_REGIONAL_PAYER_ID') or os.getenv('GROWTH_META_REGIONAL_PAYER_ID') or ''),
        )
    )

    _assert_ops_api_permissions_complete(app)
    app.state.assert_ops_api_permissions_complete = lambda: _assert_ops_api_permissions_complete(app)

    return app


_GLOBAL_APP_BOOTSTRAP_DISABLED = any(
    str(os.getenv(name) or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    for name in ('MCN_DEDICATED_DB_WRITER_PROCESS', 'MCN_DISABLE_GLOBAL_APP_BOOTSTRAP')
)

app = None  # Bootstrapped by app.main after compatibility wiring.

__all__ = [name for name in globals() if not name.startswith('__')]
