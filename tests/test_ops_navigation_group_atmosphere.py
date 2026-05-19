import re
import shutil
import subprocess

from fastapi.testclient import TestClient

from app.main import create_app


def make_client(settings=None):
    cfg = {"DB_PATH": ":memory:", "AUTO_LARK_REPLY": False}
    if settings:
        cfg.update(settings)
    return TestClient(create_app(cfg))


COMMON_ADMIN_NAV = [
    ('/ops', '管理员看板'),
    ('/ops/intake-submit', '绑定中心'),
    ('/ops/production-ops', '群审批控制台'),
    ('/ops/registration-group-approval-batch-members', '注册群审批留存页'),
    ('/ops/group-atmosphere', '群聊天助手'),
    ('/ops/accounts', '账号设置'),
]


def extract_first_nav_links(html: str):
    match = re.search(r"<div\s+class=[\"'](?:shell-nav|nav)[\"'][^>]*>(.*?)</div>", html, re.S)
    assert match, 'page should render the shared ops navigation'
    return re.findall(r"<a\s+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", match.group(1), re.S)


def assert_common_nav(html: str):
    assert 'shell-nav' in html or 'class="nav"' in html or 'class=\"nav\"' in html
    assert extract_first_nav_links(html) == COMMON_ADMIN_NAV


def test_group_atmosphere_has_visible_ops_page_and_common_nav():
    client = make_client({'AUTH_ENABLED': False})

    response = client.get('/ops/group-atmosphere')

    assert response.status_code == 200
    html = response.text
    assert '群聊天助手' in html
    assert '群聊天助手' in html
    assert 'ga_accounts' in html
    assert 'ga_accounts_card' in html
    assert 'ga_learning_upload_card' in html
    assert '<h2>话术学习</h2>' not in html
    assert 'ga_upload_chat_btn' in html
    assert html.index('id="ga_role_bridge_card"') < html.index('id="ga_accounts_card"')
    assert 'data-layout="ops-workbench-redesign"' in html
    assert '桥接操作区' in html
    assert '新增桥接' in html
    assert 'WhatsApp 账号与群组' in html
    assert 'ga_editor_card' in html
    assert 'ga_editor_modal' in html
    assert 'modal-card' in html
    assert html.index('id="ga_accounts_card"') < html.index('id="ga_editor_modal"')
    assert 'openNewAccountEditor' in html
    assert 'closeAccountEditor' in html
    assert 'setSelectedAtmosphereAccountKey' in html
    assert 'id="ga_reload_btn"' not in html
    assert 'ga_reload_btn:()=>reloadAll()' not in html
    assert '.group-card-grid{display:flex;flex-direction:column' in html
    assert "setSelectedAtmosphereAccountKey('${esc(r.account_key)}');startAtmosphereQr(false)" in html
    assert "selectAtmosphereAccount('${esc(r.account_key)}');startAtmosphereQr(false)" not in html
    assert '账号用途' not in html
    assert '运行状态' not in html
    assert '登录状态' in html
    assert 'Runtime' not in html
    assert '当前账号' not in html
    assert '像群审批后台一样管理多个 WhatsApp 账号' not in html
    assert '系统会学习该地区/角色的常用表达' not in html
    assert '/api/ops/group-atmosphere/accounts' in html
    assert 'linear-gradient(135deg,#fff 0%,#f8fbff 56%,#eef5ff 100%)' not in html
    assert '.ga-page-head{display:flex!important;align-items:flex-start!important;justify-content:space-between!important;gap:16px!important;margin:0!important;padding:20px 24px!important;background:var(--crm-panel)!important;border:1px solid var(--crm-border)!important;border-radius:24px!important;box-shadow:var(--crm-shadow-card)!important;}' in html
    assert '.ga-proto-page .ga-workbench-stats ~ .ga-proto-stack{margin-top:var(--crm-card-gap,16px)!important;}' in html
    assert_common_nav(html)


def test_ops_navigation_uses_exact_active_link_not_hardcoded_nth_child():
    client = make_client({'AUTH_ENABLED': False})

    response = client.get('/ops/group-atmosphere')

    assert response.status_code == 200
    html = response.text
    assert 'data-ops-shell-active="true"' in html
    assert 'canonicalOpsPath' in html
    assert "path.indexOf('/ops/group-atmosphere')===0" not in html
    assert "path.indexOf('/ops/bind-failed-users')===0" not in html
    assert 'data-current-ops-path' in html
    assert '.shell-nav a.is-active,.nav a.is-active' in html
    assert 'a:nth-child(' not in html


