import re

from fastapi.testclient import TestClient

from app.main import create_app


OPS_PRIMARY_PAGES = [
    '/ops/production-ops',
    '/ops/registration-group-approval-batch-members',
    '/ops/accounts',
    '/ops/intake-bot-presets',
    '/ops/group-atmosphere',
    '/ops/intake-submit',
]

COMMON_ADMIN_NAV = [
    ('/ops', '管理员看板'),
    ('/ops/intake-submit', '绑定中心'),
    ('/ops/production-ops', '群审批控制台'),
    ('/ops/registration-group-approval-batch-members', '注册群审批留存页'),
    ('/ops/group-atmosphere', '群聊天助手'),
    ('/ops/accounts', '账号设置'),
]


def make_client():
    return TestClient(create_app({'DB_PATH': ':memory:', 'AUTO_LARK_REPLY': False, 'AUTH_ENABLED': False}))


def first_nav_links(html: str):
    match = re.search(r'<div\s+class=["\'](?:shell-nav|nav)["\'][^>]*>(.*?)</div>', html, re.S)
    assert match, 'page should render one shared nav container'
    return re.findall(r'<a\s+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', match.group(1), re.S)


def strip_shared_shell_style(html: str) -> str:
    return re.sub(
        r'<style\s+data-ops-shell-normalized="true"[^>]*>.*?</style>\s*<script\s+data-ops-shell-active="true"[^>]*>.*?</script>',
        '',
        html,
        flags=re.S,
    )


def test_primary_ops_pages_use_single_shared_shell_and_nav():
    client = make_client()
    for path in OPS_PRIMARY_PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        html = response.text
        assert html.count('data-ops-shell-normalized="true"') == 1, path
        assert html.count('data-ops-shell-active="true"') == 1, path
        assert 'data-ops-shell-page=' in html, path
        assert first_nav_links(html) == COMMON_ADMIN_NAV, path


def test_primary_ops_pages_do_not_redeclare_base_layout_css_outside_shared_shell():
    client = make_client()
    banned_patterns = [
        r'\.page-shell\s*[{,]',
        r'\.shell-nav\s*[{,]',
        r'(^|[}\s])\.nav\s*[{,]',
        r'(^|[}\s])\.hero\s*[{,]',
        r'(^|[}\s])\.card\s*[{,]',
        r'(^|[}\s])table\s*[{,]',
        r'(^|[}\s])th\s*[{,]',
        r'(^|[}\s])td\s*[{,]',
        r'(^|[}\s])\.toolbar\s*[{,]',
    ]
    for path in OPS_PRIMARY_PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        local_html = strip_shared_shell_style(response.text)
        for pattern in banned_patterns:
            assert not re.search(pattern, local_html), f'{path} still redeclares base selector {pattern}'


def test_shared_shell_exposes_geometry_contract_tokens():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert '--ops-nav-width:248px' in html
    assert '--ops-content-left-gap:32px' in html
    assert '--ops-card-gap:16px' in html
    assert '--ops-hero-min-height:72px' in html
    assert '--ops-table-row-padding-y:11px' in html
    assert 'overflow-x:hidden!important' in html
