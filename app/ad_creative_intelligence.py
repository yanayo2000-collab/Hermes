from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.meta_ad_account_access import MetaAdAccountAccessPolicy, access_summary


CREATIVE_INTELLIGENCE_SCHEMA_VERSION = 'ad_creative_intelligence_v1'

CREATIVE_FEATURE_FLAGS: Dict[str, bool] = {
    'AD_CREATIVE_SYNC_ENABLED': False,
    'AD_CREATIVE_MEDIA_INGESTION_ENABLED': False,
    'AD_CREATIVE_IMAGE_ANALYSIS_ENABLED': False,
    'AD_CREATIVE_VIDEO_FRAME_ANALYSIS_ENABLED': False,
    'AD_CREATIVE_OCR_ENABLED': False,
    'AD_CREATIVE_DIAGNOSIS_ENABLED': False,
    'AD_CREATIVE_EXPERIMENT_PLANNER_ENABLED': False,
    'AD_CREATIVE_KNOWLEDGE_SYNC_ENABLED': False,
}

CREATIVE_FORMATS = {
    '真人口播', '真人场景', 'App 界面', '聊天截图', '收益截图', '入会流程说明',
    '教程步骤图', '纯文字海报', '对比图', 'UGC 风格', '模板海报', '轮播组合', '未识别',
}
HOOK_TYPES = {
    '收入吸引', '兼职机会', '低门槛加入', '新手教程', '真实案例', '平台背书',
    '公会支持', '限时机会', '流程透明', '问题痛点', '结果展示', '未识别',
}
VALUE_PROPOSITIONS = {
    '有运营指导', '入驻流程简单', '可获得公会支持', '新人友好', '本地语言支持',
    '真实案例', '收益潜力', '社群氛围', '快速申请', '未识别',
}
RISK_TAGS = {
    '收益承诺过强', '金额刺激明显', '像钓鱼 / 诈骗', '信息过于夸张', '文字过密',
    '手机端可读性差', '疑似敏感个人信息', 'WhatsApp / 手机号外露', '截图可信度低', '无明显风险',
}
TRUST_SIGNAL_TAGS = {'真人出镜', '流程展示', 'App 真实界面', '社群证明', '案例说明', '明确 CTA', '无可信度信号'}

ATTRIBUTION_GRAIN_ZH = {
    'asset': '素材级',
    'ad': '广告级',
    'adset': '广告组级',
    'campaign': '广告系列级',
    'dynamic': '动态素材组合级',
    'unknown': '无法判断',
}

DIAGNOSIS_ZH = {
    'winner_extension': '优先延展',
    'funnel_repair': '漏斗修复',
    'controlled_exploration': '受控探索',
    'manual_review': '人工复核',
}

PII_PATTERNS = [
    re.compile(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', re.I),
    re.compile(r'(?:\+?\d[\s-]?){8,16}'),
    re.compile(r'whats\s*app|wa\s*[:：]|\bWA\b', re.I),
]


def stable_id(*parts: Any, length: int = 20) -> str:
    raw = '|'.join(str(part or '') for part in parts)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:length]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_domain(url: Any) -> str:
    try:
        return urlparse(str(url or '')).netloc.lower()
    except Exception:
        return ''


