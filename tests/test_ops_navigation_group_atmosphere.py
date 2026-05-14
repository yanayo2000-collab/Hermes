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


def assert_common_nav(html: str):
    expected = [
        ('/ops', '运营工作台'),
        ('/ops/intake-bot-presets', '收口配置中心'),
        ('/ops/production-ops', '群审批控制台'),
        ('/ops/group-atmosphere', '群聊天助手'),
        ('/ops/registration-group-approval-batch-members', '注册群审批留存页'),
        ('/ops/accounts', '账号设置'),
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
    assert '群聊天助手' in html
    assert 'ga_accounts' in html
    assert 'ga_accounts_card' in html
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
    assert '账号用途' in html
    assert '运行状态' in html
    assert '登录状态' in html
    assert 'Runtime' not in html
    assert '当前账号' not in html
    assert '像群审批后台一样管理多个 WhatsApp 账号' not in html
    assert '系统会学习该地区/角色的常用表达' not in html
    assert '/api/ops/group-atmosphere/accounts' in html
    assert_common_nav(html)


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


def test_production_ops_page_keeps_only_nav_group_atmosphere_entry():
    client = make_client({'AUTH_ENABLED': False})

    response = client.get('/ops/production-ops')

    assert response.status_code == 200
    html = response.text
    assert '群聊天助手' in html
    assert 'href="/ops/group-atmosphere"' in html or 'href=\"/ops/group-atmosphere\"' in html
    assert 'groupAtmosphereEntryCard' not in html
    assert '进入群活跃助手' not in html
    assert_common_nav(html)
