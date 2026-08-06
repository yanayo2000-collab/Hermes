from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.im_result_message_facts import (
    ensure_im_result_message_tables,
    im_result_message_summary,
    result_message_chain_steps,
)


TAXONOMY_VERSION = 'im_diagnosis_taxonomy_v1'
PROMPT_VERSION = 'fixture_rules_v1'
DEFAULT_IM_DIAGNOSTICS_RUN_ID = 'timetrade_im_api_last7d'
YESTERDAY_IM_DIAGNOSTICS_RUN_ID = 'timetrade_im_api_yesterday'
HERMES_LLM_PROVIDER_MODE = 'hermes_llm'
HERMES_LLM_PROMPT_VERSION = 'hermes_im_diagnosis_prompt_v8_v2_3'
IM_LLM_TASK_STATUS_QUEUED = 'queued'
IM_LLM_TASK_STATUS_CLAIMED = 'claimed'
IM_LLM_TASK_STATUS_COMPLETED = 'completed'
IM_LLM_TASK_STATUS_FAILED = 'failed'

IM_FUNNEL_STATES = {
    'lead_created',
    'first_message_sent',
    'first_reply',
    'interest_confirmed',
    'linky_explained',
    'safety_explained',
    'linky_link_sent',
    'linky_link_clicked',
    'linky_registered',
    'linky_id_requested',
    'linky_id_received',
    'bind_submitted',
    'bind_success',
    'bind_failed',
    'group_invited',
    'group_joined',
    'first_message_received',
    'first_diamond_seen',
    'withdrawal_rule_viewed',
    'inactive',
    'blocked_or_complained',
}

IM_USER_CONCERN_TYPES = {
    'scam_concern',
    'fee_concern',
    'privacy_concern',
    'app_install_concern',
    'income_uncertainty',
    'withdrawal_concern',
    'adult_concern',
    'time_effort_concern',
    'language_barrier',
    'technical_issue',
    'no_response',
    'ad_mismatch',
    'agency_settlement_question',
}

IM_SCRIPT_RISK_DIMENSIONS = (
    'income_promise_risk',
    'withdrawal_promise_risk',
    'adult_implication_risk',
    'misrepresentation_risk',
    'urgency_pressure_risk',
    'payment_scam_risk',
    'platform_impersonation_risk',
    'privacy_request_risk',
    'whatsapp_redirect_risk',
    'ad_mismatch_risk',
)

IM_LAUNCH_DECISIONS = {'allow_testing', 'needs_human_review', 'blocked_by_risk', 'insufficient_context'}

PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ('email', re.compile(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', re.IGNORECASE)),
    ('phone', re.compile(r'(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)')),
    ('whatsapp', re.compile(r'\b(?:whatsapp|wa|zap|zapi)\s*[:：]?\s*\+?\d[\d\s().-]{6,}\d\b', re.IGNORECASE)),
    ('url_token', re.compile(r'https?://\S*(?:token|auth|code|key|secret|session|jwt)=\S+', re.IGNORECASE)),
    ('bank_card', re.compile(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)')),
)

DROP_ORDER = (
    'entered_im',
    'first_user_reply',
    'im_message_ge_3',
    'link_sent',
    'link_clicked',
    'linky_registered',
    'bind_succeeded',
    'crm_succeeded',
    'real_join_succeeded',
)

DIAGNOSIS_LABELS = {
    'cs_first_response_slow': '客服首响过慢',
    'opening_trust_missing': '开场缺少信任建立',
    'linky_trust_explanation_missing': '没有解释对应 App 的作用',
    'early_registration_push': '过早要求注册',
    'objection_not_addressed': '没有回应用户顾虑',
    'unclear_steps': '步骤说明不清楚',
    'intent_misread': '用户意图识别失败',
    'language_localization_weak': '语言不自然 / 本地化差',
    'overpromise_risk': '话术过度承诺',
    'scam_like_script': '话术像诈骗',
    'high_intent_not_advanced': '用户已高意向但客服推进失败',
    'low_intent_user': '用户低意向 / 非目标用户',
    'silent_user_not_reactivated': '用户沉默未唤醒',
    'auto_message_handoff_failed': '系统自动消息有效但人工承接失败',
    'linky_registration_guidance_failed': 'App 注册引导失败',
    'bind_guidance_failed': 'bind 引导失败',
    'crm_process_issue': 'CRM / 流程问题',
    'ad_promise_mismatch': '广告承诺偏差',
    'success_sample': '成功入会样本',
    'data_insufficient': '数据不足',
}

SCRIPT_ATTRIBUTION_LABELS = {
    'explanation_unclear': '解释不清',
    'too_pushy': '话术太强势',
    'linky_value_missing': '没有解释对应 App 作用',
    'next_step_unclear': '下一步不清楚',
    'trust_not_built': '信任建立不足',
    'tone_too_template': '模板感太强',
    'objection_unanswered': '用户顾虑未回应',
    'history_success_pattern_missing': '没有复用成功话术模式',
    'other': '其他话术归因',
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(*parts: Any, prefix: str = '') -> str:
    digest = hashlib.sha1('|'.join(str(part or '') for part in parts).encode('utf-8')).hexdigest()[:20]
    return f'{prefix}{digest}' if prefix else digest


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _normalize_script_risk_score(value: Any) -> Dict[str, int]:
    if isinstance(value, str):
        value = _loads(value, {})
    scores = {key: 0 for key in IM_SCRIPT_RISK_DIMENSIONS}
    if isinstance(value, dict):
        for key in IM_SCRIPT_RISK_DIMENSIONS:
            raw = value.get(key, 0)
            try:
                scores[key] = max(0, min(3, int(float(raw or 0))))
            except Exception:
                scores[key] = 0
    elif isinstance(value, list):
        for item in value:
            key = ''
            raw_score: Any = 0
            if isinstance(item, dict):
                key = str(item.get('risk_type') or item.get('key') or item.get('dimension') or '').strip()
                raw_score = item.get('score', 0)
            else:
                key = str(item or '').strip()
                raw_score = 1
            if key in scores:
                try:
                    scores[key] = max(scores[key], max(0, min(3, int(float(raw_score or 0)))))
                except Exception:
                    scores[key] = max(scores[key], 1)
    return scores


def _max_script_risk_score(value: Any) -> int:
    scores = _normalize_script_risk_score(value)
    return max(scores.values()) if scores else 0


def _normalize_launch_decision(value: Any, risk_score: Any, *, has_complete_script: bool = True) -> str:
    if _max_script_risk_score(risk_score) >= 3:
        return 'blocked_by_risk'
    decision = str(value or '').strip()
    if decision in IM_LAUNCH_DECISIONS:
        return decision
    return 'needs_human_review' if has_complete_script else 'insufficient_context'


def _normalize_funnel_state(value: Any, fallback_stage: Any = '') -> str:
    state = str(value or '').strip()
    if state in IM_FUNNEL_STATES:
        return state
    stage = str(fallback_stage or '').strip()
    mapping = {
        'before_first_user_reply': 'first_message_sent',
        'before_im_message_ge_3': 'first_reply',
        'before_link_sent': 'interest_confirmed',
        'before_link_clicked': 'linky_link_sent',
        'after_link_click_before_bind': 'linky_link_clicked',
        'before_bind_success': 'bind_submitted',
        'before_joined': 'bind_success',
    }
    return mapping.get(stage, '')


def _normalize_user_concern_type(value: Any, fallback: Any = '') -> str:
    concern = str(value or '').strip()
    if concern in IM_USER_CONCERN_TYPES:
        return concern
    text = str(fallback or '').lower()
    if any(word in text for word in ['安全', '骗', 'scam', 'golpe']):
        return 'scam_concern'
    if any(word in text for word in ['收费', '充值', 'free', 'grátis', 'gratis']):
        return 'fee_concern'
    if 'id' in text or '隐私' in text or 'privacy' in text:
        return 'privacy_concern'
    if any(word in text for word in ['提现', 'saque', 'withdraw']):
        return 'withdrawal_concern'
    if any(word in text for word in ['linky', '广告', 'ad ', 'mismatch']):
        return 'ad_mismatch'
    return 'no_response'


def scan_pii(text: str) -> Dict[str, Any]:
    raw = str(text or '')
    tags: List[str] = []
    redacted = raw
    for tag, pattern in PII_PATTERNS:
        if pattern.search(redacted):
            tags.append(tag)
            redacted = pattern.sub(f'[{tag.upper()}_REDACTED]', redacted)
    return {
        'status': 'blocked' if tags else 'passed',
        'tags': sorted(set(tags)),
        'redacted_text': redacted,
    }


def ensure_im_diagnostics_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS im_conversations (
            conversation_id TEXT PRIMARY KEY,
            anonymous_user_id TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT '',
            media_source TEXT NOT NULL DEFAULT '',
            external_app TEXT NOT NULL DEFAULT '',
            campaign_id TEXT NOT NULL DEFAULT '',
            campaign_name TEXT NOT NULL DEFAULT '',
            adset_id TEXT NOT NULL DEFAULT '',
            adset_name TEXT NOT NULL DEFAULT '',
            ad_id TEXT NOT NULL DEFAULT '',
            ad_name TEXT NOT NULL DEFAULT '',
            creative_id TEXT NOT NULL DEFAULT '',
            ad_account_id TEXT NOT NULL DEFAULT '',
            entered_im_at TEXT NOT NULL DEFAULT '',
            conversation_start_time TEXT NOT NULL DEFAULT '',
            conversation_end_time TEXT NOT NULL DEFAULT '',
            first_user_message_at TEXT NOT NULL DEFAULT '',
            first_agent_reply_at TEXT NOT NULL DEFAULT '',
            first_response_seconds REAL NOT NULL DEFAULT 0,
            final_join_status TEXT NOT NULL DEFAULT '',
            final_outcome TEXT NOT NULL DEFAULT '',
            dropoff_stage TEXT NOT NULL DEFAULT '',
            dropoff_time TEXT NOT NULL DEFAULT '',
            agent_id_hash TEXT NOT NULL DEFAULT '',
            agent_team TEXT NOT NULL DEFAULT '',
            agent_shift TEXT NOT NULL DEFAULT '',
            handoff_type TEXT NOT NULL DEFAULT '',
            data_quality_status TEXT NOT NULL DEFAULT 'unchecked',
            pii_scan_status TEXT NOT NULL DEFAULT 'unchecked',
            attribution_quality_status TEXT NOT NULL DEFAULT 'unchecked',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS im_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            message_index INTEGER NOT NULL DEFAULT 0,
            sender_type TEXT NOT NULL DEFAULT '',
            message_type TEXT NOT NULL DEFAULT 'text',
            message_at TEXT NOT NULL DEFAULT '',
            message_text_redacted TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT '',
            template_id TEXT NOT NULL DEFAULT '',
            template_name TEXT NOT NULL DEFAULT '',
            is_auto_message INTEGER NOT NULL DEFAULT 0,
            is_template_message INTEGER NOT NULL DEFAULT 0,
            is_human_agent_message INTEGER NOT NULL DEFAULT 0,
            has_link INTEGER NOT NULL DEFAULT 0,
            link_id_hash TEXT NOT NULL DEFAULT '',
            pii_scan_status TEXT NOT NULL DEFAULT 'unchecked',
            risk_tags_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS im_conversion_events (
            event_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            anonymous_user_id TEXT NOT NULL DEFAULT '',
            event_name TEXT NOT NULL,
            event_time TEXT NOT NULL DEFAULT '',
            event_status TEXT NOT NULL DEFAULT '',
            event_source TEXT NOT NULL DEFAULT '',
            external_app TEXT NOT NULL DEFAULT '',
            campaign_id TEXT NOT NULL DEFAULT '',
            adset_id TEXT NOT NULL DEFAULT '',
            ad_id TEXT NOT NULL DEFAULT '',
            creative_id TEXT NOT NULL DEFAULT '',
            link_id_hash TEXT NOT NULL DEFAULT '',
            link_url_domain TEXT NOT NULL DEFAULT '',
            link_url_type TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            error_message_redacted TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS im_bot_timing_facts (
            conversation_id TEXT PRIMARY KEY,
            anonymous_user_id TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            is_high_value INTEGER NOT NULL DEFAULT 0,
            entered_im_at TEXT NOT NULL DEFAULT '',
            reception_status TEXT NOT NULL DEFAULT '',
            current_assignee_type TEXT NOT NULL DEFAULT '',
            guild_join_status TEXT NOT NULL DEFAULT '',
            handoff_classification TEXT NOT NULL DEFAULT '',
            auto_apply_sent_at TEXT NOT NULL DEFAULT '',
            auto_apply_source TEXT NOT NULL DEFAULT '',
            r1_sent_at TEXT NOT NULL DEFAULT '',
            r1_source TEXT NOT NULL DEFAULT '',
            r1_after_auto_apply_seconds REAL,
            r1_after_auto_apply_within_60 INTEGER NOT NULL DEFAULT 0,
            r1_step_triggered_at TEXT NOT NULL DEFAULT '',
            link_sent_at TEXT NOT NULL DEFAULT '',
            link_clicked_at TEXT NOT NULL DEFAULT '',
            guild_bind_request_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS im_conversation_diagnoses (
            diagnosis_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            diagnosis_run_id TEXT NOT NULL,
            model_provider TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '',
            prompt_version TEXT NOT NULL DEFAULT '',
            taxonomy_version TEXT NOT NULL DEFAULT '',
            final_outcome TEXT NOT NULL DEFAULT '',
            dropoff_stage TEXT NOT NULL DEFAULT '',
            primary_diagnosis TEXT NOT NULL DEFAULT '',
            secondary_diagnoses_json TEXT NOT NULL DEFAULT '[]',
            user_intent TEXT NOT NULL DEFAULT '',
            user_objection TEXT NOT NULL DEFAULT '',
            agent_issue TEXT NOT NULL DEFAULT '',
            critical_turn_index INTEGER NOT NULL DEFAULT 0,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            recommended_replacement_json TEXT NOT NULL DEFAULT '{}',
            action_type TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL DEFAULT 'low',
            needs_human_review INTEGER NOT NULL DEFAULT 1,
            human_review_status TEXT NOT NULL DEFAULT 'pending',
            human_review_comment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS im_aggregate_diagnoses (
            aggregate_id TEXT PRIMARY KEY,
            diagnosis_run_id TEXT NOT NULL,
            date TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            media_source TEXT NOT NULL DEFAULT '',
            campaign_id TEXT NOT NULL DEFAULT '',
            adset_id TEXT NOT NULL DEFAULT '',
            ad_id TEXT NOT NULL DEFAULT '',
            creative_id TEXT NOT NULL DEFAULT '',
            sample_conversations INTEGER NOT NULL DEFAULT 0,
            successful_conversations INTEGER NOT NULL DEFAULT 0,
            lost_conversations INTEGER NOT NULL DEFAULT 0,
            top_failure_reasons_json TEXT NOT NULL DEFAULT '[]',
            response_time_summary_json TEXT NOT NULL DEFAULT '{}',
            dropoff_summary_json TEXT NOT NULL DEFAULT '{}',
            recommended_actions_json TEXT NOT NULL DEFAULT '[]',
            linked_ad_diagnosis_type TEXT NOT NULL DEFAULT '',
            linked_ad_action_type TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS im_script_suggestions (
            script_suggestion_id TEXT PRIMARY KEY,
            country TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT '',
            scenario TEXT NOT NULL DEFAULT '',
            diagnosis_type TEXT NOT NULL DEFAULT '',
            funnel_stage TEXT NOT NULL DEFAULT '',
            target_metric TEXT NOT NULL DEFAULT '',
            experiment_hypothesis TEXT NOT NULL DEFAULT '',
            old_script_summary TEXT NOT NULL DEFAULT '',
            old_script_summary_translation_zh TEXT NOT NULL DEFAULT '',
            old_script_summary_translation_source TEXT NOT NULL DEFAULT '',
            old_script_summary_interpretation_zh TEXT NOT NULL DEFAULT '',
            old_script_summary_interpretation_source TEXT NOT NULL DEFAULT '',
            suggested_script TEXT NOT NULL DEFAULT '',
            suggested_script_translation_zh TEXT NOT NULL DEFAULT '',
            suggested_script_source TEXT NOT NULL DEFAULT '',
            suggested_script_translation_source TEXT NOT NULL DEFAULT '',
            risk_tags_json TEXT NOT NULL DEFAULT '[]',
            risk_score_json TEXT NOT NULL DEFAULT '{}',
            max_risk_score INTEGER NOT NULL DEFAULT 0,
            launch_decision TEXT NOT NULL DEFAULT 'needs_human_review',
            current_state TEXT NOT NULL DEFAULT '',
            user_concern_type TEXT NOT NULL DEFAULT '',
            source_conversation_count INTEGER NOT NULL DEFAULT 0,
            success_pattern_summary TEXT NOT NULL DEFAULT '',
            evidence_summary_json TEXT NOT NULL DEFAULT '{}',
            experiment_design_json TEXT NOT NULL DEFAULT '{}',
            approval_status TEXT NOT NULL DEFAULT 'draft',
            approved_by TEXT NOT NULL DEFAULT '',
            approved_at TEXT NOT NULL DEFAULT '',
            experiment_status TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS im_script_experiments (
            experiment_id TEXT PRIMARY KEY,
            script_suggestion_id TEXT NOT NULL,
            diagnosis_type TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT '',
            funnel_stage TEXT NOT NULL DEFAULT '',
            target_metric TEXT NOT NULL DEFAULT '',
            primary_metric TEXT NOT NULL DEFAULT '',
            old_script_summary TEXT NOT NULL DEFAULT '',
            suggested_script TEXT NOT NULL DEFAULT '',
            suggested_script_translation_zh TEXT NOT NULL DEFAULT '',
            experiment_design_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'shadow_review',
            sample_target INTEGER NOT NULL DEFAULT 0,
            observed_sample INTEGER NOT NULL DEFAULT 0,
            baseline_value REAL,
            experiment_value REAL,
            guardrail_status TEXT NOT NULL DEFAULT 'unchecked',
            decision TEXT NOT NULL DEFAULT '',
            review_summary TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            ended_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS im_llm_diagnosis_tasks (
            task_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            diagnosis_run_id TEXT NOT NULL,
            provider_mode TEXT NOT NULL DEFAULT 'hermes_llm',
            status TEXT NOT NULL DEFAULT 'queued',
            prompt_version TEXT NOT NULL DEFAULT 'hermes_im_diagnosis_prompt_v1',
            taxonomy_version TEXT NOT NULL DEFAULT 'im_diagnosis_taxonomy_v1',
            payload_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            claimed_at TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        """
    )
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(im_conversations)").fetchall()}
    if 'handoff_type' not in existing:
        conn.execute("ALTER TABLE im_conversations ADD COLUMN handoff_type TEXT NOT NULL DEFAULT ''")
    if 'external_app' not in existing:
        conn.execute("ALTER TABLE im_conversations ADD COLUMN external_app TEXT NOT NULL DEFAULT ''")
    event_existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(im_conversion_events)").fetchall()}
    if 'link_url_domain' not in event_existing:
        conn.execute("ALTER TABLE im_conversion_events ADD COLUMN link_url_domain TEXT NOT NULL DEFAULT ''")
    if 'link_url_type' not in event_existing:
        conn.execute("ALTER TABLE im_conversion_events ADD COLUMN link_url_type TEXT NOT NULL DEFAULT ''")
    if 'external_app' not in event_existing:
        conn.execute("ALTER TABLE im_conversion_events ADD COLUMN external_app TEXT NOT NULL DEFAULT ''")
    bot_existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(im_bot_timing_facts)").fetchall()}
    bot_columns = {
        'handoff_classification': "ALTER TABLE im_bot_timing_facts ADD COLUMN handoff_classification TEXT NOT NULL DEFAULT ''",
        'auto_apply_sent_at': "ALTER TABLE im_bot_timing_facts ADD COLUMN auto_apply_sent_at TEXT NOT NULL DEFAULT ''",
        'auto_apply_source': "ALTER TABLE im_bot_timing_facts ADD COLUMN auto_apply_source TEXT NOT NULL DEFAULT ''",
        'r1_sent_at': "ALTER TABLE im_bot_timing_facts ADD COLUMN r1_sent_at TEXT NOT NULL DEFAULT ''",
        'r1_source': "ALTER TABLE im_bot_timing_facts ADD COLUMN r1_source TEXT NOT NULL DEFAULT ''",
        'r1_after_auto_apply_seconds': "ALTER TABLE im_bot_timing_facts ADD COLUMN r1_after_auto_apply_seconds REAL",
        'r1_after_auto_apply_within_60': "ALTER TABLE im_bot_timing_facts ADD COLUMN r1_after_auto_apply_within_60 INTEGER NOT NULL DEFAULT 0",
        'r1_step_triggered_at': "ALTER TABLE im_bot_timing_facts ADD COLUMN r1_step_triggered_at TEXT NOT NULL DEFAULT ''",
        'link_sent_at': "ALTER TABLE im_bot_timing_facts ADD COLUMN link_sent_at TEXT NOT NULL DEFAULT ''",
        'link_clicked_at': "ALTER TABLE im_bot_timing_facts ADD COLUMN link_clicked_at TEXT NOT NULL DEFAULT ''",
        'guild_bind_request_at': "ALTER TABLE im_bot_timing_facts ADD COLUMN guild_bind_request_at TEXT NOT NULL DEFAULT ''",
    }
    for column, sql in bot_columns.items():
        if column not in bot_existing:
            conn.execute(sql)
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_im_conversations_ad ON im_conversations(country, media_source, campaign_id, adset_id, ad_id);
        CREATE INDEX IF NOT EXISTS idx_im_messages_conversation ON im_messages(conversation_id, message_index);
        CREATE INDEX IF NOT EXISTS idx_im_events_conversation ON im_conversion_events(conversation_id, event_name);
        CREATE INDEX IF NOT EXISTS idx_im_bot_timing_country ON im_bot_timing_facts(country, handoff_classification);
        CREATE INDEX IF NOT EXISTS idx_im_diagnoses_run ON im_conversation_diagnoses(diagnosis_run_id, primary_diagnosis);
        CREATE INDEX IF NOT EXISTS idx_im_aggregate_lookup ON im_aggregate_diagnoses(diagnosis_run_id, country, media_source, campaign_id, adset_id, ad_id);
        CREATE INDEX IF NOT EXISTS idx_im_llm_tasks_queue ON im_llm_diagnosis_tasks(provider_mode, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_im_llm_tasks_lease ON im_llm_diagnosis_tasks(provider_mode, status, lease_expires_at);
        CREATE INDEX IF NOT EXISTS idx_im_llm_tasks_conversation ON im_llm_diagnosis_tasks(conversation_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_im_script_experiments_suggestion ON im_script_experiments(script_suggestion_id, status);
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_im_events_link_type ON im_conversion_events(link_url_type, link_url_domain)")
    script_existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(im_script_suggestions)").fetchall()}
    script_columns = {
        'funnel_stage': "ALTER TABLE im_script_suggestions ADD COLUMN funnel_stage TEXT NOT NULL DEFAULT ''",
        'target_metric': "ALTER TABLE im_script_suggestions ADD COLUMN target_metric TEXT NOT NULL DEFAULT ''",
        'experiment_hypothesis': "ALTER TABLE im_script_suggestions ADD COLUMN experiment_hypothesis TEXT NOT NULL DEFAULT ''",
        'success_pattern_summary': "ALTER TABLE im_script_suggestions ADD COLUMN success_pattern_summary TEXT NOT NULL DEFAULT ''",
        'experiment_design_json': "ALTER TABLE im_script_suggestions ADD COLUMN experiment_design_json TEXT NOT NULL DEFAULT '{}'",
        'old_script_summary_translation_zh': "ALTER TABLE im_script_suggestions ADD COLUMN old_script_summary_translation_zh TEXT NOT NULL DEFAULT ''",
        'suggested_script_translation_zh': "ALTER TABLE im_script_suggestions ADD COLUMN suggested_script_translation_zh TEXT NOT NULL DEFAULT ''",
        'old_script_summary_translation_source': "ALTER TABLE im_script_suggestions ADD COLUMN old_script_summary_translation_source TEXT NOT NULL DEFAULT ''",
        'old_script_summary_interpretation_zh': "ALTER TABLE im_script_suggestions ADD COLUMN old_script_summary_interpretation_zh TEXT NOT NULL DEFAULT ''",
        'old_script_summary_interpretation_source': "ALTER TABLE im_script_suggestions ADD COLUMN old_script_summary_interpretation_source TEXT NOT NULL DEFAULT ''",
        'suggested_script_source': "ALTER TABLE im_script_suggestions ADD COLUMN suggested_script_source TEXT NOT NULL DEFAULT ''",
        'suggested_script_translation_source': "ALTER TABLE im_script_suggestions ADD COLUMN suggested_script_translation_source TEXT NOT NULL DEFAULT ''",
        'risk_score_json': "ALTER TABLE im_script_suggestions ADD COLUMN risk_score_json TEXT NOT NULL DEFAULT '{}'",
        'max_risk_score': "ALTER TABLE im_script_suggestions ADD COLUMN max_risk_score INTEGER NOT NULL DEFAULT 0",
        'launch_decision': "ALTER TABLE im_script_suggestions ADD COLUMN launch_decision TEXT NOT NULL DEFAULT 'needs_human_review'",
        'current_state': "ALTER TABLE im_script_suggestions ADD COLUMN current_state TEXT NOT NULL DEFAULT ''",
        'user_concern_type': "ALTER TABLE im_script_suggestions ADD COLUMN user_concern_type TEXT NOT NULL DEFAULT ''",
    }
    for column, ddl in script_columns.items():
        if column not in script_existing:
            conn.execute(ddl)
    experiment_existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(im_script_experiments)").fetchall()}
    if 'suggested_script_translation_zh' not in experiment_existing:
        conn.execute("ALTER TABLE im_script_experiments ADD COLUMN suggested_script_translation_zh TEXT NOT NULL DEFAULT ''")
    ensure_im_result_message_tables(conn)


def _truthy(value: Any) -> int:
    raw = str(value or '').strip().lower()
    return 1 if raw in {'1', 'true', 'yes', 'y', 't'} else 0


def _float_or_none(value: Any) -> Optional[float]:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _event_names(events: Sequence[Dict[str, Any]]) -> set[str]:
    return {str(event.get('event_name') or '').strip() for event in events}


def infer_dropoff_stage(events: Sequence[Dict[str, Any]], final_outcome: str = '') -> str:
    names = _event_names(events)
    if str(final_outcome or '').strip() in {'success', 'joined', 'crm_succeeded'} or 'real_join_succeeded' in names or 'crm_succeeded' in names:
        return 'converted'
    if 'bind_result_success' in names:
        return 'after_bind_success_before_join'
    if 'guild_bind_request' in names:
        return 'after_bind_request_before_success'
    if 'link_clicked' in names:
        return 'after_link_click_before_bind'
    for stage in DROP_ORDER:
        if stage not in names:
            return f'before_{stage}'
    return 'after_crm_succeeded'


def _message_flags(sender_type: str, text: str) -> Dict[str, Any]:
    sender = str(sender_type or '').strip().lower()
    has_link = bool(re.search(r'https?://|linky|link', str(text or ''), re.IGNORECASE))
    return {
        'is_auto_message': 1 if sender in {'system', 'bot', 'agent_template'} else 0,
        'is_template_message': 1 if sender in {'agent_template'} else 0,
        'is_human_agent_message': 1 if sender in {'agent', 'agent_manual'} else 0,
        'has_link': 1 if has_link else 0,
    }


def infer_handoff_type_from_counts(
    *,
    reception_status: Any = '',
    assignee_type: Any = '',
    human_messages: Any = 0,
    template_messages: Any = 0,
    auto_messages: Any = 0,
) -> str:
    try:
        human_count = int(human_messages or 0)
    except Exception:
        human_count = 0
    try:
        template_count = int(template_messages or 0)
    except Exception:
        template_count = 0
    try:
        auto_count = int(auto_messages or 0)
    except Exception:
        auto_count = 0
    reception = str(reception_status or '').strip().lower()
    assignee = str(assignee_type or '').strip().lower()
    if human_count > 0 or reception == 'human_serving' or assignee == 'admin':
        return 'human_assisted'
    if template_count > 0:
        return 'template_assisted'
    if auto_count > 0 or reception == 'bot_serving' or assignee == 'system_bot':
        return 'bot_automated'
    if reception == 'unassigned' or assignee in {'none', ''}:
        return 'unassigned'
    return 'unknown'


def persist_im_diagnostics_payload(
    conn: sqlite3.Connection,
    *,
    conversations: Sequence[Dict[str, Any]],
    messages: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
    replace_existing: bool = True,
) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    now = _utc_now()
    conversation_ids = {str(row.get('conversation_id') or '').strip() for row in conversations if row.get('conversation_id')}
    if replace_existing and conversation_ids:
        ordered_ids = tuple(conversation_ids)
        for table in ('im_conversation_diagnoses', 'im_messages', 'im_conversion_events', 'im_bot_timing_facts', 'im_conversations'):
            for start in range(0, len(ordered_ids), 500):
                params = ordered_ids[start:start + 500]
                placeholders = ','.join('?' for _ in params)
                conn.execute(f'DELETE FROM {table} WHERE conversation_id IN ({placeholders})', params)

    pii_blocked = 0
    for row in conversations:
        conversation_id = str(row.get('conversation_id') or '').strip()
        if not conversation_id:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO im_conversations (
                conversation_id, anonymous_user_id, country, language, media_source, external_app, campaign_id, campaign_name,
                adset_id, adset_name, ad_id, ad_name, creative_id, ad_account_id, entered_im_at,
                conversation_start_time, conversation_end_time, first_user_message_at, first_agent_reply_at,
                first_response_seconds, final_join_status, final_outcome, dropoff_stage, dropoff_time,
                agent_id_hash, agent_team, agent_shift, handoff_type, data_quality_status, pii_scan_status,
                attribution_quality_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                str(row.get('anonymous_user_id') or ''),
                str(row.get('country') or ''),
                str(row.get('language') or ''),
                str(row.get('media_source') or ''),
                str(row.get('external_app') or ''),
                str(row.get('campaign_id') or ''),
                str(row.get('campaign_name') or ''),
                str(row.get('adset_id') or ''),
                str(row.get('adset_name') or ''),
                str(row.get('ad_id') or ''),
                str(row.get('ad_name') or ''),
                str(row.get('creative_id') or ''),
                str(row.get('ad_account_id') or ''),
                str(row.get('entered_im_at') or ''),
                str(row.get('conversation_start_time') or ''),
                str(row.get('conversation_end_time') or ''),
                str(row.get('first_user_message_at') or ''),
                str(row.get('first_agent_reply_at') or ''),
                float(row.get('first_response_seconds') or 0.0),
                str(row.get('final_join_status') or ''),
                str(row.get('final_outcome') or ''),
                str(row.get('dropoff_stage') or ''),
                str(row.get('dropoff_time') or ''),
                str(row.get('agent_id_hash') or ''),
                str(row.get('agent_team') or ''),
                str(row.get('agent_shift') or ''),
                str(row.get('handoff_type') or ''),
                str(row.get('data_quality_status') or 'mock'),
                str(row.get('pii_scan_status') or 'passed'),
                str(row.get('attribution_quality_status') or 'mock'),
                str(row.get('created_at') or now),
                now,
            ),
        )

    for index, row in enumerate(messages):
        conversation_id = str(row.get('conversation_id') or '').strip()
        if not conversation_id:
            continue
        raw_text = str(row.get('message_text_redacted') or row.get('message_text') or '')
        pii = scan_pii(raw_text)
        if pii['status'] == 'blocked':
            pii_blocked += 1
        sender_type = str(row.get('sender_type') or '').strip()
        flags = _message_flags(sender_type, pii['redacted_text'])
        message_id = str(row.get('message_id') or '').strip() or _stable_id(conversation_id, row.get('message_index', index), pii['redacted_text'], prefix='im_msg_')
        conn.execute(
            """
            INSERT OR REPLACE INTO im_messages (
                message_id, conversation_id, message_index, sender_type, message_type, message_at,
                message_text_redacted, language, template_id, template_name, is_auto_message,
                is_template_message, is_human_agent_message, has_link, link_id_hash,
                pii_scan_status, risk_tags_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                conversation_id,
                int(row.get('message_index') or index),
                sender_type,
                str(row.get('message_type') or 'text'),
                str(row.get('message_at') or ''),
                pii['redacted_text'],
                str(row.get('language') or ''),
                str(row.get('template_id') or ''),
                str(row.get('template_name') or ''),
                flags['is_auto_message'],
                flags['is_template_message'],
                flags['is_human_agent_message'],
                flags['has_link'],
                str(row.get('link_id_hash') or ''),
                pii['status'],
                _json(pii['tags']),
                str(row.get('created_at') or now),
            ),
        )

    for row in events:
        conversation_id = str(row.get('conversation_id') or '').strip()
        event_name = str(row.get('event_name') or '').strip()
        if not conversation_id or not event_name:
            continue
        event_id = str(row.get('event_id') or '').strip() or _stable_id(conversation_id, event_name, row.get('event_time'), prefix='im_evt_')
        error_scan = scan_pii(str(row.get('error_message_redacted') or row.get('error_message') or ''))
        conn.execute(
            """
            INSERT OR REPLACE INTO im_conversion_events (
                event_id, conversation_id, anonymous_user_id, event_name, event_time, event_status,
                event_source, external_app, campaign_id, adset_id, ad_id, creative_id, link_id_hash,
                link_url_domain, link_url_type, error_code, error_message_redacted, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                conversation_id,
                str(row.get('anonymous_user_id') or ''),
                event_name,
                str(row.get('event_time') or ''),
                str(row.get('event_status') or ''),
                str(row.get('event_source') or ''),
                str(row.get('external_app') or ''),
                str(row.get('campaign_id') or ''),
                str(row.get('adset_id') or ''),
                str(row.get('ad_id') or ''),
                str(row.get('creative_id') or ''),
                str(row.get('link_id_hash') or ''),
                str(row.get('link_url_domain') or ''),
                str(row.get('link_url_type') or ''),
                str(row.get('error_code') or ''),
                error_scan['redacted_text'],
                str(row.get('created_at') or now),
            ),
        )
    conn.commit()
    return {
        'ok': True,
        'conversations': len(conversation_ids),
        'messages': len(messages),
        'events': len(events),
        'pii_blocked_messages': pii_blocked,
    }


def persist_im_bot_timing_facts(
    conn: sqlite3.Connection,
    *,
    facts: Sequence[Dict[str, Any]],
    replace_existing: bool = True,
) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    now = _utc_now()
    conversation_ids = tuple(str(row.get('conversation_id') or '').strip() for row in facts if row.get('conversation_id'))
    if replace_existing and conversation_ids:
        for start in range(0, len(conversation_ids), 500):
            params = conversation_ids[start:start + 500]
            placeholders = ','.join('?' for _ in params)
            conn.execute(f'DELETE FROM im_bot_timing_facts WHERE conversation_id IN ({placeholders})', params)
    written = 0
    for row in facts:
        conversation_id = str(row.get('conversation_id') or '').strip()
        if not conversation_id:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO im_bot_timing_facts (
                conversation_id, anonymous_user_id, country, is_high_value, entered_im_at, reception_status,
                current_assignee_type, guild_join_status, handoff_classification, auto_apply_sent_at,
                auto_apply_source, r1_sent_at, r1_source, r1_after_auto_apply_seconds,
                r1_after_auto_apply_within_60, r1_step_triggered_at, link_sent_at, link_clicked_at,
                guild_bind_request_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                str(row.get('anonymous_user_id') or ''),
                str(row.get('country') or ''),
                _truthy(row.get('is_high_value')),
                str(row.get('entered_im_at') or ''),
                str(row.get('reception_status') or ''),
                str(row.get('current_assignee_type') or ''),
                str(row.get('guild_join_status') or ''),
                str(row.get('handoff_classification') or ''),
                str(row.get('auto_apply_sent_at') or ''),
                str(row.get('auto_apply_source') or ''),
                str(row.get('r1_sent_at') or ''),
                str(row.get('r1_source') or ''),
                _float_or_none(row.get('r1_after_auto_apply_seconds')),
                _truthy(row.get('r1_after_auto_apply_within_60')),
                str(row.get('r1_step_triggered_at') or ''),
                str(row.get('link_sent_at') or ''),
                str(row.get('link_clicked_at') or ''),
                str(row.get('guild_bind_request_at') or ''),
                now,
                now,
            ),
        )
        written += 1
    conn.commit()
    return {'ok': True, 'bot_timing_facts': written}


def _script_language(country: Any, language: Any) -> str:
    country_raw = str(country or '').strip().lower()
    language_raw = str(language or '').strip().lower()
    if country_raw in {'brazil', 'br', 'bra'} or language_raw in {'pt', 'pt-br', 'portuguese', 'portugues', 'português'}:
        return 'pt-BR'
    if country_raw in {'indonesia', 'id', 'idn'} or language_raw in {'id', 'id-id', 'bahasa', 'indonesian'}:
        return 'id-ID'
    if country_raw in {
        'mexico', 'venezuela', 'colombia', 'chile', 'peru', 'ecuador', 'argentina', 'bolivia',
        'paraguay', 'uruguay', 'mex', 've', 'co', 'cl', 'pe', 'ec', 'ar', 'bo', 'py', 'uy',
    } or language_raw in {'es', 'es-es', 'es-mx', 'es-co', 'es-cl', 'spanish', 'local'}:
        return 'es'
    return str(language or '').strip() or 'local'


def _canonical_country(country: Any) -> str:
    raw = str(country or '').strip()
    lowered = raw.lower()
    if lowered in {'br', 'bra', 'brazil'}:
        return 'Brazil'
    if lowered in {'id', 'idn', 'indonesia'}:
        return 'Indonesia'
    return raw


def _contains_cjk(text: Any) -> bool:
    return bool(re.search(r'[\u4e00-\u9fff]', str(text or '')))


def _localized_script_suggestion(primary: str, country: Any, language: Any, fallback: str = '') -> str:
    lang = _script_language(country, language)
    pt_br = {
        'linky_trust_explanation_missing': 'O Linky é um app social de conversa que eu recomendo como parceira oficial. O cadastro é gratuito; você pode conversar, receber presentes e acumular diamantes conforme as regras do app. Se quiser, eu te explico com segurança antes de começar 😊',
        'scam_like_script': 'Antes de se cadastrar, deixa eu explicar com calma: você não precisa pagar nada para começar. Somos agência parceira oficial da Linky; no app, os diamantes vêm de conversas e presentes, e o saque segue as regras da plataforma.',
        'linky_registration_guidance_failed': 'Depois de se cadastrar no Linky, abra a página “Minha conta” e copie seu Linky ID. Com esse ID eu consigo habilitar seu acesso de atendimento e ganhos pela nossa agência parceira oficial.',
        'bind_guidance_failed': 'Me envie seu Linky ID da página “Minha conta”. Eu uso esse ID para confirmar seu vínculo com a agência oficial e liberar seu perfil para receber mensagens e seguir para a etapa de ganhos.',
        'silent_user_not_reactivated': 'Você quer entender primeiro como funciona ganhar diamantes conversando pelo celular, ou prefere que eu já te mostre o cadastro gratuito no Linky?',
        'auto_message_handoff_failed': 'Vi sua solicitação. Você quer conhecer uma oportunidade gratuita para conversar pelo celular e acumular diamantes no Linky? Eu posso te explicar o processo com segurança.',
    }
    id_id = {
        'linky_trust_explanation_missing': 'Linky adalah aplikasi sosial untuk chat yang saya rekomendasikan sebagai agensi partner resmi. Daftarnya gratis; kamu bisa chat, menerima gift, dan mengumpulkan diamond sesuai aturan aplikasi. Saya bisa jelaskan dulu dengan aman 😊',
        'scam_like_script': 'Sebelum daftar, saya jelaskan dulu ya: kamu tidak perlu membayar untuk mulai. Kami adalah agensi partner resmi Linky; diamond didapat dari chat dan gift, lalu penarikan mengikuti aturan platform.',
        'linky_registration_guidance_failed': 'Setelah daftar di Linky, buka halaman “Saya/Profil” dan salin Linky ID kamu. Dengan ID itu saya bisa bantu aktifkan akses penerimaan chat dan penghasilan lewat agensi partner resmi.',
        'bind_guidance_failed': 'Kirim Linky ID kamu dari halaman “Saya/Profil”. Saya pakai ID itu untuk mengonfirmasi akun kamu di agensi resmi, supaya profil kamu bisa menerima chat dan lanjut ke langkah penghasilan.',
        'silent_user_not_reactivated': 'Kamu mau pahami dulu cara mengumpulkan diamond lewat chat dari HP, atau mau saya tunjukkan pendaftaran gratis Linky dulu?',
        'auto_message_handoff_failed': 'Saya sudah melihat permintaan kamu. Kamu mau tahu peluang gratis untuk chat lewat HP dan mengumpulkan diamond di Linky? Saya bisa jelaskan prosesnya dengan aman.',
    }
    es = {
        'linky_trust_explanation_missing': 'Linky es una app social de chat que te recomiendo como agencia partner oficial. El registro es gratis; puedes conversar, recibir regalos y acumular diamantes según las reglas de la app. Si quieres, te explico con seguridad antes de empezar 😊',
        'scam_like_script': 'Antes de registrarte, te explico con calma: no necesitas pagar para empezar. Somos agencia partner oficial de Linky; los diamantes vienen de chats y regalos, y el retiro sigue las reglas de la plataforma.',
        'linky_registration_guidance_failed': 'Después de registrarte en Linky, abre la página “Mi perfil” y copia tu Linky ID. Con ese ID puedo ayudarte a habilitar tu acceso de atención y ganancias desde nuestra agencia partner oficial.',
        'bind_guidance_failed': 'Envíame tu Linky ID desde “Mi perfil”. Uso ese ID para confirmar tu vínculo con la agencia oficial y habilitar tu perfil para recibir mensajes y seguir con la etapa de ganancias.',
        'silent_user_not_reactivated': '¿Quieres entender primero cómo ganar diamantes conversando desde el celular, o prefieres que te muestre el registro gratuito de Linky?',
        'auto_message_handoff_failed': 'Ya vi tu solicitud. ¿Quieres conocer una oportunidad gratuita para conversar desde el celular y acumular diamantes en Linky? Te explico el proceso con seguridad.',
        'ad_promise_mismatch': 'Primero te explico la forma real: es una actividad de chat social desde el celular. No es dinero garantizado; los diamantes dependen de chats, regalos y las reglas de la app.',
        'cs_first_response_slow': 'Perdón por la espera. Ya estoy aquí para ayudarte. Linky es una app gratuita de chat social; puedo explicarte seguridad, diamantes y cómo activar tu acceso paso a paso.',
    }
    if lang == 'pt-BR':
        return pt_br.get(primary, fallback)
    if lang == 'id-ID':
        return id_id.get(primary, fallback)
    if lang == 'es':
        return es.get(primary, fallback)
    return fallback


def _localized_non_cjk_script(primary: str, country: Any, language: Any, suggested: Any) -> str:
    raw = scan_pii(str(suggested or '').strip())['redacted_text'][:1200]
    if raw and not _contains_cjk(raw):
        return raw
    fallback = _localized_script_suggestion(primary, country, language)
    if fallback:
        return fallback
    return raw if raw and not _contains_cjk(raw) else ''


INVALID_LINKY_POSITIONING_RE = re.compile(
    r'(?:\b(?:o\s+)?linky\s+(?:é|e|eh|is)\s+(?:a\s+|uma\s+|um\s+)?(?:página|page|formul[aá]rio|form)\b)'
    r'|(?:linky[^。；;.!?]{0,28}(?:注册页面|注册页|资料确认页|资料确认页面|确认注册页|确认注册页面|表单))'
    r'|(?:(?:用来|用于|作为)[^。；;.!?]{0,18}确认(?:你的)?注册[^。；;.!?]{0,18}(?:页面|页))',
    re.IGNORECASE,
)


def _has_invalid_linky_positioning(*values: Any) -> bool:
    text = ' '.join(str(value or '') for value in values)
    return bool(INVALID_LINKY_POSITIONING_RE.search(text))


def _script_translation_zh(primary: str, fallback: Any = '', *, country: Any = '', language: Any = '') -> str:
    raw = str(fallback or '').strip()
    if raw:
        return scan_pii(raw)['redacted_text'][:1200]
    lang = _script_language(country, language)
    pt_br = {
        'linky_trust_explanation_missing': 'Linky 是我们作为官方合作机构推荐你了解的社交聊天 App。注册免费，用户可通过聊天、回复消息和礼物按平台规则获得钻石。',
        'scam_like_script': '注册前需要解释清楚：开始不收费，我们是 Linky 官方合作机构；钻石来自聊天互动和礼物，提现按平台规则。',
        'linky_registration_guidance_failed': '用户注册 Linky 后，到“我的/个人页”找到 Linky ID；客服用 ID 帮她开通接待/收益权限并绑定到官方合作机构。',
        'bind_guidance_failed': '用户需要提供 Linky ID，用于确认官方合作机构绑定和接待权限；绑定后账号才能获得更多接待和收益流程指导。',
        'silent_user_not_reactivated': '用户想先了解手机聊天赚钻石的逻辑，还是先免费注册 Linky；用低压力问题推进。',
        'auto_message_handoff_failed': '用户发起申请后，应先解释这是免费了解 Linky 社交聊天机会，并说明会继续安全指导。',
        'ad_promise_mismatch': '需要把外层网赚/积分兴趣和 Linky 社交聊天 App 体验衔接清楚，不能让用户以为只是领现金、填表或保证收益。',
        'cs_first_response_slow': '首响慢时先安抚，再解释 Linky 是免费社交聊天 App、官方合作机构、安全边界、钻石来源和 Linky ID 用途。',
    }
    id_id = {
        'linky_trust_explanation_missing': 'Linky 是客服推荐给用户尝试的社交聊天 App。注册免费，用户可在里面聊天、收礼物并按平台规则获得钻石。',
        'scam_like_script': '注册前需要解释清楚：开始不收费，Linky 是社交聊天 App，钻石来自聊天互动和礼物，提现按平台规则。',
        'linky_registration_guidance_failed': '引导用户免费注册 Linky 后进入聊天区域；完成后回到 IM，由客服继续说明下一步和安全事项。',
        'bind_guidance_failed': '如果页面显示错误，请发送错误信息或不含敏感资料的截图。我会帮你检查哪一步需要修正。',
        'silent_user_not_reactivated': '你想先了解流程，还是想直接开始注册？我可以简短地帮你完成下一步。',
        'auto_message_handoff_failed': '我已经看到你的申请，会帮你确认下一步。你想先了解流程，还是直接继续注册？',
        'ad_promise_mismatch': '需要把外层网赚/积分兴趣和 Linky 社交聊天 App 体验衔接清楚，不能让用户以为只是领现金或填表。',
        'cs_first_response_slow': '首响慢时先安抚，再解释 Linky 是免费社交聊天 App，用户可通过聊天和礼物按规则累积钻石。',
    }
    translations = {
        'pt-BR': pt_br,
        'id-ID': id_id,
    }
    return translations.get(lang, pt_br).get(str(primary or ''), '先确认用户目标，再用简单、清楚的方式说明流程和下一步。')


def _machine_translate_script_zh(text: Any, *, country: Any = '', language: Any = '') -> str:
    source = scan_pii(str(text or '').strip())['redacted_text']
    if not source:
        return ''
    lang = _script_language(country, language)
    source_lang = {
        'pt-BR': 'pt',
        'id-ID': 'id',
        'es': 'es',
    }.get(lang, 'auto')
    query = urllib.parse.urlencode({
        'client': 'gtx',
        'sl': source_lang,
        'tl': 'zh-CN',
        'dt': 't',
        'q': source,
    })
    request = urllib.request.Request(
        f'https://translate.googleapis.com/translate_a/single?{query}',
        headers={'User-Agent': 'mcn-ai-automation/translation-backfill'},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except Exception:
        return ''
    translated = ''.join(str(part[0] or '') for part in (payload[0] or []) if isinstance(part, list) and part)
    return scan_pii(translated.strip())['redacted_text'][:1200]


def _required_script_translation_zh(
    primary: str,
    suggested_message: Any,
    fallback: Any = '',
    *,
    country: Any = '',
    language: Any = '',
) -> str:
    explicit = _script_translation_zh(primary, fallback, country=country, language=language) if str(fallback or '').strip() else ''
    if explicit:
        return explicit
    suggested = str(suggested_message or '').strip()
    if suggested and suggested == _localized_script_suggestion(primary, country, language):
        return _script_translation_zh(primary, '', country=country, language=language)
    translated = _machine_translate_script_zh(suggested, country=country, language=language)
    if translated:
        return translated
    return _script_translation_zh(primary, '', country=country, language=language)


def _remove_linky_form_negation(text: Any) -> str:
    value = str(text or '').strip()
    if not value:
        return ''
    replacements = [
        (r'\b[oO]\s+Linky\s+não\s+é\s+(?:um\s+)?formul[aá]rio\s*,\s*(?:mas\s+)?(?:é\s+)?', 'O Linky é '),
        (r'\b[oO]\s+Linky\s+não\s+é\s+(?:uma\s+)?p[aá]gina\s+de\s+(?:dados|confirma[cç][aã]o)\s*,\s*(?:mas\s+)?(?:é\s+)?', 'O Linky é '),
        (r'\b[nN]ão\s+é\s+(?:um\s+)?formul[aá]rio\s*,\s*(?:mas\s+)?(?:é\s+)?', 'É '),
        (r'\b[nN]ão\s+é\s+(?:uma\s+)?p[aá]gina\s+de\s+(?:dados|confirma[cç][aã]o)\s*,\s*(?:mas\s+)?(?:é\s+)?', 'É '),
        (r'Linky\s+不是表单[，,]\s*而是(?:一个)?', 'Linky 是一个'),
        (r'Linky\s+不是资料(?:确认)?页[，,]\s*而是(?:一个)?', 'Linky 是一个'),
        (r'不是表单[，,]\s*而是(?:一个)?', '是一个'),
        (r'不是资料(?:确认)?页[，,]\s*而是(?:一个)?', '是一个'),
    ]
    for pattern, repl in replacements:
        value = re.sub(pattern, repl, value)
    return re.sub(r'\s{2,}', ' ', value).strip()


def _conversation_rows(conn: sqlite3.Connection, conversation_ids: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    ensure_im_diagnostics_tables(conn)
    if conversation_ids:
        rows = []
        ordered_ids = tuple(conversation_ids)
        for start in range(0, len(ordered_ids), 500):
            params = ordered_ids[start:start + 500]
            placeholders = ','.join('?' for _ in params)
            rows.extend(
                conn.execute(
                    f'SELECT * FROM im_conversations WHERE conversation_id IN ({placeholders})',
                    params,
                ).fetchall()
            )
    else:
        rows = conn.execute('SELECT * FROM im_conversations ORDER BY entered_im_at DESC, conversation_id').fetchall()
    return [dict(row) for row in rows]


def _messages_for(conn: sqlite3.Connection, conversation_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT * FROM im_messages WHERE conversation_id = ? ORDER BY message_index, message_at',
        (conversation_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _events_for(conn: sqlite3.Connection, conversation_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT * FROM im_conversion_events WHERE conversation_id = ? ORDER BY event_time, event_name',
        (conversation_id,),
    ).fetchall()
    return [dict(row) for row in rows]


class ConversationDiagnosisEvaluator:
    provider = 'fixture'
    model_name = 'rule_based_fixture'

    def evaluate(self, conversation: Dict[str, Any], messages: Sequence[Dict[str, Any]], events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        names = _event_names(events)
        final_status = str(conversation.get('final_join_status') or '').strip().lower()
        final_outcome = str(conversation.get('final_outcome') or '').strip() or ('success' if final_status in {'joined', 'success', 'succeed'} or any(e.get('event_name') in {'crm_succeeded', 'real_join_succeeded'} for e in events) else 'lost')
        dropoff_stage = str(conversation.get('dropoff_stage') or '').strip() or infer_dropoff_stage(events, final_outcome)
        response_seconds = float(conversation.get('first_response_seconds') or 0.0)
        joined = final_outcome in {'success', 'joined', 'crm_succeeded'} or dropoff_stage == 'converted'
        handoff_type = str(conversation.get('handoff_type') or '').strip() or 'unknown'
        text = '\n'.join(str(m.get('message_text_redacted') or '') for m in messages).lower()
        user_text = '\n'.join(str(m.get('message_text_redacted') or '') for m in messages if str(m.get('sender_type') or '').startswith('user')).lower()
        agent_text = '\n'.join(str(m.get('message_text_redacted') or '') for m in messages if 'agent' in str(m.get('sender_type') or '').lower())
        user_message_count = len([m for m in messages if str(m.get('sender_type') or '').startswith('user')])
        human_agent_count = len([m for m in messages if int(m.get('is_human_agent_message') or 0) > 0])

        primary = 'data_insufficient'
        secondary: List[str] = []
        user_objection = ''
        agent_issue = ''
        action_type = 'observe'
        suggested = ''
        critical_turn = 0

        if joined:
            primary = 'success_sample'
            action_type = 'script_library_candidate'
            suggested = '保留当前承接节奏，提炼为同国家成功样本。'
        elif response_seconds <= 0 and human_agent_count <= 0 and handoff_type in {'unassigned', 'unknown'}:
            primary = 'auto_message_handoff_failed'
            agent_issue = '用户进入 IM 后没有有效人工承接，需检查分配或转人工规则。'
            action_type = 'im_handoff_fix'
            suggested = '当用户进入后无人响应超过阈值，应自动补一句低压确认并转人工：我已经看到你的申请，正在帮你确认下一步。'
        elif response_seconds > 60:
            primary = 'cs_first_response_slow'
            agent_issue = '客服首响超过 60 秒，用户可能在首响前流失。'
            action_type = 'im_response_sla_improvement'
        elif 'linky' in user_text and any(token in user_text for token in ('why', 'por que', 'porque', 'kenapa', 'safe', 'seguro', 'scam', 'golpe', 'tipu')):
            primary = 'linky_trust_explanation_missing'
            user_objection = '用户质疑 Linky 的用途或安全性。'
            agent_issue = '客服没有先解释 Linky 是免费社交聊天 App、官方合作关系、是否安全、钻石来源、平台提现规则和 Linky ID 用途，再推动下一步。'
            action_type = 'im_script_improvement'
            suggested = 'Linky 是我们官方合作机构推荐你了解的免费社交聊天 App。你可以通过聊天和礼物按平台规则获得钻石；注册后把 Linky ID 发我，我帮你开通接待和收益权限。'
        elif any(token in text for token in ('scam', 'golpe', 'fraude', 'tipu', 'fake')):
            primary = 'scam_like_script'
            user_objection = '用户表达不信任或担心被骗。'
            agent_issue = '承接话术缺少可信解释和风险消除。'
            action_type = 'im_script_improvement'
            suggested = '先解释流程、费用和资料用途，避免直接催促注册。'
        elif 'link_clicked' in names and 'guild_bind_request' not in names:
            primary = 'linky_registration_guidance_failed'
            agent_issue = '用户已点击 Linky 但没有进入 bind，请检查点击后是否说明这是社交聊天 App、免费安全、钻石、提现规则、Linky ID 位置和官方合作机构绑定用途。'
            action_type = 'im_handoff_fix' if handoff_type == 'bot_automated' else 'im_script_improvement'
            suggested = '你点开 Linky 后先免费注册，进入“我的页面”找到 Linky ID 发给我。我会帮你绑定到官方合作机构，开通接待和收益相关权限。'
        elif 'guild_bind_request' in names and 'bind_result_success' not in names:
            primary = 'bind_guidance_failed'
            agent_issue = '用户已进入 bind 请求，但 bind 未成功，需要检查资料填写、页面错误或客服辅助。'
            action_type = 'process_fix'
            suggested = '如果页面提示失败，请把失败提示发给我；我帮你确认需要修改哪一步。'
        elif 'bind_result_success' in names and final_status != 'joined':
            primary = 'crm_process_issue'
            agent_issue = 'bind 成功后未真实入会，可能是 CRM/审核/回写链路问题。'
            action_type = 'process_fix'
        elif user_message_count <= 1:
            primary = 'silent_user_not_reactivated'
            agent_issue = '用户进入 IM 后互动不足，客服没有有效唤醒。'
            action_type = 'im_handoff_fix' if handoff_type == 'bot_automated' else 'im_script_improvement'
            suggested = '用一个低压力问题确认用户目标，例如：你想先了解流程，还是直接开始注册？'
        elif any(token in text for token in ('money', 'income', 'salário', 'renda', 'gaji', 'earn')):
            primary = 'ad_promise_mismatch'
            user_objection = '用户关注收益，但后续互动不足。'
            agent_issue = '可能需要素材和客服话术同时降低收益刺激、解释真实工作方式。'
            action_type = 'review_ad_promise'
        else:
            primary = 'unclear_steps'
            agent_issue = '对话未清楚推进到下一步，需人工复核关键掉线点。'
            action_type = 'human_review'

        if primary != 'cs_first_response_slow' and response_seconds > 45:
            secondary.append('cs_first_response_slow')
        if 'link_sent' in names and 'link_clicked' not in names and primary != 'linky_trust_explanation_missing':
            secondary.append('linky_trust_explanation_missing')

        suggested = _localized_script_suggestion(
            primary,
            conversation.get('country'),
            conversation.get('language'),
            suggested,
        )
        experiment_plan = _script_experiment_plan(
            diagnosis_type=primary,
            dropoff_stage=dropoff_stage,
            country=conversation.get('country'),
            language=conversation.get('language'),
        )
        evidence = _evidence_for(primary, messages, response_seconds)
        return {
            'conversation_id': conversation.get('conversation_id'),
            'final_outcome': final_outcome,
            'dropoff_stage': dropoff_stage,
            'primary_diagnosis': primary,
            'primary_diagnosis_zh': DIAGNOSIS_LABELS.get(primary, primary),
            'secondary_diagnoses': secondary,
            'user_intent': _infer_user_intent(user_text, events),
            'user_objection': user_objection,
            'agent_issue': agent_issue or DIAGNOSIS_LABELS.get(primary, primary),
            'critical_turn_index': critical_turn,
            'evidence': evidence,
            'recommended_replacement': {
                'language': conversation.get('language') or '',
                'scenario': DIAGNOSIS_LABELS.get(primary, primary),
                'suggested_message': suggested,
                'suggested_message_translation_zh': _script_translation_zh(primary),
                'funnel_stage': experiment_plan['funnel_stage'],
                'funnel_stage_label': experiment_plan['funnel_stage_label'],
                'target_metric': experiment_plan['target_metric'],
                'experiment_hypothesis': experiment_plan['experiment_hypothesis'],
                'experiment_design': experiment_plan['experiment_design'],
            },
            'action_type': action_type,
            'confidence': 'high' if primary in {'success_sample', 'cs_first_response_slow', 'crm_process_issue'} else 'medium',
            'needs_human_review': primary != 'success_sample',
        }


def _infer_user_intent(user_text: str, events: Sequence[Dict[str, Any]]) -> str:
    names = _event_names(events)
    if 'link_clicked' in names or 'linky_registered' in names:
        return '用户已推进到 Linky 前后步骤'
    if any(token in user_text for token in ('income', 'money', 'renda', 'gaji', 'earn')):
        return '用户关注收益和工作方式'
    if any(token in user_text for token in ('why', 'por que', 'porque', 'kenapa')):
        return '用户想理解流程原因'
    return '用户意图不明确'


def _evidence_for(primary: str, messages: Sequence[Dict[str, Any]], response_seconds: float) -> List[Dict[str, Any]]:
    if primary == 'cs_first_response_slow':
        return [{'turn_index': 0, 'speaker': 'system', 'summary': f'客服首响 {round(response_seconds)} 秒，超过 60 秒阈值。'}]
    evidence: List[Dict[str, Any]] = []
    for message in messages[:12]:
        text = str(message.get('message_text_redacted') or '').strip()
        if not text:
            continue
        evidence.append({
            'turn_index': int(message.get('message_index') or 0),
            'speaker': str(message.get('sender_type') or ''),
            'summary': text[:120],
        })
        if len(evidence) >= 3:
            break
    return evidence


def run_im_diagnosis(
    conn: sqlite3.Connection,
    *,
    conversation_ids: Optional[Sequence[str]] = None,
    diagnosis_run_id: Optional[str] = None,
    commit_every: int = 0,
) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    conn.row_factory = sqlite3.Row
    evaluator = ConversationDiagnosisEvaluator()
    run_id = diagnosis_run_id or f'im_diag_run_{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")}_{uuid.uuid4().hex[:6]}'
    now = _utc_now()
    conversations = _conversation_rows(conn, conversation_ids)
    prepared_diagnoses: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    for conversation in conversations:
        cid = conversation['conversation_id']
        messages = _messages_for(conn, cid)
        if any(str(m.get('pii_scan_status') or '') == 'blocked' for m in messages):
            continue
        events = _events_for(conn, cid)
        result = evaluator.evaluate(conversation, messages, events)
        diagnosis_id = _stable_id(run_id, cid, result['primary_diagnosis'], prefix='im_diag_')
        prepared_diagnoses.append((conversation, result, diagnosis_id))

    # Diagnosis evaluation and evidence collection are CPU/read-heavy.  Finish
    # them before the first DML statement so callers using a cross-process short
    # write window do not monopolize the shared SQLite writer lock.
    written = 0
    script_suggestions = 0
    for conversation, result, diagnosis_id in prepared_diagnoses:
        cid = conversation['conversation_id']
        conn.execute(
            """
            INSERT OR REPLACE INTO im_conversation_diagnoses (
                diagnosis_id, conversation_id, diagnosis_run_id, model_provider, model_name, prompt_version,
                taxonomy_version, final_outcome, dropoff_stage, primary_diagnosis, secondary_diagnoses_json,
                user_intent, user_objection, agent_issue, critical_turn_index, evidence_json,
                recommended_replacement_json, action_type, confidence, needs_human_review,
                human_review_status, human_review_comment, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                diagnosis_id,
                cid,
                run_id,
                evaluator.provider,
                evaluator.model_name,
                PROMPT_VERSION,
                TAXONOMY_VERSION,
                result['final_outcome'],
                result['dropoff_stage'],
                result['primary_diagnosis'],
                _json(result['secondary_diagnoses']),
                result['user_intent'],
                result['user_objection'],
                result['agent_issue'],
                int(result['critical_turn_index'] or 0),
                _json(result['evidence']),
                _json(result['recommended_replacement']),
                result['action_type'],
                result['confidence'],
                1 if result['needs_human_review'] else 0,
                'pending',
                '',
                now,
                now,
            ),
        )
        written += 1
        replacement = result.get('recommended_replacement') or {}
        if result['action_type'] == 'im_script_improvement' and replacement.get('suggested_message'):
            _upsert_script_suggestion(
                conn,
                diagnosis_type=result['primary_diagnosis'],
                country=conversation.get('country'),
                language=conversation.get('language'),
                scenario=replacement.get('scenario') or '',
                agent_issue=result.get('agent_issue') or '',
                suggested_message=replacement.get('suggested_message') or '',
                suggested_message_translation_zh=replacement.get('suggested_message_translation_zh') or '',
                evidence=result.get('evidence') or [],
                conversation_id=cid,
                dropoff_stage=result.get('dropoff_stage') or '',
                source='rule',
                now=now,
            )
            script_suggestions += 1
        if commit_every > 0 and written % commit_every == 0:
            conn.commit()
    aggregate_im_diagnoses(conn, diagnosis_run_id=run_id)
    conn.commit()
    return {'ok': True, 'diagnosis_run_id': run_id, 'diagnosed_conversations': written, 'script_suggestions': script_suggestions}


def aggregate_im_diagnoses(conn: sqlite3.Connection, *, diagnosis_run_id: str) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT c.*, d.primary_diagnosis, d.dropoff_stage, d.final_outcome, d.action_type
        FROM im_conversations c
        JOIN im_conversation_diagnoses d ON d.conversation_id = c.conversation_id
        WHERE d.diagnosis_run_id = ?
        """,
        (diagnosis_run_id,),
    ).fetchall()
    grouped: Dict[Tuple[str, str, str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = dict(row)
        date = str(item.get('entered_im_at') or '')[:10]
        key = (
            date,
            str(item.get('country') or ''),
            str(item.get('media_source') or ''),
            str(item.get('campaign_id') or item.get('campaign_name') or ''),
            str(item.get('adset_id') or item.get('adset_name') or ''),
            str(item.get('ad_id') or item.get('ad_name') or ''),
        )
        grouped[key].append(item)
    now = _utc_now()
    conn.execute('DELETE FROM im_aggregate_diagnoses WHERE diagnosis_run_id = ?', (diagnosis_run_id,))
    count = 0
    for key, items in grouped.items():
        date, country, media_source, campaign_id, adset_id, ad_id = key
        sample = len(items)
        success = sum(1 for item in items if item.get('final_outcome') in {'success', 'joined', 'crm_succeeded'})
        lost = sample - success
        failure_counts = Counter(item.get('primary_diagnosis') for item in items if item.get('primary_diagnosis') != 'success_sample')
        dropoff_counts = Counter(item.get('dropoff_stage') for item in items)
        response_values = [float(item.get('first_response_seconds') or 0.0) for item in items]
        top_reasons = [
            {
                'diagnosis': diagnosis,
                'diagnosis_zh': DIAGNOSIS_LABELS.get(str(diagnosis), str(diagnosis)),
                'affected_conversations': affected,
                'share': round(affected / sample, 4) if sample else 0.0,
            }
            for diagnosis, affected in failure_counts.most_common(5)
        ]
        actions = _recommended_actions(top_reasons)
        aggregate_id = _stable_id(diagnosis_run_id, *key, prefix='im_agg_')
        conn.execute(
            """
            INSERT OR REPLACE INTO im_aggregate_diagnoses (
                aggregate_id, diagnosis_run_id, date, country, media_source, campaign_id, adset_id, ad_id,
                creative_id, sample_conversations, successful_conversations, lost_conversations,
                top_failure_reasons_json, response_time_summary_json, dropoff_summary_json,
                recommended_actions_json, linked_ad_diagnosis_type, linked_ad_action_type, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                aggregate_id,
                diagnosis_run_id,
                date,
                country,
                media_source,
                campaign_id,
                adset_id,
                ad_id,
                '',
                sample,
                success,
                lost,
                _json(top_reasons),
                _json({
                    'avg_seconds': round(sum(response_values) / len(response_values), 2) if response_values else 0.0,
                    'over_60s': sum(1 for value in response_values if value > 60),
                    'within_60s_rate': round(sum(1 for value in response_values if 0 < value <= 60) / sample, 4) if sample else 0.0,
                }),
                _json(dict(dropoff_counts)),
                _json(actions),
                _linked_ad_diagnosis(top_reasons),
                _linked_ad_action(top_reasons),
                now,
                now,
            ),
        )
        count += 1
    conn.commit()
    return {'ok': True, 'diagnosis_run_id': diagnosis_run_id, 'aggregate_count': count}


def _recommended_actions(top_reasons: Sequence[Dict[str, Any]]) -> List[str]:
    actions: List[str] = []
    reason_keys = {str(item.get('diagnosis') or '') for item in top_reasons}
    if 'linky_trust_explanation_missing' in reason_keys:
        actions.append('优化 Linky 信任解释话术，不优先改素材。')
    if 'cs_first_response_slow' in reason_keys:
        actions.append('提升 1 分钟内首响覆盖。')
    if 'ad_promise_mismatch' in reason_keys:
        actions.append('复核广告素材收益表达，必要时生成修正素材。')
    if not actions:
        actions.append('进入影子运行观察，抽样人工复核关键对话。')
    return actions


def _linked_ad_diagnosis(top_reasons: Sequence[Dict[str, Any]]) -> str:
    keys = {str(item.get('diagnosis') or '') for item in top_reasons}
    if 'ad_promise_mismatch' in keys:
        return 'low_quality_traffic'
    if keys & {'linky_trust_explanation_missing', 'cs_first_response_slow', 'unclear_steps'}:
        return 'im_handoff_issue'
    if 'crm_process_issue' in keys:
        return 'linky_crm_issue'
    return 'continue_observe'


def _linked_ad_action(top_reasons: Sequence[Dict[str, Any]]) -> str:
    keys = {str(item.get('diagnosis') or '') for item in top_reasons}
    if 'ad_promise_mismatch' in keys:
        return 'generate_repair_creative'
    if keys & {'linky_trust_explanation_missing', 'cs_first_response_slow', 'unclear_steps'}:
        return 'im_script_improvement'
    if 'crm_process_issue' in keys:
        return 'inspect_linky_crm'
    return 'observe'


FUNNEL_STAGE_LABELS = {
    'before_first_user_reply': '进入 IM 后未首回',
    'before_im_message_ge_3': '未形成有效对话',
    'before_link_sent': '未发 Linky 链接',
    'before_link_clicked': '发链后未点击',
    'after_link_sent_before_link_click_or_linky_registration': '发链后未点击或未注册',
    'after_link_sent_before_linky_registration': '发链后未注册',
    'after_link_click_before_bind': '点链后未 bind',
    'after_bind_request_before_success': 'bind 后未成功入会',
    'converted': '已入会',
}


FUNNEL_STAGE_ACTIONS = {
    'before_first_user_reply': '检查广告承诺、首条消息和用户唤醒话术；不要只看 CPA 判断素材。',
    'before_im_message_ge_3': '判断是素材承诺偏差还是开场承接弱；优先抽样看对话。',
    'before_link_sent': '检查客服或机器人是否及时推进 Linky 链接。',
    'before_link_clicked': '补强 Linky 信任解释，说明它是免费社交聊天 App、官方合作机构、安全边界、钻石来源和提现规则。',
    'after_link_sent_before_link_click_or_linky_registration': '补强 Linky 社交聊天 App 解释，同时检查点击、免费注册和 Linky ID 引导是否清楚。',
    'after_link_sent_before_linky_registration': '补强 Linky 注册解释，说明免费、安全、聊天/礼物获得钻石、提现规则和注册后找 Linky ID。',
    'after_link_click_before_bind': '优化 Linky ID / bind 指引，降低跳转、安全、免费、钻石规则和“为什么要给 ID”的疑虑。',
    'after_bind_request_before_success': '检查 bind、CRM succeed 和客服后续确认链路。',
}


DIAGNOSIS_EXPERIMENT_RULES: Dict[str, Dict[str, str]] = {
    'silent_user_not_reactivated': {
        'funnel_stage': 'before_first_user_reply',
        'target_metric': 'R1 回复率 / 用户首回率',
        'hypothesis': '用低压力问题替代直接催注册，可以提升进入 IM 后的用户首回率。',
        'success_metric': 'first_user_reply_rate',
    },
    'opening_trust_missing': {
        'funnel_stage': 'before_im_message_ge_3',
        'target_metric': '真人消息>=3 率',
        'hypothesis': '先建立信任和解释工作方式，可以提升用户继续互动的比例。',
        'success_metric': 'im_message_ge_3_rate',
    },
    'linky_trust_explanation_missing': {
        'funnel_stage': 'before_link_clicked',
        'target_metric': 'Linky 链接点击率',
        'hypothesis': '先解释 Linky 是官方合作机构推荐的免费社交聊天 App，并说明安全边界、聊天/礼物获得钻石和提现按平台规则，再给链接，可以提升发链后的点击率。',
        'success_metric': 'link_click_rate',
    },
    'early_registration_push': {
        'funnel_stage': 'before_link_clicked',
        'target_metric': 'Linky 链接点击率 / 用户继续回复率',
        'hypothesis': '把“先注册”改成“先解释流程再注册”，可以降低用户警惕并提升继续推进率。',
        'success_metric': 'link_click_rate',
    },
    'linky_registration_guidance_failed': {
        'funnel_stage': 'after_link_click_before_bind',
        'target_metric': 'Linky 点击→注册 / bind 请求率',
        'hypothesis': '把 Linky 后续步骤拆成“免费注册社交聊天 App、到我的页复制 Linky ID、发给客服开通接待/收益权限”的明确动作，可以提升点击后的注册和 bind 推进。',
        'success_metric': 'bind_request_rate',
    },
    'bind_guidance_failed': {
        'funnel_stage': 'after_bind_request_before_success',
        'target_metric': 'bind 请求→bind 成功 / CRM succeed 率',
        'hypothesis': '在 bind 失败时要求用户反馈错误提示并给出纠错路径，可以提升 bind 成功率。',
        'success_metric': 'bind_success_rate',
    },
    'scam_like_script': {
        'funnel_stage': 'before_link_clicked',
        'target_metric': '用户继续回复率 / Linky 点击率',
        'hypothesis': '先解释费用、资料用途和流程边界，减少诈骗感，可以提升用户信任和继续推进。',
        'success_metric': 'link_click_rate',
    },
    'unclear_steps': {
        'funnel_stage': 'before_link_clicked',
        'target_metric': '下一步完成率',
        'hypothesis': '每次只给一个动作和完成后的反馈要求，可以降低步骤混乱导致的流失。',
        'success_metric': 'next_step_completion_rate',
    },
    'auto_message_handoff_failed': {
        'funnel_stage': 'before_first_user_reply',
        'target_metric': '自动触达→R1 回复率',
        'hypothesis': '自动触达后补一条低压人工/半自动承接，可以提高用户回应率。',
        'success_metric': 'r1_after_auto_apply_reply_rate',
    },
    'ad_promise_mismatch': {
        'funnel_stage': 'before_im_message_ge_3',
        'target_metric': '有效 IM 率 / Linky 点击率',
        'hypothesis': '把收益刺激改成真实工作方式解释，可以减少低质量流量和承诺偏差。',
        'success_metric': 'user_engaged_im_rate',
    },
}


def _funnel_stage_label(stage: str) -> str:
    raw = str(stage or '')
    if raw in FUNNEL_STAGE_LABELS:
        return FUNNEL_STAGE_LABELS[raw]
    normalized = raw.lower()
    if not normalized:
        return '未分阶段'
    if 'human_message' in normalized or 'message_ge_3' in normalized:
        return '未形成有效对话'
    if 'link_click' in normalized or 'no_click' in normalized:
        return '发链后未点击或未注册'
    if ('registration' in normalized or 'bind' in normalized or 'linky_registered' in normalized) and ('link' in normalized or 'linky' in normalized):
        return '发链后未注册或未 bind'
    if 'first_user_reply' in normalized:
        return '进入 IM 后未首回'
    if 'link_sent' in normalized:
        if 'link_click' in normalized or 'no_click' in normalized:
            return '发链后未点击或未注册'
        if 'registration' in normalized or 'bind' in normalized or 'linky_registered' in normalized:
            return '发链后未注册或未 bind'
        return '未发送下载链'
    if 'link_click' in normalized:
        return '点链后未 bind'
    if 'bind' in normalized:
        return 'bind 后未成功入会'
    return raw or '未分阶段'


def _funnel_stage_action(stage: str, diagnosis: str) -> str:
    if str(stage or '') == 'before_link_clicked':
        return '检查对应 App 是否被解释为免费、安全的社交聊天 App，并讲清聊天/礼物获得钻石、提现规则和为什么要继续点击。'
    if str(stage or '') == 'after_link_click_before_bind':
        return '检查注册后查找并提交平台 ID、绑定官方合作机构的说明是否清楚，优先优化 IM 话术和回流指引。'
    if diagnosis == 'ad_promise_mismatch':
        return '回看素材承诺与 IM 话术是否一致，必要时生成修正素材。'
    if diagnosis == 'cs_first_response_slow':
        return '抽查超时样本，优化首响排班、提醒和接待优先级。'
    if diagnosis in {'linky_registration_guidance_failed', 'bind_guidance_failed'}:
        return '重写对应 App / bind 引导话术，并验证点击到 bind 的推进率。'
    if diagnosis in {'unclear_steps', 'linky_trust_explanation_missing'}:
        return '把流程、对应 App 作用和下一步说清楚，避免用户点链前流失。'
    if diagnosis == 'silent_user_not_reactivated':
        return '补用户沉默后的二次唤醒话术，不要重复机械催促。'
    return FUNNEL_STAGE_ACTIONS.get(str(stage or ''), '系统结合历史对话归因后，给出对应话术、流程或素材动作。')


def _script_experiment_plan(
    *,
    diagnosis_type: str,
    dropoff_stage: str = '',
    country: Any = '',
    language: Any = '',
    source_count: int = 1,
) -> Dict[str, Any]:
    diagnosis = str(diagnosis_type or '').strip()
    rule = dict(DIAGNOSIS_EXPERIMENT_RULES.get(diagnosis) or {})
    stage = str(rule.get('funnel_stage') or dropoff_stage or '')
    target_metric = str(rule.get('target_metric') or '对应漏斗下一步转化率')
    hypothesis = str(rule.get('hypothesis') or '替换更清晰、更可信、更低压力的话术后，目标漏斗指标应提升。')
    sample_count = max(1, int(source_count or 1))
    return {
        'funnel_stage': stage,
        'funnel_stage_label': _funnel_stage_label(stage),
        'target_metric': target_metric,
        'experiment_hypothesis': hypothesis,
        'experiment_design': {
            'experiment_type': 'script_ab_shadow_then_live',
            'unit': 'conversation',
            'sample_source': 'same_country_language_stage',
            'suggested_min_sample': max(100, sample_count * 20),
            'observation_window_hours': 24 if sample_count >= 30 else 48,
            'primary_metric': str(rule.get('success_metric') or 'next_step_conversion_rate'),
            'guardrail_metrics': ['真实入会率不下降', '投诉/诈骗感反馈不升高', 'PII 和承诺风险为 0'],
            'rollout_rule': '先 shadow 复核，再按国家/语言小流量测试；达到样本后才替换 SOP。',
        },
        'country': _canonical_country(country),
        'language': _script_language(country, language),
    }


def _compact_unique_phrases(values: Sequence[str], *, limit: int = 3, max_len: int = 96) -> List[str]:
    seen = set()
    compact: List[str] = []
    for value in values:
        text = re.sub(r'\s+', ' ', str(value or '').strip())
        if not text:
            continue
        key = re.sub(r'[\W_]+', '', text.lower())[:80]
        if not key or key in seen:
            continue
        seen.add(key)
        compact.append(text[:max_len])
        if len(compact) >= limit:
            break
    return compact


def _success_pattern_summary(conn: sqlite3.Connection, *, country: Any = '', language: Any = '', limit: int = 3) -> str:
    normalized_country = _canonical_country(country)
    normalized_language = _script_language(country, language)
    rows = conn.execute(
        """
        SELECT c.conversation_id, d.evidence_json, d.recommended_replacement_json
        FROM im_conversation_diagnoses d
        JOIN im_conversations c ON c.conversation_id = d.conversation_id
        WHERE d.primary_diagnosis = 'success_sample'
          AND (? = '' OR c.country = ?)
          AND (? = '' OR c.language = ? OR c.language = ?)
        ORDER BY d.created_at DESC
        LIMIT ?
        """,
        (
            normalized_country,
            normalized_country,
            normalized_language,
            normalized_language,
            str(language or ''),
            max(1, int(limit or 3) * 4),
        ),
    ).fetchall()
    snippets: List[str] = []
    for row in rows:
        evidence = _loads(row['evidence_json'], [])
        for item in evidence:
            summary = str((item or {}).get('summary') or '').strip()
            speaker = str((item or {}).get('speaker') or '').strip()
            if summary and speaker and 'agent' in speaker.lower():
                snippets.append(summary)
                break
    snippets = _compact_unique_phrases(snippets, limit=limit)
    if snippets:
        return '成功样本常见做法：' + '；'.join(snippets)
    return '成功样本不足；先按同国家同阶段小流量实验验证。'


def _failure_pattern_summary(agent_issue: Any, evidence: Sequence[Dict[str, Any]], diagnosis_type: str) -> str:
    snippets = []
    for item in evidence[:5]:
        speaker = str((item or {}).get('speaker') or '').lower()
        summary = str((item or {}).get('summary') or '').strip()
        if summary and ('agent' in speaker or 'bot' in speaker or 'system' in speaker):
            snippets.append(summary)
    compact = _compact_unique_phrases(snippets, limit=2)
    issue = str(agent_issue or DIAGNOSIS_LABELS.get(diagnosis_type, diagnosis_type) or '').strip()
    if compact:
        return f'失败样本常见说法：{"；".join(compact)}'
    return issue[:180] if issue else DIAGNOSIS_LABELS.get(diagnosis_type, diagnosis_type)


def _failure_pattern_translation_zh(
    summary: Any,
    diagnosis_type: str,
    *,
    country: Any = '',
    language: Any = '',
) -> str:
    text = str(summary or '').strip()
    if not text:
        return DIAGNOSIS_LABELS.get(diagnosis_type, diagnosis_type)
    mixed_prefix = ''
    if text.startswith('失败样本常见说法：'):
        mixed_prefix = '失败样本常见说法：'
        text = text.split('：', 1)[1].strip()
    if _contains_cjk(text) and not re.search(r'[A-Za-zÀ-ÿ]{3,}', text):
        return scan_pii(text)['redacted_text'][:1200]
    translated = _machine_translate_script_zh(text, country=country, language=language)
    if translated:
        return scan_pii(f'{mixed_prefix}{translated}'.strip())['redacted_text'][:1200]
    label = DIAGNOSIS_LABELS.get(diagnosis_type, diagnosis_type)
    return f'{label}。失败样本原文暂未翻译，请重新研究生成。'


def _history_snippet(text: Any, *, max_len: int = 180) -> str:
    scanned = scan_pii(re.sub(r'\s+', ' ', str(text or '').strip()))
    return scanned['redacted_text'][:max_len]


def _conversation_message_snippets(
    conn: sqlite3.Connection,
    conversation_id: str,
    *,
    sender_group: str = 'agent',
    limit: int = 3,
) -> List[Dict[str, Any]]:
    senders = {
        'agent': ('bot', 'agent_manual', 'agent_template', 'system'),
        'user': ('user',),
    }.get(sender_group, ())
    params: List[Any] = [conversation_id]
    sender_clause = ''
    if senders:
        sender_clause = ' AND sender_type IN (' + ','.join('?' for _ in senders) + ')'
        params.extend(senders)
    params.append(max(1, int(limit or 3) * 3))
    rows = conn.execute(
        """
        SELECT message_index, sender_type, message_text_redacted
        FROM im_messages
        WHERE conversation_id = ?
        """ + sender_clause + """
          AND COALESCE(message_text_redacted, '') != ''
        ORDER BY message_index ASC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    snippets: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        text = _history_snippet(row['message_text_redacted'])
        key = re.sub(r'[\W_]+', '', text.lower())[:80]
        if not text or key in seen:
            continue
        seen.add(key)
        snippets.append({
            'turn_index': int(row['message_index'] or 0),
            'speaker': str(row['sender_type'] or ''),
            'text': text,
        })
        if len(snippets) >= limit:
            break
    return snippets


def _historical_context_for_llm(
    conn: sqlite3.Connection,
    conversation: Dict[str, Any],
    diagnosis: Dict[str, Any],
    *,
    limit: int = 4,
) -> Dict[str, Any]:
    country = _canonical_country(conversation.get('country') or '')
    language = _script_language(country, conversation.get('language') or '')
    current_id = str(conversation.get('conversation_id') or '')
    primary = str(diagnosis.get('primary_diagnosis') or '').strip()
    dropoff_stage = str(diagnosis.get('dropoff_stage') or conversation.get('dropoff_stage') or '').strip()
    params: List[Any] = [current_id, country, country, language, language, str(conversation.get('language') or '')]
    match_clause = ''
    if primary or dropoff_stage:
        match_clause = ' AND ('
        sub = []
        if primary:
            sub.append('d.primary_diagnosis = ?')
            params.append(primary)
        if dropoff_stage:
            sub.append('COALESCE(d.dropoff_stage, c.dropoff_stage) = ?')
            params.append(dropoff_stage)
        match_clause += ' OR '.join(sub) + ')'
    params.append(max(1, int(limit or 4) * 3))
    failure_rows = conn.execute(
        """
        SELECT c.conversation_id, c.country, c.language, COALESCE(d.dropoff_stage, c.dropoff_stage) AS dropoff_stage,
               d.primary_diagnosis, d.agent_issue, d.evidence_json
        FROM im_conversation_diagnoses d
        JOIN im_conversations c ON c.conversation_id = d.conversation_id
        WHERE c.conversation_id != ?
          AND (? = '' OR c.country = ?)
          AND (? = '' OR c.language = ? OR c.language = ?)
          AND COALESCE(d.primary_diagnosis, '') != 'success_sample'
        """ + match_clause + """
        ORDER BY d.created_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    failures: List[Dict[str, Any]] = []
    for row in failure_rows:
        evidence = _loads(row['evidence_json'], [])
        evidence_text = [
            _history_snippet((item or {}).get('summary') or '')
            for item in list(evidence or [])[:3]
            if _history_snippet((item or {}).get('summary') or '')
        ]
        failures.append({
            'conversation_key': str(row['conversation_id'] or ''),
            'country': str(row['country'] or ''),
            'language': str(row['language'] or ''),
            'dropoff_stage': str(row['dropoff_stage'] or ''),
            'diagnosis': str(row['primary_diagnosis'] or ''),
            'diagnosis_zh': DIAGNOSIS_LABELS.get(str(row['primary_diagnosis'] or ''), str(row['primary_diagnosis'] or '')),
            'agent_issue': _history_snippet(row['agent_issue']),
            'evidence': evidence_text[:3],
            'agent_snippets': _conversation_message_snippets(conn, str(row['conversation_id'] or ''), sender_group='agent', limit=2),
        })
        if len(failures) >= limit:
            break
    success_rows = conn.execute(
        """
        SELECT c.conversation_id, c.country, c.language, d.evidence_json
        FROM im_conversation_diagnoses d
        JOIN im_conversations c ON c.conversation_id = d.conversation_id
        WHERE d.primary_diagnosis = 'success_sample'
          AND c.conversation_id != ?
          AND (? = '' OR c.country = ?)
          AND (? = '' OR c.language = ? OR c.language = ?)
        ORDER BY d.created_at DESC
        LIMIT ?
        """,
        (
            current_id,
            country,
            country,
            language,
            language,
            str(conversation.get('language') or ''),
            max(1, int(limit or 4) * 3),
        ),
    ).fetchall()
    successes: List[Dict[str, Any]] = []
    for row in success_rows:
        successes.append({
            'conversation_key': str(row['conversation_id'] or ''),
            'country': str(row['country'] or ''),
            'language': str(row['language'] or ''),
            'agent_snippets': _conversation_message_snippets(conn, str(row['conversation_id'] or ''), sender_group='agent', limit=3),
            'user_snippets': _conversation_message_snippets(conn, str(row['conversation_id'] or ''), sender_group='user', limit=2),
        })
        if len(successes) >= limit:
            break
    return {
        'purpose': '用同国家、同语言、同掉点的历史失败对话和成功入会对话做对照，避免凭空生成话术。',
        'country': country,
        'language': language,
        'dropoff_stage': dropoff_stage,
        'baseline_diagnosis': primary,
        'failure_patterns': failures,
        'success_patterns': successes,
        'attribution_candidates_allowed': [
            {'key': key, 'label_zh': label}
            for key, label in SCRIPT_ATTRIBUTION_LABELS.items()
        ],
        'style_guidance': [
            '建议话术必须像当地真人客服，不要模板腔。',
            '可以少量使用友好 emoji，例如 😊、✅、✨，也可以在收益解释场景少量使用语境匹配的 emoji；最多 1-2 个，不能表达固定收益、暴富或强刺激承诺。',
            '优先复用成功样本里的解释结构，再针对失败样本缺口改写。',
        ],
    }


def _script_priority_score(*, source_count: int, diagnosis_type: str, funnel_stage: str, experiment_status: str = '') -> int:
    stage_weight = {
        'before_first_user_reply': 80,
        'before_im_message_ge_3': 75,
        'before_link_clicked': 70,
        'after_link_click_before_bind': 65,
        'after_bind_request_before_success': 55,
    }.get(str(funnel_stage or ''), 40)
    diagnosis_weight = {
        'scam_like_script': 30,
        'linky_registration_guidance_failed': 28,
        'linky_trust_explanation_missing': 26,
        'silent_user_not_reactivated': 24,
        'ad_promise_mismatch': 22,
        'unclear_steps': 18,
    }.get(str(diagnosis_type or ''), 10)
    status_bonus = 20 if str(experiment_status or '') in {'shadow_review', 'testing'} else 0
    return stage_weight + diagnosis_weight + min(max(int(source_count or 0), 0), 200) + status_bonus


SCRIPT_FUNNEL_STAGE_ORDER = {
    'before_first_user_reply': 10,
    'before_im_message_ge_3': 20,
    'before_link_sent': 30,
    'before_link_clicked': 40,
    'after_link_sent_before_link_click_or_linky_registration': 40,
    'after_link_sent_before_linky_registration': 50,
    'after_link_click_before_bind': 50,
    'after_bind_request_before_success': 60,
    'converted': 70,
}


def _script_funnel_stage_order(stage: Any) -> int:
    raw = str(stage or '')
    if raw in SCRIPT_FUNNEL_STAGE_ORDER:
        return SCRIPT_FUNNEL_STAGE_ORDER[raw]
    normalized = raw.lower()
    if not normalized:
        return 999
    if 'human_message' in normalized or 'message_ge_3' in normalized:
        return 20
    if 'link_click' in normalized or 'no_click' in normalized:
        return 40
    if ('registration' in normalized or 'bind' in normalized or 'linky_registered' in normalized) and ('link' in normalized or 'linky' in normalized):
        return 50
    if 'first_user_reply' in normalized:
        return 10
    if 'link_sent' in normalized:
        if 'link_click' in normalized or 'no_click' in normalized:
            return 40
        if 'registration' in normalized or 'bind' in normalized or 'linky_registered' in normalized:
            return 50
        return 30
    if 'link_click' in normalized:
        return 50
    if 'bind' in normalized:
        return 60
    return 999


def _balanced_script_suggestions(rows: Sequence[Dict[str, Any]], limit: int, *, per_country_cap: Optional[int] = None) -> List[Dict[str, Any]]:
    max_items = max(1, int(limit or 1))
    sort_key = lambda item: (
        int(item.get('funnel_stage_order') or 999),
        -int(item.get('priority_score') or 0),
        -int(item.get('source_conversation_count') or 0),
        str(item.get('updated_at') or ''),
    )
    ordered = sorted(rows, key=sort_key)
    selected: List[Dict[str, Any]] = []
    selected_ids: set[str] = set()
    country_counts: Dict[str, int] = {}
    stage_country_seen: set[Tuple[str, int]] = set()
    if per_country_cap is not None:
        cap = max(1, int(per_country_cap or 1))
        for row in ordered:
            country = str(row.get('country') or '').strip().lower() or 'unknown'
            stage_order = int(row.get('funnel_stage_order') or 999)
            row_id = str(row.get('script_suggestion_id') or id(row))
            if (
                row_id in selected_ids
                or country_counts.get(country, 0) >= cap
                or (country, stage_order) in stage_country_seen
            ):
                continue
            selected.append(row)
            selected_ids.add(row_id)
            country_counts[country] = country_counts.get(country, 0) + 1
            stage_country_seen.add((country, stage_order))
            if len(selected) >= max_items:
                return sorted(selected, key=sort_key)
        for row in ordered:
            country = str(row.get('country') or '').strip().lower() or 'unknown'
            row_id = str(row.get('script_suggestion_id') or id(row))
            if row_id in selected_ids or country_counts.get(country, 0) >= cap:
                continue
            selected.append(row)
            selected_ids.add(row_id)
            country_counts[country] = country_counts.get(country, 0) + 1
            if len(selected) >= max_items:
                return sorted(selected, key=sort_key)
        return sorted(selected, key=sort_key)
    stage_counts: Dict[int, int] = {}
    for row in ordered:
        stage_order = int(row.get('funnel_stage_order') or 999)
        row_id = str(row.get('script_suggestion_id') or id(row))
        if row_id in selected_ids:
            continue
        if stage_counts.get(stage_order, 0) >= 2:
            continue
        selected.append(row)
        selected_ids.add(row_id)
        stage_counts[stage_order] = stage_counts.get(stage_order, 0) + 1
        if len(selected) >= max_items:
            return selected
    for row in ordered:
        row_id = str(row.get('script_suggestion_id') or id(row))
        if row_id in selected_ids:
            continue
        selected.append(row)
        if len(selected) >= max_items:
            break
    return sorted(selected, key=sort_key)


def _script_semantic_label(value: Any) -> str:
    text = str(value or '').strip().lower()
    if not text:
        return ''
    text = re.sub(r'\[[a-z0-9_ -]+\]', ' ', text)
    text = re.sub(r'[\W_]+', ' ', text, flags=re.UNICODE)
    text = re.sub(r'\s+', ' ', text).strip()
    noisy_prefixes = (
        'usuária',
        'usuaria',
        'usuario',
        'user',
        'high intent',
        'high-intent',
    )
    if any(text.startswith(prefix) for prefix in noisy_prefixes):
        return ''
    return text[:80]


def _script_suggestion_dedupe_key(row: Dict[str, Any]) -> Tuple[str, str, str, str, str, str, str]:
    semantic_label = (
        _script_semantic_label(row.get('user_concern_type'))
        or _script_semantic_label(row.get('current_state'))
    )
    return (
        str(row.get('country') or '').strip().lower(),
        str(row.get('language') or '').strip().lower(),
        str(row.get('funnel_stage') or '').strip().lower(),
        str(row.get('diagnosis_type') or '').strip().lower(),
        str(row.get('target_metric') or '').strip().lower(),
        str(row.get('current_state') or '').strip().lower(),
        semantic_label,
    )


def _dedupe_script_suggestions(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    for row in rows:
        key = _script_suggestion_dedupe_key(row)
        existing = merged.get(key)
        if not existing:
            item = dict(row)
            item['duplicate_count'] = 1
            merged[key] = item
            continue
        existing['duplicate_count'] = int(existing.get('duplicate_count') or 1) + 1
        existing['source_conversation_count'] = max(
            int(existing.get('source_conversation_count') or 0),
            int(row.get('source_conversation_count') or 0),
        )
        if _contains_cjk(existing.get('suggested_script')) and not _contains_cjk(row.get('suggested_script')):
            existing['suggested_script'] = row.get('suggested_script') or ''
            existing['suggested_script_translation_zh'] = row.get('suggested_script_translation_zh') or existing.get('suggested_script_translation_zh') or ''
            existing['suggested_script_source'] = row.get('suggested_script_source') or existing.get('suggested_script_source') or ''
            existing['suggested_script_translation_source'] = row.get('suggested_script_translation_source') or existing.get('suggested_script_translation_source') or ''
            existing['old_script_summary_translation_source'] = row.get('old_script_summary_translation_source') or existing.get('old_script_summary_translation_source') or ''
            existing['old_script_summary_interpretation_source'] = row.get('old_script_summary_interpretation_source') or existing.get('old_script_summary_interpretation_source') or ''
            existing['old_script_summary_interpretation_zh'] = row.get('old_script_summary_interpretation_zh') or existing.get('old_script_summary_interpretation_zh') or ''
    return list(merged.values())


def _upsert_script_suggestion(
    conn: sqlite3.Connection,
    *,
    diagnosis_type: str,
    country: Any,
    language: Any,
    scenario: Any,
    agent_issue: Any,
    suggested_message: Any,
    evidence: Sequence[Dict[str, Any]],
    conversation_id: str,
    suggested_message_translation_zh: Any = '',
    old_script_summary_original: Any = '',
    old_script_summary_translation_zh: Any = '',
    old_script_summary_interpretation_zh: Any = '',
    dropoff_stage: str = '',
    source: str = 'rule',
    now: str = '',
    source_count: int = 1,
    risk_score: Any = None,
    launch_decision: Any = '',
    current_state: Any = '',
    user_concern_type: Any = '',
) -> str:
    now = now or _utc_now()
    normalized_country = _canonical_country(country)
    normalized_language = _script_language(country, language)
    raw_suggested = str(suggested_message or '').strip()
    suggested = _localized_non_cjk_script(diagnosis_type, normalized_country, normalized_language, raw_suggested)
    hermes_source = str(source or '').strip() == HERMES_LLM_PROVIDER_MODE
    translation_zh = scan_pii(str(suggested_message_translation_zh or '').strip())['redacted_text'][:1200] if hermes_source else ''
    if hermes_source and _has_invalid_linky_positioning(
        suggested,
        translation_zh,
        old_script_summary_translation_zh,
        old_script_summary_interpretation_zh,
    ):
        return ''
    translation_source = HERMES_LLM_PROVIDER_MODE if hermes_source and suggested and translation_zh else ''
    suggested_source = HERMES_LLM_PROVIDER_MODE if hermes_source and suggested and translation_zh else str(source or 'rule')
    experiment_plan = _script_experiment_plan(
        diagnosis_type=diagnosis_type,
        dropoff_stage=dropoff_stage,
        country=normalized_country,
        language=normalized_language,
        source_count=source_count,
    )
    suggestion_id = _stable_id(
        diagnosis_type,
        normalized_country,
        normalized_language,
        str(experiment_plan.get('funnel_stage') or ''),
        str(scenario or DIAGNOSIS_LABELS.get(diagnosis_type, diagnosis_type)),
        prefix='im_script_',
    )
    success_pattern = _success_pattern_summary(conn, country=normalized_country, language=normalized_language)
    old_summary = scan_pii(str(old_script_summary_original or '').strip())['redacted_text'][:1200] if hermes_source and str(old_script_summary_original or '').strip() else _failure_pattern_summary(agent_issue, evidence, diagnosis_type)
    old_summary_translation = scan_pii(str(old_script_summary_translation_zh or '').strip())['redacted_text'][:1200] if hermes_source else ''
    old_summary_translation_source = HERMES_LLM_PROVIDER_MODE if hermes_source and old_summary_translation else ''
    old_summary_interpretation = scan_pii(str(old_script_summary_interpretation_zh or '').strip())['redacted_text'][:1200] if hermes_source else ''
    old_summary_interpretation_source = HERMES_LLM_PROVIDER_MODE if hermes_source and old_summary_interpretation else ''
    normalized_risk_score = _normalize_script_risk_score(risk_score)
    max_risk_score = _max_script_risk_score(normalized_risk_score)
    normalized_launch_decision = _normalize_launch_decision(
        launch_decision,
        normalized_risk_score,
        has_complete_script=bool(suggested and translation_zh),
    )
    normalized_current_state = _normalize_funnel_state(current_state, dropoff_stage)
    normalized_user_concern_type = _normalize_user_concern_type(user_concern_type, agent_issue)
    evidence_summary = {'conversation_id': conversation_id, 'source': source, 'evidence': list(evidence or [])[:2]}
    conn.execute(
        """
        INSERT INTO im_script_suggestions (
            script_suggestion_id, country, language, scenario, diagnosis_type, funnel_stage,
            target_metric, experiment_hypothesis, old_script_summary, old_script_summary_translation_zh,
            old_script_summary_translation_source, old_script_summary_interpretation_zh, old_script_summary_interpretation_source,
            suggested_script, suggested_script_translation_zh, suggested_script_source, suggested_script_translation_source,
            risk_tags_json, risk_score_json, max_risk_score, launch_decision, current_state, user_concern_type,
            source_conversation_count, success_pattern_summary, evidence_summary_json,
            experiment_design_json,
            approval_status, approved_by, approved_at, experiment_status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(script_suggestion_id) DO UPDATE SET
            source_conversation_count = im_script_suggestions.source_conversation_count + excluded.source_conversation_count,
            old_script_summary = CASE
                WHEN excluded.old_script_summary != ''
                     AND im_script_suggestions.old_script_summary_translation_source = ? THEN excluded.old_script_summary
                WHEN im_script_suggestions.old_script_summary = '' THEN excluded.old_script_summary
                ELSE im_script_suggestions.old_script_summary
            END,
            old_script_summary_translation_zh = CASE
                WHEN im_script_suggestions.old_script_summary_translation_source != ? THEN excluded.old_script_summary_translation_zh
                ELSE im_script_suggestions.old_script_summary_translation_zh
            END,
            old_script_summary_translation_source = CASE
                WHEN im_script_suggestions.old_script_summary_translation_source != ? THEN excluded.old_script_summary_translation_source
                ELSE im_script_suggestions.old_script_summary_translation_source
            END,
            old_script_summary_interpretation_zh = CASE
                WHEN im_script_suggestions.old_script_summary_interpretation_source != ? THEN excluded.old_script_summary_interpretation_zh
                ELSE im_script_suggestions.old_script_summary_interpretation_zh
            END,
            old_script_summary_interpretation_source = CASE
                WHEN im_script_suggestions.old_script_summary_interpretation_source != ? THEN excluded.old_script_summary_interpretation_source
                ELSE im_script_suggestions.old_script_summary_interpretation_source
            END,
            success_pattern_summary = CASE
                WHEN im_script_suggestions.success_pattern_summary LIKE '成功样本不足%' THEN excluded.success_pattern_summary
                ELSE im_script_suggestions.success_pattern_summary
            END,
            evidence_summary_json = excluded.evidence_summary_json,
            target_metric = excluded.target_metric,
            experiment_hypothesis = excluded.experiment_hypothesis,
            suggested_script = CASE
                WHEN im_script_suggestions.suggested_script_source != ? THEN excluded.suggested_script
                ELSE im_script_suggestions.suggested_script
            END,
            suggested_script_translation_zh = CASE
                WHEN im_script_suggestions.suggested_script_translation_source != ? THEN excluded.suggested_script_translation_zh
                ELSE im_script_suggestions.suggested_script_translation_zh
            END,
            suggested_script_source = CASE
                WHEN im_script_suggestions.suggested_script_source != ? THEN excluded.suggested_script_source
                ELSE im_script_suggestions.suggested_script_source
            END,
            suggested_script_translation_source = CASE
                WHEN im_script_suggestions.suggested_script_translation_source != ? THEN excluded.suggested_script_translation_source
                ELSE im_script_suggestions.suggested_script_translation_source
            END,
            risk_score_json = excluded.risk_score_json,
            max_risk_score = excluded.max_risk_score,
            launch_decision = excluded.launch_decision,
            current_state = excluded.current_state,
            user_concern_type = excluded.user_concern_type,
            experiment_design_json = excluded.experiment_design_json,
            updated_at = excluded.updated_at
        """,
        (
            suggestion_id,
            normalized_country,
            normalized_language,
            str(scenario or DIAGNOSIS_LABELS.get(diagnosis_type, diagnosis_type)),
            diagnosis_type,
            str(experiment_plan.get('funnel_stage') or ''),
            str(experiment_plan.get('target_metric') or ''),
            str(experiment_plan.get('experiment_hypothesis') or ''),
            old_summary,
            old_summary_translation,
            old_summary_translation_source,
            old_summary_interpretation,
            old_summary_interpretation_source,
            suggested,
            translation_zh,
            suggested_source,
            translation_source,
            '[]',
            _json(normalized_risk_score),
            max_risk_score,
            normalized_launch_decision,
            normalized_current_state,
            normalized_user_concern_type,
            max(1, int(source_count or 1)),
            success_pattern,
            _json(evidence_summary),
            _json(experiment_plan.get('experiment_design') or {}),
            'draft',
            '',
            '',
            '',
            now,
            now,
            HERMES_LLM_PROVIDER_MODE,
            HERMES_LLM_PROVIDER_MODE,
            HERMES_LLM_PROVIDER_MODE,
            HERMES_LLM_PROVIDER_MODE,
            HERMES_LLM_PROVIDER_MODE,
            HERMES_LLM_PROVIDER_MODE,
            HERMES_LLM_PROVIDER_MODE,
            HERMES_LLM_PROVIDER_MODE,
            HERMES_LLM_PROVIDER_MODE,
        ),
    )
    return suggestion_id


def _latest_diagnosis_run_id(conn: sqlite3.Connection) -> str:
    preferred = conn.execute(
        """
        SELECT diagnosis_run_id
        FROM im_conversation_diagnoses
        WHERE diagnosis_run_id = ?
        LIMIT 1
        """,
        (DEFAULT_IM_DIAGNOSTICS_RUN_ID,),
    ).fetchone()
    if preferred:
        return DEFAULT_IM_DIAGNOSTICS_RUN_ID
    row = conn.execute(
        """
        SELECT diagnosis_run_id
        FROM im_conversation_diagnoses
        GROUP BY diagnosis_run_id
        ORDER BY MAX(created_at) DESC, COUNT(*) DESC
        LIMIT 1
        """
    ).fetchone()
    return str(row['diagnosis_run_id']) if row else ''


def _diagnosis_run_date_window(conn: sqlite3.Connection, diagnosis_run_id: str) -> Tuple[str, str]:
    if not diagnosis_run_id:
        return '', ''
    row = conn.execute(
        """
        SELECT MIN(substr(COALESCE(NULLIF(c.entered_im_at, ''), NULLIF(c.conversation_start_time, ''), c.updated_at), 1, 10)) AS start_date,
               MAX(substr(COALESCE(NULLIF(c.entered_im_at, ''), NULLIF(c.conversation_start_time, ''), c.updated_at), 1, 10)) AS end_date
        FROM im_conversation_diagnoses d
        JOIN im_conversations c ON c.conversation_id = d.conversation_id
        WHERE d.diagnosis_run_id = ?
        """,
        (diagnosis_run_id,),
    ).fetchone()
    return (
        str(row['start_date'] or '')[:10] if row else '',
        str(row['end_date'] or '')[:10] if row else '',
    )


def _region_where_sql(alias: str = 'c', *, include_language: bool = True) -> str:
    prefix = f'{alias}.' if str(alias or '').strip() else ''
    country_expr = f"LOWER(COALESCE({prefix}country, ''))"
    language_clause = ''
    if include_language:
        language_expr = f"LOWER(COALESCE({prefix}language, ''))"
        language_clause = f" OR {language_expr} IN ('es', 'es-es', 'es-mx', 'es-co', 'es-cl', 'spanish', 'local')"
    return (
        " (? = '' OR ? = 'all' "
        f"OR (? = 'brazil' AND {country_expr} IN ('brazil', 'br')) "
        f"OR (? = 'indonesia' AND {country_expr} IN ('indonesia', 'id')) "
        f"OR (? = 'spanish' AND ("
        f"{country_expr} IN ('mexico', 'venezuela', 'colombia', 'chile', 'peru', 'ecuador', 'argentina', 'bolivia', 'paraguay', 'uruguay', 'mex', 've', 'co', 'cl', 'pe', 'ec', 'ar', 'bo', 'py', 'uy') "
        f"{language_clause}))) "
    )


def _region_params(region: str) -> Tuple[str, str, str, str, str]:
    normalized = str(region or '').strip().lower()
    if normalized in {'br', 'bra', 'brazil'}:
        normalized = 'brazil'
    elif normalized in {'id', 'idn', 'indonesia'}:
        normalized = 'indonesia'
    elif normalized in {'es', 'spanish', 'latam', 'latam_es', 'hispanic'}:
        normalized = 'spanish'
    elif normalized not in {'all', 'brazil', 'indonesia', 'spanish'}:
        normalized = ''
    return (normalized, normalized, normalized, normalized, normalized)


def _region_label(region: str) -> str:
    normalized = _region_params(region)[0]
    return {'brazil': '巴西', 'indonesia': '印尼', 'spanish': '西语', 'all': '全部'}.get(normalized, '全部')


def _ops_group_link_click_data_quality(conn: sqlite3.Connection, diagnosis_run_id: str) -> Dict[str, str]:
    """Do not present an uncovered upstream click window as a real zero."""
    window = conn.execute(
        """
        SELECT MIN(date) AS start_date, MAX(date) AS end_date
        FROM im_aggregate_diagnoses
        WHERE diagnosis_run_id = ?
        """,
        (diagnosis_run_id,),
    ).fetchone()
    start_date = str(window['start_date'] or '')[:10] if window else ''
    end_date = str(window['end_date'] or '')[:10] if window else ''
    latest = conn.execute(
        """
        SELECT MAX(substr(event_time, 1, 10)) AS latest_date
        FROM im_conversion_events
        WHERE event_name = 'ops_group_link_clicked'
        """
    ).fetchone()
    latest_date = str(latest['latest_date'] or '')[:10] if latest else ''
    if not start_date or not end_date:
        return {
            'data_quality_status': 'unknown',
            'data_quality_note': '群链接点击明细覆盖范围暂不可确认，当前数值不作为真实零点击。',
        }
    if not latest_date or latest_date < start_date:
        return {
            'data_quality_status': 'missing',
            'data_quality_note': f'群链接点击明细未覆盖本次周期 {start_date} 至 {end_date}，当前 0 不代表真实零点击。',
        }
    if latest_date < end_date:
        return {
            'data_quality_status': 'partial',
            'data_quality_note': f'群链接点击明细仅同步至 {latest_date}，未覆盖本次周期至 {end_date}。',
        }
    return {'data_quality_status': 'available', 'data_quality_note': ''}


def im_diagnostics_summary(conn: sqlite3.Connection, *, diagnosis_run_id: str = '', ad_id: str = '', region: str = '', limit: int = 20, script_limit: Optional[int] = None) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    conn.row_factory = sqlite3.Row
    if not diagnosis_run_id:
        diagnosis_run_id = _latest_diagnosis_run_id(conn)
    params: List[Any] = []
    where = ''
    if diagnosis_run_id:
        where = 'WHERE diagnosis_run_id = ?'
        params.append(diagnosis_run_id)
    if ad_id:
        where += (' AND ' if where else 'WHERE ') + 'ad_id = ?'
        params.append(ad_id)
    normalized_region = _region_params(region)[0]
    if normalized_region:
        where += (' AND ' if where else 'WHERE ') + _region_where_sql('', include_language=False)
        params.extend(_region_params(normalized_region))
    rows = [dict(row) for row in conn.execute(
        f'SELECT * FROM im_aggregate_diagnoses {where} ORDER BY sample_conversations DESC, lost_conversations DESC LIMIT ?',
        (*params, int(limit or 20)),
    ).fetchall()]
    for row in rows:
        row['top_failure_reasons'] = _loads(row.pop('top_failure_reasons_json', '[]'), [])
        row['response_time_summary'] = _loads(row.pop('response_time_summary_json', '{}'), {})
        row['dropoff_summary'] = _loads(row.pop('dropoff_summary_json', '{}'), {})
        row['recommended_actions'] = _loads(row.pop('recommended_actions_json', '[]'), [])
    total_row = conn.execute(
        """
        WITH latest_diagnoses AS (
            SELECT d.*
            FROM im_conversation_diagnoses d
            JOIN (
                SELECT MAX(rowid) AS row_id
                FROM im_conversation_diagnoses
                WHERE (? = '' OR diagnosis_run_id = ?)
                GROUP BY diagnosis_run_id, conversation_id
            ) latest ON latest.row_id = d.rowid
        )
        SELECT COUNT(*) AS sample_conversations,
               SUM(CASE WHEN d.final_outcome IN ('success', 'joined', 'crm_succeeded') THEN 1 ELSE 0 END) AS successful_conversations,
               SUM(CASE WHEN d.primary_diagnosis != 'success_sample' THEN 1 ELSE 0 END) AS lost_conversations
        FROM latest_diagnoses d
        JOIN im_conversations c ON c.conversation_id = d.conversation_id
        WHERE (? = '' OR c.ad_id = ?)
          AND """ + _region_where_sql('c') + """
        """,
        (diagnosis_run_id, diagnosis_run_id, ad_id, ad_id, *_region_params(normalized_region)),
    ).fetchone()
    totals = {
        'sample_conversations': int(total_row['sample_conversations'] or 0) if total_row else 0,
        'successful_conversations': int(total_row['successful_conversations'] or 0) if total_row else 0,
        'lost_conversations': int(total_row['lost_conversations'] or 0) if total_row else 0,
    }
    totals['join_rate'] = round(totals['successful_conversations'] / totals['sample_conversations'], 4) if totals['sample_conversations'] else 0.0
    segment_rows = conn.execute(
        """
        WITH latest_diagnoses AS (
            SELECT d.*
            FROM im_conversation_diagnoses d
            JOIN (
                SELECT MAX(rowid) AS row_id
                FROM im_conversation_diagnoses
                WHERE (? = '' OR diagnosis_run_id = ?)
                GROUP BY diagnosis_run_id, conversation_id
            ) latest ON latest.row_id = d.rowid
        ),
        bot_steps AS (
            SELECT conversation_id,
                   MIN(CASE WHEN bot_rank = 1 THEN message_index END) AS r1_message_index,
                   MIN(CASE WHEN bot_rank = 2 THEN message_index END) AS r2_message_index,
                   MIN(CASE WHEN bot_rank = 3 THEN message_index END) AS r3_message_index
            FROM (
                SELECT conversation_id,
                       message_index,
                       ROW_NUMBER() OVER (
                           PARTITION BY conversation_id
                           ORDER BY COALESCE(NULLIF(message_at, ''), created_at), message_index
                       ) AS bot_rank
                FROM (
                    SELECT conversation_id,
                           message_index,
                           sender_type,
                           is_auto_message,
                           is_template_message,
                           has_link,
                           message_at,
                           created_at,
                           LAG(COALESCE(sender_type, '')) OVER (
                               PARTITION BY conversation_id
                               ORDER BY COALESCE(NULLIF(message_at, ''), created_at), message_index
                           ) AS previous_sender_type
                    FROM im_messages
                ) message_candidates
                WHERE COALESCE(sender_type, '') IN ('bot', 'agent_manual', 'agent_template', 'system')
                  AND (
                      COALESCE(is_auto_message, 0) = 1
                      OR COALESCE(is_template_message, 0) = 1
                      OR COALESCE(has_link, 0) = 1
                      OR COALESCE(sender_type, '') = 'bot'
                      OR (
                          COALESCE(sender_type, '') = 'agent_manual'
                          AND COALESCE(previous_sender_type, '') != 'agent_manual'
                      )
                  )
            ) ranked_bot_messages
            WHERE bot_rank <= 3
            GROUP BY conversation_id
        )
        SELECT CASE
                   WHEN COALESCE(NULLIF(c.handoff_type, ''), 'unknown') = 'human_assisted'
                   THEN 'human_assisted'
                   WHEN COALESCE(NULLIF(c.handoff_type, ''), 'unknown') IN ('non_human', 'bot_automated')
                   THEN 'non_human'
                   ELSE 'unclassified'
               END AS handoff_type,
               COUNT(*) AS sample_conversations,
               SUM(CASE WHEN d.final_outcome IN ('success', 'joined', 'crm_succeeded') THEN 1 ELSE 0 END) AS successful_conversations,
               SUM(CASE WHEN d.primary_diagnosis != 'success_sample' THEN 1 ELSE 0 END) AS lost_conversations,
               AVG(CASE WHEN c.first_response_seconds > 0 THEN c.first_response_seconds ELSE NULL END) AS avg_first_response_seconds,
               SUM(CASE WHEN c.first_response_seconds > 60 THEN 1 ELSE 0 END) AS over_60s,
               SUM(CASE WHEN c.first_response_seconds > 0 AND c.first_response_seconds <= 60 THEN 1 ELSE 0 END) AS within_60s,
               SUM(CASE WHEN bt.conversation_id IS NOT NULL THEN 1 ELSE 0 END) AS bot_timing_rows,
               SUM(CASE
                       WHEN COALESCE(NULLIF(c.handoff_type, ''), 'unknown') = 'human_assisted'
                       THEN CASE WHEN COALESCE(NULLIF(c.first_user_message_at, ''), '') != '' THEN 1 ELSE 0 END
                       WHEN bt.conversation_id IS NOT NULL
                       THEN CASE WHEN bt.auto_apply_sent_at != '' THEN 1 ELSE 0 END
                       ELSE CASE WHEN EXISTS (
                           SELECT 1 FROM im_conversion_events e
                           WHERE e.conversation_id = c.conversation_id
                             AND e.event_name = 'auto_apply_message_sent'
                       ) THEN 1 ELSE 0 END
                   END) AS handoff_entry,
               SUM(CASE WHEN bt.conversation_id IS NOT NULL
                        THEN CASE WHEN bt.auto_apply_sent_at != '' OR bt.r1_sent_at != '' THEN 1 ELSE 0 END
                        ELSE CASE WHEN EXISTS (
                            SELECT 1 FROM im_conversion_events e
                            WHERE e.conversation_id = c.conversation_id
                              AND e.event_name IN ('auto_apply_message_sent', 'message_sent', 'step_triggered')
                        ) THEN 1 ELSE 0 END
                   END) AS system_touched,
               SUM(CASE WHEN bt.conversation_id IS NOT NULL
                        THEN CASE WHEN bt.auto_apply_sent_at != '' THEN 1 ELSE 0 END
                        ELSE CASE WHEN EXISTS (
                            SELECT 1 FROM im_conversion_events e
                            WHERE e.conversation_id = c.conversation_id
                              AND e.event_name = 'auto_apply_message_sent'
                        ) THEN 1 ELSE 0 END
                   END) AS auto_apply_sent,
               SUM(CASE WHEN bs.r1_message_index IS NOT NULL OR EXISTS (
                   SELECT 1 FROM im_messages m
                   WHERE m.conversation_id = c.conversation_id
                     AND UPPER(COALESCE(m.template_name, '')) = 'R1'
                     AND COALESCE(m.sender_type, '') IN ('bot', 'agent_manual', 'agent_template', 'system')
               ) OR (
                   SELECT COUNT(*)
                   FROM im_conversion_events e
                   WHERE e.conversation_id = c.conversation_id
                     AND e.event_name = 'message_sent'
                     AND e.event_source = 'im_bot_flow_events'
               ) >= 1 THEN 1 ELSE 0 END) AS r1_sent_messages,
               SUM(CASE WHEN bs.r1_message_index IS NOT NULL AND EXISTS (
                   SELECT 1
                   FROM im_messages r
                   WHERE r.conversation_id = c.conversation_id
                     AND r.sender_type = 'user'
                     AND r.message_index > bs.r1_message_index
                     AND (bs.r2_message_index IS NULL OR r.message_index < bs.r2_message_index)
               ) OR EXISTS (
                   SELECT 1
                   FROM im_messages r
                   WHERE r.conversation_id = c.conversation_id
                     AND r.sender_type = 'user'
                     AND r.message_index > COALESCE((
                         SELECT MIN(m.message_index)
                         FROM im_messages m
                         WHERE m.conversation_id = c.conversation_id
                           AND UPPER(COALESCE(m.template_name, '')) = 'R1'
                           AND COALESCE(m.sender_type, '') IN ('bot', 'agent_manual', 'agent_template', 'system')
                     ), 999999999)
               ) OR (
                   SELECT COUNT(*)
                   FROM im_conversion_events e
                   WHERE e.conversation_id = c.conversation_id
                     AND e.event_name = 'user_replied_after_step'
               ) >= 1 THEN 1 ELSE 0 END) AS r1_user_replied,
               SUM(CASE WHEN bs.r2_message_index IS NOT NULL OR EXISTS (
                   SELECT 1 FROM im_messages m
                   WHERE m.conversation_id = c.conversation_id
                     AND UPPER(COALESCE(m.template_name, '')) = 'R2'
                     AND COALESCE(m.sender_type, '') IN ('bot', 'agent_manual', 'agent_template', 'system')
               ) OR (
                   SELECT COUNT(*)
                   FROM im_conversion_events e
                   WHERE e.conversation_id = c.conversation_id
                     AND e.event_name = 'message_sent'
                     AND e.event_source = 'im_bot_flow_events'
               ) >= 2 THEN 1 ELSE 0 END) AS r2_sent_messages,
               SUM(CASE WHEN bs.r2_message_index IS NOT NULL AND EXISTS (
                   SELECT 1
                   FROM im_messages r
                   WHERE r.conversation_id = c.conversation_id
                     AND r.sender_type = 'user'
                     AND r.message_index > bs.r2_message_index
                     AND (bs.r3_message_index IS NULL OR r.message_index < bs.r3_message_index)
               ) OR EXISTS (
                   SELECT 1
                   FROM im_messages r
                   WHERE r.conversation_id = c.conversation_id
                     AND r.sender_type = 'user'
                     AND r.message_index > COALESCE((
                         SELECT MIN(m.message_index)
                         FROM im_messages m
                         WHERE m.conversation_id = c.conversation_id
                           AND UPPER(COALESCE(m.template_name, '')) = 'R2'
                           AND COALESCE(m.sender_type, '') IN ('bot', 'agent_manual', 'agent_template', 'system')
                     ), 999999999)
               ) OR (
                   SELECT COUNT(*)
                   FROM im_conversion_events e
                   WHERE e.conversation_id = c.conversation_id
                     AND e.event_name = 'user_replied_after_step'
               ) >= 2 THEN 1 ELSE 0 END) AS r2_user_replied,
               SUM(CASE WHEN bs.r3_message_index IS NOT NULL OR EXISTS (
                   SELECT 1 FROM im_messages m
                   WHERE m.conversation_id = c.conversation_id
                     AND UPPER(COALESCE(m.template_name, '')) = 'R3'
                     AND COALESCE(m.sender_type, '') IN ('bot', 'agent_manual', 'agent_template', 'system')
               ) OR (
                   SELECT COUNT(*)
                   FROM im_conversion_events e
                   WHERE e.conversation_id = c.conversation_id
                     AND e.event_name = 'message_sent'
                     AND e.event_source = 'im_bot_flow_events'
               ) >= 3 THEN 1 ELSE 0 END) AS r3_sent_messages,
               SUM(CASE WHEN bs.r3_message_index IS NOT NULL AND EXISTS (
                   SELECT 1
                   FROM im_messages r
                   WHERE r.conversation_id = c.conversation_id
                     AND r.sender_type = 'user'
                     AND r.message_index > bs.r3_message_index
               ) OR EXISTS (
                   SELECT 1
                   FROM im_messages r
                   WHERE r.conversation_id = c.conversation_id
                     AND r.sender_type = 'user'
                     AND r.message_index > COALESCE((
                         SELECT MIN(m.message_index)
                         FROM im_messages m
                         WHERE m.conversation_id = c.conversation_id
                           AND UPPER(COALESCE(m.template_name, '')) = 'R3'
                           AND COALESCE(m.sender_type, '') IN ('bot', 'agent_manual', 'agent_template', 'system')
                     ), 999999999)
               ) OR (
                   SELECT COUNT(*)
                   FROM im_conversion_events e
                   WHERE e.conversation_id = c.conversation_id
                     AND e.event_name = 'user_replied_after_step'
               ) >= 3 THEN 1 ELSE 0 END) AS r3_user_replied,
               SUM(CASE WHEN bt.conversation_id IS NOT NULL
                        THEN CASE WHEN bt.link_sent_at != '' OR EXISTS (
                            SELECT 1 FROM im_conversion_events e
                            WHERE e.conversation_id = c.conversation_id
                              AND e.event_name = 'link_sent'
                        ) OR EXISTS (
                            SELECT 1 FROM im_messages m
                            WHERE m.conversation_id = c.conversation_id
                              AND COALESCE(m.sender_type, '') IN ('bot', 'agent_manual', 'agent_template', 'system')
                              AND COALESCE(m.has_link, 0) = 1
                        ) OR EXISTS (
                            SELECT 1 FROM im_conversion_events e
                            WHERE e.conversation_id = c.conversation_id
                              AND e.event_name IN ('link_clicked', 'guild_bind_request', 'bind_result_success', 'real_join_succeeded', 'crm_succeeded')
                        ) THEN 1 ELSE 0 END
                        ELSE CASE WHEN EXISTS (
                            SELECT 1 FROM im_conversion_events e
                            WHERE e.conversation_id = c.conversation_id
                              AND e.event_name = 'link_sent'
                        ) OR EXISTS (
                            SELECT 1 FROM im_messages m
                            WHERE m.conversation_id = c.conversation_id
                              AND COALESCE(m.sender_type, '') IN ('bot', 'agent_manual', 'agent_template', 'system')
                              AND COALESCE(m.has_link, 0) = 1
                        ) OR EXISTS (
                            SELECT 1 FROM im_conversion_events e
                            WHERE e.conversation_id = c.conversation_id
                              AND e.event_name IN ('link_clicked', 'guild_bind_request', 'bind_result_success', 'real_join_succeeded', 'crm_succeeded')
                        ) THEN 1 ELSE 0 END
                   END) AS link_sent,
               SUM(CASE WHEN NOT (
                            (bt.conversation_id IS NOT NULL AND bt.link_sent_at != '')
                            OR EXISTS (
                                SELECT 1 FROM im_conversion_events e
                                WHERE e.conversation_id = c.conversation_id
                                  AND e.event_name = 'link_sent'
                            )
                            OR EXISTS (
                                SELECT 1 FROM im_messages m
                                WHERE m.conversation_id = c.conversation_id
                                  AND COALESCE(m.sender_type, '') IN ('bot', 'agent_manual', 'agent_template', 'system')
                                  AND COALESCE(m.has_link, 0) = 1
                            )
                        ) AND EXISTS (
                            SELECT 1 FROM im_conversion_events e
                            WHERE e.conversation_id = c.conversation_id
                              AND e.event_name IN ('link_clicked', 'guild_bind_request', 'bind_result_success', 'real_join_succeeded', 'crm_succeeded')
                        ) THEN 1 ELSE 0 END) AS link_sent_inferred,
               SUM(CASE WHEN bt.conversation_id IS NOT NULL
                        THEN CASE WHEN bt.link_clicked_at != '' THEN 1 ELSE 0 END
                        ELSE CASE WHEN EXISTS (
                   SELECT 1 FROM im_conversion_events e
                   WHERE e.conversation_id = c.conversation_id
                     AND e.event_name = 'link_clicked'
                        ) THEN 1 ELSE 0 END
                   END) AS link_clicked,
               SUM(CASE WHEN bt.conversation_id IS NOT NULL
                        THEN CASE WHEN bt.r1_sent_at != '' THEN 1 ELSE 0 END
                        ELSE CASE WHEN EXISTS (
                            SELECT 1 FROM im_conversion_events e
                            WHERE e.conversation_id = c.conversation_id
                              AND e.event_name = 'message_sent'
                              AND e.event_source = 'im_bot_flow_events'
                        ) THEN 1 ELSE 0 END
                   END) AS r1_touched,
               SUM(CASE WHEN bt.conversation_id IS NOT NULL
                        THEN CASE WHEN bt.r1_after_auto_apply_within_60 = 1 THEN 1 ELSE 0 END
                        ELSE CASE WHEN EXISTS (
                   SELECT 1
                   FROM im_conversion_events auto_evt
                       JOIN im_conversion_events r1_evt
                         ON r1_evt.conversation_id = auto_evt.conversation_id
                        AND r1_evt.event_name = 'message_sent'
                        AND r1_evt.event_source = 'im_bot_flow_events'
                    AND r1_evt.event_time >= auto_evt.event_time
                    AND ((julianday(r1_evt.event_time) - julianday(auto_evt.event_time)) * 86400.0) <= 60.0
                   WHERE auto_evt.conversation_id = c.conversation_id
                     AND auto_evt.event_name = 'auto_apply_message_sent'
                        ) THEN 1 ELSE 0 END
                   END) AS r1_after_auto_apply_within_60s,
               SUM(CASE WHEN bt.conversation_id IS NOT NULL
                        THEN CASE WHEN bt.guild_bind_request_at != '' THEN 1 ELSE 0 END
                        ELSE CASE WHEN EXISTS (
                   SELECT 1 FROM im_conversion_events e
                   WHERE e.conversation_id = c.conversation_id
                     AND e.event_name = 'guild_bind_request'
                        ) THEN 1 ELSE 0 END
                   END) AS bind_requested,
                   SUM(CASE WHEN d.final_outcome IN ('success', 'joined', 'crm_succeeded')
                              AND EXISTS (
                                  SELECT 1 FROM im_conversion_events join_evt
                                  WHERE join_evt.conversation_id = c.conversation_id
                                    AND join_evt.event_name IN ('real_join_succeeded', 'crm_succeeded', 'bind_result_success')
                              )
                              AND EXISTS (
                                  SELECT 1 FROM im_messages m
                                  WHERE m.conversation_id = c.conversation_id
                                    AND COALESCE(m.sender_type, '') IN ('bot', 'agent_manual', 'agent_template', 'system')
                                    AND COALESCE(m.has_link, 0) = 1
                                    AND COALESCE(NULLIF(m.message_at, ''), m.created_at) >= (
                                        SELECT MIN(join_evt.event_time)
                                        FROM im_conversion_events join_evt
                                        WHERE join_evt.conversation_id = c.conversation_id
                                          AND join_evt.event_name IN ('real_join_succeeded', 'crm_succeeded', 'bind_result_success')
                                    )
                              )
                       THEN 1 ELSE 0 END) AS ops_group_link_sent,
                   SUM(CASE WHEN d.final_outcome IN ('success', 'joined', 'crm_succeeded')
                              AND EXISTS (
                                  SELECT 1 FROM im_conversion_events join_evt
                                  WHERE join_evt.conversation_id = c.conversation_id
                                    AND join_evt.event_name IN ('real_join_succeeded', 'crm_succeeded', 'bind_result_success')
                              )
                              AND EXISTS (
                                  SELECT 1 FROM im_messages m
                                  WHERE m.conversation_id = c.conversation_id
                                    AND COALESCE(m.sender_type, '') IN ('bot', 'agent_manual', 'agent_template', 'system')
                                    AND COALESCE(m.has_link, 0) = 1
                                    AND COALESCE(NULLIF(m.message_at, ''), m.created_at) >= (
                                        SELECT MIN(join_evt.event_time)
                                        FROM im_conversion_events join_evt
                                        WHERE join_evt.conversation_id = c.conversation_id
                                          AND join_evt.event_name IN ('real_join_succeeded', 'crm_succeeded', 'bind_result_success')
                                    )
                              )
                              AND EXISTS (
                                  SELECT 1 FROM im_conversion_events e
                                  WHERE e.conversation_id = c.conversation_id
                                    AND e.event_name = 'ops_group_link_clicked'
                              )
                       THEN 1 ELSE 0 END) AS ops_group_link_clicked
        FROM im_conversations c
        JOIN latest_diagnoses d ON d.conversation_id = c.conversation_id
        LEFT JOIN im_bot_timing_facts bt ON bt.conversation_id = c.conversation_id
        LEFT JOIN bot_steps bs ON bs.conversation_id = c.conversation_id
        WHERE (? = '' OR c.ad_id = ?)
          AND """ + _region_where_sql('c') + """
        GROUP BY CASE
                     WHEN COALESCE(NULLIF(c.handoff_type, ''), 'unknown') = 'human_assisted'
                     THEN 'human_assisted'
                     WHEN COALESCE(NULLIF(c.handoff_type, ''), 'unknown') IN ('non_human', 'bot_automated')
                     THEN 'non_human'
                     ELSE 'unclassified'
                 END
        ORDER BY sample_conversations DESC
        """,
        (diagnosis_run_id, diagnosis_run_id, ad_id, ad_id, *_region_params(normalized_region)),
    ).fetchall()
    segment_summary = []
    for row in segment_rows:
        sample = int(row['sample_conversations'] or 0)
        success = int(row['successful_conversations'] or 0)
        responded = int(row['within_60s'] or 0) + int(row['over_60s'] or 0)
        segment_summary.append({
            'handoff_type': row['handoff_type'],
            'sample_conversations': sample,
            'successful_conversations': success,
            'lost_conversations': int(row['lost_conversations'] or 0),
            'join_rate': round(success / sample, 4) if sample else 0.0,
            'avg_first_response_seconds': round(float(row['avg_first_response_seconds'] or 0.0), 2),
            'over_60s': int(row['over_60s'] or 0),
            'within_60s_rate': round(int(row['within_60s'] or 0) / responded, 4) if responded else 0.0,
            'bot_timing_rows': int(row['bot_timing_rows'] or 0),
            'bot_timing_coverage_rate': round(int(row['bot_timing_rows'] or 0) / sample, 4) if sample else 0.0,
            'handoff_entry': int(row['handoff_entry'] or 0),
            'handoff_entry_rate': round(int(row['handoff_entry'] or 0) / sample, 4) if sample else 0.0,
            'system_touched': int(row['system_touched'] or 0),
            'system_touch_rate': round(int(row['system_touched'] or 0) / sample, 4) if sample else 0.0,
            'auto_apply_sent': int(row['auto_apply_sent'] or 0),
            'auto_apply_rate': round(int(row['auto_apply_sent'] or 0) / sample, 4) if sample else 0.0,
            'r1_sent_messages': int(row['r1_sent_messages'] or 0),
            'r1_sent_rate': round(int(row['r1_sent_messages'] or 0) / sample, 4) if sample else 0.0,
            'r1_user_replied': int(row['r1_user_replied'] or 0),
            'r1_user_reply_rate': round(int(row['r1_user_replied'] or 0) / int(row['r1_sent_messages'] or 0), 4) if int(row['r1_sent_messages'] or 0) else 0.0,
            'r2_sent_messages': int(row['r2_sent_messages'] or 0),
            'r2_sent_rate': round(int(row['r2_sent_messages'] or 0) / int(row['r1_user_replied'] or 0), 4) if int(row['r1_user_replied'] or 0) else 0.0,
            'r2_user_replied': int(row['r2_user_replied'] or 0),
            'r2_user_reply_rate': round(int(row['r2_user_replied'] or 0) / int(row['r2_sent_messages'] or 0), 4) if int(row['r2_sent_messages'] or 0) else 0.0,
            'r3_sent_messages': int(row['r3_sent_messages'] or 0),
            'r3_sent_rate': round(int(row['r3_sent_messages'] or 0) / int(row['r2_user_replied'] or 0), 4) if int(row['r2_user_replied'] or 0) else 0.0,
            'r3_user_replied': int(row['r3_user_replied'] or 0),
            'r3_user_reply_rate': round(int(row['r3_user_replied'] or 0) / int(row['r3_sent_messages'] or 0), 4) if int(row['r3_sent_messages'] or 0) else 0.0,
            'r1_touched': int(row['r1_touched'] or 0),
            'r1_touch_rate': round(int(row['r1_touched'] or 0) / sample, 4) if sample else 0.0,
            'r1_after_auto_apply_within_60s': int(row['r1_after_auto_apply_within_60s'] or 0),
            'r1_after_auto_apply_60s_rate': round(int(row['r1_after_auto_apply_within_60s'] or 0) / int(row['auto_apply_sent'] or 0), 4) if int(row['auto_apply_sent'] or 0) else 0.0,
            'link_sent': int(row['link_sent'] or 0),
            'link_sent_rate': round(int(row['link_sent'] or 0) / sample, 4) if sample else 0.0,
            'link_sent_inferred': int(row['link_sent_inferred'] or 0),
            'link_sent_inferred_rate': round(int(row['link_sent_inferred'] or 0) / int(row['link_sent'] or 0), 4) if int(row['link_sent'] or 0) else 0.0,
            'link_clicked': int(row['link_clicked'] or 0),
            'link_click_rate': round(int(row['link_clicked'] or 0) / int(row['link_sent'] or 0), 4) if int(row['link_sent'] or 0) else 0.0,
                'bind_requested': int(row['bind_requested'] or 0),
                'bind_request_rate': round(int(row['bind_requested'] or 0) / int(row['link_clicked'] or 0), 4) if int(row['link_clicked'] or 0) else 0.0,
                'ops_group_link_sent': int(row['ops_group_link_sent'] or 0),
                'ops_group_link_sent_rate': round(int(row['ops_group_link_sent'] or 0) / success, 4) if success else 0.0,
                'ops_group_link_clicked': int(row['ops_group_link_clicked'] or 0),
                'ops_group_link_click_rate': round(int(row['ops_group_link_clicked'] or 0) / int(row['ops_group_link_sent'] or 0), 4) if int(row['ops_group_link_sent'] or 0) else 0.0,
            })
    issue_rows = conn.execute(
        """
        WITH latest_diagnoses AS (
            SELECT d.*
            FROM im_conversation_diagnoses d
            JOIN (
                SELECT MAX(rowid) AS row_id
                FROM im_conversation_diagnoses
                WHERE (? = '' OR diagnosis_run_id = ?)
                GROUP BY diagnosis_run_id, conversation_id
            ) latest ON latest.row_id = d.rowid
        )
        SELECT d.primary_diagnosis,
               d.action_type,
               CASE
                   WHEN COALESCE(NULLIF(c.handoff_type, ''), 'unknown') = 'human_assisted'
                   THEN 'human_assisted'
                   WHEN COALESCE(NULLIF(c.handoff_type, ''), 'unknown') IN ('non_human', 'bot_automated')
                   THEN 'non_human'
                   ELSE 'unclassified'
               END AS handoff_type,
               COUNT(*) AS affected_conversations
        FROM latest_diagnoses d
        JOIN im_conversations c ON c.conversation_id = d.conversation_id
        WHERE (? = '' OR c.ad_id = ?)
          AND """ + _region_where_sql('c') + """
          AND d.primary_diagnosis != 'success_sample'
        GROUP BY d.primary_diagnosis, d.action_type,
                 CASE
                     WHEN COALESCE(NULLIF(c.handoff_type, ''), 'unknown') = 'human_assisted'
                     THEN 'human_assisted'
                     WHEN COALESCE(NULLIF(c.handoff_type, ''), 'unknown') IN ('non_human', 'bot_automated')
                     THEN 'non_human'
                     ELSE 'unclassified'
                 END
        ORDER BY affected_conversations DESC
        """,
        (diagnosis_run_id, diagnosis_run_id, ad_id, ad_id, *_region_params(normalized_region)),
    ).fetchall()
    issue_map: Dict[str, Dict[str, Any]] = {}
    for row in issue_rows:
        diagnosis = str(row['primary_diagnosis'] or '')
        item = issue_map.setdefault(diagnosis, {
            'diagnosis': diagnosis,
            'diagnosis_zh': DIAGNOSIS_LABELS.get(diagnosis, diagnosis),
            'affected_conversations': 0,
            'share': 0.0,
            'action_type': str(row['action_type'] or ''),
            'human_assisted': 0,
            'non_human': 0,
            'unclassified': 0,
        })
        affected = int(row['affected_conversations'] or 0)
        item['affected_conversations'] += affected
        if str(row['handoff_type'] or '') == 'human_assisted':
            item['human_assisted'] += affected
        elif str(row['handoff_type'] or '') == 'non_human':
            item['non_human'] += affected
        else:
            item['unclassified'] += affected
        if not item.get('action_type') and row['action_type']:
            item['action_type'] = str(row['action_type'])
    top_issues = sorted(issue_map.values(), key=lambda item: int(item.get('affected_conversations') or 0), reverse=True)[:8]
    for item in top_issues:
        item['share'] = round(int(item.get('affected_conversations') or 0) / totals['sample_conversations'], 4) if totals['sample_conversations'] else 0.0
    funnel_rows = conn.execute(
        """
        WITH latest_diagnoses AS (
            SELECT d.*
            FROM im_conversation_diagnoses d
            JOIN (
                SELECT MAX(rowid) AS row_id
                FROM im_conversation_diagnoses
                WHERE (? = '' OR diagnosis_run_id = ?)
                GROUP BY diagnosis_run_id, conversation_id
            ) latest ON latest.row_id = d.rowid
        )
        SELECT d.dropoff_stage,
               d.primary_diagnosis,
               d.action_type,
               c.country,
               CASE
                   WHEN COALESCE(NULLIF(c.handoff_type, ''), 'unknown') = 'human_assisted'
                   THEN 'human_assisted'
                   WHEN COALESCE(NULLIF(c.handoff_type, ''), 'unknown') IN ('non_human', 'bot_automated')
                   THEN 'non_human'
                   ELSE 'unclassified'
               END AS handoff_type,
               COUNT(*) AS affected_conversations
        FROM latest_diagnoses d
        JOIN im_conversations c ON c.conversation_id = d.conversation_id
        WHERE (? = '' OR c.ad_id = ?)
          AND """ + _region_where_sql('c') + """
          AND d.primary_diagnosis != 'success_sample'
        GROUP BY d.dropoff_stage, d.primary_diagnosis, d.action_type, c.country,
                 CASE
                     WHEN COALESCE(NULLIF(c.handoff_type, ''), 'unknown') = 'human_assisted'
                     THEN 'human_assisted'
                     WHEN COALESCE(NULLIF(c.handoff_type, ''), 'unknown') IN ('non_human', 'bot_automated')
                     THEN 'non_human'
                     ELSE 'unclassified'
                 END
        ORDER BY affected_conversations DESC
        """,
        (diagnosis_run_id, diagnosis_run_id, ad_id, ad_id, *_region_params(normalized_region)),
    ).fetchall()
    funnel_map: Dict[str, Dict[str, Any]] = {}
    for row in funnel_rows:
        stage = str(row['dropoff_stage'] or 'unknown')
        stage_item = funnel_map.setdefault(stage, {
            'dropoff_stage': stage,
            'stage_label': _funnel_stage_label(stage),
            'affected_conversations': 0,
            'share': 0.0,
            'top_diagnoses': {},
            'countries': {},
            'human_assisted': 0,
            'non_human': 0,
            'unclassified': 0,
        })
        affected = int(row['affected_conversations'] or 0)
        diagnosis = str(row['primary_diagnosis'] or '')
        country = str(row['country'] or '-')
        handoff = str(row['handoff_type'] or 'unclassified')
        stage_item['affected_conversations'] += affected
        if handoff == 'human_assisted':
            stage_item['human_assisted'] += affected
        elif handoff == 'non_human':
            stage_item['non_human'] += affected
        else:
            stage_item['unclassified'] += affected
        country_item = stage_item['countries'].setdefault(country, 0)
        stage_item['countries'][country] = country_item + affected
        diag_item = stage_item['top_diagnoses'].setdefault(diagnosis, {
            'diagnosis': diagnosis,
            'diagnosis_zh': DIAGNOSIS_LABELS.get(diagnosis, diagnosis),
            'affected_conversations': 0,
            'action_type': str(row['action_type'] or ''),
        })
        diag_item['affected_conversations'] += affected
        if not diag_item.get('action_type') and row['action_type']:
            diag_item['action_type'] = str(row['action_type'])
    funnel_insights = []
    for item in funnel_map.values():
        diagnoses = sorted(item.pop('top_diagnoses').values(), key=lambda diag: int(diag.get('affected_conversations') or 0), reverse=True)
        countries = sorted(
            ({'country': country, 'affected_conversations': count} for country, count in item.pop('countries').items()),
            key=lambda country_row: int(country_row.get('affected_conversations') or 0),
            reverse=True,
        )
        primary_diagnosis = diagnoses[0]['diagnosis'] if diagnoses else ''
        item['top_diagnoses'] = diagnoses[:3]
        item['top_countries'] = countries[:3]
        item['share'] = round(int(item.get('affected_conversations') or 0) / totals['lost_conversations'], 4) if totals['lost_conversations'] else 0.0
        item['recommended_action'] = _funnel_stage_action(str(item.get('dropoff_stage') or ''), str(primary_diagnosis or ''))
        funnel_insights.append(item)
    funnel_insights.sort(key=lambda item: int(item.get('affected_conversations') or 0), reverse=True)
    funnel_insights = funnel_insights[:8]
    external_app_rows = conn.execute(
        """
        WITH latest_diagnoses AS (
            SELECT d.*
            FROM im_conversation_diagnoses d
            JOIN (
                SELECT MAX(rowid) AS row_id
                FROM im_conversation_diagnoses
                WHERE (? = '' OR diagnosis_run_id = ?)
                GROUP BY diagnosis_run_id, conversation_id
            ) latest ON latest.row_id = d.rowid
        )
        SELECT DISTINCT TRIM(COALESCE(c.external_app, '')) AS external_app
        FROM im_conversations c
        JOIN latest_diagnoses d ON d.conversation_id = c.conversation_id
        WHERE TRIM(COALESCE(c.external_app, '')) != ''
          AND (? = '' OR c.ad_id = ?)
          AND """ + _region_where_sql('c') + """
        ORDER BY external_app
        """,
        (diagnosis_run_id, diagnosis_run_id, ad_id, ad_id, *_region_params(normalized_region)),
    ).fetchall()
    external_apps = [str(row['external_app'] or '') for row in external_app_rows]
    chain_steps = _im_chain_steps(
        totals=totals,
        segments=segment_summary,
        funnel_insights=funnel_insights,
        external_apps=external_apps,
    )
    # R101/R104/R105 are an independent UTC fact window.  Do not mix their
    # official UV denominators with the Asia/Shanghai conversation funnel.
    chain_steps = [
        step for step in chain_steps
        if step.get('step_key') not in {'ops_group_link_sent', 'ops_group_link_clicked'}
    ]
    result_window_start, result_window_end = _diagnosis_run_date_window(conn, diagnosis_run_id)
    if result_window_start and result_window_end:
        result_message_facts = im_result_message_summary(
            conn,
            start_date_utc=result_window_start,
            end_date_utc=result_window_end,
            region=normalized_region,
        )
    else:
        result_message_facts = {
            'coverage_status': 'missing',
            'data_quality_note': '当前诊断运行没有可映射的 UTC 日期窗口。',
            'timezone': 'UTC+0',
            'start_date_utc': '',
            'end_date_utc': '',
            'step_coverage': [
                {'step_code': code, 'coverage_status': 'missing'}
                for code in ('R101', 'R104', 'R105')
            ],
            'data_maturity_status': 'unknown',
            'metrics': {},
        }
    chain_steps.extend(result_message_chain_steps(result_message_facts))
    suggestion_limit = max(1, int(script_limit if script_limit is not None else (limit or 20)))
    suggestion_conditions = [
        "COALESCE(suggested_script_source, '') = ?",
        "COALESCE(suggested_script_translation_source, '') = ?",
        "COALESCE(old_script_summary_translation_source, '') = ?",
        "COALESCE(old_script_summary_interpretation_source, '') = ?",
        "TRIM(COALESCE(suggested_script, '')) != ''",
        "TRIM(COALESCE(suggested_script_translation_zh, '')) != ''",
        "TRIM(COALESCE(old_script_summary, '')) != ''",
        "TRIM(COALESCE(old_script_summary_translation_zh, '')) != ''",
        "TRIM(COALESCE(old_script_summary_interpretation_zh, '')) != ''",
    ]
    suggestion_params: List[Any] = [
        HERMES_LLM_PROVIDER_MODE,
        HERMES_LLM_PROVIDER_MODE,
        HERMES_LLM_PROVIDER_MODE,
        HERMES_LLM_PROVIDER_MODE,
    ]
    if normalized_region == 'brazil':
        suggestion_conditions.append("LOWER(COALESCE(country, '')) IN ('brazil', 'br')")
    elif normalized_region == 'indonesia':
        suggestion_conditions.append("LOWER(COALESCE(country, '')) IN ('indonesia', 'id')")
    elif normalized_region == 'spanish':
        suggestion_conditions.append("(LOWER(COALESCE(country, '')) IN ('mexico', 'venezuela', 'colombia', 'chile', 'peru', 'ecuador', 'argentina', 'bolivia', 'paraguay', 'uruguay', 'mex', 've', 'co', 'cl', 'pe', 'ec', 'ar', 'bo', 'py', 'uy') OR LOWER(COALESCE(language, '')) IN ('es', 'es-es', 'es-mx', 'es-co', 'es-cl'))")
    suggestion_where = "WHERE " + " AND ".join(suggestion_conditions)
    suggestions = [dict(row) for row in conn.execute(
        """
        SELECT *
        FROM im_script_suggestions
        """ + suggestion_where + """
        ORDER BY
            CASE approval_status WHEN 'approved' THEN 0 WHEN 'draft' THEN 1 WHEN 'pending_review' THEN 2 ELSE 3 END,
            source_conversation_count DESC,
            updated_at DESC
        LIMIT ?
        """,
        (*suggestion_params, max(suggestion_limit * 6, 30)),
    ).fetchall()]
    suggestions = [
        row for row in suggestions
        if not _contains_cjk(str(row.get('old_script_summary') or ''))
        and not _has_invalid_linky_positioning(
            row.get('suggested_script'),
            row.get('suggested_script_translation_zh'),
            row.get('old_script_summary_translation_zh'),
            row.get('old_script_summary_interpretation_zh'),
        )
    ]
    for row in suggestions:
        row['language'] = _script_language(row.get('country'), row.get('language'))
        if not str(row.get('funnel_stage') or '').strip():
            inferred_plan = _script_experiment_plan(
                diagnosis_type=str(row.get('diagnosis_type') or ''),
                country=row.get('country'),
                language=row.get('language'),
                source_count=int(row.get('source_conversation_count') or 0),
            )
            row['funnel_stage'] = inferred_plan.get('funnel_stage') or ''
            row['target_metric'] = row.get('target_metric') or inferred_plan.get('target_metric') or ''
            row['experiment_hypothesis'] = row.get('experiment_hypothesis') or inferred_plan.get('experiment_hypothesis') or ''
            if row.get('funnel_stage'):
                conn.execute(
                    """
                    UPDATE im_script_suggestions
                    SET funnel_stage = CASE WHEN funnel_stage = '' THEN ? ELSE funnel_stage END,
                        target_metric = CASE WHEN target_metric = '' THEN ? ELSE target_metric END,
                        experiment_hypothesis = CASE WHEN experiment_hypothesis = '' THEN ? ELSE experiment_hypothesis END,
                        updated_at = ?
                    WHERE script_suggestion_id = ?
                    """,
                    (
                        row.get('funnel_stage') or '',
                        row.get('target_metric') or '',
                        row.get('experiment_hypothesis') or '',
                        _utc_now(),
                        row.get('script_suggestion_id'),
                    ),
                )
        row['risk_tags'] = _loads(row.pop('risk_tags_json', '[]'), [])
        row['risk_score'] = _normalize_script_risk_score(row.pop('risk_score_json', '{}'))
        row['max_risk_score'] = _max_script_risk_score(row.get('risk_score'))
        row['launch_decision'] = _normalize_launch_decision(row.get('launch_decision'), row.get('risk_score'))
        row['current_state'] = _normalize_funnel_state(row.get('current_state'), row.get('funnel_stage'))
        row['user_concern_type'] = _normalize_user_concern_type(row.get('user_concern_type'), row.get('scenario'))
        row['evidence_summary'] = _loads(row.pop('evidence_summary_json', '{}'), {})
        row['experiment_design'] = _loads(row.pop('experiment_design_json', '{}'), {})
        row['funnel_stage_label'] = _funnel_stage_label(str(row.get('funnel_stage') or ''))
        exp_row = conn.execute(
            """
            SELECT *
            FROM im_script_experiments
            WHERE script_suggestion_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (row['script_suggestion_id'],),
        ).fetchone()
        row['experiment'] = _script_experiment_from_row(exp_row) if exp_row else None
        row['priority_score'] = _script_priority_score(
            source_count=int(row.get('source_conversation_count') or 0),
            diagnosis_type=str(row.get('diagnosis_type') or ''),
            funnel_stage=str(row.get('funnel_stage') or ''),
            experiment_status=str((row.get('experiment') or {}).get('status') or row.get('experiment_status') or ''),
        )
        row['funnel_stage_order'] = _script_funnel_stage_order(row.get('funnel_stage'))
        row['operator_next_step'] = _script_operator_next_step(row)
    suggestions = _dedupe_script_suggestions(suggestions)
    for row in suggestions:
        if _contains_cjk(row.get('suggested_script')):
            row['suggested_script'] = _localized_non_cjk_script(
                str(row.get('diagnosis_type') or ''),
                row.get('country'),
                row.get('language'),
                row.get('suggested_script'),
            )
    for row in suggestions:
        row['funnel_stage_order'] = _script_funnel_stage_order(row.get('funnel_stage'))
    suggestions.sort(key=lambda item: (
        int(item.get('funnel_stage_order') or 999),
        -int(item.get('priority_score') or 0),
        -int(item.get('source_conversation_count') or 0),
        str(item.get('updated_at') or ''),
    ))
    suggestions = _balanced_script_suggestions(
        suggestions,
        suggestion_limit,
        per_country_cap=None if normalized_region else 8,
    )
    task_rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM im_llm_diagnosis_tasks
        WHERE (? = '' OR diagnosis_run_id = ?)
        GROUP BY status
        """,
        (diagnosis_run_id, diagnosis_run_id),
    ).fetchall()
    llm_tasks = {str(row['status']): int(row['count'] or 0) for row in task_rows}
    return {
        'ok': True,
        'diagnosis_run_id': diagnosis_run_id,
        'region': normalized_region or 'all',
        'region_label': _region_label(normalized_region),
        'external_apps': external_apps,
        'taxonomy_version': TAXONOMY_VERSION,
        'summary': totals,
        'segments': segment_summary,
        'chain_steps': chain_steps,
        'result_message_facts': result_message_facts,
        'top_issues': top_issues,
        'funnel_insights': funnel_insights,
        'llm_tasks': llm_tasks,
        'aggregates': rows,
        'script_suggestions': suggestions,
    }


def im_conversations_payload(conn: sqlite3.Connection, *, diagnosis_run_id: str = '', ad_id: str = '', diagnosis: str = '', dropoff_stage: str = '', region: str = '', limit: int = 20) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    conn.row_factory = sqlite3.Row
    if not diagnosis_run_id:
        diagnosis_run_id = _latest_diagnosis_run_id(conn)
    params: List[Any] = []
    where = []
    if diagnosis_run_id:
        where.append('d.diagnosis_run_id = ?')
        params.append(diagnosis_run_id)
    if ad_id:
        where.append('c.ad_id = ?')
        params.append(ad_id)
    if diagnosis:
        where.append('d.primary_diagnosis = ?')
        params.append(diagnosis)
    if dropoff_stage:
        where.append('d.dropoff_stage = ?')
        params.append(dropoff_stage)
    normalized_region = _region_params(region)[0]
    if normalized_region:
        where.append(_region_where_sql('c'))
        params.extend(_region_params(normalized_region))
    clause = 'WHERE ' + ' AND '.join(where) if where else ''
    rows = [dict(row) for row in conn.execute(
        f"""
        SELECT c.conversation_id, c.country, c.language, c.media_source, c.campaign_name, c.adset_name, c.ad_name,
               c.handoff_type,
               c.first_response_seconds, c.final_outcome, d.dropoff_stage, d.primary_diagnosis,
               d.user_objection, d.agent_issue, d.evidence_json, d.recommended_replacement_json,
               d.confidence, d.human_review_status
        FROM im_conversations c
        JOIN im_conversation_diagnoses d ON d.conversation_id = c.conversation_id
        {clause}
        ORDER BY d.created_at DESC
        LIMIT ?
        """,
        (*params, int(limit or 20)),
    ).fetchall()]
    for row in rows:
        row['primary_diagnosis_zh'] = DIAGNOSIS_LABELS.get(str(row.get('primary_diagnosis') or ''), str(row.get('primary_diagnosis') or ''))
        row['evidence'] = _loads(row.pop('evidence_json', '[]'), [])
        row['recommended_replacement'] = _loads(row.pop('recommended_replacement_json', '{}'), {})
        task_row = conn.execute(
            """
            SELECT * FROM im_llm_diagnosis_tasks
            WHERE conversation_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (row['conversation_id'],),
        ).fetchone()
        row['latest_llm_task'] = _diagnosis_task_from_row(task_row) if task_row else None
    return {'ok': True, 'diagnosis_run_id': diagnosis_run_id, 'conversations': rows}


def im_conversation_detail(conn: sqlite3.Connection, conversation_id: str) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM im_conversations WHERE conversation_id = ?', (conversation_id,)).fetchone()
    if not row:
        return {'ok': False, 'detail': 'conversation_not_found'}
    diagnosis = conn.execute(
        'SELECT * FROM im_conversation_diagnoses WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1',
        (conversation_id,),
    ).fetchone()
    payload = {
        'ok': True,
        'conversation': dict(row),
        'messages': _messages_for(conn, conversation_id),
        'events': _events_for(conn, conversation_id),
        'diagnosis': dict(diagnosis) if diagnosis else None,
    }
    task_row = conn.execute(
        """
        SELECT * FROM im_llm_diagnosis_tasks
        WHERE conversation_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()
    payload['latest_llm_task'] = _diagnosis_task_from_row(task_row) if task_row else None
    if payload['diagnosis']:
        payload['diagnosis']['secondary_diagnoses'] = _loads(payload['diagnosis'].pop('secondary_diagnoses_json', '[]'), [])
        payload['diagnosis']['evidence'] = _loads(payload['diagnosis'].pop('evidence_json', '[]'), [])
        payload['diagnosis']['recommended_replacement'] = _loads(payload['diagnosis'].pop('recommended_replacement_json', '{}'), {})
        payload['diagnosis']['primary_diagnosis_zh'] = DIAGNOSIS_LABELS.get(payload['diagnosis'].get('primary_diagnosis'), payload['diagnosis'].get('primary_diagnosis'))
    return payload


def _safe_task_error(value: Any, limit: int = 500) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    text = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(token|secret|password|authorization)["\']?\s*[:=]\s*["\']?[^"\'\s,}]+', r'\1=[REDACTED]', text)
    return text[: max(1, int(limit or 500))]


def _diagnosis_task_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        'task_id': row['task_id'],
        'conversation_id': row['conversation_id'],
        'diagnosis_run_id': row['diagnosis_run_id'],
        'provider_mode': row['provider_mode'],
        'status': row['status'],
        'prompt_version': row['prompt_version'],
        'taxonomy_version': row['taxonomy_version'],
        'payload': _loads(row['payload_json'], {}),
        'result': _loads(row['result_json'], {}),
        'attempt_count': int(row['attempt_count'] or 0),
        'max_attempts': int(row['max_attempts'] or 0),
        'error_code': row['error_code'],
        'error_message': _safe_task_error(row['error_message']),
        'lease_owner': row['lease_owner'],
        'lease_expires_at': row['lease_expires_at'],
        'created_at': row['created_at'],
        'claimed_at': row['claimed_at'],
        'started_at': row['started_at'],
        'finished_at': row['finished_at'],
        'updated_at': row['updated_at'],
        'external_write_performed': False,
    }


def _script_experiment_from_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    item = dict(row)
    item['experiment_design'] = _loads(item.pop('experiment_design_json', '{}'), {})
    return item


def _script_operator_next_step(row: Dict[str, Any]) -> str:
    status = str(row.get('approval_status') or 'draft')
    experiment = row.get('experiment') or {}
    if status == 'rejected':
        return '已拒绝，不进入测试。'
    if experiment:
        exp_status = str(experiment.get('status') or '')
        if exp_status in {'shadow_review', 'testing'}:
            return '按目标样本跑小流量，观察主指标和真实入会护栏。'
        if exp_status == 'completed':
            return '查看复盘结果，决定放量、保留或回滚。'
    if status == 'approved':
        return '已通过，等待进入小流量测试。'
    return '先人工复核旧/新话术和成功样本，再决定是否通过。'


def _sum_segment_metric(segments: Sequence[Dict[str, Any]], key: str) -> int:
    return int(sum(int(row.get(key) or 0) for row in segments))


def _weighted_segment_rate(segments: Sequence[Dict[str, Any]], rate_key: str, denom_key: str) -> float:
    numerator = 0.0
    denominator = 0
    for row in segments:
        denom = int(row.get(denom_key) or 0)
        denominator += denom
        numerator += float(row.get(rate_key) or 0.0) * denom
    return round(numerator / denominator, 4) if denominator else 0.0


def _chain_step_action(stage: str, diagnosis: str) -> str:
    if stage == 'handoff_entry':
        return '人工看用户是否完成进线，非人工看自动报名是否触达；再进入后续话术步骤。'
    if stage == 'entered_im':
        return '先确认进入 IM 后是否有系统触达，再看广告承诺是否把用户带偏。'
    if stage == 'auto_apply_sent':
        return '检查自动报名模板是否清楚、低压，并能引出用户下一句。'
    if stage == 'r1_touched':
        return '检查 R1 模板是否说明下一步，不要只机械催注册。'
    if stage == 'before_im_message_ge_3':
        return '改开场承接和真实工作说明，让用户愿意继续聊。'
    if stage == 'link_sent':
        return '确认客服/机器人是否及时发送对应 App 下载链，没发链优先查流程触发。'
    if stage == 'before_link_clicked':
        return _funnel_stage_action(stage, diagnosis)
    if stage == 'after_link_click_before_bind':
        return '改提交平台 ID 指引话术，强调免费、安全、聊天/礼物获得钻石和完成后回到 IM。'
    if stage == 'after_bind_request_before_success':
        return '检查 bind/CRM，同时补失败提示收集和安抚话术。'
    if stage == 'converted':
        return '提炼成功样本，沉淀成同国家同语言 SOP。'
    if stage == 'ops_group_link_sent':
        return '检查入会成功后是否及时发送运营群链接，没发优先查 SOP 或客服执行。'
    if stage == 'ops_group_link_clicked':
        return '检查运营群链接说明和进群利益点，降低用户点群链接前的疑虑。'
    return _funnel_stage_action(stage, diagnosis)


def _im_chain_steps(
    *,
    totals: Dict[str, Any],
    segments: Sequence[Dict[str, Any]],
    funnel_insights: Sequence[Dict[str, Any]],
    external_apps: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    sample = int(totals.get('sample_conversations') or 0)
    successful = int(totals.get('successful_conversations') or 0)
    handoff_entry = _sum_segment_metric(segments, 'handoff_entry') or sample
    auto_apply = _sum_segment_metric(segments, 'auto_apply_sent')
    link_sent = _sum_segment_metric(segments, 'link_sent')
    link_sent_inferred = _sum_segment_metric(segments, 'link_sent_inferred')
    link_clicked = _sum_segment_metric(segments, 'link_clicked')
    bind_requested = _sum_segment_metric(segments, 'bind_requested')
    ops_group_sent = _sum_segment_metric(segments, 'ops_group_link_sent')
    ops_group_clicked = _sum_segment_metric(segments, 'ops_group_link_clicked')
    stage_loss: Dict[str, Dict[str, Any]] = {
        str(row.get('dropoff_stage') or ''): dict(row)
        for row in funnel_insights
    }

    def loss(*stages: str) -> Dict[str, Any]:
        matched = [stage_loss.get(stage) for stage in stages if stage_loss.get(stage)]
        diagnosis_counts: Dict[str, Dict[str, Any]] = {}
        country_counts: Dict[str, int] = {}
        total = 0
        weighted_share = 0.0
        for item in matched:
            affected = int(item.get('affected_conversations') or 0)
            total += affected
            weighted_share += float(item.get('share') or 0.0) * affected
            for diag in item.get('top_diagnoses') or []:
                diagnosis = str(diag.get('diagnosis') or '')
                if not diagnosis:
                    continue
                target = diagnosis_counts.setdefault(diagnosis, {
                    'diagnosis': diagnosis,
                    'diagnosis_zh': str(diag.get('diagnosis_zh') or DIAGNOSIS_LABELS.get(diagnosis, diagnosis) or ''),
                    'affected_conversations': 0,
                })
                target['affected_conversations'] += int(diag.get('affected_conversations') or 0)
            for country in item.get('top_countries') or []:
                country_name = str(country.get('country') or '-')
                country_counts[country_name] = country_counts.get(country_name, 0) + int(country.get('affected_conversations') or 0)
        top = max(diagnosis_counts.values(), key=lambda row: int(row.get('affected_conversations') or 0), default={})
        diagnosis = str(top.get('diagnosis') or '')
        countries = sorted(
            ({'country': country, 'affected_conversations': count} for country, count in country_counts.items()),
            key=lambda row: int(row.get('affected_conversations') or 0),
            reverse=True,
        )
        return {
            'lost_conversations': total,
            'loss_share': round(weighted_share / total, 4) if total else 0.0,
            'primary_diagnosis': diagnosis,
            'primary_diagnosis_zh': str(top.get('diagnosis_zh') or DIAGNOSIS_LABELS.get(diagnosis, diagnosis) or ''),
            'top_countries': countries[:3],
        }

    def step(
        key: str,
        label: str,
        event_name: str,
        template_step: str,
        count: int,
        denominator: int,
        metric_label: str,
        next_event: str,
        loss_stages: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        dropped = loss(*(loss_stages or [key]))
        diagnosis = dropped['primary_diagnosis']
        raw_count = int(count or 0)
        bounded_count = min(raw_count, int(denominator or 0)) if denominator else raw_count
        rate = round(bounded_count / denominator, 4) if denominator else 0.0
        actual_dropoff = max(int(denominator or 0) - bounded_count, 0)
        if actual_dropoff <= 0:
            dropped = {
                'lost_conversations': 0,
                'loss_share': 0.0,
                'primary_diagnosis': '',
                'primary_diagnosis_zh': '暂无明显主因',
                'top_countries': [],
            }
            diagnosis = ''
        attributed = int(dropped.get('lost_conversations') or 0)
        attribution_coverage = round(min(attributed, actual_dropoff) / actual_dropoff, 4) if actual_dropoff else 0.0
        return {
            'step_key': key,
            'step_label': label,
            'event_name': event_name,
            'template_step': template_step,
            'count': bounded_count,
            'raw_count': raw_count,
            'denominator': int(denominator or 0),
            'rate': rate,
            'actual_dropoff_count': actual_dropoff,
            'actual_dropoff_rate': round(actual_dropoff / denominator, 4) if denominator else 0.0,
            'attributed_conversations': attributed,
            'attribution_coverage': attribution_coverage,
            'unattributed_dropoff_count': max(actual_dropoff - min(attributed, actual_dropoff), 0),
            'attribution_note': '已归因样本含历史阶段口径，可能与严格漏斗掉点重叠。' if attributed > actual_dropoff and actual_dropoff > 0 else '',
            'metric_label': metric_label,
            'next_event': next_event,
            **dropped,
            'recommended_action': _chain_step_action(key, diagnosis),
        }

    effective_conversations = max(
        sample - int((stage_loss.get('before_im_message_ge_3') or {}).get('affected_conversations') or 0),
        0,
    )

    normalized_apps = sorted({str(value or '').strip() for value in external_apps if str(value or '').strip()})
    platform_id_label = (
        f'提交 {normalized_apps[0]} ID'
        if len(normalized_apps) == 1 and normalized_apps[0].lower() in {'linky', 'timo'}
        else '提交平台 ID'
    )
    platform_id_template = (
        f'社交 App 注册与 {normalized_apps[0]} ID 指引'
        if len(normalized_apps) == 1 and normalized_apps[0].lower() in {'linky', 'timo'}
        else '社交 App 注册与平台 ID 指引'
    )
    rows = [
        step('handoff_entry', '承接起点', 'first_user_message_at / auto_apply_message_sent', '人工用户进线 / 非人工自动报名', handoff_entry, sample, '起点有效覆盖率', 'im_message_ge_3'),
        step('before_im_message_ge_3', '有效对话', 'im_message_ge_3', '开场承接话术', effective_conversations, handoff_entry or sample, '承接起点→有效对话率', 'link_sent', loss_stages=['before_im_message_ge_3', 'before_first_user_reply']),
        step('link_sent', '发送下载链', 'link_sent', '对应 App 下载链发送模板', link_sent, effective_conversations or handoff_entry or sample, '有效对话→发链率', 'link_clicked', loss_stages=['link_sent', 'before_link_sent']),
        step('before_link_clicked', '点击下载链', 'link_clicked', '对应社交 App 信任解释', link_clicked, link_sent, '下载链点击率', 'guild_bind_request'),
        step('after_link_click_before_bind', platform_id_label, 'guild_bind_request', platform_id_template, bind_requested, link_clicked, '点击→提交ID率', 'joined'),
        step('converted', '入会成功', 'joined', '成功样本沉淀', successful, bind_requested, '提交ID→入会成功率', '发送运营群链接', loss_stages=['converted', 'after_bind_request_before_success', 'after_bind_success_before_join']),
        step('ops_group_link_sent', '发送运营群链接', 'ops_group_link_sent', '入会后运营群链接', ops_group_sent, successful, '入会→发送群链接率', 'ops_group_link_clicked'),
        step('ops_group_link_clicked', '点击运营群链接', 'ops_group_link_clicked', '运营群链接点击', ops_group_clicked, ops_group_sent, '发送群链接→点击率', '-'),
    ]
    link_sent_step = next(row for row in rows if row.get('step_key') == 'link_sent')
    link_sent_step['inferred_count'] = link_sent_inferred
    if link_sent_inferred:
        link_sent_step['data_quality_status'] = 'inferred_partial'
        link_sent_step['data_quality_note'] = (
            f'其中 {link_sent_inferred} 人仅由后续点击、提交 ID 或入会事件推定已发送；'
            '推定记录不参与真实发链时间计算。'
        )
    return rows


def _create_or_refresh_script_experiment(
    conn: sqlite3.Connection,
    *,
    script_suggestion_id: str,
    approved_by: str = '',
    now: str = '',
) -> Optional[Dict[str, Any]]:
    now = now or _utc_now()
    row = conn.execute(
        'SELECT * FROM im_script_suggestions WHERE script_suggestion_id = ?',
        (script_suggestion_id,),
    ).fetchone()
    if not row:
        return None
    suggestion = dict(row)
    design = _loads(suggestion.get('experiment_design_json') or '{}', {})
    sample_target = int(design.get('suggested_min_sample') or 0)
    primary_metric = str(design.get('primary_metric') or '')
    experiment_id = _stable_id(script_suggestion_id, suggestion.get('target_metric'), primary_metric, prefix='im_exp_')
    conn.execute(
        """
        INSERT INTO im_script_experiments (
            experiment_id, script_suggestion_id, diagnosis_type, country, language, funnel_stage,
            target_metric, primary_metric, old_script_summary, suggested_script, suggested_script_translation_zh, experiment_design_json,
            status, sample_target, observed_sample, baseline_value, experiment_value,
            guardrail_status, decision, review_summary, started_at, ended_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(experiment_id) DO UPDATE SET
            status = CASE
                WHEN im_script_experiments.status IN ('completed', 'retired') THEN im_script_experiments.status
                ELSE excluded.status
            END,
            sample_target = excluded.sample_target,
            target_metric = excluded.target_metric,
            primary_metric = excluded.primary_metric,
            old_script_summary = excluded.old_script_summary,
            suggested_script = excluded.suggested_script,
            suggested_script_translation_zh = excluded.suggested_script_translation_zh,
            experiment_design_json = excluded.experiment_design_json,
            updated_at = excluded.updated_at
        """,
        (
            experiment_id,
            script_suggestion_id,
            str(suggestion.get('diagnosis_type') or ''),
            str(suggestion.get('country') or ''),
            str(suggestion.get('language') or ''),
            str(suggestion.get('funnel_stage') or ''),
            str(suggestion.get('target_metric') or ''),
            primary_metric,
            str(suggestion.get('old_script_summary') or ''),
            str(suggestion.get('suggested_script') or ''),
            str(suggestion.get('suggested_script_translation_zh') or ''),
            _json(design),
            'shadow_review',
            sample_target,
            0,
            None,
            None,
            'unchecked',
            '',
            '',
            '',
            '',
            now,
            now,
        ),
    )
    conn.execute(
        """
        UPDATE im_script_suggestions
        SET experiment_status = ?, approved_by = COALESCE(NULLIF(approved_by, ''), ?), updated_at = ?
        WHERE script_suggestion_id = ?
        """,
        ('shadow_review', str(approved_by or ''), now, script_suggestion_id),
    )
    exp_row = conn.execute('SELECT * FROM im_script_experiments WHERE experiment_id = ?', (experiment_id,)).fetchone()
    return _script_experiment_from_row(exp_row)


def sanitize_im_script_suggestions(conn: sqlite3.Connection) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute('SELECT * FROM im_script_suggestions').fetchall()]
    fixed_scripts = 0
    fixed_translations = 0
    invalidated_linky_positioning = 0
    now = _utc_now()
    for row in rows:
        script = str(row.get('suggested_script') or '')
        translation = str(row.get('suggested_script_translation_zh') or '')
        language = _script_language(row.get('country'), row.get('language'))
        next_script = script
        next_translation = translation
        if _has_invalid_linky_positioning(
            script,
            translation,
            row.get('old_script_summary_translation_zh'),
            row.get('old_script_summary_interpretation_zh'),
        ):
            conn.execute(
                """
                UPDATE im_script_suggestions
                SET suggested_script_source = '',
                    suggested_script_translation_source = '',
                    old_script_summary_translation_source = '',
                    old_script_summary_interpretation_source = '',
                    launch_decision = 'needs_rewrite',
                    updated_at = ?
                WHERE script_suggestion_id = ?
                """,
                (now, row['script_suggestion_id']),
            )
            invalidated_linky_positioning += 1
            continue
        if _contains_cjk(script):
            next_script = _localized_non_cjk_script(
                str(row.get('diagnosis_type') or ''),
                row.get('country'),
                language,
                script,
            )
            if not next_translation:
                next_translation = scan_pii(script)['redacted_text'][:1200]
        if next_script != script or next_translation != translation:
            conn.execute(
                """
                UPDATE im_script_suggestions
                SET language = ?,
                    suggested_script = ?,
                    suggested_script_translation_zh = ?,
                    suggested_script_translation_source = CASE
                        WHEN suggested_script_translation_source = ? THEN suggested_script_translation_source
                        ELSE ''
                    END,
                    updated_at = ?
                WHERE script_suggestion_id = ?
                """,
                (language, next_script, next_translation, HERMES_LLM_PROVIDER_MODE, now, row['script_suggestion_id']),
            )
            fixed_scripts += int(next_script != script)
            fixed_translations += int(next_translation != translation)
        elif language != str(row.get('language') or ''):
            conn.execute(
                """
                UPDATE im_script_suggestions
                SET language = ?, updated_at = ?
                WHERE script_suggestion_id = ?
                """,
                (language, now, row['script_suggestion_id']),
            )
    conn.commit()
    return {
        'ok': True,
        'checked': len(rows),
        'fixed_scripts': fixed_scripts,
        'fixed_translations': fixed_translations,
        'invalidated_linky_positioning': invalidated_linky_positioning,
    }


def im_diagnosis_business_knowledge_pack(country: str = '', language: str = '') -> Dict[str, Any]:
    country_key = str(country or '').strip().lower()
    baseline = {
        'brazil': {
            'im_to_join_reference_rate': 0.10,
            'language': 'pt-BR',
            'notes': 'Brazil 当前可先按约 10% IM→真实入会率做参考，低于参考值时先分段看是否是 IM 承接、Linky 注册、bind 或 CRM succeed 掉点。',
        },
        'br': {
            'im_to_join_reference_rate': 0.10,
            'language': 'pt-BR',
            'notes': 'Brazil 当前可先按约 10% IM→真实入会率做参考。',
        },
        'indonesia': {
            'im_to_join_reference_rate': 0.20,
            'language': 'id-ID',
            'notes': 'Indonesia 当前可先按约 20% IM→真实入会率做参考，若进入 IM 后不注册 Linky，优先检查信任解释和步骤引导。',
        },
        'id': {
            'im_to_join_reference_rate': 0.20,
            'language': 'id-ID',
            'notes': 'Indonesia 当前可先按约 20% IM→真实入会率做参考。',
        },
    }
    selected_baseline = baseline.get(country_key, {'im_to_join_reference_rate': None, 'language': str(language or ''), 'notes': '该国家暂未配置稳定 baseline，只能按链路证据诊断，不要武断下结论。'})
    return {
        'version': 'im_business_knowledge_v2_3',
        'spec_version': 'v2.3',
        'businessFacts': {
            'tugao_positioning': 'Tugao 外层是网赚/积分/手机任务兴趣承接，不能直接包装成主播招聘、成人陪聊、固定日薪或保证收益。',
            'linky_definition': 'Linky 是免费注册的社交聊天 App，不是注册页面、资料确认页、表单、问卷或普通下一步页面。',
            'linky_value': '女性用户可在 Linky 内聊天互动、回复消息、收到礼物并累积钻石，达到 Linky App 当前规则后按 App 流程申请提现。',
            'agency_role': '我们是 Linky 授权合作机构/官方合作机构路径，帮助用户完成账号绑定、新手教程、接待权限、收益路径和后续运营支持。',
            'linky_id_use': 'Linky ID 用于后台确认账号并绑定到官方合作机构路径，对外解释为帮用户开通接待/收益相关权限、确认提现路径和进入培训指导。',
            'training_group': 'bind/入会成功后可邀请用户进入官方指导/教程/新手/收益指导 WhatsApp 群，群内有教程、答疑和专人培训。',
        },
        'conversionPolicy': {
            'default_do_not_mention': [
                '后台结算、机构抽成、分成比例、从用户收益中抽佣',
                '成人、裸聊、擦边、暧昧换收益',
                '广告和 Linky 的完整后台映射逻辑',
            ],
            'trigger_only_short_answers': {
                'agency_settlement_question': '用户主动问机构关系时，只说我们是 Linky 授权合作机构，负责新手绑定、教程和运营支持；不展开抽成。',
                'adult_concern': '用户主动问是否成人/裸聊时，短答为正常社交聊天互动，禁止成人、裸聊或违规内容，不展开刺激描述。',
                'ad_mismatch': '用户质疑为什么从 Tugao 到 Linky 时，短答为 Tugao 负责初步报名和客服指导，Linky 是后续实际进行社交聊天和钻石收益的平台。',
                'withdrawal_concern': '用户问提现时，说明按 Linky App 当前规则，首提和后续门槛以 App 内显示为准，不保证固定到账。',
            },
            'must_not_say': [
                '我能保证你提现',
                '今天一定到账',
                '只要聊天就一定赚钱',
                'Linky 是资料确认页面',
                'Linky 是表单',
                '把 ID 给我是为了我们抽成',
            ],
        },
        'funnelStates': [
            'lead_created', 'first_message_sent', 'first_reply', 'interest_confirmed', 'linky_explained',
            'safety_explained', 'linky_link_sent', 'linky_link_clicked', 'linky_registered',
            'linky_id_requested', 'linky_id_received', 'bind_submitted', 'bind_success', 'bind_failed',
            'group_invited', 'group_joined', 'first_message_received', 'first_diamond_seen',
            'withdrawal_rule_viewed', 'inactive', 'blocked_or_complained',
        ],
        'stateGuards': [
            '未解释 Linky 前，不要直接长篇催 Linky ID。',
            '未点击 Linky 前，不要催 Linky ID。',
            '未注册 Linky 前，不要说 bind 已成功。',
            '未收到 ID 前，不要说已经绑定。',
            'bind_failed 时，不要邀请进群为成功用户；先处理失败原因。',
            'bind_success 后，不要重复发注册链接；应解释下一步和邀请进指导群。',
            '用户有安全/费用/隐私顾虑时，先处理顾虑，不继续硬催注册或发 ID。',
        ],
        'concernClassifier': sorted(IM_USER_CONCERN_TYPES),
        'riskScorer': {
            'dimensions': list(IM_SCRIPT_RISK_DIMENSIONS),
            'scale': '0=无明显风险，1=轻微需注意，2=高风险需人工审核，3=禁止复制和上线',
            'hard_block_rule': '任一风险维度为 3 时，launch_decision 必须为 blocked_by_risk。',
        },
        'triggerFaqTemplates': {
            'linky_is_what': 'Linky 是一个免费的社交聊天 App，你可以在里面聊天互动、回复消息、收到礼物并累积钻石，提现按 App 当前规则。',
            'is_free_safe': '注册和了解流程不需要付费，也不需要充值；我会在这里一步一步指导你，提现和钻石规则以 App 内显示为准。',
            'why_need_linky_id': 'ID 是 Linky 账号编号，不是密码或验证码。我需要它帮你确认账号并绑定到官方合作机构路径，后面才能继续给你新手教程和接待指导。',
            'where_find_id': '注册完成后，在 Linky 的“我的/个人页”可以看到你的 Linky ID，复制发给我就可以。',
            'after_bind_group': '我这边确认后，会邀请你进官方指导群，里面有教程、答疑和专人培训，能帮你更快上手。',
        },
        'business_goal': {
            'north_star': '提高从广告进入 IM 后到真实入会 succeed 的转化率，同时避免低质量流量和违规话术。',
            'diagnosis_question': '这段 IM 对话失败时，问题发生在用户意图、客服承接、Linky 注册、bind、CRM succeed，还是广告承诺偏差？',
            'llm_role': 'Hermes LLM 要像资深一线转化客服主管一样思考：读懂用户心理、历史成功/失败说法和事件掉点，再生成客服可直接参考的话术实验方案；不直接修改广告、不直接改客服 SOP、不直接标记任务完成。',
            'script_quality_bar': '建议话术不是普通解释或礼貌润色，必须回应一个具体顾虑、解释一个必要原因、给一个低压力下一步，并明确服务于当前漏斗掉点。',
        },
        'external_positioning': {
            'public_app_positioning': '对外广告和 App 外显是网赚/积分类 App，吸引有线上副业、积分奖励、手机任务意愿的人进入 App/IM。不要在广告或首轮承接里直接包装成强招聘、陪聊工作或保证收益。',
            'im_conversion_intent': '进入 IM 后，客服先确认用户是否有居家手机兼职/赚钱兴趣；当用户表达明显兴趣后，再引导她了解 Linky/Timo/Haiya/HeatUp 这类社交聊天 App，并完成免费注册、提供 Linky ID、绑定到官方合作机构、公会/KA 权限确认和后续入会。',
            'script_implication': '客服话术不能一上来像广告一样强推工作或保证收益，也不能把 Linky 说成表单/资料确认页。要顺着用户的网赚/积分兴趣，正向解释 Linky 是免费的社交聊天 App，用户可通过聊天互动和礼物按平台规则获得钻石；注册后提供 Linky ID，是为了由官方合作机构帮她开通接待/收益权限并进入培训指导。除非用户主动误解，否则不要特意说“不是表单/不是资料页”。',
        },
        'product_roles': {
            'Tugao': '业务/App 侧主体。对外承接网赚/积分/手机任务兴趣用户，IM 里筛选并引导合适用户进入社交聊天 App 链路。',
            'Premiou': '巴西市场常见品牌露出之一。诊断时可识别品牌，但不要把品牌名当成用户行为证据。',
            'MCN_or_agency': '本业务是 MCN/公会/KA 官方合作机构承接链路。对用户可表达为 Linky 官方合作机构/partner agency，帮助她完成账号绑定、接待权限、收益路径和新手培训；不要向用户表达“机构从你收益里抽成”。',
            'Linky': '客服推荐给用户尝试/注册的付费社交聊天 App。女生可在里面通过回复男用户聊天、收到礼物等方式获得钻石；钻石积累到平台规则后可以申请提现。注册/开始使用应强调免费和安全边界。',
            'Timo_Haiya_HeatUp': '与 Linky 类似的社交聊天 App，核心都是聊天互动、礼物、钻石和按平台规则提现。不同 App 名称可能出现在不同国家或账户承接链路里。',
            'diamonds': '社交聊天 App 内的激励单位。来源通常包括聊天互动、回复消息、语音/视频通话和礼物；不能承诺固定钻石、固定收入或保证提现。',
            'withdrawal_rules': '可说明平台规则级信息：Linky 首提门槛约 2500 钻石=0.5 美元；后续通常 5000 钻石起提，每次约 1 美元；新手任务可能提供约 800 钻石。必须说“按平台规则/通常/有机会”，不能承诺固定到账或固定时长一定提现。',
            'linky_id': '用户完成 Linky 注册后，可在 Linky App 的“我的/个人页”看到 Linky ID。客服需要用户提供 Linky ID，用于帮她绑定到官方合作机构、开通接待/收益相关权限、确认后续提现路径和进入培训指导。',
            'bind': '用户完成 Linky/Timo 等 App 与当前业务链路的绑定/核验动作。绑定后账号才进入官方合作机构承接链路，才更适合继续接收平台分配的男用户消息和收益指导。',
            'CRM_succeed': '后续 bind / CRM 校验成功，是真实入会口径。最终经营校验以 succeed 为准。',
            'submit_application_or_join_guild': '媒体归因入会事件，对标用户完成入会申请/入会动作；用于归因对比，但诊断时仍要和真实 succeed 分开看。',
            'official_training_group': '用户绑定/加入后通常会被邀请进官方指导/教程/新手/收益指导群。群内提供教程、快速上手方法、答疑、专人培训指导和日常运营提醒。',
        },
        'funnel_definition': [
            {'stage': 'ad_impression_or_click', 'owner': 'ad_material', 'meaning': '广告把用户吸引到 App/IM。'},
            {'stage': 'entered_im', 'owner': 'ad_material_then_im', 'meaning': '用户进入 IM。素材到这里基本完成主要职责。'},
            {'stage': 'system_auto_touch', 'owner': 'system', 'meaning': '自动报名或自动欢迎消息发送成功，只说明系统触达，不代表用户有效意愿。'},
            {'stage': 'first_user_reply', 'owner': 'user_and_im', 'meaning': '用户主动回复，是用户行为型有效 IM 的起点。'},
            {'stage': 'human_messages_ge_3', 'owner': 'im_service', 'meaning': '真人消息达到互动门槛，可作为高质量 IM 的辅助证据。'},
            {'stage': 'link_sent', 'owner': 'im_service', 'meaning': '客服发送 Linky/Timo 等社交聊天 App 注册入口。只发链接不解释 App 是什么、是否免费安全、为什么要注册、注册后为什么要给 Linky ID，通常不是好承接。'},
            {'stage': 'link_clicked', 'owner': 'user_and_im', 'meaning': '用户点击链接，是高意向 IM 信号。'},
            {'stage': 'linky_registered', 'owner': 'user_and_linky', 'meaning': '用户完成社交聊天 App 注册，说明客服对 App 价值、安全、免费和步骤的解释部分有效。注册后应引导她在“我的/个人页”找到 Linky ID。'},
            {'stage': 'linky_id_collected', 'owner': 'im_service_and_user', 'meaning': '用户把 Linky ID 发给客服。客服对外解释为帮她开通接待/收益权限、绑定到官方合作机构和确认提现路径。'},
            {'stage': 'bind_succeeded', 'owner': 'linky_bind', 'meaning': '绑定成功，是入会前关键动作。绑定后应邀请用户进入官方指导/教程群，学习如何接待消息、聊天、获得钻石和处理问题。'},
            {'stage': 'crm_succeeded_or_real_join', 'owner': 'crm_and_ops', 'meaning': '真实入会 succeed，最终经营口径。'},
        ],
        'country_baseline': selected_baseline,
        'response_time_rule': {
            'first_response_target_seconds': 60,
            'interpretation': '超过 1 分钟首响会明显影响转化。若用户进入 IM 后迟迟无人接，优先判客服响应问题，而不是素材问题。',
        },
        'common_user_scenarios': {
            'valid_user': [
                '想了解是否可以居家用手机兼职赚钱，或通过社交聊天获得钻石奖励',
                '关注 App 是否安全、是否免费、是否需要充值、钻石怎么来、提现门槛、为什么要提供 Linky ID、工作步骤是否真实可信',
                '愿意继续问问题、点击 Linky/Timo 等社交 App 链接或尝试注册',
            ],
            'common_dropoffs': [
                '进入 IM 后不说话',
                '客服发步骤或链接后用户不去注册 Linky/Timo 等 App',
                '用户不理解 Linky 是社交聊天 App，只觉得是陌生链接或担心有风险',
                '用户担心被骗或担心链接不安全',
                '用户担心是否收费、是否需要充值、钻石是否真的能提现',
                '用户完成注册后不知道在哪里找 Linky ID，或不理解为什么要把 ID 发给客服',
                '用户问收益/工作方式，客服没有解释清楚',
                '客服重复模板，没有根据用户顾虑推进',
                '首响超过 1 分钟导致用户离开',
            ],
        },
        'diagnosis_boundaries': {
            'material_problem_when': [
                '用户进入 IM 后明显表达与业务不匹配，例如只想领现金、薅羊毛、下载奖励，且广告表达可能过宽或误导',
                '大量用户只问广告中过度承诺的收益或福利，且不愿了解真实工作流程',
                '广告承诺和真实 Linky/入会流程明显不一致',
            ],
            'im_service_problem_when': [
                '用户有兴趣或有疑问，但客服只发链接、不解释 Linky/Timo 是社交聊天 App',
                '用户问安全/免费/提现/钻石/Linky ID 用途，客服没有回应核心顾虑',
                '用户表达担忧，客服没有建立信任或回应顾虑',
                '客服过早要求注册，未先解释价值和流程',
                '客服步骤跳跃，用户不知道下一步该做什么',
                '首响慢或高意向用户没有被继续推进',
            ],
            'linky_or_bind_problem_when': [
                '用户已经点击链接或尝试注册，但卡在 Linky/Timo 等 App 注册、bind 或页面流程',
                '用户反馈打不开、不会填、步骤失败、提交后没反馈',
            ],
            'crm_problem_when': [
                '用户已完成 bind 或入会前动作，但 CRM succeed 未发生或延迟异常',
                '对话本身承接正常，但最终 succeed 掉线',
            ],
            'observe_when': [
                '样本太少',
                '对话缺失关键轮次',
                '事件链路不完整',
                '用户完全沉默且没有足够广告承诺证据',
            ],
        },
        'good_service_principles': [
            '先顺着用户从网赚/积分 App 进来的期待，确认她是想了解手机居家兼职/线上副业，再介绍 Linky 机会。',
            '不要一上来把话术说成强工作招聘或保证赚钱；更适合解释为“我们是 Linky 官方合作机构，可以推荐你先了解一个免费的社交聊天 App，看是否适合通过聊天互动获得钻石”。',
            '解释 Linky 的作用：它是社交聊天 App，用户可以在里面回复男用户消息、聊天、收到礼物，并按平台规则获得钻石和申请提现。不要主动用“不是表单”这类否定式纠偏做开场。',
            '必须回应安全和免费：注册/开始了解不收费；不要让用户充值或支付；客服会在 IM 里继续指导；提现、钻石、礼物都按 App 平台规则。',
            '可说明提现规则，但必须用平台规则口径：首提约 2500 钻石=0.5 美元，后续通常 5000 钻石起提，每次约 1 美元；新手任务可能提供约 800 钻石；不能保证固定收入、固定提现或几小时必定提现。',
            '解释 Linky ID：注册后在“我的/个人页”找到 Linky ID；客服需要 ID 帮用户开通接待/收益权限、绑定到官方合作机构并确认提现路径。不要对用户讲机构抽成。',
            '分步骤推进：先解释 App 是什么和为什么推荐，再让用户完成一个小动作；完成注册后回到 IM 发 Linky ID，由客服继续确认绑定/入会状态。',
            '绑定或加入后邀请用户进官方指导/教程/新手/收益指导群，说明群里有教程、答疑、专人培训和快速上手方法。',
            '回应顾虑：针对安全、免费、钻石来源、提现规则、Linky ID 用途、是否真实、如何开始分别回答，不要只复制模板；用户有自然语言回复时要承接，而不是要求她按固定数字。',
            '高意向用户要继续推进，不要在用户问完关键问题后断联。',
            '语言应贴近当地用户，巴西使用自然葡语，印尼使用自然印尼语。',
        ],
        'bad_service_patterns': [
            '只发链接不解释',
            '用户问为什么还要注册，客服重复“点链接”',
            '把 Linky 说成资料确认页、表单页或单纯注册页面，导致用户不知道它是社交聊天 App；修正时应正向说明真实用途，不要机械强调“不是表单”',
            '不解释免费、安全、钻石来源和提现规则，直接催用户注册或发 ID',
            '向用户暴露“机构抽成/从你收益里拿分成”的内部商业逻辑',
            '用户完成注册后，不说明 Linky ID 在哪里找，也不解释 ID 是为了开通接待/收益权限和绑定官方合作机构',
            '过早要求填写资料或注册，未建立信任',
            '承诺固定收入、保证收益、夸大赚钱速度',
            '话术像诈骗，例如催促转账、索要敏感信息、过度强调马上赚钱',
            '使用与用户语言不匹配或机器翻译感很强的表达',
            '用户沉默后没有低压唤醒，也没有给更简单的下一步',
        ],
        'risk_and_compliance': {
            'hard_forbidden': [
                '手机号、邮箱、WhatsApp、真实姓名、用户 ID、身份证、银行卡等 PII',
                '保证收益、固定高收入、现金雨、夸张赚钱承诺',
                '要求用户转账、提供银行卡或敏感证件',
                '把自动消息发送成功描述成用户真实意愿',
            ],
            'safe_reply_style': [
                '解释流程原因',
                '降低用户不确定感',
                '给一个下一步动作',
                '不承诺结果，只说明完成步骤后会继续核验',
            ],
        },
        'diagnosis_output_guidance': {
            'primary_diagnosis': '优先从 taxonomy 里选一个最主要原因，不要同时罗列所有可能。',
            'evidence': '证据必须来自对话或事件链路，用 turn_index 指明关键轮次。',
            'suggested_reply': '建议话术要能直接给客服参考，但保持 draft，不要声称已经生效。',
            'confidence': '证据完整且对话和事件一致时 high；缺消息或缺事件时 medium/low。',
        },
    }


def _conversation_llm_payload(conn: sqlite3.Connection, conversation_id: str) -> Dict[str, Any]:
    detail = im_conversation_detail(conn, conversation_id)
    if not detail.get('ok'):
        raise ValueError('conversation_not_found')
    conversation = dict(detail.get('conversation') or {})
    messages = list(detail.get('messages') or [])
    if any(str(message.get('pii_scan_status') or '') == 'blocked' for message in messages):
        raise ValueError('conversation_contains_pii')
    diagnosis = dict(detail.get('diagnosis') or {})
    safe_messages: List[Dict[str, Any]] = []
    for message in messages[:80]:
        text = str(message.get('message_text_redacted') or '')
        pii = scan_pii(text)
        if pii['status'] == 'blocked':
            raise ValueError('conversation_contains_pii')
        safe_messages.append({
            'turn_index': int(message.get('message_index') or 0),
            'sender_type': str(message.get('sender_type') or ''),
            'message_type': str(message.get('message_type') or 'text'),
            'message_at': str(message.get('message_at') or ''),
            'text': pii['redacted_text'],
            'is_auto_message': bool(message.get('is_auto_message')),
            'is_template_message': bool(message.get('is_template_message')),
            'is_human_agent_message': bool(message.get('is_human_agent_message')),
            'has_link': bool(message.get('has_link')),
        })
    safe_events = [
        {
            'event_name': str(event.get('event_name') or ''),
            'event_time': str(event.get('event_time') or ''),
            'event_status': str(event.get('event_status') or ''),
            'event_source': str(event.get('event_source') or ''),
        }
        for event in list(detail.get('events') or [])[:80]
    ]
    return {
        'conversation_key': conversation_id,
        'taxonomy_version': TAXONOMY_VERSION,
        'prompt_version': HERMES_LLM_PROMPT_VERSION,
        'business_context': im_diagnosis_business_knowledge_pack(conversation.get('country'), conversation.get('language')),
        'attribution': {
            'country': conversation.get('country') or '',
            'language': conversation.get('language') or '',
            'media_source': conversation.get('media_source') or '',
            'campaign_id': conversation.get('campaign_id') or '',
            'campaign_name': conversation.get('campaign_name') or '',
            'adset_id': conversation.get('adset_id') or '',
            'adset_name': conversation.get('adset_name') or '',
            'ad_id': conversation.get('ad_id') or '',
            'ad_name': conversation.get('ad_name') or '',
            'creative_id': conversation.get('creative_id') or '',
        },
        'conversation_metrics': {
            'first_response_seconds': float(conversation.get('first_response_seconds') or 0),
            'final_outcome': conversation.get('final_outcome') or '',
            'dropoff_stage': conversation.get('dropoff_stage') or '',
            'final_join_status': conversation.get('final_join_status') or '',
        },
        'baseline_rule_diagnosis': {
            'primary_diagnosis': diagnosis.get('primary_diagnosis') or '',
            'primary_diagnosis_zh': diagnosis.get('primary_diagnosis_zh') or '',
            'agent_issue': diagnosis.get('agent_issue') or '',
            'user_objection': diagnosis.get('user_objection') or '',
            'action_type': diagnosis.get('action_type') or '',
        },
        'historical_context': _historical_context_for_llm(conn, conversation, diagnosis),
        'allowed_taxonomy': [
            {'key': key, 'label_zh': label}
            for key, label in DIAGNOSIS_LABELS.items()
        ],
        'messages': safe_messages,
        'events': safe_events,
        'output_schema': {
            'primary_diagnosis': 'string, must use taxonomy key when possible',
            'secondary_diagnoses': ['string'],
            'loss_stage': 'string',
            'current_state': f'string, one of {sorted(IM_FUNNEL_STATES)}',
            'user_concern_type': f'string, one of {sorted(IM_USER_CONCERN_TYPES)}',
            'severity': 'low|medium|high',
            'user_intent': 'string',
            'user_objection': 'string',
            'agent_issue': 'string',
            'critical_turn_index': 'integer',
            'evidence': [{'turn_index': 'integer', 'speaker': 'string', 'summary': 'string'}],
            'suggested_reply': 'string',
            'recommended_replacement': {
                'scenario': 'string',
                'suggested_message': 'string',
                'suggested_message_translation_zh': 'string, Chinese translation of suggested_message for operators',
                'target_metric': 'string, which funnel metric this reply should improve',
                'experiment_hypothesis': 'string, why this reply should improve the metric',
            },
            'old_script': {
                'text': 'original-language failed historical wording or pattern',
                'translation_zh': 'accurate Chinese translation',
                'interpretation_zh': 'business interpretation for operators',
            },
            'new_script': {
                'text': 'localized replacement script',
                'translation_zh': 'accurate Chinese translation',
            },
            'risk_score': {key: 'integer 0-3' for key in IM_SCRIPT_RISK_DIMENSIONS},
            'launch_decision': 'allow_testing|needs_human_review|blocked_by_risk|insufficient_context',
            'attribution_candidates': [{
                'attribution_type': 'string, one of historical_context.attribution_candidates_allowed when possible',
                'attribution_zh': 'string, Chinese attribution label',
                'evidence_summary': 'string, how current and historical chats support this attribution',
                'failure_pattern_original': 'original-language historical failed wording',
                'failure_pattern_translation_zh': 'accurate Chinese translation',
                'interpretation_zh': 'business interpretation',
                'solution': 'string, concrete operator/customer-service action',
                'target_metric': 'string, metric this script should improve',
                'suggested_message': 'localized reply',
                'suggested_message_translation_zh': 'Chinese translation of suggested_message',
            }],
            'action_type': 'im_script_improvement|im_handoff_fix|material_review|observe|process_fix',
            'confidence': 'low|medium|high',
        },
    }


def create_im_llm_diagnosis_task(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    diagnosis_run_id: str = '',
    max_attempts: int = 3,
    force: bool = False,
) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    conn.row_factory = sqlite3.Row
    conversation_id = str(conversation_id or '').strip()
    if not conversation_id:
        raise ValueError('conversation_id_required')
    active = conn.execute(
        """
        SELECT * FROM im_llm_diagnosis_tasks
        WHERE conversation_id = ? AND status IN ('queued', 'claimed')
        ORDER BY created_at DESC LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()
    if active and not force:
        return {'ok': True, 'task': _diagnosis_task_from_row(active), 'created': False}
    payload = _conversation_llm_payload(conn, conversation_id)
    run_id = str(diagnosis_run_id or '').strip() or f'im_llm_run_{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")}_{uuid.uuid4().hex[:6]}'
    now = _utc_now()
    task_id = f'im_llm_task_{_stable_id(conversation_id, run_id, now)}'
    conn.execute(
        """
        INSERT INTO im_llm_diagnosis_tasks (
            task_id, conversation_id, diagnosis_run_id, provider_mode, status, prompt_version, taxonomy_version,
            payload_json, result_json, max_attempts, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            conversation_id,
            run_id,
            HERMES_LLM_PROVIDER_MODE,
            IM_LLM_TASK_STATUS_QUEUED,
            HERMES_LLM_PROMPT_VERSION,
            TAXONOMY_VERSION,
            _json(payload),
            '{}',
            max(1, min(int(max_attempts or 3), 10)),
            now,
            now,
        ),
    )
    conn.commit()
    return {'ok': True, 'task': get_im_llm_diagnosis_task(conn, task_id), 'created': True}


def create_im_llm_diagnosis_tasks_for_latest_run(
    conn: sqlite3.Connection,
    *,
    diagnosis_run_id: str = '',
    primary_diagnosis: str = '',
    dropoff_stage: str = '',
    limit: int = 50,
    force: bool = False,
) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    conn.row_factory = sqlite3.Row
    if not diagnosis_run_id:
        diagnosis_run_id = _latest_diagnosis_run_id(conn)
    params: List[Any] = [diagnosis_run_id]
    diagnosis_clause = ''
    if str(primary_diagnosis or '').strip():
        diagnosis_clause = ' AND primary_diagnosis = ?'
        params.append(str(primary_diagnosis or '').strip())
    stage_clause = ''
    if str(dropoff_stage or '').strip():
        stage_clause = ' AND dropoff_stage = ?'
        params.append(str(dropoff_stage or '').strip())
    params.append(max(1, min(int(limit or 50), 500)))
    rows = conn.execute(
        """
        SELECT conversation_id
        FROM im_conversation_diagnoses
        WHERE diagnosis_run_id = ? AND needs_human_review = 1
        """ + diagnosis_clause + stage_clause + """
        ORDER BY created_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    tasks: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    for row in rows:
        cid = str(row['conversation_id'] or '')
        try:
            tasks.append(create_im_llm_diagnosis_task(conn, conversation_id=cid, diagnosis_run_id=diagnosis_run_id, force=force)['task'])
        except ValueError as exc:
            skipped.append({'conversation_id': cid, 'reason': str(exc)})
    return {'ok': True, 'diagnosis_run_id': diagnosis_run_id, 'primary_diagnosis': str(primary_diagnosis or ''), 'dropoff_stage': str(dropoff_stage or ''), 'created_or_existing': len(tasks), 'tasks': tasks, 'skipped': skipped}


def get_im_llm_diagnosis_task(conn: sqlite3.Connection, task_id: str) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM im_llm_diagnosis_tasks WHERE task_id = ?', (str(task_id or ''),)).fetchone()
    if not row:
        raise ValueError('im_llm_diagnosis_task_not_found')
    return _diagnosis_task_from_row(row)


def reconcile_expired_im_llm_diagnosis_tasks(
    conn: sqlite3.Connection,
    *,
    now: str = '',
    limit: int = 100,
) -> Dict[str, Any]:
    """Recover expired claims without stealing live leases or retrying forever."""
    ensure_im_diagnostics_tables(conn)
    conn.row_factory = sqlite3.Row
    effective_now = str(now or _utc_now())
    rows = conn.execute(
        """
        SELECT task_id, attempt_count, max_attempts
        FROM im_llm_diagnosis_tasks
        WHERE provider_mode = ?
          AND status = ?
          AND lease_expires_at <> ''
          AND julianday(lease_expires_at) <= julianday(?)
        ORDER BY lease_expires_at ASC, created_at ASC
        LIMIT ?
        """,
        (
            HERMES_LLM_PROVIDER_MODE,
            IM_LLM_TASK_STATUS_CLAIMED,
            effective_now,
            max(1, min(int(limit or 100), 1000)),
        ),
    ).fetchall()
    requeued_task_ids: List[str] = []
    failed_task_ids: List[str] = []
    for row in rows:
        task_id = str(row['task_id'] or '')
        attempt_count = int(row['attempt_count'] or 0)
        max_attempts = max(1, int(row['max_attempts'] or 3))
        exhausted = attempt_count >= max_attempts
        next_status = IM_LLM_TASK_STATUS_FAILED if exhausted else IM_LLM_TASK_STATUS_QUEUED
        error_code = 'lease_expired_attempts_exhausted' if exhausted else 'lease_expired_requeued'
        error_message = (
            'IM diagnosis lease expired after the maximum number of attempts.'
            if exhausted
            else 'IM diagnosis lease expired and was returned to the queue.'
        )
        cursor = conn.execute(
            """
            UPDATE im_llm_diagnosis_tasks
            SET status = ?, lease_owner = '', lease_expires_at = '',
                error_code = ?, error_message = ?,
                finished_at = CASE WHEN ? = ? THEN ? ELSE '' END,
                updated_at = ?
            WHERE task_id = ?
              AND status = ?
              AND lease_expires_at <> ''
              AND julianday(lease_expires_at) <= julianday(?)
            """,
            (
                next_status,
                error_code,
                error_message,
                next_status,
                IM_LLM_TASK_STATUS_FAILED,
                effective_now,
                effective_now,
                task_id,
                IM_LLM_TASK_STATUS_CLAIMED,
                effective_now,
            ),
        )
        if int(cursor.rowcount or 0) != 1:
            continue
        if exhausted:
            failed_task_ids.append(task_id)
        else:
            requeued_task_ids.append(task_id)
    conn.commit()
    return {
        'ok': True,
        'checked': len(rows),
        'requeued': len(requeued_task_ids),
        'failed': len(failed_task_ids),
        'requeued_task_ids': requeued_task_ids,
        'failed_task_ids': failed_task_ids,
    }


def next_im_llm_diagnosis_task(conn: sqlite3.Connection, *, claim: bool = False, lease_owner: str = 'hermes-llm-agent', lease_seconds: int = 900) -> Optional[Dict[str, Any]]:
    ensure_im_diagnostics_tables(conn)
    reconcile_expired_im_llm_diagnosis_tasks(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT * FROM im_llm_diagnosis_tasks
        WHERE provider_mode = ? AND status = ?
        ORDER BY created_at ASC LIMIT 1
        """,
        (HERMES_LLM_PROVIDER_MODE, IM_LLM_TASK_STATUS_QUEUED),
    ).fetchone()
    if not row:
        return None
    task = _diagnosis_task_from_row(row)
    if claim:
        return claim_im_llm_diagnosis_task(conn, task['task_id'], lease_owner=lease_owner, lease_seconds=lease_seconds)
    return task


def claim_im_llm_diagnosis_task(conn: sqlite3.Connection, task_id: str, *, lease_owner: str = 'hermes-llm-agent', lease_seconds: int = 900) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    task = get_im_llm_diagnosis_task(conn, task_id)
    if task['status'] not in {IM_LLM_TASK_STATUS_QUEUED, IM_LLM_TASK_STATUS_CLAIMED}:
        raise ValueError('im_llm_diagnosis_task_not_claimable')
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_expires_at = (now_dt + timedelta(seconds=max(60, int(lease_seconds or 900)))).isoformat()
    cursor = conn.execute(
        """
        UPDATE im_llm_diagnosis_tasks
        SET status = ?, lease_owner = ?, lease_expires_at = ?, claimed_at = CASE WHEN claimed_at = '' THEN ? ELSE claimed_at END,
            started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END, attempt_count = attempt_count + 1, updated_at = ?
        WHERE task_id = ?
          AND (
              status = ?
              OR (
                  status = ?
                  AND lease_expires_at <> ''
                  AND julianday(lease_expires_at) <= julianday(?)
              )
          )
        """,
        (
            IM_LLM_TASK_STATUS_CLAIMED,
            str(lease_owner or 'hermes-llm-agent'),
            lease_expires_at,
            now,
            now,
            now,
            task_id,
            IM_LLM_TASK_STATUS_QUEUED,
            IM_LLM_TASK_STATUS_CLAIMED,
            now,
        ),
    )
    if int(cursor.rowcount or 0) != 1:
        conn.rollback()
        raise ValueError('im_llm_diagnosis_task_not_claimable')
    conn.commit()
    return get_im_llm_diagnosis_task(conn, task_id)


def fail_im_llm_diagnosis_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    error_code: str,
    error_message: str = '',
    retryable: bool = True,
    provider_response: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    task = get_im_llm_diagnosis_task(conn, task_id)
    if task['status'] not in {IM_LLM_TASK_STATUS_QUEUED, IM_LLM_TASK_STATUS_CLAIMED}:
        raise ValueError('im_llm_diagnosis_task_not_failable')
    attempt_count = int(task.get('attempt_count') or 0)
    max_attempts = max(1, int(task.get('max_attempts') or 3))
    can_retry = bool(retryable) and attempt_count < max_attempts
    next_status = IM_LLM_TASK_STATUS_QUEUED if can_retry else IM_LLM_TASK_STATUS_FAILED
    now = _utc_now()
    cursor = conn.execute(
        """
        UPDATE im_llm_diagnosis_tasks
        SET status = ?, lease_owner = '', lease_expires_at = '', error_code = ?, error_message = ?,
            result_json = ?, updated_at = ?
        WHERE task_id = ? AND status IN (?, ?)
        """,
        (
            next_status,
            _safe_task_error(error_code or 'hermes_llm_diagnosis_failed', 120),
            _safe_task_error(error_message or error_code or 'hermes_llm_diagnosis_failed', 500),
            _json(provider_response or {}),
            now,
            task_id,
            IM_LLM_TASK_STATUS_QUEUED,
            IM_LLM_TASK_STATUS_CLAIMED,
        ),
    )
    if int(cursor.rowcount or 0) != 1:
        conn.rollback()
        raise ValueError('im_llm_diagnosis_task_not_failable')
    conn.commit()
    return get_im_llm_diagnosis_task(conn, task_id)


def _normalize_llm_diagnosis_result(result: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    diagnosis_obj = result.get('diagnosis') if isinstance(result.get('diagnosis'), dict) else {}
    old_script_obj = result.get('old_script') if isinstance(result.get('old_script'), dict) else {}
    new_script_obj = result.get('new_script') if isinstance(result.get('new_script'), dict) else {}
    primary = str(
        result.get('primary_diagnosis')
        or diagnosis_obj.get('primary_diagnosis')
        or diagnosis_obj.get('diagnosis_type')
        or fallback.get('primary_diagnosis')
        or 'data_insufficient'
    ).strip()
    if primary not in DIAGNOSIS_LABELS:
        primary = 'data_insufficient'
    secondary = [
        str(item).strip()
        for item in (result.get('secondary_diagnoses') or [])
        if str(item).strip() in DIAGNOSIS_LABELS and str(item).strip() != primary
    ][:5]
    confidence = str(result.get('confidence') or result.get('severity') or 'low').strip().lower()
    if confidence not in {'low', 'medium', 'high'}:
        confidence = 'low'
    action_type = str(result.get('action_type') or fallback.get('action_type') or 'observe').strip()
    if action_type not in {'im_script_improvement', 'im_handoff_fix', 'material_review', 'observe', 'process_fix'}:
        action_type = 'observe'
    evidence: List[Dict[str, Any]] = []
    for item in list(result.get('evidence') or [])[:6]:
        if not isinstance(item, dict):
            continue
        summary = scan_pii(str(item.get('summary') or ''))['redacted_text'][:240]
        evidence.append({
            'turn_index': int(item.get('turn_index') or 0),
            'speaker': str(item.get('speaker') or item.get('sender_type') or '')[:40],
            'summary': summary,
        })
    replacement = result.get('recommended_replacement') if isinstance(result.get('recommended_replacement'), dict) else {}
    raw_candidates = result.get('attribution_candidates') if isinstance(result.get('attribution_candidates'), list) else []
    if not raw_candidates and isinstance(result.get('script_candidates'), list):
        raw_candidates = result.get('script_candidates') or []
    normalized_candidates: List[Dict[str, Any]] = []
    for item in raw_candidates[:5]:
        if not isinstance(item, dict):
            continue
        attribution_type = str(item.get('attribution_type') or item.get('type') or 'other').strip()
        if attribution_type not in SCRIPT_ATTRIBUTION_LABELS:
            attribution_type = 'other'
        candidate_message = scan_pii(
            _remove_linky_form_negation(item.get('suggested_message') or item.get('suggested_reply') or '')
        )['redacted_text'][:1200]
        candidate_translation = scan_pii(
            _remove_linky_form_negation(item.get('suggested_message_translation_zh') or item.get('translation_zh') or '')
        )['redacted_text'][:1200]
        failure_pattern_original = scan_pii(str(
            item.get('failure_pattern_original')
            or item.get('failure_pattern_text')
            or item.get('old_script_summary_original')
            or item.get('common_failed_script')
            or old_script_obj.get('text')
            or ''
        ).strip())['redacted_text'][:1200]
        failure_pattern_translation_zh = scan_pii(str(
            item.get('failure_pattern_translation_zh')
            or item.get('old_script_summary_translation_zh')
            or item.get('evidence_summary_translation_zh')
            or old_script_obj.get('translation_zh')
            or ''
        ).strip())['redacted_text'][:1200]
        interpretation_zh = scan_pii(str(
            item.get('interpretation_zh')
            or item.get('business_interpretation_zh')
            or old_script_obj.get('interpretation_zh')
            or ''
        ).strip())['redacted_text'][:500]
        normalized_candidates.append({
            'attribution_type': attribution_type,
            'attribution_zh': scan_pii(str(item.get('attribution_zh') or SCRIPT_ATTRIBUTION_LABELS.get(attribution_type) or '其他话术归因'))['redacted_text'][:120],
            'evidence_summary': scan_pii(str(item.get('evidence_summary') or ''))['redacted_text'][:500],
            'failure_pattern_original': failure_pattern_original,
            'failure_pattern_translation_zh': failure_pattern_translation_zh,
            'interpretation_zh': interpretation_zh,
            'solution': scan_pii(str(item.get('solution') or ''))['redacted_text'][:500],
            'target_metric': scan_pii(str(item.get('target_metric') or '下一步转化率'))['redacted_text'][:160],
            'suggested_message': candidate_message,
            'suggested_message_translation_zh': candidate_translation,
        })
        if len(normalized_candidates) >= 3:
            break
    if (
        not normalized_candidates
        and (new_script_obj.get('text') or replacement.get('suggested_message') or result.get('suggested_reply'))
        and (new_script_obj.get('translation_zh') or replacement.get('suggested_message_translation_zh') or result.get('suggested_reply_translation_zh'))
        and old_script_obj.get('text')
        and old_script_obj.get('translation_zh')
        and old_script_obj.get('interpretation_zh')
    ):
        normalized_candidates.append({
            'attribution_type': 'other',
            'attribution_zh': scan_pii(str(diagnosis_obj.get('root_cause_zh') or replacement.get('scenario') or DIAGNOSIS_LABELS.get(primary, primary)))['redacted_text'][:120],
            'evidence_summary': scan_pii(str(diagnosis_obj.get('evidence_summary') or ''))['redacted_text'][:500],
            'failure_pattern_original': scan_pii(str(old_script_obj.get('text') or ''))['redacted_text'][:1200],
            'failure_pattern_translation_zh': scan_pii(str(old_script_obj.get('translation_zh') or ''))['redacted_text'][:1200],
            'interpretation_zh': scan_pii(str(old_script_obj.get('interpretation_zh') or ''))['redacted_text'][:500],
            'solution': scan_pii(str(diagnosis_obj.get('solution_zh') or ''))['redacted_text'][:500],
            'target_metric': scan_pii(str((result.get('target_metric') if not isinstance(result.get('target_metric'), dict) else result.get('target_metric', {}).get('primary')) or replacement.get('target_metric') or '下一步转化率'))['redacted_text'][:160],
            'suggested_message': scan_pii(_remove_linky_form_negation(new_script_obj.get('text') or replacement.get('suggested_message') or result.get('suggested_reply') or ''))['redacted_text'][:1200],
            'suggested_message_translation_zh': scan_pii(_remove_linky_form_negation(new_script_obj.get('translation_zh') or replacement.get('suggested_message_translation_zh') or result.get('suggested_reply_translation_zh') or ''))['redacted_text'][:1200],
        })
    suggested_reply = str(result.get('suggested_reply') or new_script_obj.get('text') or replacement.get('suggested_message') or (normalized_candidates[0].get('suggested_message') if normalized_candidates else '') or '').strip()
    if suggested_reply:
        suggested_reply = scan_pii(_remove_linky_form_negation(suggested_reply))['redacted_text'][:1200]
    translation_zh = str(result.get('suggested_reply_translation_zh') or new_script_obj.get('translation_zh') or replacement.get('suggested_message_translation_zh') or (normalized_candidates[0].get('suggested_message_translation_zh') if normalized_candidates else '') or '').strip()
    if translation_zh:
        translation_zh = scan_pii(_remove_linky_form_negation(translation_zh))['redacted_text'][:1200]
    replacement_translation_zh = translation_zh if suggested_reply else ''
    risk_score = _normalize_script_risk_score(result.get('risk_score') or replacement.get('risk_score') or {})
    max_risk_score = _max_script_risk_score(risk_score)
    launch_decision = _normalize_launch_decision(
        result.get('launch_decision') or replacement.get('launch_decision'),
        risk_score,
        has_complete_script=bool(suggested_reply and replacement_translation_zh and normalized_candidates),
    )
    current_state = _normalize_funnel_state(
        result.get('current_state') or diagnosis_obj.get('current_state') or replacement.get('current_state'),
        result.get('loss_stage') or result.get('dropoff_stage') or fallback.get('dropoff_stage') or '',
    )
    user_concern_type = _normalize_user_concern_type(
        result.get('user_concern_type') or diagnosis_obj.get('user_concern_type') or replacement.get('user_concern_type'),
        result.get('user_objection') or diagnosis_obj.get('root_cause_zh') or fallback.get('user_objection') or '',
    )
    normalized_replacement = {
        'language': str(replacement.get('language') or fallback.get('language') or ''),
        'scenario': str(replacement.get('scenario') or DIAGNOSIS_LABELS.get(primary, primary)),
        'suggested_message': suggested_reply,
        'suggested_message_translation_zh': replacement_translation_zh,
        'attribution_candidates': normalized_candidates,
        'risk_score': risk_score,
        'max_risk_score': max_risk_score,
        'launch_decision': launch_decision,
        'current_state': current_state,
        'user_concern_type': user_concern_type,
    }
    experiment_plan = _script_experiment_plan(
        diagnosis_type=primary,
        dropoff_stage=str(result.get('loss_stage') or result.get('dropoff_stage') or fallback.get('dropoff_stage') or ''),
        country=(fallback.get('country') or ''),
        language=normalized_replacement['language'],
    )
    normalized_replacement.update({
        'funnel_stage': experiment_plan['funnel_stage'],
        'funnel_stage_label': experiment_plan['funnel_stage_label'],
        'target_metric': experiment_plan['target_metric'],
        'experiment_hypothesis': experiment_plan['experiment_hypothesis'],
        'experiment_design': experiment_plan['experiment_design'],
    })
    return {
        'primary_diagnosis': primary,
        'secondary_diagnoses': secondary,
        'final_outcome': str(result.get('final_outcome') or fallback.get('final_outcome') or ''),
        'dropoff_stage': str(result.get('loss_stage') or result.get('dropoff_stage') or fallback.get('dropoff_stage') or ''),
        'user_intent': scan_pii(str(result.get('user_intent') or ''))['redacted_text'][:500],
        'user_objection': scan_pii(str(result.get('user_objection') or ''))['redacted_text'][:500],
        'agent_issue': scan_pii(str(result.get('agent_issue') or DIAGNOSIS_LABELS.get(primary, primary)))['redacted_text'][:500],
        'critical_turn_index': int(result.get('critical_turn_index') or 0),
        'evidence': evidence,
        'recommended_replacement': normalized_replacement,
        'risk_score': risk_score,
        'max_risk_score': max_risk_score,
        'launch_decision': launch_decision,
        'current_state': current_state,
        'user_concern_type': user_concern_type,
        'action_type': action_type,
        'confidence': confidence,
        'needs_human_review': True,
    }


def complete_im_llm_diagnosis_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Dict[str, Any],
    provider_response: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    task = get_im_llm_diagnosis_task(conn, task_id)
    if task['status'] not in {IM_LLM_TASK_STATUS_CLAIMED, IM_LLM_TASK_STATUS_QUEUED}:
        raise ValueError('im_llm_diagnosis_task_not_completable')
    payload = task.get('payload') if isinstance(task.get('payload'), dict) else {}
    fallback = dict(payload.get('baseline_rule_diagnosis') or {})
    metrics = dict(payload.get('conversation_metrics') or {})
    attribution = dict(payload.get('attribution') or {})
    fallback.update({
        'final_outcome': metrics.get('final_outcome') or '',
        'dropoff_stage': metrics.get('dropoff_stage') or '',
        'country': attribution.get('country') or '',
        'language': attribution.get('language') or '',
    })
    normalized = _normalize_llm_diagnosis_result(dict(result or {}), fallback)
    now = _utc_now()
    diagnosis_id = _stable_id(task['diagnosis_run_id'], task['conversation_id'], HERMES_LLM_PROVIDER_MODE, normalized['primary_diagnosis'], prefix='im_diag_')
    conn.execute(
        """
        INSERT OR REPLACE INTO im_conversation_diagnoses (
            diagnosis_id, conversation_id, diagnosis_run_id, model_provider, model_name, prompt_version,
            taxonomy_version, final_outcome, dropoff_stage, primary_diagnosis, secondary_diagnoses_json,
            user_intent, user_objection, agent_issue, critical_turn_index, evidence_json,
            recommended_replacement_json, action_type, confidence, needs_human_review,
            human_review_status, human_review_comment, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            diagnosis_id,
            task['conversation_id'],
            task['diagnosis_run_id'],
            HERMES_LLM_PROVIDER_MODE,
            str((provider_response or {}).get('model') or 'hermes_llm'),
            HERMES_LLM_PROMPT_VERSION,
            TAXONOMY_VERSION,
            normalized['final_outcome'],
            normalized['dropoff_stage'],
            normalized['primary_diagnosis'],
            _json(normalized['secondary_diagnoses']),
            normalized['user_intent'],
            normalized['user_objection'],
            normalized['agent_issue'],
            normalized['critical_turn_index'],
            _json(normalized['evidence']),
            _json(normalized['recommended_replacement']),
            normalized['action_type'],
            normalized['confidence'],
            1,
            'pending',
            '',
            now,
            now,
        ),
    )
    replacement = normalized['recommended_replacement']
    suggestion_candidates = [
        item for item in list(replacement.get('attribution_candidates') or [])
        if (
            isinstance(item, dict)
            and str(item.get('suggested_message') or '').strip()
            and str(item.get('suggested_message_translation_zh') or '').strip()
            and str(item.get('failure_pattern_original') or '').strip()
            and str(item.get('failure_pattern_translation_zh') or '').strip()
            and str(item.get('interpretation_zh') or '').strip()
        )
    ]
    if (
        not suggestion_candidates
        and replacement.get('suggested_message')
        and replacement.get('suggested_message_translation_zh')
    ):
        suggestion_candidates = [{
            'attribution_zh': replacement.get('scenario') or DIAGNOSIS_LABELS.get(normalized['primary_diagnosis'], normalized['primary_diagnosis']),
            'solution': normalized.get('agent_issue') or '',
            'target_metric': replacement.get('target_metric') or '',
            'suggested_message': replacement.get('suggested_message') or '',
            'suggested_message_translation_zh': replacement.get('suggested_message_translation_zh') or '',
            'failure_pattern_original': '',
            'failure_pattern_translation_zh': '',
            'interpretation_zh': '',
        }]
    if normalized['action_type'] in {'im_script_improvement', 'im_handoff_fix'}:
        for candidate in suggestion_candidates[:3]:
            evidence = list(normalized.get('evidence') or [])
            if candidate.get('evidence_summary') or candidate.get('solution'):
                evidence = [{
                    'turn_index': normalized.get('critical_turn_index') or 0,
                    'speaker': 'history_compare',
                    'summary': '；'.join(
                        str(part or '').strip()
                        for part in [candidate.get('evidence_summary'), candidate.get('solution')]
                        if str(part or '').strip()
                    ),
                }] + evidence
            _upsert_script_suggestion(
                conn,
                diagnosis_type=normalized['primary_diagnosis'],
                country=attribution.get('country'),
                language=attribution.get('language'),
                scenario=candidate.get('attribution_zh') or replacement.get('scenario') or '',
                agent_issue=candidate.get('failure_pattern_original') or candidate.get('solution') or normalized.get('agent_issue') or '',
                suggested_message=candidate.get('suggested_message') or '',
                suggested_message_translation_zh=candidate.get('suggested_message_translation_zh') or '',
                old_script_summary_original=candidate.get('failure_pattern_original') or '',
                old_script_summary_translation_zh=candidate.get('failure_pattern_translation_zh') or '',
                old_script_summary_interpretation_zh=candidate.get('interpretation_zh') or '',
                evidence=evidence,
                conversation_id=task['conversation_id'],
                dropoff_stage=normalized.get('dropoff_stage') or '',
                source=HERMES_LLM_PROVIDER_MODE,
                now=now,
                risk_score=normalized.get('risk_score') or {},
                launch_decision=normalized.get('launch_decision') or '',
                current_state=normalized.get('current_state') or '',
                user_concern_type=normalized.get('user_concern_type') or '',
            )
    conn.execute(
        """
        UPDATE im_llm_diagnosis_tasks
        SET status = ?, lease_owner = '', lease_expires_at = '', result_json = ?, error_code = '', error_message = '',
            finished_at = ?, updated_at = ?
        WHERE task_id = ?
        """,
        (
            IM_LLM_TASK_STATUS_COMPLETED,
            _json({'diagnosis': normalized, 'provider_response': provider_response or {}}),
            now,
            now,
            task_id,
        ),
    )
    aggregate_im_diagnoses(conn, diagnosis_run_id=task['diagnosis_run_id'])
    conn.commit()
    return {'ok': True, 'task': get_im_llm_diagnosis_task(conn, task_id), 'diagnosis': normalized, 'external_write_performed': False}


def review_im_conversation_diagnosis(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    review_status: str,
    comment: str = '',
) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    normalized = str(review_status or '').strip()
    if normalized not in {'accepted', 'rejected', 'excellent_script', 'ignored', 'pending'}:
        return {'ok': False, 'detail': 'invalid_review_status'}
    now = _utc_now()
    cur = conn.execute(
        """
        UPDATE im_conversation_diagnoses
        SET human_review_status = ?, human_review_comment = ?, updated_at = ?
        WHERE diagnosis_id = (
            SELECT diagnosis_id
            FROM im_conversation_diagnoses
            WHERE conversation_id = ?
            ORDER BY created_at DESC
            LIMIT 1
        )
        """,
        (normalized, str(comment or ''), now, str(conversation_id or '')),
    )
    conn.commit()
    if cur.rowcount <= 0:
        return {'ok': False, 'detail': 'diagnosis_not_found'}
    return {'ok': True, 'conversation_id': conversation_id, 'human_review_status': normalized}


def update_im_script_suggestion_status(
    conn: sqlite3.Connection,
    *,
    script_suggestion_id: str,
    approval_status: str,
    approved_by: str = '',
) -> Dict[str, Any]:
    ensure_im_diagnostics_tables(conn)
    normalized = str(approval_status or '').strip()
    if normalized not in {'draft', 'pending_review', 'approved', 'rejected', 'testing', 'active', 'retired'}:
        return {'ok': False, 'detail': 'invalid_approval_status'}
    now = _utc_now()
    approved_at = now if normalized == 'approved' else ''
    cur = conn.execute(
        """
        UPDATE im_script_suggestions
        SET approval_status = ?,
            approved_by = ?,
            approved_at = ?,
            experiment_status = CASE WHEN ? = 'approved' THEN COALESCE(NULLIF(experiment_status, ''), 'shadow_review') ELSE experiment_status END,
            updated_at = ?
        WHERE script_suggestion_id = ?
        """,
        (normalized, str(approved_by or ''), approved_at, normalized, now, str(script_suggestion_id or '')),
    )
    if cur.rowcount <= 0:
        conn.commit()
        return {'ok': False, 'detail': 'script_suggestion_not_found'}
    experiment = None
    if normalized == 'approved':
        experiment = _create_or_refresh_script_experiment(
            conn,
            script_suggestion_id=str(script_suggestion_id or ''),
            approved_by=str(approved_by or ''),
            now=now,
        )
    conn.commit()
    return {
        'ok': True,
        'script_suggestion_id': script_suggestion_id,
        'approval_status': normalized,
        'experiment': experiment,
    }


def generate_im_diagnosis_fixtures(count: int = 100, *, start_date: str = '2026-06-27') -> Dict[str, List[Dict[str, Any]]]:
    base_date = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    scenarios = [
        ('linky_trust_explanation_missing', 'Brazil', 'pt-BR', '自巴装-广泛人群—网赚效率02'),
        ('cs_first_response_slow', 'Brazil', 'pt-BR', '自巴装-广泛人群0605—网赚效率02'),
        ('ad_promise_mismatch', 'Indonesia', 'id-ID', '网赚1'),
        ('linky_registration_guidance_failed', 'Indonesia', 'id-ID', '自印装-广泛人群—网赚效率02'),
        ('success_sample', 'Brazil', 'pt-BR', '自巴装-广泛人群—网赚效率02'),
        ('silent_user_not_reactivated', 'Indonesia', 'id-ID', '网赚1'),
        ('crm_process_issue', 'Brazil', 'pt-BR', '5'),
    ]
    conversations: List[Dict[str, Any]] = []
    messages: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for i in range(max(int(count or 0), 1)):
        scenario, country, language, ad_name = scenarios[i % len(scenarios)]
        entered = base_date + timedelta(minutes=i * 7)
        cid = f'im_mock_{base_date.strftime("%Y%m%d")}_{i:04d}'
        ad_id = _stable_id(country, ad_name, prefix='ad_')
        response_seconds = 95 if scenario == 'cs_first_response_slow' else 28
        final_outcome = 'success' if scenario == 'success_sample' else 'lost'
        conv_events = _fixture_events(cid, scenario, entered, ad_id)
        dropoff = infer_dropoff_stage(conv_events, final_outcome)
        conversations.append({
            'conversation_id': cid,
            'anonymous_user_id': _stable_id('user', i, prefix='anon_'),
            'country': country,
            'language': language,
            'media_source': 'Facebook Ads',
            'campaign_id': _stable_id(country, 'campaign', prefix='camp_'),
            'campaign_name': '自投-巴西—安装' if country == 'Brazil' else 'MIAO- IDN - 安装',
            'adset_id': _stable_id(country, 'adset', ad_name, prefix='adset_'),
            'adset_name': '广泛人群' if country == 'Brazil' else '兼职人群0605',
            'ad_id': ad_id,
            'ad_name': ad_name,
            'creative_id': _stable_id(ad_name, 'creative', prefix='creative_'),
            'ad_account_id': 'mock_meta_account',
            'entered_im_at': entered.isoformat(),
            'conversation_start_time': entered.isoformat(),
            'conversation_end_time': (entered + timedelta(minutes=12)).isoformat(),
            'first_user_message_at': (entered + timedelta(seconds=12)).isoformat(),
            'first_agent_reply_at': (entered + timedelta(seconds=12 + response_seconds)).isoformat(),
            'first_response_seconds': response_seconds,
            'final_join_status': 'succeed' if final_outcome == 'success' else 'lost',
            'final_outcome': final_outcome,
            'dropoff_stage': dropoff,
            'dropoff_time': (entered + timedelta(minutes=8)).isoformat(),
            'agent_id_hash': _stable_id('agent', i % 5, prefix='agent_'),
            'agent_team': 'mock_team',
            'agent_shift': 'day',
            'handoff_type': 'bot_automated',
            'data_quality_status': 'mock',
            'pii_scan_status': 'passed',
            'attribution_quality_status': 'mock',
        })
        messages.extend(_fixture_messages(cid, scenario, entered, language))
        events.extend(conv_events)
    return {'conversations': conversations, 'messages': messages, 'events': events}


def _fixture_events(conversation_id: str, scenario: str, entered: datetime, ad_id: str) -> List[Dict[str, Any]]:
    event_names = ['entered_im', 'auto_signup_message_sent', 'first_user_reply']
    if scenario not in {'silent_user_not_reactivated'}:
        event_names.append('im_message_ge_3')
    if scenario in {'linky_trust_explanation_missing', 'linky_registration_guidance_failed', 'success_sample', 'crm_process_issue'}:
        event_names.append('link_sent')
    if scenario in {'linky_registration_guidance_failed', 'success_sample', 'crm_process_issue'}:
        event_names.append('link_clicked')
    if scenario in {'success_sample', 'crm_process_issue'}:
        event_names.extend(['linky_registered', 'bind_succeeded'])
    if scenario == 'success_sample':
        event_names.extend(['crm_succeeded', 'real_join_succeeded'])
    rows = []
    for idx, name in enumerate(event_names):
        rows.append({
            'conversation_id': conversation_id,
            'event_name': name,
            'event_time': (entered + timedelta(minutes=idx)).isoformat(),
            'event_status': 'ok',
            'event_source': 'mock',
            'ad_id': ad_id,
        })
    return rows


def _fixture_messages(conversation_id: str, scenario: str, entered: datetime, language: str) -> List[Dict[str, Any]]:
    if scenario == 'linky_trust_explanation_missing':
        texts = [('system', 'Olá, podemos te ajudar com o cadastro.'), ('user', 'Por que preciso usar Linky? É seguro?'), ('agent_manual', 'Registre-se no Linky primeiro: https://linky.example/register')]
    elif scenario == 'cs_first_response_slow':
        texts = [('user', 'Quero começar.'), ('agent_manual', 'Oi, desculpe a demora. Você pode se cadastrar aqui.')]
    elif scenario == 'ad_promise_mismatch':
        texts = [('user', 'How much money can I earn today?'), ('agent_manual', 'Você precisa registrar antes.'), ('user', 'This seems fake.')]
    elif scenario == 'linky_registration_guidance_failed':
        texts = [('user', 'Saya sudah klik link.'), ('agent_manual', 'Daftar dulu.'), ('user', 'Bingung langkahnya.')]
    elif scenario == 'crm_process_issue':
        texts = [('user', 'Completei o cadastro.'), ('agent_manual', 'Obrigado, vou confirmar.'), ('system', 'bind ok, crm pending')]
    elif scenario == 'success_sample':
        texts = [('user', 'Quero participar.'), ('agent_manual', 'Linky confirma seus dados gratuitamente. Eu te acompanho aqui.'), ('user', 'Pronto, registrei.'), ('agent_manual', 'Perfeito, entrada confirmada.')]
    else:
        texts = [('system', 'Olá, podemos te ajudar.'), ('user', 'Oi'), ('agent_manual', 'Você ainda está aí?')]
    return [
        {
            'conversation_id': conversation_id,
            'message_index': idx,
            'sender_type': sender,
            'message_type': 'text',
            'message_at': (entered + timedelta(seconds=idx * 42)).isoformat(),
            'message_text_redacted': text,
            'language': language,
        }
        for idx, (sender, text) in enumerate(texts)
    ]