def test_ops_active_nav_script_highlights_only_current_product(tmp_path):
    client = make_client({'AUTH_ENABLED': False})
    response = client.get('/ops/group-atmosphere')
    assert response.status_code == 200
    html = response.text
    match = re.search(r'<script data-ops-shell-active="true">(.*?)</script>', html, re.S)
    assert match, 'shared active-nav script should be present'
    script = match.group(1)
    if not shutil.which('node'):
        return
    script_path = tmp_path / 'active_nav_check.js'
    script_path.write_text(
        """
const anchors = [
  {href:'/ops', classList:{set:new Set(), add(x){this.set.add(x)}, remove(x){this.set.delete(x)}, contains(x){return this.set.has(x)}}},
  {href:'/ops/intake-submit', classList:{set:new Set(), add(x){this.set.add(x)}, remove(x){this.set.delete(x)}, contains(x){return this.set.has(x)}}},
  {href:'/ops/production-ops', classList:{set:new Set(), add(x){this.set.add(x)}, remove(x){this.set.delete(x)}, contains(x){return this.set.has(x)}}},
  {href:'/ops/group-atmosphere', classList:{set:new Set(), add(x){this.set.add(x)}, remove(x){this.set.delete(x)}, contains(x){return this.set.has(x)}}},
];
anchors.forEach(a => a.getAttribute = () => a.href);
global.location = { pathname: '/ops/group-atmosphere' };
global.document = {
  readyState: 'complete',
  documentElement: { attrs:{}, setAttribute(k,v){ this.attrs[k]=v; } },
  querySelectorAll(){ return anchors; },
  addEventListener(){}
};
""" + script + """
const active = anchors.filter(a => a.classList.contains('is-active')).map(a => a.href);
if (active.length !== 1 || active[0] !== '/ops/group-atmosphere') {
  throw new Error('bad active nav: ' + JSON.stringify(active));
}
if (anchors[0].classList.contains('is-active')) {
  throw new Error('dashboard should not stay active on group atmosphere');
}
"""
    )
    result = subprocess.run(['node', str(script_path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr

def test_group_atmosphere_buttons_script_is_valid_javascript(tmp_path):
    client = make_client({'AUTH_ENABLED': False})

    response = client.get('/ops/group-atmosphere')

    assert response.status_code == 200
    html = response.text
    match = re.search(r'<script>(.*?)</script>', html, re.S)
    assert match, 'group atmosphere page should include inline button handlers'
    script = match.group(1)
    idx = script.index('split(/')
    assert ord(script[idx + len('split(/')]) == 92
    assert script[idx + len('split(/') + 1] == 'n'
    assert ord(script[idx + len('split(/')]) != 10
    if shutil.which('node'):
        script_path = tmp_path / 'group_atmosphere_inline.js'
        script_path.write_text(script)
        result = subprocess.run(
            ['node', '--check', str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_ops_admin_pages_share_identical_navigation_order():
    client = make_client({'AUTH_ENABLED': False})
    paths = [
        '/ops',
        '/ops/intake-submit',
        '/ops/production-ops',
        '/ops/registration-group-approval-batch-members',
        '/ops/group-atmosphere',
    ]
    navs = {}
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        navs[path] = extract_first_nav_links(response.text)
    assert set(tuple(nav) for nav in navs.values()) == {tuple(COMMON_ADMIN_NAV)}


def test_production_ops_page_keeps_only_nav_group_atmosphere_entry():
    client = make_client({'AUTH_ENABLED': False})

    response = client.get('/ops/production-ops')

    assert response.status_code == 200
    html = response.text
    assert '群聊天助手' in html
    assert 'href="/ops/group-atmosphere"' in html or 'href=\\"/ops/group-atmosphere\\"' in html
    assert 'groupAtmosphereEntryCard' not in html
    assert '进入群活跃助手' not in html
    assert_common_nav(html)


def test_production_ops_approval_account_modal_uses_compact_width_and_no_empty_second_column():
    client = make_client({'AUTH_ENABLED': False})

    response = client.get('/ops/production-ops')

    assert response.status_code == 200
    html = response.text
    assert '.approval-account-editor-card { width:min(960px, calc(100vw - 48px))' in html
    assert '.approval-account-editor-body .section-split { display:block!important;' in html
    assert '.approval-account-editor-body .binding-config-grid { grid-template-columns:minmax(280px, 1.35fr) minmax(220px, .95fr) minmax(160px, .65fr)!important;' in html
    assert '.approval-account-editor-body .binding-meta-grid { grid-template-columns:minmax(170px, 1fr) minmax(140px, .72fr) minmax(140px, .72fr) minmax(160px, .82fr)!important;' in html
