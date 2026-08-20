from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from app.growth.creative_naming import next_launch_creative_name


CREATIVE_IMAGE_GENERATION_SCHEMA_VERSION = 'creative_image_generation_v1'
FEED_STATIC_AD_SURFACE = 'feed_static_ad'
DEFAULT_FEED_IMAGE_SIZE = (1024, 1024)
CHATGPT_PRO_ACCEPTED_IMAGE_SIZES = {
    (1024, 1024),
    (512, 512),
}
DEFAULT_IMAGE_OUTPUT_DIR = Path(__file__).resolve().parents[1] / 'data' / 'ad_creative_generated_images'
PROVIDER_FIXTURE = 'fixture_svg'
PROVIDER_EXTERNAL_WRAPPER = 'external_wrapper'
PROVIDER_CHATGPT_PRO_MANUAL = 'chatgpt_pro_manual'
PROVIDER_LOCAL_PRODUCTION_PNG = 'local_production_png'
PROVIDER_HERMES_IMAGE2_AGENT = 'hermes_image2_agent'
HERMES_TASK_STATUS_QUEUED = 'queued'
HERMES_TASK_STATUS_CLAIMED = 'claimed'
HERMES_TASK_STATUS_UPLOADED = 'uploaded'
HERMES_TASK_STATUS_REJECTED = 'rejected'
HERMES_TASK_STATUS_FAILED = 'failed'
HERMES_TASK_STATUS_CANCELLED = 'cancelled'
HERMES_TASK_STATUS_EXPIRED = 'expired'
IMAGE_PROVIDER_BINARY_TYPES = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/jpg': '.jpg',
    'image/webp': '.webp',
}

IMAGE_BINARY_SIGNATURES = {
    'image/png': b'\x89PNG\r\n\x1a\n',
    'image/jpeg': b'\xff\xd8',
    'image/webp': b'RIFF',
}
CREATIVE_IMAGE_PREVIEW_MAX_SIZE = 512


class InvalidCreativeImageError(ValueError):
    pass


EXPERIMENT_MODE_REPLACEMENT = 'replacement'
EXPERIMENT_MODE_NEW_TEST = 'new_test'

BINDING_CONFIDENCE_HIGH = 'HIGH'
BINDING_CONFIDENCE_MEDIUM = 'MEDIUM'
BINDING_CONFIDENCE_LOW = 'LOW'
BINDING_CONFIDENCE_MANUAL_CONFIRMED = 'MANUAL_CONFIRMED'

BINDING_METHOD_EXPERIMENT_ID_NAME_MATCH = 'EXPERIMENT_ID_NAME_MATCH'
BINDING_METHOD_GENERATED_IMAGE_HASH_MATCH = 'GENERATED_IMAGE_HASH_MATCH'
BINDING_METHOD_PERCEPTUAL_HASH_MATCH = 'PERCEPTUAL_HASH_MATCH'
BINDING_METHOD_ORIGINAL_AD_CREATIVE_REPLACED = 'ORIGINAL_AD_CREATIVE_REPLACED'
BINDING_METHOD_MANUAL_AD_ID_BINDING = 'MANUAL_AD_ID_BINDING'
BINDING_METHOD_MANUAL_CREATIVE_ID_BINDING = 'MANUAL_CREATIVE_ID_BINDING'
BINDING_METHOD_TIME_WINDOW_INFERENCE = 'TIME_WINDOW_INFERENCE'
BINDING_METHOD_META_CREATIVE_SYNC_MATCH = 'META_CREATIVE_SYNC_MATCH'
BINDING_METHOD_META_ACTIVITY_MATCH = 'META_ACTIVITY_MATCH'
GENERATION_MODE_NEW_DIRECTION = 'new_direction_generation'
GENERATION_MODE_DIRECTION_REDRAW = 'direction_redraw'
GENERATION_MODE_OLD_IMAGE_REFERENCE_REVISION = 'old_image_reference_revision'
GENERATION_MODE_OLD_IMAGE_REGION_EDIT = 'old_image_region_edit'
CREATIVE_DIRECTION_POINTS_REWARD = 'points_reward'
CREATIVE_DIRECTION_EASY_START = 'easy_start'
CREATIVE_DIRECTION_GUIDED_TRUST = 'guided_trust'
CREATIVE_DIRECTION_SAFE_COMPLIANCE = 'safe_compliance'
FINAL_VERDICT_PENDING_REVIEW = 'pending_review'
FINAL_VERDICT_MANUAL_REVIEW_REQUIRED = 'manual_review_required'
FINAL_VERDICT_AUTO_REJECTED = 'auto_rejected'
FINAL_VERDICT_VALIDATION_FAILED = 'validation_failed'
PROMPT_PACKAGE_VERSION = 'creative_prompt_package_v1_8'
VISIBLE_CLAIM_POLICY_VERSION = 'visible_claim_policy_v1_5'
CURRENCY_THRESHOLD_VERSION = 'currency_threshold_v1_4_1'
OLD_IMAGE_REVISION_BLOCKED_DIAGNOSIS_TYPES = {
    'sample_insufficient',
    'continue_observe',
    'data_anomaly',
    'view_capability_missing',
    'post_funnel_event_inconsistent',
    'creative_effective_post_im_failed',
    'business_result_anomaly',
    'linky_crm_issue',
    'crm_issue',
}
OLD_IMAGE_REVISION_BLOCKED_ACTION_TYPES = {
    'observe',
    'inspect_data_quality',
    'check_linky_bind_crm_tracking',
    'check_view_capability',
    'check_mapping',
    'sync_crm',
}

CONFIDENCE_BY_BINDING_METHOD = {
    BINDING_METHOD_GENERATED_IMAGE_HASH_MATCH: BINDING_CONFIDENCE_HIGH,
    BINDING_METHOD_PERCEPTUAL_HASH_MATCH: BINDING_CONFIDENCE_HIGH,
    BINDING_METHOD_MANUAL_AD_ID_BINDING: BINDING_CONFIDENCE_MANUAL_CONFIRMED,
    BINDING_METHOD_MANUAL_CREATIVE_ID_BINDING: BINDING_CONFIDENCE_MANUAL_CONFIRMED,
    BINDING_METHOD_EXPERIMENT_ID_NAME_MATCH: BINDING_CONFIDENCE_MEDIUM,
    BINDING_METHOD_ORIGINAL_AD_CREATIVE_REPLACED: BINDING_CONFIDENCE_MEDIUM,
    BINDING_METHOD_META_CREATIVE_SYNC_MATCH: BINDING_CONFIDENCE_MEDIUM,
    BINDING_METHOD_META_ACTIVITY_MATCH: BINDING_CONFIDENCE_MEDIUM,
    BINDING_METHOD_TIME_WINDOW_INFERENCE: BINDING_CONFIDENCE_LOW,
}

SUPPORTED_BRAND_MARKETS: Dict[str, Dict[str, Any]] = {
    'ID': {
        'brand': 'TUGAO',
        'market_label': 'Indonesia',
        'language_hint': 'Bahasa Indonesia',
        'headline': 'Mulai tugas dari HP',
        'subheadline': 'Kumpulkan poin dan reward lewat aplikasi',
    },
    'BR': {
        'brand': 'Premiou',
        'market_label': 'Brazil',
        'language_hint': 'Portuguese',
        'headline': 'Ganhe pontos pelo celular',
        'subheadline': 'Tarefas simples, recompensas e orientação no app',
    },
    'ES_LATAM': {
        'brand': 'Recompa',
        'market_label': 'LatAm Spanish',
        'language_hint': 'Spanish',
        'headline': 'Gana puntos desde tu celular',
        'subheadline': 'Tareas simples, recompensas y guía en la app',
    },
    'MX': {
        'brand': 'Recompa',
        'market_label': 'Mexico',
        'language_hint': 'Spanish (Mexico)',
        'headline': 'Gana recompensas desde tu celular',
        'subheadline': 'Tareas simples, recompensas y guía en la app',
    },
    'CO': {'brand': 'Recompa', 'market_label': 'Colombia', 'language_hint': 'Spanish (Colombia)', 'headline': 'Gana recompensas desde tu celular', 'subheadline': 'Tareas simples, recompensas y guía en la app'},
}

PUBLIC_AD_POSITIONING = {
    'category': 'mobile_rewards_task_app',
    'visible_positioning': [
        '手机端积分/任务 App',
        '完成 App 内简单任务获得积分或小额奖励',
        '本地语言指导和新手引导',
        '奖励进度、任务卡片、手机 UI 和品牌收口',
    ],
    'internal_business_goal': [
        '把有网赚意愿的正确用户带进 App/IM',
        '由 IM 承接进一步筛选和转化 MCN/主播链路',
    ],
    'internal_goal_visibility_policy': 'internal_only_never_visible_in_ad',
}

SAFE_COMPLIANCE_POSITIONING = {
    'category': 'mobile_rewards_task_app',
    'visible_positioning': [
        '手机端积分/任务 App 的真实核心使用逻辑',
        '完成 App 内任务并查看积分到账状态',
        '使用积分兑换 App 内可用的非现金奖励',
        '界面可以是随版本变化的概念化 UI，但展示的功能必须与产品对应',
        '不展示现金价值、提现、收入、就业或确定性经济回报',
    ],
}

SAFE_COMPLIANCE_FUNCTIONALITY_CONTRACT = {
    'required_flow': [
        'complete one visible in-app task',
        'show points credited on that same completed task card without any cash conversion',
        'connect the credited points to available in-app rewards with one clear directional transition',
        'show generic non-cash reward options that points can be exchanged for inside the app',
    ],
    'conceptual_ui_policy': (
        'The interface may be a product-faithful conceptual UI rather than an exact screenshot, '
        'because the app UI can change between versions. Every visible module must still map to '
        'the verified product functions in required_flow.'
    ),
    'forbidden_invented_functions': [
        'generic content discovery feed',
        'news, recipes, travel, shopping, courses, entertainment, or home-decor categories',
        'social feed, community, chat, messaging, creator, host, or recruitment modules',
        'wallet, cash balance, withdrawal, payout, salary, income, or job modules',
        'placeholder labels such as Activity 1, Atividade 1, Tarea 1, or Aktivitas 1',
        'a standalone completed row that is not attached to a visible task',
        'cash, currency, merchant-branded gift cards, named prizes, or guaranteed reward values',
        'CTA buttons, in-app action buttons, navigation bars, ribbons, pills, or unreadable microcopy',
    ],
}

SAFE_COMPLIANCE_HEADLINES = {
    'BR': [
        'Complete atividades.\nAcompanhe seu progresso.',
        'Tarefas no app.\nProgresso visível.',
        'Complete tarefas.\nUse seus pontos.',
    ],
    'ID': [
        'Selesaikan aktivitas.\nPantau progresmu.',
        'Tugas di aplikasi.\nProgres yang jelas.',
        'Selesaikan tugas.\nGunakan poinmu.',
    ],
    'ES_LATAM': [
        'Completa actividades.\nSigue tu progreso.',
        'Tareas en la app.\nProgreso visible.',
        'Completa tareas.\nUsa tus puntos.',
    ],
    'MX': [
        'Completa actividades.\nSigue tu progreso.',
        'Tareas en la app.\nProgreso visible.',
        'Completa tareas.\nMira tus recompensas.',
    ],
    'CO': ['Completa actividades.\nSigue tu progreso.', 'Tareas en la app.\nProgreso visible.', 'Completa tareas.\nMira tus recompensas.'],
}

SAFE_COMPLIANCE_SUBHEADLINES = {
    'BR': [
        'Conclua tarefas no app, acumule pontos e veja recompensas disponíveis.',
        'Acompanhe atividades, pontos e recompensas disponíveis no app.',
    ],
    'ID': [
        'Selesaikan tugas di aplikasi, kumpulkan poin, dan lihat hadiah yang tersedia.',
        'Pantau aktivitas, poin, dan hadiah yang tersedia di aplikasi.',
    ],
    'ES_LATAM': [
        'Completa tareas en la app, suma puntos y mira las recompensas disponibles.',
        'Sigue actividades, puntos y recompensas disponibles en la app.',
    ],
    'MX': [
        'Completa tareas en la app y mira las recompensas disponibles.',
        'Sigue actividades y recompensas disponibles en la app.',
    ],
    'CO': ['Completa tareas en la app y mira las recompensas disponibles.', 'Sigue actividades y recompensas disponibles en la app.'],
}

SAFE_COMPLIANCE_PHONE_COPY = {
    'BR': ['Progresso de hoje', 'Atividades diárias', 'Pontos no app', 'Recompensas disponíveis'],
    'ID': ['Progres hari ini', 'Aktivitas harian', 'Poin di aplikasi', 'Hadiah tersedia'],
    'ES_LATAM': ['Progreso de hoy', 'Actividades diarias', 'Puntos en la app', 'Recompensas disponibles'],
    'MX': ['Progreso de hoy', 'Actividades diarias', 'Recompensas en la app', 'Recompensas disponibles'],
    'CO': ['Progreso de hoy', 'Actividades diarias', 'Recompensas en la app', 'Recompensas disponibles'],
}

SAFE_COMPLIANCE_BLUEPRINT_VERSION = 'safe_generation_blueprint_v4'

POSITIVE_DAYLIGHT_ART_DIRECTION = {
    'brightness': 'high-key, sunlit, airy, optimistic, and immediately inviting in a mobile feed',
    'lighting': 'natural daylight or a bright daylight-like studio treatment with clean skin tones and soft readable shadows',
    'palette': 'use a flexible bright color family appropriate to the market and direction; colors are not fixed, but large dark surfaces and gloomy grading are forbidden',
    'download_motivation': 'the first impression should feel friendly, active, trustworthy, and desirable to download without fake urgency or exaggerated claims',
    'commercial_finish': 'finished performance-ad craft rather than a presentation template: every visible object must have intentional typography, spacing, edge treatment, material response, and a clear role in the composition',
    'material_language': 'combine realistic people and environments with polished dimensional product illustration; use coherent highlights, gradients, surface detail, soft contact shadows, and one consistent light source',
    'depth_system': 'build foreground, middle ground, and background through scale variation, overlap, perspective, selective edge cropping, and restrained depth of field; elements must feel staged together rather than pasted side by side',
    'component_craft': 'cards and UI modules need nested hierarchy, icon containers, borders or translucent surfaces, controlled corner radii, readable type, status color, and soft elevation; generic reward cards still need designed iconography and material depth',
    'decorative_rule': 'waves, ribbons, sparkles, coins, diamonds, gifts, badges, geometric forms, and room settings are allowed; each must either communicate product meaning or strengthen focus and spatial depth, and must be removed if it is only filler',
    'forbidden_shortcuts': 'raw circles, triangles, semicircles or rectangles used as placeholder art; flat SVG filler; single-color empty waves; randomly scattered sparkle symbols; generic slide-deck decoration; clip-art pasted without shared lighting, perspective, or contact shadow',
    'avoid': 'night scenes, low-key cinematic lighting, black or deep-navy-dominant canvases, muddy browns, heavy amber grading, casino glow, oppressive shadows, somber luxury styling, and unfinished primitive geometric decoration',
}


def safe_compliance_generation_blueprint(market: str, brand: str) -> Dict[str, Any]:
    headlines = SAFE_COMPLIANCE_HEADLINES.get(market) or ['Discover how the app works']
    subheadlines = SAFE_COMPLIANCE_SUBHEADLINES.get(market) or ['Complete app tasks, track progress, and view available rewards.']
    return {
        'version': SAFE_COMPLIANCE_BLUEPRINT_VERSION,
        'authority': 'sole_authoritative_first_pass_template',
        'intent': 'production-ready Meta feed ad with product proof as the focal point',
        'format': '1024x1024 full-bleed square',
        'visual_hierarchy': {
            'phone_product_proof': '48-58% of canvas; unmistakable smartphone dashboard is the primary product proof',
            'adult_woman': '20-28% of canvas; natural, positive supporting usage context, never a dark stock-photo hero',
            'copy_block': 'compact upper-left area with one readable headline and one concise supporting sentence',
            'feature_proof': 'two or three icon-led feature cards may summarize daily activities, clear progress, and in-app points; if used, they must be fully designed product components rather than simple geometric placeholders',
            'brand_footer': 'slim light or high-key brand area integrated into the composition; reserve the rightmost 28-32% for a verified compact light brand card overlay',
        },
        'visible_copy': {
            'headline_candidates': list(headlines),
            'subheadline_candidates': list(subheadlines),
            'phone_copy_semantics': SAFE_COMPLIANCE_PHONE_COPY.get(market, ['Today progress', 'Daily activities', 'Points in app', 'Rewards available']),
            'semantic_requirement': 'wording may vary naturally in the market language, but it must preserve the same meaning: app activities, visible progress, in-app points, and rewards available in the app',
            'copy_policy': 'use one concise headline, one supporting sentence, and a small readable set of product labels; avoid fixed-template repetition, CTA buttons, legal-style microcopy, and text too small for a mobile feed',
        },
        'product_modules_exactly': [
            'one large smartphone dashboard with named in-app activity rows, visible completion states, and one internally consistent progress summary',
            'one readable in-app points state connected visually to the completed activity; fictional P tokens are optional and must never resemble money',
            'one clear reward destination with an open gift box or generic unbranded reward cards representing options available in the app',
        ],
        'product_truth': SAFE_COMPLIANCE_FUNCTIONALITY_CONTRACT,
        'art_direction': {
            **POSITIVE_DAYLIGHT_ART_DIRECTION,
            'finish': 'information-rich but ordered commercial poster finish with strong scale, clear typography, dimensional reward illustration, layered component detail, coherent daylight, and a single readable product story; never settle for a minimal slide-template treatment',
            'phone_policy': 'conceptual version-flexible UI is allowed, but every module must map to verified product functions',
            'flow': 'in-app activities -> visible progress -> in-app points -> available in-app rewards',
        },
        'brand_closure': {
            'brand': brand,
            'model_logo_policy': 'do not draw or write any logo or wordmark',
            'system_overlay': 'verified official logo and brand wordmark are composited after generation',
        },
        'forbidden': SAFE_COMPLIANCE_VISIBLE_CLAIM_POLICY['forbidden_claims'] + [
            'money, currency symbols, cash-value reward modules, wealth imagery, or financial framing',
            'old-image layout preservation or source-image structure in new generation',
            'repeated copy, placeholder task names, empty decorative cards, or dead footer space',
            'giant cropped phone, sparse template, generic stock-photo hero, awkward open-palm presentation pose, or gloomy dark visual treatment',
            'raw primitive shapes used as reward art, flat SVG placeholders, single-color wave filler, random sparkle filler, or generic presentation-slide decoration',
            'any CTA button, in-app button, ribbon, pill, tab bar, navigation bar, tiny helper text, or legal-style microcopy',
        ],
    }

OFFICIAL_APP_LOGO_PATH = '/static/brand/app-logo.png.asset'
OFFICIAL_APP_LOGO_SHA256 = 'c1113ae014728a27d5ee54dbe6e4548e3acee6e4ae12bcdb74ccff3ddd495de2'

VISIBLE_CLAIM_POLICY = {
    'version': VISIBLE_CLAIM_POLICY_VERSION,
    'allowed_claims': [
        'points/rewards for completing in-app tasks',
        'simple mobile tasks',
        'beginner guidance inside the app',
        'local-language support',
        'reward progress or small task reward examples within configured thresholds',
        'visible local-currency reward module with small contextual amounts',
    ],
    'manual_review_claims': [
        'wallet balance',
        'daily earning/reward totals',
        'large point totals without context',
        'strong income-adjacent phrasing',
    ],
    'forbidden_claims': [
        'guaranteed income',
        'fixed salary',
        'cash piles or cash rain',
        'withdraw proof',
        'chat-to-earn',
        'creator/social chat/host recruitment',
        'MCN/guild/anchor conversion',
    ],
}

SAFE_COMPLIANCE_VISIBLE_CLAIM_POLICY = {
    'allowed_claims': [
        'complete a visible in-app task',
        'track points credited on the completed task inside the app',
        'show fictional P app-point tokens as a non-cash product state',
        'exchange points for generic available in-app rewards without cash value or payout claims',
    ],
    'manual_review_claims': [
        'large point totals without context',
        'reward language that could imply cash value',
        'redemption terminology without clear non-cash context',
    ],
    'forbidden_claims': [
        'money, currency, cash value, balance, wallet, payout, withdrawal, salary, or guaranteed benefit',
        'job, employment, recruitment, income, earning, or work-from-home opportunity',
        'task or points converted into money or presented as an income opportunity',
        'invented content, social, chat, shopping, course, news, travel, recipe, entertainment, or recruitment features',
        'counterfeit, third-party, or regenerated brand marks',
        'fake app UI, fake notifications, fake testimonials, or fake native CTA buttons',
    ],
}

LOCALIZED_NEGATIVE_CONSTRAINTS = {
    'EN': 'No guaranteed income, fixed salary, cash piles, cash rain, chat-to-earn claims, creator recruitment, social chatting job claims, host recruitment, MCN, or guild conversion language.',
    'PT': 'Não mostre renda garantida, salário fixo, pilhas de dinheiro, chuva de dinheiro, promessa de ganhar dinheiro conversando, recrutamento de criador/anfitrião, MCN ou guilda.',
    'ID': 'Jangan tampilkan penghasilan tetap, jaminan uang, tumpukan uang, hujan uang, klaim mendapat uang dari chat, rekrutmen kreator/host, MCN, atau guild.',
    'ES': 'No muestres ingresos garantizados, salario fijo, montones de dinero, lluvia de dinero, promesas de ganar dinero chateando, reclutamiento de creador/anfitrión, MCN o guild.',
}

FEMALE_ONLY_VISUAL_REQUIREMENT = (
    'Human visual requirement: the main person must be an adult woman/female. '
    'Use feminine styling and body/face presentation appropriate for the market.'
)

_MALE_NEGATIVE_PROMPT_RE = re.compile(
    r'\b(male|man|boy|husband|boyfriend)\b|男性|男生|男人|男主角',
    re.I,
)


def _sanitize_generation_negative_prompt(value: Any) -> str:
    parts = re.split(r'[;\n]+', str(value or ''))
    safe_parts = [part.strip() for part in parts if part.strip() and not _MALE_NEGATIVE_PROMPT_RE.search(part)]
    return '; '.join(safe_parts)

CURRENCY_REWARD_THRESHOLDS = {
    'ID': {
        'currency': 'Rp',
        'exact_task_reward': 3000,
        'task_reward': {'pass_min': 1000, 'pass_max': 3000, 'manual_max': 3000},
        'daily_reward': {'pass_max': 20000, 'manual_max': 50000},
        'wallet_balance': {'pass_max': 0, 'manual_max': 100000},
    },
    'BR': {
        'currency': 'R$',
        'exact_task_reward': 3,
        'task_reward': {'pass_min': 1, 'pass_max': 3, 'manual_max': 3},
        'daily_reward': {'pass_max': 10, 'manual_max': 30},
        'wallet_balance': {'pass_max': 0, 'manual_max': 100},
    },
    'MX': {
        'currency': 'MX$',
        'exact_task_reward': 5,
        'task_reward': {'pass_min': 1, 'pass_max': 10, 'manual_max': 10},
        'daily_reward': {'pass_min': 1, 'pass_max': 30, 'manual_max': 50},
        'wallet_balance': {'pass_max': 0, 'manual_max': 0},
    },
    'ES_LATAM': {
        'currency': 'points',
        'exact_task_reward': 20,
        'task_reward': {'pass_max': 20, 'manual_max': 50},
        'daily_reward': {'pass_max': 100, 'manual_max': 200},
        'wallet_balance': {'pass_max': 0, 'manual_max': 0},
    },
    'CO': {'currency': 'COP$', 'exact_task_reward': 1000, 'task_reward': {'pass_min': 500, 'pass_max': 2000, 'manual_max': 2000}, 'daily_reward': {'pass_min': 500, 'pass_max': 8000, 'manual_max': 15000}, 'wallet_balance': {'pass_max': 0, 'manual_max': 0}},
}


def currency_reward_generation_contract(market: str) -> Dict[str, Any]:
    thresholds = dict(CURRENCY_REWARD_THRESHOLDS.get(str(market or '').strip()) or {})
    if not thresholds:
        return {}
    currency = str(thresholds.get('currency') or '').strip()
    exact_amount = float(thresholds.get('exact_task_reward') or 0)
    task_rule = dict(thresholds.get('task_reward') or {})
    daily_rule = dict(thresholds.get('daily_reward') or {})
    wallet_rule = dict(thresholds.get('wallet_balance') or {})

    def _number(value: Any) -> Any:
        number = float(value or 0)
        return int(number) if number.is_integer() else number

    exact_display = _number(exact_amount)
    if currency == 'Rp':
        exact_visible_text = f'Rp {int(exact_amount):,}'.replace(',', '.')
    elif currency == 'R$':
        exact_visible_text = f'R$ {exact_amount:.2f}'.replace('.', ',')
    elif currency == 'MX$':
        exact_visible_text = f'MX$ {exact_display}'
    elif currency == 'COP$':
        exact_visible_text = f'$ {int(exact_amount):,} COP'.replace(',', '.')
    else:
        exact_visible_text = f'{exact_display} puntos'
    return {
        'version': CURRENCY_THRESHOLD_VERSION,
        'market': market,
        'unit': currency,
        'required_visible_task_reward': exact_visible_text,
        'task_reward': {
            'pass_min_inclusive': _number(task_rule.get('pass_min')),
            'pass_max_inclusive': _number(task_rule.get('pass_max')),
            'manual_review_min_exclusive': _number(task_rule.get('pass_max')),
            'manual_review_max_inclusive': _number(task_rule.get('manual_max')),
            'reject_below': _number(task_rule.get('pass_min')),
            'reject_above': _number(task_rule.get('manual_max')),
        },
        'daily_reward': {
            'pass_min_inclusive': 0,
            'pass_max_inclusive': _number(daily_rule.get('pass_max')),
            'manual_review_min_exclusive': _number(daily_rule.get('pass_max')),
            'manual_review_max_inclusive': _number(daily_rule.get('manual_max')),
            'reject_above': _number(daily_rule.get('manual_max')),
        },
        'wallet_balance': {
            'pass_min_inclusive': 0,
            'pass_max_inclusive': _number(wallet_rule.get('pass_max')),
            'manual_review_min_exclusive': _number(wallet_rule.get('pass_max')),
            'manual_review_max_inclusive': _number(wallet_rule.get('manual_max')),
            'reject_above': _number(wallet_rule.get('manual_max')),
        },
        'generation_rule': (
            f'Show exactly one task-reward amount: {exact_visible_text}. '
            'Do not invent, vary, add, or repeat any other reward, daily-total, wallet, balance, or currency amount.'
        ),
    }


def _append_currency_reward_constraint(prompt: str, market: str) -> str:
    contract = currency_reward_generation_contract(market)
    if not contract:
        return str(prompt or '').strip()
    marker = 'Exact numeric reward contract:'
    base = str(prompt or '').strip()
    if marker in base:
        base = base.split(marker, 1)[0].rstrip()
    task = contract['task_reward']
    daily = contract['daily_reward']
    wallet = contract['wallet_balance']
    return '\n'.join([
        base,
        (
            f"{marker} {contract['generation_rule']} "
            f"Task reward: pass {task['pass_min_inclusive']}-{task['pass_max_inclusive']}, reject below {task['reject_below']}, manual review above {task['manual_review_min_exclusive']} through {task['manual_review_max_inclusive']}, reject above {task['reject_above']}. "
            f"Daily reward: pass 0-{daily['pass_max_inclusive']}, manual review above {daily['manual_review_min_exclusive']} through {daily['manual_review_max_inclusive']}, reject above {daily['reject_above']}. "
            f"Wallet/balance: pass 0-{wallet['pass_max_inclusive']}, manual review above {wallet['manual_review_min_exclusive']} through {wallet['manual_review_max_inclusive']}, reject above {wallet['reject_above']}."
        ),
    ]).strip()

BRAND_VISUAL_GUIDELINES_BY_MARKET = {
    'ID': {
        'brand_footer': 'TUGAO bottom lockup',
        'language': 'Bahasa Indonesia',
        'reward_copy_style': 'small local-currency reward module should be visually prominent and slightly larger than points labels; not salary',
    },
    'BR': {
        'brand_footer': 'Premiou bottom lockup',
        'language': 'Portuguese (Brazil)',
        'reward_copy_style': 'small R$ task/withdrawable reward should be visually prominent and slightly larger than points labels; not fixed income',
    },
    'ES_LATAM': {
        'brand_footer': 'Recompa bottom lockup',
        'language': 'Spanish',
        'reward_copy_style': 'small local reward/progress module should be visually prominent and slightly larger than points labels; not guaranteed income',
    },
    'MX': {
        'brand_footer': 'Recompa bottom lockup',
        'language': 'Spanish (Mexico)',
        'reward_copy_style': 'small MX$ task reward should be visually prominent and slightly larger than points labels; not guaranteed income',
    },
    'CO': {'brand_footer': 'Recompa bottom lockup', 'language': 'Spanish (Colombia)', 'reward_copy_style': 'small COP task reward should be visually prominent and slightly larger than points labels; not guaranteed income'},
}

COUNTRY_MARKET_ALIASES = {
    'id': 'ID',
    'idn': 'ID',
    'indonesia': 'ID',
    '印尼': 'ID',
    'br': 'BR',
    'bra': 'BR',
    'brazil': 'BR',
    'brasil': 'BR',
    '巴西': 'BR',
    'mx': 'MX',
    'mexico': 'MX',
    'méxico': 'MX',
    've': 'ES_LATAM',
    'venezuela': 'ES_LATAM',
    'co': 'CO',
    'colombia': 'CO',
    'es_latam': 'ES_LATAM',
    'recompa': 'ES_LATAM',
}

REQUIRED_PROMPT_COMPONENTS = {
    'strong_hero_visual': '强首图',
    'headline': '大标题',
    'subheadline': '副标题',
    'phone_ui': '手机 UI 承接层',
    'trust_person': '人物信任承接',
    'brand_footer': '底部品牌收口',
}

PII_PATTERNS = [
    ('email', re.compile(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', re.I)),
    ('phone_number', re.compile(r'(?:\+?\d[\s-]?){8,16}')),
    ('whatsapp_contact', re.compile(r'whats\s*app|wa\s*[:：]|\bWA\b', re.I)),
    ('bank_card', re.compile(r'\b(?:\d[ -]?){13,19}\b')),
    ('id_card', re.compile(r'\b[A-Z]{1,3}\d{6,}\b', re.I)),
]

INCOME_RISK_PATTERNS = [
    ('guaranteed_income', re.compile(r'保证收益|保底收入|稳赚|guaranteed income|guaranteed earning|renda garantida|ganancia garantizada', re.I)),
    ('fixed_income', re.compile(r'每天\s*\$?\d+|日赚\s*\$?\d+|earn\s*\$?\d+\s*(daily|per day)|ganha[rs]?\s*\$?\d+|gana[rs]?\s*\$?\d+', re.I)),
    ('cash_stimulus', re.compile(r'现金雨|现金堆|cash rain|pile of cash|dinheiro caindo|lluvia de dinero', re.I)),
]

STYLE_RISK_PATTERNS = [
    ('download_page_style', re.compile(r'下载页截图|产品介绍页|纯 UI 展示|plain ui showcase|product page layout|download page screenshot', re.I)),
    ('too_much_blank_space', re.compile(r'大留白|白边|white margin|large blank area|border frame', re.I)),
]

WRONG_SURFACE_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r'商店图',
        r'应用商店',
        r'App Store',
        r'Google Play\s+(?:store\s+)?screenshot',
        r'Google Play\s+(?:store\s+)?page',
        r'产品页',
        r'UI展示',
        r'UI 展示',
    )
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(*parts: Any, length: int = 20) -> str:
    raw = '|'.join(str(part or '') for part in parts)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:length]


def safe_provider_error(value: Any, limit: int = 180) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    text = re.sub(r'(access_token|token|api[_-]?key|key|signature|sig)=([^&\s]+)', r'\1=[REDACTED]', text, flags=re.I)
    text = re.sub(r'(Bearer\s+)[A-Za-z0-9._\-]+', r'\1[REDACTED]', text, flags=re.I)
    text = re.sub(r'https?://\S+', '[url_redacted]', text)
    return text[:limit]


def file_sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ''
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _optimize_creative_image_bytes(content: bytes, content_type: str) -> Tuple[bytes, Dict[str, Any]]:
    original = bytes(content or b'')
    meta = {
        'optimized': False,
        'original_size_bytes': len(original),
        'optimized_size_bytes': len(original),
        'content_type': str(content_type or '').strip().lower(),
    }
    if not original:
        return original, meta
    try:
        from PIL import Image
        with Image.open(io.BytesIO(original)) as image:
            image.load()
            image_format = str(image.format or '').upper()
            candidates: List[Tuple[str, bytes]] = []
            if image_format == 'PNG':
                output = io.BytesIO()
                image.save(output, format='PNG', optimize=True, compress_level=9)
                candidates.append(('png_lossless', output.getvalue()))
                palette_source = image.convert('RGBA' if image.mode in {'RGBA', 'LA', 'P'} else 'RGB')
                quantize_method = getattr(getattr(Image, 'Quantize', Image), 'MEDIANCUT', 0)
                if palette_source.mode == 'RGBA':
                    quantize_method = getattr(getattr(Image, 'Quantize', Image), 'FASTOCTREE', quantize_method)
                for colors in (256, 192):
                    output = io.BytesIO()
                    palette_source.quantize(colors=colors, method=quantize_method).save(
                        output,
                        format='PNG',
                        optimize=True,
                        compress_level=9,
                    )
                    candidates.append((f'png_palette_{colors}', output.getvalue()))
            elif image_format == 'JPEG':
                output = io.BytesIO()
                image.convert('RGB').save(output, format='JPEG', quality=88, optimize=True, progressive=True)
                candidates.append(('jpeg_optimized', output.getvalue()))
            elif image_format == 'WEBP':
                output = io.BytesIO()
                image.save(output, format='WEBP', quality=86, method=6)
                candidates.append(('webp_optimized', output.getvalue()))
            else:
                return original, meta
            optimization_method, optimized = min(candidates, key=lambda item: len(item[1])) if candidates else ('', b'')
    except Exception as exc:
        meta['error'] = safe_provider_error(exc)
        return original, meta
    if optimized and len(optimized) < len(original):
        meta.update({
            'optimized': True,
            'method': optimization_method,
            'optimized_size_bytes': len(optimized),
            'saved_bytes': len(original) - len(optimized),
            'saved_ratio': round((len(original) - len(optimized)) / max(1, len(original)), 4),
        })
        return optimized, meta
    return original, meta


def _write_creative_image_thumbnail(source_content: bytes, thumbnail_path: Path) -> Tuple[str, Dict[str, Any]]:
    meta: Dict[str, Any] = {'created': False}
    try:
        from PIL import Image
        with Image.open(io.BytesIO(source_content or b'')) as image:
            image.load()
            preview = image.convert('RGB')
            preview.thumbnail((CREATIVE_IMAGE_PREVIEW_MAX_SIZE, CREATIVE_IMAGE_PREVIEW_MAX_SIZE), Image.Resampling.LANCZOS)
            thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                preview.save(thumbnail_path, format='WEBP', quality=82, method=6)
            except Exception:
                thumbnail_path = thumbnail_path.with_suffix('.png')
                preview.save(thumbnail_path, format='PNG', optimize=True, compress_level=9)
            meta.update({
                'created': True,
                'path': str(thumbnail_path),
                'width': int(preview.size[0]),
                'height': int(preview.size[1]),
                'file_size_bytes': int(thumbnail_path.stat().st_size),
            })
            return str(thumbnail_path), meta
    except Exception as exc:
        meta['error'] = safe_provider_error(exc)
        return '', meta


def _detect_uploaded_image_content_type(content: bytes) -> str:
    data = bytes(content or b'')
    if len(data) < 12:
        raise InvalidCreativeImageError('invalid_image_file')
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            image_format = str(image.format or '').upper()
    except Exception as exc:
        raise InvalidCreativeImageError('invalid_image_file') from exc
    if image_format == 'PNG':
        return 'image/png'
    if image_format == 'JPEG':
        return 'image/jpeg'
    if image_format == 'WEBP':
        return 'image/webp'
    if data.startswith(IMAGE_BINARY_SIGNATURES['image/png']):
        if b'IHDR' not in data[:40] or b'IEND' not in data[-64:]:
            raise InvalidCreativeImageError('invalid_image_file')
        return 'image/png'
    if data.startswith(IMAGE_BINARY_SIGNATURES['image/jpeg']):
        if not data.endswith(b'\xff\xd9'):
            raise InvalidCreativeImageError('invalid_image_file')
        return 'image/jpeg'
    if data.startswith(IMAGE_BINARY_SIGNATURES['image/webp']) and data[8:12] == b'WEBP':
        return 'image/webp'
    raise InvalidCreativeImageError('invalid_image_file')


def _validate_chatgpt_pro_uploaded_image_quality(content: bytes, content_type: str) -> Tuple[int, int]:
    data = bytes(content or b'')
    if str(content_type or '').strip().lower() not in {'image/png', 'image/jpeg', 'image/jpg', 'image/webp'}:
        return DEFAULT_FEED_IMAGE_SIZE
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            mode = str(image.mode or '').upper()
            sample = image.convert('RGB').resize((64, 64))
            colors = sample.getcolors(maxcolors=4096)
    except Exception as exc:
        raise InvalidCreativeImageError('invalid_image_file') from exc
    if (width, height) not in CHATGPT_PRO_ACCEPTED_IMAGE_SIZES:
        raise InvalidCreativeImageError('invalid_image_dimensions')
    min_bytes = 30_000 if (width, height) == (1024, 1024) else 12_000
    if len(data) < min_bytes:
        raise InvalidCreativeImageError('low_quality_image_file')
    if mode in {'1', 'P'}:
        raise InvalidCreativeImageError('low_quality_image_file')
    if colors is not None and len(colors) < 64:
        raise InvalidCreativeImageError('low_quality_image_file')
    return width, height


