from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional


EXECUTION_STATUS_ZH = {
    'CLEAN_EXECUTED': '干净执行',
    'PARTIALLY_EXECUTED': '部分执行',
    'NOT_EXECUTED': '未执行',
    'REVERSE_EXECUTED': '反向执行',
    'MIXED_CHANGED': '混合调整',
    'UNKNOWN': '无法判断',
}

REVIEW_STATUS_ZH = {
    'EFFECTIVE': '有效',
    'INEFFECTIVE': '无效',
    'NEUTRAL': '无明显变化',
    'INSUFFICIENT_SAMPLE': '样本不足',
    'DATA_INCOMPLETE': '数据不完整',
    'NOT_ATTRIBUTABLE': '无法归因',
    'MIXED_CHANGE': '混合调整不可归因',
    'NOT_EXECUTED': '未执行',
    'PENDING': '等待数据',
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(*parts: Any, length: int = 20) -> str:
    raw = '|'.join(str(part or '') for part in parts)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:length]


def _safe_meta_activity_error(value: Any, limit: int = 180) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    text = re.sub(r'(access_token|token|sig|signature|key)=([^&\s]+)', r'\1=[REDACTED]', text, flags=re.I)
    text = re.sub(r'https?://\S+', '[url_redacted]', text)
    return text[:limit]


@dataclass(frozen=True)
class MetaActivityChange:
    change_id: str
    object_type: str
    object_id: str
    change_type: str
    changed_at_utc: str
    before_value: Any = None
    after_value: Any = None
    actor: str = ''
    source: str = 'Meta Activity'
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecommendationExecutionRecord:
    execution_record_id: str
    recommendation_id: str
    report_id: str
    object_type: str
    object_id: str
    recommended_action: str
    recommended_value: Any
    execution_status: str
    execution_status_zh: str
    matched_change_ids: List[str]
    executed_at: str
    actual_action: str
    actual_value: Any
    variance_pct: Optional[float]
    mixed_change_flags: List[str]
    detection_version: str
    evidence: Dict[str, Any]


@dataclass(frozen=True)
class RecommendationReviewResult:
    review_result_id: str
    recommendation_id: str
    execution_record_id: str
    d1_status: str
    d2_status: str
    d3_status: str
    final_status: str
    final_status_zh: str
    baseline_window: Dict[str, str]
    post_window: Dict[str, str]
    baseline_metrics: Dict[str, Any]
    post_metrics: Dict[str, Any]
    data_quality_status: str
    dedupe_version: str
    attribution_version: str
    evaluated_at: str
    revision_reason: str = ''