def normalize_feature_flags(config: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
    config = config or {}
    flags = dict(CREATIVE_FEATURE_FLAGS)
    for key in list(flags):
        if key in config:
            raw = config.get(key)
            flags[key] = raw if isinstance(raw, bool) else str(raw or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    return flags


def safe_error_reason(value: Any, limit: int = 180) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    text = re.sub(r'(access_token|token|sig|signature|key)=([^&\s]+)', r'\1=[REDACTED]', text, flags=re.I)
    text = re.sub(r'https?://\S+', '[url_redacted]', text)
    return text[:limit]


def safe_media_url(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        parsed = urlparse(text)
    except Exception:
        return ''
    if parsed.scheme and parsed.scheme not in {'http', 'https'}:
        return ''
    if parsed.query:
        query = [
            (key, val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in {'access_token', 'appsecret_proof'}
        ]
        text = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
    return text


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or '').strip()
        if text:
            return text
    return ''


def _compact_text(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '').strip())


def _append_copy_fragment(
    fragments: List[Dict[str, str]],
    seen: set,
    *,
    role: str,
    source: str,
    field: str,
    text: Any,
) -> None:
    cleaned = _compact_text(text)
    if not cleaned:
        return
    key = cleaned.lower()
    if key in seen:
        return
    seen.add(key)
    fragments.append({
        'role': str(role or 'copy'),
        'source': str(source or ''),
        'field': str(field or ''),
        'text': cleaned,
    })


def _iter_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item


def _fragments_by_role(fragments: Iterable[Dict[str, str]], role: str) -> List[str]:
    return [str(item.get('text') or '').strip() for item in fragments if str(item.get('role') or '') == role and str(item.get('text') or '').strip()]


def join_copy_fragments(values: Iterable[str], *, max_items: int = 12, max_chars: int = 2000) -> str:
    parts: List[str] = []
    seen = set()
    for value in values:
        text = _compact_text(value)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        parts.append(text)
        if len(parts) >= max_items:
            break
    return '\n'.join(parts)[:max_chars]


def extract_meta_creative_copy_fragments(
    *,
    ad_row: Optional[Dict[str, Any]] = None,
    creative: Optional[Dict[str, Any]] = None,
    story_media: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """Extract every readable copy fragment Meta exposes for a creative.

    Meta spreads copy across creative fields, object_story_spec, story/post
    attachments, and dynamic creative asset_feed_spec. This function keeps all
    distinct readable fragments with source metadata instead of collapsing them
    too early.
    """
    ad_row = ad_row or {}
    creative = creative or {}
    story = dict_get(creative, 'object_story_spec') or {}
    link_data = dict_get(story, 'link_data') or {}
    video_data = dict_get(story, 'video_data') or {}
    photo_data = dict_get(story, 'photo_data') or {}
    template_data = dict_get(story, 'template_data') or {}
    asset_feed_spec = dict_get(creative, 'asset_feed_spec') or {}
    if not isinstance(asset_feed_spec, dict):
        asset_feed_spec = {}
    story_media = story_media or {}
    fragments: List[Dict[str, str]] = []
    seen: set = set()

    _append_copy_fragment(fragments, seen, role='name', source='ad', field='name', text=ad_row.get('name'))
    _append_copy_fragment(fragments, seen, role='name', source='creative', field='name', text=creative.get('name'))
    _append_copy_fragment(fragments, seen, role='body', source='creative', field='body', text=creative.get('body'))
    _append_copy_fragment(fragments, seen, role='title', source='creative', field='title', text=creative.get('title'))

    for source, data in (
        ('object_story_spec.link_data', link_data),
        ('object_story_spec.video_data', video_data),
        ('object_story_spec.photo_data', photo_data),
        ('object_story_spec.template_data', template_data),
    ):
        for field in ('message', 'caption'):
            _append_copy_fragment(fragments, seen, role='body', source=source, field=field, text=dict_get(data, field))
        for field in ('name', 'title'):
            _append_copy_fragment(fragments, seen, role='title', source=source, field=field, text=dict_get(data, field))
        for field in ('description', 'link_description'):
            _append_copy_fragment(fragments, seen, role='description', source=source, field=field, text=dict_get(data, field))
        cta = dict_get(data, 'call_to_action') or {}
        if isinstance(cta, dict):
            _append_copy_fragment(fragments, seen, role='cta', source=f'{source}.call_to_action', field='type', text=cta.get('type'))
            cta_value = cta.get('value') if isinstance(cta.get('value'), dict) else {}
            for field in ('link_title', 'link_caption', 'app_link', 'lead_gen_form_id'):
                _append_copy_fragment(fragments, seen, role='cta', source=f'{source}.call_to_action.value', field=field, text=cta_value.get(field) if isinstance(cta_value, dict) else '')

    for child in _iter_dicts(link_data.get('child_attachments') or []):
        for field in ('message', 'caption'):
            _append_copy_fragment(fragments, seen, role='body', source='object_story_spec.link_data.child_attachments', field=field, text=child.get(field))
        for field in ('name', 'title'):
            _append_copy_fragment(fragments, seen, role='title', source='object_story_spec.link_data.child_attachments', field=field, text=child.get(field))
        for field in ('description', 'link_description'):
            _append_copy_fragment(fragments, seen, role='description', source='object_story_spec.link_data.child_attachments', field=field, text=child.get(field))

    dynamic_roles = {
        'bodies': 'body',
        'titles': 'title',
        'descriptions': 'description',
        'link_urls': 'landing_url',
        'call_to_action_types': 'cta',
    }
    for collection, role in dynamic_roles.items():
        for index, item in enumerate(_iter_dicts(asset_feed_spec.get(collection) or [])):
            for field in ('text', 'name', 'title', 'description', 'type', 'website_url', 'display_url'):
                _append_copy_fragment(
                    fragments,
                    seen,
                    role=role,
                    source=f'asset_feed_spec.{collection}[{index}]',
                    field=field,
                    text=item.get(field),
                )
        if isinstance(asset_feed_spec.get(collection), list):
            for index, raw in enumerate(asset_feed_spec.get(collection) or []):
                if not isinstance(raw, dict):
                    _append_copy_fragment(
                        fragments,
                        seen,
                        role=role,
                        source=f'asset_feed_spec.{collection}[{index}]',
                        field='value',
                        text=raw,
                    )

    attachments = story_media.get('attachments') or {}
    for attachment in _iter_dicts(attachments.get('data') if isinstance(attachments, dict) else attachments):
        for field in ('title', 'name'):
            _append_copy_fragment(fragments, seen, role='title', source='story.attachments', field=field, text=attachment.get(field))
        for field in ('description', 'caption'):
            _append_copy_fragment(fragments, seen, role='description', source='story.attachments', field=field, text=attachment.get(field))
        target = attachment.get('target') if isinstance(attachment.get('target'), dict) else {}
        _append_copy_fragment(fragments, seen, role='landing_url', source='story.attachments.target', field='url', text=target.get('url') if isinstance(target, dict) else '')
        subattachments = attachment.get('subattachments') if isinstance(attachment.get('subattachments'), dict) else {}
        for child in _iter_dicts(subattachments.get('data') if isinstance(subattachments, dict) else subattachments):
            for field in ('title', 'name'):
                _append_copy_fragment(fragments, seen, role='title', source='story.subattachments', field=field, text=child.get(field))
            for field in ('description', 'caption'):
                _append_copy_fragment(fragments, seen, role='description', source='story.subattachments', field=field, text=child.get(field))

    return fragments


def dict_get(data: Any, key: str) -> Any:
    return data.get(key) if isinstance(data, dict) else None


def collect_meta_media_candidates(value: Any) -> List[str]:
    candidates: List[str] = []
    media_keys = {
        'thumbnail_url', 'picture', 'full_picture', 'image_url', 'url', 'source',
        'media_url', 'original_image_url',
    }

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, raw in item.items():
                if key in media_keys:
                    cleaned = safe_media_url(raw)
                    if cleaned and cleaned not in candidates:
                        candidates.append(cleaned)
                elif key in {'hash', 'image_hash', 'video_id', 'id'}:
                    continue
                else:
                    visit(raw)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return candidates


def meta_thumbnail_needs_image_hash_fallback(value: Any) -> bool:
    url = safe_media_url(value)
    if not url:
        return True
    try:
        parsed = urlparse(url)
    except Exception:
        return True
    host = str(parsed.hostname or '').lower()
    path = str(parsed.path or '').lower()
    return host.startswith('external-') or '/emg1/' in path


def extract_meta_creative_media(creative: Dict[str, Any], story_media: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    story = dict_get(creative, 'object_story_spec') or {}
    link_data = dict_get(story, 'link_data') or {}
    video_data = dict_get(story, 'video_data') or {}
    photo_data = dict_get(story, 'photo_data') or {}
    template_data = dict_get(story, 'template_data') or {}
    asset_feed_spec = dict_get(creative, 'asset_feed_spec') or {}
    if not isinstance(asset_feed_spec, dict):
        asset_feed_spec = {}
    story_media = story_media or {}

    thumbnail_candidates = [
        creative.get('thumbnail_url'),
        link_data.get('picture'),
        video_data.get('image_url'),
        video_data.get('picture'),
        photo_data.get('url'),
        photo_data.get('picture'),
        template_data.get('picture'),
    ]
    thumbnail_candidates.extend(collect_meta_media_candidates(asset_feed_spec))
    thumbnail_candidates.extend(collect_meta_media_candidates(story_media))

    image_candidates = [
        creative.get('image_url'),
        link_data.get('image_url'),
        photo_data.get('url'),
    ]
    image_candidates.extend(collect_meta_media_candidates(link_data.get('child_attachments') or []))
    image_candidates.extend(collect_meta_media_candidates(asset_feed_spec))
    image_candidates.extend(collect_meta_media_candidates(story_media))

    video_id = first_non_empty(
        creative.get('video_id'),
        video_data.get('video_id'),
        dict_get((asset_feed_spec.get('videos') or [{}])[0] if isinstance(asset_feed_spec.get('videos'), list) else {}, 'video_id'),
    )
    return {
        'thumbnail_url': safe_media_url(first_non_empty(*thumbnail_candidates, *image_candidates)),
        'image_url': safe_media_url(first_non_empty(*image_candidates)),
        'image_hash': first_non_empty(
            creative.get('image_hash'),
            link_data.get('image_hash'),
            dict_get((asset_feed_spec.get('images') or [{}])[0] if isinstance(asset_feed_spec.get('images'), list) else {}, 'hash'),
        ),
        'video_id': video_id,
    }


@dataclass(frozen=True)
class AdCreativeAsset:
    asset_id: str
    platform: str
    account_id: str
    campaign_id: str
    adset_id: str
    ad_id: str
    creative_id: str
    asset_type: str
    media_source_type: str
    ad_name: str = ''
    image_hash: str = ''
    video_id: str = ''
    thumbnail_url: str = ''
    local_media_ref: str = ''
    source_image_url: str = ''
    source_image_local_ref: str = ''
    source_image_hash: str = ''
    source_image_width: int = 0
    source_image_height: int = 0
    source_image_quality: str = ''
    source_image_origin: str = ''
    body_text: str = ''
    title_text: str = ''
    description_text: str = ''
    copy_fragments_json: str = '[]'
    cta_type: str = ''
    landing_url_domain: str = ''
    country: str = ''
    project: str = ''
    language_hint: str = ''
    first_seen_at: str = ''
    last_seen_at: str = ''
    status: str = 'active'
    content_hash: str = ''
    sync_status: str = 'synced'
    sync_error_reason: str = ''
    created_at: str = ''
    updated_at: str = ''
    is_dynamic_creative: bool = False


@dataclass(frozen=True)
class AdCreativeAssetLink:
    asset_id: str
    ad_id: str
    adset_id: str
    campaign_id: str
    link_type: str
    first_active_date: str
    last_active_date: str
    status: str = 'active'


@dataclass(frozen=True)
class CreativeVisualAnalysis:
    visual_tags: List[str] = field(default_factory=list)
    creative_format: str = '未识别'
    hook_type: str = '未识别'
    trust_signal_tags: List[str] = field(default_factory=list)
    risk_tags: List[str] = field(default_factory=lambda: ['无明显风险'])
    confidence: float = 0.45


@dataclass(frozen=True)
class CreativeOcrResult:
    ocr_text: str = ''
    risk_tags: List[str] = field(default_factory=list)
    confidence: float = 0.4


@dataclass(frozen=True)
class CreativeCopyAnalysis:
    copy_tags: List[str] = field(default_factory=list)
    language_detected: str = '未识别'
    localization_score: float = 0.0
    value_proposition_tags: List[str] = field(default_factory=list)
    readability_score: float = 0.0
    quality_flags: List[str] = field(default_factory=list)
    risk_tags: List[str] = field(default_factory=list)
    confidence: float = 0.5


@dataclass(frozen=True)
class AdCreativeAnalysis:
    analysis_id: str
    asset_id: str
    analysis_version: str
    analysis_status: str
    analyzer_type: str
    analyzed_at: str
    visual_tags: List[str] = field(default_factory=list)
    ocr_text: str = ''
    copy_tags: List[str] = field(default_factory=list)
    language_detected: str = '未识别'
    localization_score: float = 0.0
    risk_tags: List[str] = field(default_factory=list)
    creative_format: str = '未识别'
    hook_type: str = '未识别'
    value_proposition_tags: List[str] = field(default_factory=list)
    trust_signal_tags: List[str] = field(default_factory=list)
    readability_score: float = 0.0
    quality_flags: List[str] = field(default_factory=list)
    confidence: float = 0.0
    failure_reason: str = ''


@dataclass(frozen=True)
class AdCreativeFrameAnalysis:
    frame_id: str
    asset_id: str
    video_id: str
    timestamp_ms: int
    frame_ref: str
    ocr_text: str = ''
    visual_tags: List[str] = field(default_factory=list)
    hook_tags: List[str] = field(default_factory=list)
    cta_tags: List[str] = field(default_factory=list)
    risk_tags: List[str] = field(default_factory=list)
    analysis_status: str = 'pending'


@dataclass(frozen=True)
class AdCreativePerformanceDaily:
    report_date_london: str
    asset_id: str
    creative_id: str
    ad_id: str
    adset_id: str
    campaign_id: str
    country: str
    project: str
    spend: float
    impressions: float
    clicks: float
    ctr: float
    cpm: float
    installs: float
    cpi: Optional[float]
    af_model_join_events: float
    tugao_real_bind_count: int
    real_bind_cpa: Optional[float]
    af_to_real_bind_rate: Optional[float]
    data_quality_status: str
    attribution_level: str
    creative_grain: str
    is_dynamic_creative: bool
    grain_warning: str


@dataclass(frozen=True)
class CreativeDirectionInsight:
    direction: str
    spend: float
    ctr: float
    cpi: Optional[float]
    af_model_join_events: float
    tugao_real_bind_count: int
    real_bind_cpa: Optional[float]
    judgment: str
    next_step: str
    attribution_grain: str
    confidence: float


@dataclass(frozen=True)
class CreativeExperimentPlan:
    plan_id: str
    experiment_name: str
    country: str
    project: str
    current_direction: str
    current_problem: str
    hypothesis: str
    control_asset_id: str
    changed_variable: str
    suggested_format: str
    hook_type: str
    visual_brief: str
    localized_copy: str
    chinese_meaning: str
    success_metric: str
    minimum_sample: str
    stop_condition: str
    data_window: str
    linked_ads: List[str]
    enter_knowledge_base: bool
    plan_type: str


class CreativeVisionAnalyzer(Protocol):
    def analyze_image(self, image_ref: str, context: Dict[str, Any]) -> CreativeVisualAnalysis:
        ...


class CreativeOcrAnalyzer(Protocol):
    def extract_text(self, image_ref: str, context: Dict[str, Any]) -> CreativeOcrResult:
        ...


class CreativeTextAnalyzer(Protocol):
    def analyze_copy(self, body: str, title: str, description: str, cta: str, context: Dict[str, Any]) -> CreativeCopyAnalysis:
        ...


class FixtureCreativeAnalyzer:
    analyzer_type = 'fixture'

    def analyze_image(self, image_ref: str, context: Dict[str, Any]) -> CreativeVisualAnalysis:
        text = ' '.join(str(context.get(key) or '') for key in ('body_text', 'title_text', 'description_text', 'asset_type')).lower()
        tags: List[str] = []
        creative_format = '未识别'
        hook = '未识别'
        trust = ['明确 CTA'] if context.get('cta_type') else []
        if any(word in text for word in ['income', 'earning', '收益', 'money', 'bonus', '$']):
            tags.append('收益截图')
            creative_format = '收益截图'
            hook = '收入吸引'
        if any(word in text for word in ['flow', 'step', 'register', 'bind', '流程', '申请', '绑定']):
            tags.append('入会流程说明')
            creative_format = '入会流程说明'
            hook = '流程透明'
            trust.append('流程展示')
        if any(word in text for word in ['chat', 'whatsapp', 'wa', '聊天']):
            tags.append('聊天截图')
            creative_format = '聊天截图'
        if context.get('asset_type') == 'video':
            tags.append('真人口播')
            creative_format = '真人口播'
            trust.append('真人出镜')
        if context.get('asset_type') == 'carousel':
            tags.append('轮播组合')
            creative_format = '轮播组合'
        risks = detect_pii_risk_tags(text)
        if '收益截图' in tags and '$' in text:
            risks.append('金额刺激明显')
        if not risks:
            risks = ['无明显风险']
        return CreativeVisualAnalysis(
            visual_tags=normalize_tags(tags or [creative_format], CREATIVE_FORMATS),
            creative_format=creative_format,
            hook_type=hook,
            trust_signal_tags=normalize_tags(trust or ['无可信度信号'], TRUST_SIGNAL_TAGS),
            risk_tags=normalize_tags(risks, RISK_TAGS),
            confidence=0.72,
        )

    def extract_text(self, image_ref: str, context: Dict[str, Any]) -> CreativeOcrResult:
        text = ' '.join(str(context.get(key) or '') for key in ('title_text', 'body_text')).strip()
        risk_tags = detect_pii_risk_tags(text)
        return CreativeOcrResult(ocr_text=text[:500], risk_tags=risk_tags, confidence=0.68 if text else 0.2)

    def analyze_copy(self, body: str, title: str, description: str, cta: str, context: Dict[str, Any]) -> CreativeCopyAnalysis:
        raw = ' '.join([str(title or ''), str(body or ''), str(description or ''), str(cta or '')]).strip()
        lower = raw.lower()
        value_tags: List[str] = []
        copy_tags: List[str] = []
        if any(word in lower for word in ['guide', 'support', 'mentor', '运营', '指导', 'support']):
            value_tags.append('有运营指导')
        if any(word in lower for word in ['easy', 'simple', '简单', '快速']):
            value_tags.append('入驻流程简单')
        if any(word in lower for word in ['local', 'language', 'bahasa', 'português', '本地']):
            value_tags.append('本地语言支持')
        if any(word in lower for word in ['case', '真实', 'example', 'story']):
            value_tags.append('真实案例')
        if cta:
            copy_tags.append('明确 CTA')
        language = language_hint(raw)
        risk_tags = detect_pii_risk_tags(raw)
        if len(raw) > 160:
            risk_tags.append('文字过密')
        if not risk_tags:
            risk_tags = ['无明显风险']
        readability = max(0.25, min(0.95, 1.0 - max(len(raw) - 120, 0) / 500))
        return CreativeCopyAnalysis(
            copy_tags=copy_tags or ['文案待复核'],
            language_detected=language,
            localization_score=0.75 if language in {'印尼语', '葡萄牙语', '西班牙语', '英语'} else 0.35,
            value_proposition_tags=normalize_tags(value_tags or ['未识别'], VALUE_PROPOSITIONS),
            readability_score=round(readability, 4),
            quality_flags=[] if len(raw) <= 160 else ['文字过密'],
            risk_tags=normalize_tags(risk_tags, RISK_TAGS),
            confidence=0.7 if raw else 0.25,
        )


def normalize_tags(values: Iterable[str], allowed: Iterable[str]) -> List[str]:
    allowed_set = set(allowed)
    result: List[str] = []
    for value in values:
        text = str(value or '').strip()
        if text and text in allowed_set and text not in result:
            result.append(text)
    return result or ['未识别']


def detect_pii_risk_tags(text: Any) -> List[str]:
    raw = str(text or '')
    if not raw:
        return []
    risks: List[str] = []
    if any(pattern.search(raw) for pattern in PII_PATTERNS):
        risks.append('WhatsApp / 手机号外露')
        risks.append('疑似敏感个人信息')
    return risks


def language_hint(text: Any) -> str:
    raw = str(text or '').lower()
    if not raw:
        return '未识别'
    if re.search(r'[\u4e00-\u9fff]', raw):
        return '中文'
    if any(word in raw for word in ['anda', 'uang', 'daftar', 'mudah', 'bahasa']):
        return '印尼语'
    if any(word in raw for word in ['você', 'ganhe', 'cadastro', 'português']):
        return '葡萄牙语'
    if any(word in raw for word in ['ganar', 'registro', 'fácil', 'español']):
        return '西班牙语'
    if re.search(r'[a-z]{3,}', raw):
        return '英语'
    return '未识别'


def infer_asset_type(payload: Dict[str, Any]) -> str:
    if payload.get('asset_feed_spec'):
        return 'dynamic'
    if payload.get('video_id') or payload.get('video_data'):
        return 'video'
    if payload.get('carousel_data') or payload.get('child_attachments'):
        return 'carousel'
    if payload.get('image_hash') or payload.get('thumbnail_url') or payload.get('image_url'):
        return 'image'
    if payload.get('body_text') or payload.get('title_text'):
        return 'text_only'
    return 'unknown'


def content_hash_for_creative(payload: Dict[str, Any]) -> str:
    fields = [
        payload.get('creative_id'), payload.get('image_hash'), payload.get('video_id'),
        payload.get('thumbnail_url'), payload.get('image_url'), payload.get('body_text'), payload.get('title_text'),
        payload.get('description_text'), payload.get('cta_type'), payload.get('copy_fragments_json'),
    ]
    return stable_id(*fields, length=32)


def creative_asset_from_meta_payload(payload: Dict[str, Any], *, now: Optional[str] = None) -> AdCreativeAsset:
    now = now or utc_now()
    platform = str(payload.get('platform') or 'meta').strip().lower() or 'meta'
    ad_id = str(payload.get('ad_id') or payload.get('id') or '').strip()
    creative_id = str(payload.get('creative_id') or '').strip()
    content_hash = str(payload.get('content_hash') or content_hash_for_creative(payload))
    asset_id = str(payload.get('asset_id') or f'aci_{stable_id(platform, ad_id, creative_id, content_hash)}')
    asset_type = str(payload.get('asset_type') or infer_asset_type(payload))
    image_url = safe_media_url(payload.get('image_url'))
    thumbnail_url = safe_media_url(payload.get('thumbnail_url')) or image_url
    source_image_url = safe_media_url(payload.get('source_image_url')) or image_url
    source_width = int(float(payload.get('source_image_width') or 0))
    source_height = int(float(payload.get('source_image_height') or 0))
    source_quality = str(payload.get('source_image_quality') or '').strip()
    if not source_quality and source_image_url:
        largest_edge = max(source_width, source_height)
        source_quality = 'high_res' if largest_edge >= 600 else 'thumbnail'
    copy_fragments = payload.get('copy_fragments') or []
    if not isinstance(copy_fragments, list):
        copy_fragments = []
    copy_fragments_json = str(payload.get('copy_fragments_json') or json.dumps(copy_fragments, ensure_ascii=False, sort_keys=True))
    return AdCreativeAsset(
        asset_id=asset_id,
        platform=platform,
        account_id=str(payload.get('account_id') or '').strip(),
        campaign_id=str(payload.get('campaign_id') or '').strip(),
        adset_id=str(payload.get('adset_id') or payload.get('ad_group_id') or '').strip(),
        ad_id=ad_id,
        ad_name=str(payload.get('ad_name') or '').strip(),
        creative_id=creative_id,
        asset_type=asset_type,
        media_source_type='thumbnail_only' if thumbnail_url else ('meta_url' if image_url else 'generated_placeholder'),
        image_hash=str(payload.get('image_hash') or '').strip(),
        video_id=str(payload.get('video_id') or '').strip(),
        thumbnail_url=thumbnail_url,
        local_media_ref=str(payload.get('local_media_ref') or '').strip(),
        source_image_url=source_image_url,
        source_image_local_ref=str(payload.get('source_image_local_ref') or '').strip(),
        source_image_hash=str(payload.get('source_image_hash') or payload.get('image_hash') or '').strip(),
        source_image_width=source_width,
        source_image_height=source_height,
        source_image_quality=source_quality,
        source_image_origin=str(payload.get('source_image_origin') or ('adimages' if source_image_url else '')).strip(),
        body_text=str(payload.get('body_text') or payload.get('body') or '').strip(),
        title_text=str(payload.get('title_text') or payload.get('title') or payload.get('name') or '').strip(),
        description_text=str(payload.get('description_text') or payload.get('description') or '').strip(),
        copy_fragments_json=copy_fragments_json,
        cta_type=str(payload.get('cta_type') or '').strip(),
        landing_url_domain=str(payload.get('landing_url_domain') or parse_domain(payload.get('landing_url') or payload.get('link_url'))),
        country=str(payload.get('country') or '').strip(),
        project=str(payload.get('project') or '').strip(),
        language_hint=str(payload.get('language_hint') or language_hint(' '.join([str(payload.get('body_text') or ''), str(payload.get('title_text') or '')]))),
        first_seen_at=str(payload.get('first_seen_at') or now),
        last_seen_at=str(payload.get('last_seen_at') or now),
        status=str(payload.get('status') or 'active'),
        content_hash=content_hash,
        sync_status=str(payload.get('sync_status') or ('partial' if not creative_id else 'synced')),
        sync_error_reason=safe_error_reason(payload.get('sync_error_reason')),
        created_at=str(payload.get('created_at') or now),
        updated_at=str(payload.get('updated_at') or now),
        is_dynamic_creative=asset_type == 'dynamic' or bool(payload.get('is_dynamic_creative')),
    )


def creative_copy_fragments(asset: AdCreativeAsset) -> List[Dict[str, str]]:
    try:
        parsed = json.loads(asset.copy_fragments_json or '[]')
    except Exception:
        parsed = []
    if not isinstance(parsed, list):
        return []
    fragments: List[Dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        text = _compact_text(item.get('text'))
        if not text:
            continue
        fragments.append({
            'role': str(item.get('role') or 'copy'),
            'source': str(item.get('source') or ''),
            'field': str(item.get('field') or ''),
            'text': text,
        })
    return fragments


EXPRESSION_ACTION_TYPES = {
    'continue_observe',
    'edit_ad_copy',
    'edit_image_headline',
    'regenerate_creative',
    'generate_derivative_creative',
    'change_audience',
    'check_im_flow',
    'check_customer_response',
    'check_linky_bind_crm',
    'pause',
    'reduce_budget',
    'scale',
    'manual_review',
}

_REWARD_PROMISE_TERMS = {
    '$', 'r$', 'rp', 'money', 'cash', 'income', 'earn', 'bonus', 'reward', 'profit',
    'ganhe', 'ganhar', 'renda', 'lucro', 'premio', 'prêmio', 'uang', 'gaji',
    '赚钱', '收入', '奖励', '提现', '佣金', '高薪', '日赚',
}
_IM_EXPECTATION_TERMS = {
    'chat', 'message', 'conversation', 'conversa', 'mensagem', 'app', 'phone',
    'celular', 'whatsapp', 'bate-papo', 'obrolan', 'ngobrol', 'pesan', 'im',
    '聊天', '私信', '消息', '手机', '社交',
}
_SUPPORT_FLOW_TERMS = {
    'support', 'guide', 'guided', 'mentor', 'team', 'local', 'orientação', 'apoio',
    'panduan', 'bimbingan', '客服', '指导', '支持', '流程', '步骤', '培训',
}
_LOW_QUALITY_VISUAL_FORMATS = {'纯文字海报', '教程步骤图', '流程图风', '线框图'}
_TRUST_VISUAL_TAGS = {'真人口播', '真人场景', '真实案例', 'App 界面', '聊天截图'}


def _contains_any_term(text: str, terms: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def _asset_expression_text(asset: AdCreativeAsset, analysis: Optional[AdCreativeAnalysis] = None) -> str:
    parts = [asset.body_text, asset.title_text, asset.description_text, asset.cta_type]
    parts.extend(fragment.get('text', '') for fragment in creative_copy_fragments(asset))
    if analysis:
        parts.append(analysis.ocr_text)
    return _compact_text(' '.join(part for part in parts if part))


def infer_expression_attribution_granularity(asset: AdCreativeAsset) -> str:
    if not asset.ad_id and not asset.creative_id:
        return 'unknown'
    if asset.is_dynamic_creative:
        return 'ad_level'
    if asset.creative_id:
        return 'creative_level'
    return 'ad_level'


def build_ad_expression_diagnosis(
    asset: AdCreativeAsset,
    analysis: Optional[AdCreativeAnalysis] = None,
) -> Dict[str, Any]:
    """Build a read-only business diagnosis for ad expression quality."""
    expression_text = _asset_expression_text(asset, analysis)
    copy_fragments = creative_copy_fragments(asset)
    visual_tags = set(analysis.visual_tags if analysis else [])
    risk_tags = set(analysis.risk_tags if analysis else [])
    creative_format = analysis.creative_format if analysis else '未识别'
    has_visual = bool(asset.thumbnail_url or asset.local_media_ref or asset.image_hash or asset.video_id)
    has_copy = bool(expression_text)
    has_reward_promise = _contains_any_term(expression_text, _REWARD_PROMISE_TERMS) or '金额刺激明显' in risk_tags or '收益承诺过强' in risk_tags
    has_im_expectation = _contains_any_term(expression_text, _IM_EXPECTATION_TERMS)
    has_support_flow = _contains_any_term(expression_text, _SUPPORT_FLOW_TERMS) or bool(visual_tags & {'入会流程说明', '聊天截图', 'App 界面'})
    visual_weak = (not has_visual) or creative_format in _LOW_QUALITY_VISUAL_FORMATS
    trust_visual = bool(visual_tags & _TRUST_VISUAL_TAGS) or creative_format in {'真人口播', '真人场景', '聊天截图', 'App 界面'}
    copy_risky = has_reward_promise and not has_im_expectation
    copy_under_explained = has_copy and not has_im_expectation and not has_support_flow
    alignment_weak = copy_risky or (has_reward_promise and not has_support_flow)

    attribution_granularity = infer_expression_attribution_granularity(asset)
    attribution_warning = ''
    if asset.is_dynamic_creative:
        attribution_warning = '当前为动态创意，仅能按广告级聚合判断，不能确认单个文案片段或素材元素独立贡献。'
    elif attribution_granularity == 'ad_level':
        attribution_warning = '当前只有广告级归因，表达诊断需要结合前后链路表现复核。'
    elif attribution_granularity == 'unknown':
        attribution_warning = '缺少广告或创意标识，表达归因不明确。'

    visual_issues: List[str] = []
    if not has_visual:
        visual_issues.append('缺少可诊断素材图或视频')
    if creative_format in _LOW_QUALITY_VISUAL_FORMATS:
        visual_issues.append(f'素材形式偏弱：{creative_format}')
    if has_visual and not trust_visual:
        visual_issues.append('真人场景、手机 UI 或信任承接信号不足')

    image_text_issues: List[str] = []
    if analysis and analysis.ocr_text and has_reward_promise and not has_im_expectation:
        image_text_issues.append('图中文字偏收益刺激，但未明确进入 IM 后的真实互动预期')
    if analysis and '文字过密' in risk_tags:
        image_text_issues.append('图中文字过密，可能影响信息流首屏理解')

    copy_issues: List[str] = []
    if not has_copy:
        copy_issues.append('缺少可读广告文案')
    if copy_risky:
        copy_issues.append('文案强调收益但没有解释 IM / App 内互动路径，容易带来低质量流量')
    elif copy_under_explained:
        copy_issues.append('文案未充分说明用户进入 IM 后要完成什么动作')
    if asset.is_dynamic_creative and len(copy_fragments) > 1:
        copy_issues.append('动态创意存在多个文案片段，当前只能判断组合风险')

    alignment_issues: List[str] = []
    if alignment_weak:
        alignment_issues.append('广告承诺与真实 IM 承接路径可能不一致')
    if has_reward_promise and not trust_visual:
        alignment_issues.append('收益表达缺少真人 / 手机 UI / 品牌承接支撑')

    effect_issues: List[str] = []
    if attribution_warning:
        effect_issues.append(attribution_warning)
    effect_issues.append('第一阶段仅输出表达诊断；最终动作需要叠加 IM 有效率、用户行为型有效 IM 成本和后链路转化。')

    if visual_weak and (copy_risky or alignment_weak):
        diagnosis_type = '广告表达承诺偏差'
        action_type = 'regenerate_creative'
    elif copy_risky:
        diagnosis_type = '文案收益诱导偏强'
        action_type = 'edit_ad_copy'
    elif visual_weak:
        diagnosis_type = '视觉承接偏弱'
        action_type = 'regenerate_creative'
    elif alignment_weak:
        diagnosis_type = '表达链路不一致'
        action_type = 'edit_image_headline'
    elif not has_copy and not has_visual:
        diagnosis_type = '表达数据不足'
        action_type = 'manual_review'
    else:
        diagnosis_type = '表达可保留'
        action_type = 'continue_observe'

    if action_type not in EXPRESSION_ACTION_TYPES:
        action_type = 'manual_review'

    recommended_actions = [action_type]
    if copy_risky and action_type != 'edit_ad_copy':
        recommended_actions.append('edit_ad_copy')
    if visual_weak and action_type != 'regenerate_creative':
        recommended_actions.append('regenerate_creative')

    confidence = 'medium'
    if attribution_granularity in {'unknown', 'ad_level'} or not has_copy:
        confidence = 'low'
    elif analysis and analysis.confidence >= 0.65:
        confidence = 'high'

    return {
        'diagnosis_type': diagnosis_type,
        'action_type': action_type,
        'confidence': confidence,
        'attribution_granularity': attribution_granularity,
        'attribution_warning': attribution_warning,
        'visual_diagnosis': {
            'status': 'weak' if visual_issues else 'ok',
            'issues': visual_issues,
            'recommended_action': 'regenerate_creative' if visual_issues else 'continue_observe',
        },
        'image_text_diagnosis': {
            'status': 'risky' if image_text_issues else 'ok',
            'issues': image_text_issues,
            'recommended_action': 'edit_image_headline' if image_text_issues else 'continue_observe',
        },
        'ad_copy_diagnosis': {
            'status': 'risky' if copy_risky else ('weak' if copy_issues else 'ok'),
            'issues': copy_issues,
            'recommended_action': 'edit_ad_copy' if copy_issues else 'continue_observe',
        },
        'expression_alignment': {
            'status': 'weak' if alignment_issues else 'ok',
            'issues': alignment_issues,
            'recommended_action': 'edit_image_headline' if alignment_issues else 'continue_observe',
        },
        'expression_effect_diagnosis': {
            'status': 'needs_funnel_data' if effect_issues else 'ok',
            'issues': effect_issues,
            'recommended_action': 'continue_observe',
        },
        'recommended_actions': list(dict.fromkeys(recommended_actions)),
        'allow_generate_creative': action_type in {'regenerate_creative', 'generate_derivative_creative'},
        'allow_edit_copy': bool(copy_issues),
        'allow_pause': False,
        'allow_scale': False,
        'needs_data': ['user_engaged_im', 'high_intent_im', 'im_to_join_rate', 'copy_fragment_performance'],
    }


def normalize_meta_ad_account_id(value: Any) -> str:
    raw = str(value or '').strip()
    if raw.startswith('act_'):
        raw = raw[4:]
    return raw.strip()


def is_meta_transient_error(response: Any) -> bool:
    try:
        body = response.json()
    except Exception:
        return False
    error = body.get('error') if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return False
    if bool(error.get('is_transient')):
        return True
    try:
        code = int(error.get('code') or 0)
    except (TypeError, ValueError):
        code = 0
    return code in {1, 2, 4, 17, 32, 613}


def ensure_creative_intelligence_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ad_creative_asset (
            asset_id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            account_id TEXT,
            campaign_id TEXT,
            adset_id TEXT,
            ad_id TEXT NOT NULL,
            ad_name TEXT,
            creative_id TEXT,
            asset_type TEXT NOT NULL,
            media_source_type TEXT NOT NULL,
            image_hash TEXT,
            video_id TEXT,
            thumbnail_url TEXT,
            local_media_ref TEXT,
            source_image_url TEXT,
            source_image_local_ref TEXT,
            source_image_hash TEXT,
            source_image_width INTEGER NOT NULL DEFAULT 0,
            source_image_height INTEGER NOT NULL DEFAULT 0,
            source_image_quality TEXT,
            source_image_origin TEXT,
            body_text TEXT,
            title_text TEXT,
            description_text TEXT,
            copy_fragments_json TEXT NOT NULL DEFAULT '[]',
            cta_type TEXT,
            landing_url_domain TEXT,
            country TEXT,
            project TEXT,
            language_hint TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            status TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            sync_status TEXT NOT NULL,
            sync_error_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_dynamic_creative INTEGER NOT NULL DEFAULT 0,
            UNIQUE(platform, ad_id, creative_id, content_hash)
        );
        CREATE TABLE IF NOT EXISTS ad_creative_asset_link (
            asset_id TEXT NOT NULL,
            ad_id TEXT NOT NULL,
            adset_id TEXT,
            campaign_id TEXT,
            link_type TEXT NOT NULL,
            first_active_date TEXT,
            last_active_date TEXT,
            status TEXT NOT NULL,
            PRIMARY KEY(asset_id, ad_id, link_type)
        );
        CREATE TABLE IF NOT EXISTS ad_creative_analysis (
            analysis_id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL,
            analysis_version TEXT NOT NULL,
            analysis_status TEXT NOT NULL,
            analyzer_type TEXT NOT NULL,
            analyzed_at TEXT NOT NULL,
            visual_tags_json TEXT NOT NULL,
            ocr_text TEXT,
            copy_tags_json TEXT NOT NULL,
            language_detected TEXT,
            localization_score REAL,
            risk_tags_json TEXT NOT NULL,
            creative_format TEXT,
            hook_type TEXT,
            value_proposition_tags_json TEXT NOT NULL,
            trust_signal_tags_json TEXT NOT NULL,
            readability_score REAL,
            quality_flags_json TEXT NOT NULL,
            confidence REAL,
            failure_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS ad_creative_frame_analysis (
            frame_id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL,
            video_id TEXT,
            timestamp_ms INTEGER NOT NULL,
            frame_ref TEXT,
            ocr_text TEXT,
            visual_tags_json TEXT NOT NULL,
            hook_tags_json TEXT NOT NULL,
            cta_tags_json TEXT NOT NULL,
            risk_tags_json TEXT NOT NULL,
            analysis_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ad_creative_performance_daily (
            report_date_london TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            creative_id TEXT,
            ad_id TEXT NOT NULL,
            adset_id TEXT,
            campaign_id TEXT,
            country TEXT,
            project TEXT,
            spend REAL NOT NULL,
            impressions REAL NOT NULL,
            clicks REAL NOT NULL,
            ctr REAL NOT NULL,
            cpm REAL NOT NULL,
            installs REAL NOT NULL,
            cpi REAL,
            af_model_join_events REAL NOT NULL,
            tugao_real_bind_count INTEGER NOT NULL,
            real_bind_cpa REAL,
            af_to_real_bind_rate REAL,
            data_quality_status TEXT NOT NULL,
            attribution_level TEXT NOT NULL,
            creative_grain TEXT NOT NULL,
            is_dynamic_creative INTEGER NOT NULL,
            grain_warning TEXT,
            PRIMARY KEY(report_date_london, asset_id, ad_id)
        );
        CREATE TABLE IF NOT EXISTS ad_creative_strategy_knowledge (
            knowledge_id TEXT PRIMARY KEY,
            entry_type TEXT NOT NULL,
            asset_id TEXT,
            payload_json TEXT NOT NULL,
            evidence_window TEXT NOT NULL,
            review_status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(ad_creative_asset)").fetchall()}
        if 'copy_fragments_json' not in columns:
            conn.execute("ALTER TABLE ad_creative_asset ADD COLUMN copy_fragments_json TEXT NOT NULL DEFAULT '[]'")
        for column_name, ddl in {
            'ad_name': "ALTER TABLE ad_creative_asset ADD COLUMN ad_name TEXT",
            'source_image_url': "ALTER TABLE ad_creative_asset ADD COLUMN source_image_url TEXT",
            'source_image_local_ref': "ALTER TABLE ad_creative_asset ADD COLUMN source_image_local_ref TEXT",
            'source_image_hash': "ALTER TABLE ad_creative_asset ADD COLUMN source_image_hash TEXT",
            'source_image_width': "ALTER TABLE ad_creative_asset ADD COLUMN source_image_width INTEGER NOT NULL DEFAULT 0",
            'source_image_height': "ALTER TABLE ad_creative_asset ADD COLUMN source_image_height INTEGER NOT NULL DEFAULT 0",
            'source_image_quality': "ALTER TABLE ad_creative_asset ADD COLUMN source_image_quality TEXT",
            'source_image_origin': "ALTER TABLE ad_creative_asset ADD COLUMN source_image_origin TEXT",
        }.items():
            if column_name not in columns:
                conn.execute(ddl)
        conn.commit()
    except sqlite3.DatabaseError:
        pass


def persist_creative_assets(conn: sqlite3.Connection, assets: Iterable[AdCreativeAsset]) -> int:
    ensure_creative_intelligence_tables(conn)
    count = 0
    for asset in assets:
        conn.execute(
            """
            INSERT OR REPLACE INTO ad_creative_asset
            (asset_id, platform, account_id, campaign_id, adset_id, ad_id, ad_name, creative_id, asset_type, media_source_type,
             image_hash, video_id, thumbnail_url, local_media_ref, source_image_url, source_image_local_ref,
             source_image_hash, source_image_width, source_image_height, source_image_quality, source_image_origin,
             body_text, title_text, description_text, copy_fragments_json, cta_type,
             landing_url_domain, country, project, language_hint, first_seen_at, last_seen_at, status, content_hash,
             sync_status, sync_error_reason, created_at, updated_at, is_dynamic_creative)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset.asset_id, asset.platform, asset.account_id, asset.campaign_id, asset.adset_id, asset.ad_id,
                asset.ad_name, asset.creative_id, asset.asset_type, asset.media_source_type, asset.image_hash, asset.video_id,
                asset.thumbnail_url, asset.local_media_ref, asset.source_image_url, asset.source_image_local_ref,
                asset.source_image_hash, int(asset.source_image_width or 0), int(asset.source_image_height or 0),
                asset.source_image_quality, asset.source_image_origin, asset.body_text, asset.title_text, asset.description_text,
                asset.copy_fragments_json, asset.cta_type, asset.landing_url_domain, asset.country, asset.project, asset.language_hint,
                asset.first_seen_at, asset.last_seen_at, asset.status, asset.content_hash, asset.sync_status,
                asset.sync_error_reason, asset.created_at, asset.updated_at, 1 if asset.is_dynamic_creative else 0,
            ),
        )
        link = AdCreativeAssetLink(
            asset_id=asset.asset_id,
            ad_id=asset.ad_id,
            adset_id=asset.adset_id,
            campaign_id=asset.campaign_id,
            link_type='dynamic_component' if asset.is_dynamic_creative else 'primary',
            first_active_date=asset.first_seen_at[:10],
            last_active_date=asset.last_seen_at[:10],
            status=asset.status,
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO ad_creative_asset_link
            (asset_id, ad_id, adset_id, campaign_id, link_type, first_active_date, last_active_date, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (link.asset_id, link.ad_id, link.adset_id, link.campaign_id, link.link_type, link.first_active_date, link.last_active_date, link.status),
        )
        count += 1
    conn.commit()
    return count


def load_creative_assets(conn: sqlite3.Connection, *, limit: int = 500) -> List[AdCreativeAsset]:
    ensure_creative_intelligence_tables(conn)
    rows = conn.execute(
        "SELECT * FROM ad_creative_asset ORDER BY last_seen_at DESC, updated_at DESC LIMIT ?",
        (max(1, min(int(limit or 500), 5000)),),
    ).fetchall()
    return [
        AdCreativeAsset(
            asset_id=str(row['asset_id']),
            platform=str(row['platform'] or ''),
            account_id=str(row['account_id'] or ''),
            campaign_id=str(row['campaign_id'] or ''),
            adset_id=str(row['adset_id'] or ''),
            ad_id=str(row['ad_id'] or ''),
            ad_name=str(row['ad_name'] or '') if 'ad_name' in row.keys() else '',
            creative_id=str(row['creative_id'] or ''),
            asset_type=str(row['asset_type'] or 'unknown'),
            media_source_type=str(row['media_source_type'] or 'generated_placeholder'),
            image_hash=str(row['image_hash'] or ''),
            video_id=str(row['video_id'] or ''),
            thumbnail_url=str(row['thumbnail_url'] or ''),
            local_media_ref=str(row['local_media_ref'] or ''),
            source_image_url=str(row['source_image_url'] or '') if 'source_image_url' in row.keys() else '',
            source_image_local_ref=str(row['source_image_local_ref'] or '') if 'source_image_local_ref' in row.keys() else '',
            source_image_hash=str(row['source_image_hash'] or '') if 'source_image_hash' in row.keys() else '',
            source_image_width=int(row['source_image_width'] or 0) if 'source_image_width' in row.keys() else 0,
            source_image_height=int(row['source_image_height'] or 0) if 'source_image_height' in row.keys() else 0,
            source_image_quality=str(row['source_image_quality'] or '') if 'source_image_quality' in row.keys() else '',
            source_image_origin=str(row['source_image_origin'] or '') if 'source_image_origin' in row.keys() else '',
            body_text=str(row['body_text'] or ''),
            title_text=str(row['title_text'] or ''),
            description_text=str(row['description_text'] or ''),
            copy_fragments_json=str(row['copy_fragments_json'] or '[]') if 'copy_fragments_json' in row.keys() else '[]',
            cta_type=str(row['cta_type'] or ''),
            landing_url_domain=str(row['landing_url_domain'] or ''),
            country=str(row['country'] or ''),
            project=str(row['project'] or ''),
            language_hint=str(row['language_hint'] or ''),
            first_seen_at=str(row['first_seen_at'] or ''),
            last_seen_at=str(row['last_seen_at'] or ''),
            status=str(row['status'] or ''),
            content_hash=str(row['content_hash'] or ''),
            sync_status=str(row['sync_status'] or ''),
            sync_error_reason=str(row['sync_error_reason'] or ''),
            created_at=str(row['created_at'] or ''),
            updated_at=str(row['updated_at'] or ''),
            is_dynamic_creative=bool(row['is_dynamic_creative']),
        )
        for row in rows
    ]


def persist_creative_analysis(conn: sqlite3.Connection, analyses: Iterable[AdCreativeAnalysis]) -> int:
    ensure_creative_intelligence_tables(conn)
    count = 0
    for item in analyses:
        conn.execute(
            """
            INSERT OR REPLACE INTO ad_creative_analysis
            (analysis_id, asset_id, analysis_version, analysis_status, analyzer_type, analyzed_at, visual_tags_json,
             ocr_text, copy_tags_json, language_detected, localization_score, risk_tags_json, creative_format,
             hook_type, value_proposition_tags_json, trust_signal_tags_json, readability_score, quality_flags_json,
             confidence, failure_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.analysis_id, item.asset_id, item.analysis_version, item.analysis_status, item.analyzer_type,
                item.analyzed_at, json.dumps(item.visual_tags, ensure_ascii=False), item.ocr_text,
                json.dumps(item.copy_tags, ensure_ascii=False), item.language_detected, item.localization_score,
                json.dumps(item.risk_tags, ensure_ascii=False), item.creative_format, item.hook_type,
                json.dumps(item.value_proposition_tags, ensure_ascii=False), json.dumps(item.trust_signal_tags, ensure_ascii=False),
                item.readability_score, json.dumps(item.quality_flags, ensure_ascii=False), item.confidence, item.failure_reason,
            ),
        )
        count += 1
    conn.commit()
    return count


def load_latest_creative_analysis(conn: sqlite3.Connection) -> Dict[str, AdCreativeAnalysis]:
    ensure_creative_intelligence_tables(conn)
    rows = conn.execute(
        """
        SELECT a.*
        FROM ad_creative_analysis a
        JOIN (
            SELECT asset_id, MAX(analyzed_at) AS analyzed_at
            FROM ad_creative_analysis
            GROUP BY asset_id
        ) latest ON latest.asset_id = a.asset_id AND latest.analyzed_at = a.analyzed_at
        """
    ).fetchall()
    result: Dict[str, AdCreativeAnalysis] = {}
    for row in rows:
        result[str(row['asset_id'])] = AdCreativeAnalysis(
            analysis_id=str(row['analysis_id']),
            asset_id=str(row['asset_id']),
            analysis_version=str(row['analysis_version']),
            analysis_status=str(row['analysis_status']),
            analyzer_type=str(row['analyzer_type']),
            analyzed_at=str(row['analyzed_at']),
            visual_tags=json.loads(row['visual_tags_json'] or '[]'),
            ocr_text=str(row['ocr_text'] or ''),
            copy_tags=json.loads(row['copy_tags_json'] or '[]'),
            language_detected=str(row['language_detected'] or '未识别'),
            localization_score=float(row['localization_score'] or 0.0),
            risk_tags=json.loads(row['risk_tags_json'] or '[]'),
            creative_format=str(row['creative_format'] or '未识别'),
            hook_type=str(row['hook_type'] or '未识别'),
            value_proposition_tags=json.loads(row['value_proposition_tags_json'] or '[]'),
            trust_signal_tags=json.loads(row['trust_signal_tags_json'] or '[]'),
            readability_score=float(row['readability_score'] or 0.0),
            quality_flags=json.loads(row['quality_flags_json'] or '[]'),
            confidence=float(row['confidence'] or 0.0),
            failure_reason=str(row['failure_reason'] or ''),
        )
    return result


class MetaCreativeSyncService:
    def __init__(
        self,
        *,
        token: str = '',
        account_ids: Optional[List[str]] = None,
        api_version: str = 'v25.0',
        base_url: str = 'https://graph.facebook.com',
        session: Any = None,
        page_size: int = 100,
        enabled: bool = False,
        account_access_policy: Optional[MetaAdAccountAccessPolicy] = None,
    ) -> None:
        self.token = token
        self.account_ids = [normalize_meta_ad_account_id(item) for item in (account_ids or []) if normalize_meta_ad_account_id(item)]
        self.api_version = str(api_version or 'v25.0').strip() or 'v25.0'
        self.base_url = str(base_url or 'https://graph.facebook.com').rstrip('/')
        self.session = session
        self.page_size = max(1, min(int(page_size or 100), 500))
        self.enabled = bool(enabled)
        self.account_access_policy = account_access_policy or MetaAdAccountAccessPolicy.from_environment()

    def sync_payloads(self, payloads: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        assets = [creative_asset_from_meta_payload(payload) for payload in payloads]
        return {
            'ok': True,
            'mode': 'fixture_payload',
            'synced_count': len(assets),
            'assets': assets,
            'errors': [],
        }

    def readiness(self) -> Dict[str, Any]:
        blocking_reasons: List[str] = []
        if not self.enabled:
            blocking_reasons.append('creative_sync_disabled')
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
            'mode': 'meta_readonly' if not blocking_reasons else 'not_ready',
        }

    def probe_readonly_access(self) -> Dict[str, Any]:
        readiness = self.readiness()
        if not readiness['ready']:
            return {**readiness, 'probe_status': 'skipped'}
        account_results: List[Dict[str, Any]] = []
        errors: List[str] = []
        access_decisions = []
        fields = 'id,creative{id}'
        for account_id in self.account_ids:
            configured_access = self.account_access_policy.configured(account_id)
            access_decisions.append(configured_access)
            if not configured_access.should_sync:
                account_results.append({
                    'account_id': account_id,
                    'ok': True,
                    'skipped': True,
                    'access': configured_access.to_dict(),
                })
                continue
            url = f'{self.base_url}/{self.api_version}/act_{account_id}/ads'
            try:
                response = self.session.get(
                    url,
                    params={'fields': fields, 'limit': 1, 'access_token': self.token},
                    timeout=15,
                )
                status_code = int(getattr(response, 'status_code', 200) or 200)
                if status_code >= 400:
                    access_decision = self.account_access_policy.classify_response(account_id, response)
                    access_decisions[-1] = access_decision
                    reason = safe_error_reason(getattr(response, 'text', '') or status_code)
                    errors.append(f'{account_id}:{reason}')
                    account_results.append({
                        'account_id': account_id,
                        'ok': False,
                        'status_code': status_code,
                        'reason': reason,
                    })
                    continue
                body = response.json()
                account_results.append({
                    'account_id': account_id,
                    'ok': True,
                    'status_code': status_code,
                    'sample_count': len(body.get('data') or []) if isinstance(body, dict) else 0,
                })
            except Exception as exc:
                reason = safe_error_reason(exc.__class__.__name__)
                errors.append(f'{account_id}:{reason}')
                account_results.append({
                    'account_id': account_id,
                    'ok': False,
                    'status_code': 0,
                    'reason': reason,
                })
        return {
            **readiness,
            'probe_status': 'ok' if not errors else 'failed',
            'probe_checked_at': utc_now(),
            'accounts': account_results,
            'errors': errors,
            'account_access': access_summary(access_decisions),
        }

    def fetch_story_media(self, story_id: Any) -> Dict[str, Any]:
        story_id_text = str(story_id or '').strip()
        if not story_id_text or self.session is None:
            return {}
        url = f'{self.base_url}/{self.api_version}/{story_id_text}'
        try:
            response = self.session.get(
                url,
                params={
                    'fields': 'message,story,full_picture,picture,attachments{media,subattachments,target,title,description,url}',
                    'access_token': self.token,
                },
                timeout=20,
            )
            if getattr(response, 'status_code', 200) >= 400:
                return {}
            body = response.json()
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}

    def fetch_image_hash_media(self, account_id: Any, image_hash: Any) -> Dict[str, Any]:
        account_text = normalize_meta_ad_account_id(account_id)
        hash_text = str(image_hash or '').strip()
        if not account_text or not hash_text or self.session is None:
            return {}
        url = f'{self.base_url}/{self.api_version}/act_{account_text}/adimages'
        try:
            response = self.session.get(
                url,
                params={
                    'fields': 'hash,url,url_128,permalink_url,created_time,name',
                    'hashes': json.dumps([hash_text]),
                    'access_token': self.token,
                },
                timeout=20,
            )
            if getattr(response, 'status_code', 200) >= 400:
                return {}
            body = response.json()
            for row in (body.get('data') or []) if isinstance(body, dict) else []:
                if not isinstance(row, dict):
                    continue
                if str(row.get('hash') or '').strip() != hash_text:
                    continue
                return row
        except Exception:
            return {}
        return {}

    def asset_from_ad_row(self, account_id: Any, row: Dict[str, Any]) -> Optional[AdCreativeAsset]:
        if not isinstance(row, dict) or not row.get('id'):
            return None
        creative = row.get('creative') or {}
        if not isinstance(creative, dict):
            creative = {}
        story = creative.get('object_story_spec') or {}
        link_data = story.get('link_data') or {}
        video_data = story.get('video_data') or {}
        template_data = story.get('template_data') or {}
        call_to_action = link_data.get('call_to_action') or video_data.get('call_to_action') or {}
        call_to_action_value = call_to_action.get('value') if isinstance(call_to_action, dict) else {}
        story_media = {}
        if creative.get('effective_object_story_id'):
            story_media = self.fetch_story_media(creative.get('effective_object_story_id'))
        media_refs = extract_meta_creative_media(creative, story_media=story_media)
        copy_fragments = extract_meta_creative_copy_fragments(ad_row=row, creative=creative, story_media=story_media)
        body_text = join_copy_fragments(_fragments_by_role(copy_fragments, 'body'))
        title_text = first_non_empty(
            creative.get('title'),
            link_data.get('name'),
            video_data.get('title'),
            template_data.get('name'),
            join_copy_fragments(_fragments_by_role(copy_fragments, 'title'), max_items=4),
            row.get('name'),
            creative.get('name'),
        )
        description_text = join_copy_fragments(_fragments_by_role(copy_fragments, 'description'), max_items=8)
        cta_type = first_non_empty(
            (call_to_action.get('type') if isinstance(call_to_action, dict) else ''),
            *(_fragments_by_role(copy_fragments, 'cta')[:3]),
        )
        landing_url = first_non_empty(
            link_data.get('link'),
            (call_to_action_value.get('link') if isinstance(call_to_action_value, dict) else ''),
            *(_fragments_by_role(copy_fragments, 'landing_url')[:3]),
        )
        image_hash_media = {}
        if media_refs.get('image_hash'):
            image_hash_media = self.fetch_image_hash_media(account_id or row.get('account_id'), media_refs.get('image_hash'))
            hash_preview = safe_media_url(image_hash_media.get('url_128') or image_hash_media.get('url'))
            if hash_preview and meta_thumbnail_needs_image_hash_fallback(media_refs.get('thumbnail_url')):
                media_refs['thumbnail_url'] = hash_preview
            hash_image_url = safe_media_url(image_hash_media.get('url') or image_hash_media.get('url_128'))
            if hash_image_url:
                media_refs['image_url'] = hash_image_url
        source_image_url = safe_media_url(image_hash_media.get('url') or media_refs.get('image_url') or '')
        source_image_width = int(float(image_hash_media.get('width') or 0)) if isinstance(image_hash_media, dict) else 0
        source_image_height = int(float(image_hash_media.get('height') or 0)) if isinstance(image_hash_media, dict) else 0
        source_image_quality = 'high_res' if source_image_url and max(source_image_width, source_image_height) >= 600 else ('' if not source_image_url else 'thumbnail')
        return creative_asset_from_meta_payload({
            'platform': 'meta',
            'account_id': row.get('account_id') or account_id,
            'campaign_id': row.get('campaign_id'),
            'adset_id': row.get('adset_id'),
            'ad_id': row.get('id'),
            'ad_name': row.get('name'),
            'creative_id': creative.get('id'),
            'body_text': body_text or creative.get('body') or link_data.get('message') or video_data.get('message') or template_data.get('message'),
            'title_text': title_text,
            'description_text': description_text or link_data.get('description') or video_data.get('description') or creative.get('title') or creative.get('name'),
            'copy_fragments': copy_fragments,
            'cta_type': cta_type,
            'landing_url': landing_url,
            'thumbnail_url': media_refs.get('thumbnail_url'),
            'image_url': media_refs.get('image_url'),
            'image_hash': media_refs.get('image_hash'),
            'source_image_url': source_image_url,
            'source_image_hash': media_refs.get('image_hash'),
            'source_image_width': source_image_width,
            'source_image_height': source_image_height,
            'source_image_quality': source_image_quality,
            'source_image_origin': 'adimages' if image_hash_media and source_image_url else ('creative_image_url' if source_image_url else ''),
            'video_id': media_refs.get('video_id'),
            'child_attachments': link_data.get('child_attachments') or [],
            'video_data': video_data,
            'asset_feed_spec': creative.get('asset_feed_spec'),
            'is_dynamic_creative': bool(creative.get('asset_feed_spec')),
        })

    def fetch_ad_asset(self, ad_id: Any, *, account_id: Any = '') -> Optional[AdCreativeAsset]:
        ad_text = str(ad_id or '').strip()
        if not ad_text or not self.enabled or not self.token or self.session is None:
            return None
        fields = ','.join([
            'id', 'name', 'account_id', 'campaign_id', 'adset_id',
            'creative{id,name,body,title,object_story_spec,asset_feed_spec,thumbnail_url,image_url,image_hash,video_id,effective_object_story_id}',
        ])
        account_candidates = list(dict.fromkeys([normalize_meta_ad_account_id(account_id), *self.account_ids]))
        try:
            response = self.session.get(
                f'{self.base_url}/{self.api_version}/{ad_text}',
                params={'fields': fields, 'access_token': self.token},
                timeout=20,
            )
            if getattr(response, 'status_code', 200) < 400:
                body = response.json()
                if isinstance(body, dict) and body.get('id'):
                    fallback_account = body.get('account_id') or (account_candidates[0] if account_candidates else '')
                    return self.asset_from_ad_row(fallback_account, body)
        except Exception:
            pass
        for account_text in account_candidates:
            if not account_text:
                continue
            try:
                response = self.session.get(
                    f'{self.base_url}/{self.api_version}/act_{account_text}/ads',
                    params={
                        'fields': fields,
                        'limit': 1,
                        'filtering': json.dumps([{'field': 'id', 'operator': 'EQUAL', 'value': ad_text}]),
                        'access_token': self.token,
                    },
                    timeout=20,
                )
                if getattr(response, 'status_code', 200) >= 400:
                    continue
                body = response.json()
                for row in (body.get('data') or []) if isinstance(body, dict) else []:
                    if str(row.get('id') or '').strip() == ad_text:
                        return self.asset_from_ad_row(account_text, row)
            except Exception:
                continue
        return None

    def sync(self) -> Dict[str, Any]:
        if not self.enabled:
            return {'ok': False, 'mode': 'disabled', 'synced_count': 0, 'assets': [], 'errors': ['creative_sync_disabled']}
        if not self.token or not self.account_ids or self.session is None:
            return {'ok': False, 'mode': 'not_configured', 'synced_count': 0, 'assets': [], 'errors': ['meta_creative_sync_not_configured']}
        assets: List[AdCreativeAsset] = []
        errors: List[str] = []
        access_decisions = []
        fields = ','.join([
            'id', 'name', 'campaign_id', 'adset_id',
            'creative{id,name,body,title,object_story_spec,asset_feed_spec,thumbnail_url,image_url,image_hash,video_id,effective_object_story_id}',
        ])
        for account_id in self.account_ids:
            configured_access = self.account_access_policy.configured(account_id)
            access_decisions.append(configured_access)
            if not configured_access.should_sync:
                continue
            url = f'{self.base_url}/{self.api_version}/act_{account_id}/ads'
            params = {'fields': fields, 'limit': self.page_size, 'access_token': self.token}
            while url:
                try:
                    response = None
                    for attempt in range(3):
                        response = self.session.get(url, params=params, timeout=30)
                        if getattr(response, 'status_code', 200) < 400:
                            break
                        if not is_meta_transient_error(response) or attempt >= 2:
                            break
                        time.sleep(0.5 * (attempt + 1))
                    params = None
                    if response is None or getattr(response, 'status_code', 200) >= 400:
                        if response is not None:
                            access_decisions[-1] = self.account_access_policy.classify_response(
                                account_id, response,
                            )
                        errors.append(f'{account_id}:{safe_error_reason(getattr(response, "text", ""))}')
                        break
                    body = response.json()
                except Exception as exc:
                    errors.append(f'{account_id}:{safe_error_reason(exc.__class__.__name__)}')
                    break
                for row in body.get('data') or []:
                    creative = row.get('creative') or {}
                    story = creative.get('object_story_spec') or {}
                    link_data = story.get('link_data') or {}
                    video_data = story.get('video_data') or {}
                    photo_data = story.get('photo_data') or {}
                    template_data = story.get('template_data') or {}
                    call_to_action = link_data.get('call_to_action') or video_data.get('call_to_action') or {}
                    call_to_action_value = call_to_action.get('value') if isinstance(call_to_action, dict) else {}
                    story_media = {}
                    if creative.get('effective_object_story_id'):
                        story_media = self.fetch_story_media(creative.get('effective_object_story_id'))
                    media_refs = extract_meta_creative_media(creative, story_media=story_media)
                    copy_fragments = extract_meta_creative_copy_fragments(
                        ad_row=row,
                        creative=creative,
                        story_media=story_media,
                    )
                    body_text = join_copy_fragments(_fragments_by_role(copy_fragments, 'body'))
                    title_text = first_non_empty(
                        creative.get('title'),
                        link_data.get('name'),
                        video_data.get('title'),
                        template_data.get('name'),
                        join_copy_fragments(_fragments_by_role(copy_fragments, 'title'), max_items=4),
                        row.get('name'),
                        creative.get('name'),
                    )
                    description_text = join_copy_fragments(_fragments_by_role(copy_fragments, 'description'), max_items=8)
                    cta_type = first_non_empty(
                        (call_to_action.get('type') if isinstance(call_to_action, dict) else ''),
                        *(_fragments_by_role(copy_fragments, 'cta')[:3]),
                    )
                    landing_url = first_non_empty(
                        link_data.get('link'),
                        (call_to_action_value.get('link') if isinstance(call_to_action_value, dict) else ''),
                        *(_fragments_by_role(copy_fragments, 'landing_url')[:3]),
                    )
                    if not media_refs.get('thumbnail_url') and story_media:
                        media_refs = extract_meta_creative_media(creative, story_media=story_media)
                    image_hash_media = {}
                    if media_refs.get('image_hash') and meta_thumbnail_needs_image_hash_fallback(media_refs.get('thumbnail_url')):
                        image_hash_media = self.fetch_image_hash_media(account_id, media_refs.get('image_hash'))
                        hash_preview = safe_media_url(image_hash_media.get('url_128') or image_hash_media.get('url'))
                        if hash_preview:
                            media_refs['thumbnail_url'] = hash_preview
                        hash_image_url = safe_media_url(image_hash_media.get('url') or image_hash_media.get('url_128'))
                        if hash_image_url:
                            media_refs['image_url'] = hash_image_url
                    source_image_url = safe_media_url(
                        image_hash_media.get('url')
                        or media_refs.get('image_url')
                        or ''
                    )
                    source_image_width = int(float(image_hash_media.get('width') or 0)) if isinstance(image_hash_media, dict) else 0
                    source_image_height = int(float(image_hash_media.get('height') or 0)) if isinstance(image_hash_media, dict) else 0
                    source_image_quality = ''
                    if source_image_url:
                        source_image_quality = 'high_res' if max(source_image_width, source_image_height) >= 600 else 'thumbnail'
                    payload = {
                        'platform': 'meta',
                        'account_id': account_id,
                        'campaign_id': row.get('campaign_id'),
                        'adset_id': row.get('adset_id'),
                        'ad_id': row.get('id'),
                        'ad_name': row.get('name'),
                        'creative_id': creative.get('id'),
                        'body_text': body_text or creative.get('body') or link_data.get('message') or video_data.get('message') or template_data.get('message'),
                        'title_text': title_text,
                        'description_text': description_text or link_data.get('description') or video_data.get('description') or creative.get('title') or creative.get('name'),
                        'copy_fragments': copy_fragments,
                        'cta_type': cta_type,
                        'landing_url': landing_url,
                        'thumbnail_url': media_refs.get('thumbnail_url'),
                        'image_url': media_refs.get('image_url'),
                        'image_hash': media_refs.get('image_hash'),
                        'source_image_url': source_image_url,
                        'source_image_hash': media_refs.get('image_hash'),
                        'source_image_width': source_image_width,
                        'source_image_height': source_image_height,
                        'source_image_quality': source_image_quality,
                        'source_image_origin': 'adimages' if image_hash_media and source_image_url else ('creative_image_url' if source_image_url else ''),
                        'video_id': media_refs.get('video_id'),
                        'child_attachments': link_data.get('child_attachments') or [],
                        'video_data': video_data,
                        'photo_data': photo_data,
                        'asset_feed_spec': creative.get('asset_feed_spec'),
                        'is_dynamic_creative': bool(creative.get('asset_feed_spec')),
                    }
                    assets.append(creative_asset_from_meta_payload(payload))
                url = ((body.get('paging') or {}).get('next') or '').strip()
        return {
            'ok': not errors,
            'mode': 'meta_readonly',
            'synced_count': len(assets),
            'assets': assets,
            'errors': errors,
            'account_access': access_summary(access_decisions),
        }


class CreativeMediaIngestionService:
    def __init__(self, *, enabled: bool = False, max_bytes: int = 10_000_000) -> None:
        self.enabled = bool(enabled)
        self.max_bytes = int(max_bytes or 10_000_000)

    def ingest(self, asset: AdCreativeAsset) -> Dict[str, Any]:
        if not self.enabled:
            return {'status': 'skipped', 'reason': 'media_ingestion_disabled', 'asset_id': asset.asset_id}
        if not asset.thumbnail_url and not asset.local_media_ref:
            return {'status': 'degraded', 'reason': 'no_media_url', 'asset_id': asset.asset_id}
        return {'status': 'queued', 'reason': '', 'asset_id': asset.asset_id, 'content_hash': asset.content_hash}

    def video_frame_plan(self, asset: AdCreativeAsset, *, duration_ms: Optional[int] = None) -> List[AdCreativeFrameAnalysis]:
        if not self.enabled or asset.asset_type != 'video':
            return []
        points = [0, 1000, 3000, 5000]
        if duration_ms and duration_ms > 8000:
            points.extend([duration_ms // 2, max(duration_ms - 1000, 0)])
        return [
            AdCreativeFrameAnalysis(
                frame_id=f'frame_{stable_id(asset.asset_id, point)}',
                asset_id=asset.asset_id,
                video_id=asset.video_id,
                timestamp_ms=point,
                frame_ref=f'{asset.asset_id}:{point}',
                analysis_status='pending',
            )
            for point in sorted(set(points))
        ]


class CreativeAnalysisService:
    def __init__(
        self,
        *,
        vision_analyzer: Optional[CreativeVisionAnalyzer] = None,
        ocr_analyzer: Optional[CreativeOcrAnalyzer] = None,
        text_analyzer: Optional[CreativeTextAnalyzer] = None,
        analysis_version: str = CREATIVE_INTELLIGENCE_SCHEMA_VERSION,
    ) -> None:
        fixture = FixtureCreativeAnalyzer()
        self.vision_analyzer = vision_analyzer or fixture
        self.ocr_analyzer = ocr_analyzer or fixture
        self.text_analyzer = text_analyzer or fixture
        self.analysis_version = analysis_version

    def analyze_asset(self, asset: AdCreativeAsset) -> AdCreativeAnalysis:
        analyzed_at = utc_now()
        context = asdict(asset)
        try:
            image_ref = asset.local_media_ref or asset.thumbnail_url or asset.asset_id
            visual = self.vision_analyzer.analyze_image(image_ref, context)
            ocr = self.ocr_analyzer.extract_text(image_ref, context)
            copy = self.text_analyzer.analyze_copy(asset.body_text, asset.title_text, asset.description_text, asset.cta_type, context)
            risk_tags = normalize_tags([*visual.risk_tags, *ocr.risk_tags, *copy.risk_tags] or ['无明显风险'], RISK_TAGS)
            confidence = round((visual.confidence + ocr.confidence + copy.confidence) / 3, 4)
            return AdCreativeAnalysis(
                analysis_id=f'analysis_{stable_id(asset.asset_id, self.analysis_version, asset.content_hash)}',
                asset_id=asset.asset_id,
                analysis_version=self.analysis_version,
                analysis_status='ok',
                analyzer_type='fixture',
                analyzed_at=analyzed_at,
                visual_tags=normalize_tags(visual.visual_tags, CREATIVE_FORMATS),
                ocr_text=ocr.ocr_text,
                copy_tags=copy.copy_tags,
                language_detected=copy.language_detected,
                localization_score=copy.localization_score,
                risk_tags=risk_tags,
                creative_format=visual.creative_format,
                hook_type=visual.hook_type,
                value_proposition_tags=normalize_tags(copy.value_proposition_tags, VALUE_PROPOSITIONS),
                trust_signal_tags=normalize_tags(visual.trust_signal_tags, TRUST_SIGNAL_TAGS),
                readability_score=copy.readability_score,
                quality_flags=copy.quality_flags,
                confidence=confidence,
                failure_reason='',
            )
        except Exception as exc:
            return AdCreativeAnalysis(
                analysis_id=f'analysis_{stable_id(asset.asset_id, self.analysis_version, "failed")}',
                asset_id=asset.asset_id,
                analysis_version=self.analysis_version,
                analysis_status='failed',
                analyzer_type='fixture',
                analyzed_at=analyzed_at,
                risk_tags=['人工复核'],
                confidence=0.0,
                failure_reason=safe_error_reason(exc.__class__.__name__),
            )


def object_matches_asset(item: Any, asset: AdCreativeAsset) -> bool:
    item_ad = str(getattr(item, 'ad', '') or '')
    item_group = str(getattr(item, 'ad_group', '') or '')
    item_campaign = str(getattr(item, 'campaign', '') or '')
    return bool(
        (asset.ad_id and asset.ad_id == item_ad)
        or (asset.ad_id and asset.ad_id in {str(getattr(item, 'object_id', '') or '')})
        or (asset.title_text and asset.title_text == item_ad)
        or (asset.adset_id and asset.adset_id == item_group)
        or (asset.campaign_id and asset.campaign_id == item_campaign)
        or (asset.ad_id and asset.ad_id in item_ad)
    )


def build_creative_performance_daily(
    report_date: str,
    assets: Iterable[AdCreativeAsset],
    ad_objects: Iterable[Any],
) -> List[AdCreativePerformanceDaily]:
    rows: List[AdCreativePerformanceDaily] = []
    object_list = list(ad_objects or [])
    for asset in assets:
        matched = [item for item in object_list if object_matches_asset(item, asset)]
        if not matched:
            continue
        for item in matched:
            spend = float(getattr(item, 'spend', 0.0) or 0.0)
            clicks = float(getattr(item, 'clicks', 0.0) or 0.0)
            impressions = float(getattr(item, 'impressions', 0.0) or 0.0)
            installs = float(getattr(item, 'installs', 0.0) or 0.0)
            real_binds = int(getattr(item, 'real_bind_count', 0) or 0)
            af_joins = float(getattr(item, 'af_guild_joins', 0.0) or 0.0)
            dynamic = bool(asset.is_dynamic_creative)
            grain = 'dynamic' if dynamic else 'ad'
            warning = '当前仅能归因到广告级，该广告包含多个素材元素，不能代表单个素材元素的独立贡献。'
            if dynamic:
                warning = '当前为动态素材组合级表现，不能代表单个素材元素的独立贡献。'
            rows.append(AdCreativePerformanceDaily(
                report_date_london=report_date,
                asset_id=asset.asset_id,
                creative_id=asset.creative_id,
                ad_id=asset.ad_id or str(getattr(item, 'ad', '') or ''),
                adset_id=asset.adset_id or str(getattr(item, 'ad_group', '') or ''),
                campaign_id=asset.campaign_id or str(getattr(item, 'campaign', '') or ''),
                country=str(getattr(item, 'country', '') or asset.country),
                project=str(getattr(item, 'project', '') or asset.project),
                spend=round(spend, 4),
                impressions=round(impressions, 4),
                clicks=round(clicks, 4),
                ctr=round(float(getattr(item, 'ctr', 0.0) or (clicks / impressions if impressions else 0.0)), 6),
                cpm=round(float(getattr(item, 'cpm', 0.0) or (spend / impressions * 1000 if impressions else 0.0)), 4),
                installs=round(installs, 4),
                cpi=round(spend / installs, 4) if installs else None,
                af_model_join_events=af_joins,
                tugao_real_bind_count=real_binds,
                real_bind_cpa=round(spend / real_binds, 4) if real_binds else None,
                af_to_real_bind_rate=round(real_binds / af_joins, 4) if af_joins else None,
                data_quality_status=str(getattr(getattr(item, 'data_quality', None), 'status', 'ok') or 'ok'),
                attribution_level=ATTRIBUTION_GRAIN_ZH[grain],
                creative_grain=grain,
                is_dynamic_creative=dynamic,
                grain_warning=warning,
            ))
    return rows


def aggregate_direction_insights(
    assets: Iterable[AdCreativeAsset],
    analyses_by_asset: Dict[str, AdCreativeAnalysis],
    performance_rows: Iterable[AdCreativePerformanceDaily],
) -> List[CreativeDirectionInsight]:
    asset_map = {asset.asset_id: asset for asset in assets}
    buckets: Dict[str, Dict[str, Any]] = {}
    for perf in performance_rows:
        analysis = analyses_by_asset.get(perf.asset_id)
        direction = (analysis.hook_type if analysis and analysis.hook_type != '未识别' else '') or (analysis.creative_format if analysis else '') or '未识别'
        bucket = buckets.setdefault(direction, {'spend': 0.0, 'clicks': 0.0, 'impressions': 0.0, 'installs': 0.0, 'af': 0.0, 'real': 0, 'confidence': [], 'grains': set()})
        bucket['spend'] += perf.spend
        bucket['clicks'] += perf.clicks
        bucket['impressions'] += perf.impressions
        bucket['installs'] += perf.installs
        bucket['af'] += perf.af_model_join_events
        bucket['real'] += perf.tugao_real_bind_count
        bucket['confidence'].append((analysis.confidence if analysis else 0.25))
        bucket['grains'].add(perf.creative_grain)
        _ = asset_map.get(perf.asset_id)
    insights: List[CreativeDirectionInsight] = []
    for direction, bucket in buckets.items():
        spend = float(bucket['spend'])
        real = int(bucket['real'])
        cpi = round(spend / bucket['installs'], 4) if bucket['installs'] else None
        real_cpa = round(spend / real, 4) if real else None
        ctr = round(bucket['clicks'] / bucket['impressions'], 6) if bucket['impressions'] else 0.0
        judgment, next_step = diagnose_direction(
            spend=spend,
            ctr=ctr,
            cpi=cpi,
            af_model_join_events=float(bucket['af']),
            real_bind_count=real,
            real_bind_cpa=real_cpa,
        )
        grain = 'dynamic' if 'dynamic' in bucket['grains'] else ('ad' if bucket['grains'] else 'unknown')
        insights.append(CreativeDirectionInsight(
            direction=direction,
            spend=round(spend, 4),
            ctr=ctr,
            cpi=cpi,
            af_model_join_events=round(float(bucket['af']), 4),
            tugao_real_bind_count=real,
            real_bind_cpa=real_cpa,
            judgment=judgment,
            next_step=next_step,
            attribution_grain=ATTRIBUTION_GRAIN_ZH.get(grain, '无法判断'),
            confidence=round(sum(bucket['confidence']) / len(bucket['confidence']), 4) if bucket['confidence'] else 0.0,
        ))
    insights.sort(key=lambda item: (item.judgment == '优先延展', item.tugao_real_bind_count, item.spend), reverse=True)
    return insights


def diagnose_direction(
    *,
    spend: float,
    ctr: float,
    cpi: Optional[float],
    af_model_join_events: float,
    real_bind_count: int,
    real_bind_cpa: Optional[float],
) -> Tuple[str, str]:
    if real_bind_count >= 10 and real_bind_cpa is not None and real_bind_cpa <= 0.9:
        return DIAGNOSIS_ZH['winner_extension'], '保留核心卖点，换人物、开头 3 秒、场景和本地化表达。'
    if af_model_join_events >= 5 and real_bind_count == 0:
        return DIAGNOSIS_ZH['funnel_repair'], 'AF 信号好但真实入会弱，减少纯收益刺激，增加流程和资格说明。'
    if ctr >= 0.04 and real_bind_count == 0 and spend > 0:
        return DIAGNOSIS_ZH['funnel_repair'], '点击高但真实入会弱，排查好奇点击、素材承诺和 App 承接不一致。'
    if spend == 0 or real_bind_count < 3:
        return DIAGNOSIS_ZH['controlled_exploration'], '小预算受控探索，先补足最低样本，不直接放量。'
    if real_bind_cpa is not None and real_bind_cpa > 1.4:
        return DIAGNOSIS_ZH['manual_review'], '真实入会成本高，先人工复核素材风险和承接链路。'
    if cpi is not None and cpi <= 0.25 and real_bind_count < 3:
        return DIAGNOSIS_ZH['funnel_repair'], '安装便宜但真实入会弱，素材可能吸引低意向用户。'
    return DIAGNOSIS_ZH['controlled_exploration'], '继续观察并做受控对照测试。'


def build_creative_experiment_plans(
    direction_insights: Iterable[CreativeDirectionInsight],
    performance_rows: Iterable[AdCreativePerformanceDaily],
) -> List[CreativeExperimentPlan]:
    by_direction = list(direction_insights)
    perf_by_direction: Dict[str, List[AdCreativePerformanceDaily]] = {}
    for perf in performance_rows:
        perf_by_direction.setdefault(perf.asset_id, []).append(perf)
    plans: List[CreativeExperimentPlan] = []
    for insight in by_direction[:12]:
        plan_type = 'winner_extension' if insight.judgment == '优先延展' else ('funnel_repair' if insight.judgment == '漏斗修复' else 'controlled_exploration')
        country = ''
        project = ''
        linked_ads: List[str] = []
        control_asset_id = ''
        for perf_rows in perf_by_direction.values():
            for perf in perf_rows:
                if not control_asset_id:
                    control_asset_id = perf.asset_id
                if not country:
                    country = perf.country
                if not project:
                    project = perf.project
                linked_ads.append(perf.ad_id)
                break
            if control_asset_id:
                break
        if plan_type == 'winner_extension':
            hypothesis = '保留当前有效素材方向，替换人物、场景或前 3 秒钩子后，真实入会成本仍能保持达标。'
            changed = '只改变人物/场景/首屏表达，不同时改预算和广告结构。'
            visual = '延续现有赢家方向，增加本地人物或 App 流程露出，结尾明确 CTA。'
        elif plan_type == 'funnel_repair':
            hypothesis = '提前说明注册、绑定和申请入会流程，可以减少低意向点击，提高真实绑定率。'
            changed = '首屏钩子从收益刺激改为流程透明 + 新手支持。'
            visual = '真人口播 + App 流程截图，少用夸张金额，增加适合人群说明。'
        else:
            hypothesis = '用小预算测试新表达，和当前方向做对照，避免直接放量未验证素材。'
            changed = '仅改变首屏钩子或素材形式。'
            visual = '准备 2-3 个本地化素材方向，每个方向保持单一变量。'
        plan_id = f'creative_plan_{stable_id(insight.direction, insight.judgment, control_asset_id)}'
        plans.append(CreativeExperimentPlan(
            plan_id=plan_id,
            experiment_name=f'{country or "多国家"}{insight.direction}素材测试',
            country=country or 'UNKNOWN',
            project=project or 'unknown_project',
            current_direction=insight.direction,
            current_problem=insight.next_step,
            hypothesis=hypothesis,
            control_asset_id=control_asset_id,
            changed_variable=changed,
            suggested_format='真人口播 + App 流程截图' if plan_type != 'controlled_exploration' else '受控新方向',
            hook_type=insight.direction,
            visual_brief=visual,
            localized_copy='Use a local-language headline that explains the joining flow before promising benefits.',
            chinese_meaning='用本地语言先讲清入会流程，再表达收益或支持。',
            success_metric='真实入会成本低于国家红线，且 AF 到真实绑定转化率高于当前方向均值 20%。',
            minimum_sample='至少 10 个真实入会或达到国家红线 3 倍消耗。',
            stop_condition='D+2 后消耗达到红线 3 倍且真实入会为 0，或真实入会成本超过红线 30%。',
            data_window='按最新完整 UTC 日 + 近 7 天复核。',
            linked_ads=sorted(set(linked_ads))[:20],
            enter_knowledge_base=insight.judgment in {'优先延展', '漏斗修复'},
            plan_type=plan_type,
        ))
    return plans


def build_creative_knowledge_entries(
    plans: Iterable[CreativeExperimentPlan],
    direction_insights: Iterable[CreativeDirectionInsight],
    *,
    report_id: str,
    evidence_window: str,
) -> List[Dict[str, Any]]:
    insights_by_direction = {item.direction: item for item in direction_insights}
    entries: List[Dict[str, Any]] = []
    for plan in plans:
        if not plan.enter_knowledge_base:
            continue
        insight = insights_by_direction.get(plan.current_direction)
        entry_type = '赢家素材方向' if plan.plan_type == 'winner_extension' else '漏斗修复有效案例'
        payload = {
            'entry_type': entry_type,
            'creative_tags': [plan.current_direction],
            'asset_id': plan.control_asset_id,
            'linked_ads': plan.linked_ads,
            'report_id': report_id,
            'real_bind_performance': asdict(insight) if insight else {},
            'country': plan.country,
            'project': plan.project,
            'applicable_conditions': ['同国家', '同项目', '同素材方向'],
            'not_applicable_conditions': ['动态素材单元素归因不足', '预算和素材同时变化'],
            'evidence_window': evidence_window,
            'review_status': '待复核',
        }
        entries.append({
            'knowledge_id': f'creative_kb_{stable_id(report_id, plan.plan_id)}',
            'entry_type': entry_type,
            'asset_id': plan.control_asset_id,
            'payload': payload,
            'evidence_window': evidence_window,
            'review_status': '待复核',
            'created_at': utc_now(),
        })
    return entries


def persist_creative_knowledge_entries(conn: sqlite3.Connection, entries: Iterable[Dict[str, Any]]) -> int:
    ensure_creative_intelligence_tables(conn)
    count = 0
    for entry in entries:
        conn.execute(
            """
            INSERT OR REPLACE INTO ad_creative_strategy_knowledge
            (knowledge_id, entry_type, asset_id, payload_json, evidence_window, review_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry['knowledge_id'], entry['entry_type'], entry.get('asset_id') or '',
                json.dumps(entry.get('payload') or {}, ensure_ascii=False, sort_keys=True),
                entry['evidence_window'], entry['review_status'], entry['created_at'],
            ),
        )
        count += 1
    conn.commit()
    return count


def build_fixture_creative_assets_for_report(ad_objects: Iterable[Any]) -> List[AdCreativeAsset]:
    assets: List[AdCreativeAsset] = []
    now = utc_now()
    for item in list(ad_objects or [])[:80]:
        country = str(getattr(item, 'country', '') or '')
        ad = str(getattr(item, 'ad', '') or '')
        campaign = str(getattr(item, 'campaign', '') or '')
        binds = int(getattr(item, 'real_bind_count', 0) or 0)
        body = '流程透明，新手支持，申请后完成绑定即可入会。'
        title = ad or campaign or '素材'
        cta = 'SIGN_UP'
        if binds >= 10:
            body = '真实案例展示，公会支持，新人可快速申请。'
        elif 'zero' in ad.lower() or binds == 0:
            body = 'Earn money fast $ today WhatsApp WA contact, simple job.'
            cta = 'APPLY_NOW'
        payload = {
            'platform': 'meta',
            'account_id': str(getattr(item, 'account_id', '') or ''),
            'campaign_id': campaign,
            'adset_id': str(getattr(item, 'ad_group', '') or ''),
            'ad_id': ad,
            'creative_id': f'cr_{stable_id(country, campaign, ad)}',
            'asset_type': 'video' if binds >= 10 else 'image',
            'body_text': body,
            'title_text': title,
            'description_text': f'{country} 素材验证',
            'cta_type': cta,
            'country': country,
            'project': str(getattr(item, 'project', '') or ''),
            'first_seen_at': now,
            'last_seen_at': now,
        }
        assets.append(creative_asset_from_meta_payload(payload, now=now))
    return assets


def build_creative_intelligence_payload(
    report: Any,
    *,
    conn: Optional[sqlite3.Connection] = None,
    feature_flags: Optional[Dict[str, bool]] = None,
    assets: Optional[List[AdCreativeAsset]] = None,
) -> Dict[str, Any]:
    flags = dict(CREATIVE_FEATURE_FLAGS)
    flags.update(feature_flags or {})
    owned_conn = False
    if conn is not None:
        ensure_creative_intelligence_tables(conn)
        if assets is None:
            loaded_assets = load_creative_assets(conn)
            assets = loaded_assets or build_fixture_creative_assets_for_report(getattr(report, 'ad_objects', []))
    if assets is None:
        assets = build_fixture_creative_assets_for_report(getattr(report, 'ad_objects', []))
    if conn is not None and assets:
        persist_creative_assets(conn, assets)

    analyzer = CreativeAnalysisService()
    analyses = [analyzer.analyze_asset(asset) for asset in assets]
    if conn is not None and analyses:
        persist_creative_analysis(conn, analyses)
        analyses_by_asset = load_latest_creative_analysis(conn)
    else:
        analyses_by_asset = {item.asset_id: item for item in analyses}

    performance = build_creative_performance_daily(str(getattr(report, 'report_date', '') or date.today().isoformat()), assets, getattr(report, 'ad_objects', []))
    directions = aggregate_direction_insights(assets, analyses_by_asset, performance)
    plans = build_creative_experiment_plans(directions, performance)
    evidence_window = f"{getattr(report, 'window_start_utc', '')} - {getattr(report, 'window_end_utc', '')}"
    knowledge_entries = build_creative_knowledge_entries(plans, directions, report_id=str(getattr(report, 'report_id', '') or ''), evidence_window=evidence_window)
    if conn is not None and flags.get('AD_CREATIVE_KNOWLEDGE_SYNC_ENABLED'):
        persist_creative_knowledge_entries(conn, knowledge_entries)

    synced_count = len(assets)
    analyzable_count = sum(1 for asset in assets if asset.thumbnail_url or asset.body_text or asset.title_text or asset.local_media_ref)
    ad_level_count = sum(1 for row in performance if row.creative_grain == 'ad')
    dynamic_count = sum(1 for asset in assets if asset.is_dynamic_creative)
    failed_count = sum(1 for item in analyses if item.analysis_status != 'ok')
    latest_sync = max([asset.last_seen_at for asset in assets if asset.last_seen_at] or [''])

    return {
        'schema_version': CREATIVE_INTELLIGENCE_SCHEMA_VERSION,
        'feature_flags': flags,
        'status': {
            'enabled': any(flags.values()),
            'sync_enabled': flags.get('AD_CREATIVE_SYNC_ENABLED', False),
            'synced_asset_count': synced_count,
            'analyzable_asset_count': analyzable_count,
            'ad_level_attribution_count': ad_level_count,
            'dynamic_creative_count': dynamic_count,
            'analysis_failed_count': failed_count,
            'last_synced_at': latest_sync,
            'degradation_notice': (
                '当前未开启生产素材同步，仅展示广告级可见素材线索；不能代表单个素材元素的独立贡献。'
                if not flags.get('AD_CREATIVE_SYNC_ENABLED') else ''
            ),
        },
        'direction_performance': [asdict(item) for item in directions],
        'assets': [
            {
                **asdict(asset),
                'thumbnail_url': safe_media_url(asset.thumbnail_url),
                'preview_url': asset.local_media_ref or safe_media_url(asset.thumbnail_url),
                'thumbnail_available': bool(asset.thumbnail_url or asset.local_media_ref),
                'copy_fragments': creative_copy_fragments(asset),
                'copy_fragment_count': len(creative_copy_fragments(asset)),
                'analysis': asdict(analyses_by_asset.get(asset.asset_id)) if analyses_by_asset.get(asset.asset_id) else {},
                'expression_diagnosis': build_ad_expression_diagnosis(asset, analyses_by_asset.get(asset.asset_id)),
                'grain_warning': next((row.grain_warning for row in performance if row.asset_id == asset.asset_id), ''),
            }
            for asset in assets[:500]
        ],
        'performance_daily': [asdict(item) for item in performance[:200]],
        'winner_commonalities': summarize_winner_commonalities(directions, analyses_by_asset),
        'low_quality_click_assets': [
            asdict(item) for item in directions
            if item.judgment in {'漏斗修复', '人工复核'} and item.spend > 0
        ][:20],
        'fatigue_signals': [
            asdict(item) for item in directions
            if item.judgment == '优先延展' and item.ctr < 0.025
        ][:20],
        'experiment_plans': [asdict(item) for item in plans],
        'knowledge_entries': knowledge_entries,
        'guardrails': [
            '不自动发布素材',
            '不自动修改 Meta 广告',
            '不自动修改预算',
            '广告级或动态素材组合级归因不能代表单个素材元素独立贡献',
        ],
    }


def summarize_winner_commonalities(
    direction_insights: Iterable[CreativeDirectionInsight],
    analyses_by_asset: Dict[str, AdCreativeAnalysis],
) -> List[Dict[str, Any]]:
    winners = [item for item in direction_insights if item.judgment == '优先延展']
    if not winners:
        return []
    return [
        {
            'direction': item.direction,
            'common_tags': [item.direction],
            'difference_from_expensive_assets': '真实入会成本更低，建议优先保留该方向并做单变量延展。',
            'real_bind_cpa': item.real_bind_cpa,
            'confidence': item.confidence,
        }
        for item in winners[:10]
    ]
