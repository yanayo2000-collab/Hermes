from __future__ import annotations

from datetime import date

import pytest

from app.tugao_funnel_api import (
    TugaoFunnelDailyMetricsClient,
    TugaoFunnelApiError,
    TugaoFunnelPiiError,
    assert_no_forbidden_pii_keys,
    tugao_funnel_api_row_to_fact,
    validate_date_window,
    validate_group_by,
)


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = '{}'

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({'url': url, 'params': dict(params or {}), 'headers': dict(headers or {}), 'timeout': timeout})
        return self.responses.pop(0)


def _api_row(**overrides):
    row = {
        'date': '2026-06-22',
        'country': 'Brazil',
        'media_source': 'Meta',
        'campaign_id': 'camp_1',
        'campaign_name': '自投-巴西-安装',
        'adset_id': 'adset_1',
        'adset_name': '广泛受众0606',
        'ad_id': 'ad_1',
        'ad_name': '素材2',
        'external_app': 'TUGAO',
        'new_registered_users': 971,
        'high_value_l1_female_18_40_users': 669,
        'auto_apply_message_users': 355,
        'im_user_message_ge_3_users': 183,
        'guild_join_success_users': 66,
        'guild_join_success_no_wa_users': 8,
        'guild_join_total_users': 66,
    }
    row.update(overrides)
    return row


def test_tugao_funnel_api_row_maps_to_fact_row():
    fact = tugao_funnel_api_row_to_fact(_api_row())

    assert fact['date'] == '2026-06-22'
    assert fact['data_source'] == 'TugaoFunnel'
    assert fact['platform'] == 'Meta'
    assert fact['app_id'] == ''
    assert fact['external_app'] == 'TUGAO'
    assert fact['country'] == 'Brazil'
    assert fact['campaign'] == '自投-巴西-安装'
    assert fact['ad_group'] == '广泛受众0606'
    assert fact['ad'] == '素材2'
    assert fact['onsite_registrations'] == 971
    assert fact['high_value_users'] == 669
    assert fact['im_entries'] == 355
    assert fact['im_manual_reply_3'] == 183
    assert fact['guild_joins'] == 66
    assert fact['promotion_guild_joins'] == 66
    assert fact['organic_guild_joins'] == 0


def test_tugao_funnel_api_internal_media_maps_to_organic_fact_row():
    fact = tugao_funnel_api_row_to_fact(_api_row(media_source='Internal', guild_join_total_users=24))

    assert fact['platform'] == 'Internal'
    assert fact['source_type'] == '自然量'
    assert fact['promotion_guild_joins'] == 0
    assert fact['organic_guild_joins'] == 24


def test_tugao_funnel_api_unknown_media_maps_to_internal_organic_fact_row():
    fact = tugao_funnel_api_row_to_fact(_api_row(
        media_source='Unknown',
        campaign_id='',
        campaign_name='',
        adset_id='',
        adset_name='',
        ad_id='',
        ad_name='',
        guild_join_total_users=17,
    ))

    assert fact['platform'] == 'Internal'
    assert fact['source_type'] == '自然量'
    assert fact['guild_joins'] == 17
    assert fact['promotion_guild_joins'] == 0
    assert fact['organic_guild_joins'] == 17


def test_tugao_funnel_api_fetch_uses_cursor_only_for_second_page():
    session = _Session([
        _Response({
            'ok': True,
            'data': [_api_row(ad_id='ad_1')],
            'metrics_definition': {'new_registered_users': '站内注册'},
            'has_more': True,
            'next_cursor': 'cursor-2',
        }),
        _Response({
            'ok': True,
            'data': [_api_row(ad_id='ad_2', ad_name='素材3')],
            'has_more': False,
            'next_cursor': '',
        }),
    ])
    client = TugaoFunnelDailyMetricsClient(
        token='production-token',
        base_url='https://api.example.invalid/funnel',
        session=session,
        page_size=1,
    )

    result = client.fetch(start_date=date(2026, 6, 1), end_date=date(2026, 6, 2), page_size=1)

    assert result.pages == 2
    assert result.raw_row_count == 2
    assert len(result.rows) == 2
    assert session.calls[0]['params']['start_date'] == '2026-06-01'
    assert session.calls[0]['params']['group_by'] == 'date,country,media_source,campaign_id,adset_id,ad_id,external_app'
    assert session.calls[1]['params'] == {'cursor': 'cursor-2', 'page_size': 1}
    assert session.calls[0]['headers']['Authorization'] == 'Bearer production-token'


def test_tugao_funnel_api_can_use_x_bi_token_header():
    session = _Session([_Response({'ok': True, 'data': [_api_row()], 'has_more': False})])
    client = TugaoFunnelDailyMetricsClient(
        token='production-token',
        auth_header='x-bi-api-token',
        session=session,
    )

    client.fetch(start_date=date(2026, 6, 1), end_date=date(2026, 6, 1))

    assert session.calls[0]['headers'] == {'x-bi-api-token': 'production-token'}


def test_tugao_funnel_api_rejects_forbidden_pii_keys():
    with pytest.raises(TugaoFunnelPiiError):
        assert_no_forbidden_pii_keys({'ok': True, 'data': [{'phone': '6280000000'}]})

    assert_no_forbidden_pii_keys({'data': [{'campaign_name': 'Allowed name field'}]})


def test_tugao_funnel_api_validates_params_locally():
    with pytest.raises(ValueError):
        validate_group_by(['date', 'bad_field'])

    with pytest.raises(ValueError):
        validate_date_window(date(2026, 1, 1), date(2026, 4, 5))


def test_tugao_funnel_api_rejects_missing_required_metric():
    row = _api_row()
    row.pop('guild_join_success_users')
    session = _Session([_Response({'ok': True, 'data': [row], 'has_more': False})])
    client = TugaoFunnelDailyMetricsClient(token='test-token', session=session)

    with pytest.raises(TugaoFunnelApiError, match='missing_fields:guild_join_success_users'):
        client.fetch(start_date=date(2026, 6, 1), end_date=date(2026, 6, 1))


def test_tugao_funnel_api_rejects_duplicate_exact_tuple_across_pages():
    session = _Session([
        _Response({'ok': True, 'data': [_api_row()], 'has_more': True, 'next_cursor': 'next'}),
        _Response({'ok': True, 'data': [_api_row()], 'has_more': False}),
    ])
    client = TugaoFunnelDailyMetricsClient(token='test-token', session=session)

    with pytest.raises(TugaoFunnelApiError, match='duplicate_qualified_tuple'):
        client.fetch(start_date=date(2026, 6, 1), end_date=date(2026, 6, 1))


def test_tugao_funnel_api_rejects_cursor_loop_and_http_error():
    loop_session = _Session([
        _Response({'ok': True, 'data': [_api_row(ad_id='ad_1')], 'has_more': True, 'next_cursor': 'same'}),
        _Response({'ok': True, 'data': [_api_row(ad_id='ad_2')], 'has_more': True, 'next_cursor': 'same'}),
    ])
    with pytest.raises(TugaoFunnelApiError, match='cursor_loop'):
        TugaoFunnelDailyMetricsClient(token='test-token', session=loop_session).fetch(
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 1),
        )

    error_session = _Session([_Response({}, status_code=503)])
    with pytest.raises(TugaoFunnelApiError, match='http_503'):
        TugaoFunnelDailyMetricsClient(token='test-token', session=error_session).fetch(
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 1),
        )