def ensure_ad_production_closure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ad_activity_snapshots (
            change_id TEXT PRIMARY KEY,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            change_type TEXT NOT NULL,
            changed_at_utc TEXT NOT NULL,
            before_value TEXT,
            after_value TEXT,
            actor TEXT,
            source TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ad_object_daily_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            snapshot_date TEXT NOT NULL,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recommendation_execution_records (
            execution_record_id TEXT PRIMARY KEY,
            recommendation_id TEXT NOT NULL,
            report_id TEXT NOT NULL,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            recommended_value TEXT,
            execution_status TEXT NOT NULL,
            execution_status_zh TEXT NOT NULL,
            matched_change_ids_json TEXT NOT NULL,
            executed_at TEXT,
            actual_action TEXT,
            actual_value TEXT,
            variance_pct REAL,
            mixed_change_flags_json TEXT NOT NULL,
            detection_version TEXT NOT NULL,
            evidence_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recommendation_review_results (
            review_result_id TEXT PRIMARY KEY,
            recommendation_id TEXT NOT NULL,
            execution_record_id TEXT NOT NULL,
            d1_status TEXT NOT NULL,
            d2_status TEXT NOT NULL,
            d3_status TEXT NOT NULL,
            final_status TEXT NOT NULL,
            final_status_zh TEXT NOT NULL,
            baseline_window_json TEXT NOT NULL,
            post_window_json TEXT NOT NULL,
            baseline_metrics_json TEXT NOT NULL,
            post_metrics_json TEXT NOT NULL,
            data_quality_status TEXT NOT NULL,
            dedupe_version TEXT NOT NULL,
            attribution_version TEXT NOT NULL,
            evaluated_at TEXT NOT NULL,
            revision_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS strategy_knowledge_entries (
            knowledge_id TEXT PRIMARY KEY,
            entry_type TEXT NOT NULL,
            report_id TEXT,
            recommendation_id TEXT,
            execution_record_id TEXT,
            review_result_id TEXT,
            asset_id TEXT,
            generated_image_id TEXT,
            ad_id TEXT,
            creative_id TEXT,
            evidence_window TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            review_status TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS trg_strategy_knowledge_entries_no_insert
        BEFORE INSERT ON strategy_knowledge_entries
        BEGIN
            SELECT RAISE(ABORT, 'strategy_knowledge_entries_read_only_use_growth_strategy_knowledge');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_strategy_knowledge_entries_no_update
        BEFORE UPDATE ON strategy_knowledge_entries
        BEGIN
            SELECT RAISE(ABORT, 'strategy_knowledge_entries_read_only_use_growth_strategy_knowledge');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_strategy_knowledge_entries_no_delete
        BEFORE DELETE ON strategy_knowledge_entries
        BEGIN
            SELECT RAISE(ABORT, 'strategy_knowledge_entries_read_only_use_growth_strategy_knowledge');
        END;
        """
    )


def persist_activity_changes(conn: sqlite3.Connection, changes: Iterable[MetaActivityChange]) -> int:
    ensure_ad_production_closure_tables(conn)
    count = 0
    for change in changes:
        conn.execute(
            """
            INSERT OR REPLACE INTO ad_activity_snapshots
            (change_id, object_type, object_id, change_type, changed_at_utc, before_value, after_value, actor, source, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                change.change_id,
                change.object_type,
                change.object_id,
                change.change_type,
                change.changed_at_utc,
                json.dumps(change.before_value, ensure_ascii=False),
                json.dumps(change.after_value, ensure_ascii=False),
                change.actor,
                change.source,
                json.dumps(change.payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        count += 1
    conn.commit()
    return count


class MetaActivityReadonlyService:
    def __init__(
        self,
        *,
        token: str = '',
        account_ids: Optional[List[str]] = None,
        api_version: str = 'v25.0',
        base_url: str = 'https://graph.facebook.com',
        session: Optional[Any] = None,
        page_size: int = 100,
        enabled: bool = False,
    ) -> None:
        self.token = str(token or '').strip()
        self.account_ids = [
            str(item or '').strip().replace('act_', '')
            for item in (account_ids or [])
            if str(item or '').strip()
        ]
        self.api_version = str(api_version or 'v25.0').strip().strip('/') or 'v25.0'
        self.base_url = str(base_url or 'https://graph.facebook.com').strip().rstrip('/') or 'https://graph.facebook.com'
        self.session = session
        self.page_size = max(1, min(int(page_size or 100), 500))
        self.enabled = bool(enabled)

    def readiness(self) -> Dict[str, Any]:
        blocking_reasons = []
        if not self.enabled:
            blocking_reasons.append('meta_activity_sync_disabled')
        if not self.token:
            blocking_reasons.append('meta_token_missing')
        if not self.account_ids:
            blocking_reasons.append('meta_account_ids_missing')
        if self.session is None:
            blocking_reasons.append('http_session_missing')
        return {
            'enabled': self.enabled,
            'token_configured': bool(self.token),
            'account_ids_configured': bool(self.account_ids),
            'account_count': len(self.account_ids),
            'api_version': self.api_version,
            'base_url_configured': bool(self.base_url),
            'session_configured': self.session is not None,
            'ready': not blocking_reasons,
            'blocking_reasons': blocking_reasons,
            'mode': 'meta_activity_readonly' if not blocking_reasons else 'not_ready',
        }

    @staticmethod
    def _change_type_from_activity(row: Dict[str, Any]) -> str:
        text = ' '.join([
            str(row.get('event_type') or ''),
            str(row.get('translated_event_type') or ''),
            str(row.get('extra_data') or ''),
        ]).lower()
        if 'creative' in text or 'adcreative' in text:
            return 'creative_changed'
        if 'budget' in text and any(word in text for word in {'increase', 'increased', 'raise', 'raised', '提高', '增加'}):
            return 'budget_increase'
        if 'budget' in text and any(word in text for word in {'decrease', 'decreased', 'lower', 'lowered', '减少', '降低'}):
            return 'budget_decrease'
        if any(word in text for word in {'pause', 'paused', 'stop', 'stopped', '暂停'}):
            return 'pause'
        if any(word in text for word in {'enable', 'enabled', 'start', 'started', 'active', '开启', '启动'}):
            return 'enable'
        if 'budget' in text:
            return 'budget_changed'
        if 'status' in text:
            return 'status_changed'
        return str(row.get('event_type') or 'meta_activity').strip() or 'meta_activity'

    @staticmethod
    def _object_type_from_activity(row: Dict[str, Any]) -> str:
        raw = str(row.get('object_type') or row.get('object') or '').strip().lower()
        if raw in {'ad', 'adset', 'ad_set', 'campaign', 'creative'}:
            return 'ad_set' if raw == 'adset' else raw
        object_id = str(row.get('object_id') or row.get('id') or '').strip().lower()
        if object_id.startswith('act_'):
            return 'account'
        return raw or 'unknown'

    def _activity_row_to_change(self, account_id: str, row: Dict[str, Any]) -> MetaActivityChange:
        object_id = str(row.get('object_id') or row.get('id') or '').strip()
        changed_at = str(row.get('event_time') or row.get('event_time_utc') or row.get('created_time') or _utc_now()).strip()
        event_type = self._change_type_from_activity(row)
        extra = row.get('extra_data') if isinstance(row.get('extra_data'), dict) else {}
        before_value = extra.get('old_value') if isinstance(extra, dict) else None
        after_value = extra.get('new_value') if isinstance(extra, dict) else None
        change_id = f'meta_activity_{_stable_id(account_id, object_id, event_type, changed_at, json.dumps(row, ensure_ascii=False, sort_keys=True))}'
        return MetaActivityChange(
            change_id=change_id,
            object_type=self._object_type_from_activity(row),
            object_id=object_id,
            change_type=event_type,
            changed_at_utc=changed_at,
            before_value=before_value,
            after_value=after_value,
            actor=str(row.get('actor_name') or row.get('actor_id') or ''),
            source='Meta Activity',
            payload={'account_id': account_id, **row},
        )

    def sync(self, *, since: str = '', until: str = '') -> Dict[str, Any]:
        readiness = self.readiness()
        if not readiness['ready']:
            return {'ok': False, 'mode': readiness['mode'], 'synced_count': 0, 'changes': [], 'errors': readiness['blocking_reasons']}
        changes: List[MetaActivityChange] = []
        errors: List[str] = []
        fields = 'event_time,event_type,translated_event_type,object_id,object_name,object_type,actor_name,extra_data'
        for account_id in self.account_ids:
            url = f'{self.base_url}/{self.api_version}/act_{account_id}/activities'
            params: Optional[Dict[str, Any]] = {
                'fields': fields,
                'limit': self.page_size,
                'access_token': self.token,
            }
            if since:
                params['since'] = since
            if until:
                params['until'] = until
            while url:
                try:
                    response = self.session.get(url, params=params, timeout=30)
                    params = None
                    if getattr(response, 'status_code', 200) >= 400:
                        errors.append(f'{account_id}:{_safe_meta_activity_error(getattr(response, "text", ""))}')
                        break
                    body = response.json()
                except Exception as exc:
                    errors.append(f'{account_id}:{_safe_meta_activity_error(exc.__class__.__name__)}')
                    break
                for row in body.get('data') or []:
                    if isinstance(row, dict):
                        changes.append(self._activity_row_to_change(account_id, row))
                url = ((body.get('paging') or {}).get('next') or '').strip()
        return {
            'ok': not errors,
            'mode': 'meta_activity_readonly',
            'synced_count': len(changes),
            'changes': changes,
            'errors': errors,
        }


def classify_recommendation_execution(
    recommendation: Dict[str, Any],
    changes: Iterable[MetaActivityChange],
    *,
    report_id: str,
    tolerance_pct: float = 5.0,
    execution_window_hours: int = 36,
) -> RecommendationExecutionRecord:
    object_id = str(recommendation.get('object_id') or '')
    object_type = str(recommendation.get('object_level') or recommendation.get('object_type') or 'ad')
    action = str(recommendation.get('primary_action') or '')
    recommended_pct = float(recommendation.get('adjustment_pct') or 0.0)
    created_at_text = str(recommendation.get('created_at_utc') or _utc_now())
    try:
        created_at = datetime.fromisoformat(created_at_text)
    except Exception:
        created_at = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    latest_at = created_at + timedelta(hours=max(1, int(execution_window_hours or 36)))
    matched: List[MetaActivityChange] = []
    mixed_flags: List[str] = []
    for change in changes:
        if str(change.object_id) != object_id or str(change.object_type or object_type) != object_type:
            continue
        try:
            changed_at = datetime.fromisoformat(change.changed_at_utc)
        except Exception:
            continue
        if changed_at.tzinfo is None:
            changed_at = changed_at.replace(tzinfo=timezone.utc)
        if created_at <= changed_at <= latest_at:
            matched.append(change)
            if change.change_type not in {'budget_increase', 'budget_decrease', 'pause', 'enable'}:
                mixed_flags.append(change.change_type)
    status = 'NOT_EXECUTED'
    actual_action = ''
    actual_value: Any = None
    variance: Optional[float] = None
    budget_changes = [change for change in matched if change.change_type in {'budget_increase', 'budget_decrease'}]
    pause_changes = [change for change in matched if change.change_type == 'pause']
    if action == 'scale_up' and budget_changes:
        actual_action = budget_changes[0].change_type
        actual_value = budget_changes[0].payload.get('change_pct') if budget_changes[0].payload else None
        variance = abs(float(actual_value or 0.0) - recommended_pct)
        status = 'CLEAN_EXECUTED' if actual_action == 'budget_increase' and variance <= tolerance_pct else 'PARTIALLY_EXECUTED'
        if actual_action == 'budget_decrease':
            status = 'REVERSE_EXECUTED'
    elif action == 'reduce_budget' and budget_changes:
        actual_action = budget_changes[0].change_type
        actual_value = budget_changes[0].payload.get('change_pct') if budget_changes[0].payload else None
        variance = abs(float(actual_value or 0.0) - recommended_pct)
        status = 'CLEAN_EXECUTED' if actual_action == 'budget_decrease' and variance <= tolerance_pct else 'PARTIALLY_EXECUTED'
        if actual_action == 'budget_increase':
            status = 'REVERSE_EXECUTED'
    elif action == 'pause' and pause_changes:
        actual_action = 'pause'
        actual_value = True
        variance = 0.0
        status = 'CLEAN_EXECUTED'
    elif matched:
        status = 'UNKNOWN'
        actual_action = matched[0].change_type
        actual_value = matched[0].after_value
    if mixed_flags and status in {'CLEAN_EXECUTED', 'PARTIALLY_EXECUTED'}:
        status = 'MIXED_CHANGED'
    execution_record_id = f'exec_{_stable_id(report_id, recommendation.get("recommendation_id"), status, ",".join(change.change_id for change in matched))}'
    return RecommendationExecutionRecord(
        execution_record_id=execution_record_id,
        recommendation_id=str(recommendation.get('recommendation_id') or ''),
        report_id=report_id,
        object_type=object_type,
        object_id=object_id,
        recommended_action=action,
        recommended_value=recommended_pct,
        execution_status=status,
        execution_status_zh=EXECUTION_STATUS_ZH.get(status, status),
        matched_change_ids=[change.change_id for change in matched],
        executed_at=matched[0].changed_at_utc if matched else '',
        actual_action=actual_action,
        actual_value=actual_value,
        variance_pct=variance,
        mixed_change_flags=sorted(set(mixed_flags)),
        detection_version='ad_execution_detection_v1',
        evidence={'execution_window_hours': execution_window_hours, 'tolerance_pct': tolerance_pct},
    )


def persist_execution_record(conn: sqlite3.Connection, record: RecommendationExecutionRecord) -> None:
    ensure_ad_production_closure_tables(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO recommendation_execution_records
        (execution_record_id, recommendation_id, report_id, object_type, object_id, recommended_action, recommended_value,
         execution_status, execution_status_zh, matched_change_ids_json, executed_at, actual_action, actual_value,
         variance_pct, mixed_change_flags_json, detection_version, evidence_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.execution_record_id,
            record.recommendation_id,
            record.report_id,
            record.object_type,
            record.object_id,
            record.recommended_action,
            json.dumps(record.recommended_value, ensure_ascii=False),
            record.execution_status,
            record.execution_status_zh,
            json.dumps(record.matched_change_ids, ensure_ascii=False),
            record.executed_at,
            record.actual_action,
            json.dumps(record.actual_value, ensure_ascii=False),
            record.variance_pct,
            json.dumps(record.mixed_change_flags, ensure_ascii=False),
            record.detection_version,
            json.dumps(record.evidence, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()


def evaluate_recommendation_outcome(
    record: RecommendationExecutionRecord,
    *,
    baseline_metrics: Dict[str, Any],
    post_metrics: Dict[str, Any],
    country_cap: Optional[float],
    data_quality_status: str,
    dedupe_version: str,
    attribution_version: str,
) -> RecommendationReviewResult:
    if record.execution_status == 'NOT_EXECUTED':
        final_status = 'NOT_EXECUTED'
    elif record.execution_status == 'MIXED_CHANGED':
        final_status = 'MIXED_CHANGE'
    elif data_quality_status != 'PASS':
        final_status = 'DATA_INCOMPLETE'
    else:
        before_binds = float(baseline_metrics.get('real_bind_count') or 0)
        after_binds = float(post_metrics.get('real_bind_count') or 0)
        before_cpa = baseline_metrics.get('real_bind_cpa')
        after_cpa = post_metrics.get('real_bind_cpa')
        if before_binds < 10 or after_binds < 10:
            final_status = 'INSUFFICIENT_SAMPLE'
        elif record.recommended_action == 'scale_up' and after_binds >= before_binds * 1.1 and (country_cap is None or (after_cpa is not None and float(after_cpa) <= float(country_cap))):
            final_status = 'EFFECTIVE'
        elif after_cpa is not None and before_cpa is not None and float(after_cpa) > float(before_cpa) * 1.2:
            final_status = 'INEFFECTIVE'
        else:
            final_status = 'NEUTRAL'
    return RecommendationReviewResult(
        review_result_id=f'review_{_stable_id(record.execution_record_id, final_status)}',
        recommendation_id=record.recommendation_id,
        execution_record_id=record.execution_record_id,
        d1_status='PENDING',
        d2_status='PENDING',
        d3_status=final_status,
        final_status=final_status,
        final_status_zh=REVIEW_STATUS_ZH.get(final_status, final_status),
        baseline_window=dict(baseline_metrics.get('window') or {}),
        post_window=dict(post_metrics.get('window') or {}),
        baseline_metrics=baseline_metrics,
        post_metrics=post_metrics,
        data_quality_status=data_quality_status,
        dedupe_version=dedupe_version,
        attribution_version=attribution_version,
        evaluated_at=_utc_now(),
    )


def persist_review_result(conn: sqlite3.Connection, result: RecommendationReviewResult) -> None:
    ensure_ad_production_closure_tables(conn)
    from app.growth.schema import ensure_growth_schema

    ensure_growth_schema(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO recommendation_review_results
        (review_result_id, recommendation_id, execution_record_id, d1_status, d2_status, d3_status, final_status,
         final_status_zh, baseline_window_json, post_window_json, baseline_metrics_json, post_metrics_json,
         data_quality_status, dedupe_version, attribution_version, evaluated_at, revision_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.review_result_id,
            result.recommendation_id,
            result.execution_record_id,
            result.d1_status,
            result.d2_status,
            result.d3_status,
            result.final_status,
            result.final_status_zh,
            json.dumps(result.baseline_window, ensure_ascii=False, sort_keys=True),
            json.dumps(result.post_window, ensure_ascii=False, sort_keys=True),
            json.dumps(result.baseline_metrics, ensure_ascii=False, sort_keys=True),
            json.dumps(result.post_metrics, ensure_ascii=False, sort_keys=True),
            result.data_quality_status,
            result.dedupe_version,
            result.attribution_version,
            result.evaluated_at,
            result.revision_reason,
        ),
    )
    episode = conn.execute(
        """
        SELECT e.episode_id, e.status
        FROM growth_decision_episode e
        JOIN growth_decision d ON d.decision_id=e.decision_id
        WHERE d.recommendation_id=?
        ORDER BY e.created_at DESC
        LIMIT 1
        """,
        (result.recommendation_id,),
    ).fetchone()
    if episode and episode['status'] == 'WAITING_OUTCOME':
        from app.growth.episode_service import EpisodeService

        EpisodeService(conn).transition(
            episode['episode_id'],
            'OUTCOME_READY',
            outcome={
                'review_result_id': result.review_result_id,
                'execution_record_id': result.execution_record_id,
                'final_status': result.final_status,
                'baseline_window': result.baseline_window,
                'post_window': result.post_window,
                'baseline_metrics': result.baseline_metrics,
                'post_metrics': result.post_metrics,
                'data_quality_status': result.data_quality_status,
            },
            reason='ad_production_closure_review',
            actor='system',
        )
    conn.commit()
