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


def test_recent_zero_install_and_zero_join_is_under_delivery_not_infinite_observation():
    recommendation = AdDailyRecommendationEngine().recommend_with_funnel(
        _item(),
        {"start": "2026-08-04", "end": "2026-08-10"},
    )

    assert recommendation.status_tag == "under_delivery"
    assert recommendation.diagnosis_type == "under_delivery"
    assert recommendation.action_type == "manual_review"
    assert recommendation.primary_action == "observe"
    assert recommendation.allow_pause is False
    assert "继续延长观察期不会自动增加样本" in recommendation.reason_zh
    assert "投放状态、预算与受众交付" in recommendation.reason_zh


def test_recent_install_sample_stays_on_maturity_path_until_strong_guardrail():
    recommendation = AdDailyRecommendationEngine().recommend_with_funnel(
        _item(spend=6.0, installs=7, cpi=0.8571),
        {"start": "2026-08-04", "end": "2026-08-10"},
    )

    assert recommendation.status_tag == "data_insufficient"
    assert recommendation.diagnosis_type == "data_insufficient"
    assert recommendation.action_type == "observe"
    assert recommendation.allow_pause is False
    assert "当前样本尚未达到强动作门槛" in recommendation.reason_zh