def normalize_experiment_mode(value: Any) -> str:
    mode = str(value or EXPERIMENT_MODE_REPLACEMENT).strip().lower().replace('-', '_')
    if mode in {'new', 'test', 'new_ad', 'new_test', '新增实验', '新增测试'}:
        return EXPERIMENT_MODE_NEW_TEST
    return EXPERIMENT_MODE_REPLACEMENT


def binding_confidence_for_method(method: str, *, manual_confirmed: bool = False) -> str:
    if manual_confirmed:
        return BINDING_CONFIDENCE_MANUAL_CONFIRMED
    return CONFIDENCE_BY_BINDING_METHOD.get(str(method or ''), BINDING_CONFIDENCE_LOW)


def generate_experiment_code(*, country: str = '', now: Optional[datetime] = None, sequence: int = 1) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime('%Y%m%d')
    market, _ = normalize_market(country, '')
    prefix = market or re.sub(r'[^A-Za-z0-9]+', '', str(country or '').upper())[:6] or 'GEN'
    return f'EXP-{prefix}-{stamp}-{max(1, int(sequence or 1)):03d}'


def build_binding_instruction(experiment_mode: str, experiment_code: str) -> str:
    mode = normalize_experiment_mode(experiment_mode)
    if mode == EXPERIMENT_MODE_NEW_TEST:
        return f'新增实验广告请在广告名称中加入实验编号 {experiment_code}，系统优先用广告名自动绑定，其次用图片哈希，最后人工绑定。'
    return '替换优化素材不强制上传回系统；系统通过 Meta Creative Sync / Meta Activity / 对象快照识别原广告 creative 变化。'


def normalize_market(country: Any = '', project: Any = '') -> Tuple[str, Dict[str, Any]]:
    tokens = [str(country or '').strip(), str(project or '').strip()]
    for token in tokens:
        lowered = token.lower()
        for alias, market in COUNTRY_MARKET_ALIASES.items():
            if alias and alias in lowered:
                return market, SUPPORTED_BRAND_MARKETS[market]
    return '', {}


def _normalize_target_app(value: Any) -> str:
    raw = str(value or '').strip().lower()
    if raw in {'linky', 'linkie'}:
        return 'linky'
    if raw == 'timo':
        return 'timo'
    return 'all' if raw in {'', 'all', '全部'} else raw


def _creative_target_app_from_fields(*values: Any) -> str:
    text = ' '.join(str(value or '') for value in values).lower()
    digits = set(re.findall(r'\b\d{12,20}\b', text))
    if '1293506106236750' in digits:
        return 'timo'
    if digits & {'1898261564216326', '1511281443796277', '1022472447112808', '2014618999169375', '865675816544216'}:
        return 'linky'
    if re.search(r'(^|[\s_-])tm($|[\s_-])', text):
        return 'timo'
    if re.search(r'(^|[\s_-])lk($|[\s_-])', text):
        return 'linky'
    if 'tugao' in text or 'indonesia' in text:
        return 'linky'
    return 'inactive'


def creative_job_target_app(job: Dict[str, Any]) -> str:
    material_refs = job.get('material_refs') if isinstance(job.get('material_refs'), dict) else {}
    rules = job.get('rules') if isinstance(job.get('rules'), dict) else {}
    account_target_app = _creative_target_app_from_fields(
        material_refs.get('account_id'),
        material_refs.get('account_name'),
        material_refs.get('app_id'),
    )
    if account_target_app in {'linky', 'timo'}:
        return account_target_app
    explicit_target_app = _normalize_target_app(
        material_refs.get('target_app')
        or material_refs.get('app_target')
        or rules.get('target_app')
        or rules.get('app_target')
    )
    if explicit_target_app in {'linky', 'timo'}:
        return explicit_target_app
    return 'inactive'


def creative_image_target_app(image: Dict[str, Any]) -> str:
    metadata = image.get('metadata') if isinstance(image.get('metadata'), dict) else {}
    explicit_target_app = _normalize_target_app(metadata.get('target_app') or metadata.get('app_target'))
    if explicit_target_app in {'linky', 'timo'}:
        return explicit_target_app
    return _creative_target_app_from_fields(
        image.get('market'),
        image.get('brand'),
        image.get('request_id'),
        image.get('creative_direction'),
        metadata.get('project'),
        metadata.get('brand_display_name'),
        metadata.get('source_project'),
    )


@dataclass(frozen=True)
class CreativeImageGenerationBrief:
    country: str = ''
    project: str = ''
    campaign: str = ''
    ad_group: str = ''
    ad: str = ''
    objective: str = '真实入会'
    audience: str = '广泛受众'
    core_offer: str = '网赚效率'
    source_performance: Dict[str, Any] = field(default_factory=dict)
    source_preview_url: str = ''
    source_preview_asset_id: str = ''
    source_preview_title: str = ''
    source_diagnosis: str = ''
    revision_goal: str = ''
    requested_by: str = ''


@dataclass(frozen=True)
class CreativeImagePrompt:
    surface: str
    width: int
    height: int
    market: str
    brand: str
    prompt: str
    negative_prompt: str
    required_components: List[str]
    compliance_notes: List[str]
    prompt_hash: str
    review_status: str
    risk_status: str
    risk_tags: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class GeneratedCreativeImage:
    image_id: str
    request_id: str
    surface: str
    image_size: str
    market: str
    brand: str
    image_ref: str
    thumbnail_ref: str
    prompt_hash: str
    risk_status: str
    risk_tags: List[str]
    review_status: str
    provider: str
    created_at: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalImageProviderConfig:
    provider: str = PROVIDER_FIXTURE
    enabled: bool = False
    url: str = ''
    api_key: str = ''
    session: Optional[Any] = None
    timeout_seconds: int = 90


CREATIVE_DIRECTION_PROFILES = [
    {
        'key': CREATIVE_DIRECTION_SAFE_COMPLIANCE,
        'keys': ['safe_compliance', '安全合规', '合规安全', '低风险品牌', 'brand safety', 'policy safe'],
        'name': 'trusted task-to-points-to-in-app-rewards story with product-first proof and no cash or employment claims',
        'primary_visual': 'make one unmistakable smartphone dashboard the hero and show a single readable path from named in-app activities to visible progress, in-app points, and generic available rewards; two or three icon-led feature cards may add useful product information, while a real adult woman is supporting context only',
        'composition_system': 'bright product-led asymmetrical layout: smartphone and product proof occupy 48-58% of the canvas, the adult woman occupies 20-28%, compact copy and feature cards use the remaining space, and a slim light brand area reserves its rightmost 28-32% for the verified compact light brand card overlay',
        'reward_hierarchy': 'daily activities and progress establish the product, a readable points state connects the flow, and the generic reward destination closes it without cash, currency, or guaranteed value',
        'distinctness_guard': 'do not fall back to a sparse stock-photo-plus-phone template, progress dashboard, numbered tutorial, chat panel, empty phone, awkward presentation pose, repeated copy, or dead footer; do not add CTA buttons, in-app buttons, ribbons, pills, navigation, tiny microcopy, merchants, named prizes, monetary values, fake testimonials, fake notifications, or third-party brand marks',
        'headline_role': 'use one concise market-language headline and one supporting sentence; wording may vary while preserving app activities, visible progress, in-app points, and available rewards',
        'proof': 'show exactly three connected proof modules: named activities with internally consistent progress, an in-app points state, and a clear generic reward destination; UI styling and copy may vary by version but the verified function path may not change',
        'cta': 'no CTA or button anywhere; the visual task-to-points-to-rewards path is the complete explanation',
        'required_visible_elements': ['adult_woman_supporting_natural_pose', 'smartphone_app_hero', 'product_faithful_conceptual_app_ui', 'named_activity_rows', 'visible_progress_summary', 'in_app_points_state', 'generic_unbranded_reward_destination', 'readable_headline', 'supporting_copy', 'commercial_material_finish', 'layered_depth_and_overlap', 'purposeful_decorative_treatment', 'no_raw_geometric_placeholders', 'no_fake_cta', 'light_brand_footer_reserve', 'official_logo_and_brand_wordmark'],
    },
    {
        'key': CREATIVE_DIRECTION_POINTS_REWARD,
        'keys': ['points_reward', '任务奖励', '积分', '收入', '收益', '奖励', 'reward', 'recompensa', 'saldo', '网赚'],
        'name': 'points and task reward',
        'primary_visual': 'make a reward dashboard with local-currency task rewards, points balance, completed-task rows, and reward progress the dominant information area; keep the adult woman as the main human subject supporting that dashboard',
        'composition_system': 'reward-dashboard-led asymmetrical split layout: the reward/progress dashboard occupies 45-55% of the canvas, with an adult woman on the opposite side and compact task cards connecting the two areas',
        'reward_hierarchy': 'cash/points rewards are the primary proof for this direction; show one credible local-currency task reward prominently, supported by points balance and progress, without implying salary or guaranteed payout',
        'distinctness_guard': 'do not use an advisor conversation panel, alternating chat bubbles, a support-agent scene, or a numbered 1-2-3 tutorial flow',
        'headline_role': 'lead with app tasks plus visible cash/points rewards; never imply fixed salary or guaranteed payout',
        'proof': 'show a readable local-currency reward card as the strongest proof, larger than points chips, plus points balance, reward progress, and in-app task completion context',
        'cta': 'invite the user to start a simple in-app task',
        'required_visible_elements': ['reward_dashboard', 'completed_task_rows', 'prominent_local_currency_task_reward', 'points_balance', 'reward_progress', 'adult_woman', 'brand_footer', 'cta_button'],
    },
    {
        'key': CREATIVE_DIRECTION_EASY_START,
        'keys': ['easy_start', '简单开始', '效率', '快速', '省时', 'easy', 'fast', '流程', '透明', '申请', '注册', 'cadastro', 'processo'],
        'name': 'easy mobile start',
        'primary_visual': 'make a large numbered 1-2-3 process path with three visually separate steps, arrows, checkmarks, and a clear first action dominate the image; use the phone screen only as compact supporting context',
        'composition_system': 'process-infographic-led layout: three large numbered panels linked by arrows occupy 55-65% of the canvas; step 1 opens the app, step 2 completes one simple task, and step 3 confirms completion, while an adult woman gestures toward the path',
        'reward_hierarchy': 'reward is only a small secondary success confirmation inside step 3; do not show a large cash card, wallet balance, or reward dashboard',
        'distinctness_guard': 'do not use advisor chat bubbles, a one-to-one support conversation, a dominant reward dashboard, or a large cash/points module',
        'headline_role': 'lead with simple start, mobile completion, and low-friction guidance',
        'proof': 'show three short step labels, directional arrows, checklist cues, and a prominent start button; completion itself is the proof, with any reward shown only as a small final-state detail',
        'cta': 'invite the user to complete the first app step',
        'required_visible_elements': ['three_numbered_step_panels', 'directional_arrows', 'checkmarks', 'compact_phone_ui', 'adult_woman_gesture', 'start_button', 'brand_footer'],
    },
    {
        'key': CREATIVE_DIRECTION_GUIDED_TRUST,
        'keys': ['guided_trust', '指导可信', '私聊', '顾问', '顧問', '信任', '证明', '安全', '安心', 'trust', 'segurança', 'confiável', 'support', 'grupo', 'acompanha'],
        'name': 'one-to-one advisor guidance',
        'primary_visual': 'make a clearly readable one-to-one in-app advisor conversation the dominant scene, with a female advisor avatar, a user question bubble, several helpful reply bubbles, and one highlighted answer card',
        'composition_system': 'conversation-led split-screen layout: a one-to-one advisor chat panel occupies 45-55% of the canvas and a warm adult female advisor portrait anchors the other side; use alternating message bubbles and a clear app handoff',
        'reward_hierarchy': 'cash or points may appear only as a small secondary explanation inside one advisor reply or help card; do not show a wallet, large reward card, or reward dashboard',
        'distinctness_guard': 'do not use a numbered 1-2-3 process, a task-progress dashboard, a large cash/points module, or generic support badges without a visible one-to-one conversation',
        'headline_role': 'lead with private one-to-one app guidance, a concrete beginner question, and a reassuring advisor answer; this is product support, never a social-chat or chat-to-earn job',
        'proof': 'show a visible user question, two or three advisor reply bubbles, a verified-advisor cue, and a highlighted next-action card inside the app conversation',
        'cta': 'invite the user to ask the advisor how to take the first app step',
        'required_visible_elements': ['one_to_one_advisor_chat_panel', 'female_advisor_avatar', 'user_question_bubble', 'advisor_reply_bubbles', 'verified_advisor_cue', 'next_action_card', 'brand_footer'],
    },
]

CREATIVE_DIRECTION_DOWNLOAD_LABELS = {
    CREATIVE_DIRECTION_SAFE_COMPLIANCE: '安全合规',
    CREATIVE_DIRECTION_POINTS_REWARD: '网赚效率',
    CREATIVE_DIRECTION_EASY_START: '流程透明',
    CREATIVE_DIRECTION_GUIDED_TRUST: '私聊顾问',
    'points and task reward': '网赚效率',
    'easy mobile start': '流程透明',
    'guided and trustworthy app support': '私聊顾问',
    'safe product discovery and brand trust': '安全合规',
}

CREATIVE_DOWNLOAD_COUNTRY_LABELS = {
    'br': 'Brazil',
    'brazil': 'Brazil',
    'id': 'Indonesia',
    'indonesia': 'Indonesia',
    'mx': 'Mexico',
    'mexico': 'Mexico',
    'co': 'Colombia',
    'colombia': 'Colombia',
}


def creative_image_authoritative_name(image: Dict[str, Any]) -> str:
    metadata = image.get('metadata') if isinstance(image.get('metadata'), dict) else {}
    image_meta_names = image.get('meta_names') if isinstance(image.get('meta_names'), dict) else {}
    metadata_meta_names = metadata.get('meta_names') if isinstance(metadata.get('meta_names'), dict) else {}
    return str(
        image.get('creative_name')
        or image_meta_names.get('ad')
        or metadata_meta_names.get('ad')
        or ''
    ).strip()


def enrich_creative_image_names(conn: sqlite3.Connection, images: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    job_ids = {
        str((image.get('metadata') or {}).get('job_id') or '').strip()
        for image in images
        if isinstance(image.get('metadata'), dict)
    }
    job_ids.discard('')
    names_by_job: Dict[str, str] = {}
    if job_ids:
        placeholders = ','.join('?' for _ in job_ids)
        rows = conn.execute(
            f"SELECT job_id, material_refs_json FROM creative_pro_work_queue WHERE job_id IN ({placeholders})",
            tuple(sorted(job_ids)),
        ).fetchall()
        for row in rows:
            material_refs = _json_load(row['material_refs_json'], {})
            meta_names = material_refs.get('meta_names') if isinstance(material_refs.get('meta_names'), dict) else {}
            name = str(meta_names.get('ad') or '').strip()
            if name:
                names_by_job[str(row['job_id'])] = name
    for image in images:
        metadata = image.get('metadata') if isinstance(image.get('metadata'), dict) else {}
        job_id = str(metadata.get('job_id') or '').strip()
        image['creative_name'] = names_by_job.get(job_id) or creative_image_authoritative_name(image)
    return images


def creative_image_download_filename(image: Dict[str, Any], suffix: str = '.png') -> str:
    metadata = image.get('metadata') if isinstance(image.get('metadata'), dict) else {}
    raw_country = str(image.get('market') or metadata.get('country') or '').strip()
    country = CREATIVE_DOWNLOAD_COUNTRY_LABELS.get(raw_country.lower(), raw_country or 'Unknown')
    brand = str(image.get('brand') or metadata.get('brand') or 'Brand').strip()
    raw_direction = str(image.get('creative_direction') or metadata.get('creative_direction') or metadata.get('creative_angle') or '').strip()
    direction = CREATIVE_DIRECTION_DOWNLOAD_LABELS.get(raw_direction.lower(), raw_direction or '素材')
    created_at = str(image.get('created_at') or metadata.get('created_at') or '').strip()
    created_digits = re.sub(r'\D+', '', created_at)
    generated_at = f'{created_digits[:8]}-{created_digits[8:14]}' if len(created_digits) >= 14 else 'unknown-time'
    raw_unique_id = str(image.get('image_id') or metadata.get('image_id') or image.get('prompt_hash') or '').strip()
    compact_id = re.sub(r'[^A-Za-z0-9]+', '', raw_unique_id)[-8:]
    if not compact_id:
        compact_id = stable_id(country, brand, direction, created_at, length=8)

    def safe_part(value: Any) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '-', str(value or '').strip())
        return re.sub(r'[\s-]+', '-', cleaned).strip('-') or 'Unknown'

    normalized_suffix = str(suffix or '.png').lower()
    if not normalized_suffix.startswith('.') or not re.fullmatch(r'\.[a-z0-9]{2,5}', normalized_suffix):
        normalized_suffix = '.png'
    creative_name = creative_image_authoritative_name(image)
    if creative_name:
        return f'{safe_part(creative_name)}{normalized_suffix}'
    return f'{safe_part(country)}-{safe_part(brand)}-{safe_part(direction)}-{generated_at}-{compact_id}{normalized_suffix}'


def creative_direction_profile(core_offer: Any) -> Dict[str, str]:
    raw = str(core_offer or '').strip()
    lowered = raw.lower()
    for profile in CREATIVE_DIRECTION_PROFILES:
        if any(str(key).lower() in lowered for key in profile['keys']):
            return profile
    return {
        'key': CREATIVE_DIRECTION_POINTS_REWARD,
        'name': raw or 'balanced funnel repair',
        'primary_visual': 'balance mobile task cards, points or reward progress, phone UI, app guidance, and brand footer',
        'composition_system': 'balanced app-task composition with one clear primary information area',
        'reward_hierarchy': 'keep rewards contextual and subordinate unless the selected direction explicitly centers rewards',
        'distinctness_guard': 'avoid blending multiple competing composition systems in the same image',
        'headline_role': 'lead with the selected app reward angle while keeping the message concrete, local, and compliant',
        'proof': 'use task cards, reward progress, and in-app guidance as proof',
        'cta': 'invite the user to start the first app task',
        'required_visible_elements': ['task_cards', 'reward_progress', 'phone_ui', 'brand_footer'],
    }


def creative_direction_key(core_offer: Any) -> str:
    return str(creative_direction_profile(core_offer).get('key') or CREATIVE_DIRECTION_POINTS_REWARD)


def _market_negative_constraint_key(market: str) -> str:
    if market == 'BR':
        return 'PT'
    if market == 'ID':
        return 'ID'
    if market in {'ES_LATAM', 'MX', 'CO'}:
        return 'ES'
    return 'EN'


def localized_negative_constraints(market: str) -> List[str]:
    language_key = _market_negative_constraint_key(market)
    return [
        LOCALIZED_NEGATIVE_CONSTRAINTS['EN'],
        LOCALIZED_NEGATIVE_CONSTRAINTS.get(language_key, LOCALIZED_NEGATIVE_CONSTRAINTS['EN']),
    ]


def headline_candidates_for(market: str, direction_key: str) -> List[str]:
    if direction_key == CREATIVE_DIRECTION_SAFE_COMPLIANCE:
        return list(SAFE_COMPLIANCE_HEADLINES.get(market) or ['Discover how the app works'])
    candidates = {
        ('BR', CREATIVE_DIRECTION_POINTS_REWARD): ['Ganhe pontos pelo celular', 'Tarefas simples, recompensas no app', 'Complete tarefas e acompanhe seus pontos'],
        ('BR', CREATIVE_DIRECTION_EASY_START): ['Comece pelo celular', 'Três passos para começar', 'Comece com tarefas simples'],
        ('BR', CREATIVE_DIRECTION_GUIDED_TRUST): ['Comece com orientação', 'Entenda as tarefas no app', 'Ajuda para dar o primeiro passo'],
        ('ID', CREATIVE_DIRECTION_POINTS_REWARD): ['Kumpulkan poin dari HP', 'Tugas mudah, reward di app', 'Selesaikan tugas dan lihat progres'],
        ('ID', CREATIVE_DIRECTION_EASY_START): ['Mulai dari HP', 'Tiga langkah untuk mulai', 'Mulai dengan tugas sederhana'],
        ('ID', CREATIVE_DIRECTION_GUIDED_TRUST): ['Mulai dengan panduan', 'Pahami tugas di app', 'Bantuan untuk langkah pertama'],
        ('ES_LATAM', CREATIVE_DIRECTION_POINTS_REWARD): ['Gana puntos desde tu celular', 'Tareas simples y recompensas', 'Completa tareas y mira tu progreso'],
        ('ES_LATAM', CREATIVE_DIRECTION_EASY_START): ['Empieza desde tu celular', 'Tres pasos para empezar', 'Comienza con tareas simples'],
        ('ES_LATAM', CREATIVE_DIRECTION_GUIDED_TRUST): ['Empieza con guía', 'Entiende las tareas en la app', 'Ayuda para el primer paso'],
        ('MX', CREATIVE_DIRECTION_POINTS_REWARD): ['Gana recompensas desde tu celular', 'Tareas simples y recompensas', 'Completa tareas y mira tu progreso'],
        ('MX', CREATIVE_DIRECTION_EASY_START): ['Empieza desde tu celular', 'Tres pasos para empezar', 'Comienza con tareas simples'],
        ('MX', CREATIVE_DIRECTION_GUIDED_TRUST): ['Empieza con guía', 'Entiende las tareas en la app', 'Ayuda para el primer paso'],
        ('CO', CREATIVE_DIRECTION_POINTS_REWARD): ['Gana recompensas desde tu celular', 'Tareas simples y recompensas', 'Completa tareas y mira tu progreso'],
        ('CO', CREATIVE_DIRECTION_EASY_START): ['Empieza desde tu celular', 'Tres pasos para empezar', 'Comienza con tareas simples'],
        ('CO', CREATIVE_DIRECTION_GUIDED_TRUST): ['Empieza con guía', 'Entiende las tareas en la app', 'Ayuda para el primer paso'],
    }
    market_profile = SUPPORTED_BRAND_MARKETS.get(market, {})
    return candidates.get((market, direction_key), [str(market_profile.get('headline') or 'Start with app tasks')])


def public_ad_positioning_for(direction_key: str) -> List[str]:
    if direction_key == CREATIVE_DIRECTION_SAFE_COMPLIANCE:
        return list(SAFE_COMPLIANCE_POSITIONING['visible_positioning'])
    return list(PUBLIC_AD_POSITIONING['visible_positioning'])


def brand_visual_guidelines_for(market: str, direction_key: str) -> Dict[str, Any]:
    if direction_key == CREATIVE_DIRECTION_SAFE_COMPLIANCE:
        return {
            'language': str(BRAND_VISUAL_GUIDELINES_BY_MARKET.get(market, {}).get('language') or ''),
            **POSITIVE_DAYLIGHT_ART_DIRECTION,
            'official_logo_rule': 'the image model must keep a slim light or high-key brand area and leave its rightmost 28-32% clean; it must not render any logo or wordmark because the system will programmatically composite a compact verified light brand card there',
            'financial_symbol_rule': 'the diamond is a brand mark only and must never be treated as money, a coin, a reward token, a prize, treasure, or wealth',
        }
    return {
        **dict(BRAND_VISUAL_GUIDELINES_BY_MARKET.get(market, {})),
        **POSITIVE_DAYLIGHT_ART_DIRECTION,
    }


def brand_assets_for(direction_key: str) -> List[Dict[str, Any]]:
    if direction_key != CREATIVE_DIRECTION_SAFE_COMPLIANCE:
        return []
    return [{
        'asset_id': 'official_app_logo',
        'role': 'official_logo_small_secondary',
        'source_url': _creative_source_image_url(OFFICIAL_APP_LOGO_PATH),
        'sha256': OFFICIAL_APP_LOGO_SHA256,
        'required': True,
        'usage': 'System-only post-generation overlay source. The image model must not render, redraw, reinterpret, duplicate, animate, or write a logo or wordmark.',
        'max_area_ratio': 0.12,
    }]


def source_image_fields_from_reference(reference: Dict[str, Any]) -> Dict[str, Any]:
    source_url = str(
        reference.get('source_image_signed_url')
        or reference.get('source_image_url')
        or reference.get('image_url')
        or ''
    ).strip()
    preview_url = str(reference.get('source_preview_url') or reference.get('url') or '').strip()
    source_hash = str(reference.get('source_image_hash') or reference.get('image_hash') or '').strip()
    source_id = str(reference.get('source_image_id') or reference.get('asset_id') or '').strip()
    source_width = int(reference.get('source_image_width') or reference.get('image_width') or 0)
    source_height = int(reference.get('source_image_height') or reference.get('image_height') or 0)
    source_quality = str(reference.get('source_image_quality') or '').strip()
    has_true_source_image = bool(source_url and (source_hash or reference.get('source_image_id') or reference.get('source_image_file')))
    generation_mode = GENERATION_MODE_OLD_IMAGE_REFERENCE_REVISION if has_true_source_image else (
        GENERATION_MODE_DIRECTION_REDRAW if preview_url else GENERATION_MODE_NEW_DIRECTION
    )
    return {
        'generation_mode': generation_mode,
        'source_image_id': source_id,
        'source_image_signed_url': source_url,
        'source_image_hash': source_hash,
        'source_preview_url': preview_url,
        'source_preview_asset_id': source_id,
        'source_preview_title': str(reference.get('title') or '').strip(),
        'source_image_width': source_width,
        'source_image_height': source_height,
        'source_image_quality': source_quality,
        'source_image_required': 1 if generation_mode == GENERATION_MODE_OLD_IMAGE_REFERENCE_REVISION else 0,
    }


def validate_true_source_image_reference(reference: Dict[str, Any]) -> Tuple[bool, List[str]]:
    source_url = str(
        reference.get('source_image_signed_url')
        or reference.get('source_image_url')
        or ''
    ).strip()
    source_hash = str(reference.get('source_image_hash') or reference.get('image_hash') or '').strip()
    source_id = str(reference.get('source_image_id') or reference.get('asset_id') or '').strip()
    resolution_status = str(reference.get('source_image_resolution_status') or '').strip().lower()
    reasons: List[str] = []
    parsed = urlparse(source_url)
    if not source_id:
        reasons.append('source_image_id_missing')
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        reasons.append('source_image_url_not_absolute')
    if parsed.path.rstrip('/').lower().endswith('/preview'):
        reasons.append('source_image_preview_not_allowed')
    route_match = (
        re.fullmatch(r'/api/ops/ad-data-dashboard/creative-assets/([^/]+)/source/?', parsed.path)
        or re.fullmatch(r'/api/ops/ad-data-dashboard/creative-images/([^/]+)/?', parsed.path)
    )
    if not route_match:
        reasons.append('source_image_route_not_allowed')
    elif source_id and unquote(route_match.group(1)) != source_id:
        reasons.append('source_image_route_id_mismatch')
    trusted_origin = urlparse(_creative_source_image_url('/'))
    if (
        parsed.scheme.lower() != trusted_origin.scheme.lower()
        or parsed.netloc.lower() != trusted_origin.netloc.lower()
    ):
        reasons.append('source_image_origin_not_allowed')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', source_hash):
        reasons.append('source_image_sha256_required')
    if resolution_status in {
        'missing_source_identity',
        'source_image_not_synced',
        'source_image_not_localized',
        'source_identity_ambiguous',
    }:
        reasons.append(resolution_status)
    return not reasons, sorted(set(reasons))


def source_image_structure_from_reference(reference: Dict[str, Any]) -> Dict[str, Any]:
    width = int(reference.get('source_image_width') or reference.get('image_width') or 0)
    height = int(reference.get('source_image_height') or reference.get('image_height') or 0)
    return {
        'version': 'old_image_structure_v1',
        'source_image_id': str(reference.get('source_image_id') or reference.get('asset_id') or '').strip(),
        'width': width,
        'height': height,
        'locked_regions': [
            'overall composition',
            'main person or human scene position',
            'phone/device position and scale',
            'brand/footer area',
            'background scene relationship',
        ],
        'editable_regions': [
            'headline emphasis',
            'reward or withdrawable benefit module',
            'task/proof card hierarchy',
            'CTA clarity',
            'minor trust/support cue text',
        ],
        'preservation_target': 'keep original layout/person/phone/background/brand; make local hierarchy edits only',
    }


def build_old_image_revision_plan(
    *,
    source_diagnosis: str,
    revision_goal: str,
    creative_direction: Dict[str, Any],
    task: Dict[str, Any],
) -> Dict[str, Any]:
    diagnosis_type = str(task.get('diagnosis_type') or '').strip()
    action_type = str(task.get('action_type') or task.get('primary_action') or '').strip()
    manual_reference_override = bool(task.get('manual_reference_override'))
    weak_stage = diagnosis_type or 'creative_expression'
    if manual_reference_override and (
        weak_stage in OLD_IMAGE_REVISION_BLOCKED_DIAGNOSIS_TYPES
        or action_type in OLD_IMAGE_REVISION_BLOCKED_ACTION_TYPES
    ):
        weak_stage = 'manual_control_group'
    return {
        'version': 'old_image_revision_plan_v1',
        'weak_stage': weak_stage,
        'goal': revision_goal or (
            '人工参考旧图做低强度对照组，保留原图主体，只优化局部收益/任务表达'
            if manual_reference_override
            else '在原图基础上补强弱势漏斗环节'
        ),
        'diagnosis': source_diagnosis,
        'action_type': action_type,
        'edit_intensity': 'low' if manual_reference_override else 'medium',
        'allowed_edits': [
            '局部增强标题层级',
            '强化任务/奖励/可提现收益模块的视觉优先级',
            '微调证明卡片和 CTA 的可读性',
            '增加或优化小范围信任提示',
        ],
        'forbidden_edits': [
            '不要更换主人物、主体场景、手机位置或品牌区域',
            '不要重画成一张不同构图的新广告',
            '不要新增夸张现金、提现截图、保底收益、联系方式或招聘表达',
            '不要改变原图主要国家语言和产品类别',
        ],
        'preserve': [
            'original composition',
            'main person or scene relationship',
            'phone/device position',
            'brand identity and footer',
            'target market language',
            'product/app category',
        ],
        'modify': [
            'headline hierarchy',
            'withdrawable reward visibility',
            'task/proof module hierarchy',
            'CTA clarity',
            'trust/support cue',
        ],
        'expected_metric_impact': str(creative_direction.get('headline_role') or 'improve front funnel clarity and qualified intent'),
        'compliance_notes': [
            '收益金额保持小额、上下文化，不承诺固定收入',
            '外显只表达手机任务 App、积分、小额奖励、引导和品牌',
        ],
    }


def validate_old_image_revision_plan(plan: Dict[str, Any]) -> Tuple[bool, List[str]]:
    missing = [
        key for key in [
            'weak_stage',
            'goal',
            'edit_intensity',
            'allowed_edits',
            'forbidden_edits',
            'preserve',
            'modify',
            'expected_metric_impact',
            'compliance_notes',
        ]
        if not plan.get(key)
    ]
    if str(plan.get('edit_intensity') or '') not in {'low', 'medium'}:
        missing.append('edit_intensity_invalid')
    if str(plan.get('weak_stage') or '') in OLD_IMAGE_REVISION_BLOCKED_DIAGNOSIS_TYPES:
        missing.append('weak_stage_not_allowed')
    return not missing, missing


def old_image_revision_gate(task: Dict[str, Any]) -> Tuple[bool, List[str]]:
    if bool(task.get('manual_reference_override')):
        return True, []
    reasons: List[str] = []
    gate = task.get('action_gate') if isinstance(task.get('action_gate'), dict) else {}
    if gate:
        if gate.get('allow_generate_creative') is False:
            reasons.extend(str(item) for item in (gate.get('blocked_reasons') or ['action_gate_blocked']) if item)
    if task.get('allow_generate_creative') is False:
        reasons.append('allow_generate_creative_false')
    diagnosis_type = str(task.get('diagnosis_type') or '').strip()
    action_type = str(task.get('action_type') or task.get('primary_action') or '').strip()
    if diagnosis_type in OLD_IMAGE_REVISION_BLOCKED_DIAGNOSIS_TYPES:
        reasons.append(diagnosis_type)
    if action_type in OLD_IMAGE_REVISION_BLOCKED_ACTION_TYPES:
        reasons.append(action_type)
    return not reasons, sorted(set(reasons))


def _creative_db_data_dir(conn: sqlite3.Connection) -> Path:
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
        for row in rows:
            path = str(row[2] if len(row) > 2 else '').strip()
            if path and path != ':memory:':
                return Path(path).expanduser().resolve().parent
    except Exception:
        pass
    return Path(__file__).resolve().parents[1] / 'data'


def _creative_asset_preview_route(asset_id: str) -> str:
    return f"/api/ops/ad-data-dashboard/creative-assets/{str(asset_id or '').strip()}/preview"


def _creative_asset_source_route(asset_id: str) -> str:
    return f"/api/ops/ad-data-dashboard/creative-assets/{str(asset_id or '').strip()}/source"


def _creative_source_image_url(path_or_url: str) -> str:
    raw = str(path_or_url or '').strip()
    if not raw:
        return ''
    if raw.startswith('http://') or raw.startswith('https://'):
        return raw
    if not raw.startswith('/'):
        return raw
    base = (
        os.getenv('OPS_PUBLIC_BASE_URL')
        or os.getenv('MCN_OPS_BASE_URL')
        or os.getenv('PRODUCTION_OPS_API_BASE_URL')
        or 'http://127.0.0.1:8011'
    )
    return f"{str(base).strip().rstrip('/')}{raw}"


def _creative_preview_file_for_asset(conn: sqlite3.Connection, asset_id: str) -> Optional[Path]:
    safe_id = re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(asset_id or '').strip())[:120]
    if not safe_id:
        return None
    cache_dir = _creative_db_data_dir(conn) / 'ad_creative_previews'
    for path in cache_dir.glob(f'{safe_id}.*'):
        if path.is_file() and not path.name.endswith('.tmp'):
            return path
    return None


def _creative_source_file_for_asset(conn: sqlite3.Connection, asset_id: str) -> Optional[Path]:
    safe_id = re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(asset_id or '').strip())[:120]
    if not safe_id:
        return None
    cache_dir = _creative_db_data_dir(conn) / 'ad_creative_sources'
    for path in cache_dir.glob(f'{safe_id}.*'):
        if path.is_file() and not path.name.endswith('.tmp'):
            return path
    return None


def cleanup_temporary_creative_source_images(conn: sqlite3.Connection, *, job_id: str = '', task_id: str = '') -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    source_ids: Set[str] = set()
    if str(task_id or '').strip():
        rows = conn.execute(
            "SELECT source_image_id FROM creative_generation_tasks WHERE task_id = ?",
            (str(task_id or '').strip(),),
        ).fetchall()
        source_ids.update(str(row['source_image_id'] or '').strip() for row in rows)
    if str(job_id or '').strip():
        task_rows = conn.execute(
            "SELECT source_image_id FROM creative_generation_tasks WHERE job_id = ?",
            (str(job_id or '').strip(),),
        ).fetchall()
        source_ids.update(str(row['source_image_id'] or '').strip() for row in task_rows)
        job_row = conn.execute(
            "SELECT material_refs_json, source_asset_ids_json FROM creative_pro_work_queue WHERE job_id = ?",
            (str(job_id or '').strip(),),
        ).fetchone()
        if job_row:
            material_refs = _json_load(job_row['material_refs_json'], {})
            if isinstance(material_refs, dict):
                source_ids.add(str(material_refs.get('source_image_id') or material_refs.get('source_preview_asset_id') or '').strip())
            for asset_id in _json_load(job_row['source_asset_ids_json'], []):
                source_ids.add(str(asset_id or '').strip())
    source_ids = {source_id for source_id in source_ids if source_id}
    deleted: List[str] = []
    missing: List[str] = []
    has_asset_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ad_creative_asset'"
    ).fetchone() is not None
    for source_id in sorted(source_ids):
        source_file = _creative_source_file_for_asset(conn, source_id)
        if source_file and source_file.is_file():
            try:
                source_file.unlink()
                deleted.append(source_id)
            except Exception:
                missing.append(source_id)
                continue
        else:
            missing.append(source_id)
        if has_asset_table:
            conn.execute(
                """
                UPDATE ad_creative_asset
                SET source_image_local_ref = '',
                    updated_at = ?
                WHERE asset_id = ?
                """,
                (utc_now(), source_id),
            )
    return {
        'ok': True,
        'source_image_ids': sorted(source_ids),
        'deleted_source_image_ids': deleted,
        'missing_source_image_ids': missing,
        'deleted_count': len(deleted),
    }


def _creative_source_filename(asset_id: str, content_type: str) -> str:
    safe_id = re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(asset_id or '').strip())[:120] or f"aci_source_{stable_id(content_type, utc_now())}"
    ext = {
        'image/jpeg': '.jpg',
        'image/jpg': '.jpg',
        'image/png': '.png',
        'image/webp': '.webp',
        'image/gif': '.gif',
        'image/svg+xml': '.svg',
    }.get(str(content_type or '').split(';', 1)[0].strip().lower(), '.img')
    return f'{safe_id}{ext}'


def _creative_remote_source_allowed(raw_url: str) -> bool:
    parsed = urlparse(str(raw_url or '').strip())
    host = str(parsed.hostname or '').strip().lower()
    allowed_suffixes = ('facebook.com', 'fbcdn.net', 'fbsbx.com')
    return parsed.scheme == 'https' and bool(host) and any(host == suffix or host.endswith(f'.{suffix}') for suffix in allowed_suffixes)


def _image_dimensions_from_bytes(content: bytes) -> Tuple[int, int]:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(content or b'')) as image:
            image.load()
            width, height = image.size
        return int(width or 0), int(height or 0)
    except Exception:
        return 0, 0


