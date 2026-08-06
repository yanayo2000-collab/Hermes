from __future__ import annotations

import sys as _sys
import types as _types
from functools import wraps as _wraps

from app import main_app as _app_module
from app import main_shared as _shared_module
from app import main_service_group_atmosphere as _service_group_atmosphere
from app import main_service_intake as _service_intake
from app import main_service_approval as _service_approval
from app import main_service_timo as _service_timo
from app import main_service_whatsapp as _service_whatsapp
from app import main_service_executor as _service_executor

# Route composition remains explicit in app.main_app, including
# create_streamer_analytics_router(...), while this module owns bootstrap.


def _export_module(module):
    for name in getattr(module, "__all__", ()):
        if name not in {"Service", "create_app", "app"}:
            globals()[name] = getattr(module, name)


_export_module(_shared_module)
_export_module(_app_module)


class Service(_service_group_atmosphere.GroupAtmosphereServiceMixin, _service_intake.IntakeServiceMixin, _service_approval.ApprovalServiceMixin, _service_timo.TimoServiceMixin, _service_whatsapp.WhatsAppServiceMixin, _service_executor.ExecutorServiceMixin):
    """Compatibility facade retaining the historic app.main.Service surface."""
    def __init__(self, db: Database, crm_adapter: Any = None, ocr_adapter: Any = None, lark_media_adapter: Any = None, lark_reply_adapter: Any = None, lark_reply_adapter_by_app_id: Optional[Dict[str, Any]] = None, media_cache_dir: Optional[str] = None, lark_default_app_name: Optional[str] = None, lark_default_dept_name: Optional[str] = None, current_lark_app_id: Optional[str] = None, auto_bind_simulation: bool = False, bind_simulator: Any = None, real_bind_executor: Any = None, registration_group_approval_executor: Any = None, official_group_approval_executor: Any = None, timo_guild_executor: Any = None, official_group_target_map: Optional[Dict[str, str]] = None, auto_bind_simulation_success_rate: float = 0.5, auto_bind_simulation_seed: Optional[int] = None, crm_base_url: Optional[str] = None, crm_username: Optional[str] = None, crm_login_error: Optional[str] = None, ingress_async_default: bool = False, ingress_worker_enabled: bool = False, ingress_worker_poll_interval: float = 0.5, ingress_worker_count: int = 1, ingress_rate_limit_per_minute: int = 600, external_call_rate_limit_per_minute: int = 300, require_invite_code: bool = False, crm_retry_delays_seconds: Optional[List[int]] = None, crm_retry_max_attempts: int = 3, bind_retry_max_attempts: int = 2, official_group_approval_webhook_url: Optional[str] = None, official_group_bridge_token: Optional[str] = None, group_atmosphere_scheduler_enabled: bool = False, group_atmosphere_scheduler_poll_interval_seconds: float = 30.0, group_atmosphere_candidate_translator: Any = None, group_atmosphere_translation_background_enabled: bool = False, guild_executor_proxy_region_urls: Optional[Dict[str, str]] = None, group_atmosphere_media_dir: Optional[str] = None, ops_intake_auto_clear_stale_feedback_enabled: bool = False, ops_intake_auto_clear_stale_feedback_poll_interval_seconds: float = 300.0, ops_intake_auto_clear_stale_feedback_threshold_minutes: int = 120) -> None:
        self.db = db
        self.crm_adapter = crm_adapter
        self.ocr_adapter = ocr_adapter
        self.lark_media_adapter = lark_media_adapter
        self.lark_reply_adapter = lark_reply_adapter
        self._lark_reply_adapter_by_app_id = {
            str(k).strip(): v for k, v in dict(lark_reply_adapter_by_app_id or {}).items() if str(k).strip() and v is not None
        }
        self._profile_reply_adapter_cache: Dict[str, Any] = {}
        self.lark_default_app_name = lark_default_app_name
        self.lark_default_dept_name = lark_default_dept_name
        self.current_lark_app_id = current_lark_app_id
        self.auto_bind_simulation = auto_bind_simulation
        self.bind_simulator = bind_simulator
        self.real_bind_executor = real_bind_executor
        self.registration_group_approval_executor = registration_group_approval_executor
        self.official_group_approval_executor = official_group_approval_executor
        self.timo_guild_executor = timo_guild_executor
        self.official_group_target_map = {
            str(k).strip().lower(): str(v).strip()
            for k, v in dict(official_group_target_map or {}).items()
            if str(k).strip() and str(v).strip()
        }
        self.official_group_approval_webhook_url = str(official_group_approval_webhook_url or '').strip() or None
        self.official_group_bridge_token = str(official_group_bridge_token or '').strip()
        self.guild_executor_proxy_region_urls = {
            str(k).strip(): str(v).strip()
            for k, v in dict(guild_executor_proxy_region_urls or {}).items()
            if str(k).strip() and str(v).strip()
        }
        self.auto_bind_simulation_success_rate = max(0.0, min(1.0, float(auto_bind_simulation_success_rate or 0.5)))
        self._bind_random = random.Random(auto_bind_simulation_seed) if auto_bind_simulation_seed is not None else random.Random()
        self.media_cache_dir = Path(media_cache_dir or './data/lark_media_cache')
        self.media_cache_dir.mkdir(parents=True, exist_ok=True)
        self.group_atmosphere_media_dir = Path(group_atmosphere_media_dir or os.getenv('GROUP_ATMOSPHERE_MEDIA_DIR') or (self.media_cache_dir.parent / 'group_atmosphere_media'))
        self.group_atmosphere_media_dir.mkdir(parents=True, exist_ok=True)
        self.crm_base_url = crm_base_url
        self.crm_username = crm_username
        self.crm_login_error = crm_login_error
        self.require_invite_code = require_invite_code
        self.crm_retry_delays_seconds = [max(0, int(v)) for v in list(crm_retry_delays_seconds or [5, 10, 20])]
        self.crm_retry_max_attempts = max(1, int(crm_retry_max_attempts or len(self.crm_retry_delays_seconds) or 1))
        self.bind_retry_max_attempts = max(0, int(bind_retry_max_attempts if bind_retry_max_attempts is not None else 2))
        self.ingress_async_default = ingress_async_default
        self.ingress_worker_enabled = ingress_worker_enabled
        self.ingress_worker_poll_interval = max(1.0, float(ingress_worker_poll_interval or 1.0))
        self.ingress_worker_count = max(1, int(ingress_worker_count or 1))
        self.ingress_rate_limiter = TokenBucketRateLimiter(rate=max(1, int(ingress_rate_limit_per_minute or 600)), window_seconds=60)
        self.external_call_rate_limiter = TokenBucketRateLimiter(rate=max(1, int(external_call_rate_limit_per_minute or 300)), window_seconds=60)
        self.reply_circuit_breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=30)
        self.crm_circuit_breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=30)
        self.ocr_circuit_breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=30)
        self._worker_threads: List[threading.Thread] = []
        self._worker_stop = threading.Event()
        self._worker_wakeup = threading.Event()
        self._crm_compensation_patrol_interval_seconds = max(
            30.0,
            float(os.getenv('CRM_COMPENSATION_PATROL_INTERVAL_SECONDS') or 45.0),
        )
        self._crm_compensation_patrol_last_monotonic = 0.0
        self._crm_compensation_patrol_lock = threading.Lock()
        self._operation_task_worker_thread: Optional[threading.Thread] = None
        self._operation_task_worker_stop = threading.Event()
        self._operation_task_worker_wakeup = threading.Event()
        self._operation_task_worker_poll_interval = max(1.0, float(os.getenv('OPERATION_TASK_WORKER_POLL_INTERVAL_SECONDS') or 5.0))
        self.approval_operation_realtime_callback = None
        self.ops_intake_auto_clear_stale_feedback_enabled = bool(ops_intake_auto_clear_stale_feedback_enabled)
        self.ops_intake_auto_clear_stale_feedback_threshold_minutes = max(1, int(ops_intake_auto_clear_stale_feedback_threshold_minutes or 120))
        self.ops_intake_auto_clear_stale_feedback_poll_interval_seconds = max(60.0, float(ops_intake_auto_clear_stale_feedback_poll_interval_seconds or 300.0))
        self._ops_intake_stale_feedback_cleanup_thread: Optional[threading.Thread] = None
        self._ops_intake_stale_feedback_cleanup_lock = threading.Lock()
        self._ops_intake_stale_feedback_last_cleanup_date_bj = ''
        default_anchor_stats_enabled = 'false' if self.db.db_path == ':memory:' else 'true'
        self.guild_anchor_daily_stats_enabled = str(os.getenv('GUILD_ANCHOR_DAILY_STATS_ENABLED') or default_anchor_stats_enabled).strip().lower() in {'1', 'true', 'yes', 'on'}
        self.guild_anchor_daily_stats_hour_bj = max(0, min(23, int(os.getenv('GUILD_ANCHOR_DAILY_STATS_HOUR_BJ') or 9)))
        self.guild_anchor_daily_stats_minute_bj = max(0, min(59, int(os.getenv('GUILD_ANCHOR_DAILY_STATS_MINUTE_BJ') or 0)))
        self.guild_anchor_daily_stats_page_size = max(20, min(500, int(os.getenv('GUILD_ANCHOR_DAILY_STATS_PAGE_SIZE') or 500)))
        self.guild_anchor_daily_stats_max_pages = max(1, min(10000, int(os.getenv('GUILD_ANCHOR_DAILY_STATS_MAX_PAGES') or 5000)))
        self.guild_anchor_daily_stats_guard_pages = max(0, min(20, int(os.getenv('GUILD_ANCHOR_DAILY_STATS_GUARD_PAGES') or 5)))
        self.guild_anchor_daily_stats_backfill_days = max(1, min(7, int(os.getenv('GUILD_ANCHOR_DAILY_STATS_BACKFILL_DAYS') or 1)))
        self.guild_anchor_daily_stats_job_lease_seconds = max(60.0, float(os.getenv('GUILD_ANCHOR_DAILY_STATS_JOB_LEASE_SECONDS') or 1800))
        self.guild_anchor_daily_stats_job_max_attempts = max(1, min(10, int(os.getenv('GUILD_ANCHOR_DAILY_STATS_JOB_MAX_ATTEMPTS') or 5)))
        self._guild_anchor_daily_stats_thread: Optional[threading.Thread] = None
        self._guild_anchor_daily_stats_lock = threading.RLock()
        self._guild_anchor_daily_stats_refresh_thread: Optional[threading.Thread] = None
        self._guild_anchor_daily_stats_refresh_state: Dict[str, Any] = {
            'running': False,
            'stat_date': '',
            'source': '',
            'started_at': '',
            'finished_at': '',
            'error': '',
        }
        self._guild_anchor_daily_stats_last_run_date_bj = ''
        self._guild_anchor_daily_stats_worker_role_allowed = (
            str(os.getenv('MCN_PROCESS_ROLE') or '').strip().lower() == 'backend'
        )
        self._worker_id = f"worker-{os.getpid()}-{create_id('lease')}"
        self._bind_task_lease_seconds = 300.0
        self._ingress_job_lease_seconds = max(30.0, float(os.getenv('INGRESS_JOB_LEASE_SECONDS') or 300.0))
        self._ingress_job_max_attempts = max(1, int(os.getenv('INGRESS_JOB_MAX_ATTEMPTS') or 3))
        self._registration_group_approval_batch_lock = threading.Lock()
        self._manual_whatsapp_approval_inflight_lock = threading.Lock()
        self._manual_whatsapp_approval_inflight: set[str] = set()
        self._whatsapp_binding_operation_lock = threading.Lock()
        self._whatsapp_binding_operations: dict[str, dict[str, Any]] = {}
        self._whatsapp_runtime_actor_condition = threading.Condition(threading.Lock())
        self._whatsapp_runtime_actor_states: dict[str, dict[str, Any]] = {}
        self._approval_truth_acquisition_lock = threading.Lock()
        self._approval_truth_acquisitions: dict[str, dict[str, Any]] = {}
        self._background_approval_truth_refresh_lock = threading.Lock()
        self._background_approval_truth_refresh_started_monotonic: dict[str, float] = {}
        self._whatsapp_approval_runtime_lock = threading.RLock()
        # Baileys init/reset may wait for a QR or reconnect for tens of seconds.
        # Keep one provider action per account and let the HTTP request return
        # a pending state instead of holding the web request open.
        self._baileys_provider_action_lock = threading.RLock()
        self._baileys_provider_actions: Dict[str, Dict[str, Any]] = {}
        self.whatsapp_approval_api_positive_override_enabled = str(os.getenv('WHATSAPP_APPROVAL_API_POSITIVE_OVERRIDE_ENABLED') or 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
        self.whatsapp_registration_group_approval_cutover_enabled = str(os.getenv('WHATSAPP_REGISTRATION_GROUP_APPROVAL_CUTOVER_ENABLED') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'}
        self.whatsapp_approval_runtime_adapter = DefaultWhatsAppApprovalRuntimeAdapter()
        self._official_group_bridge_recover_lock = threading.Lock()
        self._official_group_bridge_recover_state: Dict[str, Any] = {}
        self._whatsapp_approval_auto_recover_lock = threading.Lock()
        self._whatsapp_approval_auto_recover_state: Dict[str, Any] = {}
        self._baileys_qr_recovery_thread: Optional[threading.Thread] = None
        self._baileys_qr_recovery_lock = threading.RLock()
        self._baileys_qr_recovery_poll_interval_seconds = max(
            5.0,
            float(os.getenv('BAILEYS_QR_RECOVERY_POLL_INTERVAL_SECONDS') or 10.0),
        )
        self._baileys_qr_recovery_enabled = (
            self.db.db_path != ':memory:'
            and str(os.getenv('MCN_PROCESS_ROLE') or '').strip().lower() == 'backend'
            and str(os.getenv('BAILEYS_QR_RECOVERY_ENABLED') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'}
        )
        self._baileys_qr_recovery_state: Dict[str, Any] = {
            'enabled': self._baileys_qr_recovery_enabled,
            'last_tick_at': '',
            'last_success_at': '',
            'last_error_at': '',
            'last_error': '',
            'last_result': {},
        }
        self._task_residue_reconcile_interval_seconds = 60.0
        self._bind_processing_stale_seconds = 900.0
        self._group_join_pending_stale_seconds = 900.0
        self._crm_task_stale_seconds = 900.0
        self._task_residue_last_reconciled_monotonic = 0.0
        self.group_atmosphere_scheduler_enabled = bool(group_atmosphere_scheduler_enabled)
        self.event_ledger_enabled = str(os.getenv('EVENT_LEDGER_ENABLED') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'}
        self.truth_snapshot_enabled = str(os.getenv('TRUTH_SNAPSHOT_ENABLED') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'}
        self.task_engine_enabled = str(os.getenv('TASK_ENGINE_ENABLED') or 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
        self.group_atmosphere_scheduler_poll_interval_seconds = max(5.0, float(group_atmosphere_scheduler_poll_interval_seconds or 30.0))
        self._group_atmosphere_scheduler_lease_seconds = max(30.0, float(os.getenv('GROUP_ATMOSPHERE_SCHEDULER_LEASE_SECONDS') or (self.group_atmosphere_scheduler_poll_interval_seconds * 3)))
        self.group_atmosphere_candidate_translator = group_atmosphere_candidate_translator
        self.group_atmosphere_translation_background_enabled = bool(group_atmosphere_translation_background_enabled)
        self._group_atmosphere_translation_preprocess_lock = threading.Lock()
        self._group_atmosphere_translation_preprocess_thread: Optional[threading.Thread] = None
        self._group_atmosphere_allow_test_worker_urls = self.db.db_path == ':memory:'
        self._group_atmosphere_scheduler_thread: Optional[threading.Thread] = None
        self._group_atmosphere_scheduler_state: Dict[str, Any] = {
            'last_tick_at': '',
            'last_success_at': '',
            'last_error_at': '',
            'last_error': '',
            'last_result': {},
        }
        self._crm_option_cache: Dict[str, Dict[str, Dict[str, Any]]] = {
            'app': {},
            'guild': {},
        }
        self._load_persisted_crm_option_cache()
        if self.ingress_worker_enabled:
            self._start_ingress_worker()
        if self.group_atmosphere_scheduler_enabled:
            self._start_group_atmosphere_scheduler_worker()
        if self.task_engine_enabled:
            self._start_operation_task_worker()
        if self.ops_intake_auto_clear_stale_feedback_enabled:
            self._start_ops_intake_stale_feedback_cleanup_worker()
        if self.guild_anchor_daily_stats_enabled and self._guild_anchor_daily_stats_worker_role_allowed:
            self._start_guild_anchor_daily_stats_worker()
        if self._baileys_qr_recovery_enabled:
            self._start_baileys_qr_recovery_worker()
    @staticmethod
    def _whatsapp_binding_runtime_group_id(binding: Dict[str, Any]) -> str:
        raw_group_id = str(binding.get('group_id') or '').strip()
        if raw_group_id and not _looks_like_whatsapp_invite_link(raw_group_id):
            return raw_group_id
        runtime_probe_group_id = str(binding.get('runtime_probe_group_id') or '').strip()
        if runtime_probe_group_id and not _looks_like_whatsapp_invite_link(runtime_probe_group_id):
            return runtime_probe_group_id
        registration_group = str(binding.get('registration_group') or '').strip()
        if registration_group and registration_group.endswith('@g.us'):
            return registration_group
        return ''
    def _upsert_binding_current_truth_snapshot(self, item: Dict[str, Any], result: Optional[Dict[str, Any]] = None) -> None:
        item_id = str(item.get('item_id') or '').strip()
        if not item_id:
            return
        result = dict(result or {})
        truth_status, confidence, reason = self._binding_truth_status_from_item(item, result)
        now = utc_now()
        facts = {
            'item_id': item_id,
            'guild_name': item.get('guild_name'),
            'phone': item.get('parsed_phone'),
            'account_id': item.get('parsed_account_id'),
            'group': item.get('parsed_group'),
            'code_present': bool(str(item.get('parsed_code') or '').strip()),
            'system_status': item.get('system_status'),
            'feedback_status': item.get('feedback_status'),
            'result_code': result.get('result_code') or item.get('result_code'),
            'result_reason': result.get('result_reason') or result.get('reason') or item.get('result_reason'),
            'crm_verified': bool(result.get('crm_verified') or result.get('current_submission_crm_verified')),
            'lead_id': result.get('lead_id'),
            'task_id': result.get('task_id'),
        }
        source = {
            'service': 'mcn-backend',
            'source': 'ops_intake_items',
            'result': result,
            'created_at': item.get('created_at'),
            'processed_at': item.get('processed_at'),
        }
        snapshot = {
            'snapshot_id': f'binding-submission:{item_id}:binding_current_truth',
            'object_type': 'binding_submission',
            'object_key': item_id,
            'snapshot_type': 'binding_current_truth',
            'truth_status': truth_status,
            'confidence': confidence,
            'confidence_reason': reason,
            'facts_json': json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str),
            'source_json': json.dumps(source, ensure_ascii=False, sort_keys=True, default=str),
            'checked_at': str(item.get('processed_at') or now),
            'expires_at': None,
            'recommended_action': 'none' if truth_status == 'verified_success' else ('retry_crm_or_recheck' if truth_status == 'cms_bound_crm_failed' else 'manual_review_or_recheck'),
            'updated_at': now,
        }
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO mcn_truth_snapshots (
                    snapshot_id, object_type, object_key, snapshot_type, truth_status,
                    confidence, confidence_reason, facts_json, source_json, checked_at,
                    expires_at, recommended_action, updated_at
                ) VALUES (
                    :snapshot_id, :object_type, :object_key, :snapshot_type, :truth_status,
                    :confidence, :confidence_reason, :facts_json, :source_json, :checked_at,
                    :expires_at, :recommended_action, :updated_at
                )
                ON CONFLICT(object_type, object_key, snapshot_type) DO UPDATE SET
                    truth_status=excluded.truth_status,
                    confidence=excluded.confidence,
                    confidence_reason=excluded.confidence_reason,
                    facts_json=excluded.facts_json,
                    source_json=excluded.source_json,
                    checked_at=excluded.checked_at,
                    expires_at=excluded.expires_at,
                    recommended_action=excluded.recommended_action,
                    updated_at=excluded.updated_at
                """,
                snapshot,
            )
            conn.commit()
    def create_verify_binding_current_truth_task(self, *, item_id: str, fields: Optional[Dict[str, Any]], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized_item_id = str(item_id or '').strip()
        if not normalized_item_id:
            raise HTTPException(status_code=400, detail='item_id_required')
        # Permission check before accepting a task.
        item = self._get_ops_intake_item(normalized_item_id)
        guild_name = str(item.get('guild_name') or '').strip()
        if not self._ops_intake_user_can_access_guild(user, guild_name):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        now = utc_now()
        task_id = create_id('op_task')
        input_payload = {
            'item_id': normalized_item_id,
            'fields': dict(fields or {}),
            'requested_by': str((user or {}).get('username') or (user or {}).get('user_id') or '').strip(),
        }
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO mcn_operation_tasks (
                    task_id, task_type, object_type, object_key, idempotency_key,
                    status, stage, priority, input_json, created_by, created_at
                ) VALUES (?, 'verify_binding_current_truth', 'binding_submission', ?, ?, 'pending', 'queued', 20, ?, ?, ?)
                """,
                (
                    task_id,
                    normalized_item_id,
                    f'verify_binding_current_truth:{normalized_item_id}:{now}',
                    json.dumps(input_payload, ensure_ascii=False, default=str),
                    input_payload['requested_by'],
                    now,
                ),
            )
            conn.commit()
        if self.db.db_path == ':memory:':
            self._execute_operation_task(task_id, user=user)
        else:
            threading.Thread(target=self._execute_operation_task, args=(task_id,), kwargs={'user': user}, daemon=True).start()
        task = self.get_operation_task(task_id)
        return {'ok': True, 'task_id': task_id, 'task_type': task.get('task_type'), 'status': task.get('status'), 'task': task}
    def _execute_verify_binding_current_truth_task(self, task: Dict[str, Any], *, user: Optional[Dict[str, Any]] = None) -> None:
        task_id = str(task.get('task_id') or '').strip()
        payload = dict(task.get('input') or {})
        item_id = str(payload.get('item_id') or task.get('object_key') or '').strip()
        fields = dict(payload.get('fields') or {})
        self._set_operation_task_status(task_id, status='running', stage='verifying')
        try:
            result = self.recheck_ops_intake_bind_failed_item(item_id=item_id, fields=fields, user=user or {'role': OPS_AUTH_ROLE_INTERNAL, 'username': 'task_engine'})
            item = self._get_ops_intake_item(item_id)
            recheck = result.get('recheck') if isinstance(result.get('recheck'), dict) else result
            self._upsert_binding_current_truth_snapshot(item, recheck)
            self._set_operation_task_status(task_id, status='success', stage='snapshot_updated', result={'item_id': item_id, 'recheck': recheck, 'current_truth': self._load_binding_current_truth_snapshot(item_id)})
        except HTTPException as exc:
            error_code = str(exc.detail if isinstance(exc.detail, str) else (exc.detail or {}).get('reason') or 'http_error')
            try:
                item = self._get_ops_intake_item(item_id)
                self._upsert_binding_current_truth_snapshot(item, {'result_code': error_code, 'result_reason': error_code})
            except Exception:
                pass
            self._set_operation_task_status(task_id, status='failed', stage='failed', result={'item_id': item_id}, error_code=error_code, error_message=error_code)
        except Exception as exc:
            error_code = 'verify_binding_current_truth_failed'
            try:
                item = self._get_ops_intake_item(item_id)
                self._upsert_binding_current_truth_snapshot(item, {'result_code': error_code, 'result_reason': str(exc)})
            except Exception:
                pass
            self._set_operation_task_status(task_id, status='failed', stage='failed', result={'item_id': item_id}, error_code=error_code, error_message=str(exc))
    def _load_binding_current_truth_snapshot(self, item_id: str) -> Optional[Dict[str, Any]]:
        normalized_item_id = str(item_id or '').strip()
        if not normalized_item_id:
            return None
        try:
            with self.db.connect() as conn:
                row = conn.execute(
                    """
                    SELECT object_key, truth_status, confidence, confidence_reason, facts_json,
                           checked_at, expires_at, recommended_action, updated_at
                    FROM mcn_truth_snapshots
                    WHERE object_type = 'binding_submission'
                      AND object_key = ?
                      AND snapshot_type = 'binding_current_truth'
                    LIMIT 1
                    """,
                    (normalized_item_id,),
                ).fetchone()
        except Exception:
            return None
        if not row:
            return None
        try:
            facts = json.loads(row['facts_json'] or '{}')
        except Exception:
            facts = {}
        return {
            'object_key': row['object_key'],
            'truth_status': row['truth_status'],
            'confidence': row['confidence'],
            'confidence_reason': row['confidence_reason'],
            'facts': facts if isinstance(facts, dict) else {},
            'checked_at': row['checked_at'],
            'expires_at': row['expires_at'],
            'recommended_action': row['recommended_action'],
            'updated_at': row['updated_at'],
        }
    def _load_binding_current_truth_snapshot_map(self, conn: sqlite3.Connection, item_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        normalized_ids = [str(item_id or '').strip() for item_id in item_ids if str(item_id or '').strip()]
        if not normalized_ids:
            return {}
        rows: List[sqlite3.Row] = []
        chunk_size = 400
        for idx in range(0, len(normalized_ids), chunk_size):
            chunk = normalized_ids[idx:idx + chunk_size]
            placeholders = ','.join('?' for _ in chunk)
            rows.extend(conn.execute(
                f"""
                SELECT object_key, truth_status, confidence, confidence_reason, facts_json,
                       checked_at, expires_at, recommended_action, updated_at
                FROM mcn_truth_snapshots
                WHERE object_type = 'binding_submission'
                  AND snapshot_type = 'binding_current_truth'
                  AND object_key IN ({placeholders})
                """,
                tuple(chunk),
            ).fetchall())
        mapping: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            try:
                facts = json.loads(row['facts_json'] or '{}')
            except Exception:
                facts = {}
            mapping[str(row['object_key'] or '').strip()] = {
                'object_key': row['object_key'],
                'truth_status': row['truth_status'],
                'confidence': row['confidence'],
                'confidence_reason': row['confidence_reason'],
                'facts': facts if isinstance(facts, dict) else {},
                'checked_at': row['checked_at'],
                'expires_at': row['expires_at'],
                'recommended_action': row['recommended_action'],
                'updated_at': row['updated_at'],
            }
        return mapping
    @staticmethod
    def _binding_history_parse_current_truth_payload(payload: Any) -> Dict[str, Any]:
        if isinstance(payload, dict):
            return dict(payload)
        raw = str(payload or '').strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    def _write_official_group_executor_current_truth_from_result(
        self,
        *,
        routed_runtime: Optional[Dict[str, Any]],
        target_group: str,
        executor_result: Dict[str, Any],
        source: str,
        approval_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(routed_runtime, dict) or not routed_runtime:
            return {'written': False, 'reason': 'runtime_route_missing'}
        account_key = str(routed_runtime.get('account_key') or '').strip()
        binding = dict(routed_runtime.get('binding') or {})
        if not account_key or not binding:
            return {'written': False, 'reason': 'binding_route_missing'}
        binding['account_key'] = account_key
        binding['responsible_type'] = 'official_group'
        raw_result = dict((executor_result or {}).get('raw_result') or {})
        requesters = [dict(item) for item in list(raw_result.get('requesters') or []) if isinstance(item, dict)]
        requester_ids = [
            str(item.get('requesterId') or item.get('requester_id') or '').strip()
            for item in requesters
            if str(item.get('requesterId') or item.get('requester_id') or '').strip()
        ]
        pending_count = normalize_int_or_none(raw_result.get('pending_after'))
        if pending_count is None:
            pending_count = normalize_int_or_none(raw_result.get('pendingAfter'))
        if pending_count is None:
            pending_count = normalize_int_or_none(raw_result.get('pending_count'))
        if pending_count is None:
            pending_count = normalize_int_or_none(raw_result.get('pendingCount'))
        if pending_count is None:
            pending_count = normalize_int_or_none(raw_result.get('pending_before'))
        if pending_count is None:
            pending_count = normalize_int_or_none(raw_result.get('pendingBefore'))
        if pending_count is None and requesters:
            pending_count = len(requesters)
        if pending_count is None:
            return {'written': False, 'reason': 'pending_count_missing'}
        pending_count = max(int(pending_count), 0)
        status = str((executor_result or {}).get('status') or '').strip().lower()
        trusted_requester_count = bool(pending_count > 0 and requesters and len(requesters) >= pending_count)
        if status != 'success' and not trusted_requester_count:
            return {'written': False, 'reason': 'executor_result_not_success_current_truth_preserved'}
        if pending_count > 0:
            trust_status = 'TRUSTED_CONFIRMED_PENDING' if trusted_requester_count else 'UNTRUSTED_LIVE_PENDING'
        else:
            trust_status = 'TRUSTED_CONFIRMED_EMPTY' if status == 'success' else 'UNTRUSTED_LIVE_EMPTY'
        group_id = (
            _sanitize_whatsapp_group_jid(raw_result.get('group_id'))
            or _sanitize_whatsapp_group_jid(raw_result.get('groupId'))
            or _sanitize_whatsapp_group_jid((executor_result or {}).get('resolvedGroupId'))
            or _sanitize_whatsapp_group_jid(target_group)
            or self._whatsapp_binding_runtime_group_id(binding)
        )
        group_name = str(raw_result.get('group_name') or raw_result.get('groupName') or '').strip()
        member_count = normalize_int_or_none(raw_result.get('member_count'))
        if member_count is None:
            member_count = normalize_int_or_none(raw_result.get('memberCount'))
        observed_at = utc_now()
        sync_result = {
            'ok': True,
            'trust_status': trust_status,
            'reason_code': str((executor_result or {}).get('result_code') or raw_result.get('result_code') or 'official_group_executor_queue_state').strip(),
            'confidence_reason': 'official_group_executor_queue_state',
            'pending_count': pending_count,
            'trusted_pending_count': pending_count if trust_status.startswith('TRUSTED') else None,
            'ui_pending_count': pending_count,
            'api_pending_count': pending_count,
            'requesters': requesters,
            'requester_ids': requester_ids,
            'member_count': member_count,
            'group_id': group_id,
            'group_name': group_name,
            'actual_group_name': group_name,
            'can_manual_approve': trust_status == 'TRUSTED_CONFIRMED_PENDING',
            'manual_approve_allowed': trust_status == 'TRUSTED_CONFIRMED_PENDING',
            'display_trusted': trust_status.startswith('TRUSTED'),
            'group_identity_verified': bool(group_id),
            'runtime_identity_match': bool(group_id),
            'session_authenticated': True,
            'review_surface_ready': bool(requesters),
            'source_ts': observed_at,
            'verified_at': observed_at,
            'active_approval_run_id': str(approval_run_id or raw_result.get('approval_run_id') or '').strip() or None,
            'source': {
                'mode': str(source or 'official_group_executor_queue_state').strip() or 'official_group_executor_queue_state',
                'provider': (executor_result or {}).get('provider') or 'baileys',
                'provider_endpoint': (executor_result or {}).get('provider_endpoint'),
                'runtime_group_id': group_id,
                'approval_run_id': str(approval_run_id or raw_result.get('approval_run_id') or '').strip() or None,
                'executor_status': status or None,
            },
        }
        return self.upsert_approval_queue_current_truth(
            account_key=account_key,
            binding=binding,
            sync_result=sync_result,
            source_priority=98,
            observed_at=observed_at,
            force=True,
        )
    @staticmethod
    def _approval_queue_current_truth_is_fresh(current_truth: Dict[str, Any], *, max_age_seconds: float) -> bool:
        if not isinstance(current_truth, dict) or not current_truth:
            return False
        now_dt = now_utc()
        expires_at = str(current_truth.get('expires_at') or '').strip()
        if expires_at:
            try:
                expiry_dt = parse_iso_datetime(expires_at)
                if expiry_dt.tzinfo is None:
                    expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
                if now_dt < expiry_dt:
                    return True
            except Exception:
                pass
        verified_at = str(current_truth.get('verified_at') or current_truth.get('source_ts') or current_truth.get('checked_at') or '').strip()
        if verified_at:
            try:
                verified_dt = parse_iso_datetime(verified_at)
                if verified_dt.tzinfo is None:
                    verified_dt = verified_dt.replace(tzinfo=timezone.utc)
                return (now_dt - verified_dt).total_seconds() <= max(float(max_age_seconds or 0), 1.0)
            except Exception:
                return False
        return False
    def _manual_approve_preflight_from_current_truth(
        self,
        *,
        account_key: str,
        binding: Dict[str, Any],
        preflight_error: Optional[BaseException] = None,
    ) -> Dict[str, Any]:
        snapshots = self._load_approval_binding_queue_snapshots(account_key, binding)
        current_truth = dict(snapshots.get('current_truth') or {})
        if not current_truth:
            return {}
        truth_view = self._approval_queue_truth_view(current_truth, snapshots.get('latest_probe'))
        current_payload = dict(current_truth.get('payload') or {}) if isinstance(current_truth.get('payload'), dict) else {}
        current_facts = dict(current_truth.get('facts') or {}) if isinstance(current_truth.get('facts'), dict) else {}
        pending_count = normalize_int_or_none(truth_view.get('pending_count'))
        if pending_count is None:
            pending_count = normalize_int_or_none(current_truth.get('pending_count'))
        if pending_count is None:
            pending_count = normalize_int_or_none(current_payload.get('pending_count'))
        if pending_count is None:
            pending_count = normalize_int_or_none(current_facts.get('pending_count'))
        if pending_count is None or pending_count <= 0:
            return {}
        if truth_view.get('stale') is True or current_truth.get('stale') is True:
            return {}
        if not bool(
            truth_view.get('can_manual_approve')
            or current_truth.get('can_manual_approve')
            or current_payload.get('can_manual_approve')
            or current_facts.get('can_manual_approve')
            or current_truth.get('manual_approve_allowed')
            or current_payload.get('manual_approve_allowed')
            or current_facts.get('manual_approve_allowed')
        ):
            return {}
        requester_ids: List[str] = []
        for requester_id_source in (
            current_truth.get('requester_ids'),
            current_truth.get('requesterIds'),
            current_payload.get('requester_ids'),
            current_payload.get('requesterIds'),
            current_facts.get('requester_ids'),
            current_facts.get('requesterIds'),
            truth_view.get('requester_ids'),
        ):
            if isinstance(requester_id_source, list):
                requester_ids.extend(str(item).strip() for item in requester_id_source if str(item).strip())
        requesters: List[Dict[str, Any]] = []
        for requester_source in (
            current_truth.get('requesters'),
            current_payload.get('requesters'),
            current_facts.get('requesters'),
            truth_view.get('requesters'),
        ):
            if isinstance(requester_source, list):
                requesters = [dict(item) for item in requester_source if isinstance(item, dict)]
                if requesters:
                    break
        if not requester_ids:
            for requester in requesters:
                if isinstance(requester, dict):
                    requester_id = str(requester.get('requesterId') or requester.get('requester_id') or '').strip()
                    if requester_id:
                        requester_ids.append(requester_id)
        requester_ids = list(dict.fromkeys(requester_ids))
        if len(requester_ids) != pending_count:
            return {}
        source_payload = dict(current_truth.get('source') or {}) if isinstance(current_truth.get('source'), dict) else {}
        source_payload = {
            **source_payload,
            'mode': 'manual_approve_current_truth_reuse',
            'reused_current_truth': True,
            'preflight_error': str(preflight_error or '')[:240] or None,
        }
        return {
            'ok': True,
            'trust_status': str(current_truth.get('trust_status') or 'TRUSTED_CONFIRMED_PENDING').strip() or 'TRUSTED_CONFIRMED_PENDING',
            'reason_code': 'manual_approve_preflight_reused_current_truth',
            'pending_count': pending_count,
            'trusted_pending_count': pending_count,
            'ui_pending_count': pending_count,
            'api_pending_count': normalize_int_or_none(current_truth.get('api_pending_count')),
            'member_count': normalize_int_or_none(current_truth.get('member_count') or current_payload.get('member_count') or current_facts.get('member_count')),
            'group_id': str(current_truth.get('group_id') or current_payload.get('group_id') or current_facts.get('group_id') or truth_view.get('group_id') or '').strip() or None,
            'group_name': str(current_truth.get('group_name') or current_truth.get('actual_group_name') or current_payload.get('group_name') or current_payload.get('actual_group_name') or current_facts.get('group_name') or current_facts.get('actual_group_name') or truth_view.get('group_name') or '').strip() or None,
            'requester_ids': requester_ids,
            'requesters': requesters,
            'can_manual_approve': True,
            'manual_approve_allowed': True,
            'approval_queue_truth': truth_view,
            'current_truth_reused_for_manual_approve': True,
            'current_truth_checked_at': str(current_truth.get('checked_at') or '').strip() or None,
            'source': source_payload,
        }
    @staticmethod
    def _approval_binding_truth_object_keys(account_key: str, binding: Dict[str, Any]) -> List[str]:
        normalized_key = str(account_key or '').strip()
        item = dict(binding or {})
        keys: List[str] = []

        binding_id = str(item.get('binding_id') or '').strip()
        if normalized_key and binding_id:
            key = f'{normalized_key}:binding:{binding_id}'
            if key not in keys:
                keys.append(key)

        runtime_group_id = Service._whatsapp_binding_runtime_group_id(item)
        if normalized_key and runtime_group_id:
            key = f'{normalized_key}:group:{runtime_group_id}'
            if key not in keys:
                keys.append(key)
        return keys
    def _approval_binding_truth_object_key(self, account_key: str, binding: Dict[str, Any]) -> str:
        normalized_key = str(account_key or '').strip()
        item = self._resolve_truth_binding_identity(account_key, binding)
        binding_id = str(item.get('binding_id') or '').strip()
        if normalized_key and binding_id:
            return f'{normalized_key}:binding:{binding_id}'
        runtime_group_id = Service._whatsapp_binding_runtime_group_id(item)
        if normalized_key and runtime_group_id:
            return f'{normalized_key}:group:{runtime_group_id}'
        object_keys = Service._approval_binding_truth_object_keys(account_key, item)
        return object_keys[0] if object_keys else ''
    def _maybe_promote_pending_truth_confirmed_pending_to_approval_queue_current_truth(self, account_key: str, binding: Dict[str, Any], current_truth: Optional[Dict[str, Any]]) -> bool:
        return False
    def _maybe_promote_pending_truth_confirmed_empty_to_approval_queue_current_truth(self, account_key: str, binding: Dict[str, Any], current_truth: Optional[Dict[str, Any]]) -> bool:
        return False
    @staticmethod
    def _approval_queue_truth_view(current_truth: Optional[Dict[str, Any]], latest_probe: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return build_diagnostic_approval_queue_truth_view(current_truth, latest_probe)
    def _write_post_approval_queue_current_truth(
        self,
        *,
        account_key: str,
        binding: Dict[str, Any],
        approved_count: Optional[int] = None,
        pending_count: Optional[int] = None,
        approval_run_id: str = '',
        action_ts: Optional[str] = None,
        requester_ids: Optional[List[str]] = None,
        member_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        observed_at = str(action_ts or utc_now())
        try:
            normalized_pending_count = int(pending_count) if pending_count is not None else None
        except Exception:
            normalized_pending_count = None
        try:
            normalized_member_count = int(member_count) if member_count is not None else None
        except Exception:
            normalized_member_count = None
        normalized_requester_ids = [str(item).strip() for item in (requester_ids or []) if str(item).strip()]
        sync_result = {
            'trust_status': 'POST_APPROVAL_PENDING_SNAPSHOT',
            'trusted_pending_count': normalized_pending_count,
            'pending_count': normalized_pending_count,
            'ui_pending_count': normalized_pending_count,
            'api_pending_count': normalized_pending_count,
            'member_count': normalized_member_count,
            'requester_ids': normalized_requester_ids,
            'reason_code': 'approval_result_pending_after',
            'can_manual_approve': False,
            'manual_approve_allowed': False,
            'last_approval_action_ts': observed_at,
            'last_approved_count': int(approved_count or 0),
            'display_schema_version': 1,
            'source_ts': observed_at,
            'verified_at': observed_at,
            'stale': False,
            'source': {
                'mode': 'manual_approve_result',
                'reason': 'approval_completed',
                'verification_state': 'pending_verify',
                'approval_run_id': str(approval_run_id or '').strip() or None,
            },
        }
        return self.upsert_approval_queue_current_truth(
            account_key=account_key,
            binding=binding,
            sync_result=sync_result,
            source_priority=100,
            observed_at=observed_at,
            force=True,
            skip_guard=True,
        )
    def _enqueue_official_group_post_approval_verify_task(
        self,
        *,
        account_key: str,
        binding_index: int,
        request_id: str,
        approval_run_id: str,
    ) -> Dict[str, Any]:
        normalized_account_key = str(account_key or '').strip()
        if not normalized_account_key:
            return {'queued': False, 'reason': 'account_key_missing'}
        if self.db.db_path == ':memory:':
            return {'queued': False, 'reason': 'memory_db'}
        if not self.task_engine_enabled:
            return {'queued': False, 'reason': 'task_engine_disabled'}
        try:
            queued = self.enqueue_whatsapp_approval_task(
                account_key=normalized_account_key,
                binding_index=int(binding_index),
                operation='full_sync',
                input_payload={
                    'source': 'approval_after_sync',
                    'timeout_seconds': 20.0,
                    'request_id': str(request_id or '').strip() or None,
                    'reason': 'official_manual_approve_post_verify_deferred',
                    'approval_run_id': str(approval_run_id or '').strip() or None,
                },
                priority=30,
                timeout_seconds=45,
                max_retries=2,
                created_by='official_manual_approve_post_verify',
            )
            return {
                'queued': True,
                'task_id': str(queued.get('task_id') or '').strip() or None,
                'deduped': bool(queued.get('deduped')),
                'status': str(queued.get('status') or queued.get('task_status') or '').strip() or None,
            }
        except Exception as exc:
            return {'queued': False, 'reason': 'enqueue_failed', 'error': str(exc)[:240]}
    def _mark_approval_queue_current_truth_stale(
        self,
        *,
        account_key: str,
        binding: Dict[str, Any],
        reason_code: str,
        action_ts: Optional[str] = None,
        approval_run_id: str = '',
    ) -> Dict[str, Any]:
        observed_at = str(action_ts or utc_now())
        snapshots = self._load_approval_binding_queue_snapshots_raw(account_key, binding)
        current = dict(snapshots.get('current_truth') or {}) if isinstance(snapshots.get('current_truth'), dict) else {}
        if not current:
            return {
                'written': False,
                'object_key': self._approval_binding_truth_object_key(account_key, binding),
                'snapshot_type': 'approval_queue_current_truth',
                'reason': 'current_truth_missing',
            }
        source = dict(current.get('source') or {}) if isinstance(current.get('source'), dict) else {}
        source.update({
            'mode': str(source.get('mode') or 'manual_approve_result').strip() or 'manual_approve_result',
            'stale_mark_reason': str(reason_code or '').strip() or None,
        })
        if approval_run_id:
            source['approval_run_id'] = str(approval_run_id).strip()
        sync_result = {
            'trust_status': str(current.get('trust_status') or 'TRUTH_UNKNOWN').strip() or 'TRUTH_UNKNOWN',
            'pending_count': current.get('pending_count'),
            'ui_pending_count': current.get('ui_pending_count'),
            'api_pending_count': current.get('api_pending_count'),
            'requester_ids': list(current.get('requester_ids') or []) if isinstance(current.get('requester_ids'), list) else [],
            'can_manual_approve': False,
            'manual_approve_allowed': False,
            'last_approval_action_ts': observed_at,
            'last_approved_count': current.get('last_approved_count'),
            'display_schema_version': int(current.get('display_schema_version') or 1),
            'source_ts': str(current.get('source_ts') or current.get('verified_at') or observed_at),
            'verified_at': str(current.get('verified_at') or current.get('source_ts') or observed_at),
            'reason_code': str(reason_code or '').strip() or None,
            'stale': True,
            'source': source,
        }
        return self.upsert_approval_queue_current_truth(
            account_key=account_key,
            binding=binding,
            sync_result=sync_result,
            source_priority=int(current.get('source_priority') or 100),
            observed_at=observed_at,
            force=True,
            skip_guard=True,
        )
    def invalidate_approval_queue_truth_after_mutation(
        self,
        *,
        account_key: str,
        binding: Dict[str, Any],
        invalidated_reason: str,
        approved_count: Optional[int] = None,
        pending_count: Optional[int] = None,
        approval_run_id: str = '',
        action_ts: Optional[str] = None,
    ) -> Dict[str, Any]:
        observed_at = str(action_ts or utc_now())
        normalized_reason = str(invalidated_reason or '').strip()
        try:
            normalized_pending_count = int(pending_count) if pending_count is not None else None
        except Exception:
            normalized_pending_count = None
        write: Dict[str, Any] = {
            'written': False,
            'object_key': self._approval_binding_truth_object_key(account_key, binding),
            'snapshot_type': 'approval_queue_current_truth',
            'reason': 'event_only_mutation',
        }
        responsible_type = str(binding.get('responsible_type') or '').strip().lower()
        if normalized_reason == 'approval_completed' and normalized_pending_count is not None:
            write = self._write_post_approval_queue_current_truth(
                account_key=account_key,
                binding=binding,
                approved_count=approved_count,
                pending_count=normalized_pending_count,
                approval_run_id=approval_run_id,
                action_ts=observed_at,
            )
        try:
            self.write_event_ledger(
                event_type='approval_truth_invalidated',
                object_type='registration_group_binding',
                object_key=str(write.get('object_key') or self._approval_binding_truth_object_key(account_key, binding) or ''),
                status='success',
                evidence_level='mutation',
                payload={
                    'account_key': str(account_key or '').strip(),
                    'binding_id': str(binding.get('binding_id') or '').strip() or None,
                    'invalidated_reason': normalized_reason,
                    'pending_count': normalized_pending_count,
                    'last_approved_count': int(approved_count or 0),
                    'approval_run_id': str(approval_run_id or '').strip() or None,
                    'last_approval_action_ts': observed_at,
                },
            )
        except Exception as exc:
            self._record_worker_loop_error(exc)
        return write
    @staticmethod
    def _approval_queue_current_truth_ttl_seconds(trust_status: str) -> int:
        normalized = str(trust_status or '').strip()
        if normalized == 'GROUP_BANNED':
            return 315360000
        if normalized == 'PERMISSION_DENIED':
            return 300
        if normalized == 'TRUSTED_CONFIRMED_EMPTY':
            return APPROVAL_TRUTH_ZERO_TTL_SECONDS
        if normalized in {'TRUSTED_CONFIRMED_PENDING', 'POST_APPROVAL_PENDING_SNAPSHOT'}:
            return APPROVAL_TRUTH_PENDING_TTL_SECONDS
        return APPROVAL_TRUTH_UNKNOWN_TTL_SECONDS
    @staticmethod
    def _approval_queue_current_truth_guard(sync_result: Dict[str, Any], facts: Dict[str, Any]) -> Tuple[bool, str]:
        trust_status = str(facts.get('trust_status') or '').strip()
        pending_count = facts.get('pending_count')
        try:
            pending_count = int(pending_count) if pending_count is not None else None
        except Exception:
            pending_count = None
        source_payload = dict(sync_result.get('source') or {}) if isinstance(sync_result.get('source'), dict) else {}
        source_mode = str(source_payload.get('mode') or '').strip()
        reason_code = str(facts.get('reason_code') or '').strip()
        if trust_status == 'GROUP_BANNED' and reason_code == 'group_banned':
            if not bool(facts.get('group_identity_verified')) or facts.get('runtime_identity_match') is not True:
                return False, 'group_banned_identity_unverified'
            if facts.get('terminal_confirmed') is not True:
                return False, 'group_banned_confirmation_required'
            facts['pending_count'] = None
            facts['trusted_pending_count'] = None
            facts['ui_pending_count'] = None
            facts['api_pending_count'] = None
            facts['requester_ids'] = []
            facts['requesters'] = []
            facts['display_trusted'] = True
            facts['can_manual_approve'] = False
            facts['manual_approve_allowed'] = False
            facts['stale'] = False
            return True, ''
        if trust_status == 'PERMISSION_DENIED' and reason_code in {'not_group_member', 'not_group_admin'}:
            if not bool(facts.get('group_identity_verified')) or facts.get('runtime_identity_match') is not True:
                return False, 'permission_identity_unverified'
            if facts.get('session_authenticated') is not True:
                return False, 'permission_session_unverified'
            if reason_code == 'not_group_member' and facts.get('self_participant_found') is not False:
                return False, 'permission_member_evidence_incomplete'
            if reason_code == 'not_group_admin' and not (
                facts.get('self_participant_found') is True
                and (facts.get('self_is_admin') is False or facts.get('can_manage_membership_requests') is False)
            ):
                return False, 'permission_admin_evidence_incomplete'
            facts['pending_count'] = None
            facts['trusted_pending_count'] = None
            facts['ui_pending_count'] = None
            facts['api_pending_count'] = None
            facts['requester_ids'] = []
            facts['requesters'] = []
            facts['display_trusted'] = True
            facts['can_manual_approve'] = False
            facts['manual_approve_allowed'] = False
            return True, ''
        if (
            source_mode == 'executor_group_state_fallback'
            or reason_code in {'executor_group_state_fallback', 'executor_group_state_fallback_pending_only'}
        ):
            return False, 'executor_fallback_forbidden'
        if pending_count is None:
            return False, 'pending_count_required'
        if pending_count < 0:
            return False, 'pending_count_invalid'
        requester_ids: List[str] = []
        for source in (facts, sync_result):
            raw_requester_ids = source.get('requester_ids') or source.get('requesterIds') or []
            if not isinstance(raw_requester_ids, list):
                raw_requester_ids = []
            for raw_requester_id in raw_requester_ids:
                requester_id = str(raw_requester_id or '').strip()
                if requester_id and requester_id not in requester_ids:
                    requester_ids.append(requester_id)
            requesters = source.get('requesters') or []
            if not isinstance(requesters, list):
                requesters = []
            for requester in requesters:
                if not isinstance(requester, dict):
                    continue
                requester_id = str(
                    requester.get('requesterId')
                    or requester.get('requester_id')
                    or requester.get('jid')
                    or requester.get('id')
                    or ''
                ).strip()
                if requester_id and requester_id not in requester_ids:
                    requester_ids.append(requester_id)
        if len(requester_ids) != pending_count:
            return False, 'requester_ids_incomplete'
        facts['requester_ids'] = requester_ids
        facts['pending_count'] = len(requester_ids)
        if trust_status.startswith('TRUSTED') or trust_status == 'POST_APPROVAL_PENDING_SNAPSHOT':
            facts['trusted_pending_count'] = len(requester_ids)
        if trust_status in {'TRUSTED_CONFIRMED_PENDING', 'POST_APPROVAL_PENDING_SNAPSHOT'}:
            if pending_count <= 0:
                return False, 'pending_count_required'
            return True, ''
        if trust_status == 'TRUSTED_CONFIRMED_EMPTY':
            if pending_count != 0:
                return False, 'pending_count_not_zero'
            return True, ''
        return False, 'trusted_truth_required'
    def upsert_approval_queue_current_truth(self, *, account_key: str, binding: Dict[str, Any], sync_result: Dict[str, Any], source_priority: int, observed_at: Optional[str] = None, force: bool = False, skip_guard: bool = False) -> Dict[str, Any]:
        payload = dict(sync_result or {})
        if skip_guard:
            payload['skip_guard'] = True
        return self._write_approval_queue_snapshot(
            account_key=account_key,
            binding=binding,
            snapshot_type='approval_queue_current_truth',
            sync_result=payload,
            source_priority=source_priority,
            observed_at=observed_at,
            force=force,
        )
    def downgrade_polluted_approval_queue_current_truth(self) -> Dict[str, Any]:
        changed = 0
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT object_key, truth_status, facts_json, source_json, checked_at, expires_at
                FROM mcn_truth_snapshots
                WHERE object_type='registration_group_binding'
                  AND snapshot_type='approval_queue_current_truth'
                """
            ).fetchall()
            for row in rows:
                try:
                    facts = json.loads(row['facts_json'] or '{}')
                except Exception:
                    facts = {}
                try:
                    source = json.loads(row['source_json'] or '{}')
                except Exception:
                    source = {}
                if not isinstance(facts, dict):
                    facts = {}
                if not isinstance(source, dict):
                    source = {}
                trust_status = str(facts.get('trust_status') or row['truth_status'] or '').strip()
                source_mode = str(source.get('mode') or '').strip()
                fallback_reason = str(source.get('fallback_reason') or '').strip()
                strong_empty_evidence = bool(facts.get('strong_empty_evidence'))
                polluted_empty = (
                    trust_status == 'TRUSTED_CONFIRMED_EMPTY'
                    and (
                        source_mode == 'executor_group_state_fallback'
                        or 'worker_untrusted' in fallback_reason
                        or not strong_empty_evidence
                    )
                )
                if not polluted_empty:
                    continue
                facts['trust_status'] = 'EMPTY_UNVERIFIED'
                facts['trusted_pending_count'] = None
                facts['pending_count'] = 0
                facts['display_trusted'] = False
                facts['can_manual_approve'] = False
                facts['manual_approve_allowed'] = False
                facts['strong_empty_evidence'] = False
                facts['reason_code'] = 'historical_polluted_empty_downgraded'
                facts['downgraded_from'] = 'TRUSTED_CONFIRMED_EMPTY'
                source['downgraded_from_mode'] = source_mode or None
                source['downgrade_reason'] = 'historical_polluted_empty'
                conn.execute(
                    """
                    UPDATE mcn_truth_snapshots
                    SET truth_status=?, confidence='untrusted', confidence_reason=?, facts_json=?, source_json=?,
                        recommended_action='manual_full_sync_or_recovery', updated_at=?
                    WHERE object_type='registration_group_binding' AND object_key=? AND snapshot_type='approval_queue_current_truth'
                    """,
                    (
                        'EMPTY_UNVERIFIED',
                        'historical_polluted_empty_downgraded',
                        json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str),
                        json.dumps(source, ensure_ascii=False, sort_keys=True, default=str),
                        utc_now(),
                        str(row['object_key'] or '').strip(),
                    ),
                )
                changed += 1
            conn.commit()
        return {'ok': True, 'changed': changed}
    def _call_whatsapp_worker_full_queue_sync(self, *, account: Dict[str, Any], binding: Dict[str, Any], timeout_seconds: float = 30.0) -> Dict[str, Any]:
        runtime_state = dict(account.get('runtime_state') or {})
        base_url = str(runtime_state.get('base_url') or '').strip().rstrip('/')
        if not base_url:
            raise RuntimeError('worker_base_url_missing')
        target = self._whatsapp_binding_runtime_group_id(binding)
        if not target:
            raise RuntimeError('registration_group_runtime_group_id_required')
        payload = {
            'registration_group': target,
            'group_id': target,
            'binding_id': binding.get('binding_id'),
            'account_key': account.get('account_key'),
            'provider_mode': binding.get('provider_mode') or runtime_state.get('provider_mode') or account.get('provider_mode'),
        }
        response = requests.post(f'{base_url}/full-queue-sync', json=payload, timeout=timeout_seconds)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {'ok': False, 'trust_status': 'UNTRUSTED_SYNC_INVALID', 'raw': data}
    def _approval_queue_current_truth_runtime_generation_floor(self, account_key: str) -> int:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            return 0
        max_generation = 0
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT facts_json, source_json
                    FROM mcn_truth_snapshots
                    WHERE object_type='registration_group_binding'
                      AND snapshot_type='approval_queue_current_truth'
                      AND object_key LIKE ?
                    """,
                    (f'{normalized_key}:binding:%',),
                ).fetchall()
        except Exception:
            return 0
        for row in rows:
            for raw in (row['facts_json'], row['source_json']):
                try:
                    payload = json.loads(raw or '{}')
                except Exception:
                    payload = {}
                if not isinstance(payload, dict):
                    continue
                try:
                    generation = int(payload.get('runtime_generation') or 0)
                except Exception:
                    generation = 0
                max_generation = max(max_generation, generation)
        return max_generation
    def _whatsapp_approval_runtime_generation(self, account_key: str) -> int:
        meta = self._read_whatsapp_approval_runtime_meta(account_key)
        try:
            runtime_generation = max(0, int((meta or {}).get('runtime_generation') or 0))
        except Exception:
            runtime_generation = 0
        return max(runtime_generation, self._approval_queue_current_truth_runtime_generation_floor(account_key))
    def _build_current_truth_from_live_probe(
        self,
        *,
        binding: Dict[str, Any],
        binding_runtime: Dict[str, Any],
        probe: Dict[str, Any],
        observed_at: str,
        source: str,
    ) -> Dict[str, Any]:
        pending_count = normalize_int_or_none(probe.get('pending_count'))
        member_count = normalize_int_or_none(probe.get('member_count'))
        requester_ids = self._approval_probe_requester_ids(probe)
        identity_group_id = self._whatsapp_binding_runtime_group_id(binding_runtime) or self._whatsapp_binding_runtime_group_id(binding)
        probe_group_id = str(probe.get('group_id') or '').strip()
        probe_group_name = (
            str(probe.get('group_name') or '').strip()
            or self._extract_whatsapp_group_name_from_payload(probe)
        )
        if _looks_like_whatsapp_group_jid(probe_group_name) or _looks_like_whatsapp_invite_link(probe_group_name):
            probe_group_name = ''
        group_identity_verified = bool(identity_group_id and probe_group_id and identity_group_id == probe_group_id)
        runtime_identity_match = True if group_identity_verified else False
        session_authenticated = bool(binding_runtime.get('session_authenticated'))
        if not session_authenticated:
            session_authenticated = bool(binding_runtime.get('authenticated')) or bool(binding_runtime.get('ready'))
        self_participant_found = probe.get('self_participant_found')
        if self_participant_found is None:
            self_participant_found = binding_runtime.get('self_participant_found')
        self_is_admin = probe.get('self_is_admin')
        if self_is_admin is None:
            self_is_admin = binding_runtime.get('self_is_admin')
        can_manage_membership_requests = probe.get('can_manage_membership_requests')
        if can_manage_membership_requests is None:
            can_manage_membership_requests = binding_runtime.get('can_manage_membership_requests')
        source_mode = str(source or 'manual_truth_refresh').strip() or 'manual_truth_refresh'
        probe_terminal_state = probe.get('terminalState') if isinstance(probe.get('terminalState'), dict) else {}
        group_banned = str(
            probe.get('terminal_state')
            or probe.get('terminalStatus')
            or probe_terminal_state.get('state')
            or probe.get('error')
            or ''
        ).strip().lower() == 'group_banned'
        trust_status = 'TRUTH_UNKNOWN'
        reason_code = 'live_probe_pending_count_missing'
        if group_banned:
            trust_status = 'GROUP_BANNED'
            reason_code = 'group_banned'
            pending_count = None
            requester_ids = []
        elif pending_count is not None:
            if pending_count > 0:
                if group_identity_verified and runtime_identity_match and session_authenticated and self_participant_found is True and self_is_admin is True and can_manage_membership_requests is True and len(requester_ids) == pending_count:
                    trust_status = 'TRUSTED_CONFIRMED_PENDING'
                    reason_code = 'live_probe_confirmed_pending'
                else:
                    trust_status = 'UNTRUSTED_LIVE_PENDING'
                    reason_code = 'live_probe_pending_unverified'
            else:
                if group_identity_verified and runtime_identity_match and session_authenticated and self_participant_found is True and self_is_admin is True and can_manage_membership_requests is True:
                    trust_status = 'TRUSTED_CONFIRMED_EMPTY'
                    reason_code = 'live_probe_confirmed_empty'
                else:
                    trust_status = 'UNTRUSTED_LIVE_EMPTY'
                    reason_code = 'live_probe_empty_unverified'
        can_manual_approve = trust_status == 'TRUSTED_CONFIRMED_PENDING'
        confidence_reasons: List[str] = []
        if pending_count is not None and pending_count > 0 and len(requester_ids) != pending_count:
            confidence_reasons.append('requester_ids_incomplete')
        if not group_identity_verified:
            confidence_reasons.append('group_identity_unverified')
        if runtime_identity_match is not True:
            confidence_reasons.append('runtime_identity_mismatch')
        if not session_authenticated:
            confidence_reasons.append('session_authentication_unverified')
        if self_participant_found is not True:
            confidence_reasons.append('self_participant_unverified')
        if self_is_admin is not True or can_manage_membership_requests is not True:
            confidence_reasons.append('approval_capability_unverified')
        return {
            'ok': trust_status.startswith('TRUSTED'),
            'trust_status': trust_status,
            'trusted_pending_count': pending_count if trust_status.startswith('TRUSTED') else None,
            'pending_count': pending_count,
            'ui_pending_count': pending_count,
            'api_pending_count': pending_count,
            'member_count': member_count,
            'group_id': probe_group_id or identity_group_id or None,
            'group_name': probe_group_name if group_identity_verified else None,
            'actual_group_name': probe_group_name if group_identity_verified else None,
            'runtime_probe_group_id': probe_group_id or None,
            'runtime_probe_group_name': probe_group_name if group_identity_verified else None,
            'requester_ids': requester_ids,
            'requesters': list(probe.get('requesters') or []) if isinstance(probe.get('requesters'), list) else [],
            'group_identity_verified': group_identity_verified,
            'runtime_identity_match': runtime_identity_match,
            'session_authenticated': session_authenticated,
            'self_participant_found': self_participant_found,
            'self_is_admin': self_is_admin,
            'can_manage_membership_requests': can_manage_membership_requests,
            'review_surface_ready': False,
            'can_manual_approve': can_manual_approve,
            'manual_approve_allowed': can_manual_approve,
            'display_trusted': trust_status.startswith('TRUSTED'),
            'terminal_confirmed': group_banned and bool(probe.get('terminal')),
            'terminal_state': 'group_banned' if group_banned else None,
            'terminal_source': str(probe_terminal_state.get('source') or '').strip() or None,
            'display_schema_version': 1,
            'reason_code': reason_code,
            'confidence_reason': ','.join(confidence_reasons) or None,
            'source_ts': observed_at,
            'verified_at': observed_at,
            'stale': False,
            'source': {
                'mode': source_mode,
                'evidence_layer': 'live_probe_current_truth',
                'probe_group_id': probe_group_id or None,
                'probe_group_name': probe_group_name or None,
                'runtime_group_id': identity_group_id or None,
            },
        }
    @staticmethod
    def _approval_truth_result_is_commit_candidate(result: Dict[str, Any]) -> bool:
        payload = dict(result or {})
        if normalize_int_or_none(payload.get('pending_count')) is not None:
            return True
        if (
            str(payload.get('trust_status') or '').strip() == 'GROUP_BANNED'
            and str(payload.get('reason_code') or '').strip() == 'group_banned'
            and payload.get('terminal_confirmed') is True
        ):
            return True
        return bool(
            str(payload.get('trust_status') or '').strip() == 'PERMISSION_DENIED'
            and str(payload.get('reason_code') or payload.get('permission_status') or '').strip()
            in {'not_group_member', 'not_group_admin'}
        )

    def refresh_whatsapp_approval_binding_truth(
        self,
        account_key: str,
        binding_index: int,
        *,
        source: str = 'manual_truth_refresh',
        timeout_seconds: Optional[float] = None,
        _skip_operation_lock: bool = False,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        runtime_actor: Optional[Dict[str, Any]] = None
        normalized_request_id = str(request_id or '').strip() or create_id('approval_op')
        normalized_source = str(source or 'manual_truth_refresh').strip() or 'manual_truth_refresh'
        background_refresh = self._approval_truth_refresh_is_background_source(normalized_source)
        started_monotonic = time.perf_counter()
        stages: List[Dict[str, Any]] = []
        operation_started = False
        final_result: Optional[Dict[str, Any]] = None
        if not _skip_operation_lock:
            existing_operation = self._get_whatsapp_binding_operation_state(account_key, binding_index)
            if isinstance(existing_operation, dict):
                existing_name = str(existing_operation.get('operation') or '').strip()
                if existing_name == 'manual_approve':
                    raise HTTPException(
                        status_code=409,
                        detail={
                            'reason': 'binding_operation_in_progress',
                            'account_key': str(account_key or '').strip() or None,
                            'binding_index': int(binding_index),
                            'active_operation': existing_name or None,
                            'active_operation_label': str(existing_operation.get('operation_label') or '').strip() or self._whatsapp_binding_operation_label(existing_name),
                            'active_detail': str(existing_operation.get('detail') or '').strip() or None,
                            'active_stage_code': str(existing_operation.get('stage_code') or '').strip() or None,
                            'active_stage_label': str(existing_operation.get('stage_label') or '').strip() or None,
                            'request_id': str(existing_operation.get('request_id') or '').strip() or None,
                            'started_at': existing_operation.get('started_at'),
                        },
                    )
        acquisition = self._begin_approval_truth_acquisition(account_key=account_key, binding_index=binding_index, trigger=normalized_source)
        if not bool(acquisition.get('owner')):
            try:
                reused_wait_timeout = float(timeout_seconds if timeout_seconds is not None else 10.0)
            except Exception:
                reused_wait_timeout = 10.0
            return self._wait_for_approval_truth_acquisition(
                acquisition,
                timeout_seconds=min(max(reused_wait_timeout, 1.0), 10.0),
            )
        if not _skip_operation_lock:
            actor_wait_timeout = max(float(timeout_seconds or 45.0), 60.0)
            if background_refresh:
                actor_wait_timeout = min(max(float(timeout_seconds or 1.0), 0.5), 2.0)
            elif normalized_source == 'manual_truth_refresh':
                actor_wait_timeout = min(max(float(timeout_seconds or 5.0), 1.0), 15.0)
                try:
                    if float(timeout_seconds or 0) <= 3.0:
                        actor_wait_timeout = min(max(float(timeout_seconds or 1.0) * 0.35, 0.35), 1.0)
                except Exception:
                    pass
            actor_wait_started = time.perf_counter()
            try:
                runtime_actor = self._acquire_whatsapp_runtime_actor(
                    account_key=account_key,
                    operation='truth_refresh',
                    binding_index=binding_index,
                    wait_timeout_seconds=actor_wait_timeout,
                )
                self._append_truth_acquisition_stage(
                    stages,
                    stage='runtime_actor',
                    status='acquired',
                    elapsed_ms=int(max((time.perf_counter() - actor_wait_started) * 1000.0, 0.0)),
                    wait_timeout_ms=int(max(actor_wait_timeout * 1000.0, 0.0)),
                )
            except Exception as exc:
                self._append_truth_acquisition_stage(
                    stages,
                    stage='runtime_actor',
                    status='error',
                    error=str(exc),
                    elapsed_ms=int(max((time.perf_counter() - actor_wait_started) * 1000.0, 0.0)),
                    wait_timeout_ms=int(max(actor_wait_timeout * 1000.0, 0.0)),
                )
                self._finish_approval_truth_acquisition(acquisition, result=final_result, error=exc)
                raise
        try:
            if not _skip_operation_lock:
                self._mark_whatsapp_binding_operation_started(
                    account_key,
                    binding_index,
                    operation='truth_refresh',
                    detail='正在刷新权威人数',
                    stage_code='live_probe',
                    stage_label='实时取数',
                    request_id=normalized_request_id,
                )
                operation_started = True
            account = self._get_whatsapp_approval_account_runtime_row_lightweight(account_key)
            bindings = list(account.get('group_binding_runtimes') or account.get('group_link_bindings') or [])
            if binding_index < 0 or binding_index >= len(bindings):
                account = self._get_whatsapp_approval_account_runtime_row(account_key)
                bindings = list(account.get('group_binding_runtimes') or account.get('group_link_bindings') or [])
            if binding_index < 0 or binding_index >= len(bindings):
                raise HTTPException(status_code=404, detail='whatsapp approval binding not found')
            binding = dict(bindings[binding_index] or {})
            binding['account_key'] = str(account.get('account_key') or '').strip()
            binding['responsible_type'] = str(binding.get('responsible_type') or account.get('responsible_type') or '').strip()
            if not _skip_operation_lock:
                login_gate_detail = self._whatsapp_approval_binding_operation_login_gate_detail(
                    account=account,
                    binding=binding,
                    binding_index=binding_index,
                    operation='truth_refresh',
                )
                if login_gate_detail:
                    self._update_whatsapp_binding_operation_state(
                        account_key,
                        binding_index,
                        detail=str(login_gate_detail.get('message') or '账号未登录，无法刷新人数'),
                        stage_code='login_preflight_blocked',
                        stage_label='登录校验',
                        request_id=normalized_request_id,
                    )
                    raise HTTPException(status_code=409, detail=login_gate_detail)
            observed_at = utc_now()
            source_priority = 95 if normalized_source == 'manual_truth_refresh' else 70
            self._append_truth_acquisition_stage(stages, stage='live_probe', status='started', trigger=normalized_source)
            if not _skip_operation_lock:
                self._update_whatsapp_binding_operation_state(
                    account_key,
                    binding_index,
                    detail='正在获取最新待审批事实',
                    stage_code='live_probe',
                    stage_label='实时取数',
                    request_id=normalized_request_id,
                )
            binding_runtime: Dict[str, Any] = dict(binding)
            sync_result: Dict[str, Any] = {}
            responsible_type = str(binding.get('responsible_type') or account.get('responsible_type') or '').strip()
            prefer_fast_probe = bool(
                responsible_type == 'official_group'
                and normalized_source in {
                    'manual_truth_refresh',
                    'production_ops_daemon_official_truth_refresh',
                    'lightweight_probe_escalation',
                    'scheduled_full_sync',
                }
            )
            if not sync_result and prefer_fast_probe:
                fast_probe_started = time.perf_counter()
                try:
                    fast_probe_timeout = min(max(float(timeout_seconds or 4.0), 0.5), 4.0)
                except Exception:
                    fast_probe_timeout = 4.0
                self._append_truth_acquisition_stage(stages, stage='official_fast_probe', status='started', trigger=normalized_source, timeout_ms=int(max(fast_probe_timeout * 1000.0, 0.0)))
                try:
                    fast_probe_result = self._probe_official_approval_binding_fast(account=account, binding=binding, timeout_seconds=fast_probe_timeout)
                    binding_runtime = dict(fast_probe_result.get('binding_runtime') or {}) or dict(binding_runtime or binding)
                    live_probe = dict(fast_probe_result.get('probe') or {}) if isinstance(fast_probe_result.get('probe'), dict) else {}
                    fast_sync_result = self._build_current_truth_from_live_probe(
                        binding=binding,
                        binding_runtime=binding_runtime,
                        probe=live_probe,
                        observed_at=observed_at,
                        source=normalized_source,
                    )
                    if normalize_int_or_none(fast_sync_result.get('pending_count')) is not None:
                        sync_result = fast_sync_result
                        persisted_binding = self._persist_whatsapp_approval_binding_probe_identity(
                            account_key,
                            binding_index,
                            binding,
                            sync_result,
                        )
                        if isinstance(persisted_binding, dict) and persisted_binding:
                            binding = {**binding, **persisted_binding}
                            binding_runtime = {**binding_runtime, **persisted_binding}
                        self._append_truth_acquisition_stage(
                            stages,
                            stage='official_fast_probe',
                            status='completed',
                            trust_status=str(sync_result.get('trust_status') or '').strip() or None,
                            reason_code=str(sync_result.get('reason_code') or '').strip() or None,
                            elapsed_ms=int(max((time.perf_counter() - fast_probe_started) * 1000.0, 0.0)),
                        )
                    else:
                        self._append_truth_acquisition_stage(
                            stages,
                            stage='official_fast_probe',
                            status='skipped',
                            reason='pending_count_missing',
                            elapsed_ms=int(max((time.perf_counter() - fast_probe_started) * 1000.0, 0.0)),
                        )
                except Exception as exc:
                    self._append_truth_acquisition_stage(stages, stage='official_fast_probe', status='error', error=str(exc), elapsed_ms=int(max((time.perf_counter() - fast_probe_started) * 1000.0, 0.0)))
            if (
                not sync_result
                and responsible_type == 'official_group'
                and normalized_source == 'manual_truth_refresh'
                and timeout_seconds is not None
                and float(timeout_seconds or 0) <= 4.0
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        'reason': 'official_fast_truth_unavailable',
                        'account_key': str(account_key or '').strip() or None,
                        'binding_index': int(binding_index),
                    },
                )
            short_foreground_refresh = bool(
                normalized_source == 'manual_truth_refresh'
                and timeout_seconds is not None
                and float(timeout_seconds or 0) <= 4.0
            )
            if not sync_result and not short_foreground_refresh:
                bridge_snapshot = self._fetch_registration_group_bridge_snapshot(account=account, binding=binding)
                bridge_result = self._build_registration_group_bridge_result(
                    account=account,
                    binding=binding,
                    snapshot=bridge_snapshot,
                    acquisition_result=None,
                )
                if bridge_result and self._approval_truth_result_is_commit_candidate(bridge_result):
                    sync_result = self._decorate_approval_truth_result(
                        account=account,
                        binding=binding,
                        result=bridge_result,
                        account_key=account_key,
                    )
                    binding_runtime = {
                        **binding_runtime,
                        'group_id': sync_result.get('group_id') or binding_runtime.get('group_id'),
                        'registration_group': sync_result.get('group_id') or binding_runtime.get('registration_group'),
                        'group_name': sync_result.get('group_name') or binding_runtime.get('group_name'),
                        'authenticated': True,
                        'ready': True,
                    }
                    self._append_truth_acquisition_stage(
                        stages,
                        stage=str((sync_result.get('source') or {}).get('mode') or 'registration_group_poc_bridge'),
                        status='completed',
                        trust_status=str(sync_result.get('trust_status') or '').strip() or None,
                        reason_code=str(sync_result.get('reason_code') or '').strip() or None,
                    )
            provider_decision: Dict[str, Any] = {}
            baileys_base_url = ''
            if not sync_result:
                provider_decision = self.whatsapp_approval_runtime_adapter.provider_decision(
                    account=account,
                    binding={**dict(binding or {}), 'responsible_type': str(binding.get('responsible_type') or account.get('responsible_type') or '').strip() or 'registration_group'},
                ).to_dict()
                baileys_base_url = self._resolve_baileys_runtime_base_url(
                    account=account,
                    binding=binding,
                    runtime_state=dict(account.get('runtime_state') or {}),
                )
            if not sync_result and bool(provider_decision.get('authoritative_read')) and baileys_base_url:
                self._append_truth_acquisition_stage(stages, stage='baileys_poc_probe', status='started', trigger=normalized_source)
                try:
                    baileys_result = self._call_baileys_full_queue_sync(
                        account=account,
                        binding=binding,
                        timeout_seconds=(min(max(float(timeout_seconds or 5.0), 1.0), 5.0) if background_refresh else float(timeout_seconds or 45.0)),
                        priority=(
                            'P0'
                            if normalized_source == 'manual_truth_refresh'
                            else 'P2'
                            if normalized_source in {'scheduled_full_sync', 'lightweight_probe_escalation', 'production_ops_daemon_official_truth_refresh'}
                            else 'P1'
                        ),
                    )
                    if self._approval_truth_result_is_commit_candidate(dict(baileys_result or {})):
                        sync_result = self._decorate_approval_truth_result(
                            account=account,
                            binding=binding,
                            result=dict(baileys_result or {}),
                            account_key=account_key,
                        )
                        persisted_binding = self._persist_whatsapp_approval_binding_probe_identity(
                            account_key,
                            binding_index,
                            binding,
                            sync_result,
                        )
                        if isinstance(persisted_binding, dict) and persisted_binding:
                            binding = {**binding, **persisted_binding}
                            binding_runtime = {**binding_runtime, **persisted_binding}
                        binding_runtime = {
                            **binding_runtime,
                            'group_id': sync_result.get('group_id') or binding_runtime.get('group_id'),
                            'registration_group': sync_result.get('group_id') or binding_runtime.get('registration_group'),
                            'group_name': sync_result.get('group_name') or binding_runtime.get('group_name'),
                            'authenticated': True,
                            'ready': True,
                        }
                        self._append_truth_acquisition_stage(
                            stages,
                            stage='baileys_poc_probe',
                            status='completed',
                            trust_status=str(sync_result.get('trust_status') or '').strip() or None,
                            reason_code=str(sync_result.get('reason_code') or '').strip() or None,
                        )
                    else:
                        self._append_truth_acquisition_stage(
                            stages,
                            stage='baileys_poc_probe',
                            status='skipped',
                            reason=str((baileys_result or {}).get('reason_code') or 'pending_count_missing'),
                        )
                except Exception as exc:
                    self._append_truth_acquisition_stage(stages, stage='baileys_poc_probe', status='error', error=str(exc))
            if not sync_result and short_foreground_refresh:
                raise HTTPException(
                    status_code=409,
                    detail={
                        'reason': 'foreground_truth_unavailable',
                        'account_key': str(account_key or '').strip() or None,
                        'binding_index': int(binding_index),
                    },
                )
            if not sync_result:
                probe_result = self.refresh_whatsapp_approval_binding_probe(
                    account_key,
                    binding_index,
                    probe_mode='strict',
                    _skip_operation_lock=True,
                )
                binding_runtime = dict(probe_result.get('binding_runtime') or {})
                live_probe = dict(probe_result.get('probe') or {})
                sync_result = self._build_current_truth_from_live_probe(
                    binding=binding,
                    binding_runtime=binding_runtime,
                    probe=live_probe,
                    observed_at=observed_at,
                    source=normalized_source,
                )
            latest_probe_write = self.upsert_approval_queue_latest_probe(
                account_key=account_key,
                binding=binding_runtime or binding,
                probe_result={
                    **sync_result,
                    'source': {
                        **(dict(sync_result.get('source') or {}) if isinstance(sync_result.get('source'), dict) else {}),
                        'mode': 'latest_probe_debug',
                        'diagnostic_only': True,
                    },
                },
                observed_at=observed_at,
            )
            self._append_truth_acquisition_stage(
                stages,
                stage='live_probe',
                status='completed',
                trust_status=str(sync_result.get('trust_status') or '').strip() or None,
                reason_code=str(sync_result.get('reason_code') or '').strip() or None,
            )
            if not _skip_operation_lock:
                self._update_whatsapp_binding_operation_state(
                    account_key,
                    binding_index,
                    detail='正在写入 current_truth',
                    stage_code='write_current_truth',
                    stage_label='写入主真值',
                    request_id=normalized_request_id,
                )
            current_truth_write_started = time.perf_counter()
            current_truth_write = self.upsert_approval_queue_current_truth(
                account_key=account_key,
                binding=binding_runtime or binding,
                sync_result=sync_result,
                source_priority=source_priority,
                observed_at=observed_at,
                force=True,
            )
            self._append_truth_acquisition_stage(
                stages,
                stage='write_current_truth',
                status='completed' if bool((current_truth_write or {}).get('written')) else 'skipped',
                reason=(current_truth_write or {}).get('reason'),
                elapsed_ms=int(max((time.perf_counter() - current_truth_write_started) * 1000.0, 0.0)),
            )
            snapshots = self._load_approval_binding_queue_snapshots(account_key, binding_runtime or binding)
            view = self._approval_queue_truth_view(snapshots.get('current_truth'), snapshots.get('latest_probe'))
            response_can_manual_approve = bool(view.get('can_manual_approve'))
            result = {
                **sync_result,
                'approval_queue_truth': view,
                'can_manual_approve': response_can_manual_approve,
                'manual_approve_allowed': response_can_manual_approve,
                'foreground_budget_ms': int(max(float(timeout_seconds or 45.0), 0.0) * 1000),
            }
            final_result = self._finalize_truth_acquisition_result(
                acquisition_id=str(acquisition.get('acquisition_id') or ''),
                trigger=normalized_source,
                result=result,
                stages=stages,
                latest_probe_write=latest_probe_write,
                current_truth_write=current_truth_write,
                started_monotonic=started_monotonic,
            )
            self._upsert_truth_acquisition_log(
                acquisition_id=str(final_result.get('truth_acquisition_id') or acquisition.get('acquisition_id') or ''),
                account_key=account_key,
                binding=binding_runtime or binding,
                trigger=normalized_source,
                result=final_result,
                stages=stages,
            )
            self._finish_approval_truth_acquisition(acquisition, result=final_result)
            return final_result
        except Exception as exc:
            self._append_truth_acquisition_stage(stages, stage='failed', status='error', error=str(exc))
            self._finish_approval_truth_acquisition(acquisition, result=final_result, error=exc)
            raise
        finally:
            if operation_started and not _skip_operation_lock:
                self._clear_whatsapp_binding_operation(account_key, binding_index)
            if not _skip_operation_lock:
                self._release_whatsapp_runtime_actor(runtime_actor)
    def full_sync_whatsapp_approval_binding(
        self,
        account_key: str,
        binding_index: int,
        *,
        source: str = 'manual_full_sync',
        timeout_seconds: Optional[float] = None,
        _skip_operation_lock: bool = False,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        runtime_actor: Optional[Dict[str, Any]] = None
        normalized_request_id = str(request_id or '').strip() or create_id('approval_op')
        if not _skip_operation_lock:
            existing_operation = self._get_whatsapp_binding_operation_state(account_key, binding_index)
            if isinstance(existing_operation, dict):
                existing_name = str(existing_operation.get('operation') or '').strip()
                if existing_name == 'manual_approve':
                    raise HTTPException(
                        status_code=409,
                        detail={
                            'reason': 'binding_operation_in_progress',
                            'account_key': str(account_key or '').strip() or None,
                            'binding_index': int(binding_index),
                            'active_operation': existing_name or None,
                            'active_operation_label': str(existing_operation.get('operation_label') or '').strip() or self._whatsapp_binding_operation_label(existing_name),
                            'active_detail': str(existing_operation.get('detail') or '').strip() or None,
                            'active_stage_code': str(existing_operation.get('stage_code') or '').strip() or None,
                            'active_stage_label': str(existing_operation.get('stage_label') or '').strip() or None,
                            'request_id': str(existing_operation.get('request_id') or '').strip() or None,
                            'started_at': existing_operation.get('started_at'),
                        },
                    )
        acquisition = self._begin_approval_truth_acquisition(account_key=account_key, binding_index=binding_index, trigger=source)
        if not bool(acquisition.get('owner')):
            return self._wait_for_approval_truth_acquisition(acquisition)
        if not _skip_operation_lock:
            actor_wait_timeout = max(float(timeout_seconds or 45.0), 90.0)
            if str(source or '').strip() == 'manual_truth_refresh':
                actor_wait_timeout = max(float(timeout_seconds or 20.0), 1.0)
            runtime_actor = self._acquire_whatsapp_runtime_actor(
                account_key=account_key,
                operation='full_sync',
                binding_index=binding_index,
                wait_timeout_seconds=actor_wait_timeout,
            )
        started_monotonic = time.perf_counter()
        stages: List[Dict[str, Any]] = []
        operation_started = False
        final_result: Optional[Dict[str, Any]] = None
        try:
            if not _skip_operation_lock:
                self._mark_whatsapp_binding_operation_started(
                    account_key,
                    binding_index,
                    operation='full_sync',
                    detail='正在执行完整同步',
                    stage_code='worker_sync',
                    stage_label='同步审批队列',
                    request_id=normalized_request_id,
                )
                operation_started = True
            account = self._get_whatsapp_approval_account_runtime_row(account_key)
            bindings = list(account.get('group_binding_runtimes') or account.get('group_link_bindings') or [])
            if binding_index < 0 or binding_index >= len(bindings):
                raise HTTPException(status_code=404, detail='whatsapp approval binding not found')
            binding = dict(bindings[binding_index] or {})
            binding['account_key'] = str(account.get('account_key') or '').strip()
            binding['responsible_type'] = str(binding.get('responsible_type') or account.get('responsible_type') or '').strip()
            if not _skip_operation_lock:
                login_gate_detail = self._whatsapp_approval_binding_operation_login_gate_detail(
                    account=account,
                    binding=binding,
                    binding_index=binding_index,
                    operation='full_sync',
                )
                if login_gate_detail:
                    self._update_whatsapp_binding_operation_state(
                        account_key,
                        binding_index,
                        detail=str(login_gate_detail.get('message') or '账号未登录，无法执行完整同步'),
                        stage_code='login_preflight_blocked',
                        stage_label='登录校验',
                        request_id=normalized_request_id,
                    )
                    raise HTTPException(status_code=409, detail=login_gate_detail)
            priority_by_source = {
                'manual_truth_refresh': 100,
                'manual_full_sync': 100,
                'manual_approve_preflight': 100,
                'official_manual_approve_preflight': 100,
                'official_ready_precise_sync': 100,
                'approval_after_sync': 90,
                'scheduled_full_sync': 60,
                'lightweight_probe_escalation': 60,
            }
            source_priority = priority_by_source.get(str(source or ''), 60)
            hard_timeout = float(timeout_seconds if timeout_seconds is not None else (45.0 if source == 'manual_full_sync' else 30.0))
            observed_at = utc_now()
            registration_group = self._whatsapp_binding_runtime_group_id(binding)
            if not _skip_operation_lock:
                self._update_whatsapp_binding_operation_state(
                    account_key,
                    binding_index,
                    detail='正在同步审批队列',
                    stage_code='worker_sync',
                    stage_label='同步审批队列',
                    request_id=normalized_request_id,
                )
            self._append_truth_acquisition_stage(stages, stage='worker_sync', status='started', trigger=source)
            result = self._acquire_approval_truth_minimal(
                account_key=account_key,
                account=account,
                binding=binding,
                registration_group=registration_group,
                source=source,
                hard_timeout=hard_timeout,
                allow_soft_reload=source in {'manual_truth_refresh', 'manual_full_sync', 'manual_approve_preflight', 'official_manual_approve_preflight', 'official_ready_precise_sync', 'approval_after_sync', 'scheduled_full_sync', 'lightweight_probe_escalation'},
            )
            result = self._normalize_approval_truth_result(
                account_key=account_key,
                binding=binding,
                result=result,
            )
            if not isinstance(result, dict):
                result = {'ok': False, 'trust_status': 'UNTRUSTED_SYNC_INVALID', 'reason_code': 'invalid_worker_response', 'source': source}
            result.setdefault('source', source)
            result['foreground_budget_ms'] = int(max(hard_timeout, 0.0) * 1000)
            trust_status = str(result.get('trust_status') or '').strip()
            result_source = dict(result.get('source') or {}) if isinstance(result.get('source'), dict) else {}
            if str(result_source.get('full_sync_provider') or '').strip() == 'official_group_authoritative_baileys':
                source_priority = max(source_priority, 80)
            self._append_truth_acquisition_stage(
                stages,
                stage='worker_sync',
                status='completed',
                trust_status=trust_status or None,
                reason_code=str(result.get('reason_code') or '').strip() or None,
            )
            latest_probe_write = {
                'written': False,
                'reason': 'single_truth_current_truth_only',
            }
            self._append_truth_acquisition_stage(
                stages,
                stage='write_latest_probe',
                status='skipped',
                reason=latest_probe_write.get('reason'),
            )
            current_truth_write: Optional[Dict[str, Any]] = None
            permission_state_confirmed = bool(
                trust_status == 'PERMISSION_DENIED'
                and str(result.get('reason_code') or '').strip() in {'not_group_member', 'not_group_admin'}
            )
            group_banned_confirmed = bool(
                trust_status == 'GROUP_BANNED'
                and str(result.get('reason_code') or '').strip() == 'group_banned'
                and result.get('terminal_confirmed') is True
            )
            if trust_status.startswith('TRUSTED') or permission_state_confirmed or group_banned_confirmed:
                current_truth_write = self.upsert_approval_queue_current_truth(
                    account_key=account_key,
                    binding=binding,
                    sync_result=result,
                    source_priority=source_priority,
                    observed_at=observed_at,
                    force=permission_state_confirmed or group_banned_confirmed or source in {'manual_truth_refresh', 'manual_full_sync', 'manual_approve_preflight', 'official_manual_approve_preflight', 'official_ready_precise_sync', 'approval_after_sync'},
                )
            self._append_truth_acquisition_stage(
                stages,
                stage='write_current_truth',
                status='completed' if (current_truth_write or {}).get('written') else 'skipped',
                reason=(current_truth_write or {}).get('reason') if current_truth_write is not None else ('trust_status_not_trusted' if not trust_status.startswith('TRUSTED') else 'no_current_truth_write'),
            )
            snapshots = self._load_approval_binding_queue_snapshots(account_key, binding)
            view = self._approval_queue_truth_view(
                snapshots.get('current_truth'),
                snapshots.get('latest_probe'),
            )
            response_can_manual_approve = bool((current_truth_write or {}).get('written') and trust_status == 'TRUSTED_CONFIRMED_PENDING')
            if (
                not response_can_manual_approve
                and bool(result.get('manual_override_eligible'))
                and str(result.get('reason_code') or '').strip() in {'api_pending_ui_not_converged', 'untrusted_ui_not_converged'}
                and result.get('self_participant_found') is True
                and result.get('self_is_admin') is True
                and result.get('can_manage_membership_requests') is True
            ):
                response_can_manual_approve = True
            final_result = self._finalize_truth_acquisition_result(
                acquisition_id=str(acquisition.get('acquisition_id') or ''),
                trigger=source,
                result={**result, 'can_manual_approve': response_can_manual_approve, 'approval_queue_truth': view},
                stages=stages,
                latest_probe_write=latest_probe_write,
                current_truth_write=current_truth_write,
                started_monotonic=started_monotonic,
            )
            self._upsert_truth_acquisition_log(
                acquisition_id=str(final_result.get('truth_acquisition_id') or acquisition.get('acquisition_id') or ''),
                account_key=account_key,
                binding=binding,
                trigger=source,
                result=final_result,
                stages=stages,
            )
            self._finish_approval_truth_acquisition(acquisition, result=final_result)
            return final_result
        except Exception as exc:
            self._append_truth_acquisition_stage(stages, stage='failed', status='error', error=str(exc))
            self._finish_approval_truth_acquisition(acquisition, result=final_result, error=exc)
            raise
        finally:
            if operation_started and not _skip_operation_lock:
                self._clear_whatsapp_binding_operation(account_key, binding_index)
            if not _skip_operation_lock:
                self._release_whatsapp_runtime_actor(runtime_actor)



_COMPAT_TARGETS = (
    _shared_module,
    _app_module,
    _service_group_atmosphere, _service_intake, _service_approval, _service_timo, _service_whatsapp, _service_executor,
)

_app_module.Service = Service
for _target in _COMPAT_TARGETS[2:]:
    _target.Service = Service


@_wraps(_app_module.create_app)
def create_app(*args, **kwargs):
    _app_module.Service = Service
    return _app_module.create_app(*args, **kwargs)


class _MainCompatibilityModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__") or name in {"Service", "create_app", "app"}:
            return
        for target in _COMPAT_TARGETS:
            if name in target.__dict__:
                target.__dict__[name] = value


_sys.modules[__name__].__class__ = _MainCompatibilityModule

_GLOBAL_APP_BOOTSTRAP_DISABLED = _app_module._GLOBAL_APP_BOOTSTRAP_DISABLED
app = None if _GLOBAL_APP_BOOTSTRAP_DISABLED else create_app()
