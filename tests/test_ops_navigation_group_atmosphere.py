from fastapi.testclient import TestClient

from app.main import create_app


def make_client(settings=None):
    cfg = {"DB_PATH": ":memory:", "AUTO_LARK_REPLY": False}
    if settings:
        cfg.update(settings)
    return TestClient(create_app(cfg))


def assert_common_nav(html: str):
    expected = [
        ('/ops', '运营工作台'),
        ('/ops/intake-bot-presets', '收口配置中心'),
        ('/ops/production-ops', '群审批控制台'),
        ('/ops/group-atmosphere', '群活跃助手'),
        ('/ops/registration-group-approval-batch-members', '注册群审批留存页'),
        ('/ops/official-group-bridge', '官方群审批桥接台'),
        ('/ops/accounts', '账号管理'),
    ]
    assert 'shell-nav' in html
    for href, label in expected:
        assert f'href="{href}"' in html or f'href=\"{href}\"' in html
        assert label in html


def test_group_atmosphere_has_visible_ops_page_and_common_nav():
    client = make_client({'AUTH_ENABLED': False})

    response = client.get('/ops/group-atmosphere')

    assert response.status_code == 200
    html = response.text
    assert '群活跃助手' in html
    assert 'group-atmosphere-configs' in html
    assert '/api/ops/group-atmosphere/configs' in html
    assert_common_nav(html)


def test_production_ops_page_exposes_group_atmosphere_entry_and_common_nav():
    client = make_client({'AUTH_ENABLED': False})

    response = client.get('/ops/production-ops')

    assert response.status_code == 200
    html = response.text
    assert '群活跃助手' in html
    assert 'href="/ops/group-atmosphere"' in html or 'href=\"/ops/group-atmosphere\"' in html
    assert_common_nav(html)
