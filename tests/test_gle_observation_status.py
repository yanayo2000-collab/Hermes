from app.ad_daily_report import AdDailyRecommendationEngine, AdObjectMetrics, DataQualityStatus


def _item(**overrides):
    values = dict(
        object_id="ad-low-delivery",
        object_level="ad",
        country="BR",
        project="Timo",
        target_app="timo",
        account_id="1457588552349197",
        campaign="campaign",
        ad_group="adset",
        ad="long-running-ad",
        spend=0.04,
        impressions=20,
        clicks=3,
        ctr=0.15,
        cpm=2.0,
        installs=0,
        cpi=None,
        real_bind_count=0,
        real_bind_cpa=None,
        maturity_day=6,
        data_quality=DataQualityStatus(
            status="ok",
            attribution_quality="tugao_funnel_fact",
        ),
    )
    values.update(overrides)
    return AdObjectMetrics(**values)


def test_mature_zero_delivery_requires_controlled_rebuild_not_more_observation():
    recommendation = AdDailyRecommendationEngine().recommend_with_funnel(
        _item(),
        {"start": "2026-08-04", "end": "2026-08-10"},
    )

    assert recommendation.status_tag == "under_delivery"
    assert recommendation.diagnosis_type == "under_delivery"
    assert recommendation.action_type == "repair_delivery_config"
    assert recommendation.primary_action == "repair_delivery_config"
    assert recommendation.allow_pause is False
    assert "继续等待不会修复交付" in recommendation.reason_zh
    assert "CPI 成本上限" in recommendation.reason_zh


def test_mature_spend_without_install_requires_repair_creative_experiment():
    recommendation = AdDailyRecommendationEngine().recommend_with_funnel(
        _item(spend=6.0),
        {"start": "2026-08-04", "end": "2026-08-10"},
    )

    assert recommendation.status_tag == "frontend_risk"
    assert recommendation.diagnosis_type == "front_funnel_weak"
    assert recommendation.action_type == "generate_repair_creative"
    assert recommendation.primary_action == "generate_repair_creative"
    assert "不再延长观察" in recommendation.reason_zh


def test_mature_install_without_join_routes_to_post_im_investigation():
    recommendation = AdDailyRecommendationEngine().recommend_with_funnel(
        _item(spend=6.0, installs=7, cpi=0.8571),
        {"start": "2026-08-04", "end": "2026-08-10"},
    )

    assert recommendation.status_tag == "data_quality"
    assert recommendation.diagnosis_type == "creative_effective_post_im_failed"
    assert recommendation.action_type == "inspect_post_im_funnel"
    assert recommendation.allow_pause is False
    assert "后链路核对" in recommendation.reason_zh


def test_pre_deadline_sample_has_a_finite_d4_checkpoint():
    recommendation = AdDailyRecommendationEngine().recommend_with_funnel(
        _item(maturity_day=2),
        {"start": "2026-08-04", "end": "2026-08-10"},
    )

    assert recommendation.status_tag == "data_insufficient"
    assert recommendation.action_type == "observe"
    assert "D+4 有限观察期" in recommendation.reason_zh
    assert "不会无限等待" in recommendation.reason_zh