def _download_source_image_for_asset(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    source_url: str,
    expected_hash: str = '',
    source_width: int = 0,
    source_height: int = 0,
) -> Dict[str, Any]:
    asset_id = str(asset_id or '').strip()
    source_url = str(source_url or '').strip()
    if not asset_id or not source_url:
        return {'ok': False, 'status': 'missing_source_url'}
    if not _creative_remote_source_allowed(source_url):
        return {'ok': False, 'status': 'source_url_not_allowed'}
    existing = _creative_source_file_for_asset(conn, asset_id)
    if existing and existing.is_file():
        content_hash = _sha256_file_if_exists(existing)
        width, height = _image_dimensions_from_bytes(existing.read_bytes())
        return {
            'ok': bool(content_hash),
            'status': 'cached',
            'route': _creative_asset_source_route(asset_id),
            'hash': content_hash,
            'width': width or int(source_width or 0),
            'height': height or int(source_height or 0),
            'quality': 'high_res' if max(width, height, int(source_width or 0), int(source_height or 0)) >= 600 else 'thumbnail',
        }
    request = urllib.request.Request(
        source_url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20.0) as response:
            content_type = str(response.headers.get('content-type') or '').split(';', 1)[0].strip().lower()
            content = response.read(12 * 1024 * 1024 + 1)
    except Exception as exc:
        return {'ok': False, 'status': 'download_failed', 'error': safe_provider_error(exc)}
    if not content or len(content) > 12 * 1024 * 1024 or not content_type.startswith('image/'):
        return {'ok': False, 'status': 'invalid_source_image'}
    width, height = _image_dimensions_from_bytes(content)
    quality = 'high_res' if max(width, height, int(source_width or 0), int(source_height or 0)) >= 600 else 'thumbnail'
    content_hash = hashlib.sha256(content).hexdigest()
    if expected_hash and re.fullmatch(r'[a-fA-F0-9]{64}', expected_hash) and content_hash.lower() != expected_hash.lower():
        return {'ok': False, 'status': 'source_hash_mismatch'}
    cache_dir = _creative_db_data_dir(conn) / 'ad_creative_sources'
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / _creative_source_filename(asset_id, content_type)
    tmp = target.with_suffix(target.suffix + '.tmp')
    tmp.write_bytes(content)
    tmp.replace(target)
    route = _creative_asset_source_route(asset_id)
    try:
        conn.execute(
            """
            UPDATE ad_creative_asset
            SET source_image_local_ref = ?,
                source_image_hash = ?,
                source_image_width = ?,
                source_image_height = ?,
                source_image_quality = ?,
                updated_at = ?
            WHERE asset_id = ?
            """,
            (route, content_hash, width or int(source_width or 0), height or int(source_height or 0), quality, utc_now(), asset_id),
        )
    except Exception:
        pass
    return {
        'ok': True,
        'status': 'downloaded',
        'route': route,
        'hash': content_hash,
        'width': width or int(source_width or 0),
        'height': height or int(source_height or 0),
        'quality': quality,
    }


def _sha256_file_if_exists(path: Optional[Path]) -> str:
    if not path or not path.is_file():
        return ''
    digest = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_true_source_image_reference(
    conn: sqlite3.Connection,
    reference: Dict[str, Any],
    *,
    force_asset_resolution: bool = False,
) -> Dict[str, Any]:
    resolved = dict(reference or {})
    existing_url = str(
        resolved.get('source_image_signed_url')
        or resolved.get('source_image_url')
        or ''
    ).strip()
    existing_hash = str(resolved.get('source_image_hash') or resolved.get('image_hash') or '').strip()
    existing_is_preview_route = (
        '/api/ops/ad-data-dashboard/creative-assets/' in existing_url
        and existing_url.rstrip('/').endswith('/preview')
    )
    if existing_url and existing_hash and not existing_is_preview_route and not force_asset_resolution:
        if _creative_remote_source_allowed(existing_url):
            existing_asset_id = str(
                resolved.get('source_image_id')
                or resolved.get('source_preview_asset_id')
                or resolved.get('asset_id')
                or ''
            ).strip()
            if existing_asset_id:
                downloaded = _download_source_image_for_asset(
                    conn,
                    asset_id=existing_asset_id,
                    source_url=existing_url,
                    expected_hash=existing_hash,
                )
                if downloaded.get('ok') and str(downloaded.get('quality') or '') != 'thumbnail':
                    resolved['source_image_signed_url'] = _creative_source_image_url(str(downloaded.get('route') or _creative_asset_source_route(existing_asset_id)))
                    resolved['source_image_hash'] = str(downloaded.get('hash') or existing_hash)
                    resolved['source_image_id'] = existing_asset_id
                    resolved['source_image_resolution_status'] = str(downloaded.get('status') or 'downloaded')
                    return resolved
            resolved['source_image_resolution_status'] = 'source_image_not_localized'
            return resolved
        parsed_existing_url = urlparse(existing_url)
        internal_source_route = bool(
            re.fullmatch(r'/api/ops/ad-data-dashboard/creative-assets/[^/]+/source/?', parsed_existing_url.path)
            or re.fullmatch(r'/api/ops/ad-data-dashboard/creative-images/[^/]+/?', parsed_existing_url.path)
        )
        internal_source_ref = parsed_existing_url.path + (f'?{parsed_existing_url.query}' if parsed_existing_url.query else '')
        resolved['source_image_signed_url'] = _creative_source_image_url(
            internal_source_ref if internal_source_route else existing_url
        )
        resolved['source_image_hash'] = existing_hash
        resolved.setdefault('source_image_id', str(resolved.get('source_image_id') or resolved.get('asset_id') or '').strip())
        if resolved.get('source_image_width'):
            resolved['source_image_width'] = int(resolved.get('source_image_width') or 0)
        if resolved.get('source_image_height'):
            resolved['source_image_height'] = int(resolved.get('source_image_height') or 0)
        resolved['source_image_resolution_status'] = 'provided'
        return resolved

    source_image_id = str(resolved.get('source_image_id') or '').strip()
    if source_image_id:
        try:
            row = conn.execute(
                """
                SELECT image_id, image_ref, image_hash
                FROM creative_generated_images
                WHERE image_id = ?
                LIMIT 1
                """,
                (source_image_id,),
            ).fetchone()
        except Exception:
            row = None
        if row:
            image_hash = str(row['image_hash'] or '').strip() or _sha256_file_if_exists(Path(str(row['image_ref'] or '')).expanduser())
            if image_hash:
                resolved.update({
                    'source_image_id': str(row['image_id']),
                    'source_image_signed_url': _creative_source_image_url(
                        f"/api/ops/ad-data-dashboard/creative-images/{row['image_id']}?download=1"
                    ),
                    'source_image_hash': image_hash,
                    'source_image_width': int(resolved.get('source_image_width') or 0),
                    'source_image_height': int(resolved.get('source_image_height') or 0),
                    'source_image_quality': str(resolved.get('source_image_quality') or 'generated_image'),
                    'source_image_resolution_status': 'generated_image',
                })
                return resolved

    title_values = [resolved.get('source_preview_title'), resolved.get('title')]
    identity_groups = [
        ('asset_id', [resolved.get('source_image_id'), resolved.get('source_preview_asset_id'), resolved.get('asset_id')]),
        ('ad_id', [resolved.get('source_ad_id'), resolved.get('ad_id')]),
        ('creative_id', [resolved.get('source_creative_id'), resolved.get('creative_id')]),
    ]
    if not any(str(value or '').strip() for _, values in identity_groups for value in values) and not any(
        str(value or '').strip() for value in title_values
    ):
        resolved['source_image_resolution_status'] = 'missing_source_identity'
        return resolved

    def _load_asset_rows(where_clauses: List[str], where_params: List[Any]) -> List[sqlite3.Row]:
        if not where_clauses:
            return []
        try:
            return conn.execute(
                f"""
                SELECT asset_id, ad_id, creative_id, title_text, local_media_ref, thumbnail_url, image_hash,
                       source_image_url, source_image_local_ref, source_image_hash,
                       source_image_width, source_image_height, source_image_quality
                FROM ad_creative_asset
                WHERE {' OR '.join(where_clauses)}
                ORDER BY last_seen_at DESC, updated_at DESC
                LIMIT 12
                """,
                tuple(where_params),
            ).fetchall()
        except Exception:
            try:
                return conn.execute(
                    f"""
                    SELECT asset_id, ad_id, creative_id, title_text, local_media_ref, thumbnail_url, image_hash
                    FROM ad_creative_asset
                    WHERE {' OR '.join(where_clauses)}
                    ORDER BY last_seen_at DESC, updated_at DESC
                    LIMIT 12
                    """,
                    tuple(where_params),
                ).fetchall()
            except Exception:
                return []

    rows: List[sqlite3.Row] = []
    for column, values in identity_groups:
        normalized_values = list(dict.fromkeys(str(value or '').strip() for value in values if str(value or '').strip()))
        rows = _load_asset_rows([f'{column} = ?' for _ in normalized_values], normalized_values)
        if rows:
            break
    if not rows:
        normalized_titles = list(dict.fromkeys(str(value or '').strip() for value in title_values if str(value or '').strip()))
        rows = _load_asset_rows(['title_text = ?' for _ in normalized_titles], normalized_titles)
    rows = list(rows or [])
    canonical_ad_ids = list(dict.fromkeys(
        str(row['ad_id'] or '').strip()
        for row in rows
        if hasattr(row, 'keys') and 'ad_id' in row.keys() and str(row['ad_id'] or '').strip()
    ))
    if len(canonical_ad_ids) > 1:
        resolved['source_image_resolution_status'] = 'source_identity_ambiguous'
        return resolved
    if canonical_ad_ids:
        try:
            sibling_rows = conn.execute(
                f"""
                SELECT asset_id, ad_id, creative_id, title_text, local_media_ref, thumbnail_url, image_hash,
                       source_image_url, source_image_local_ref, source_image_hash,
                       source_image_width, source_image_height, source_image_quality
                FROM ad_creative_asset
                WHERE ad_id = ?
                ORDER BY
                    CASE WHEN COALESCE(source_image_local_ref, '') <> '' THEN 0 ELSE 1 END,
                    CASE WHEN COALESCE(source_image_quality, '') = 'thumbnail' THEN 1 ELSE 0 END,
                    last_seen_at DESC,
                    updated_at DESC
                LIMIT 24
                """,
                (canonical_ad_ids[0],),
            ).fetchall()
        except Exception:
            sibling_rows = []
        if sibling_rows:
            rows_by_asset_id = {
                str(row['asset_id'] or '').strip(): row
                for row in rows
                if hasattr(row, 'keys') and 'asset_id' in row.keys() and str(row['asset_id'] or '').strip()
            }
            rows = list(sibling_rows) + [
                row for asset_id, row in rows_by_asset_id.items()
                if asset_id not in {
                    str(sibling['asset_id'] or '').strip()
                    for sibling in sibling_rows
                    if hasattr(sibling, 'keys') and 'asset_id' in sibling.keys()
                }
            ]
    for row in rows:
        asset_id = str(row['asset_id'] or '').strip()
        source_file = _creative_source_file_for_asset(conn, asset_id)
        row_keys = set(row.keys()) if hasattr(row, 'keys') else set()
        source_quality = str(row['source_image_quality'] or '').strip() if 'source_image_quality' in row_keys else ''
        source_width = int(row['source_image_width'] or 0) if 'source_image_width' in row_keys else 0
        source_height = int(row['source_image_height'] or 0) if 'source_image_height' in row_keys else 0
        source_url = (
            str(row['source_image_url'] or '').strip()
            if 'source_image_url' in row_keys
            else ''
        )
        source_hash = (
            str(row['source_image_hash'] or '').strip()
            if 'source_image_hash' in row_keys
            else ''
        ) or _sha256_file_if_exists(source_file)
        if not source_file and source_url:
            downloaded = _download_source_image_for_asset(
                conn,
                asset_id=asset_id,
                source_url=source_url,
                expected_hash=source_hash,
                source_width=source_width,
                source_height=source_height,
            )
            if downloaded.get('ok'):
                source_file = _creative_source_file_for_asset(conn, asset_id)
                source_hash = str(downloaded.get('hash') or source_hash).strip()
                source_width = int(downloaded.get('width') or source_width or 0)
                source_height = int(downloaded.get('height') or source_height or 0)
                source_quality = str(downloaded.get('quality') or source_quality or '').strip()
        if not asset_id or not source_hash or source_quality == 'thumbnail' or max(source_width, source_height) < 600:
            continue
        preview_route = str(row['local_media_ref'] or '').strip()
        if not preview_route.startswith('/api/ops/ad-data-dashboard/creative-assets/'):
            preview_route = _creative_asset_preview_route(asset_id)
        source_route = (
            str(row['source_image_local_ref'] or '').strip()
            if 'source_image_local_ref' in row_keys
            else ''
        )
        if not source_route.startswith('/api/ops/ad-data-dashboard/creative-assets/'):
            source_route = _creative_asset_source_route(asset_id)
        resolved.update({
            'source_ad_id': str(row['ad_id'] or '').strip(),
            'source_creative_id': str(row['creative_id'] or '').strip(),
            'source_image_id': asset_id,
            'source_preview_asset_id': resolved.get('source_preview_asset_id') or asset_id,
            'source_preview_url': resolved.get('source_preview_url') or preview_route,
            'source_preview_title': resolved.get('source_preview_title') or row['title_text'] or row['ad_id'] or row['creative_id'] or '',
            'source_image_signed_url': _creative_source_image_url(source_route),
            'source_image_hash': source_hash,
            'source_image_width': source_width,
            'source_image_height': source_height,
            'source_image_resolution_status': 'creative_asset_cache',
            'source_image_quality': source_quality or 'high_res',
        })
        return resolved
    resolved['source_image_resolution_status'] = 'source_image_not_synced'
    return resolved


def build_visible_claim_policy(market: str, direction_key: str = '') -> Dict[str, Any]:
    if direction_key == CREATIVE_DIRECTION_SAFE_COMPLIANCE:
        return {
            'version': VISIBLE_CLAIM_POLICY_VERSION,
            **SAFE_COMPLIANCE_VISIBLE_CLAIM_POLICY,
            'market': market,
            'currency_threshold_version': CURRENCY_THRESHOLD_VERSION,
            'currency_thresholds': {},
            'localized_negative_constraints': localized_negative_constraints(market),
        }
    return {
        **VISIBLE_CLAIM_POLICY,
        'market': market,
        'currency_threshold_version': CURRENCY_THRESHOLD_VERSION,
        'currency_thresholds': CURRENCY_REWARD_THRESHOLDS.get(market, {}),
        'localized_negative_constraints': localized_negative_constraints(market),
    }


def build_creative_prompt_package(
    *,
    job_id: str = '',
    experiment_id: str = '',
    experiment_code: str = '',
    market: str,
    brand: str,
    country: Any,
    language_hint: str,
    image_size: str,
    creative_direction: Dict[str, Any],
    prompt: str,
    negative_prompt: str,
    source_reference: Optional[Dict[str, Any]] = None,
    preserve: Optional[List[str]] = None,
    modify: Optional[List[str]] = None,
    revision_plan: Optional[Dict[str, Any]] = None,
    source_image_structure: Optional[Dict[str, Any]] = None,
    candidate_count: int = 1,
) -> Dict[str, Any]:
    direction_key = str(creative_direction.get('key') or CREATIVE_DIRECTION_POINTS_REWARD)
    safe_compliance = direction_key == CREATIVE_DIRECTION_SAFE_COMPLIANCE
    source = source_image_fields_from_reference(source_reference or {})
    package_preserve = (
        ['official logo pixels and proportions', 'target market language', 'product/app identity']
        if safe_compliance else
        (preserve or ['brand identity', 'target market language', 'product/app category'])
    )
    package_modify = (
        ['bright realistic daylight app-use scene', 'product-faithful conceptual activities-progress-points-rewards UI', 'named activity rows', 'internally consistent progress summary', 'in-app points state', 'generic non-cash reward destination', 'semantic copy variation', 'readable feature proof', 'no fake CTA']
        if safe_compliance else
        (modify or ['visual hierarchy', 'headline clarity', 'task reward proof module', 'phone UI handoff'])
    )
    safe_blueprint = safe_compliance_generation_blueprint(market, brand) if safe_compliance else {}
    constrained_prompt = prompt if safe_compliance else _append_currency_reward_constraint(prompt, market)
    return {
        'version': PROMPT_PACKAGE_VERSION,
        'job_id': job_id,
        'experiment_id': experiment_id,
        'experiment_code': experiment_code,
        'brand': brand,
        'country': country,
        'market': market,
        'language': language_hint,
        'image_size': image_size,
        'generation_mode': source['generation_mode'],
        'creative_direction': direction_key,
        'creative_direction_name': creative_direction.get('name'),
        'internal_business_goal': (
            ['evaluate low-risk product discovery and official brand-trust creative']
            if safe_compliance else
            PUBLIC_AD_POSITIONING['internal_business_goal']
        ),
        'internal_goal_visibility_policy': PUBLIC_AD_POSITIONING['internal_goal_visibility_policy'],
        'public_ad_positioning': public_ad_positioning_for(direction_key),
        'verified_app_functionality': SAFE_COMPLIANCE_FUNCTIONALITY_CONTRACT if safe_compliance else {},
        'visible_claim_policy': build_visible_claim_policy(market, direction_key),
        'currency_reward_contract': {} if safe_compliance else currency_reward_generation_contract(market),
        'brand_visual_guidelines': brand_visual_guidelines_for(market, direction_key),
        'required_visible_elements': list(creative_direction.get('required_visible_elements') or []),
        'headline_candidates': list(safe_blueprint['visible_copy']['headline_candidates']) if safe_compliance else headline_candidates_for(market, direction_key),
        'safe_generation_blueprint': safe_blueprint,
        'brand_assets': brand_assets_for(direction_key),
        'negative_constraints': SAFE_COMPLIANCE_VISIBLE_CLAIM_POLICY['forbidden_claims'] if safe_compliance else VISIBLE_CLAIM_POLICY['forbidden_claims'],
        'localized_negative_constraints': localized_negative_constraints(market),
        'source_image': source,
        'revision_plan': revision_plan or {},
        'source_image_structure': {} if safe_compliance else (source_image_structure or source_image_structure_from_reference(source_reference or {})),
        'preserve': package_preserve,
        'modify': package_modify,
        'currency_threshold_version': CURRENCY_THRESHOLD_VERSION,
        'candidate_count': candidate_count,
        'final_prompt': constrained_prompt,
        'negative_prompt': negative_prompt,
    }


def external_image_provider_readiness(config: Optional[ExternalImageProviderConfig]) -> Dict[str, Any]:
    config = config or ExternalImageProviderConfig()
    provider = (config.provider or '').strip().lower()
    chatgpt_pro_manual = provider == PROVIDER_CHATGPT_PRO_MANUAL
    local_production_provider = provider == PROVIDER_LOCAL_PRODUCTION_PNG
    hermes_image2_agent = provider == PROVIDER_HERMES_IMAGE2_AGENT
    fixture_provider = provider in {'', 'fixture', PROVIDER_FIXTURE, 'local'}
    external_provider = not fixture_provider and not chatgpt_pro_manual and not local_production_provider and not hermes_image2_agent
    blocking_reasons = [
        reason for reason, blocked in {
            'image_provider_disabled': not config.enabled,
            'image_provider_not_configured': not external_provider and not chatgpt_pro_manual and not local_production_provider and not hermes_image2_agent,
            'image_provider_url_missing': external_provider and not bool(str(config.url or '').strip()),
            'image_provider_api_key_missing': external_provider and not bool(str(config.api_key or '').strip()),
            'image_provider_session_missing': external_provider and config.session is None,
        }.items() if blocked
    ]
    if chatgpt_pro_manual:
        mode = PROVIDER_CHATGPT_PRO_MANUAL
    elif local_production_provider:
        mode = PROVIDER_LOCAL_PRODUCTION_PNG
    elif hermes_image2_agent:
        mode = PROVIDER_HERMES_IMAGE2_AGENT
    elif external_provider:
        mode = PROVIDER_EXTERNAL_WRAPPER
    else:
        mode = PROVIDER_FIXTURE
    return {
        'provider': provider or PROVIDER_FIXTURE,
        'mode': mode,
        'enabled': bool(config.enabled),
        'url_configured': bool(str(config.url or '').strip()),
        'api_key_configured': bool(str(config.api_key or '').strip()),
        'session_configured': config.session is not None,
        'ready': not blocking_reasons,
        'blocking_reasons': blocking_reasons,
        'manual_workbench': chatgpt_pro_manual,
        'local_production': local_production_provider,
        'hermes_image2_agent': hermes_image2_agent,
        'requires_external_url': external_provider,
        'requires_external_api_key': external_provider,
    }


def build_feed_static_ad_prompt(brief: CreativeImageGenerationBrief) -> CreativeImagePrompt:
    market, profile = normalize_market(brief.country, brief.project)
    if not market:
        country_text = str(brief.country or brief.project or 'unknown')
        prompt_hash = stable_id(FEED_STATIC_AD_SURFACE, country_text, 'blocked')
        return CreativeImagePrompt(
            surface=FEED_STATIC_AD_SURFACE,
            width=DEFAULT_FEED_IMAGE_SIZE[0],
            height=DEFAULT_FEED_IMAGE_SIZE[1],
            market='',
            brand='',
            prompt='',
            negative_prompt='',
            required_components=list(REQUIRED_PROMPT_COMPONENTS.values()),
            compliance_notes=['未知国家或项目，需人工选择品牌后再生成。'],
            prompt_hash=prompt_hash,
            review_status='needs_manual_input',
            risk_status='blocked',
            risk_tags=['unknown_market'],
        )

    performance = brief.source_performance or {}
    cost = performance.get('cost')
    installs = performance.get('installs')
    joins = performance.get('guild_joins')
    evidence_line = f"参考表现：消耗 {cost if cost not in (None, '') else '-'}，安装 {installs if installs not in (None, '') else '-'}，真实入会 {joins if joins not in (None, '') else '-'}。"
    direction = creative_direction_profile(brief.core_offer)
    direction_key = str(direction.get('key') or CREATIVE_DIRECTION_POINTS_REWARD)
    headline_candidates = headline_candidates_for(market, direction_key)
    visible_policy = build_visible_claim_policy(market, direction_key)
    safe_compliance = direction_key == CREATIVE_DIRECTION_SAFE_COMPLIANCE
    public_positioning = public_ad_positioning_for(direction_key)
    subheadline = SAFE_COMPLIANCE_SUBHEADLINES.get(market, '') if safe_compliance else str(profile['subheadline'])
    safe_blueprint = safe_compliance_generation_blueprint(market, str(profile['brand'])) if safe_compliance else {}
    audience_line = (
        'broad adult users interested in completing simple in-app tasks, collecting app points, and viewing available in-app rewards'
        if safe_compliance
        else 'people interested in mobile reward apps, simple phone tasks, and beginner-friendly in-app guidance'
    )
    reference_lines: List[str] = []
    if str(brief.source_preview_url or '').strip():
        internal_diagnosis = ' '.join(str(brief.source_diagnosis or evidence_line).split())
        reference_lines = [
            "Generation mode: direction_redraw.",
            "This is not a pixel-preserving image-to-image edit because only a preview/reference URL is available, not a true source image file.",
            f"Original ad preview reference: {brief.source_preview_url}.",
            f"Internal reference URL (not visible copy): {brief.source_preview_url}.",
            f"Internal reference label (not visible copy): {brief.source_preview_title or brief.ad or '-'}; asset id: {brief.source_preview_asset_id or '-'}.",
            "Use the original ad only as market/brand/context reference: keep recognizable brand identity, market language, product/app category, and any useful proven visual motif.",
            f"Revision goal: {brief.revision_goal or 'repair weak creative expression while preserving useful original context'}.",
            f"Internal diagnosis/evidence (not visible copy): {internal_diagnosis}.",
            "Do not blindly copy the original; improve visual hierarchy, headline emphasis, reward/task proof module, trust cues, and app handoff according to the selected creative direction.",
        ]
    prompt = '\n'.join([
        f"Create a high-conversion Meta/Facebook feed static ad image for {profile['brand']} in {profile['market_label']}.",
        "Format: 1024x1024 square, full-bleed composition, no white margin, no border, no framed blank canvas.",
        "The image must look like a social feed performance ad, not a download page screenshot, product page layout, or plain UI showcase.",
        f"Language: {profile['language_hint']}.",
        (f"Approved headline options: {' / '.join(safe_blueprint['visible_copy']['headline_candidates'])}; choose one or write a faithful market-language paraphrase with the same product meaning." if safe_compliance else f"Headline candidates on image: {' / '.join(headline_candidates)}."),
        (f"Approved subheadline/supporting-copy options: {' / '.join(safe_blueprint['visible_copy']['subheadline_candidates'])}; wording may vary but must preserve the required semantics." if safe_compliance else f"Subheadline: {subheadline}."),
        f"Public ad positioning: {', '.join(public_positioning)}.",
        "Internal business goal is strictly internal and must not appear visually or textually in the ad.",
        f"Audience: {audience_line}.",
        ("Human visual requirement: one adult woman may appear only as natural supporting usage context; the product story board is the primary visual." if safe_compliance else FEMALE_ONLY_VISUAL_REQUIREMENT),
        f"Selected creative direction: {brief.core_offer}.",
        f"Creative direction key: {direction_key}.",
        f"Primary focus: {direction['name']}.",
        f"Visual emphasis: {direction['primary_visual']}.",
        f"Composition system: {direction['composition_system']}.",
        f"Reward hierarchy: {direction['reward_hierarchy']}.",
        f"Direction separation guard: {direction['distinctness_guard']}.",
        f"Headline role: {direction['headline_role']}.",
        f"Proof point: {direction['proof']}.",
        f"CTA emphasis: {direction['cta']}.",
        f"Required visible elements: {', '.join(direction.get('required_visible_elements') or [])}.",
        *( [
            f"Official logo source: {_creative_source_image_url(OFFICIAL_APP_LOGO_PATH)}; sha256={OFFICIAL_APP_LOGO_SHA256}.",
            "Do not render, redraw, imitate, or write any logo, brand symbol, or brand wordmark inside the generated image.",
            "Keep a slim light or high-key brand area integrated with the composition; reserve only its rightmost 28-32% without text or icons so the system can programmatically composite a compact verified light logo-and-wordmark card there after generation.",
            "The diamond logo is a brand mark only. Never present it as money, currency, a coin, an app-point token, a prize, treasure, or a financial benefit.",
            "Use one concise headline and one short supporting sentence. Natural market-language paraphrases are allowed only when they preserve the same meaning: app activities, visible progress, in-app points, and rewards available in the app.",
            "The phone interface may be conceptual rather than an exact screenshot, because the production UI may change between versions.",
            "Conceptual does not mean fictional: every visible module must map to the verified app flow—complete a task, receive app points, then exchange points for available non-cash rewards inside the app.",
            "Show named in-app activities, visible completion states, one internally consistent progress summary, a readable points state, and a clear generic reward destination; use icons and hierarchy instead of tiny explanatory copy.",
            "The reward destination must be unmistakable but need not dominate the whole phone; an open gift box, reward cards, abstract geometry, or other compliant objects are all allowed, but they must have polished iconography, material detail, coherent highlights, and contact shadows rather than raw circles, triangles, or rectangles.",
            "Do not show CTA buttons, fake native controls, legal-style microcopy, or unreadable helper text anywhere.",
            "Do not invent content categories or unrelated functions such as news, recipes, travel, shopping, courses, entertainment, home decor, social feed, community, chat, jobs, or recruitment.",
            "Build a complete product story board around one unmistakable smartphone: phone and product proof occupy 48-58% of the canvas; the woman is natural supporting context at 20-28% and must not use an awkward open-palm presentation pose.",
            "Show exactly three connected proof modules with no decorative placeholders: activity/progress dashboard, in-app points state, and a non-cash in-app reward destination.",
            "Keep a slim light or high-key brand area; leave only the rightmost 28-32% clean for the system to add the compact verified light logo-and-wordmark card. Do not leave a dead empty band.",
            "Use a flexible sunlit color family appropriate to the market and direction. Keep the overall image high-key, airy, optimistic, and inviting; avoid night scenes, dark-dominant canvases, heavy amber grading, muddy colors, giant cropped phones, and generic stock-photo templates.",
            "Match the craft level of a finished commercial performance poster: use one coherent light source, material-specific highlights, soft contact shadows, layered cards, nested icon containers, intentional edge treatment, and foreground/middle-ground/background depth through overlap, scale, perspective, and selective edge cropping.",
            "Do not simplify decorative or reward elements into flat primitive geometry, generic SVG clip-art, single-color wave filler, random four-point stars, or presentation-slide decoration. Waves, ribbons, sparkles, gifts, cards, room settings, and abstract shapes remain allowed when they are dimensional, integrated, and purposeful.",
            "Keep copy compact and highly readable; preserve generous spacing and never let copy compete with the phone dashboard.",
        ] if safe_compliance else []),
        ("This sole safe-compliance blueprint owns the entire composition; do not blend any generic reward, process, advisor, or old-image layout system into it." if safe_compliance else "The selected direction must own at least 60% of the visual information area. Do not reuse one universal phone-card layout across directions, and do not blend the other two direction systems into this image."),
        "Keep only the shared brand and compliance anchors; the selected composition system must determine the layout, hierarchy, headline emphasis, and proof module.",
        *reference_lines,
        evidence_line,
        ("Visual anchor: a strong hero visual built around unmistakable smartphone phone UI with one readable activities-to-progress-to-points-to-rewards path; the adult female person supports the everyday-use context; compact meaning-compatible copy hierarchy and the bottom brand lockup close the ad." if safe_compliance else "Shared visual anchors only: strong hero visual led by an adult female person, bold headline, short subheadline, and bottom brand lockup. Include phone UI only in the direction-specific form described above."),
        ("Tone: polished, credible, bright product-led performance creative with a strong activities-to-progress-to-points-to-in-app-rewards path, realistic daylight app use, product-faithful functionality, and no cash-income or employment cues."
         if safe_compliance else
         "Tone: bright, sunlit, optimistic mobile app advertising with local guidance, simple tasks, clear in-app steps, and a friendly download-worthy first impression; credible but not exaggerated."),
        f"Visible claim policy: allowed={visible_policy['allowed_claims']}; manual_review={visible_policy['manual_review_claims']}; forbidden={visible_policy['forbidden_claims']}.",
        "Compliance: keep reward examples small and contextual; do not guarantee income; do not show explicit phone number, email, WhatsApp contact, ID card, bank card, cash piles, cash rain, withdrawal proof, payout screenshot, creator recruitment, social chat job, MCN, guild, or host recruitment.",
        *localized_negative_constraints(market),
    ])
    negative_prompt = '; '.join([
        'download page screenshot',
        'product page layout',
        'plain UI showcase',
        'white margin',
        'border frame',
        'night scene',
        'low-key cinematic lighting',
        'dark-dominant canvas',
        'muddy color grading',
        'raw primitive geometric placeholder',
        'flat SVG filler',
        'circle triangle rectangle placeholder art',
        'single-color empty wave filler',
        'random sparkle filler',
        'presentation-slide decoration',
        'generic vector clipart',
        'cash pile',
        'cash rain',
        'guaranteed income',
        'phone number',
        'email address',
        'bank card',
        'ID document',
        'creator recruitment',
        'social chatting job',
        'chat-to-earn',
        'MCN',
        'guild conversion',
        'host recruitment',
        *( [
            'money', 'currency symbol', 'cash value', 'wallet', 'cash balance', 'payout', 'withdrawal', 'salary',
            'job', 'employment', 'recruitment', 'income', 'earning', 'work from home', 'paid task claim', 'guaranteed reward',
            'oversized diamond logo', 'floating diamonds', 'gold coins', 'real-world coin', 'currency-like coin', 'treasure', 'jackpot', 'prize badge',
            'cash symbolism', 'wealth imagery', 'duplicated logo', 'regenerated logo', 'altered logo',
            'fake notification', 'fake testimonial', 'fake native CTA button', 'in-app button', 'CTA ribbon', 'navigation bar', 'third-party logo',
            'placeholder task label', 'Activity 1', 'Atividade 1', 'Tarea 1', 'Aktivitas 1',
            'standalone completed row', 'tiny helper text', 'unreadable microcopy', 'merchant-branded gift card', 'named prize', 'model-generated logo', 'model-generated wordmark',
            'generic content feed', 'news category', 'recipe category', 'travel category', 'shopping category',
            'course category', 'entertainment category', 'home decor category', 'social feed', 'community', 'chat module',
        ] if safe_compliance else []),
    ])
    if not safe_compliance:
        prompt = _append_currency_reward_constraint(prompt, market)
    risk_status, risk_tags = review_prompt_safety(prompt)
    prompt_hash = stable_id(FEED_STATIC_AD_SURFACE, market, profile['brand'], prompt, negative_prompt)
    return CreativeImagePrompt(
        surface=FEED_STATIC_AD_SURFACE,
        width=DEFAULT_FEED_IMAGE_SIZE[0],
        height=DEFAULT_FEED_IMAGE_SIZE[1],
        market=market,
        brand=profile['brand'],
        prompt=prompt,
        negative_prompt=negative_prompt,
        required_components=list(REQUIRED_PROMPT_COMPONENTS.values()),
        compliance_notes=[
            '只生成信息流广告图初稿，不自动发布。',
            '默认方图 1024x1024，满版无白边。',
            ('安全合规方向完整展示任务、积分与 App 内非现金奖励闭环，但禁止现金价值、提现、收入、就业、招聘、保证收益、按钮和虚构功能。' if safe_compliance else '收益表达必须保守，不承诺固定收入。'),
        ],
        prompt_hash=prompt_hash,
        review_status='needs_review' if risk_tags else 'pending_review',
        risk_status=risk_status,
        risk_tags=risk_tags,
    )


def review_prompt_safety(text: str, *, required_components: Optional[Iterable[str]] = None) -> Tuple[str, List[str]]:
    tags: List[str] = []
    checked_text = '\n'.join(
        line for line in str(text or '').splitlines()
        if not line.lower().startswith('compliance:')
        and not line.lower().startswith('visible claim policy:')
        and not line.lower().startswith(('original ad preview reference:', 'internal reference url (not visible copy):', 'internal reference label (not visible copy):', 'internal diagnosis/evidence (not visible copy):'))
        and 'no white margin' not in line.lower()
        and 'do not ' not in line.lower()
        and not line.lower().startswith(('no guaranteed income', 'não mostre', 'jangan tampilkan', 'no muestres'))
        and 'not a download page' not in line.lower()
    )
    for tag, pattern in [*PII_PATTERNS, *INCOME_RISK_PATTERNS, *STYLE_RISK_PATTERNS]:
        if pattern.search(checked_text):
            tags.append(tag)
    lower = (text or '').lower()
    component_map = required_components or REQUIRED_PROMPT_COMPONENTS.keys()
    required_tokens = {
        'strong_hero_visual': ['strong hero', '强首图'],
        'headline': ['headline', '大标题'],
        'subheadline': ['subheadline', '副标题'],
        'phone_ui': ['phone ui', '手机 ui'],
        'trust_person': ['person', '人物'],
        'brand_footer': ['brand lockup', '底部品牌'],
    }
    for component in component_map:
        tokens = required_tokens.get(str(component), [])
        if tokens and not any(token in lower for token in tokens):
            tags.append(f'missing_{component}')
    tags = sorted(set(tags))
    if any(tag in tags for tag in {'email', 'phone_number', 'whatsapp_contact', 'bank_card', 'id_card', 'guaranteed_income', 'fixed_income'}):
        return 'blocked', tags
    if tags:
        return 'needs_review', tags
    return 'ok', []


def ensure_creative_image_generation_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
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
        );
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
            created_at TEXT NOT NULL
        );
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
        );
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
        );
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
            payload_json TEXT NOT NULL
        );
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
        );
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
        );
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
        );
        CREATE TABLE IF NOT EXISTS creative_generation_tasks (
            task_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            generation_request_id TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT 'hermes_image2_agent',
            provider_mode TEXT NOT NULL DEFAULT 'hermes_image2_agent',
            status TEXT NOT NULL DEFAULT 'queued',
            image_size TEXT NOT NULL DEFAULT '1024x1024',
            prompt TEXT NOT NULL DEFAULT '',
            negative_prompt TEXT NOT NULL DEFAULT '',
            candidate_count INTEGER NOT NULL DEFAULT 1,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            accepted_image_count INTEGER NOT NULL DEFAULT 0,
            rejected_candidate_count INTEGER NOT NULL DEFAULT 0,
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            quality_summary_json TEXT NOT NULL DEFAULT '{}',
            payload_json TEXT NOT NULL DEFAULT '{}',
            provider_response_json TEXT NOT NULL DEFAULT '{}',
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
    _ensure_columns(conn, 'creative_generated_images', {
        'image_hash': "TEXT NOT NULL DEFAULT ''",
        'perceptual_hash': "TEXT NOT NULL DEFAULT ''",
        'final_delivery_hash': "TEXT NOT NULL DEFAULT ''",
        'source_provider': "TEXT NOT NULL DEFAULT ''",
        'uploaded_manually': "INTEGER NOT NULL DEFAULT 0",
        'uploaded_final_version': "INTEGER NOT NULL DEFAULT 0",
        'is_exact_generated_asset': "INTEGER NOT NULL DEFAULT 1",
        'task_id': "TEXT NOT NULL DEFAULT ''",
        'generation_mode': "TEXT NOT NULL DEFAULT ''",
        'creative_direction': "TEXT NOT NULL DEFAULT ''",
        'candidate_index': "INTEGER NOT NULL DEFAULT 0",
        'width': "INTEGER NOT NULL DEFAULT 0",
        'height': "INTEGER NOT NULL DEFAULT 0",
        'file_path': "TEXT NOT NULL DEFAULT ''",
        'thumbnail_path': "TEXT NOT NULL DEFAULT ''",
        'file_size_bytes': "INTEGER NOT NULL DEFAULT 0",
        'file_quality_status': "TEXT NOT NULL DEFAULT ''",
        'ocr_text_check_status': "TEXT NOT NULL DEFAULT ''",
        'currency_reward_check_status': "TEXT NOT NULL DEFAULT ''",
        'direction_fit_status': "TEXT NOT NULL DEFAULT ''",
        'public_positioning_fit_status': "TEXT NOT NULL DEFAULT ''",
        'visible_risk_status': "TEXT NOT NULL DEFAULT ''",
        'old_image_preservation_status': "TEXT NOT NULL DEFAULT ''",
        'quality_check_json': "TEXT NOT NULL DEFAULT '{}'",
        'final_verdict': "TEXT NOT NULL DEFAULT ''",
    })
    _ensure_columns(conn, 'creative_generation_tasks', {
        'provider_response_json': "TEXT NOT NULL DEFAULT '{}'",
        'lease_owner': "TEXT NOT NULL DEFAULT ''",
        'lease_expires_at': "TEXT NOT NULL DEFAULT ''",
        'started_at': "TEXT NOT NULL DEFAULT ''",
        'finished_at': "TEXT NOT NULL DEFAULT ''",
        'generation_mode': "TEXT NOT NULL DEFAULT ''",
        'creative_direction': "TEXT NOT NULL DEFAULT ''",
        'prompt_package_json': "TEXT NOT NULL DEFAULT '{}'",
        'final_prompt': "TEXT NOT NULL DEFAULT ''",
        'source_image_id': "TEXT NOT NULL DEFAULT ''",
        'source_image_signed_url': "TEXT NOT NULL DEFAULT ''",
        'source_image_hash': "TEXT NOT NULL DEFAULT ''",
        'source_image_required': "INTEGER NOT NULL DEFAULT 0",
        'source_image_used': "INTEGER NOT NULL DEFAULT 0",
        'preserve_json': "TEXT NOT NULL DEFAULT '[]'",
        'modify_json': "TEXT NOT NULL DEFAULT '[]'",
        'visible_claim_policy_json': "TEXT NOT NULL DEFAULT '{}'",
        'brand_visual_guidelines_json': "TEXT NOT NULL DEFAULT '{}'",
        'currency_threshold_version': "TEXT NOT NULL DEFAULT ''",
        'heartbeat_at': "TEXT NOT NULL DEFAULT ''",
        'state_history_json': "TEXT NOT NULL DEFAULT '[]'",
    })
    _ensure_columns(conn, 'creative_adoption_records', {
        'experiment_id': "TEXT NOT NULL DEFAULT ''",
        'experiment_code': "TEXT NOT NULL DEFAULT ''",
        'suggestion_id': "TEXT NOT NULL DEFAULT ''",
        'generation_request_id': "TEXT NOT NULL DEFAULT ''",
        'generated_image_id': "TEXT NOT NULL DEFAULT ''",
        'source_ad_id': "TEXT NOT NULL DEFAULT ''",
        'source_creative_id': "TEXT NOT NULL DEFAULT ''",
        'adopted_ad_id': "TEXT NOT NULL DEFAULT ''",
        'adopted_creative_id': "TEXT NOT NULL DEFAULT ''",
        'adopted_adset_id': "TEXT NOT NULL DEFAULT ''",
        'adopted_campaign_id': "TEXT NOT NULL DEFAULT ''",
        'adoption_type': "TEXT NOT NULL DEFAULT ''",
        'binding_method': "TEXT NOT NULL DEFAULT ''",
        'binding_confidence': "TEXT NOT NULL DEFAULT ''",
        'binding_status': "TEXT NOT NULL DEFAULT ''",
        'matched_at': "TEXT NOT NULL DEFAULT ''",
        'confirmed_by': "TEXT NOT NULL DEFAULT ''",
        'confirmed_at': "TEXT NOT NULL DEFAULT ''",
        'evidence_json': "TEXT NOT NULL DEFAULT '{}'",
        'notes': "TEXT NOT NULL DEFAULT ''",
    })
    _ensure_columns(conn, 'creative_pro_work_queue', {
        'analysis_json': "TEXT NOT NULL DEFAULT '{}'",
        'generation_plan_json': "TEXT NOT NULL DEFAULT '{}'",
        'error_code': "TEXT NOT NULL DEFAULT ''",
        'error_message': "TEXT NOT NULL DEFAULT ''",
    })


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    existing = {row['name'] if isinstance(row, sqlite3.Row) else row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')


def persist_generation_result(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    brief: CreativeImageGenerationBrief,
    prompt: CreativeImagePrompt,
    image: Optional[GeneratedCreativeImage],
) -> None:
    ensure_creative_image_generation_tables(conn)
    now = utc_now()
    status = 'blocked' if prompt.risk_status == 'blocked' else 'generated'
    conn.execute(
        """
        INSERT OR REPLACE INTO creative_generation_requests
        (request_id, surface, image_size, market, brand, country, project, campaign, ad_group, ad, objective,
         prompt, negative_prompt, prompt_hash, risk_status, risk_tags_json, review_status, status,
         requested_by, payload_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            prompt.surface,
            f'{prompt.width}x{prompt.height}',
            prompt.market,
            prompt.brand,
            brief.country,
            brief.project,
            brief.campaign,
            brief.ad_group,
            brief.ad,
            brief.objective,
            prompt.prompt,
            prompt.negative_prompt,
            prompt.prompt_hash,
            prompt.risk_status,
            json.dumps(prompt.risk_tags, ensure_ascii=False),
            prompt.review_status,
            status,
            brief.requested_by,
            json.dumps(asdict(brief), ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )
    if image is not None:
        image_hash = file_sha256(Path(image.image_ref))
        metadata = dict(image.metadata or {})
        creative_direction = creative_direction_key(brief.core_offer)
        metadata.setdefault('creative_direction', creative_direction)
        if image_hash:
            metadata.setdefault('image_hash', image_hash)
        conn.execute(
            """
            INSERT OR REPLACE INTO creative_generated_images
            (image_id, request_id, surface, image_size, market, brand, image_ref, thumbnail_ref, prompt_hash,
             risk_status, risk_tags_json, review_status, provider, metadata_json, created_at, image_hash, source_provider,
             creative_direction, is_exact_generated_asset)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                image.image_id,
                image.request_id,
                image.surface,
                image.image_size,
                image.market,
                image.brand,
                image.image_ref,
                image.thumbnail_ref,
                image.prompt_hash,
                image.risk_status,
                json.dumps(image.risk_tags, ensure_ascii=False),
                image.review_status,
                image.provider,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                image.created_at,
                image_hash,
                image.provider,
                creative_direction,
                1,
            ),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO creative_generated_image_links
            (link_id, image_id, platform, campaign, ad_group, ad, status, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f'link_{stable_id(image.image_id, brief.campaign, brief.ad_group, brief.ad)}',
                image.image_id,
                'Meta',
                brief.campaign,
                brief.ad_group,
                brief.ad,
                'draft',
                json.dumps({'source': 'ad_dashboard'}, ensure_ascii=False),
                image.created_at,
            ),
        )
    conn.commit()


def generate_fixture_feed_image(
    prompt: CreativeImagePrompt,
    brief: CreativeImageGenerationBrief,
    *,
    output_dir: Path = DEFAULT_IMAGE_OUTPUT_DIR,
) -> Optional[GeneratedCreativeImage]:
    if prompt.risk_status == 'blocked':
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    request_id = f'creative_req_{stable_id(prompt.prompt_hash, brief.campaign, brief.ad_group, brief.ad)}'
    image_id = f'feed_img_{stable_id(request_id, prompt.prompt_hash)}'
    image_path = output_dir / f'{image_id}.svg'
    svg = fixture_svg(prompt, brief)
    image_path.write_text(svg, encoding='utf-8')
    created_at = utc_now()
    return GeneratedCreativeImage(
        image_id=image_id,
        request_id=request_id,
        surface=prompt.surface,
        image_size=f'{prompt.width}x{prompt.height}',
        market=prompt.market,
        brand=prompt.brand,
        image_ref=str(image_path),
        thumbnail_ref=str(image_path),
        prompt_hash=prompt.prompt_hash,
        risk_status=prompt.risk_status,
        risk_tags=prompt.risk_tags,
        review_status=prompt.review_status,
        provider='fixture_svg',
        created_at=created_at,
        metadata={
            'full_bleed': True,
            'no_white_margin': True,
            'surface_label': '信息流广告图',
            'width': prompt.width,
            'height': prompt.height,
        },
    )


def _load_feed_font(size: int, *, bold: bool = False) -> Any:
    from PIL import ImageFont

    candidates = [
        '/Library/Fonts/Arial Unicode.ttf',
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
        '/System/Library/Fonts/Hiragino Sans GB.ttc',
        '/System/Library/Fonts/STHeiti Medium.ttc',
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold else '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/System/Library/Fonts/Supplemental/Helvetica Bold.ttf' if bold else '/System/Library/Fonts/Supplemental/Helvetica.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_feed_text(draw: Any, text: str, font: Any, max_width: int, *, max_lines: int = 3) -> List[str]:
    words = str(text or '').split()
    if not words:
        return []
    lines: List[str] = []
    current = ''
    for word in words:
        candidate = f'{current} {word}'.strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = candidate
            continue
        lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    return lines


def _draw_round_rect(draw: Any, xy: Tuple[int, int, int, int], radius: int, fill: str, outline: Optional[str] = None, width: int = 1) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _contains_cjk(text: str) -> bool:
    return any('\u4e00' <= char <= '\u9fff' for char in str(text or ''))


def generate_local_production_feed_image(
    prompt: CreativeImagePrompt,
    brief: CreativeImageGenerationBrief,
    *,
    output_dir: Path = DEFAULT_IMAGE_OUTPUT_DIR,
) -> Optional[GeneratedCreativeImage]:
    if prompt.risk_status == 'blocked':
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    profile = SUPPORTED_BRAND_MARKETS.get(prompt.market, {})
    brand = str(prompt.brand or profile.get('brand') or 'TUGAO')
    headline = str(profile.get('headline') or 'Start as creator')
    subheadline = str(profile.get('subheadline') or 'Local support and clear application flow')
    campaign = str(brief.campaign or brief.project or '').strip()
    angle = str(brief.core_offer or subheadline).strip()
    palette = {
        'TUGAO': {
            'bg': '#0b6b55',
            'bg2': '#063f35',
            'accent': '#f5c45e',
            'soft': '#e9fff4',
            'ink': '#06241f',
            'cta': '#103b78',
        },
        'Premiou': {
            'bg': '#8b3f22',
            'bg2': '#5c2416',
            'accent': '#ffd166',
            'soft': '#fff2df',
            'ink': '#35170d',
            'cta': '#0f766e',
        },
        'Recompa': {
            'bg': '#4338ca',
            'bg2': '#1e1b4b',
            'accent': '#f472b6',
            'soft': '#eef2ff',
            'ink': '#171433',
            'cta': '#be185d',
        },
    }.get(brand, {
        'bg': '#14532d',
        'bg2': '#0f172a',
        'accent': '#facc15',
        'soft': '#f8fafc',
        'ink': '#111827',
        'cta': '#2563eb',
    })

    width, height = prompt.width, prompt.height
    image = Image.new('RGB', (width, height), palette['bg'])
    draw = ImageDraw.Draw(image)
    for y in range(height):
        mix = y / max(height - 1, 1)
        def blend(a: str, b: str) -> int:
            return round(int(a, 16) * (1 - mix) + int(b, 16) * mix)
        bg = palette['bg'].lstrip('#')
        bg2 = palette['bg2'].lstrip('#')
        draw.line((0, y, width, y), fill=(blend(bg[0:2], bg2[0:2]), blend(bg[2:4], bg2[2:4]), blend(bg[4:6], bg2[4:6])))

    title_font = _load_feed_font(78, bold=True)
    sub_font = _load_feed_font(30, bold=True)
    small_font = _load_feed_font(22, bold=True)
    body_font = _load_feed_font(24, bold=False)
    brand_font = _load_feed_font(32, bold=True)
    cta_font = _load_feed_font(26, bold=True)

    # Full-bleed layout with large headline, creator visual, and app handoff.
    draw.rectangle((0, 0, width, height), outline=None)
    draw.ellipse((760, -80, 1140, 300), fill=palette['accent'])
    draw.ellipse((-170, 720, 240, 1130), fill='#ffffff')
    draw.ellipse((800, -40, 1080, 240), fill=palette['bg'])

    x0, y0 = 64, 68
    for idx, line in enumerate(_wrap_feed_text(draw, headline, title_font, 620, max_lines=2)):
        draw.text((x0, y0 + idx * 88), line, fill='#ffffff', font=title_font)
    sub_y = y0 + 188
    for idx, line in enumerate(_wrap_feed_text(draw, subheadline, sub_font, 600, max_lines=2)):
        draw.text((x0, sub_y + idx * 40), line, fill='#e7f8ef', font=sub_font)

    if campaign:
        _draw_round_rect(draw, (64, 300, 430, 350), 25, '#ffffff')
        draw.text((88, 313), campaign[:28], fill=palette['ink'], font=small_font)

    card_x, card_y = 72, 410
    _draw_round_rect(draw, (card_x, card_y, card_x + 430, card_y + 332), 34, '#ffffff')
    draw.ellipse((card_x + 38, card_y + 54, card_x + 172, card_y + 188), fill=palette['soft'])
    draw.ellipse((card_x + 76, card_y + 78, card_x + 134, card_y + 136), fill=palette['bg'])
    draw.pieslice((card_x + 58, card_y + 122, card_x + 154, card_y + 224), 200, 340, fill=palette['bg'])
    draw.text((card_x + 204, card_y + 66), 'Local support', fill=palette['ink'], font=_load_feed_font(33, bold=True))
    draw.text((card_x + 204, card_y + 116), 'Fast review', fill='#475467', font=body_font)
    draw.text((card_x + 204, card_y + 154), 'Clear steps', fill='#475467', font=body_font)
    _draw_round_rect(draw, (card_x + 38, card_y + 238, card_x + 392, card_y + 294), 28, palette['cta'])
    draw.text((card_x + 118, card_y + 252), 'Apply with support', fill='#ffffff', font=cta_font)

    phone_x, phone_y = 610, 286
    _draw_round_rect(draw, (phone_x, phone_y, phone_x + 260, phone_y + 532), 46, '#0b1220')
    _draw_round_rect(draw, (phone_x + 22, phone_y + 54, phone_x + 238, phone_y + 476), 24, '#f8fafc')
    _draw_round_rect(draw, (phone_x + 50, phone_y + 92, phone_x + 210, phone_y + 138), 23, palette['bg'])
    draw.text((phone_x + 78, phone_y + 104), brand, fill='#ffffff', font=_load_feed_font(21, bold=True))
    for i, line_width in enumerate((158, 128, 176)):
        _draw_round_rect(draw, (phone_x + 52, phone_y + 180 + i * 52, phone_x + 52 + line_width, phone_y + 204 + i * 52), 12, '#cbd5e1')
    _draw_round_rect(draw, (phone_x + 48, phone_y + 372, phone_x + 212, phone_y + 426), 27, palette['accent'])
    draw.text((phone_x + 86, phone_y + 386), 'Join', fill=palette['ink'], font=cta_font)

    _draw_round_rect(draw, (64, 830, 960, 938), 38, '#ffffff')
    draw.text((96, 860), brand, fill=palette['ink'], font=brand_font)
    angle_text = angle if angle and angle != subheadline and not _contains_cjk(angle) else 'Creator onboarding, local guidance, clear application path'
    for idx, line in enumerate(_wrap_feed_text(draw, angle_text, body_font, 430, max_lines=2)):
        draw.text((250, 852 + idx * 34), line, fill='#334155', font=body_font)
    _draw_round_rect(draw, (740, 852, 922, 910), 29, palette['cta'])
    draw.text((786, 868), 'Start', fill='#ffffff', font=cta_font)

    output_dir.mkdir(parents=True, exist_ok=True)
    request_id = f'creative_req_{stable_id(prompt.prompt_hash, brief.campaign, brief.ad_group, brief.ad)}'
    image_bytes_hint = stable_id(prompt.prompt_hash, brief.country, brief.project, brief.campaign, brief.ad_group, brief.ad, 'local_png_v1')
    image_id = f'feed_img_{stable_id(request_id, image_bytes_hint)}'
    image_path = output_dir / f'{image_id}.png'
    image.save(image_path, format='PNG', optimize=True)
    created_at = utc_now()
    return GeneratedCreativeImage(
        image_id=image_id,
        request_id=request_id,
        surface=prompt.surface,
        image_size=f'{prompt.width}x{prompt.height}',
        market=prompt.market,
        brand=prompt.brand,
        image_ref=str(image_path),
        thumbnail_ref=str(image_path),
        prompt_hash=prompt.prompt_hash,
        risk_status=prompt.risk_status,
        risk_tags=prompt.risk_tags,
        review_status=prompt.review_status,
        provider=PROVIDER_LOCAL_PRODUCTION_PNG,
        created_at=created_at,
        metadata={
            'full_bleed': True,
            'no_white_margin': True,
            'surface_label': '信息流广告图',
            'width': prompt.width,
            'height': prompt.height,
            'content_type': 'image/png',
            'local_production': True,
            'downloadable': True,
        },
    )


def _provider_response_content_type(response: Any) -> str:
    headers = getattr(response, 'headers', {}) or {}
    if isinstance(headers, dict):
        content_type = headers.get('content-type') or headers.get('Content-Type') or ''
    else:
        content_type = getattr(headers, 'get', lambda _key, _default='': '')('content-type', '')
    return str(content_type or '').split(';', 1)[0].strip().lower()


def _decode_provider_base64(value: Any) -> bytes:
    text = str(value or '').strip()
    if ',' in text and text.lower().startswith('data:image/'):
        text = text.split(',', 1)[1]
    return base64.b64decode(text, validate=False)


def _provider_json_image_candidates(body: Dict[str, Any]) -> List[Tuple[str, Any]]:
    candidates: List[Tuple[str, Any]] = []
    for key in ('image_base64', 'b64_json', 'image', 'base64'):
        if body.get(key):
            candidates.append(('base64', body.get(key)))
    data = body.get('data')
    if isinstance(data, list) and data:
        first = data[0] if isinstance(data[0], dict) else {}
        for key in ('b64_json', 'image_base64', 'base64', 'image'):
            if first.get(key):
                candidates.append(('base64', first.get(key)))
        if first.get('url'):
            candidates.append(('url', first.get('url')))
    if body.get('url'):
        candidates.append(('url', body.get('url')))
    if body.get('image_url'):
        candidates.append(('url', body.get('image_url')))
    return candidates


def _provider_download_image(session: Any, url: str, timeout_seconds: int) -> Tuple[bytes, str]:
    if session is None or not hasattr(session, 'get'):
        return b'', ''
    response = session.get(url, timeout=timeout_seconds)
    status_code = int(getattr(response, 'status_code', 200) or 200)
    if status_code >= 400:
        return b'', ''
    return bytes(getattr(response, 'content', b'') or b''), _provider_response_content_type(response)


def _extract_provider_image_bytes(response: Any, session: Any, timeout_seconds: int) -> Tuple[bytes, str]:
    content_type = _provider_response_content_type(response)
    if content_type in IMAGE_PROVIDER_BINARY_TYPES:
        return bytes(getattr(response, 'content', b'') or b''), content_type
    body: Any = None
    try:
        body = response.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return b'', ''
    for kind, value in _provider_json_image_candidates(body):
        if kind == 'base64':
            try:
                return _decode_provider_base64(value), 'image/png'
            except Exception:
                continue
        if kind == 'url':
            image_bytes, downloaded_type = _provider_download_image(session, str(value or ''), timeout_seconds)
            if image_bytes:
                return image_bytes, downloaded_type or 'image/png'
    return b'', ''


def generate_external_feed_image(
    prompt: CreativeImagePrompt,
    brief: CreativeImageGenerationBrief,
    config: ExternalImageProviderConfig,
    *,
    output_dir: Path = DEFAULT_IMAGE_OUTPUT_DIR,
) -> Optional[GeneratedCreativeImage]:
    if prompt.risk_status == 'blocked':
        return None
    readiness = external_image_provider_readiness(config)
    if not readiness['ready']:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    provider = str(config.provider or 'external').strip().lower() or 'external'
    payload = {
        'prompt': prompt.prompt,
        'negative_prompt': prompt.negative_prompt,
        'width': prompt.width,
        'height': prompt.height,
        'size': f'{prompt.width}x{prompt.height}',
        'surface': prompt.surface,
        'surface_label': '信息流广告图',
        'brand': prompt.brand,
        'market': prompt.market,
        'required_components': prompt.required_components,
        'compliance_notes': prompt.compliance_notes,
        'metadata': {
            'full_bleed': True,
            'no_white_margin': True,
            'source': 'ad_dashboard',
            'country': brief.country,
            'project': brief.project,
            'campaign': brief.campaign,
            'ad_group': brief.ad_group,
            'ad': brief.ad,
            'objective': brief.objective,
        },
    }
    headers = {
        'Authorization': f'Bearer {config.api_key}',
        'Content-Type': 'application/json',
    }
    try:
        response = config.session.post(
            config.url,
            json=payload,
            headers=headers,
            timeout=max(1, int(config.timeout_seconds or 90)),
        )
        status_code = int(getattr(response, 'status_code', 200) or 200)
        if status_code >= 400:
            return None
        image_bytes, content_type = _extract_provider_image_bytes(response, config.session, max(1, int(config.timeout_seconds or 90)))
    except Exception:
        return None
    if not image_bytes:
        return None
    suffix = IMAGE_PROVIDER_BINARY_TYPES.get(content_type or 'image/png', '.png')
    request_id = f'creative_req_{stable_id(prompt.prompt_hash, brief.campaign, brief.ad_group, brief.ad)}'
    image_id = f'feed_img_{stable_id(request_id, prompt.prompt_hash, provider, hashlib.sha256(image_bytes).hexdigest())}'
    image_path = output_dir / f'{image_id}{suffix}'
    image_path.write_bytes(image_bytes)
    created_at = utc_now()
    return GeneratedCreativeImage(
        image_id=image_id,
        request_id=request_id,
        surface=prompt.surface,
        image_size=f'{prompt.width}x{prompt.height}',
        market=prompt.market,
        brand=prompt.brand,
        image_ref=str(image_path),
        thumbnail_ref=str(image_path),
        prompt_hash=prompt.prompt_hash,
        risk_status=prompt.risk_status,
        risk_tags=prompt.risk_tags,
        review_status=prompt.review_status,
        provider=provider,
        created_at=created_at,
        metadata={
            'full_bleed': True,
            'no_white_margin': True,
            'surface_label': '信息流广告图',
            'width': prompt.width,
            'height': prompt.height,
            'content_type': content_type or 'image/png',
            'external_provider': True,
        },
    )


def fixture_svg(prompt: CreativeImagePrompt, brief: CreativeImageGenerationBrief) -> str:
    profile = SUPPORTED_BRAND_MARKETS.get(prompt.market, {})
    headline = str(profile.get('headline') or 'Start as creator')
    subheadline = str(profile.get('subheadline') or 'Local support and clear application flow')
    brand = prompt.brand or 'Brand'
    palette = {
        'TUGAO': ('#075985', '#22c55e', '#e0f2fe'),
        'Premiou': ('#7c2d12', '#f97316', '#ffedd5'),
        'Recompa': ('#3730a3', '#ec4899', '#eef2ff'),
    }.get(brand, ('#111827', '#2563eb', '#dbeafe'))
    primary, accent, soft = palette
    esc = html.escape
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="{primary}"/>
    <stop offset="100%" stop-color="{accent}"/>
  </linearGradient>
  <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
    <feDropShadow dx="0" dy="20" stdDeviation="24" flood-color="#0f172a" flood-opacity="0.28"/>
  </filter>
</defs>
<rect width="1024" height="1024" fill="url(#bg)"/>
<circle cx="850" cy="170" r="190" fill="{soft}" opacity="0.22"/>
<circle cx="80" cy="890" r="240" fill="#ffffff" opacity="0.14"/>
<g transform="translate(74 86)">
  <text x="0" y="70" fill="#ffffff" font-family="Arial, Helvetica, sans-serif" font-size="70" font-weight="800">{esc(headline)}</text>
  <text x="4" y="132" fill="#eaf2ff" font-family="Arial, Helvetica, sans-serif" font-size="30" font-weight="700">{esc(subheadline)}</text>
</g>
<g filter="url(#shadow)" transform="translate(598 250)">
  <rect x="0" y="0" width="260" height="520" rx="42" fill="#0f172a"/>
  <rect x="22" y="46" width="216" height="424" rx="22" fill="#f8fafc"/>
  <rect x="48" y="92" width="164" height="42" rx="21" fill="{accent}" opacity="0.92"/>
  <rect x="48" y="166" width="164" height="26" rx="13" fill="#cbd5e1"/>
  <rect x="48" y="212" width="120" height="26" rx="13" fill="#cbd5e1"/>
  <rect x="48" y="278" width="164" height="82" rx="18" fill="{soft}"/>
  <rect x="72" y="392" width="116" height="44" rx="22" fill="{primary}"/>
  <text x="130" y="421" fill="#ffffff" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="18" font-weight="800">Apply</text>
</g>
<g transform="translate(90 310)" filter="url(#shadow)">
  <circle cx="160" cy="165" r="124" fill="#fef3c7"/>
  <circle cx="160" cy="120" r="52" fill="#7c2d12"/>
  <path d="M72 302c22-82 154-112 220 0z" fill="#fff7ed"/>
  <path d="M74 322h248v132H74z" fill="#ffffff" opacity="0.94"/>
  <text x="198" y="382" fill="#111827" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="34" font-weight="800">Local support</text>
  <text x="198" y="430" fill="#475467" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="24" font-weight="700">Clear steps</text>
</g>
<g transform="translate(74 850)">
  <rect x="0" y="0" width="876" height="86" rx="34" fill="#ffffff" opacity="0.92"/>
  <text x="42" y="55" fill="{primary}" font-family="Arial, Helvetica, sans-serif" font-size="38" font-weight="900">{esc(brand)}</text>
  <text x="828" y="54" fill="#111827" text-anchor="end" font-family="Arial, Helvetica, sans-serif" font-size="26" font-weight="800">Creator guild support</text>
</g>
</svg>'''


def create_feed_image_generation(
    conn: sqlite3.Connection,
    brief: CreativeImageGenerationBrief,
    *,
    output_dir: Path = DEFAULT_IMAGE_OUTPUT_DIR,
    image_provider_config: Optional[ExternalImageProviderConfig] = None,
) -> Dict[str, Any]:
    prompt = build_feed_static_ad_prompt(brief)
    request_id = f'creative_req_{stable_id(prompt.prompt_hash, brief.country, brief.project, brief.campaign, brief.ad_group, brief.ad)}'
    provider_status = external_image_provider_readiness(image_provider_config)
    image = None
    local_provider_ready = bool(provider_status.get('mode') == PROVIDER_LOCAL_PRODUCTION_PNG and provider_status.get('ready'))
    provider_attempted = bool(image_provider_config and provider_status.get('ready'))
    if local_provider_ready:
        image = generate_local_production_feed_image(prompt, brief, output_dir=output_dir)
    elif provider_attempted and image_provider_config is not None:
        image = generate_external_feed_image(prompt, brief, image_provider_config, output_dir=output_dir)
    provider_fallback = provider_attempted and image is None and prompt.risk_status != 'blocked'
    if image is None:
        image = generate_fixture_feed_image(prompt, brief, output_dir=output_dir)
    if image is not None and image.request_id != request_id:
        image = GeneratedCreativeImage(**{**asdict(image), 'request_id': request_id})
    persist_generation_result(conn, request_id=request_id, brief=brief, prompt=prompt, image=image)
    return {
        'ok': prompt.risk_status != 'blocked',
        'schema_version': CREATIVE_IMAGE_GENERATION_SCHEMA_VERSION,
        'request_id': request_id,
        'surface': prompt.surface,
        'surface_label': '信息流广告图',
        'image_size': f'{prompt.width}x{prompt.height}',
        'width': prompt.width,
        'height': prompt.height,
        'market': prompt.market,
        'brand': prompt.brand,
        'prompt': prompt.prompt,
        'negative_prompt': prompt.negative_prompt,
        'required_components': prompt.required_components,
        'compliance_notes': prompt.compliance_notes,
        'risk_status': prompt.risk_status,
        'risk_tags': prompt.risk_tags,
        'review_status': prompt.review_status,
        'image_provider': {
            **provider_status,
            'attempted': provider_attempted,
            'fallback_to_fixture': provider_fallback,
        },
        'image': asdict(image) if image is not None else None,
    }


CHATGPT_PRO_RULES = {
    'surface': FEED_STATIC_AD_SURFACE,
    'image_size': '1024x1024',
    'full_bleed': True,
    'no_white_margin': True,
    'forbidden_outputs': ['商店图', '应用商店截图', '产品介绍页', '功能说明海报', '单纯 UI 展示图'],
    'forbidden_content': ['手机号', '邮箱', 'WhatsApp 明文号码', '身份证', '银行卡', '固定收益承诺', '保证赚钱', '现金雨', '品牌混用'],
    'binding': {
        'replacement': '替换原广告通过原 ad_id 的 creative_id 变化识别。',
        'new_test': '新增实验通过广告名包含 experiment_code 识别。',
        'manual_upload_optional': True,
    },
}


def _json_load(value: Any, default: Any) -> Any:
    try:
        if value in (None, ''):
            return default
        parsed = json.loads(value) if isinstance(value, str) else value
        return parsed if parsed is not None else default
    except Exception:
        return default


def _job_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        'job_id': row['job_id'],
        'job_type': row['job_type'],
        'provider_mode': row['provider_mode'],
        'status': row['status'],
        'country': row['country'],
        'project': row['project'],
        'brand_display_name': row['brand_display_name'],
        'experiment_type': row['experiment_type'],
        'experiment_id': row['experiment_id'],
        'experiment_code': row['experiment_code'],
        'source_ad_ids': _json_load(row['source_ad_ids_json'], []),
        'source_creative_ids': _json_load(row['source_creative_ids_json'], []),
        'source_asset_ids': _json_load(row['source_asset_ids_json'], []),
        'creative_diagnosis_id': row['creative_diagnosis_id'],
        'recommendation_id': row['recommendation_id'],
        'metrics_snapshot': _json_load(row['metrics_snapshot_json'], {}),
        'rules': _json_load(row['rules_json'], {}),
        'material_refs': _json_load(row['material_refs_json'], {}),
        'signed_thumbnail_urls': _json_load(row['signed_thumbnail_urls_json'], []),
        'analysis': _json_load(row['analysis_json'], {}),
        'generation_plan': _json_load(row['generation_plan_json'], {}),
        'created_by': row['created_by'],
        'claimed_by': row['claimed_by'],
        'created_at': row['created_at'],
        'claimed_at': row['claimed_at'],
        'completed_at': row['completed_at'],
        'expires_at': row['expires_at'],
        'error_code': row['error_code'],
        'error_message': safe_provider_error(row['error_message']),
        'manual_image_upload_required': False,
        'external_write_performed': False,
    }


def _attach_generation_origins(
    conn: sqlite3.Connection,
    jobs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    job_ids = [str(job.get('job_id') or '') for job in jobs if job.get('job_id')]
    latest_tasks: Dict[str, sqlite3.Row] = {}
    if job_ids:
        placeholders = ','.join('?' for _ in job_ids)
        rows = conn.execute(
            f"""
            SELECT job_id, task_id, payload_json, created_at
            FROM creative_generation_tasks
            WHERE job_id IN ({placeholders})
            ORDER BY created_at DESC, task_id DESC
            """,
            job_ids,
        ).fetchall()
        for row in rows:
            latest_tasks.setdefault(str(row['job_id'] or ''), row)
    for job in jobs:
        task = latest_tasks.get(str(job.get('job_id') or ''))
        task_payload = _json_load(task['payload_json'], {}) if task else {}
        actor = str(task_payload.get('created_by') or job.get('created_by') or '').strip()
        normalized_actor = actor.lower()
        is_system = normalized_actor in {'growth-autopilot', 'internal-system', 'system'} or 'autopilot' in normalized_actor
        material = dict(job.get('material_refs') or {})
        meta_names = dict(material.get('meta_names') or {})
        order_name = str(meta_names.get('campaign') or material.get('campaign') or material.get('launch_id') or '').strip()
        job['generation_origin'] = {
            'type': 'system' if is_system else 'operator',
            'label': '系统自动生成' if is_system else '人工触发',
            'created_by': actor,
            'generated_at': str(task['created_at'] or '') if task else str(job.get('created_at') or ''),
            'task_id': str(task['task_id'] or '') if task else '',
            'order_name': order_name,
        }
    return jobs


def chatgpt_pro_workbench_status(conn: sqlite3.Connection, *, enabled: bool = False) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM creative_pro_work_queue WHERE status IN ('pending', 'claimed')",
    ).fetchone()['n']
    return {
        'enabled': bool(enabled),
        'configured': bool(enabled),
        'status': 'ready' if enabled else 'disabled',
        'message_cn': 'ChatGPT Pro 创意工作台可用，可创建人工处理任务' if enabled else 'ChatGPT Pro 创意工作台未启用',
        'pending_jobs': int(pending or 0),
        'actions_api_ready': bool(enabled),
        'manual_upload_ready': bool(enabled),
    }


def create_chatgpt_pro_job(
    conn: sqlite3.Connection,
    *,
    brief: CreativeImageGenerationBrief,
    payload: Optional[Dict[str, Any]] = None,
    created_by: str = '',
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    body = dict(payload or {})
    prompt = build_feed_static_ad_prompt(brief)
    if prompt.risk_status == 'blocked':
        return {
            'ok': False,
            'provider_mode': PROVIDER_CHATGPT_PRO_MANUAL,
            'risk_status': prompt.risk_status,
            'risk_tags': prompt.risk_tags,
            'detail': 'creative_job_blocked_by_market',
        }
    task = dict(body.get('production_task') or {})
    requested_mode = body.get('experiment_mode') or task.get('mode') or task.get('experiment_mode')
    mode = normalize_experiment_mode(requested_mode or EXPERIMENT_MODE_NEW_TEST)
    recommendation_id = str(body.get('recommendation_id') or task.get('recommendation_id') or '').strip()
    source_ad_id = str(body.get('source_ad_id') or task.get('source_ad_id') or task.get('ad_id') or '').strip()
    source_creative_id = str(body.get('source_creative_id') or task.get('source_creative_id') or task.get('creative_id') or '').strip()
    source_campaign_id = str(body.get('source_campaign_id') or task.get('source_campaign_id') or task.get('campaign_id') or '').strip()
    source_adset_id = str(body.get('source_adset_id') or task.get('source_adset_id') or task.get('adset_id') or '').strip()
    if recommendation_id and source_ad_id:
        existing = conn.execute(
            """
            SELECT job_id, experiment_id
            FROM creative_pro_work_queue
            WHERE recommendation_id=? AND source_ad_ids_json=? AND status<>'deleted'
            ORDER BY created_at DESC, job_id DESC
            LIMIT 1
            """,
            (recommendation_id, json.dumps([source_ad_id], ensure_ascii=False)),
        ).fetchone()
        if existing:
            return {
                'ok': True,
                'schema_version': CREATIVE_IMAGE_GENERATION_SCHEMA_VERSION,
                'provider_mode': PROVIDER_CHATGPT_PRO_MANUAL,
                'deduplicated': True,
                'external_write_performed': False,
                'job': get_chatgpt_pro_job(conn, str(existing['job_id'])),
                'experiment': get_creative_experiment(conn, str(existing['experiment_id'])),
                'image': None,
            }
    metrics = dict(brief.source_performance or body.get('metrics_snapshot') or task.get('metrics_snapshot') or {})
    source_preview_url = str(body.get('source_preview_url') or task.get('source_preview_url') or task.get('creative_preview_url') or brief.source_preview_url or '').strip()
    source_preview_asset_id = str(body.get('source_preview_asset_id') or task.get('source_preview_asset_id') or task.get('creative_preview_asset_id') or brief.source_preview_asset_id or '').strip()
    source_preview_title = str(body.get('source_preview_title') or task.get('source_preview_title') or task.get('creative_preview_title') or brief.source_preview_title or '').strip()
    source_image_signed_url = str(body.get('source_image_signed_url') or task.get('source_image_signed_url') or body.get('source_image_url') or task.get('source_image_url') or '').strip()
    source_image_hash = str(body.get('source_image_hash') or task.get('source_image_hash') or body.get('image_hash') or task.get('image_hash') or '').strip()
    source_image_id = str(body.get('source_image_id') or task.get('source_image_id') or source_preview_asset_id).strip()
    source_diagnosis = str(body.get('source_diagnosis') or task.get('source_diagnosis') or task.get('diagnosis') or brief.source_diagnosis or '').strip()
    revision_goal = str(body.get('revision_goal') or task.get('revision_goal') or task.get('action') or brief.revision_goal or '').strip()
    source_reference = resolve_true_source_image_reference(conn, {
        'source_ad_id': source_ad_id,
        'source_creative_id': source_creative_id,
        'source_preview_url': source_preview_url,
        'source_preview_asset_id': source_preview_asset_id,
        'source_preview_title': source_preview_title,
        'source_image_signed_url': source_image_signed_url,
        'source_image_hash': source_image_hash,
        'source_image_id': source_image_id,
        'asset_id': source_preview_asset_id,
        'ad_id': source_ad_id,
        'creative_id': source_creative_id,
        'title': source_preview_title or brief.ad,
    })
    source_image_signed_url = str(source_reference.get('source_image_signed_url') or '').strip()
    source_image_hash = str(source_reference.get('source_image_hash') or '').strip()
    source_image_id = str(source_reference.get('source_image_id') or source_image_id).strip()
    source_preview_url = str(source_reference.get('source_preview_url') or source_preview_url).strip()
    source_preview_asset_id = str(source_reference.get('source_preview_asset_id') or source_preview_asset_id).strip()
    source_preview_title = str(source_reference.get('source_preview_title') or source_preview_title).strip()
    source_image_width = int(source_reference.get('source_image_width') or body.get('source_image_width') or task.get('source_image_width') or 0)
    source_image_height = int(source_reference.get('source_image_height') or body.get('source_image_height') or task.get('source_image_height') or 0)
    source_image_quality = str(source_reference.get('source_image_quality') or body.get('source_image_quality') or task.get('source_image_quality') or '').strip()
    direction = creative_direction_profile(brief.core_offer)
    if mode == EXPERIMENT_MODE_REPLACEMENT and direction.get('key') == CREATIVE_DIRECTION_SAFE_COMPLIANCE:
        return {
            'ok': False,
            'provider_mode': PROVIDER_CHATGPT_PRO_MANUAL,
            'detail': 'safe_compliance_requires_new_image',
            'message_cn': '安全合规方向必须使用新图生成，不能基于旧广告局部重绘，以免保留被拒素材中的收益、就业或仿冒信号。',
            'blocked_reasons': ['rejected_creative_reuse_not_allowed'],
        }
    if mode == EXPERIMENT_MODE_REPLACEMENT:
        gate_allowed, gate_reasons = old_image_revision_gate(task)
        if not gate_allowed:
            return {
                'ok': False,
                'provider_mode': PROVIDER_CHATGPT_PRO_MANUAL,
                'detail': 'old_image_revision_not_allowed',
                'message_cn': '该建议属于样本不足、数据缺失或后链路异常，不允许创建旧图补强任务。',
                'blocked_reasons': gate_reasons,
            }
    source_image_ok, source_image_reasons = validate_true_source_image_reference(source_reference)
    if mode == EXPERIMENT_MODE_REPLACEMENT and not source_image_ok:
        return {
            'ok': False,
            'provider_mode': PROVIDER_CHATGPT_PRO_MANUAL,
            'detail': 'source_image_not_synced',
            'message_cn': '旧图修改需要先同步到可下载的真实原图；当前只找到预览或广告定位，已阻断生成，避免误按方向重绘。',
            'source_image_resolution': {
                'status': str(source_reference.get('source_image_resolution_status') or 'source_image_not_synced'),
                'source_ad_id': source_ad_id,
                'source_creative_id': source_creative_id,
                'source_preview_asset_id': source_preview_asset_id,
                'source_preview_url': source_preview_url,
                'blocked_reasons': source_image_reasons,
            },
        }
    revision_plan = build_old_image_revision_plan(
        source_diagnosis=source_diagnosis,
        revision_goal=revision_goal,
        creative_direction=direction,
        task=task,
    ) if mode == EXPERIMENT_MODE_REPLACEMENT else {}
    if mode == EXPERIMENT_MODE_REPLACEMENT:
        revision_plan_ok, revision_plan_errors = validate_old_image_revision_plan(revision_plan)
        if not revision_plan_ok:
            return {
                'ok': False,
                'provider_mode': PROVIDER_CHATGPT_PRO_MANUAL,
                'detail': 'old_image_revision_plan_invalid',
                'message_cn': '旧图补强缺少可执行的局部修改方案，已阻断生成。',
                'blocked_reasons': revision_plan_errors,
            }
    launch_id = str(task.get('launch_id') or body.get('launch_id') or '').strip()
    growth_experiment_id = str(task.get('growth_experiment_id') or body.get('growth_experiment_id') or '').strip()
    launch_name = next_launch_creative_name(
        conn, launch_id=launch_id, growth_experiment_id=growth_experiment_id,
    )
    if launch_name:
        canonical_ad_name = str(launch_name['ad_name'])
        body['ad'] = canonical_ad_name
        body_names = dict(body.get('meta_names') or task.get('meta_names') or {})
        body_names['ad'] = canonical_ad_name
        body['meta_names'] = body_names
        task['ad'] = canonical_ad_name
        task['meta_names'] = body_names
    experiment = approve_creative_experiment_generation(
        conn,
        suggestion_id=recommendation_id or f'chatgpt_pro_{stable_id(brief.country, brief.campaign, brief.ad, utc_now())}',
        generated_image_id='',
        experiment_mode=mode,
        source_ad_id=source_ad_id,
        source_creative_id=source_creative_id,
        source_campaign_id=source_campaign_id,
        source_adset_id=source_adset_id,
        country=brief.country,
        created_by=created_by,
        payload={
            **body,
            'provider_mode': PROVIDER_CHATGPT_PRO_MANUAL,
            'manual_upload_optional': True,
            'external_write_performed': False,
        },
    )
    job_id = f'creative_pro_job_{stable_id(experiment["experiment_id"], recommendation_id, brief.country, brief.ad)}'
    market, profile = normalize_market(brief.country, brief.project)
    now = utc_now()
    material_refs = {
        'target_app': _normalize_target_app(body.get('target_app') or task.get('target_app') or body.get('app_target') or task.get('app_target')),
        'source_ad_id': source_ad_id,
        'source_creative_id': source_creative_id,
        'source_campaign_id': source_campaign_id,
        'source_adset_id': source_adset_id,
        'source_preview_url': source_preview_url,
        'source_preview_asset_id': source_preview_asset_id,
        'source_preview_title': source_preview_title,
        'source_image_signed_url': source_image_signed_url,
        'source_image_hash': source_image_hash,
        'source_image_id': source_image_id,
        'source_image_width': source_image_width,
        'source_image_height': source_image_height,
        'source_image_quality': source_image_quality,
        'source_image_resolution_status': str(source_reference.get('source_image_resolution_status') or ''),
        'source_diagnosis': source_diagnosis,
        'revision_goal': revision_goal,
        'audience': str(brief.audience or '').strip(),
        'audience_strategy': str(task.get('audience_strategy') or body.get('audience_strategy') or 'BROAD').strip(),
        'audience_strategy_label': str(task.get('audience_strategy_label') or body.get('audience_strategy_label') or '广泛受众').strip(),
        'base_targeting': dict(task.get('base_targeting') or body.get('base_targeting') or {}),
        'creative_angle': str(brief.core_offer or '').strip(),
        'creative_direction': direction.get('key') or creative_direction_key(brief.core_offer),
        'account_id': str(body.get('account_id') or task.get('account_id') or '').strip(),
        'account_name': str(body.get('account_name') or task.get('account_name') or task.get('account') or '').strip(),
        'ad': str(body.get('ad') or brief.ad),
        'ad_group': brief.ad_group,
        'campaign': brief.campaign,
        'growth_experiment_id': growth_experiment_id,
        'launch_id': launch_id,
        'page_id': str(task.get('page_id') or body.get('page_id') or '').strip(),
        'targeting': dict(task.get('targeting') or body.get('targeting') or {}),
        'meta_rejection': dict(task.get('meta_rejection') or body.get('meta_rejection') or {}),
        'meta_names': dict(task.get('meta_names') or body.get('meta_names') or {}),
        'initial_daily_budget': task.get('initial_daily_budget') or body.get('initial_daily_budget') or 0,
        'source': str(task.get('source') or '').strip(),
        'auto_rebuild_on_approval': task.get('auto_rebuild_on_approval') is True,
        'rebuild_initial_status': str(task.get('rebuild_initial_status') or '').strip().upper(),
        'rebuild_authorized_at': str(task.get('rebuild_authorized_at') or '').strip(),
        'rebuild_batch_id': str(task.get('rebuild_batch_id') or '').strip(),
        'rebuild_entry_point': str(task.get('rebuild_entry_point') or '').strip(),
    }
    if launch_name:
        material_refs['creative_name_version'] = 'launch_date_sequence_v1'
        material_refs['creative_retry'] = int(launch_name['retry'])
    reference_asset = {
        'url': source_preview_url,
        'asset_id': source_preview_asset_id,
        'title': source_preview_title,
        'source_image_signed_url': source_image_signed_url,
        'source_image_hash': source_image_hash,
        'source_image_id': source_image_id,
        'source_image_width': source_image_width,
        'source_image_height': source_image_height,
        'source_image_quality': source_image_quality,
        'source_image_resolution_status': str(source_reference.get('source_image_resolution_status') or ''),
    }
    creative_revision_brief = {
        'mode': mode,
        'goal': revision_goal,
        'diagnosis': source_diagnosis,
        'preserve': list(revision_plan.get('preserve') or [
            'brand identity',
            'target market language',
            'product/app category',
            'useful proven visual motifs from the original ad',
        ]),
        'modify': list(revision_plan.get('modify') or [
            'visual hierarchy',
            'headline emphasis',
            'proof module',
            'trust cues',
            'conversion handoff to app or IM',
        ]),
    }
    source_image_structure = source_image_structure_from_reference({**reference_asset, **material_refs})
    prompt_package = build_creative_prompt_package(
        job_id=job_id,
        experiment_id=experiment['experiment_id'],
        experiment_code=experiment['experiment_code'],
        market=market,
        brand=profile.get('brand') or prompt.brand,
        country=brief.country,
        language_hint=profile.get('language_hint') or '',
        image_size='1024x1024',
        creative_direction=direction,
        prompt=prompt.prompt,
        negative_prompt=prompt.negative_prompt,
        source_reference={**reference_asset, **material_refs},
        preserve=creative_revision_brief['preserve'],
        modify=creative_revision_brief['modify'],
        revision_plan=revision_plan,
        source_image_structure=source_image_structure,
        candidate_count=1,
    )
    rules = {
        **CHATGPT_PRO_RULES,
        'prompt': prompt.prompt,
        'negative_prompt': prompt.negative_prompt,
        'required_components': prompt.required_components,
        'compliance_notes': prompt.compliance_notes,
        'reference_asset': reference_asset,
        'creative_revision_brief': creative_revision_brief,
        'revision_plan': revision_plan,
        'source_image_structure': source_image_structure,
        'prompt_package': prompt_package,
        'generation_mode': prompt_package['generation_mode'],
        'creative_direction': prompt_package['creative_direction'],
        'creative_angle': str(brief.core_offer or '').strip(),
        'audience': str(brief.audience or '').strip(),
        'visible_claim_policy': prompt_package['visible_claim_policy'],
        'brand_visual_guidelines': prompt_package['brand_visual_guidelines'],
        'target_app': material_refs['target_app'],
        'currency_threshold_version': CURRENCY_THRESHOLD_VERSION,
        'binding_instruction_cn': experiment['binding_instruction_cn'],
    }
    conn.execute(
        """
        INSERT OR REPLACE INTO creative_pro_work_queue
        (job_id, job_type, provider_mode, status, country, project, brand_display_name, experiment_type,
         experiment_id, experiment_code, source_ad_ids_json, source_creative_ids_json, source_asset_ids_json,
         creative_diagnosis_id, recommendation_id, metrics_snapshot_json, rules_json, material_refs_json,
         signed_thumbnail_urls_json, created_by, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            'generation',
            PROVIDER_CHATGPT_PRO_MANUAL,
            'pending',
            brief.country,
            brief.project,
            profile.get('brand') or prompt.brand,
            str(body.get('experiment_type') or task.get('experiment_type') or ('WINNER_EXTENSION' if mode == EXPERIMENT_MODE_NEW_TEST else 'FUNNEL_REPAIR')),
            experiment['experiment_id'],
            experiment['experiment_code'],
            json.dumps([source_ad_id] if source_ad_id else [], ensure_ascii=False),
            json.dumps([source_creative_id] if source_creative_id else [], ensure_ascii=False),
            json.dumps(list(body.get('source_asset_ids') or task.get('source_asset_ids') or []), ensure_ascii=False),
            str(body.get('creative_diagnosis_id') or task.get('creative_diagnosis_id') or ''),
            recommendation_id,
            json.dumps(metrics, ensure_ascii=False, sort_keys=True),
            json.dumps(rules, ensure_ascii=False, sort_keys=True),
            json.dumps(material_refs, ensure_ascii=False, sort_keys=True),
            json.dumps([item for item in [source_preview_url, *(body.get('signed_thumbnail_urls') or [])] if item], ensure_ascii=False),
            created_by,
            now,
            body.get('expires_at') or '',
        ),
    )
    conn.commit()
    return {
        'ok': True,
        'schema_version': CREATIVE_IMAGE_GENERATION_SCHEMA_VERSION,
        'provider_mode': PROVIDER_CHATGPT_PRO_MANUAL,
        'surface': FEED_STATIC_AD_SURFACE,
        'surface_label': '信息流广告图',
        'image_size': '1024x1024',
        'width': 1024,
        'height': 1024,
        'market': market,
        'brand': prompt.brand,
        'prompt': prompt.prompt,
        'negative_prompt': prompt.negative_prompt,
        'required_components': prompt.required_components,
        'compliance_notes': prompt.compliance_notes,
        'risk_status': prompt.risk_status,
        'risk_tags': prompt.risk_tags,
        'review_status': 'pending_chatgpt_pro',
        'image_provider': {
            'provider': PROVIDER_CHATGPT_PRO_MANUAL,
            'mode': PROVIDER_CHATGPT_PRO_MANUAL,
            'ready': True,
            'manual_workbench': True,
            'external_write_performed': False,
        },
        'external_write_performed': False,
        'job': get_chatgpt_pro_job(conn, job_id),
        'experiment': experiment,
        'image': None,
    }


def create_hermes_image2_generation_job(
    conn: sqlite3.Connection,
    *,
    brief: CreativeImageGenerationBrief,
    payload: Optional[Dict[str, Any]] = None,
    created_by: str = '',
    image_size: str = '1024x1024',
    candidate_count: int = 1,
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    body = dict(payload or {})
    image_size, _ = _normalize_hermes_image_size(body.get('image_size') or image_size)
    job_result = create_chatgpt_pro_job(conn, brief=brief, payload=body, created_by=created_by)
    if not job_result.get('ok'):
        return {
            **job_result,
            'provider_mode': PROVIDER_HERMES_IMAGE2_AGENT,
            'image_provider': {
                'provider': PROVIDER_HERMES_IMAGE2_AGENT,
                'mode': PROVIDER_HERMES_IMAGE2_AGENT,
                'ready': False,
                'external_write_performed': False,
            },
        }
    job = dict(job_result.get('job') or {})
    rules = dict(job.get('rules') or {})
    headline = str(body.get('image_headline') or rules.get('headline') or '').strip()
    if not headline:
        market, profile = normalize_market(brief.country, brief.project)
        headline = str(profile.get('headline') or 'Comece em casa').strip()
    plan = {
        'surface': FEED_STATIC_AD_SURFACE,
        'image_size': image_size,
        'image_headline': headline,
        'prompt': str(rules.get('prompt') or job_result.get('prompt') or '').strip(),
        'negative_prompt': _sanitize_generation_negative_prompt(rules.get('negative_prompt') or job_result.get('negative_prompt') or ''),
        'required_components': list(rules.get('required_components') or job_result.get('required_components') or []),
        'provider_mode': PROVIDER_HERMES_IMAGE2_AGENT,
        'auto_created': True,
        'external_write_performed': False,
    }
    updated_job = update_chatgpt_pro_job_generation_plan(conn, str(job['job_id']), plan)
    task_result = start_hermes_image2_generation_task(
        conn,
        job_id=str(job['job_id']),
        image_size=image_size,
        candidate_count=int(body.get('candidate_count') or candidate_count or 1),
        max_attempts=int(body.get('max_attempts') or 3),
        created_by=created_by,
        force_regenerate=bool(body.get('force_regenerate')),
    )
    task = dict(task_result.get('task') or {})
    latest_job = dict(task_result.get('job') or updated_job)
    return {
        **job_result,
        'ok': True,
        'provider_mode': PROVIDER_HERMES_IMAGE2_AGENT,
        'review_status': 'queued_for_hermes',
        'image_provider': {
            'provider': PROVIDER_HERMES_IMAGE2_AGENT,
            'mode': PROVIDER_HERMES_IMAGE2_AGENT,
            'ready': True,
            'external_write_performed': False,
        },
        'job': latest_job,
        'task': task,
        'generation_task': task,
        'generation_plan': latest_job.get('generation_plan') or {},
        'image': None,
        'external_write_performed': False,
    }


def list_chatgpt_pro_jobs(conn: sqlite3.Connection, *, status: str = '', limit: int = 20, target_app: str = 'all') -> List[Dict[str, Any]]:
    ensure_creative_image_generation_tables(conn)
    # The workbench is a decision surface, so do not return a stale "generating"
    # card after its worker lease has already expired.  This is deliberately a
    # cheap no-op query in the normal path and also gives the UI an independent
    # recovery path when the worker's failure callback was lost.
    reconcile_stale_hermes_image2_generation_tasks(conn)
    normalized_status = str(status or '').strip().lower()
    normalized_target_app = _normalize_target_app(target_app)
    requested_limit = max(1, min(int(limit or 20), 100))
    sql = "SELECT * FROM creative_pro_work_queue"
    params: List[Any] = []
    if normalized_status:
        sql += " WHERE status = ?"
        params.append(normalized_status)
    else:
        sql += " WHERE status NOT IN ('deleted', 'completed')"
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(requested_limit if normalized_target_app in {'', 'all'} else min(requested_limit * 5, 300))
    jobs = []
    for row in conn.execute(sql, params).fetchall():
        job = _job_from_row(row)
        job['target_app'] = creative_job_target_app(job)
        jobs.append(job)
    if normalized_target_app not in {'', 'all'}:
        jobs = [job for job in jobs if job.get('target_app') == normalized_target_app]
    return _attach_generation_origins(conn, jobs[:requested_limit])


def get_chatgpt_pro_job(conn: sqlite3.Connection, job_id: str) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    row = conn.execute("SELECT * FROM creative_pro_work_queue WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        raise ValueError('creative_pro_job_not_found')
    return _attach_generation_origins(conn, [_job_from_row(row)])[0]


def claim_next_chatgpt_pro_job(conn: sqlite3.Connection, *, claimed_by: str = 'chatgpt_pro', claim: bool = True) -> Optional[Dict[str, Any]]:
    ensure_creative_image_generation_tables(conn)
    row = conn.execute(
        "SELECT * FROM creative_pro_work_queue WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1",
    ).fetchone()
    if not row:
        return None
    job = _job_from_row(row)
    if claim:
        now = utc_now()
        conn.execute(
            "UPDATE creative_pro_work_queue SET status = 'claimed', claimed_by = ?, claimed_at = ? WHERE job_id = ?",
            (claimed_by, now, job['job_id']),
        )
        conn.commit()
        job = get_chatgpt_pro_job(conn, job['job_id'])
    return job


def update_chatgpt_pro_job_analysis(conn: sqlite3.Connection, job_id: str, analysis: Dict[str, Any]) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    get_chatgpt_pro_job(conn, job_id)
    conn.execute(
        "UPDATE creative_pro_work_queue SET analysis_json = ?, status = CASE WHEN status = 'pending' THEN 'claimed' ELSE status END WHERE job_id = ?",
        (json.dumps(dict(analysis or {}), ensure_ascii=False, sort_keys=True), job_id),
    )
    conn.commit()
    return get_chatgpt_pro_job(conn, job_id)


def validate_chatgpt_generation_plan(plan: Dict[str, Any]) -> Tuple[bool, List[str]]:
    positive_plan = {
        key: value
        for key, value in dict(plan or {}).items()
        if str(key) not in {'negative_prompt', 'negativePrompt'}
    }
    text = json.dumps(positive_plan, ensure_ascii=False)
    missing = [key for key in ['image_headline', 'prompt'] if not str((plan or {}).get(key) or '').strip()]
    image_size = str((plan or {}).get('image_size') or '1024x1024').strip()
    if image_size and image_size not in {'1024x1024', '512x512'}:
        missing.append('invalid_image_size')
    status, tags = review_prompt_safety(text)
    if any(pattern.search(text) for pattern in WRONG_SURFACE_PATTERNS):
        tags.append('wrong_surface')
    if re.search(r'\b(male|man|boy|husband|boyfriend)\b|男性|男生|男人|男主角', text, re.I):
        tags.append('male_main_character')
    hard_block_tags = {'wrong_surface', 'male_main_character'}
    return not missing and status != 'blocked' and not (hard_block_tags & set(tags)), [*missing, *tags]


def _chatgpt_pro_generation_request_id(job: Dict[str, Any]) -> str:
    return f'creative_req_{stable_id(job.get("job_id"), job.get("experiment_id"), job.get("experiment_code"))}'


def update_chatgpt_pro_job_generation_plan(conn: sqlite3.Connection, job_id: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    job = get_chatgpt_pro_job(conn, job_id)
    ok, issues = validate_chatgpt_generation_plan(plan)
    request_id = _chatgpt_pro_generation_request_id(job)
    stored = {
        **dict(plan or {}),
        'generation_request_id': request_id,
        'validation_ok': ok,
        'validation_issues': issues,
    }
    market, profile = normalize_market(str(job.get('country') or ''), str(job.get('project') or ''))
    rules = dict(job.get('rules') or {})
    material_refs = dict(job.get('material_refs') or {})
    now = utc_now()
    conn.execute(
        "UPDATE creative_pro_work_queue SET generation_plan_json = ?, status = ? WHERE job_id = ?",
        (json.dumps(stored, ensure_ascii=False, sort_keys=True), 'claimed' if ok else 'failed', job_id),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO creative_generation_requests
        (request_id, surface, image_size, market, brand, country, project, campaign, ad_group, ad, objective,
         prompt, negative_prompt, prompt_hash, risk_status, risk_tags_json, review_status, status,
         requested_by, payload_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            FEED_STATIC_AD_SURFACE,
            '1024x1024',
            market,
            str(job.get('brand_display_name') or profile.get('brand') or ''),
            str(job.get('country') or ''),
            str(job.get('project') or ''),
            str(material_refs.get('campaign') or ''),
            str(material_refs.get('ad_group') or ''),
            str(material_refs.get('ad') or ''),
            '真实入会',
            str((plan or {}).get('prompt') or rules.get('prompt') or ''),
            str((plan or {}).get('negative_prompt') or rules.get('negative_prompt') or ''),
            stable_id(job_id, (plan or {}).get('prompt') or rules.get('prompt') or ''),
            'ok' if ok else 'blocked',
            json.dumps(issues, ensure_ascii=False),
            'pending_review' if ok else 'needs_manual_input',
            'pending_image' if ok else 'blocked',
            str(job.get('claimed_by') or job.get('created_by') or ''),
            json.dumps({
                'provider_mode': PROVIDER_CHATGPT_PRO_MANUAL,
                'job_id': job_id,
                'experiment_id': job.get('experiment_id') or '',
                'experiment_code': job.get('experiment_code') or '',
                'generation_plan': stored,
                'manual_upload_optional': True,
                'external_write_performed': False,
            }, ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )
    conn.execute(
        "UPDATE creative_experiment_suggestions SET generation_request_id = ?, updated_at = ? WHERE experiment_id = ?",
        (request_id, now, job['experiment_id']),
    )
    if not ok:
        conn.execute(
            "UPDATE creative_pro_work_queue SET error_code = ?, error_message = ? WHERE job_id = ?",
            ('generation_plan_validation_failed', ','.join(issues), job_id),
        )
    conn.commit()
    return get_chatgpt_pro_job(conn, job_id)


def mark_chatgpt_pro_job_completed(conn: sqlite3.Connection, job_id: str) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    get_chatgpt_pro_job(conn, job_id)
    conn.execute(
        "UPDATE creative_pro_work_queue SET status = 'completed', completed_at = ? WHERE job_id = ?",
        (utc_now(), job_id),
    )
    cleanup_temporary_creative_source_images(conn, job_id=job_id)
    conn.commit()
    return get_chatgpt_pro_job(conn, job_id)


def delete_chatgpt_pro_job(conn: sqlite3.Connection, job_id: str) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    get_chatgpt_pro_job(conn, job_id)
    conn.execute(
        """
        UPDATE creative_pro_work_queue
        SET status = 'deleted',
            error_code = 'deleted_by_operator',
            error_message = 'Deleted from ops creative workbench'
        WHERE job_id = ?
        """,
        (job_id,),
    )
    conn.commit()
    return get_chatgpt_pro_job(conn, job_id)


def save_chatgpt_pro_uploaded_image(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    filename: str,
    content: bytes,
    uploaded_by: str = '',
    output_dir: Path = DEFAULT_IMAGE_OUTPUT_DIR,
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    job = get_chatgpt_pro_job(conn, job_id)
    detected_content_type = _detect_uploaded_image_content_type(content)
    uploaded_width, uploaded_height = _validate_chatgpt_pro_uploaded_image_quality(content, detected_content_type)
    uploaded_image_size = f'{uploaded_width}x{uploaded_height}'
    suffix = IMAGE_PROVIDER_BINARY_TYPES.get(detected_content_type, '.png')
    output_dir.mkdir(parents=True, exist_ok=True)
    image_id = f'pro_img_{stable_id(job_id, filename, hashlib.sha256(content or b"").hexdigest())}'
    image_path = output_dir / f'{image_id}{suffix}'
    optimized_content, compression_meta = _optimize_creative_image_bytes(content or b'', detected_content_type)
    image_path.write_bytes(optimized_content)
    thumbnail_ref, thumbnail_meta = _write_creative_image_thumbnail(optimized_content, output_dir / f'{image_id}_thumb.webp')
    image_hash = file_sha256(image_path)
    request_id = _chatgpt_pro_generation_request_id(job)
    now = utc_now()
    job_rules = dict(job.get('rules') or {})
    manual_generation_mode = str(job_rules.get('generation_mode') or GENERATION_MODE_NEW_DIRECTION)
    manual_creative_direction = str(job_rules.get('creative_direction') or CREATIVE_DIRECTION_POINTS_REWARD)
    existing_request = conn.execute(
        "SELECT request_id FROM creative_generation_requests WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if not existing_request:
        market, profile = normalize_market(str(job.get('country') or ''), str(job.get('project') or ''))
        rules = dict(job.get('rules') or {})
        material_refs = dict(job.get('material_refs') or {})
        conn.execute(
            """
            INSERT INTO creative_generation_requests
            (request_id, surface, image_size, market, brand, country, project, campaign, ad_group, ad, objective,
             prompt, negative_prompt, prompt_hash, risk_status, risk_tags_json, review_status, status,
             requested_by, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                FEED_STATIC_AD_SURFACE,
                uploaded_image_size,
                market,
                str(job.get('brand_display_name') or profile.get('brand') or ''),
                str(job.get('country') or ''),
                str(job.get('project') or ''),
                str(material_refs.get('campaign') or ''),
                str(material_refs.get('ad_group') or ''),
                str(material_refs.get('ad') or ''),
                '真实入会',
                str(rules.get('prompt') or ''),
                str(rules.get('negative_prompt') or ''),
                stable_id(job_id, rules.get('prompt') or ''),
                'ok',
                '[]',
                'pending_review',
                'pending_image',
                str(uploaded_by or job.get('claimed_by') or job.get('created_by') or ''),
                json.dumps({
                    'provider_mode': PROVIDER_CHATGPT_PRO_MANUAL,
                    'job_id': job_id,
                    'experiment_id': job.get('experiment_id') or '',
                    'experiment_code': job.get('experiment_code') or '',
                    'manual_upload_optional': True,
                    'external_write_performed': False,
                }, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
    conn.execute(
        """
        UPDATE creative_generation_requests
        SET status = ?, image_size = ?, review_status = CASE WHEN review_status = '' THEN 'pending_review' ELSE review_status END, updated_at = ?
        WHERE request_id = ?
        """,
        ('generated', uploaded_image_size, now, request_id),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO creative_generated_images
        (image_id, request_id, surface, image_size, market, brand, image_ref, thumbnail_ref, prompt_hash,
         risk_status, risk_tags_json, review_status, provider, metadata_json, created_at, image_hash,
         final_delivery_hash, source_provider, uploaded_manually, uploaded_final_version, is_exact_generated_asset,
         task_id, generation_mode, creative_direction, candidate_index, width, height, file_path, thumbnail_path,
         file_size_bytes, file_quality_status, ocr_text_check_status, currency_reward_check_status,
         direction_fit_status, public_positioning_fit_status, visible_risk_status, old_image_preservation_status,
         quality_check_json, final_verdict)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            image_id,
            request_id,
            FEED_STATIC_AD_SURFACE,
            uploaded_image_size,
            str(job.get('country') or ''),
            str(job.get('brand_display_name') or ''),
            str(image_path),
            thumbnail_ref or str(image_path),
            stable_id(job_id, 'chatgpt_pro_uploaded'),
            'pending_review',
            '[]',
            'pending_review',
            PROVIDER_CHATGPT_PRO_MANUAL,
            json.dumps({
                'job_id': job_id,
                'uploaded_by': uploaded_by,
                'manual_upload_optional': True,
                'compression': compression_meta,
                'thumbnail': thumbnail_meta,
            }, ensure_ascii=False, sort_keys=True),
            now,
            image_hash,
            image_hash,
            PROVIDER_CHATGPT_PRO_MANUAL,
            1,
            1,
            1,
            '',
            manual_generation_mode,
            manual_creative_direction,
            0,
            uploaded_width,
            uploaded_height,
            str(image_path),
            thumbnail_ref or str(image_path),
            len(optimized_content or b''),
            'passed',
            'pending_manual_review',
            'pending_manual_review',
            'pending_manual_review',
            'pending_manual_review',
            'pending_manual_review',
            'not_applicable',
            json.dumps({'source': 'chatgpt_pro_manual_upload'}, ensure_ascii=False, sort_keys=True),
            FINAL_VERDICT_PENDING_REVIEW,
        ),
    )
    conn.execute(
        "UPDATE creative_experiment_suggestions SET generated_image_id = ?, generation_request_id = ?, updated_at = ? WHERE experiment_id = ?",
        (image_id, request_id, now, job['experiment_id']),
    )
    conn.commit()
    # A different generation task may finish between this task's INSERT and
    # serialization. Never let that race auto-review the wrong image.
    image = next(
        (item for item in latest_generated_images(conn, limit=100) if str(item.get('image_id') or '') == image_id),
        {},
    )
    if not image:
        raise InvalidCreativeImageError('uploaded_image_serialization_failed')
    updated_job = get_chatgpt_pro_job(conn, job_id)
    updated_job['manual_image'] = {
        'image_id': image['image_id'],
        'filename': filename or image_path.name,
        'provider': PROVIDER_CHATGPT_PRO_MANUAL,
        'manual_upload_optional': True,
    }
    return {
        'ok': True,
        'job': updated_job,
        'image': image,
        'manual_upload_required': False,
        'external_write_performed': False,
    }


def creative_image_auto_approval_eligible(quality_summary: Dict[str, Any]) -> bool:
    """Return true only when every strict machine and L3 review gate passed."""
    quality = dict(quality_summary or {})
    provider_evaluation = dict(quality.get('provider_evaluation') or {})
    l3_review = dict(provider_evaluation.get('l3_visual_review') or {})
    return bool(
        str(provider_evaluation.get('verdict') or '').lower() == 'pass'
        and str(l3_review.get('verdict') or '').lower() == 'pass'
        and str(l3_review.get('l3_visual_review_status') or l3_review.get('status') or '').lower() == 'passed'
        and str(quality.get('file_quality_status') or '').lower() == 'passed'
        and str(quality.get('ocr_text_check_status') or '').lower() == 'passed'
        and str(quality.get('currency_reward_check_status') or '').lower() == 'passed'
        and str(quality.get('visible_risk_status') or '').lower() == 'passed'
        and str(quality.get('direction_fit_status') or '').lower() in {'pass', 'passed'}
        and str(quality.get('public_positioning_fit_status') or '').lower() in {'pass', 'passed'}
        and str(quality.get('final_verdict') or '').lower() == 'pending_review'
    )


def _hermes_generation_task_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        'task_id': row['task_id'],
        'job_id': row['job_id'],
        'generation_request_id': row['generation_request_id'],
        'provider': row['provider'],
        'provider_mode': row['provider_mode'],
        'status': row['status'],
        'image_size': row['image_size'],
        'prompt': row['prompt'],
        'negative_prompt': row['negative_prompt'],
        'candidate_count': int(row['candidate_count'] or 0),
        'attempt_count': int(row['attempt_count'] or 0),
        'max_attempts': int(row['max_attempts'] or 0),
        'accepted_image_count': int(row['accepted_image_count'] or 0),
        'rejected_candidate_count': int(row['rejected_candidate_count'] or 0),
        'error_code': row['error_code'],
        'error_message': safe_provider_error(row['error_message']),
        'quality_summary': _json_load(row['quality_summary_json'], {}),
        'payload': _json_load(row['payload_json'], {}),
        'generation_mode': row['generation_mode'],
        'creative_direction': row['creative_direction'],
        'prompt_package': _json_load(row['prompt_package_json'], {}),
        'final_prompt': row['final_prompt'],
        'source_image': {
            'source_image_id': row['source_image_id'],
            'source_image_signed_url': row['source_image_signed_url'],
            'source_image_hash': row['source_image_hash'],
            'source_image_required': bool(row['source_image_required']),
            'source_image_used': bool(row['source_image_used']),
        },
        'preserve': _json_load(row['preserve_json'], []),
        'modify': _json_load(row['modify_json'], []),
        'visible_claim_policy': _json_load(row['visible_claim_policy_json'], {}),
        'brand_visual_guidelines': _json_load(row['brand_visual_guidelines_json'], {}),
        'currency_threshold_version': row['currency_threshold_version'],
        'heartbeat_at': row['heartbeat_at'],
        'state_history': _json_load(row['state_history_json'], []),
        'provider_response': _json_load(row['provider_response_json'], {}),
        'lease_owner': row['lease_owner'],
        'lease_expires_at': row['lease_expires_at'],
        'created_at': row['created_at'],
        'claimed_at': row['claimed_at'],
        'started_at': row['started_at'],
        'finished_at': row['finished_at'],
        'updated_at': row['updated_at'],
        'external_write_performed': False,
    }


def _append_hermes_task_history(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str,
    at: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    ensure_creative_image_generation_tables(conn)
    row = conn.execute(
        "SELECT state_history_json FROM creative_generation_tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return
    history = _json_load(row['state_history_json'], [])
    if not isinstance(history, list):
        history = []
    item: Dict[str, Any] = {'status': status, 'at': at or utc_now()}
    if meta:
        item['meta'] = dict(meta)
    history.append(item)
    conn.execute(
        "UPDATE creative_generation_tasks SET state_history_json = ? WHERE task_id = ?",
        (json.dumps(history[-80:], ensure_ascii=False, sort_keys=True), task_id),
    )


def _normalize_hermes_image_size(value: Any) -> Tuple[str, Tuple[int, int]]:
    text = str(value or '1024x1024').strip().lower().replace('×', 'x')
    if text not in {'1024x1024', '512x512'}:
        raise ValueError('invalid_image_size')
    return '1024x1024', (1024, 1024)


def _hermes_task_prompt_package(job: Dict[str, Any], *, image_size: str, candidate_count: int) -> Dict[str, Any]:
    plan = dict(job.get('generation_plan') or {})
    rules = dict(job.get('rules') or {})
    material_refs = dict(job.get('material_refs') or {})
    prompt = str(plan.get('prompt') or rules.get('prompt') or '').strip()
    negative_prompt = _sanitize_generation_negative_prompt(plan.get('negative_prompt') or rules.get('negative_prompt') or '')
    headline = str(plan.get('image_headline') or plan.get('headline') or '').strip()
    reference_asset = dict(plan.get('reference_asset') or rules.get('reference_asset') or {})
    creative_revision_brief = dict(plan.get('creative_revision_brief') or rules.get('creative_revision_brief') or {})
    revision_plan = dict(plan.get('revision_plan') or rules.get('revision_plan') or {})
    source_image_structure = dict(plan.get('source_image_structure') or rules.get('source_image_structure') or {})
    market, market_profile = normalize_market(job.get('country'), job.get('project'))
    direction_key = str(rules.get('creative_direction') or creative_direction_key(plan.get('creative_direction') or rules.get('core_offer') or headline or prompt))
    safe_compliance = direction_key == CREATIVE_DIRECTION_SAFE_COMPLIANCE
    direction = next((item for item in CREATIVE_DIRECTION_PROFILES if item.get('key') == direction_key), creative_direction_profile(direction_key))
    prompt_package = dict(rules.get('prompt_package') or {})
    prompt_package_market = str(prompt_package.get('market') or '').strip()
    if not prompt_package or (prompt_package_market and prompt_package_market != market):
        prompt_package = build_creative_prompt_package(
            job_id=str(job.get('job_id') or ''),
            experiment_id=str(job.get('experiment_id') or ''),
            experiment_code=str(job.get('experiment_code') or ''),
            market=market,
            brand=str(job.get('brand_display_name') or market_profile.get('brand') or ''),
            country=job.get('country'),
            language_hint=str(market_profile.get('language_hint') or ''),
            image_size=image_size,
            creative_direction=direction,
            prompt=prompt,
            negative_prompt=negative_prompt,
            source_reference={**reference_asset, **material_refs},
            preserve=list(creative_revision_brief.get('preserve') or []),
            modify=list(creative_revision_brief.get('modify') or []),
            revision_plan=revision_plan,
            source_image_structure=source_image_structure,
            candidate_count=candidate_count,
        )
    else:
        prompt_package = {
            **prompt_package,
            'market': market,
            'country': str(job.get('country') or ''),
            'brand': str(job.get('brand_display_name') or market_profile.get('brand') or ''),
            'language_hint': str(market_profile.get('language_hint') or ''),
            'image_size': image_size,
            'candidate_count': candidate_count,
            'final_prompt': prompt_package.get('final_prompt') or prompt,
            'negative_prompt': _sanitize_generation_negative_prompt(prompt_package.get('negative_prompt') or negative_prompt),
        }
    if safe_compliance:
        prompt_package['safe_generation_blueprint'] = safe_compliance_generation_blueprint(
            market,
            str(job.get('brand_display_name') or market_profile.get('brand') or ''),
        )
        prompt_package['headline_candidates'] = list(
            prompt_package['safe_generation_blueprint']['visible_copy']['headline_candidates']
        )
    else:
        prompt = _append_currency_reward_constraint(prompt_package.get('final_prompt') or prompt, market)
        prompt_package.update({
            'version': PROMPT_PACKAGE_VERSION,
            'final_prompt': prompt,
            'visible_claim_policy': build_visible_claim_policy(market, direction_key),
            'currency_reward_contract': currency_reward_generation_contract(market),
            'currency_threshold_version': CURRENCY_THRESHOLD_VERSION,
        })
    source_image = source_image_fields_from_reference({**reference_asset, **material_refs})
    if not source_image.get('source_image_signed_url') and isinstance(prompt_package.get('source_image'), dict):
        source_image = dict(prompt_package.get('source_image') or {})
    prompt_package = {
        **prompt_package,
        'generation_mode': source_image.get('generation_mode') or prompt_package.get('generation_mode') or GENERATION_MODE_NEW_DIRECTION,
        'source_image': source_image,
        'revision_plan': prompt_package.get('revision_plan') or revision_plan,
        'source_image_structure': (
            {}
            if safe_compliance else
            (prompt_package.get('source_image_structure') or source_image_structure or source_image_structure_from_reference({**reference_asset, **material_refs}))
        ),
    }
    return {
        'job_id': job.get('job_id'),
        'experiment_id': job.get('experiment_id'),
        'experiment_code': job.get('experiment_code'),
        'surface': FEED_STATIC_AD_SURFACE,
        'image_size': image_size,
        'country': job.get('country'),
        'brand_display_name': job.get('brand_display_name'),
        'generation_mode': prompt_package.get('generation_mode') or source_image.get('generation_mode') or GENERATION_MODE_NEW_DIRECTION,
        'creative_direction': prompt_package.get('creative_direction') or direction_key,
        'prompt_package': prompt_package,
        'public_ad_positioning': prompt_package.get('public_ad_positioning') or PUBLIC_AD_POSITIONING['visible_positioning'],
        'visible_claim_policy': prompt_package.get('visible_claim_policy') or build_visible_claim_policy(market),
        'brand_visual_guidelines': prompt_package.get('brand_visual_guidelines') or BRAND_VISUAL_GUIDELINES_BY_MARKET.get(market, {}),
        'source_image': source_image,
        'preserve': prompt_package.get('preserve') or creative_revision_brief.get('preserve') or [],
        'modify': prompt_package.get('modify') or creative_revision_brief.get('modify') or [],
        'currency_threshold_version': CURRENCY_THRESHOLD_VERSION,
        'headline': headline,
        'prompt': prompt_package.get('final_prompt') or prompt,
        'negative_prompt': negative_prompt,
        'candidate_count': candidate_count,
        'material_refs': material_refs,
        'reference_asset': reference_asset,
        'creative_revision_brief': creative_revision_brief,
        'revision_plan': prompt_package.get('revision_plan') or revision_plan,
        'source_image_structure': {} if safe_compliance else (prompt_package.get('source_image_structure') or source_image_structure),
        'must_have': ([
            'production-ready full-bleed Meta feed static ad',
            'product-led smartphone dashboard occupying 48-58% of canvas',
            'named in-app activities, visible completion states, one consistent progress summary, an in-app points state, and a clear reward destination',
            'one concise meaning-compatible headline, one supporting sentence, and a small readable set of product labels',
            'adult woman only as natural positive supporting context occupying 20-28% of canvas',
            'high-key daylight art direction with a flexible bright palette and no gloomy or dark-dominant treatment',
            'finished commercial material treatment with layered depth, coherent lighting, and no raw geometric placeholders',
            'slim light brand area with a right-side reserve for the compact verified light logo-and-wordmark card',
        ] if safe_compliance else [
            'Meta/Facebook feed static ad visual',
            'realistic person or human trust scene',
            'phone UI product proof occupying 48-58% of canvas',
            'at least three readable named in-app task rows; every row states a concrete task action',
            'both completed and pending task states plus one internally consistent numeric progress summary such as 3/5 or 60%',
            'a readable points state and a clear reward destination; anonymous bars, empty cards, and placeholder rows do not count',
            'prominent local-currency reward module that is visually stronger than points labels but uses small compliant amounts',
            'clear headline in the target language',
            'bottom brand lockup',
            'full-bleed square composition',
        ]),
        'must_not_generate': [
            'icon',
            'flowchart',
            'wireframe',
            'low-detail placeholder',
            'UI-only mockup',
            'app store screenshot',
            'download page screenshot',
            'cash pile or cash rain',
            'guaranteed income claim',
            'fixed salary claim',
            'withdraw proof',
            'creator recruitment, social chatting job, host recruitment, MCN, or guild conversion language',
            'chat-to-earn claim',
            'phone number, email, WhatsApp contact, ID card, or bank card',
        ],
        'quality_gate': {
            'file_quality_required': True,
            'semantic_evaluation_supported': True,
            'accepted_dimensions': ['1024x1024'],
            'final_state_after_pass': 'pending_review',
            'mark_completed_allowed': False,
        },
    }


def start_hermes_image2_generation_task(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    image_size: str = '1024x1024',
    candidate_count: int = 1,
    max_attempts: int = 3,
    created_by: str = '',
    force_regenerate: bool = False,
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    job = get_chatgpt_pro_job(conn, job_id)
    image_size, _ = _normalize_hermes_image_size(image_size or (job.get('generation_plan') or {}).get('image_size'))
    plan = dict(job.get('generation_plan') or {})
    if not plan.get('validation_ok'):
        raise ValueError('generation_plan_required')
    candidate_count = max(1, min(int(candidate_count or 1), 6))
    max_attempts = max(1, min(int(max_attempts or 3), 10))
    request_id = str(plan.get('generation_request_id') or _chatgpt_pro_generation_request_id(job))
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute('BEGIN IMMEDIATE')
    active = conn.execute(
        """
        SELECT * FROM creative_generation_tasks
        WHERE job_id = ? AND status IN ('queued', 'claimed')
        ORDER BY created_at DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if active and not force_regenerate:
        if owns_transaction:
            conn.commit()
        return {'ok': True, 'task': _hermes_generation_task_from_row(active), 'job': job, 'created': False}
    usable_image = conn.execute(
        """
        SELECT image_id, review_status, task_id
        FROM creative_generated_images
        WHERE (json_extract(metadata_json,'$.job_id')=? OR (? != '' AND request_id=?))
          AND lower(review_status) IN ('pending_review','manual_review_required','approved','used_in_ad')
        ORDER BY created_at DESC,image_id DESC LIMIT 1
        """,
        (job_id, request_id, request_id),
    ).fetchone()
    if usable_image and not force_regenerate:
        if owns_transaction:
            conn.commit()
        return {
            'ok': True,
            'task': get_creative_generation_task(conn, str(usable_image['task_id'])) if usable_image['task_id'] else {},
            'job': get_chatgpt_pro_job(conn, job_id),
            'created': False,
            'suppression_reason': f"creative_already_{str(usable_image['review_status'] or '').lower()}",
            'image_id': str(usable_image['image_id']),
        }
    payload = _hermes_task_prompt_package(job, image_size=image_size, candidate_count=candidate_count)
    now = utc_now()
    generation_round = int(conn.execute(
        "SELECT COUNT(*) FROM creative_generation_tasks WHERE job_id = ? AND generation_request_id = ?",
        (job_id, request_id),
    ).fetchone()[0]) + 1
    task_id = f'creative_generation_task_{stable_id(job_id, request_id, image_size, generation_round)}'
    conn.execute(
        """
        INSERT INTO creative_generation_tasks
        (task_id, job_id, generation_request_id, provider, provider_mode, status, image_size, prompt,
         negative_prompt, candidate_count, max_attempts, payload_json, generation_mode, creative_direction,
         prompt_package_json, final_prompt, source_image_id, source_image_signed_url, source_image_hash,
         source_image_required, preserve_json, modify_json, visible_claim_policy_json, brand_visual_guidelines_json,
         currency_threshold_version, state_history_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            job_id,
            request_id,
            PROVIDER_HERMES_IMAGE2_AGENT,
            PROVIDER_HERMES_IMAGE2_AGENT,
            HERMES_TASK_STATUS_QUEUED,
            image_size,
            payload['prompt'],
            payload['negative_prompt'],
            candidate_count,
            max_attempts,
            json.dumps({**payload, 'created_by': created_by}, ensure_ascii=False, sort_keys=True),
            str(payload.get('generation_mode') or GENERATION_MODE_NEW_DIRECTION),
            str(payload.get('creative_direction') or CREATIVE_DIRECTION_POINTS_REWARD),
            json.dumps(payload.get('prompt_package') or {}, ensure_ascii=False, sort_keys=True),
            str((payload.get('prompt_package') or {}).get('final_prompt') or payload.get('prompt') or ''),
            str((payload.get('source_image') or {}).get('source_image_id') or ''),
            str((payload.get('source_image') or {}).get('source_image_signed_url') or ''),
            str((payload.get('source_image') or {}).get('source_image_hash') or ''),
            1 if (payload.get('source_image') or {}).get('source_image_required') else 0,
            json.dumps(payload.get('preserve') or [], ensure_ascii=False, sort_keys=True),
            json.dumps(payload.get('modify') or [], ensure_ascii=False, sort_keys=True),
            json.dumps(payload.get('visible_claim_policy') or {}, ensure_ascii=False, sort_keys=True),
            json.dumps(payload.get('brand_visual_guidelines') or {}, ensure_ascii=False, sort_keys=True),
            CURRENCY_THRESHOLD_VERSION,
            json.dumps([{'status': HERMES_TASK_STATUS_QUEUED, 'at': now}], ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )
    conn.execute(
        """
        UPDATE creative_pro_work_queue
        SET provider_mode = ?, status = 'generating', error_code = '', error_message = ''
        WHERE job_id = ?
        """,
        (PROVIDER_HERMES_IMAGE2_AGENT, job_id),
    )
    conn.commit()
    return {'ok': True, 'task': get_creative_generation_task(conn, task_id), 'job': get_chatgpt_pro_job(conn, job_id), 'created': True}


def get_creative_generation_task(conn: sqlite3.Connection, task_id: str) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    row = conn.execute("SELECT * FROM creative_generation_tasks WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        raise ValueError('creative_generation_task_not_found')
    task = _hermes_generation_task_from_row(row)
    task['job'] = get_chatgpt_pro_job(conn, task['job_id'])
    return task


def get_creative_pro_generation_status(conn: sqlite3.Connection, job_id: str) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    reconcile_stale_hermes_image2_generation_tasks(conn)
    job = get_chatgpt_pro_job(conn, job_id)
    tasks = [
        _hermes_generation_task_from_row(row)
        for row in conn.execute(
            "SELECT * FROM creative_generation_tasks WHERE job_id = ? ORDER BY created_at DESC LIMIT 20",
            (job_id,),
        ).fetchall()
    ]
    return {
        'ok': True,
        'job_id': job_id,
        'job': job,
        'tasks': tasks,
        'latest_task': tasks[0] if tasks else None,
        'external_write_performed': False,
    }


def reconcile_stale_hermes_image2_generation_tasks(
    conn: sqlite3.Connection,
    *,
    now: str = '',
    limit: int = 100,
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    checked_at = str(now or utc_now())
    requested_limit = max(1, min(int(limit or 100), 1000))
    stale_rows = conn.execute(
        """
        SELECT task_id, job_id, attempt_count, max_attempts, lease_owner, lease_expires_at
        FROM creative_generation_tasks
        WHERE provider_mode = ? AND status = ? AND lease_expires_at != '' AND lease_expires_at <= ?
        ORDER BY lease_expires_at ASC
        LIMIT ?
        """,
        (PROVIDER_HERMES_IMAGE2_AGENT, HERMES_TASK_STATUS_CLAIMED, checked_at, requested_limit),
    ).fetchall()
    requeued_task_ids: List[str] = []
    failed_task_ids: List[str] = []
    for stale in stale_rows:
        attempt_count = int(stale['attempt_count'] or 0)
        max_attempts = max(1, int(stale['max_attempts'] or 3))
        can_retry = attempt_count < max_attempts
        next_status = HERMES_TASK_STATUS_QUEUED if can_retry else HERMES_TASK_STATUS_FAILED
        error_code = 'lease_expired_requeued' if can_retry else 'lease_expired_max_attempts'
        error_message = (
            'Hermes worker lease expired before upload; task requeued'
            if can_retry
            else 'Hermes worker lease expired before upload; maximum attempts exhausted'
        )
        update = conn.execute(
            """
            UPDATE creative_generation_tasks
            SET status = ?, lease_owner = '', lease_expires_at = '', error_code = ?,
                error_message = ?, finished_at = ?, updated_at = ?
            WHERE task_id = ? AND status = ? AND lease_expires_at != '' AND lease_expires_at <= ?
            """,
            (
                next_status,
                error_code,
                error_message,
                '' if can_retry else checked_at,
                checked_at,
                stale['task_id'],
                HERMES_TASK_STATUS_CLAIMED,
                checked_at,
            ),
        )
        if int(update.rowcount or 0) != 1:
            continue
        if can_retry:
            conn.execute(
                "UPDATE creative_pro_work_queue SET status = 'generating', error_code = ?, error_message = ? WHERE job_id = ?",
                (error_code, error_message, stale['job_id']),
            )
            requeued_task_ids.append(str(stale['task_id']))
        else:
            conn.execute(
                "UPDATE creative_pro_work_queue SET status = 'failed', error_code = ?, error_message = ? WHERE job_id = ?",
                (error_code, error_message, stale['job_id']),
            )
            failed_task_ids.append(str(stale['task_id']))
        _append_hermes_task_history(
            conn,
            stale['task_id'],
            status=next_status,
            at=checked_at,
            meta={
                'reason': 'lease_expired',
                'attempt_count': attempt_count,
                'max_attempts': max_attempts,
                'previous_lease_owner': str(stale['lease_owner'] or ''),
            },
        )
    if requeued_task_ids or failed_task_ids:
        conn.commit()
    return {
        'ok': True,
        'checked_at': checked_at,
        'stale_count': len(requeued_task_ids) + len(failed_task_ids),
        'requeued_count': len(requeued_task_ids),
        'failed_count': len(failed_task_ids),
        'requeued_task_ids': requeued_task_ids,
        'failed_task_ids': failed_task_ids,
    }


def next_hermes_image2_generation_task(conn: sqlite3.Connection, *, claim: bool = False, lease_owner: str = 'hermes_image2_agent', lease_seconds: int = 900) -> Optional[Dict[str, Any]]:
    ensure_creative_image_generation_tables(conn)
    reconcile_stale_hermes_image2_generation_tasks(conn)
    row = conn.execute(
        """
        SELECT * FROM creative_generation_tasks
        WHERE provider_mode = ? AND status = ?
        ORDER BY created_at ASC LIMIT 1
        """,
        (PROVIDER_HERMES_IMAGE2_AGENT, HERMES_TASK_STATUS_QUEUED),
    ).fetchone()
    if not row:
        return None
    task = _hermes_generation_task_from_row(row)
    if claim:
        return claim_hermes_image2_generation_task(conn, task['task_id'], lease_owner=lease_owner, lease_seconds=lease_seconds)
    task['job'] = get_chatgpt_pro_job(conn, task['job_id'])
    return task


def claim_hermes_image2_generation_task(conn: sqlite3.Connection, task_id: str, *, lease_owner: str = 'hermes_image2_agent', lease_seconds: int = 900) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    task = get_creative_generation_task(conn, task_id)
    if task['status'] != HERMES_TASK_STATUS_QUEUED:
        raise ValueError('creative_generation_task_not_claimable')
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_expires_at = (now_dt + timedelta(seconds=max(60, int(lease_seconds or 900)))).isoformat()
    updated = conn.execute(
        """
        UPDATE creative_generation_tasks
        SET status = ?, lease_owner = ?, lease_expires_at = ?, heartbeat_at = ?,
            claimed_at = CASE WHEN claimed_at = '' THEN ? ELSE claimed_at END,
            started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END, attempt_count = attempt_count + 1, updated_at = ?
        WHERE task_id = ? AND status = ?
        """,
        (HERMES_TASK_STATUS_CLAIMED, lease_owner, lease_expires_at, now, now, now, now, task_id, HERMES_TASK_STATUS_QUEUED),
    )
    if int(updated.rowcount or 0) != 1:
        conn.rollback()
        raise ValueError('creative_generation_task_not_claimable')
    _append_hermes_task_history(conn, task_id, status=HERMES_TASK_STATUS_CLAIMED, at=now, meta={'lease_owner': lease_owner})
    conn.commit()
    return get_creative_generation_task(conn, task_id)


def heartbeat_hermes_image2_generation_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    lease_owner: str = 'hermes_image2_agent',
    lease_seconds: int = 900,
    provider_response: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    task = get_creative_generation_task(conn, task_id)
    if task['status'] != HERMES_TASK_STATUS_CLAIMED:
        raise ValueError('creative_generation_task_not_running')
    if lease_owner and task.get('lease_owner') and lease_owner != task.get('lease_owner'):
        raise ValueError('creative_generation_task_lease_owner_mismatch')
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_expires_at = (now_dt + timedelta(seconds=max(60, int(lease_seconds or 900)))).isoformat()
    current_response = dict(task.get('provider_response') or {})
    if provider_response:
        current_response.update(provider_response)
    conn.execute(
        """
        UPDATE creative_generation_tasks
        SET heartbeat_at = ?, lease_expires_at = ?, provider_response_json = ?, updated_at = ?
        WHERE task_id = ?
        """,
        (
            now,
            lease_expires_at,
            json.dumps(current_response, ensure_ascii=False, sort_keys=True),
            now,
            task_id,
        ),
    )
    _append_hermes_task_history(conn, task_id, status='heartbeat', at=now, meta={'lease_owner': lease_owner})
    conn.commit()
    return get_creative_generation_task(conn, task_id)


def fail_hermes_image2_generation_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    error_code: str,
    error_message: str = '',
    retryable: bool = True,
    provider_response: Optional[Dict[str, Any]] = None,
    expected_attempt_count: Optional[int] = None,
    expected_lease_owner: str = '',
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    task = get_creative_generation_task(conn, task_id)
    normalized_expected_owner = str(expected_lease_owner or '').strip()
    normalized_expected_attempt = None
    if expected_attempt_count is not None:
        try:
            normalized_expected_attempt = int(expected_attempt_count)
        except (TypeError, ValueError):
            normalized_expected_attempt = None
    ignore_reason = ''
    if task.get('status') != HERMES_TASK_STATUS_CLAIMED:
        ignore_reason = f"task_not_claimed:{task.get('status') or 'unknown'}"
    elif normalized_expected_attempt is not None and int(task.get('attempt_count') or 0) != normalized_expected_attempt:
        ignore_reason = 'attempt_count_mismatch'
    elif normalized_expected_owner and str(task.get('lease_owner') or '').strip() != normalized_expected_owner:
        ignore_reason = 'lease_owner_mismatch'
    if ignore_reason:
        return {
            **task,
            'failure_report_ignored': True,
            'failure_report_ignore_reason': ignore_reason,
        }
    attempt_count = int(task.get('attempt_count') or 0)
    max_attempts = max(1, int(task.get('max_attempts') or 3))
    can_retry = bool(retryable) and attempt_count < max_attempts
    next_status = HERMES_TASK_STATUS_QUEUED if can_retry else HERMES_TASK_STATUS_FAILED
    job_status = 'generating' if can_retry else 'failed'
    safe_code = safe_provider_error(error_code or 'hermes_image2_generation_failed', limit=120)
    safe_message = safe_provider_error(error_message or safe_code, limit=500)
    now = utc_now()
    conn.execute(
        """
        UPDATE creative_generation_tasks
        SET status = ?, lease_owner = '', lease_expires_at = '', error_code = ?, error_message = ?,
            provider_response_json = ?, finished_at = ?, updated_at = ?
        WHERE task_id = ?
        """,
        (
            next_status,
            safe_code,
            safe_message,
            json.dumps(provider_response or {}, ensure_ascii=False, sort_keys=True),
            '' if can_retry else now,
            now,
            task_id,
        ),
    )
    conn.execute(
        "UPDATE creative_pro_work_queue SET status = ?, error_code = ?, error_message = ? WHERE job_id = ?",
        (job_status, safe_code, safe_message, task['job_id']),
    )
    _append_hermes_task_history(conn, task_id, status=next_status, at=now, meta={'error_code': safe_code, 'retryable': bool(can_retry)})
    conn.commit()
    return get_creative_generation_task(conn, task_id)


def cancel_hermes_image2_generation_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    cancelled_by: str = '',
    reason: str = 'cancelled_by_operator',
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    task = get_creative_generation_task(conn, task_id)
    if task['status'] == HERMES_TASK_STATUS_UPLOADED:
        raise ValueError('creative_generation_task_already_uploaded')
    now = utc_now()
    safe_reason = safe_provider_error(reason or 'cancelled_by_operator', limit=300)
    conn.execute(
        """
        UPDATE creative_generation_tasks
        SET status = ?, lease_owner = '', lease_expires_at = '', error_code = 'cancelled',
            error_message = ?, finished_at = ?, updated_at = ?
        WHERE task_id = ?
        """,
        (HERMES_TASK_STATUS_CANCELLED, safe_reason, now, now, task_id),
    )
    conn.execute(
        "UPDATE creative_pro_work_queue SET status = 'claimed', error_code = 'generation_cancelled', error_message = ? WHERE job_id = ?",
        (safe_reason, task['job_id']),
    )
    _append_hermes_task_history(conn, task_id, status=HERMES_TASK_STATUS_CANCELLED, at=now, meta={'cancelled_by': cancelled_by, 'reason': safe_reason})
    conn.commit()
    return get_creative_generation_task(conn, task_id)


def refresh_hermes_image2_source_snapshot(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    update_task: bool = True,
    commit: bool = True,
) -> Dict[str, Any]:
    task = get_creative_generation_task(conn, task_id)
    generation_mode = str(task.get('generation_mode') or '').strip()
    source_required = bool((task.get('source_image') or {}).get('source_image_required'))
    if generation_mode not in {
        GENERATION_MODE_OLD_IMAGE_REFERENCE_REVISION,
        GENERATION_MODE_OLD_IMAGE_REGION_EDIT,
    } and not source_required:
        return {'ok': True, 'task': task, 'source_image_refreshed': False}

    job = dict(task.get('job') or {})
    rules = dict(job.get('rules') or {})
    material_refs = dict(job.get('material_refs') or {})
    payload = dict(task.get('payload') or {})
    prompt_package = dict(task.get('prompt_package') or {})
    references = [
        material_refs,
        dict(rules.get('reference_asset') or {}),
        dict((rules.get('prompt_package') or {}).get('source_image') or {}),
        dict(payload.get('reference_asset') or {}),
        dict(payload.get('material_refs') or {}),
        dict(payload.get('source_image') or {}),
        dict((payload.get('prompt_package') or {}).get('source_image') or {}),
        dict(task.get('source_image') or {}),
    ]
    source_reference: Dict[str, Any] = {}
    for reference in references:
        for key, value in reference.items():
            if value not in ('', None) and value != [] and value != {}:
                source_reference[key] = value
    resolved = resolve_true_source_image_reference(conn, source_reference, force_asset_resolution=True)
    source_ok, source_reasons = validate_true_source_image_reference(resolved)
    if not source_ok:
        raise ValueError(
            'creative_generation_task_source_image_invalid:' + ','.join(source_reasons)
        )

    source_image = source_image_fields_from_reference(resolved)
    source_image.update({
        'generation_mode': GENERATION_MODE_OLD_IMAGE_REFERENCE_REVISION,
        'source_image_required': 1,
    })
    source_fields = {
        key: source_image.get(key)
        for key in (
            'source_image_id',
            'source_image_signed_url',
            'source_image_hash',
            'source_image_width',
            'source_image_height',
            'source_image_quality',
        )
    }
    source_fields.update({
        'source_ad_id': str(resolved.get('source_ad_id') or material_refs.get('source_ad_id') or '').strip(),
        'source_creative_id': str(resolved.get('source_creative_id') or material_refs.get('source_creative_id') or '').strip(),
    })
    source_fields['source_image_resolution_status'] = str(
        resolved.get('source_image_resolution_status') or 'creative_asset_cache'
    )
    source_image_structure = source_image_structure_from_reference({**resolved, **source_fields})

    material_refs.update(source_fields)
    reference_asset = dict(rules.get('reference_asset') or {})
    reference_asset.update(source_fields)
    rules_prompt_package = dict(rules.get('prompt_package') or {})
    rules_prompt_package['source_image'] = source_image
    rules_prompt_package['source_image_structure'] = source_image_structure
    rules.update({
        'reference_asset': reference_asset,
        'prompt_package': rules_prompt_package,
        'source_image_structure': source_image_structure,
        'generation_mode': GENERATION_MODE_OLD_IMAGE_REFERENCE_REVISION,
    })
    generation_plan = dict(job.get('generation_plan') or {})
    if isinstance(generation_plan.get('reference_asset'), dict):
        generation_plan['reference_asset'] = {
            **dict(generation_plan.get('reference_asset') or {}),
            **source_fields,
        }
    if isinstance(generation_plan.get('prompt_package'), dict):
        generation_plan_prompt_package = dict(generation_plan.get('prompt_package') or {})
        generation_plan_prompt_package['source_image'] = source_image
        generation_plan_prompt_package['source_image_structure'] = source_image_structure
        generation_plan['prompt_package'] = generation_plan_prompt_package
    if isinstance(generation_plan.get('source_image_structure'), dict):
        generation_plan['source_image_structure'] = source_image_structure
    source_image_id = str(source_fields.get('source_image_id') or '').strip()
    source_asset_ids = [source_image_id] if source_image_id else list(job.get('source_asset_ids') or [])
    source_ad_id = str(source_fields.get('source_ad_id') or '').strip()
    source_ad_ids = [source_ad_id] if source_ad_id else list(job.get('source_ad_ids') or [])
    source_creative_id = str(source_fields.get('source_creative_id') or '').strip()
    source_creative_ids = [source_creative_id] if source_creative_id else list(job.get('source_creative_ids') or [])
    conn.execute(
        """
        UPDATE creative_pro_work_queue
        SET rules_json = ?, material_refs_json = ?, generation_plan_json = ?, source_asset_ids_json = ?,
            source_ad_ids_json = ?, source_creative_ids_json = ?
        WHERE job_id = ?
        """,
        (
            json.dumps(rules, ensure_ascii=False, sort_keys=True),
            json.dumps(material_refs, ensure_ascii=False, sort_keys=True),
            json.dumps(generation_plan, ensure_ascii=False, sort_keys=True),
            json.dumps(source_asset_ids, ensure_ascii=False, sort_keys=True),
            json.dumps(source_ad_ids, ensure_ascii=False, sort_keys=True),
            json.dumps(source_creative_ids, ensure_ascii=False, sort_keys=True),
            task['job_id'],
        ),
    )

    experiment_id = str(job.get('experiment_id') or '').strip()
    if experiment_id:
        experiment_row = conn.execute(
            'SELECT source_ad_id, source_creative_id, payload_json FROM creative_experiment_suggestions WHERE experiment_id = ?',
            (experiment_id,),
        ).fetchone()
        if experiment_row:
            experiment_payload = _json_load(experiment_row['payload_json'], {})
            if not isinstance(experiment_payload, dict):
                experiment_payload = {}
            experiment_task = dict(experiment_payload.get('production_task') or {})
            experiment_task.update(source_fields)
            experiment_payload.update({
                **source_fields,
                'source_image': source_image,
                'source_image_structure': source_image_structure,
                'production_task': experiment_task,
            })
            conn.execute(
                """
                UPDATE creative_experiment_suggestions
                SET source_ad_id = ?, source_creative_id = ?, payload_json = ?, updated_at = ?
                WHERE experiment_id = ?
                """,
                (
                    source_ad_id or str(experiment_row['source_ad_id'] or ''),
                    source_creative_id or str(experiment_row['source_creative_id'] or ''),
                    json.dumps(experiment_payload, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                    experiment_id,
                ),
            )

    if update_task:
        payload_prompt_package = dict(payload.get('prompt_package') or {})
        payload_prompt_package['source_image'] = source_image
        payload_prompt_package['source_image_structure'] = source_image_structure
        payload.update({
            'source_image': source_image,
            'prompt_package': payload_prompt_package,
            'material_refs': material_refs,
            'reference_asset': reference_asset,
            'source_image_structure': source_image_structure,
            'generation_mode': GENERATION_MODE_OLD_IMAGE_REFERENCE_REVISION,
        })
        prompt_package['source_image'] = source_image
        prompt_package['source_image_structure'] = source_image_structure
        prompt_package['generation_mode'] = GENERATION_MODE_OLD_IMAGE_REFERENCE_REVISION
        conn.execute(
            """
            UPDATE creative_generation_tasks
            SET payload_json = ?, prompt_package_json = ?, generation_mode = ?,
                source_image_id = ?, source_image_signed_url = ?, source_image_hash = ?,
                source_image_required = 1, updated_at = ?
            WHERE task_id = ?
            """,
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                json.dumps(prompt_package, ensure_ascii=False, sort_keys=True),
                GENERATION_MODE_OLD_IMAGE_REFERENCE_REVISION,
                source_fields['source_image_id'],
                source_fields['source_image_signed_url'],
                source_fields['source_image_hash'],
                utc_now(),
                task_id,
            ),
        )
    if commit:
        conn.commit()
    return {
        'ok': True,
        'task': get_creative_generation_task(conn, task_id),
        'source_image_refreshed': True,
        'source_image': source_image,
    }


def retry_hermes_image2_generation_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    retry_by: str = '',
    reset_attempts: bool = True,
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    task = get_creative_generation_task(conn, task_id)
    if task['status'] not in {
        HERMES_TASK_STATUS_FAILED,
        HERMES_TASK_STATUS_REJECTED,
        HERMES_TASK_STATUS_CANCELLED,
        HERMES_TASK_STATUS_EXPIRED,
    }:
        raise ValueError('creative_generation_task_not_retryable')
    try:
        refreshed = refresh_hermes_image2_source_snapshot(conn, task_id, update_task=True, commit=False)
        task = dict(refreshed.get('task') or task)
        now = utc_now()
        updated = conn.execute(
            """
            UPDATE creative_generation_tasks
            SET status = ?, lease_owner = '', lease_expires_at = '', heartbeat_at = '', error_code = '',
                error_message = '', finished_at = '', attempt_count = CASE WHEN ? THEN 0 ELSE attempt_count END,
                updated_at = ?
            WHERE task_id = ? AND status IN ('failed', 'rejected', 'cancelled', 'expired')
            """,
            (HERMES_TASK_STATUS_QUEUED, 1 if reset_attempts else 0, now, task_id),
        )
        if updated.rowcount != 1:
            raise ValueError('creative_generation_task_not_retryable')
        job = get_chatgpt_pro_job(conn, task['job_id'])
        refreshed_payload = _hermes_task_prompt_package(
            job,
            image_size=str(task.get('image_size') or '1024x1024'),
            candidate_count=int(task.get('candidate_count') or 1),
        )
        refreshed_prompt_package = dict(refreshed_payload.get('prompt_package') or {})
        conn.execute(
            """
            UPDATE creative_generation_tasks
            SET payload_json = ?, prompt_package_json = ?, final_prompt = ?,
                currency_threshold_version = ?, updated_at = ?
            WHERE task_id = ? AND status = ?
            """,
            (
                json.dumps(refreshed_payload, ensure_ascii=False, sort_keys=True),
                json.dumps(refreshed_prompt_package, ensure_ascii=False, sort_keys=True),
                str(refreshed_prompt_package.get('final_prompt') or refreshed_payload.get('prompt') or ''),
                CURRENCY_THRESHOLD_VERSION,
                now,
                task_id,
                HERMES_TASK_STATUS_QUEUED,
            ),
        )
        conn.execute(
            "UPDATE creative_pro_work_queue SET status = 'generating', provider_mode = ?, error_code = '', error_message = '' WHERE job_id = ?",
            (PROVIDER_HERMES_IMAGE2_AGENT, task['job_id']),
        )
        _append_hermes_task_history(conn, task_id, status=HERMES_TASK_STATUS_QUEUED, at=now, meta={'retry_by': retry_by, 'reset_attempts': bool(reset_attempts)})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_creative_generation_task(conn, task_id)


def list_hermes_image2_generation_tasks(
    conn: sqlite3.Connection,
    *,
    job_id: str = '',
    status: str = '',
    limit: int = 50,
) -> List[Dict[str, Any]]:
    ensure_creative_image_generation_tables(conn)
    clauses = ["provider_mode = ?"]
    params: List[Any] = [PROVIDER_HERMES_IMAGE2_AGENT]
    if job_id:
        clauses.append("job_id = ?")
        params.append(job_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    params.append(max(1, min(int(limit or 50), 200)))
    rows = conn.execute(
        f"""
        SELECT * FROM creative_generation_tasks
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_hermes_generation_task_from_row(row) for row in rows]


def _parse_quality_evaluation(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        return {}


VISIBLE_TEXT_RISK_PATTERNS = [
    ('creator_recruitment', re.compile(r'\bcreator\b|criador|criadora|creador|creadora|主播|陪聊|host recruitment|anfitri[aã]o|anfitri[oó]n', re.I)),
    ('mcn_or_guild', re.compile(r'\bMCN\b|guild|公会|公會|gremio|guilda', re.I)),
    ('chat_to_earn', re.compile(r'chat\s*to\s*earn|ganhar dinheiro conversando|ganar dinero chateando|聊天赚钱|聊天賺錢', re.I)),
    ('withdraw_proof', re.compile(r'withdraw|saque|retirada|retirar|提现|提款|tarik dana|bukti penarikan', re.I)),
]

SAFE_COMPLIANCE_VISIBLE_TEXT_RISK_PATTERNS = [
    ('financial', re.compile(r'R\$|Rp\b|\$|€|£|MXN|BRL|IDR|dinheiro|dinero|uang|money|cash|saldo|balance|wallet|carteira|dompet', re.I)),
    ('employment', re.compile(r'job|work\s*from\s*home|employment|vacancy|hiring|recruit|trabalho|emprego|vaga|contratando|trabajo|empleo|vacante|kerja|pekerjaan|lowongan', re.I)),
    ('income', re.compile(r'earn|earning|income|ganhe|ganhar|gana|ganar|penghasilan|pendapatan|dapat uang|renda|ingreso', re.I)),
    ('guaranteed_benefit', re.compile(r'garantid[oa]|garantizado|guaranteed|pasti dapat|fixed reward|recompensa fixa|reward tetap', re.I)),
    ('invented_function', re.compile(r'not[ií]cias?|receitas?|viage(?:m|ns)|compras?|cursos?|entretenimento|decora[cç][aã]o|news|recipes?|travel|shopping|courses?|entertainment|home\s*decor|berita|resep|perjalanan|belanja|hiburan', re.I)),
]

MALE_PERSON_EVALUATION_TERMS = {
    'male',
    'man',
    'boy',
    'masculine',
    'male_main_character',
    'male character',
    'male model',
    'male hero',
    '男性',
    '男生',
    '男人',
    '男主角',
}


def _quality_visible_text(evaluation: Dict[str, Any]) -> str:
    values: List[str] = []
    for key in ['ocr_text', 'visible_text', 'extracted_text', 'text', 'image_text']:
        value = evaluation.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value if str(item).strip())
        elif value:
            values.append(str(value))
    return '\n'.join(values)


def _parse_currency_amount(value: str, market: str = '') -> Optional[float]:
    normalized = str(value or '').strip().replace(' ', '')
    if not normalized:
        return None
    if market == 'ID' and (
        re.fullmatch(r'[0-9]{1,3}(?:\.[0-9]{3})+', normalized)
        or re.fullmatch(r'[0-9]{1,3}(?:,[0-9]{3})+', normalized)
    ):
        normalized = normalized.replace('.', '').replace(',', '')
    elif ',' in normalized and '.' in normalized:
        if normalized.rfind(',') > normalized.rfind('.'):
            normalized = normalized.replace('.', '').replace(',', '.')
        else:
            normalized = normalized.replace(',', '')
    elif ',' in normalized:
        normalized = normalized.replace('.', '').replace(',', '.')
    else:
        parts = normalized.split('.')
        if len(parts) > 2:
            normalized = ''.join(parts)
    try:
        return float(normalized)
    except ValueError:
        return None


def _currency_reward_review(text: str, market: str) -> Dict[str, Any]:
    thresholds = CURRENCY_REWARD_THRESHOLDS.get(market) or {}
    if not thresholds:
        return {'status': 'not_applicable', 'amounts': [], 'reason': 'market_without_currency_threshold'}
    amounts: List[Dict[str, Any]] = []
    if market == 'BR':
        pattern = re.compile(r'R\$\s*([0-9][0-9.,]*)', re.I)
    elif market == 'ID':
        pattern = re.compile(r'Rp\s*([0-9][0-9.,]*)', re.I)
    elif market == 'MX':
        pattern = re.compile(r'(?:MX\$|MXN\s*\$?|\$)\s*([0-9][0-9.,]*)', re.I)
    elif market == 'CO':
        pattern = re.compile(r'(?:COP\s*\$?|\$)\s*([0-9][0-9.,]*)(?:\s*COP)?', re.I)
    else:
        pattern = re.compile(r'\b([0-9][0-9.,]*)\s*(?:pts|pontos|puntos|punto|points|poin)\b', re.I)
    for match in pattern.finditer(text or ''):
        amount = _parse_currency_amount(match.group(1), market)
        if amount is not None:
            lower_before = (text or '').lower()[:match.start()]
            bucket_markers = {
                'task_reward': ['task', 'tarea', 'tarefa', 'tugas', 'completed', 'completada', 'concluída', 'selesai'],
                'daily_reward': ['day', 'daily', 'dia', 'hari', 'por día', 'per day', 'diária'],
                'wallet_balance': ['wallet', 'saldo', 'balance', 'carteira', 'dompet'],
            }
            nearest = [
                (lower_before.rfind(token), bucket)
                for bucket, tokens in bucket_markers.items()
                for token in tokens
                if lower_before.rfind(token) >= 0
            ]
            bucket = max(nearest, default=(-1, 'task_reward'))[1]
            amounts.append({'raw': match.group(0), 'amount': amount, 'bucket': bucket})
    if not amounts:
        return {'status': 'passed', 'amounts': [], 'reason': 'no_visible_reward_amount'}
    statuses: List[str] = []
    for item in amounts:
        bucket = str(item.get('bucket') or 'task_reward')
        rule = dict(thresholds.get(bucket) or {})
        pass_min = rule.get('pass_min')
        pass_max = rule.get('pass_max')
        manual_max = rule.get('manual_max')
        if pass_min is not None and item['amount'] < float(pass_min):
            item['status'] = 'auto_rejected'
        elif pass_max is not None and item['amount'] <= float(pass_max):
            item['status'] = 'passed'
        elif manual_max is not None and item['amount'] <= float(manual_max):
            item['status'] = 'manual_review_required'
        else:
            item['status'] = 'auto_rejected'
        statuses.append(item['status'])
    buckets = sorted({str(item.get('bucket') or 'task_reward') for item in amounts})
    bucket = buckets[0] if len(buckets) == 1 else 'mixed'
    if 'auto_rejected' in statuses:
        return {'status': 'auto_rejected', 'amounts': amounts, 'bucket': bucket, 'reason': 'amount_exceeds_threshold'}
    if 'manual_review_required' in statuses:
        return {'status': 'manual_review_required', 'amounts': amounts, 'bucket': bucket, 'reason': 'within_manual_review_threshold'}
    return {'status': 'passed', 'amounts': amounts, 'bucket': bucket, 'reason': 'within_pass_threshold'}


def _task_market_from_payload(task_payload: Optional[Dict[str, Any]]) -> str:
    payload = dict(task_payload or {})
    prompt_package = dict(payload.get('prompt_package') or {})
    country_market, _ = normalize_market(
        payload.get('country') or prompt_package.get('country'),
        payload.get('project'),
    )
    if country_market:
        return country_market
    return str(prompt_package.get('market') or '').strip()


def _task_direction_from_payload(task_payload: Optional[Dict[str, Any]]) -> str:
    payload = dict(task_payload or {})
    prompt_package = dict(payload.get('prompt_package') or {})
    return str(payload.get('creative_direction') or prompt_package.get('creative_direction') or '').strip()


def validate_hermes_image2_uploaded_image_quality(
    content: bytes,
    content_type: str,
    quality_evaluation: Optional[Dict[str, Any]] = None,
    *,
    task_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    detected = _detect_uploaded_image_content_type(content)
    if content_type and str(content_type).lower() not in {'image/png', 'image/jpeg', 'image/jpg', 'image/webp'}:
        raise InvalidCreativeImageError('unsupported_image_type')
    width, height = _validate_chatgpt_pro_uploaded_image_quality(content, detected)
    if (width, height) != (1024, 1024):
        raise InvalidCreativeImageError('invalid_image_dimensions')
    evaluation = _parse_quality_evaluation(quality_evaluation)
    direction_key = _task_direction_from_payload(task_payload)
    verdict = str(evaluation.get('verdict') or evaluation.get('status') or '').strip().lower()
    failed_tags = [
        str(tag).strip()
        for tag in (evaluation.get('failed_tags') or evaluation.get('risk_tags') or [])
        if str(tag).strip()
    ]
    l3_visual_review = (
        {}
        if direction_key == CREATIVE_DIRECTION_SAFE_COMPLIANCE
        else evaluation.get('l3_visual_review') if isinstance(evaluation.get('l3_visual_review'), dict) else {}
    )
    l3_status = str(l3_visual_review.get('l3_visual_review_status') or l3_visual_review.get('status') or '').strip().lower()
    l3_verdict = str(l3_visual_review.get('verdict') or '').strip().lower()
    gender_values = [
        evaluation.get('primary_person_gender'),
        evaluation.get('main_person_gender'),
        evaluation.get('person_gender'),
        l3_visual_review.get('primary_person_gender'),
        l3_visual_review.get('main_person_gender'),
        l3_visual_review.get('person_gender'),
    ]
    gender_text = ' '.join(str(value or '').strip().lower() for value in gender_values if str(value or '').strip())
    gender_tokens = {
        token
        for token in re.split(r'[\s,;/|]+', gender_text)
        if token
    }
    gender_phrases = {
        term
        for term in MALE_PERSON_EVALUATION_TERMS
        if ' ' in term or '_' in term or any('\u4e00' <= ch <= '\u9fff' for ch in term)
    }
    if gender_text and (gender_tokens & MALE_PERSON_EVALUATION_TERMS or any(term in gender_text for term in gender_phrases)):
        failed_tags.append('male_main_character')
    if l3_visual_review and (l3_status in {'failed', 'fail'} or l3_verdict in {'fail', 'failed', 'reject', 'rejected'}):
        l3_failed_tags = [
            str(tag).strip()
            for tag in (l3_visual_review.get('failed_tags') or ['l3_visual_review_failed'])
            if str(tag).strip()
        ]
        failed_tags.extend(l3_failed_tags or ['l3_visual_review_failed'])
    hard_fail = {
        'missing_person',
        'missing_phone_ui',
        'missing_headline',
        'missing_brand_footer',
        'insufficient_task_information_density',
        'missing_named_task_rows',
        'missing_task_completion_states',
        'missing_progress_summary',
        'missing_points_state',
        'missing_reward_destination',
        'phone_product_area_out_of_range',
        'male_main_character',
        'icon_or_flowchart',
        'cartoon_placeholder',
        'ui_only',
        'store_screenshot_style',
        'risky_income_claim',
        'pii_or_contact',
    }
    if verdict in {'fail', 'failed', 'reject', 'rejected'} or any(tag in hard_fail for tag in failed_tags):
        error_code = str(evaluation.get('error_code') or (failed_tags[0] if failed_tags else 'semantic_quality_failed'))
        raise InvalidCreativeImageError(error_code)
    visible_text = _quality_visible_text(evaluation)
    text_risk_tags = [
        tag for tag, pattern in [*PII_PATTERNS, *INCOME_RISK_PATTERNS, *VISIBLE_TEXT_RISK_PATTERNS]
        if pattern.search(visible_text)
    ]
    if text_risk_tags:
        raise InvalidCreativeImageError(text_risk_tags[0])
    if direction_key == CREATIVE_DIRECTION_SAFE_COMPLIANCE:
        safe_text_risk_tags = [
            tag for tag, pattern in SAFE_COMPLIANCE_VISIBLE_TEXT_RISK_PATTERNS
            if pattern.search(visible_text)
        ]
        if safe_text_risk_tags:
            raise InvalidCreativeImageError(f'safe_compliance_{safe_text_risk_tags[0]}')
    market = _task_market_from_payload(task_payload)
    currency_review = _currency_reward_review(visible_text, market)
    final_verdict = FINAL_VERDICT_PENDING_REVIEW
    if currency_review['status'] == 'manual_review_required':
        final_verdict = FINAL_VERDICT_MANUAL_REVIEW_REQUIRED
    elif currency_review['status'] == 'auto_rejected':
        raise InvalidCreativeImageError('currency_reward_exceeds_threshold')
    if l3_visual_review and (l3_status in {'manual_review_required', 'manual_review'} or l3_verdict == 'manual_review' or evaluation.get('l3_manual_review_required')):
        final_verdict = FINAL_VERDICT_MANUAL_REVIEW_REQUIRED
    if direction_key == CREATIVE_DIRECTION_SAFE_COMPLIANCE:
        prompt_package = dict((task_payload or {}).get('prompt_package') or {})
        brand_assets = list(prompt_package.get('brand_assets') or [])
        expected_logo_hash = str((brand_assets[0] if brand_assets else {}).get('sha256') or '').strip()
        actual_logo_hash = str(evaluation.get('official_logo_hash') or '').strip()
        official_logo_used = str(evaluation.get('official_logo_used') or '').strip().lower() in {'1', 'true', 'yes', 'used'}
        logo_composite_applied = str(evaluation.get('official_logo_composite_applied') or '').strip().lower() in {'1', 'true', 'yes', 'applied'}
        logo_composite_version = str(evaluation.get('official_logo_composite_version') or '').strip()
        if expected_logo_hash and actual_logo_hash and expected_logo_hash != actual_logo_hash:
            raise InvalidCreativeImageError('safe_compliance_logo_hash_mismatch')
        try:
            logo_area_ratio = float(evaluation.get('logo_area_ratio') or l3_visual_review.get('logo_area_ratio') or -1)
        except Exception:
            logo_area_ratio = -1
        if logo_area_ratio > 0.12:
            raise InvalidCreativeImageError('safe_compliance_logo_too_dominant')
        if logo_composite_applied and logo_composite_version != 'official_logo_wordmark_footer_v4':
            raise InvalidCreativeImageError('safe_compliance_logo_composite_version_invalid')
        if logo_composite_applied and (not official_logo_used or not actual_logo_hash or logo_area_ratio <= 0):
            raise InvalidCreativeImageError('safe_compliance_logo_composite_evidence_invalid')
        brand_name_composite_applied = str(evaluation.get('official_brand_name_composite_applied') or '').strip().lower() in {'1', 'true', 'yes', 'applied'}
        official_brand_name_text = str(evaluation.get('official_brand_name_text') or '').strip()
        expected_brand_names = {'Premiou', 'TUGAO', 'Recompa'}
        if not logo_composite_applied or not brand_name_composite_applied or official_brand_name_text not in expected_brand_names:
            raise InvalidCreativeImageError('safe_compliance_complete_brand_footer_required')
    semantic_status = 'provider_asserted' if evaluation else 'pending_manual_review'
    direction_fit_status = str(evaluation.get('direction_fit_status') or evaluation.get('direction_fit') or semantic_status)
    public_positioning_fit_status = str(evaluation.get('public_positioning_fit_status') or evaluation.get('public_positioning_fit') or semantic_status)
    old_image_preservation_status = 'not_applicable'
    if (task_payload or {}).get('generation_mode') == GENERATION_MODE_OLD_IMAGE_REFERENCE_REVISION:
        source_image = dict((task_payload or {}).get('source_image') or {})
        expected_source_hash = str(source_image.get('source_image_hash') or (task_payload or {}).get('source_image_hash') or '').strip()
        actual_source_hash = str(evaluation.get('source_image_hash') or evaluation.get('reference_image_hash') or '').strip()
        source_image_used = str(evaluation.get('source_image_used') or '').strip().lower() in {'1', 'true', 'yes', 'used'}
        if not source_image_used:
            raise InvalidCreativeImageError('source_image_not_used')
        if expected_source_hash and actual_source_hash and expected_source_hash != actual_source_hash:
            raise InvalidCreativeImageError('source_image_hash_mismatch')
        preservation_failed_tags = {
            str(tag).strip()
            for tag in (evaluation.get('old_image_preservation_failed_tags') or evaluation.get('preservation_failed_tags') or [])
            if str(tag).strip()
        }
        if preservation_failed_tags & {'direction_drift', 'brand_footer_lost', 'phone_ui_lost', 'person_region_lost'}:
            raise InvalidCreativeImageError(sorted(preservation_failed_tags)[0])
        try:
            layout_similarity_score = float(evaluation.get('layout_similarity_score') or evaluation.get('old_image_layout_similarity') or -1)
        except Exception:
            layout_similarity_score = -1
        old_image_preservation_status = str(evaluation.get('old_image_preservation_status') or evaluation.get('old_image_preservation') or '').strip()
        if layout_similarity_score >= 0:
            if layout_similarity_score < 0.35:
                raise InvalidCreativeImageError('old_image_layout_similarity_too_low')
            if layout_similarity_score < 0.55 and final_verdict == FINAL_VERDICT_PENDING_REVIEW:
                final_verdict = FINAL_VERDICT_MANUAL_REVIEW_REQUIRED
                old_image_preservation_status = old_image_preservation_status or 'manual_review_required'
        old_image_preservation_status = old_image_preservation_status or 'provider_asserted'
    return {
        'file_quality_status': 'passed',
        'semantic_quality_status': semantic_status,
        'ocr_text_check_status': 'passed',
        'currency_reward_check_status': currency_review['status'],
        'direction_fit_status': direction_fit_status,
        'public_positioning_fit_status': public_positioning_fit_status,
        'visible_risk_status': 'passed',
        'old_image_preservation_status': old_image_preservation_status,
        'final_verdict': final_verdict,
        'validation_layers': {
            'L0_file_quality': 'passed',
            'L1_ocr_text_risk': 'passed',
            'L2_currency_reward': currency_review,
            'L3_provider_semantic': semantic_status,
            'L4_human_review': final_verdict,
        },
        'content_type': detected,
        'width': width,
        'height': height,
        'image_size': f'{width}x{height}',
        'file_size_bytes': len(content or b''),
        'visible_text': visible_text,
        'provider_evaluation': evaluation,
    }


def save_hermes_image2_uploaded_image(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    filename: str,
    content: bytes,
    content_type: str = '',
    provider_session_id: str = '',
    candidate_index: int = 0,
    quality_evaluation: Optional[Dict[str, Any]] = None,
    source_image_used: bool = False,
    source_image_hash: str = '',
    output_dir: Path = DEFAULT_IMAGE_OUTPUT_DIR,
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    task = get_creative_generation_task(conn, task_id)
    job = task['job']
    now = utc_now()
    generation_mode = str(task.get('generation_mode') or (task.get('payload') or {}).get('generation_mode') or GENERATION_MODE_NEW_DIRECTION)
    creative_direction = str(task.get('creative_direction') or (task.get('payload') or {}).get('creative_direction') or CREATIVE_DIRECTION_POINTS_REWARD)
    try:
        task_payload = dict(task.get('payload') or {})
        task_payload.setdefault('generation_mode', task.get('generation_mode'))
        task_payload.setdefault('creative_direction', task.get('creative_direction'))
        task_payload.setdefault('source_image_hash', task.get('source_image_hash'))
        task_payload.setdefault('source_image', {
            'source_image_hash': task.get('source_image_hash'),
            'source_image_id': task.get('source_image_id'),
            'source_image_signed_url': task.get('source_image_signed_url'),
            'source_image_required': task.get('source_image_required'),
        })
        merged_quality_evaluation = _parse_quality_evaluation(quality_evaluation)
        if source_image_used:
            merged_quality_evaluation['source_image_used'] = True
        if str(source_image_hash or '').strip():
            merged_quality_evaluation['source_image_hash'] = str(source_image_hash or '').strip()
        quality = validate_hermes_image2_uploaded_image_quality(content, content_type, merged_quality_evaluation, task_payload=task_payload)
        optimized_content, compression_meta = _optimize_creative_image_bytes(content or b'', quality['content_type'])
        proposed_image_hash = hashlib.sha256(optimized_content).hexdigest()
        duplicate = conn.execute(
            """
            SELECT image_id,creative_direction FROM creative_generated_images
            WHERE image_hash = ? AND creative_direction <> ?
              AND review_status NOT IN ('rejected','archived')
            ORDER BY created_at DESC LIMIT 1
            """,
            (proposed_image_hash, creative_direction),
        ).fetchone()
        if duplicate:
            raise InvalidCreativeImageError('duplicate_creative_across_directions')
    except InvalidCreativeImageError as exc:
        conn.execute(
            """
            UPDATE creative_generation_tasks
            SET status = ?, rejected_candidate_count = rejected_candidate_count + 1, error_code = ?,
                error_message = ?, quality_summary_json = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (
                HERMES_TASK_STATUS_REJECTED,
                str(exc),
                str(exc),
                json.dumps({'error_code': str(exc), 'provider_evaluation': _parse_quality_evaluation(quality_evaluation)}, ensure_ascii=False, sort_keys=True),
                now,
                task_id,
            ),
        )
        conn.execute(
            "UPDATE creative_pro_work_queue SET status = 'failed', error_code = ?, error_message = ?, completed_at = ? WHERE job_id = ?",
            (str(exc), 'hermes_uploaded_image_rejected', now, job['job_id']),
        )
        conn.commit()
        raise
    suffix = IMAGE_PROVIDER_BINARY_TYPES.get(quality['content_type'], '.png')
    output_dir.mkdir(parents=True, exist_ok=True)
    image_id = f'pro_img_{stable_id(task_id, filename, provider_session_id, candidate_index, hashlib.sha256(content or b"").hexdigest())}'
    image_path = output_dir / f'{image_id}{suffix}'
    image_path.write_bytes(optimized_content)
    thumbnail_ref, thumbnail_meta = _write_creative_image_thumbnail(optimized_content, output_dir / f'{image_id}_thumb.webp')
    image_hash = file_sha256(image_path)
    request_id = str(task.get('generation_request_id') or _chatgpt_pro_generation_request_id(job))
    final_verdict = str(quality.get('final_verdict') or FINAL_VERDICT_PENDING_REVIEW)
    review_status = FINAL_VERDICT_MANUAL_REVIEW_REQUIRED if final_verdict == FINAL_VERDICT_MANUAL_REVIEW_REQUIRED else FINAL_VERDICT_PENDING_REVIEW
    source_image_was_used = bool(source_image_used or _parse_quality_evaluation(quality_evaluation).get('source_image_used'))
    used_source_image_hash = str(source_image_hash or _parse_quality_evaluation(quality_evaluation).get('source_image_hash') or '')
    conn.execute(
        "UPDATE creative_generation_requests SET status = ?, image_size = ?, review_status = ?, updated_at = ? WHERE request_id = ?",
        ('generated', quality['image_size'], review_status, now, request_id),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO creative_generated_images
        (image_id, request_id, surface, image_size, market, brand, image_ref, thumbnail_ref, prompt_hash,
         risk_status, risk_tags_json, review_status, provider, metadata_json, created_at, image_hash,
         final_delivery_hash, source_provider, uploaded_manually, uploaded_final_version, is_exact_generated_asset,
         task_id, generation_mode, creative_direction, candidate_index, width, height, file_path, thumbnail_path,
         file_size_bytes, file_quality_status, ocr_text_check_status, currency_reward_check_status,
         direction_fit_status, public_positioning_fit_status, visible_risk_status, old_image_preservation_status,
         quality_check_json, final_verdict)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            image_id,
            request_id,
            FEED_STATIC_AD_SURFACE,
            quality['image_size'],
            str(job.get('country') or ''),
            str(job.get('brand_display_name') or ''),
            str(image_path),
            thumbnail_ref or str(image_path),
            stable_id(job['job_id'], task_id, 'hermes_image2'),
            review_status,
            '[]',
            review_status,
            PROVIDER_HERMES_IMAGE2_AGENT,
            json.dumps({
                'job_id': job['job_id'],
                'task_id': task_id,
                'filename': filename,
                'provider_session_id': provider_session_id,
                'candidate_index': candidate_index,
                'source_image_used': source_image_was_used,
                'source_image_hash': used_source_image_hash,
                'quality_summary': quality,
                'compression': compression_meta,
                'thumbnail': thumbnail_meta,
                'external_write_performed': False,
            }, ensure_ascii=False, sort_keys=True),
            now,
            image_hash,
            image_hash,
            PROVIDER_HERMES_IMAGE2_AGENT,
            0,
            1,
            1,
            task_id,
            generation_mode,
            creative_direction,
            int(candidate_index or 0),
            int(quality.get('width') or 0),
            int(quality.get('height') or 0),
            str(image_path),
            thumbnail_ref or str(image_path),
            int(quality.get('file_size_bytes') or len(optimized_content or b'')),
            str(quality.get('file_quality_status') or ''),
            str(quality.get('ocr_text_check_status') or ''),
            str(quality.get('currency_reward_check_status') or ''),
            str(quality.get('direction_fit_status') or ''),
            str(quality.get('public_positioning_fit_status') or ''),
            str(quality.get('visible_risk_status') or ''),
            str(quality.get('old_image_preservation_status') or ''),
            json.dumps(quality, ensure_ascii=False, sort_keys=True),
            final_verdict,
        ),
    )
    conn.execute(
        """
        UPDATE creative_generation_tasks
        SET status = ?, accepted_image_count = accepted_image_count + 1, error_code = '', error_message = '',
            quality_summary_json = ?, provider_response_json = ?,
            source_image_used = CASE WHEN ? THEN 1 ELSE source_image_used END,
            finished_at = ?, updated_at = ?
        WHERE task_id = ?
        """,
        (
            HERMES_TASK_STATUS_UPLOADED,
            json.dumps(quality, ensure_ascii=False, sort_keys=True),
            json.dumps({
                'image_id': image_id,
                'provider_session_id': provider_session_id,
                'final_verdict': final_verdict,
                'source_image_used': source_image_was_used,
                'source_image_hash': used_source_image_hash,
            }, ensure_ascii=False, sort_keys=True),
            1 if source_image_was_used else 0,
            now,
            now,
            task_id,
        ),
    )
    conn.execute(
        """
        UPDATE creative_pro_work_queue
        SET status = 'pending_review', provider_mode = ?, completed_at = '', error_code = '', error_message = ''
        WHERE job_id = ?
        """,
        (PROVIDER_HERMES_IMAGE2_AGENT, job['job_id']),
    )
    conn.execute(
        "UPDATE creative_experiment_suggestions SET generated_image_id = ?, generation_request_id = ?, updated_at = ? WHERE experiment_id = ?",
        (image_id, request_id, now, job['experiment_id']),
    )
    source_cleanup = cleanup_temporary_creative_source_images(conn, job_id=job['job_id'], task_id=task_id)
    conn.commit()
    image = latest_generated_images(conn, limit=1)[0]
    return {
        'ok': True,
        'task': get_creative_generation_task(conn, task_id),
        'job': get_chatgpt_pro_job(conn, job['job_id']),
        'image': image,
        'quality_summary': quality,
        'source_cleanup': source_cleanup,
        'external_write_performed': False,
    }


def latest_generated_images(conn: sqlite3.Connection, *, limit: int = 12, target_app: str = 'all') -> List[Dict[str, Any]]:
    ensure_creative_image_generation_tables(conn)
    normalized_target_app = _normalize_target_app(target_app)
    requested_limit = max(1, min(int(limit or 12), 100))
    rows = conn.execute(
        """
        SELECT image_id, request_id, surface, image_size, market, brand, image_ref, thumbnail_ref,
               prompt_hash, risk_status, risk_tags_json, review_status, provider, metadata_json, created_at,
               image_hash, perceptual_hash, final_delivery_hash, uploaded_manually, uploaded_final_version,
               is_exact_generated_asset, task_id, generation_mode, creative_direction, candidate_index,
               width, height, file_size_bytes, file_quality_status, ocr_text_check_status,
               currency_reward_check_status, direction_fit_status, public_positioning_fit_status,
               visible_risk_status, old_image_preservation_status, quality_check_json, final_verdict
        FROM creative_generated_images
        WHERE COALESCE(LOWER(review_status), '') NOT IN ('deleted','archived')
          AND NOT EXISTS (
              SELECT 1
              FROM creative_pro_work_queue q
              WHERE q.job_id = json_extract(creative_generated_images.metadata_json, '$.job_id')
                AND q.status = 'deleted'
          )
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (requested_limit if normalized_target_app in {'', 'all'} else min(requested_limit * 5, 300),),
    ).fetchall()
    images = [
        {
            'image_id': row['image_id'],
            'request_id': row['request_id'],
            'surface': row['surface'],
            'surface_label': '信息流广告图',
            'image_size': row['image_size'],
            'market': row['market'],
            'brand': row['brand'],
            'image_ref': row['image_ref'],
            'thumbnail_ref': row['thumbnail_ref'],
            'prompt_hash': row['prompt_hash'],
            'risk_status': row['risk_status'],
            'risk_tags': json.loads(row['risk_tags_json'] or '[]'),
            'review_status': row['review_status'],
            'provider': row['provider'],
            'metadata': json.loads(row['metadata_json'] or '{}'),
            'image_hash': row['image_hash'],
            'perceptual_hash': row['perceptual_hash'],
            'final_delivery_hash': row['final_delivery_hash'],
            'uploaded_manually': bool(row['uploaded_manually']),
            'uploaded_final_version': bool(row['uploaded_final_version']),
            'is_exact_generated_asset': bool(row['is_exact_generated_asset']),
            'task_id': row['task_id'],
            'generation_mode': row['generation_mode'],
            'creative_direction': row['creative_direction'],
            'candidate_index': int(row['candidate_index'] or 0),
            'width': int(row['width'] or 0),
            'height': int(row['height'] or 0),
            'file_size_bytes': int(row['file_size_bytes'] or 0),
            'file_quality_status': row['file_quality_status'],
            'ocr_text_check_status': row['ocr_text_check_status'],
            'currency_reward_check_status': row['currency_reward_check_status'],
            'direction_fit_status': row['direction_fit_status'],
            'public_positioning_fit_status': row['public_positioning_fit_status'],
            'visible_risk_status': row['visible_risk_status'],
            'old_image_preservation_status': row['old_image_preservation_status'],
            'quality_check': json.loads(row['quality_check_json'] or '{}'),
            'final_verdict': row['final_verdict'] or row['review_status'],
            'created_at': row['created_at'],
        }
        for row in rows
    ]
    enrich_creative_image_names(conn, images)
    for image in images:
        image['target_app'] = creative_image_target_app(image)
    image_ids = [str(image.get('image_id') or '') for image in images if str(image.get('image_id') or '')]
    if image_ids:
        placeholders = ','.join('?' for _ in image_ids)
        adoption_rows = conn.execute(
            f"""
            SELECT *
            FROM creative_adoption_records
            WHERE image_id IN ({placeholders})
            ORDER BY adopted_at DESC
            """,
            tuple(image_ids),
        ).fetchall()
        latest_adoptions: Dict[str, Dict[str, Any]] = {}
        for row in adoption_rows:
            image_id = str(row['image_id'] or '')
            if not image_id or image_id in latest_adoptions:
                continue
            inferred = infer_generated_image_experiment_context(
                conn,
                image_id=image_id,
                request_id=str(row['request_id'] or ''),
                experiment_id=str(row['experiment_id'] or ''),
                experiment_code=str(row['experiment_code'] or ''),
                suggestion_id=str(row['suggestion_id'] or ''),
            )
            latest_adoptions[image_id] = {
                'adoption_id': row['adoption_id'],
                'ad_id': row['ad_id'],
                'creative_id': row['creative_id'],
                'adset_id': row['adset_id'],
                'campaign_id': row['campaign_id'],
                'status': row['status'],
                'adoption_type': row['adoption_type'],
                'binding_method': row['binding_method'],
                'binding_confidence': row['binding_confidence'],
                'binding_status': row['binding_status'],
                'adopted_at': row['adopted_at'],
                'matched_at': row['matched_at'],
                'experiment_id': row['experiment_id'] or inferred.get('experiment_id', ''),
                'experiment_code': row['experiment_code'] or inferred.get('experiment_code', ''),
                'suggestion_id': row['suggestion_id'] or inferred.get('suggestion_id', ''),
                'experiment_inferred': not bool(row['experiment_id']) and bool(inferred.get('experiment_id')),
                'job_id': inferred.get('job_id', ''),
                'generated_image_id': row['generated_image_id'] or row['image_id'],
                'cleanup_after': str(_json_load(row['payload_json'], {}).get('cleanup_after') or ''),
            }
        for image in images:
            image['latest_adoption'] = latest_adoptions.get(str(image.get('image_id') or '')) or None
    if normalized_target_app not in {'', 'all'}:
        images = [image for image in images if image.get('target_app') == normalized_target_app]
    return images[:requested_limit]


REVIEW_STATUS_ZH = {
    'DRAFT': '草稿',
    'GENERATED': '已生成',
    'NEEDS_REVIEW': '待审核',
    'APPROVED': '已通过',
    'REJECTED': '已拒绝',
    'USED_IN_AD': '已用于广告',
    'ARCHIVED': '已归档',
}


def infer_generated_image_experiment_context(
    conn: sqlite3.Connection,
    *,
    image_id: str,
    request_id: str = '',
    experiment_id: str = '',
    experiment_code: str = '',
    suggestion_id: str = '',
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    context: Dict[str, Any] = {
        'image_id': str(image_id or '').strip(),
        'request_id': str(request_id or '').strip(),
        'experiment_id': str(experiment_id or '').strip(),
        'experiment_code': str(experiment_code or '').strip(),
        'experiment_mode': '',
        'suggestion_id': str(suggestion_id or '').strip(),
        'job_id': '',
    }
    image = conn.execute(
        "SELECT request_id, metadata_json FROM creative_generated_images WHERE image_id = ?",
        (context['image_id'],),
    ).fetchone()
    metadata: Dict[str, Any] = {}
    if image:
        context['request_id'] = context['request_id'] or str(image['request_id'] or '').strip()
        loaded = _json_load(image['metadata_json'], {})
        metadata = loaded if isinstance(loaded, dict) else {}
        context['job_id'] = str(metadata.get('job_id') or metadata.get('creative_pro_job_id') or '').strip()
        context['experiment_id'] = context['experiment_id'] or str(metadata.get('experiment_id') or '').strip()
        context['experiment_code'] = context['experiment_code'] or str(metadata.get('experiment_code') or '').strip()
        context['suggestion_id'] = context['suggestion_id'] or str(metadata.get('suggestion_id') or metadata.get('recommendation_id') or '').strip()
    job = None
    if context['job_id']:
        job = conn.execute(
            """
            SELECT job_id, experiment_id, experiment_code, recommendation_id
            FROM creative_pro_work_queue
            WHERE job_id = ?
            """,
            (context['job_id'],),
        ).fetchone()
    if not job and context['request_id']:
        suffix = context['request_id'].replace('creative_req_', '', 1)
        job = conn.execute(
            """
            SELECT job_id, experiment_id, experiment_code, recommendation_id
            FROM creative_pro_work_queue
            WHERE job_id = ? OR job_id LIKE ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (context['request_id'], f'%{suffix}%'),
        ).fetchone()
    if job:
        context['job_id'] = context['job_id'] or str(job['job_id'] or '').strip()
        context['experiment_id'] = context['experiment_id'] or str(job['experiment_id'] or '').strip()
        context['experiment_code'] = context['experiment_code'] or str(job['experiment_code'] or '').strip()
        context['suggestion_id'] = context['suggestion_id'] or str(job['recommendation_id'] or '').strip()
    if context['experiment_id']:
        exp = conn.execute(
            """
            SELECT experiment_id, experiment_code, suggestion_id, experiment_mode
            FROM creative_experiment_suggestions
            WHERE experiment_id = ?
            """,
            (context['experiment_id'],),
        ).fetchone()
        if exp:
            context['experiment_code'] = context['experiment_code'] or str(exp['experiment_code'] or '').strip()
            context['suggestion_id'] = context['suggestion_id'] or str(exp['suggestion_id'] or '').strip()
            context['experiment_mode'] = context['experiment_mode'] or str(exp['experiment_mode'] or '').strip()
    return context


def _aggregate_generated_ad_performance_windows(conn: sqlite3.Connection, ad_id: str) -> Dict[str, Any]:
    normalized_ad_id = str(ad_id or '').strip()
    if not normalized_ad_id:
        return {'ad_id': '', 'has_data': False, 'reason': 'ad_id_missing', 'windows': []}
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ad_creative_performance_daily'"
    ).fetchone()
    if not table:
        return {'ad_id': normalized_ad_id, 'has_data': False, 'reason': 'performance_table_missing', 'windows': []}
    max_row = conn.execute(
        "SELECT MAX(report_date_london) AS max_date FROM ad_creative_performance_daily WHERE ad_id = ?",
        (normalized_ad_id,),
    ).fetchone()
    max_date = str(max_row['max_date'] or '').strip() if max_row else ''
    if not max_date:
        return {'ad_id': normalized_ad_id, 'has_data': False, 'reason': 'ad_performance_missing', 'windows': []}
    rows = conn.execute(
        """
        SELECT *
        FROM ad_creative_performance_daily
        WHERE ad_id = ? AND report_date_london <= ?
        ORDER BY report_date_london DESC
        LIMIT 14
        """,
        (normalized_ad_id, max_date),
    ).fetchall()
    windows: List[Dict[str, Any]] = []
    for days in (1, 3, 7):
        selected = rows[:days]
        spend = sum(float(row['spend'] or 0.0) for row in selected)
        impressions = sum(float(row['impressions'] or 0.0) for row in selected)
        clicks = sum(float(row['clicks'] or 0.0) for row in selected)
        installs = sum(float(row['installs'] or 0.0) for row in selected)
        af_joins = sum(float(row['af_model_join_events'] or 0.0) for row in selected)
        real_binds = sum(int(row['tugao_real_bind_count'] or 0) for row in selected)
        windows.append({
            'days': days,
            'row_count': len(selected),
            'date_from': selected[-1]['report_date_london'] if selected else '',
            'date_to': selected[0]['report_date_london'] if selected else '',
            'spend': round(spend, 4),
            'impressions': round(impressions, 4),
            'clicks': round(clicks, 4),
            'ctr': round(clicks / impressions, 6) if impressions else 0.0,
            'cpm': round(spend / impressions * 1000, 4) if impressions else 0.0,
            'installs': round(installs, 4),
            'cpi': round(spend / installs, 4) if installs else None,
            'af_model_join_events': round(af_joins, 4),
            'tugao_real_bind_count': real_binds,
            'real_bind_cpa': round(spend / real_binds, 4) if real_binds else None,
            'data_quality_status': 'ok' if selected else 'missing',
        })
    return {'ad_id': normalized_ad_id, 'has_data': True, 'max_date': max_date, 'windows': windows}


def _aggregate_source_object_performance_windows(conn: sqlite3.Connection, source: Dict[str, Any]) -> Dict[str, Any]:
    campaign = str((source or {}).get('campaign') or (source or {}).get('source_campaign_name') or '').strip()
    ad_group = str((source or {}).get('ad_group') or (source or {}).get('source_adset_name') or '').strip()
    ad = str((source or {}).get('ad') or (source or {}).get('source_ad_name') or (source or {}).get('object_name') or '').strip()
    account = str((source or {}).get('account_id') or (source or {}).get('account_name') or (source or {}).get('app_id') or '').strip()
    if not (campaign and ad_group and ad):
        return {'ad_id': '', 'has_data': False, 'reason': 'control_source_object_missing', 'windows': []}
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ad_dashboard_fact_rows'"
    ).fetchone()
    if not table:
        return {'ad_id': '', 'has_data': False, 'reason': 'dashboard_fact_table_missing', 'windows': []}
    where = ["campaign = ?", "ad_group = ?", "ad = ?"]
    args: List[Any] = [campaign, ad_group, ad]
    max_row = conn.execute(
        f"SELECT MAX(date) AS max_date FROM ad_dashboard_fact_rows WHERE {' AND '.join(where)}",
        tuple(args),
    ).fetchone()
    max_date = str(max_row['max_date'] or '').strip() if max_row else ''
    if not max_date:
        return {'ad_id': '', 'has_data': False, 'reason': 'control_source_object_performance_missing', 'windows': []}
    rows = conn.execute(
        f"""
        SELECT date,
               SUM(cost) AS cost,
               SUM(impressions) AS impressions,
               SUM(clicks) AS clicks,
               SUM(link_clicks) AS link_clicks,
               SUM(installs) AS installs,
               SUM(meta_installs) AS meta_installs,
               SUM(af_installs) AS af_installs,
               SUM(guild_joins) AS guild_joins,
               COUNT(*) AS source_row_count
        FROM ad_dashboard_fact_rows
        WHERE {' AND '.join(where)} AND date <= ?
        GROUP BY date
        ORDER BY date DESC
        LIMIT 14
        """,
        tuple(args + [max_date]),
    ).fetchall()
    windows: List[Dict[str, Any]] = []
    for days in (1, 3, 7):
        selected = rows[:days]
        spend = sum(float(row['cost'] or 0.0) for row in selected)
        impressions = sum(float(row['impressions'] or 0.0) for row in selected)
        clicks = sum(float(row['clicks'] or row['link_clicks'] or 0.0) for row in selected)
        installs = sum(float(row['installs'] or row['meta_installs'] or row['af_installs'] or 0.0) for row in selected)
        real_binds = sum(int(float(row['guild_joins'] or 0.0)) for row in selected)
        windows.append({
            'days': days,
            'row_count': sum(int(row['source_row_count'] or 0) for row in selected),
            'date_from': selected[-1]['date'] if selected else '',
            'date_to': selected[0]['date'] if selected else '',
            'spend': round(spend, 4),
            'impressions': round(impressions, 4),
            'clicks': round(clicks, 4),
            'ctr': round(clicks / impressions, 6) if impressions else 0.0,
            'cpm': round(spend / impressions * 1000, 4) if impressions else 0.0,
            'installs': round(installs, 4),
            'cpi': round(spend / installs, 4) if installs else None,
            'af_model_join_events': 0.0,
            'tugao_real_bind_count': real_binds,
            'real_bind_cpa': round(spend / real_binds, 4) if real_binds else None,
            'data_quality_status': 'dashboard_fact_object_match' if selected else 'missing',
        })
    return {
        'ad_id': '',
        'has_data': True,
        'max_date': max_date,
        'source': 'ad_dashboard_fact_rows',
        'match': {'account_id': account, 'campaign': campaign, 'ad_group': ad_group, 'ad': ad},
        'windows': windows,
    }


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if isinstance(value, (list, tuple)):
            nested = _first_non_empty(*value)
            if nested:
                return nested
            continue
        text = str(value or '').strip()
        if text:
            return text
    return ''


def _source_snapshot_has_values(snapshot: Dict[str, Any]) -> bool:
    if not isinstance(snapshot, dict):
        return False
    keys = (
        'cost',
        'spend',
        'installs',
        'guild_joins',
        'real_bind_count',
        'tugao_real_bind_count',
        'cpa',
        'real_bind_cpa',
        'evidence',
    )
    return any(str(snapshot.get(key) or '').strip() for key in keys)


def _resolve_generated_image_control_context(
    conn: sqlite3.Connection,
    *,
    context: Dict[str, Any],
    job_row: Optional[sqlite3.Row],
    adoption_row: Optional[sqlite3.Row],
) -> Dict[str, Any]:
    experiment_row = None
    experiment_id = str(context.get('experiment_id') or '').strip()
    if experiment_id:
        experiment_row = conn.execute(
            "SELECT * FROM creative_experiment_suggestions WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
    job_material = _json_load(job_row['material_refs_json'], {}) if job_row else {}
    job_metrics = _json_load(job_row['metrics_snapshot_json'], {}) if job_row else {}
    job_source_ads = _json_load(job_row['source_ad_ids_json'], []) if job_row and 'source_ad_ids_json' in job_row.keys() else []
    job_source_creatives = _json_load(job_row['source_creative_ids_json'], []) if job_row and 'source_creative_ids_json' in job_row.keys() else []
    experiment_payload = _json_load(experiment_row['payload_json'], {}) if experiment_row else {}
    payload_source_perf = experiment_payload.get('source_performance') if isinstance(experiment_payload, dict) else {}
    production_task = experiment_payload.get('production_task') if isinstance(experiment_payload, dict) else {}
    if not isinstance(payload_source_perf, dict):
        payload_source_perf = {}
    if not isinstance(production_task, dict):
        production_task = {}
    source_ad_id = _first_non_empty(
        experiment_row['source_ad_id'] if experiment_row else '',
        job_material.get('source_ad_id'),
        adoption_row['source_ad_id'] if adoption_row and 'source_ad_id' in adoption_row.keys() else '',
        job_source_ads,
        production_task.get('source_ad_id'),
        production_task.get('ad_id'),
        experiment_payload.get('source_ad_id') if isinstance(experiment_payload, dict) else '',
    )
    source_creative_id = _first_non_empty(
        experiment_row['source_creative_id'] if experiment_row else '',
        job_material.get('source_creative_id'),
        adoption_row['source_creative_id'] if adoption_row and 'source_creative_id' in adoption_row.keys() else '',
        job_source_creatives,
        production_task.get('source_creative_id'),
        production_task.get('creative_id'),
        experiment_payload.get('source_creative_id') if isinstance(experiment_payload, dict) else '',
    )
    source_campaign_id = _first_non_empty(
        experiment_row['source_campaign_id'] if experiment_row else '',
        job_material.get('source_campaign_id'),
        production_task.get('source_campaign_id'),
        production_task.get('campaign_id'),
        experiment_payload.get('source_campaign_id') if isinstance(experiment_payload, dict) else '',
    )
    source_adset_id = _first_non_empty(
        experiment_row['source_adset_id'] if experiment_row else '',
        job_material.get('source_adset_id'),
        production_task.get('source_adset_id'),
        production_task.get('adset_id'),
        experiment_payload.get('source_adset_id') if isinstance(experiment_payload, dict) else '',
    )
    source_object = {
        'recommendation_id': _first_non_empty(context.get('suggestion_id'), job_row['recommendation_id'] if job_row else '', experiment_row['suggestion_id'] if experiment_row else ''),
        'object_id': _first_non_empty(job_material.get('source_object_id'), production_task.get('source_object_id'), experiment_payload.get('source_object_id') if isinstance(experiment_payload, dict) else ''),
        'object_level': _first_non_empty(job_material.get('source_object_level'), production_task.get('source_object_level'), experiment_payload.get('source_object_level') if isinstance(experiment_payload, dict) else ''),
        'account_id': _first_non_empty(job_material.get('account_id'), production_task.get('account_id'), experiment_payload.get('account_id') if isinstance(experiment_payload, dict) else ''),
        'account_name': _first_non_empty(job_material.get('account_name'), production_task.get('account_name'), experiment_payload.get('account_name') if isinstance(experiment_payload, dict) else ''),
        'campaign': _first_non_empty(job_material.get('campaign'), production_task.get('campaign'), experiment_payload.get('campaign') if isinstance(experiment_payload, dict) else ''),
        'ad_group': _first_non_empty(job_material.get('ad_group'), production_task.get('ad_group'), experiment_payload.get('ad_group') if isinstance(experiment_payload, dict) else ''),
        'ad': _first_non_empty(job_material.get('ad'), production_task.get('ad'), experiment_payload.get('ad') if isinstance(experiment_payload, dict) else ''),
    }
    snapshot = job_metrics if _source_snapshot_has_values(job_metrics) else payload_source_perf
    performance = _aggregate_generated_ad_performance_windows(conn, source_ad_id) if source_ad_id else {
        'ad_id': '',
        'has_data': False,
        'reason': 'control_source_ad_missing',
        'windows': [],
    }
    if not performance.get('has_data'):
        object_performance = _aggregate_source_object_performance_windows(conn, source_object)
        if object_performance.get('has_data'):
            performance = object_performance
    if performance.get('has_data'):
        resolve_status = 'source_ad_performance_ready' if source_ad_id else 'source_object_performance_ready'
        missing_reason = ''
    elif source_ad_id:
        resolve_status = 'source_ad_performance_missing'
        missing_reason = str(performance.get('reason') or 'source_ad_performance_missing')
    elif _source_snapshot_has_values(snapshot):
        resolve_status = 'source_snapshot_only'
        missing_reason = 'control_source_ad_missing'
    else:
        resolve_status = 'control_source_ad_missing'
        missing_reason = 'control_source_ad_missing'
    return {
        'label': '对照旧广告',
        'source_ad_id': source_ad_id,
        'source_creative_id': source_creative_id,
        'source_campaign_id': source_campaign_id,
        'source_adset_id': source_adset_id,
        'source_object': source_object,
        'snapshot': snapshot if isinstance(snapshot, dict) else {},
        'performance': performance,
        'resolve_status': resolve_status,
        'missing_reason': missing_reason,
    }


def generated_image_experiment_tracking(conn: sqlite3.Connection, image_id: str) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    image = conn.execute(
        """
        SELECT image_id, request_id, review_status, market, brand, provider, metadata_json, created_at
        FROM creative_generated_images
        WHERE image_id = ?
        """,
        (image_id,),
    ).fetchone()
    if not image:
        raise ValueError('creative_image_not_found')
    adoption = conn.execute(
        """
        SELECT *
        FROM creative_adoption_records
        WHERE image_id = ?
        ORDER BY adopted_at DESC
        LIMIT 1
        """,
        (image_id,),
    ).fetchone()
    context = infer_generated_image_experiment_context(
        conn,
        image_id=image_id,
        request_id=str(image['request_id'] or ''),
        experiment_id=str(adoption['experiment_id'] or '') if adoption else '',
        experiment_code=str(adoption['experiment_code'] or '') if adoption else '',
        suggestion_id=str(adoption['suggestion_id'] or '') if adoption else '',
    )
    job = None
    if context.get('job_id'):
        job = conn.execute(
            """
            SELECT job_id, status, country, project, brand_display_name, experiment_id, experiment_code,
                   recommendation_id, source_ad_ids_json, source_creative_ids_json,
                   material_refs_json, metrics_snapshot_json, created_at, completed_at
            FROM creative_pro_work_queue
            WHERE job_id = ?
            """,
            (context['job_id'],),
        ).fetchone()
    ad_id = str(adoption['ad_id'] or '') if adoption else ''
    performance = _aggregate_generated_ad_performance_windows(conn, ad_id)
    control = _resolve_generated_image_control_context(conn, context=context, job_row=job, adoption_row=adoption)
    control_ready = bool(control.get('performance', {}).get('has_data') or _source_snapshot_has_values(control.get('snapshot', {})))
    return {
        'ok': True,
        'image': {
            'image_id': image['image_id'],
            'request_id': image['request_id'],
            'review_status': image['review_status'],
            'market': image['market'],
            'brand': image['brand'],
            'provider': image['provider'],
            'created_at': image['created_at'],
        },
        'experiment': context,
        'job': {
            'job_id': job['job_id'],
            'status': job['status'],
            'country': job['country'],
            'project': job['project'],
            'brand_display_name': job['brand_display_name'],
            'experiment_id': job['experiment_id'],
            'experiment_code': job['experiment_code'],
            'recommendation_id': job['recommendation_id'],
            'material_refs': _json_load(job['material_refs_json'], {}),
            'metrics_snapshot': _json_load(job['metrics_snapshot_json'], {}),
            'created_at': job['created_at'],
            'completed_at': job['completed_at'],
        } if job else None,
        'adoption': {
            'adoption_id': adoption['adoption_id'],
            'ad_id': adoption['ad_id'],
            'creative_id': adoption['creative_id'],
            'adset_id': adoption['adset_id'],
            'campaign_id': adoption['campaign_id'],
            'status': adoption['status'],
            'binding_method': adoption['binding_method'],
            'binding_confidence': adoption['binding_confidence'],
            'binding_status': adoption['binding_status'],
            'adopted_at': adoption['adopted_at'],
            'experiment_id': adoption['experiment_id'] or context.get('experiment_id', ''),
            'experiment_code': adoption['experiment_code'] or context.get('experiment_code', ''),
            'suggestion_id': adoption['suggestion_id'] or context.get('suggestion_id', ''),
            'experiment_inferred': not bool(adoption['experiment_id']) and bool(context.get('experiment_id')),
        } if adoption else None,
        'performance': performance,
        'control': control,
        'can_compare': bool(ad_id and performance.get('has_data') and control_ready),
        'can_compare_with_control': bool(ad_id and performance.get('has_data') and control.get('performance', {}).get('has_data')),
        'missing_reason': '' if performance.get('has_data') else performance.get('reason', ''),
    }


def create_review_record(
    conn: sqlite3.Connection,
    *,
    image_id: str,
    review_status: str,
    reviewer: str = '',
    checks: Optional[Dict[str, Any]] = None,
    decision_reason: str = '',
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    image = conn.execute(
        "SELECT image_id, request_id, metadata_json, image_hash, creative_direction FROM creative_generated_images WHERE image_id = ?",
        (image_id,),
    ).fetchone()
    if not image:
        raise ValueError('creative_image_not_found')
    normalized_status = str(review_status or 'NEEDS_REVIEW').strip().upper()
    if normalized_status not in REVIEW_STATUS_ZH:
        raise ValueError('invalid_review_status')
    if normalized_status == 'APPROVED' and str(image['image_hash'] or '').strip():
        duplicate = conn.execute(
            """
            SELECT image_id FROM creative_generated_images
            WHERE image_hash = ? AND image_id <> ? AND creative_direction <> ?
              AND review_status NOT IN ('rejected','archived')
            LIMIT 1
            """,
            (image['image_hash'], image_id, image['creative_direction']),
        ).fetchone()
        if duplicate:
            raise ValueError('duplicate_creative_across_directions')
    review_id = f'review_{stable_id(image_id, normalized_status, reviewer, utc_now())}'
    payload_checks = dict(checks or {})
    conn.execute(
        """
        INSERT INTO creative_review_records
        (review_id, image_id, request_id, review_status, review_status_zh, reviewer, checks_json, decision_reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            review_id,
            image['image_id'],
            image['request_id'],
            normalized_status,
            REVIEW_STATUS_ZH[normalized_status],
            reviewer,
            json.dumps(payload_checks, ensure_ascii=False, sort_keys=True),
            decision_reason,
            utc_now(),
        ),
    )
    conn.execute(
        "UPDATE creative_generated_images SET review_status = ? WHERE image_id = ?",
        (normalized_status.lower(), image_id),
    )
    metadata = _json_load(image['metadata_json'], {})
    linked_job_id = str(
        (metadata.get('job_id') or metadata.get('creative_pro_job_id') or '')
        if isinstance(metadata, dict) else ''
    ).strip()
    completed_job_id = ''
    if normalized_status == 'APPROVED':
        completed_job_id = linked_job_id
        if completed_job_id:
            conn.execute(
                """
                UPDATE creative_pro_work_queue
                SET status = 'completed',
                    completed_at = CASE WHEN completed_at = '' THEN ? ELSE completed_at END,
                    error_code = '',
                    error_message = ''
                WHERE job_id = ?
                  AND status IN ('pending', 'claimed', 'generating', 'pending_review')
                """,
                (utc_now(), completed_job_id),
            )
            cleanup_temporary_creative_source_images(conn, job_id=completed_job_id)
    conn.commit()
    return {
        'review_id': review_id,
        'image_id': image_id,
        'image_hash': str(image['image_hash'] or ''),
        'request_id': image['request_id'],
        'review_status': normalized_status,
        'review_status_zh': REVIEW_STATUS_ZH[normalized_status],
        'job_id': linked_job_id,
        'completed_job_id': completed_job_id,
        'checks': payload_checks,
    }


def mark_generated_image_adopted(
    conn: sqlite3.Connection,
    *,
    image_id: str,
    ad_id: str = '',
    creative_id: str = '',
    adset_id: str = '',
    campaign_id: str = '',
    adopted_by: str = '',
    experiment_id: str = '',
    experiment_code: str = '',
    suggestion_id: str = '',
    adoption_type: str = '',
    binding_method: str = '',
    binding_confidence: str = '',
    binding_status: str = 'confirmed',
    evidence: Optional[Dict[str, Any]] = None,
    notes: str = '',
    commit: bool = True,
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    image = conn.execute(
        "SELECT image_id, request_id, review_status FROM creative_generated_images WHERE image_id = ?",
        (image_id,),
    ).fetchone()
    if not image:
        raise ValueError('creative_image_not_found')
    if str(image['review_status'] or '').lower() not in {'approved', 'used_in_ad'}:
        raise ValueError('creative_image_not_approved')
    inferred = infer_generated_image_experiment_context(
        conn,
        image_id=image_id,
        request_id=str(image['request_id'] or ''),
        experiment_id=experiment_id,
        experiment_code=experiment_code,
        suggestion_id=suggestion_id,
    )
    experiment_id = experiment_id or str(inferred.get('experiment_id') or '')
    experiment_code = experiment_code or str(inferred.get('experiment_code') or '')
    suggestion_id = suggestion_id or str(inferred.get('suggestion_id') or '')
    normalized_method = str(binding_method or '').strip() or (
        BINDING_METHOD_MANUAL_CREATIVE_ID_BINDING if creative_id and not ad_id else BINDING_METHOD_MANUAL_AD_ID_BINDING
    )
    normalized_confidence = str(binding_confidence or '').strip() or binding_confidence_for_method(
        normalized_method,
        manual_confirmed=normalized_method in {BINDING_METHOD_MANUAL_AD_ID_BINDING, BINDING_METHOD_MANUAL_CREATIVE_ID_BINDING},
    )
    normalized_adoption_type = str(adoption_type or '').strip() or 'manual_binding'
    adoption_id = f'adopt_{stable_id(image_id, ad_id, creative_id, adset_id, campaign_id, experiment_id, normalized_method)}'
    payload = {
        'source': 'manual_adoption',
        'requires_external_manual_upload': False,
        'manual_upload_optional': True,
        'external_write_performed': False,
            'binding_method': normalized_method,
            'binding_confidence': normalized_confidence,
            'experiment_backfilled_from_job': bool(inferred.get('job_id')) and bool(experiment_id),
        }
    now = utc_now()
    conn.execute(
        """
        INSERT OR REPLACE INTO creative_adoption_records
        (adoption_id, image_id, request_id, ad_id, creative_id, adset_id, campaign_id, adopted_by, adopted_at, status,
         payload_json, experiment_id, experiment_code, suggestion_id, generation_request_id, generated_image_id,
         adopted_ad_id, adopted_creative_id, adopted_adset_id, adopted_campaign_id, adoption_type, binding_method,
         binding_confidence, binding_status, matched_at, confirmed_by, confirmed_at, evidence_json, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            adoption_id,
            image['image_id'],
            image['request_id'],
            ad_id,
            creative_id,
            adset_id,
            campaign_id,
            adopted_by,
            now,
            'USED_IN_AD',
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            experiment_id,
            experiment_code,
            suggestion_id,
            image['request_id'],
            image['image_id'],
            ad_id,
            creative_id,
            adset_id,
            campaign_id,
            normalized_adoption_type,
            normalized_method,
            normalized_confidence,
            binding_status,
            now,
            adopted_by,
            now if binding_status == 'confirmed' else '',
            json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            notes,
        ),
    )
    conn.execute(
        "UPDATE creative_generated_images SET review_status = ? WHERE image_id = ?",
        ('used_in_ad', image_id),
    )
    if commit:
        conn.commit()
    return {
        'adoption_id': adoption_id,
        'image_id': image_id,
        'request_id': image['request_id'],
        'status': 'USED_IN_AD',
        'ad_id': ad_id,
        'creative_id': creative_id,
        'adoption_type': normalized_adoption_type,
        'binding_method': normalized_method,
        'binding_confidence': normalized_confidence,
        'binding_status': binding_status,
        'external_write_performed': False,
    }


def mark_replaced_creative_pending_cleanup(
    conn: sqlite3.Connection,
    *,
    ad_id: str,
    old_creative_id: str,
    replacement_image_id: str,
    replacement_creative_id: str,
    retention_days: int = 7,
    commit: bool = True,
) -> Dict[str, Any]:
    """Detach the superseded local image binding without deleting audit history."""
    ensure_creative_image_generation_tables(conn)
    normalized_ad_id = str(ad_id or '').strip()
    normalized_old_creative = str(old_creative_id or '').strip()
    if not normalized_ad_id or not normalized_old_creative:
        return {'updated': 0, 'image_ids': [], 'cleanup_after': ''}
    source_rows = conn.execute(
        """
        SELECT DISTINCT image_id FROM creative_adoption_records
        WHERE ad_id=? AND creative_id=? AND image_id<>?
        """,
        (normalized_ad_id, normalized_old_creative, str(replacement_image_id or '').strip()),
    ).fetchall()
    image_ids = [str(row['image_id'] or '') for row in source_rows if str(row['image_id'] or '')]
    if not image_ids:
        return {'updated': 0, 'image_ids': [], 'cleanup_after': ''}
    now = datetime.now(timezone.utc)
    cleanup_after = (now + timedelta(days=max(1, int(retention_days or 7)))).isoformat()
    placeholders = ','.join('?' for _ in image_ids)
    rows = conn.execute(
        f"""
        SELECT adoption_id,payload_json FROM creative_adoption_records
        WHERE ad_id=? AND image_id IN ({placeholders})
          AND (creative_id='' OR creative_id=?)
        """,
        (normalized_ad_id, *image_ids, normalized_old_creative),
    ).fetchall()
    for row in rows:
        payload = _json_load(row['payload_json'], {})
        payload.update({
            'cleanup_status': 'pending_cleanup',
            'cleanup_after': cleanup_after,
            'replaced_at': now.isoformat(),
            'replacement_image_id': str(replacement_image_id or ''),
            'replacement_creative_id': str(replacement_creative_id or ''),
        })
        conn.execute(
            """
            UPDATE creative_adoption_records
            SET status='PENDING_CLEANUP',binding_status='pending_cleanup',payload_json=?,notes=?
            WHERE adoption_id=?
            """,
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                'replaced creative retained for seven-day audit window',
                str(row['adoption_id']),
            ),
        )
    if commit:
        conn.commit()
    return {'updated': len(rows), 'image_ids': image_ids, 'cleanup_after': cleanup_after}


def archive_due_replaced_creatives(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    now: Optional[datetime] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """Archive due local assets only when no confirmed binding still references them."""
    ensure_creative_image_generation_tables(conn)
    current = now or datetime.now(timezone.utc)
    rows = conn.execute(
        """
        SELECT adoption_id,image_id,payload_json FROM creative_adoption_records
        WHERE status='PENDING_CLEANUP' AND binding_status='pending_cleanup'
        ORDER BY adopted_at,adoption_id LIMIT ?
        """,
        (max(1, min(int(limit or 100), 500)),),
    ).fetchall()
    due_images: List[str] = []
    for row in rows:
        payload = _json_load(row['payload_json'], {})
        raw_deadline = str(payload.get('cleanup_after') or '')
        try:
            deadline = datetime.fromisoformat(raw_deadline.replace('Z', '+00:00'))
        except ValueError:
            continue
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline <= current:
            due_images.append(str(row['image_id'] or ''))
    archived: List[str] = []
    for image_id in dict.fromkeys(item for item in due_images if item):
        active = conn.execute(
            """
            SELECT 1 FROM creative_adoption_records
            WHERE image_id=? AND status='USED_IN_AD'
              AND binding_status IN ('confirmed','matched') LIMIT 1
            """,
            (image_id,),
        ).fetchone()
        if active:
            continue
        conn.execute(
            """
            UPDATE creative_adoption_records
            SET status='ARCHIVED',binding_status='archived',notes='seven-day replacement retention completed'
            WHERE image_id=? AND status='PENDING_CLEANUP' AND binding_status='pending_cleanup'
            """,
            (image_id,),
        )
        conn.execute(
            "UPDATE creative_generated_images SET review_status='archived' WHERE image_id=?",
            (image_id,),
        )
        archived.append(image_id)
    if commit:
        conn.commit()
    return {'scanned': len(rows), 'archived': archived}


def auto_adopt_generated_image_by_meta_asset(
    conn: sqlite3.Connection,
    *,
    image_id: str,
    adopted_by: str = '',
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    image = conn.execute(
        """
        SELECT image_id, request_id, review_status, image_hash, final_delivery_hash, metadata_json
        FROM creative_generated_images
        WHERE image_id = ?
        """,
        (image_id,),
    ).fetchone()
    if not image:
        raise ValueError('creative_image_not_found')
    if str(image['review_status'] or '').lower() not in {'approved', 'used_in_ad'}:
        raise ValueError('creative_image_not_approved')
    asset_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ad_creative_asset'"
    ).fetchone()
    if not asset_table:
        return {
            'ok': False,
            'matched': False,
            'reason': 'meta_creative_asset_table_missing',
            'message_cn': 'Meta 素材同步表不存在，无法自动识别。',
        }
    metadata = _json_load(image['metadata_json'], {})
    candidate_hashes = {
        str(image['image_hash'] or '').strip(),
        str(image['final_delivery_hash'] or '').strip(),
    }
    if isinstance(metadata, dict):
        candidate_hashes.update({
            str(metadata.get('image_hash') or '').strip(),
            str(metadata.get('final_delivery_hash') or '').strip(),
            str(metadata.get('source_image_hash') or '').strip(),
        })
    candidate_hashes = {value for value in candidate_hashes if value}
    if not candidate_hashes:
        return {
            'ok': False,
            'matched': False,
            'reason': 'generated_image_hash_missing',
            'message_cn': '生成图缺少可比对 hash，无法自动识别。',
        }
    placeholders = ','.join('?' for _ in candidate_hashes)
    rows = conn.execute(
        f"""
        SELECT asset_id, platform, account_id, campaign_id, adset_id, ad_id, creative_id,
               image_hash, source_image_hash, title_text, updated_at
        FROM ad_creative_asset
        WHERE COALESCE(image_hash, '') IN ({placeholders})
           OR COALESCE(source_image_hash, '') IN ({placeholders})
        ORDER BY updated_at DESC
        LIMIT 20
        """,
        tuple(candidate_hashes) + tuple(candidate_hashes),
    ).fetchall()
    if not rows:
        available = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN COALESCE(image_hash, '') <> '' THEN 1 ELSE 0 END) AS image_hash_count,
                SUM(CASE WHEN COALESCE(source_image_hash, '') <> '' THEN 1 ELSE 0 END) AS source_hash_count
            FROM ad_creative_asset
            """
        ).fetchone()
        return {
            'ok': False,
            'matched': False,
            'reason': 'no_meta_hash_match',
            'message_cn': '暂未在 Meta 同步素材里找到同 hash 广告；请先确认新广告已上传并完成 Meta 素材同步。',
            'candidate_hash_count': len(candidate_hashes),
            'meta_asset_count': int(available['total'] or 0) if available else 0,
            'meta_image_hash_count': int(available['image_hash_count'] or 0) if available else 0,
            'meta_source_hash_count': int(available['source_hash_count'] or 0) if available else 0,
        }
    unique: Dict[Tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        unique[(str(row['ad_id'] or ''), str(row['creative_id'] or ''))] = row
    if len(unique) != 1:
        return {
            'ok': False,
            'matched': False,
            'reason': 'ambiguous_meta_hash_match',
            'message_cn': f'找到 {len(unique)} 个同 hash 广告，无法自动决定绑定哪一个。',
            'matches': [
                {
                    'asset_id': row['asset_id'],
                    'ad_id': row['ad_id'],
                    'creative_id': row['creative_id'],
                    'title_text': row['title_text'],
                }
                for row in unique.values()
            ],
        }
    row = next(iter(unique.values()))
    matched_hash = str(row['image_hash'] or row['source_image_hash'] or '').strip()
    adoption = mark_generated_image_adopted(
        conn,
        image_id=image_id,
        ad_id=str(row['ad_id'] or ''),
        creative_id=str(row['creative_id'] or ''),
        adset_id=str(row['adset_id'] or ''),
        campaign_id=str(row['campaign_id'] or ''),
        adopted_by=adopted_by,
        adoption_type='auto_meta_hash_match',
        binding_method=BINDING_METHOD_GENERATED_IMAGE_HASH_MATCH,
        binding_status='matched',
        evidence={
            'asset_id': row['asset_id'],
            'platform': row['platform'],
            'account_id': row['account_id'],
            'matched_hash': matched_hash,
            'candidate_hash_count': len(candidate_hashes),
        },
        notes='auto matched from ad_creative_asset image_hash/source_image_hash',
    )
    return {
        'ok': True,
        'matched': True,
        **adoption,
        'asset_id': row['asset_id'],
        'matched_hash': matched_hash,
        'message_cn': '已通过 Meta 素材 hash 自动识别并绑定广告。',
    }


def approve_creative_experiment_generation(
    conn: sqlite3.Connection,
    *,
    suggestion_id: str = '',
    generated_image_id: str = '',
    experiment_mode: str = EXPERIMENT_MODE_REPLACEMENT,
    source_ad_id: str = '',
    source_creative_id: str = '',
    source_campaign_id: str = '',
    source_adset_id: str = '',
    country: str = '',
    created_by: str = '',
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    mode = normalize_experiment_mode(experiment_mode)
    image = None
    if generated_image_id:
        image = conn.execute(
            "SELECT image_id, request_id FROM creative_generated_images WHERE image_id = ?",
            (generated_image_id,),
        ).fetchone()
        if not image:
            raise ValueError('creative_image_not_found')
    sequence = conn.execute("SELECT COUNT(*) AS n FROM creative_experiment_suggestions").fetchone()['n'] + 1
    experiment_code = str((payload or {}).get('experiment_code') or '').strip() or generate_experiment_code(country=country, sequence=sequence)
    experiment_id = f'exp_{stable_id(suggestion_id, generated_image_id, source_ad_id, experiment_code)}'
    recommended_method = (
        BINDING_METHOD_EXPERIMENT_ID_NAME_MATCH
        if mode == EXPERIMENT_MODE_NEW_TEST
        else BINDING_METHOD_ORIGINAL_AD_CREATIVE_REPLACED
    )
    instruction = build_binding_instruction(mode, experiment_code)
    now = utc_now()
    merged_payload = {
        **dict(payload or {}),
        'external_write_performed': False,
        'upload_final_asset_optional': True,
    }
    conn.execute(
        """
        INSERT OR REPLACE INTO creative_experiment_suggestions
        (experiment_id, experiment_code, suggestion_id, generated_image_id, generation_request_id, experiment_mode,
         source_ad_id, source_creative_id, source_campaign_id, source_adset_id, recommended_binding_method,
         binding_instruction_cn, requires_manual_upload, requires_experiment_code_in_ad_name, binding_status,
         status, created_by, created_at, updated_at, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            experiment_id,
            experiment_code,
            suggestion_id,
            generated_image_id,
            image['request_id'] if image else '',
            mode,
            source_ad_id,
            source_creative_id,
            source_campaign_id,
            source_adset_id,
            recommended_method,
            instruction,
            0,
            1 if mode == EXPERIMENT_MODE_NEW_TEST else 0,
            'pending',
            'approved_for_generation',
            created_by,
            now,
            now,
            json.dumps(merged_payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()
    return get_creative_experiment(conn, experiment_id)


def get_creative_experiment(conn: sqlite3.Connection, experiment_id: str) -> Dict[str, Any]:
    ensure_creative_image_generation_tables(conn)
    row = conn.execute(
        "SELECT * FROM creative_experiment_suggestions WHERE experiment_id = ?",
        (experiment_id,),
    ).fetchone()
    if not row:
        raise ValueError('creative_experiment_not_found')
    return {
        'experiment_id': row['experiment_id'],
        'experiment_code': row['experiment_code'],
        'suggestion_id': row['suggestion_id'],
        'generated_image_id': row['generated_image_id'],
        'generation_request_id': row['generation_request_id'],
        'experiment_mode': row['experiment_mode'],
        'source_ad_id': row['source_ad_id'],
        'source_creative_id': row['source_creative_id'],
        'source_campaign_id': row['source_campaign_id'],
        'source_adset_id': row['source_adset_id'],
        'recommended_binding_method': row['recommended_binding_method'],
        'binding_instruction_cn': row['binding_instruction_cn'],
        'requires_manual_upload': bool(row['requires_manual_upload']),
        'requires_experiment_code_in_ad_name': bool(row['requires_experiment_code_in_ad_name']),
        'binding_status': row['binding_status'],
        'status': row['status'],
        'created_by': row['created_by'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'payload': json.loads(row['payload_json'] or '{}'),
    }


def _latest_adoption_for_experiment(conn: sqlite3.Connection, experiment_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT * FROM creative_adoption_records
        WHERE experiment_id = ?
        ORDER BY matched_at DESC, adopted_at DESC
        LIMIT 1
        """,
        (experiment_id,),
    ).fetchone()
    if not row:
        return None
    return {
        'adoption_id': row['adoption_id'],
        'experiment_id': row['experiment_id'],
        'experiment_code': row['experiment_code'],
        'generated_image_id': row['generated_image_id'] or row['image_id'],
        'ad_id': row['adopted_ad_id'] or row['ad_id'],
        'creative_id': row['adopted_creative_id'] or row['creative_id'],
        'adset_id': row['adopted_adset_id'] or row['adset_id'],
        'campaign_id': row['adopted_campaign_id'] or row['campaign_id'],
        'adoption_type': row['adoption_type'],
        'binding_method': row['binding_method'],
        'binding_confidence': row['binding_confidence'],
        'binding_status': row['binding_status'],
        'evidence': json.loads(row['evidence_json'] or '{}'),
        'notes': row['notes'],
        'matched_at': row['matched_at'],
        'confirmed_at': row['confirmed_at'],
    }


def creative_experiment_binding_status(conn: sqlite3.Connection, experiment_id: str) -> Dict[str, Any]:
    experiment = get_creative_experiment(conn, experiment_id)
    adoption = _latest_adoption_for_experiment(conn, experiment_id)
    conclusion_allowed = bool(adoption and adoption.get('binding_confidence') in {
        BINDING_CONFIDENCE_HIGH,
        BINDING_CONFIDENCE_MEDIUM,
        BINDING_CONFIDENCE_MANUAL_CONFIRMED,
    } and adoption.get('binding_status') in {'matched', 'confirmed'})
    return {
        'ok': True,
        'experiment': experiment,
        'binding': adoption,
        'can_conclude_generated_image_effectiveness': conclusion_allowed,
        'low_confidence_warning': None if conclusion_allowed else '低置信或未绑定时不能输出生成图效果结论',
    }


def _insert_experiment_binding(
    conn: sqlite3.Connection,
    *,
    experiment: Dict[str, Any],
    ad_id: str = '',
    creative_id: str = '',
    adset_id: str = '',
    campaign_id: str = '',
    binding_method: str,
    binding_status: str = 'matched',
    evidence: Optional[Dict[str, Any]] = None,
    confirmed_by: str = '',
    notes: str = '',
) -> Dict[str, Any]:
    image_id = str(experiment.get('generated_image_id') or '')
    request_id = str(experiment.get('generation_request_id') or '')
    if not image_id:
        image_id = f'exp_image_{stable_id(experiment.get("experiment_id"), experiment.get("experiment_code"))}'
    confidence = binding_confidence_for_method(binding_method, manual_confirmed=binding_status == 'confirmed')
    now = utc_now()
    adoption_id = f'adopt_{stable_id(experiment.get("experiment_id"), ad_id, creative_id, binding_method)}'
    payload = {
        'source': 'creative_experiment_binding',
        'requires_external_manual_upload': False,
        'manual_upload_optional': True,
        'external_write_performed': False,
        'binding_method': binding_method,
        'binding_confidence': confidence,
    }
    conn.execute(
        """
        INSERT OR REPLACE INTO creative_adoption_records
        (adoption_id, image_id, request_id, ad_id, creative_id, adset_id, campaign_id, adopted_by, adopted_at, status,
         payload_json, experiment_id, experiment_code, suggestion_id, generation_request_id, generated_image_id,
         source_ad_id, source_creative_id, adopted_ad_id, adopted_creative_id, adopted_adset_id, adopted_campaign_id,
         adoption_type, binding_method, binding_confidence, binding_status, matched_at, confirmed_by, confirmed_at,
         evidence_json, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            adoption_id,
            image_id,
            request_id,
            ad_id,
            creative_id,
            adset_id,
            campaign_id,
            confirmed_by,
            now,
            'USED_IN_AD' if binding_status == 'confirmed' else 'MATCHED',
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            experiment['experiment_id'],
            experiment['experiment_code'],
            experiment['suggestion_id'],
            request_id,
            image_id,
            experiment['source_ad_id'],
            experiment['source_creative_id'],
            ad_id,
            creative_id,
            adset_id,
            campaign_id,
            experiment['experiment_mode'],
            binding_method,
            confidence,
            binding_status,
            now,
            confirmed_by,
            now if binding_status == 'confirmed' else '',
            json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            notes,
        ),
    )
    conn.execute(
        "UPDATE creative_experiment_suggestions SET binding_status = ?, updated_at = ? WHERE experiment_id = ?",
        (binding_status, now, experiment['experiment_id']),
    )
    conn.commit()
    return creative_experiment_binding_status(conn, experiment['experiment_id'])


def detect_creative_experiment_binding(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    meta_ads: Optional[List[Dict[str, Any]]] = None,
    creative_snapshots: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    experiment = get_creative_experiment(conn, experiment_id)
    ads = [dict(item or {}) for item in (meta_ads or creative_snapshots or [])]
    mode = normalize_experiment_mode(experiment['experiment_mode'])
    if mode == EXPERIMENT_MODE_NEW_TEST:
        code = str(experiment['experiment_code'] or '')
        for item in ads:
            ad_name = str(item.get('ad_name') or item.get('name') or '')
            if code and code in ad_name:
                return _insert_experiment_binding(
                    conn,
                    experiment=experiment,
                    ad_id=str(item.get('ad_id') or item.get('id') or ''),
                    creative_id=str(item.get('creative_id') or ''),
                    adset_id=str(item.get('adset_id') or ''),
                    campaign_id=str(item.get('campaign_id') or ''),
                    binding_method=BINDING_METHOD_EXPERIMENT_ID_NAME_MATCH,
                    binding_status='matched',
                    evidence={'matched_ad_name': ad_name, 'experiment_code': code},
                )
    else:
        source_ad_id = str(experiment['source_ad_id'] or '')
        source_creative_id = str(experiment['source_creative_id'] or '')
        for item in ads:
            ad_id = str(item.get('ad_id') or item.get('id') or '')
            creative_id = str(item.get('creative_id') or '')
            if source_ad_id and ad_id == source_ad_id and creative_id and creative_id != source_creative_id:
                return _insert_experiment_binding(
                    conn,
                    experiment=experiment,
                    ad_id=ad_id,
                    creative_id=creative_id,
                    adset_id=str(item.get('adset_id') or experiment.get('source_adset_id') or ''),
                    campaign_id=str(item.get('campaign_id') or experiment.get('source_campaign_id') or ''),
                    binding_method=BINDING_METHOD_ORIGINAL_AD_CREATIVE_REPLACED,
                    binding_status='matched',
                    evidence={
                        'source_creative_id': source_creative_id,
                        'current_creative_id': creative_id,
                        'source_ad_id': source_ad_id,
                    },
                )
    return creative_experiment_binding_status(conn, experiment_id)


def confirm_creative_experiment_binding(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    ad_id: str = '',
    creative_id: str = '',
    adset_id: str = '',
    campaign_id: str = '',
    confirmed_by: str = '',
    notes: str = '',
) -> Dict[str, Any]:
    experiment = get_creative_experiment(conn, experiment_id)
    method = BINDING_METHOD_MANUAL_CREATIVE_ID_BINDING if creative_id and not ad_id else BINDING_METHOD_MANUAL_AD_ID_BINDING
    return _insert_experiment_binding(
        conn,
        experiment=experiment,
        ad_id=ad_id,
        creative_id=creative_id,
        adset_id=adset_id,
        campaign_id=campaign_id,
        binding_method=method,
        binding_status='confirmed',
        confirmed_by=confirmed_by,
        notes=notes,
        evidence={'manual_confirmed': True},
    )


def reject_creative_experiment_binding(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    rejected_by: str = '',
    reason: str = '',
) -> Dict[str, Any]:
    experiment = get_creative_experiment(conn, experiment_id)
    now = utc_now()
    conn.execute(
        "UPDATE creative_experiment_suggestions SET binding_status = ?, updated_at = ?, payload_json = ? WHERE experiment_id = ?",
        (
            'rejected',
            now,
            json.dumps({**experiment.get('payload', {}), 'rejected_by': rejected_by, 'reject_reason': reason}, ensure_ascii=False, sort_keys=True),
            experiment_id,
        ),
    )
    conn.commit()
    return creative_experiment_binding_status(conn, experiment_id)


def upload_final_creative_asset_hash(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    generated_image_id: str = '',
    final_delivery_hash: str = '',
    uploaded_by: str = '',
) -> Dict[str, Any]:
    experiment = get_creative_experiment(conn, experiment_id)
    image_id = generated_image_id or experiment.get('generated_image_id') or ''
    if not image_id:
        raise ValueError('creative_image_not_found')
    row = conn.execute("SELECT image_id FROM creative_generated_images WHERE image_id = ?", (image_id,)).fetchone()
    if not row:
        raise ValueError('creative_image_not_found')
    conn.execute(
        """
        UPDATE creative_generated_images
        SET final_delivery_hash = ?, uploaded_manually = 1, uploaded_final_version = 1, is_exact_generated_asset = ?
        WHERE image_id = ?
        """,
        (
            final_delivery_hash,
            1 if final_delivery_hash and final_delivery_hash == conn.execute(
                "SELECT image_hash FROM creative_generated_images WHERE image_id = ?",
                (image_id,),
            ).fetchone()['image_hash'] else 0,
            image_id,
        ),
    )
    conn.commit()
    return {
        'ok': True,
        'experiment_id': experiment_id,
        'generated_image_id': image_id,
        'final_delivery_hash': final_delivery_hash,
        'uploaded_by': uploaded_by,
        'manual_upload_optional': True,
        'external_write_performed': False,
    }
