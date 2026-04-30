import asyncio
import json
import threading
from pathlib import Path

import requests
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import create_app, parse_manual_cs_message, extract_invalid_group_candidate, LiveLarkReplyAdapter, format_display_phone, validate_fast_intake_fields


class StubLarkMediaAdapter:
    def __init__(self, payload: bytes = b'test-image-bytes'):
        self.payload = payload
        self.calls = []

    def download_image(self, message_id: str, file_key: str) -> bytes:
        self.calls.append((message_id, file_key))
        return self.payload


class StubLarkReplyAdapter:
    def __init__(self):
        self.calls = []

    def reply_text(self, *, message_id: str, text: str) -> dict:
        self.calls.append({'message_id': message_id, 'text': text})
        return {'ok': True}


class StubOcrAdapter:
    def __init__(self, raw_text: str = ""):
        self.raw_text = raw_text
        self.calls = []

    def extract_text(self, image_ref: str) -> dict:
        self.calls.append(image_ref)
        return {"raw_text": self.raw_text, "engine": "stub_ocr"}


class StubCrmDropdownAdapter:
    def __init__(self, apps=None, depts=None, error=None):
        self.apps = apps or []
        self.depts = depts or []
        self.error = error

    def get_apps(self):
        if self.error:
            raise RuntimeError(self.error)
        return list(self.apps)

    def get_depts(self):
        if self.error:
            raise RuntimeError(self.error)
        return list(self.depts)


def make_client(settings=None):
    cfg = {"DB_PATH": ":memory:", "AUTO_LARK_REPLY": False}
    if settings:
        cfg.update(settings)
    app = create_app(cfg)
    return TestClient(app)


def test_live_lark_reply_adapter_normalizes_markdown_bold_to_lark_text_tags():
    adapter = LiveLarkReplyAdapter(app_id='cli_test', app_secret='secret_test')
    normalized = adapter._normalize_text_markup('**❌ Device Duplicate Registration**\nPhone: 123')
    assert normalized.startswith('<b>❌ Device Duplicate Registration</b>')
    assert '**' not in normalized



def test_format_display_phone_normalizes_variants_to_area_code_space_number():
    assert format_display_phone('85249519581', area_code=62) == '+62 85249519581'
    assert format_display_phone('+6285249519581') == '+62 85249519581'
    assert format_display_phone('+62 852-4951-9581') == '+62 85249519581'
    assert format_display_phone('0852-4951-9581', area_code=62) == '+62 085249519581'
    assert format_display_phone('+621****9911') == '+621****9911'



def test_create_app_enables_rapidocr_from_env(monkeypatch):
    monkeypatch.setenv('ENABLE_RAPIDOCR', 'true')
    app = create_app({'DB_PATH': ':memory:', 'AUTO_LARK_REPLY': False})
    assert app.state.service.ocr_adapter is not None


def test_create_app_degrades_gracefully_when_live_crm_login_fails():
    class FailingLiveCrmAdapter:
        def __init__(self, *, base_url, username, password, session=None):
            self.base_url = base_url
            self.username = username
            self.password = password
        def login(self):
            raise RuntimeError('CRM login returned non-JSON response: status=502 body=')

    with patch('app.main.LiveCrmAdapter', FailingLiveCrmAdapter):
        app = create_app({
            'DB_PATH': ':memory:',
            'AUTO_LARK_REPLY': False,
            'CRM_BASE_URL': 'http://crm.example.test',
            'CRM_USERNAME': 'Hermes',
            'CRM_PASSWORD': 'secret',
        })

    runtime = app.state.service.runtime_health()
    assert runtime['crm']['enabled'] is True
    assert runtime['crm']['status'] == 'degraded'
    assert runtime['crm']['base_url'] == 'http://crm.example.test'
    assert runtime['crm']['username'] == 'Hermes'
    assert 'status=502' in (runtime['crm']['login_error'] or '')



def test_create_app_refuses_live_bind_simulation_without_explicit_override(tmp_path):
    app = create_app({
        'DB_PATH': str(tmp_path / 'live.db'),
        'AUTO_LARK_REPLY': False,
        'AUTO_BIND_SIMULATION': True,
    })
    runtime = app.state.service.runtime_health()
    assert runtime['simulation']['auto_bind_simulation'] is False
    assert runtime['simulation']['mode'] == 'live'



def test_file_backed_app_enables_async_ingress_queue_and_worker_controls(tmp_path):
    app = create_app({
        'DB_PATH': str(tmp_path / 'async-live.db'),
        'AUTO_LARK_REPLY': False,
        'INGRESS_WORKER_ENABLED': False,
    })
    runtime = app.state.service.runtime_health()
    assert runtime['ingress']['async_default'] is True
    assert runtime['ingress']['worker_enabled'] is False



def test_parse_manual_cs_message_supports_labeled_form():
    parsed = parse_manual_cs_message(
        text="手机号：+62 81234567890\nID：45678901\n注册群组：Piso-5\n应用：Linky\n公会：Piso"
    )

    assert parsed["mobile"] == "81234567890"
    assert parsed["area_code"] == 62
    assert parsed["country"] == "Indonesia"
    assert parsed["account_id"] == "45678901"
    assert parsed["registration_group"] == "Piso-5"
    assert parsed["app_name"] == "Linky"
    assert parsed["dept_name"] == "Piso"



def test_parse_manual_cs_message_supports_grouped_space_phone_form():
    parsed = parse_manual_cs_message(
        text="手机号：+62 899 9999 9999\nID：45678901\n注册群组：Piso-5\n应用：Linky\n公会：Piso"
    )

    assert parsed["mobile"] == "89999999999"
    assert parsed["area_code"] == 62
    assert parsed["country"] == "Indonesia"
    assert parsed["account_id"] == "45678901"



def test_validate_fast_intake_fields_accepts_grouped_space_phone():
    assert validate_fast_intake_fields(
        mobile='+62 899 9999 9999',
        app_name='Linky',
        account_id='45678901',
    ) is None



def test_parse_manual_cs_message_supports_messy_free_text_form():
    parsed = parse_manual_cs_message(
        text="Linky 的用户，Piso组，公会Piso，手机号 081234567891，id是 56789012，麻烦处理"
    )

    assert parsed["mobile"] == "081234567891"
    assert parsed["account_id"] == "56789012"
    assert parsed["registration_group"] == "Piso"
    assert parsed["app_name"] == "Linky"
    assert parsed["dept_name"] == "Piso"



def test_parse_manual_cs_message_does_not_infer_dept_from_registration_group_token():
    parsed = parse_manual_cs_message(
        text='88909200\n+62 18812321188\nPERMATA-88'
    )

    assert parsed['registration_group'] == 'PERMATA-88'
    assert parsed['dept_name'] is None



def test_parse_manual_cs_message_supports_text_plus_image_hint_form():
    parsed = parse_manual_cs_message(
        text="手机号 081234567892，Linky，注册群组 Piso-9，截图里有ID和公会",
        image_ocr_text="UID 67890123\nAgensi saya Permata-7"
    )

    assert parsed["mobile"] == "081234567892"
    assert parsed["account_id"] == "67890123"
    assert parsed["registration_group"] == "Piso-9"
    assert parsed["app_name"] == "Linky"
    assert parsed["dept_name"] == "Permata-7"
    assert parsed["evidence"]["image_ocr_used"] is True



def test_parse_manual_cs_message_reports_missing_fields_and_conflicts():
    parsed = parse_manual_cs_message(
        text="手机号 081234567893，Linky，公会Permata，注册群组 Piso-18，ID 88888888",
        image_ocr_text="UID 99999999\nGroup Piso-18"
    )

    assert parsed["account_id"] == "99999999"
    assert parsed["confidence"] < 1
    assert "account_id_conflict" in parsed["conflicts"]
    assert parsed["missing_fields"] == ["invite_code"]



def test_parse_manual_cs_message_reports_missing_required_fields():
    parsed = parse_manual_cs_message(text="只有 Linky，没有别的信息")

    assert parsed["app_name"] == "Linky"
    assert "mobile" in parsed["missing_fields"]
    assert "account_id" in parsed["missing_fields"]
    assert "invite_code" in parsed["missing_fields"]
    assert parsed["confidence"] < 0.5



def test_parse_manual_cs_message_extracts_personal_invite_code_from_text():
    parsed = parse_manual_cs_message(
        text="手机号：+62 81234567890\nID：45678901\n注册群组：Piso-5\n应用：Linky\n公会：Piso\n个人邀请码：EKVFGQ"
    )

    assert parsed["invite_code"] == "EKVFGQ"
    assert "invite_code" not in parsed["missing_fields"]



def test_parse_manual_cs_message_extracts_bare_multiline_invite_code():
    parsed = parse_manual_cs_message(
        text="+62 12312966899\n89008911\nPERMATA-88\nGMJY7O"
    )

    assert parsed["mobile"] == "12312966899"
    assert parsed["account_id"] == "89008911"
    assert parsed["registration_group"] == "PERMATA-88"
    assert parsed["invite_code"] == "GMJY7O"
    assert "invite_code" not in parsed["missing_fields"]



def test_parse_manual_cs_message_extracts_bare_multiline_all_letter_invite_code():
    parsed = parse_manual_cs_message(
        text='+62 12312966899\n89008911\nPERMATA-88\nGMJHJK',
        image_ocr_text=None,
    )

    assert parsed["invite_code"] == "GMJHJK"
    assert "invite_code" not in parsed["missing_fields"]



def test_parse_manual_cs_message_normalizes_hyphenated_phone_input():
    parsed = parse_manual_cs_message(
        text='Phone: +62 852-4951-9581\nGroup: PERMATA-909\nID: 51669366\nCode: EKVFGQ',
        image_ocr_text=None,
    )

    assert parsed['mobile'] == '85249519581'
    assert parsed['area_code'] == 62
    assert parsed['country'] == 'Indonesia'
    assert parsed['invite_code'] == 'EKVFGQ'



def test_parse_manual_cs_message_normalizes_parenthesized_phone_input():
    parsed = parse_manual_cs_message(
        text='Phone: +62 (852) 4951-9581\nGroup: PERMATA-909\nID: 51669366\nCode: EKVFGQ',
        image_ocr_text=None,
    )

    assert parsed['mobile'] == '85249519581'
    assert parsed['area_code'] == 62
    assert parsed['country'] == 'Indonesia'



def test_validate_fast_intake_fields_accepts_us_parenthesized_phone():
    assert validate_fast_intake_fields(
        mobile='+1 (650) 555-1212',
        app_name='Linky',
        account_id='45678901',
    ) is None



def test_parse_manual_cs_message_supports_hong_kong_grouped_space_phone():
    parsed = parse_manual_cs_message(
        text='Phone: +852 4456 8277\nGroup: Permata-66\nID: 51858602\nCode: PKUYW9',
        image_ocr_text=None,
    )

    assert parsed['mobile'] == '44568277'
    assert parsed['area_code'] == 852
    assert parsed['country'] == 'Hong Kong'
    assert parsed['account_id'] == '51858602'
    assert parsed['registration_group'] == 'Permata-66'
    assert parsed['invite_code'] == 'PKUYW9'



def test_validate_fast_intake_fields_accepts_hong_kong_grouped_space_phone():
    assert validate_fast_intake_fields(
        mobile='+852 4456 8277',
        app_name='Linky',
        account_id='51858602',
    ) is None



def test_parse_manual_cs_message_supports_united_kingdom_grouped_space_phone():
    parsed = parse_manual_cs_message(
        text='Phone: +44 7700 900123\nGroup: Permata-66\nID: 51858602\nCode: PKUYW9',
        image_ocr_text=None,
    )

    assert parsed['mobile'] == '7700900123'
    assert parsed['area_code'] == 44
    assert parsed['country'] == 'United Kingdom'
    assert parsed['account_id'] == '51858602'
    assert parsed['registration_group'] == 'Permata-66'
    assert parsed['invite_code'] == 'PKUYW9'



def test_validate_fast_intake_fields_accepts_united_kingdom_grouped_space_phone():
    assert validate_fast_intake_fields(
        mobile='+44 7700 900123',
        app_name='Linky',
        account_id='51858602',
    ) is None



def test_parse_manual_cs_message_supports_unmapped_explicit_country_code_phone():
    parsed = parse_manual_cs_message(
        text='Phone: +971 50 123 4567\nGroup: Permata-66\nID: 51858602\nCode: PKUYW9',
        image_ocr_text=None,
    )

    assert parsed['mobile'] == '501234567'
    assert parsed['area_code'] == 971
    assert parsed['country'] == 'United Arab Emirates'
    assert parsed['account_id'] == '51858602'
    assert parsed['registration_group'] == 'Permata-66'
    assert parsed['invite_code'] == 'PKUYW9'



def test_validate_fast_intake_fields_accepts_unmapped_explicit_country_code_phone():
    assert validate_fast_intake_fields(
        mobile='+971 50 123 4567',
        app_name='Linky',
        account_id='51858602',
    ) is None



def test_parse_manual_cs_message_normalizes_homoglyph_invite_code_from_text():
    parsed = parse_manual_cs_message(
        text='Phone: +62 85249519581\nGroup: PERMATA-909\nID: 51669366\nCode: 7ЕНТ9N',
        image_ocr_text=None,
    )

    assert parsed['invite_code'] == '7EHT9N'
    assert parsed['evidence']['text_invite_code'] == '7EHT9N'
    assert parsed['evidence']['invite_code_had_homoglyphs'] is True
    assert parsed['evidence']['invite_code_raw_input'] == '7ЕНТ9N'



def test_lark_event_rejects_invite_code_with_unsupported_non_latin_characters():
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': StubLarkReplyAdapter(),
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Permata',
    })
    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_bad_code'}},
            'message': {
                'message_id': 'om_bad_code',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 85249519581\\nPERMATA-909\\n51669366\\nCode 7ЕНЖ9N"}'
            }
        }
    })

    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'invalid_invite_code_format'
    assert body['reply_text'].startswith('**🚫 Invalid Code. Use 6 English letters or letters+digits only.**')



def test_parse_manual_cs_message_rejects_bare_multiline_invite_code_shorter_than_6():
    parsed = parse_manual_cs_message(
        text="+62 12312966899\n89008911\nPERMATA-88\nGMJHK"
    )

    assert parsed["invite_code"] is None
    assert "invite_code" in parsed["missing_fields"]



def test_parse_manual_cs_message_rejects_bare_multiline_invite_code_longer_than_6():
    parsed = parse_manual_cs_message(
        text="+62 12312966899\n89008911\nPERMATA-88\nGMJHJKL"
    )

    assert parsed["invite_code"] is None
    assert "invite_code" in parsed["missing_fields"]



def test_registration_group_batching_ready_when_reaches_30():
    client = make_client()
    response = client.post(
        "/api/ops/approval-batches/evaluate",
        json={
            "approval_type": "registration_group",
            "registration_group": "Piso-30",
            "pending_count": 30,
            "oldest_pending_at": "2026-04-15T10:00:00Z",
            "now": "2026-04-15T10:19:00Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["release_count"] == 30
    assert body["reason_code"] == "batch_size_reached"



def test_registration_group_batching_flushes_after_30_minutes_even_if_under_30():
    client = make_client()
    response = client.post(
        "/api/ops/approval-batches/evaluate",
        json={
            "approval_type": "registration_group",
            "registration_group": "Piso-31",
            "pending_count": 12,
            "oldest_pending_at": "2026-04-15T10:00:00Z",
            "now": "2026-04-15T10:31:00Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["release_count"] == 12
    assert body["reason_code"] == "timeout_flush"



def test_official_group_batching_ready_when_reaches_10():
    client = make_client()
    response = client.post(
        "/api/ops/approval-batches/evaluate",
        json={
            "approval_type": "official_group",
            "registration_group": "Official-A",
            "pending_count": 10,
            "oldest_pending_at": "2026-04-15T10:00:00Z",
            "now": "2026-04-15T10:15:00Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["release_count"] == 10
    assert body["reason_code"] == "batch_size_reached"



def test_official_group_batching_flushes_after_30_minutes_even_if_under_10():
    client = make_client()
    response = client.post(
        "/api/ops/approval-batches/evaluate",
        json={
            "approval_type": "official_group",
            "registration_group": "Official-B",
            "pending_count": 4,
            "oldest_pending_at": "2026-04-15T10:00:00Z",
            "now": "2026-04-15T10:31:00Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["release_count"] == 4
    assert body["reason_code"] == "timeout_flush"



def test_approval_batching_not_ready_before_threshold_or_timeout():
    client = make_client()
    response = client.post(
        "/api/ops/approval-batches/evaluate",
        json={
            "approval_type": "official_group",
            "registration_group": "Official-C",
            "pending_count": 3,
            "oldest_pending_at": "2026-04-15T10:00:00Z",
            "now": "2026-04-15T10:20:00Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["release_count"] == 0
    assert body["reason_code"] == "waiting_for_batch"



def test_ops_approval_batch_queue_returns_ready_and_waiting_groups():
    client = make_client()
    response = client.get('/api/ops/approval-batch-queue')
    assert response.status_code == 200
    body = response.json()
    assert 'registration_groups' in body
    assert 'official_groups' in body
    reg_ready = next(row for row in body['registration_groups'] if row['registration_group'] == 'Piso-30')
    off_waiting = next(row for row in body['official_groups'] if row['registration_group'] == 'Official-C')
    assert reg_ready['ready'] is True
    assert reg_ready['release_count'] == 30
    assert off_waiting['ready'] is False
    assert off_waiting['reason_code'] == 'waiting_for_batch'



def test_ops_page_includes_approval_batch_queue_section():
    client = make_client()
    response = client.get('/ops')
    assert response.status_code == 200
    body = response.text
    assert '/api/ops/approval-batch-queue' in body
    assert '审批批次队列' in body
    assert '注册群批次' in body
    assert '官方群批次' in body


def test_intake_bot_presets_page_loads():
    client = make_client({'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})
    response = client.get('/ops/intake-bot-presets')
    assert response.status_code == 200
    body = response.text
    assert '收口机器人配置中心' in body
    assert '/api/ops/intake-bot-presets' in body
    assert '/api/ops/guild-executors' in body
    assert '/ops/production-ops' in body
    assert '打开群审批控制台' in body
    assert '进入群审批控制台' in body
    assert 'robot_name' in body
    assert 'default_app' in body
    assert 'default_guild' in body
    assert '编辑名称' in body
    assert '公会执行器配置' in body
    assert '代理地区（proxy_region）' in body
    assert 'password_secret_ref' in body
    assert '保存公会执行器' in body
    assert '回填编辑' in body
    assert '后台地址' in body
    assert '登录账号' in body
    assert 'renderExecutorProxyRegionOptions' in body
    assert '先按 15 个大型城市预置' in body
    assert '历史值（待改）' in body
    assert '已分配给' in body
    assert '删除执行器' in body
    assert 'deleteExecutor' in body
    assert 'default_agency' not in body


def test_production_ops_page_loads():
    client = make_client({
        'LARK_APP_ID': 'cli_test_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'OFFICIAL_GROUP_APPROVAL_EXECUTOR_KIND': 'webhook',
        'OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL': 'http://127.0.0.1:55801/official-group/approve',
    })
    response = client.get('/ops/production-ops')
    assert response.status_code == 200
    body = response.text
    assert '群审批控制台' in body
    assert '/api/ops/production-ops-daemon' in body
    assert '/api/ops/whatsapp-approval-accounts' in body
    assert '/api/ops/whatsapp-approval-candidates' in body
    assert '/api/ops/official-group-bridge-summary' in body
    assert 'WhatsApp 审批账号' in body
    assert '官方群总览' in body
    assert '账号概览' not in body
    assert '校验框架' not in body
    assert '账号校验候选池' not in body
    assert '账号校验执行框架' not in body
    assert '账号运行态说明' not in body
    assert '校验与调度摘要' not in body
    assert '生产守护开关' not in body
    assert 'saveProductionOpsDaemonConfig' not in body
    assert 'production_ops_enabled_toggle' not in body
    assert 'launchd' in body
    assert '实时状态卡片' not in body
    assert '账号名称（account_name）' in body
    assert '逐群绑定配置（最多3组）' in body
    assert 'wa_group_area_1' in body
    assert 'wa_group_area_2' in body
    assert 'wa_group_area_3' in body
    assert 'wa_group_notify_profile_name_1' in body
    assert 'wa_group_notify_profile_name_2' in body
    assert 'wa_group_notify_profile_name_3' in body
    assert '审批人数阈值' in body
    assert '审批超时分钟' in body
    assert '自动恢复 worker' in body
    assert '/api/ops/whatsapp-approval-accounts/' in body
    assert '生成二维码' in body
    assert '刷新状态' in body
    assert '重置会话' in body
    assert '绑定二维码' in body
    assert '配置状态' in body
    assert '登录状态' in body
    assert 'approvalQrModal' in body
    assert 'openApprovalQrModal' in body
    assert 'mergeApprovalSessionState' in body
    assert '重新生成二维码' in body
    assert '/api/ops/whatsapp-approval-accounts/' in body

    assert '地区选项源' in body
    assert 'area_options_text' in body
    assert 'saveAreaOptions' in body
    assert 'const effectiveCurrentValues = Array.isArray(currentValues) && currentValues.length' in body
    assert "document.getElementById(`wa_group_area_${index}`)?.value" in body
    assert '通知机器人（Lark）' not in body
    assert '单次审批人数阈值（approval_count_threshold）' not in body
    assert '单次审批超时分钟（approval_timeout_minutes）' not in body
    assert '自动自恢复 worker（auto_recover_worker）' not in body
    assert '自动恢复 worker' in body
    assert '监控时间段（最多3个）' in body
    assert '保存本组' not in body
    assert '删除本组' not in body
    assert 'wa_group_schedule_window_1_1' in body
    assert 'wa_group_schedule_window_2_1' in body
    assert 'wa_group_schedule_window_3_1' in body
    assert 'wa_group_registration_group_1' in body
    assert 'wa_group_group_id_1' in body
    assert 'wa_group_name_1' in body
    assert '高级项' in body
    assert '填写说明' not in body
    assert '配置口径' not in body
    assert 'toggleSwitch' in body
    assert 'wa_enabled_toggle' not in body
    assert 'setApprovalAccountEnabled' in body
    assert '监控中' in body
    assert '已关闭' in body
    assert 'type=\"hidden\" id=\"wa_account_key\"' in body
    assert 'wa_group_notify_profile_name_1' in body
    assert 'wa_group_approval_count_threshold_1' in body
    assert 'wa_group_approval_timeout_minutes_1' in body
    assert 'wa_group_auto_recover_worker_1' in body
    assert '本群监控' in body
    assert 'wa_group_enabled_1' in body


def test_official_group_bridge_page_redirects_to_bridge_service():
    client = make_client({
        'OFFICIAL_GROUP_APPROVAL_EXECUTOR_KIND': 'webhook',
        'OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL': 'http://127.0.0.1:55801/official-group/approve',
    })
    response = client.get('/ops/official-group-bridge', follow_redirects=False)
    assert response.status_code == 307
    assert response.headers['location'] == 'http://127.0.0.1:55801/ops/official-group-bridge'



def test_production_ops_daemon_config_can_be_saved_from_config_center_api():
    client = make_client({'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})

    initial = client.get('/api/ops/production-ops-daemon')
    assert initial.status_code == 200
    initial_body = initial.json()
    assert initial_body['config']['registration_group'] == '🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎'

    saved = client.post('/api/ops/production-ops-daemon', json={
        'enabled': True,
        'interval_seconds': 25,
        'notify_chat_id': 'oc_test_chat',
        'area': 'Indonesia',
        'remark': 'from config center',
        'approved_count': 1,
        'auto_recover_worker': True,
    })
    assert saved.status_code == 200
    saved_body = saved.json()
    assert saved_body['saved'] is True
    assert saved_body['config']['enabled'] is True
    assert saved_body['config']['interval_seconds'] == 25.0
    assert saved_body['config']['registration_group'] == '🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎'
    assert saved_body['config']['api_base_url'] == 'http://127.0.0.1:8011'
    assert saved_body['config']['worker_base_url'] == 'http://127.0.0.1:8787'
    assert saved_body['runtime_sync']['attempted'] is False

    refreshed = client.get('/api/ops/production-ops-daemon')
    assert refreshed.status_code == 200
    refreshed_body = refreshed.json()
    assert refreshed_body['config']['enabled'] is True
    assert refreshed_body['config']['notify_chat_id'] == 'oc_test_chat'
    assert refreshed_body['config']['remark'] == 'from config center'


def test_production_ops_daemon_config_accepts_empty_registration_group_for_account_scoped_control():
    client = make_client({'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})

    saved = client.post('/api/ops/production-ops-daemon', json={
        'enabled': True,
        'registration_group': '',
        'interval_seconds': 30,
        'notify_chat_id': 'oc_test_chat',
        'area': 'Indonesia',
        'remark': 'account scoped control',
        'approved_count': 2,
        'auto_recover_worker': False,
    })
    assert saved.status_code == 200
    body = saved.json()
    assert body['saved'] is True
    assert body['config']['registration_group'] == '🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎'
    assert body['config']['auto_recover_worker'] is False


def test_whatsapp_approval_accounts_can_be_saved_and_listed():
    client = make_client({
        'LARK_APP_ID': 'cli_test_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'OFFICIAL_GROUP_APPROVAL_EXECUTOR_KIND': 'webhook',
        'OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL': 'http://127.0.0.1:55801/official-group/approve',
    })

    area_options = client.get('/api/ops/whatsapp-approval-area-options')
    assert area_options.status_code == 200
    initial_area_body = area_options.json()
    assert [item['label'] for item in initial_area_body['options']] == ['Indonesia', 'Brazil', 'Mexico']
    assert [item['label'] for item in initial_area_body['source_options']] == ['Indonesia', 'Brazil', 'Mexico']

    initial = client.get('/api/ops/whatsapp-approval-accounts')
    assert initial.status_code == 200
    initial_body = initial.json()
    assert initial_body['rows'] == []
    assert any(option['robot_name'] == '审批bot01' for option in initial_body['notify_robot_options'])
    assert [item['label'] for item in initial_body['area_options']] == ['Indonesia', 'Brazil', 'Mexico']

    updated_area_options = client.post('/api/ops/whatsapp-approval-area-options', json={
        'options': ['Indonesia', 'Mexico', 'Brazil', 'Philippines'],
    })
    assert updated_area_options.status_code == 200
    assert [item['label'] for item in updated_area_options.json()['options']] == ['Indonesia', 'Mexico', 'Brazil', 'Philippines']
    assert [item['label'] for item in updated_area_options.json()['source_options']] == ['Indonesia', 'Mexico', 'Brazil', 'Philippines']

    refreshed_options = client.get('/api/ops/whatsapp-approval-accounts')
    assert [item['label'] for item in refreshed_options.json()['area_options']] == ['Indonesia', 'Mexico', 'Brazil', 'Philippines']
    assert [item['label'] for item in refreshed_options.json()['area_option_source']] == ['Indonesia', 'Mexico', 'Brazil', 'Philippines']

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-1', json={
        'account_name': 'WA Admin 1',
        'responsible_type': 'registration_group',
        'group_link_bindings': [
            {
                'link': 'https://chat.whatsapp.com/group-a',
                'group_name': 'PH 审批群 A',
                'area': 'Philippines',
                'notify_profile_name': 'wa-approval-broadcast',
                'enabled': False,
                'registration_group': 'PH Registrations A',
                'group_id': '120363425215002841@g.us',
                'approval_count_threshold': 25,
                'approval_timeout_minutes': 28,
                'auto_recover_worker': True,
                'schedule_windows': [
                    {'start': '00:00', 'end': '23:59'},
                    {'start': '14:00', 'end': '18:00'},
                ],
            },
            {
                'link': 'https://chat.whatsapp.com/group-b',
                'group_name': 'PH 审批群 B',
                'area': 'Philippines',
                'notify_profile_name': 'wa-approval-broadcast',
                'enabled': True,
                'registration_group': 'PH Registrations B',
                'group_id': '120363425215002842@g.us',
                'approval_count_threshold': 31,
                'approval_timeout_minutes': 45,
                'auto_recover_worker': False,
                'schedule_windows': [
                    {'start': '09:00', 'end': '12:00'},
                ],
            },
        ],
        'notify_profile_name': 'wa-approval-broadcast',
        'approval_count_threshold': 25,
        'approval_timeout_minutes': 28,
        'auto_recover_worker': True,
        'schedule_windows': [
            {'start': '00:00', 'end': '23:59'},
            {'start': '14:00', 'end': '18:00'},
        ],
        'enabled': True,
        'notes': 'primary registration approver',
    })
    assert saved.status_code == 200
    body = saved.json()
    assert body['saved'] is True
    assert body['account']['account_key'] == 'wa-admin-1'
    assert body['account']['enabled'] is True
    assert body['account']['status_color'] == 'amber'
    assert body['account']['group_count'] == 2
    assert body['account']['verification_status'] == 'login_unready'
    assert body['account']['verification_status_label'] == '待登录'
    assert body['account']['schedule_active_now'] is True
    assert body['account']['runtime_status'] == 'blocked'
    assert body['account']['status_text'] == '待登录'
    assert body['account']['next_action'] == '先完成扫码登录并通过可用性检测'
    assert body['account']['service_scope']['code'] == 'registration_group_console'
    assert body['account']['verification_checks'][0]['code'] == 'group_link_format'
    assert body['account']['verification_checks'][0]['ok'] is True
    assert body['account']['area'] == 'Philippines'
    bindings = body['account']['group_link_bindings']
    assert len(bindings) == 2
    assert bindings[0]['link'] == 'https://chat.whatsapp.com/group-a'
    assert bindings[0]['group_name'] == 'PH 审批群 A'
    assert bindings[0]['area'] == 'Philippines'
    assert bindings[0]['notify_profile_name'] == 'wa-approval-broadcast'
    assert bindings[0]['enabled'] is False
    assert bindings[0]['registration_group'] == 'PH Registrations A'
    assert bindings[0]['group_id'] == '120363425215002841@g.us'
    assert bindings[0]['approval_count_threshold'] == 25
    assert bindings[0]['approval_timeout_minutes'] == 28
    assert bindings[0]['auto_recover_worker'] is True
    assert bindings[0]['schedule_windows'] == [
        {'start': '00:00', 'end': '23:59'},
        {'start': '14:00', 'end': '18:00'},
    ]
    assert bindings[0]['notify_robot_name'] == '审批bot01'
    assert bindings[0]['approval_rule_text'] == '满25人或满28分钟放行（满足其一即可）'
    assert bindings[1]['link'] == 'https://chat.whatsapp.com/group-b'
    assert bindings[1]['group_name'] == 'PH 审批群 B'
    assert bindings[1]['area'] == 'Philippines'
    assert bindings[1]['notify_profile_name'] == 'wa-approval-broadcast'
    assert bindings[1]['enabled'] is True
    assert bindings[1]['registration_group'] == 'PH Registrations B'
    assert bindings[1]['group_id'] == '120363425215002842@g.us'
    assert bindings[1]['approval_count_threshold'] == 31
    assert bindings[1]['approval_timeout_minutes'] == 45
    assert bindings[1]['auto_recover_worker'] is False
    assert bindings[1]['schedule_windows'] == [
        {'start': '09:00', 'end': '12:00'},
    ]
    assert body['account']['group_binding_runtimes'][0]['notify_profile_name'] == 'wa-approval-broadcast'
    assert body['account']['group_binding_runtimes'][1]['notify_profile_name'] == 'wa-approval-broadcast'
    assert body['account']['group_binding_runtimes'][0]['group_name'] == 'PH 审批群 A'
    assert body['account']['group_binding_runtimes'][1]['group_name'] == 'PH 审批群 B'
    assert body['account']['group_binding_runtimes'][0]['enabled'] is False
    assert body['account']['group_binding_runtimes'][1]['enabled'] is True
    assert body['account']['notify_profile_name'] == 'wa-approval-broadcast'
    assert body['account']['notify_robot_name'] == '审批bot01'
    assert body['account']['approval_count_threshold'] == 25
    assert body['account']['approval_timeout_minutes'] == 28
    assert body['account']['approval_rule_text'] == '满25人或满28分钟放行（满足其一即可）'
    assert body['account']['auto_recover_worker'] is True

    official_saved = client.post('/api/ops/whatsapp-approval-accounts/wa-official-1', json={
        'account_name': 'WA Official 1',
        'responsible_type': 'official_group',
        'group_link_bindings': [
            {
                'link': 'https://chat.whatsapp.com/official-group-a',
                'area': 'Brazil',
                'notify_profile_name': 'wa-approval-broadcast',
                'approval_count_threshold': 100,
                'approval_timeout_minutes': 45,
                'auto_recover_worker': False,
                'schedule_windows': [
                    {'start': '00:00', 'end': '23:59'},
                ],
            },
        ],
        'notify_profile_name': 'wa-approval-broadcast',
        'approval_count_threshold': 100,
        'approval_timeout_minutes': 45,
        'auto_recover_worker': False,
        'schedule_windows': [
            {'start': '00:00', 'end': '23:59'},
        ],
        'enabled': True,
        'notes': 'official approver',
    })
    assert official_saved.status_code == 200
    assert official_saved.json()['account']['verification_status'] == 'login_unready'
    assert official_saved.json()['account']['verification_status_label'] == '待登录'
    assert official_saved.json()['account']['runtime_status'] == 'blocked'
    assert official_saved.json()['account']['status_text'] == '待登录'
    assert official_saved.json()['account']['service_scope']['code'] == 'official_group_bridge'
    assert official_saved.json()['account']['approval_count_threshold'] == 100
    assert official_saved.json()['account']['approval_timeout_minutes'] == 45
    assert official_saved.json()['account']['approval_rule_text'] == '满100人或满45分钟放行（满足其一即可）'
    assert official_saved.json()['account']['group_link_bindings'][0]['link'] == 'https://chat.whatsapp.com/official-group-a'
    assert official_saved.json()['account']['group_link_bindings'][0]['area'] == 'Brazil'
    assert official_saved.json()['account']['group_link_bindings'][0]['notify_profile_name'] == 'wa-approval-broadcast'
    assert official_saved.json()['account']['group_link_bindings'][0]['approval_count_threshold'] == 100
    assert official_saved.json()['account']['group_link_bindings'][0]['approval_timeout_minutes'] == 45
    assert official_saved.json()['account']['group_link_bindings'][0]['auto_recover_worker'] is False
    assert official_saved.json()['account']['group_link_bindings'][0]['schedule_windows'] == [
        {'start': '00:00', 'end': '23:59'},
    ]
    assert official_saved.json()['account']['group_binding_runtimes'][0]['notify_profile_name'] == 'wa-approval-broadcast'
    assert official_saved.json()['account']['auto_recover_worker'] is False

    listed = client.get('/api/ops/whatsapp-approval-accounts')
    assert listed.status_code == 200
    listed_body = listed.json()
    assert len(listed_body['rows']) == 2
    assert listed_body['rows'][0]['responsible_type'] in {'registration_group', 'official_group'}
    assert listed_body['rows'][0]['verification_status_label'] == '待登录'
    assert listed_body['summary']['active_now_accounts'] == 0
    assert listed_body['summary']['ready_accounts'] == 0
    assert listed_body['summary']['verification_pending_accounts'] == 2

    candidates = client.get('/api/ops/whatsapp-approval-candidates')
    assert candidates.status_code == 200
    candidate_body = candidates.json()
    assert candidate_body['summary']['eligible_count'] == 0
    assert candidate_body['summary']['registration_group_count'] == 1
    assert candidate_body['summary']['official_group_count'] == 1
    assert candidate_body['summary']['verifier_ready_count'] == 0
    assert candidate_body['rows'][0]['candidate_status'] == 'not_ready'
    assert candidate_body['rows'][0]['verification_scope']['real_membership_check_ready'] is False
    assert candidate_body['rows'][0]['verification_scope']['requires_manual_seed'] is True


def test_whatsapp_approval_account_session_start_returns_qr_for_selected_account():
    client = make_client({
        'LARK_APP_ID': 'cli_test_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-qr', json={
        'account_name': 'WA Admin QR',
        'responsible_type': 'registration_group',
        'group_link_bindings': [
            {
                'link': 'https://chat.whatsapp.com/group-qr',
                'area': 'Indonesia',
                'notify_profile_name': 'wa-approval-broadcast',
                'approval_count_threshold': 30,
                'approval_timeout_minutes': 30,
                'auto_recover_worker': True,
                'schedule_windows': [{'start': '00:00', 'end': '23:59'}],
            },
        ],
        'enabled': True,
        'notes': 'qr binding account',
    })
    assert saved.status_code == 200

    expected_auth_path_suffix = '/webjs-approval-worker/.wwebjs_auth_accounts/wa-admin-qr'
    runtime_base_url = 'http://127.0.0.1:18787'

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    health_payload = {
        'status': 'awaiting_qr',
        'ready': False,
        'authenticated': False,
        'auth_strategy': 'LocalAuth',
        'client_id': 'wa-approval-wa-admin-qr',
        'auth_path': f'/Users/chauncey/work/mcn-ai-automation{expected_auth_path_suffix}',
        'last_qr': None,
        'approval_client': {
            'status': 'awaiting_qr',
            'ready': False,
            'authenticated': False,
            'auth_strategy': 'LocalAuth',
            'client_id': 'wa-approval-wa-admin-qr-approval',
            'auth_path': f'/Users/chauncey/work/mcn-ai-automation{expected_auth_path_suffix}',
            'last_qr': 'qr-token-123',
            'last_qr_at': '2026-04-28T09:30:00Z',
        },
    }
    health_urls = []
    warmup_urls = []
    popen_commands = []

    def fake_get(url, timeout=10.0):
        health_urls.append(url)
        assert url == f'{runtime_base_url}/health'
        return FakeResponse(health_payload)

    def fake_post(url, json=None, timeout=15.0):
        warmup_urls.append(url)
        assert url == f'{runtime_base_url}/warmup'
        return FakeResponse(health_payload)

    def fake_popen(command, *args, **kwargs):
        popen_commands.append((command, kwargs.get('env', {})))
        class Proc:
            pid = 43210
        return Proc()

    def fake_run(command, *args, **kwargs):
        cmd0 = command[0] if isinstance(command, (list, tuple)) and command else ''
        if cmd0 == 'node':
            class Result:
                returncode = 0
                stdout = 'ASCII-QR-LINE-1\nASCII-QR-LINE-2\n'
                stderr = ''
            return Result()
        raise AssertionError(f'unexpected subprocess command: {command}')

    with patch('app.main.requests.get', side_effect=fake_get), patch('app.main.requests.post', side_effect=fake_post), patch('app.main.subprocess.Popen', side_effect=fake_popen), patch('app.main.subprocess.run', side_effect=fake_run), patch('app.main.Service._pick_whatsapp_approval_runtime_port', return_value=18787):
        started = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-qr/session/start')
        assert started.status_code == 200
        started_body = started.json()
        assert started_body['started'] is True
        assert started_body['runtime']['base_url'] == runtime_base_url
        assert started_body['runtime']['active'] is True
        assert started_body['session']['account_key'] == 'wa-admin-qr'
        assert started_body['session']['session_target_match'] is True
        assert started_body['session']['qr_available'] is True
        assert 'ASCII-QR-LINE-1' in started_body['session']['qr_ascii']
        assert started_body['session']['auth_strategy'] == 'LocalAuth'
        assert started_body['session']['auth_path'].endswith(expected_auth_path_suffix)
        assert started_body['session']['login_verified'] is False
        assert started_body['session']['login_check_status'] == 'waiting_for_scan'
        assert '等待扫码完成登录' in started_body['session']['login_check_message']
        assert popen_commands[0][0] == ['npm', 'start']
        assert popen_commands[0][1]['REGISTRATION_GROUP_APPROVAL_WEBJS_PORT'] == '18787'
        assert popen_commands[0][1]['REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_DATA_PATH'].endswith(expected_auth_path_suffix)

        status = client.get('/api/ops/whatsapp-approval-accounts/wa-admin-qr/session')
        assert status.status_code == 200
        status_body = status.json()
        assert status_body['runtime']['base_url'] == runtime_base_url
        assert status_body['runtime']['active'] is True
        assert status_body['session']['account_key'] == 'wa-admin-qr'
        assert status_body['session']['session_target_match'] is True
        assert status_body['session']['qr_available'] is True

        runtime_status = client.get('/api/ops/whatsapp-approval-accounts/wa-admin-qr/runtime')
        assert runtime_status.status_code == 200
        runtime_body = runtime_status.json()
        assert runtime_body['runtime']['base_url'] == runtime_base_url
        assert runtime_body['runtime']['active'] is True

    assert health_urls
    assert warmup_urls == [f'{runtime_base_url}/warmup']


def test_whatsapp_approval_account_session_start_falls_back_to_health_when_warmup_times_out():
    client = make_client({
        'LARK_APP_ID': 'cli_test_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-fallback', json={
        'account_name': 'WA Admin Fallback',
        'responsible_type': 'registration_group',
        'group_link_bindings': [
            {
                'link': 'https://chat.whatsapp.com/group-fallback',
                'area': 'Indonesia',
                'notify_profile_name': 'wa-approval-broadcast',
                'approval_count_threshold': 30,
                'approval_timeout_minutes': 30,
                'auto_recover_worker': True,
                'schedule_windows': [{'start': '00:00', 'end': '23:59'}],
            },
        ],
        'enabled': True,
        'notes': 'session warmup fallback account',
    })
    assert saved.status_code == 200

    runtime_base_url = 'http://127.0.0.1:18789'

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    expected_auth_path = '/Users/chauncey/work/mcn-ai-automation/webjs-approval-worker/.wwebjs_auth_accounts/wa-admin-fallback'
    health_payload = {
        'status': 'awaiting_qr',
        'ready': False,
        'authenticated': False,
        'auth_strategy': 'LocalAuth',
        'client_id': 'wa-approval-wa-admin-fallback',
        'auth_path': expected_auth_path,
        'approval_client': {
            'status': 'awaiting_qr',
            'ready': False,
            'authenticated': False,
            'auth_strategy': 'LocalAuth',
            'client_id': 'wa-approval-wa-admin-fallback-approval',
            'auth_path': expected_auth_path,
            'last_qr': 'fallback-qr-token',
            'last_qr_at': '2026-04-28T10:10:17Z',
        },
    }

    def fake_post(url, json=None, timeout=15.0):
        raise requests.exceptions.ReadTimeout('warmup timeout')

    def fake_get(url, timeout=10.0):
        assert url == f'{runtime_base_url}/health'
        return FakeResponse(health_payload)

    def fake_runtime_state(self, account_key, *, worker_health=None, allow_shared_fallback=True):
        return {
            'account_key': account_key,
            'active': True,
            'base_url': runtime_base_url,
            'source': 'dedicated',
            'status': 'awaiting_qr',
            'ready': False,
            'authenticated': False,
            'session_target_match': True,
            'status_text': '独立 Runtime 运行中',
        }

    with patch('app.main.requests.post', side_effect=fake_post), patch('app.main.requests.get', side_effect=fake_get), patch('app.main.Service.start_whatsapp_approval_account_runtime', return_value={'runtime': {'base_url': runtime_base_url}, 'account': {}, 'started': True}), patch('app.main.Service._build_whatsapp_approval_runtime_state', new=fake_runtime_state):
        started = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-fallback/session/start')

    assert started.status_code == 200
    started_body = started.json()
    assert started_body['started'] is True
    assert started_body['runtime']['base_url'] == runtime_base_url
    assert started_body['session']['status'] == 'awaiting_qr'
    assert started_body['session']['qr_available'] is True
    assert started_body['session']['session_target_match'] is True
    assert started_body['session']['login_verified'] is False
    assert started_body['session']['login_check_status'] == 'waiting_for_scan'


def test_whatsapp_approval_account_session_reports_login_ready_after_scan():
    client = make_client({
        'LARK_APP_ID': 'cli_test_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-ready', json={
        'account_name': 'WA Admin Ready',
        'responsible_type': 'registration_group',
        'group_link_bindings': [
            {
                'link': 'https://chat.whatsapp.com/group-ready',
                'area': 'Indonesia',
                'notify_profile_name': 'wa-approval-broadcast',
                'approval_count_threshold': 30,
                'approval_timeout_minutes': 30,
                'auto_recover_worker': True,
                'schedule_windows': [{'start': '00:00', 'end': '23:59'}],
            },
        ],
        'enabled': True,
        'notes': 'session ready account',
    })
    assert saved.status_code == 200

    runtime_base_url = 'http://127.0.0.1:18791'
    expected_auth_path = '/Users/chauncey/work/mcn-ai-automation/webjs-approval-worker/.wwebjs_auth_accounts/wa-admin-ready'

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    health_payload = {
        'status': 'ready',
        'ready': True,
        'authenticated': True,
        'auth_strategy': 'LocalAuth',
        'client_id': 'wa-approval-wa-admin-ready',
        'auth_path': expected_auth_path,
        'approval_client': {
            'status': 'ready',
            'ready': True,
            'authenticated': True,
            'auth_strategy': 'LocalAuth',
            'client_id': 'wa-approval-wa-admin-ready-approval',
            'auth_path': expected_auth_path,
        },
    }

    def fake_runtime_state(self, account_key, *, worker_health=None, allow_shared_fallback=True):
        return {
            'account_key': account_key,
            'active': True,
            'base_url': runtime_base_url,
            'source': 'dedicated',
            'status': 'ready',
            'ready': True,
            'authenticated': True,
            'session_target_match': True,
            'status_text': '独立 Runtime 运行中',
        }

    with patch('app.main.requests.get', return_value=FakeResponse(health_payload)), patch('app.main.Service._build_whatsapp_approval_runtime_state', new=fake_runtime_state):
        status = client.get('/api/ops/whatsapp-approval-accounts/wa-admin-ready/session')

    assert status.status_code == 200
    body = status.json()
    assert body['session']['authenticated'] is True
    assert body['session']['session_target_match'] is True
    assert body['session']['login_verified'] is True
    assert body['session']['login_check_status'] == 'passed'
    assert body['session']['login_check_message'] == '账号已登录，可以正常使用。'


def test_whatsapp_approval_account_session_accepts_shared_primary_client_login():
    client = make_client({
        'LARK_APP_ID': 'cli_test_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-shared', json={
        'account_name': 'WA Admin Shared',
        'responsible_type': 'registration_group',
        'group_link_bindings': [
            {
                'link': 'https://chat.whatsapp.com/group-shared',
                'area': 'Indonesia',
                'notify_profile_name': 'wa-approval-broadcast',
                'approval_count_threshold': 30,
                'approval_timeout_minutes': 30,
                'auto_recover_worker': True,
                'schedule_windows': [{'start': '00:00', 'end': '23:59'}],
            },
        ],
        'enabled': True,
        'notes': 'shared primary login account',
    })
    assert saved.status_code == 200

    runtime_base_url = 'http://127.0.0.1:18795'
    expected_auth_path = '/Users/chauncey/work/mcn-ai-automation/webjs-approval-worker/.wwebjs_auth_accounts/wa-admin-shared'

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    health_payload = {
        'status': 'warm',
        'ready': True,
        'authenticated': True,
        'auth_strategy': 'LocalAuth',
        'client_id': 'wa-approval-wa-admin-shared',
        'auth_path': expected_auth_path,
        'approval_client': {
            'status': 'warm',
            'ready': True,
            'authenticated': True,
            'auth_strategy': 'LocalAuth',
            'client_id': 'wa-approval-wa-admin-shared',
            'auth_path': expected_auth_path,
        },
    }

    def fake_runtime_state(self, account_key, *, worker_health=None, allow_shared_fallback=True):
        return {
            'account_key': account_key,
            'active': True,
            'base_url': runtime_base_url,
            'source': 'dedicated',
            'status': 'warm',
            'ready': True,
            'authenticated': True,
            'session_target_match': True,
            'status_text': '独立 Runtime 运行中',
        }

    with patch('app.main.requests.get', return_value=FakeResponse(health_payload)), patch('app.main.Service._build_whatsapp_approval_runtime_state', new=fake_runtime_state):
        status = client.get('/api/ops/whatsapp-approval-accounts/wa-admin-shared/session')

    assert status.status_code == 200
    body = status.json()
    assert body['session']['authenticated'] is True
    assert body['session']['client_id'] == 'wa-approval-wa-admin-shared'
    assert body['session']['expected_client_id'] == 'wa-approval-wa-admin-shared'
    assert body['session']['expected_approval_client_id'] == 'wa-approval-wa-admin-shared-approval'
    assert body['session']['session_target_match'] is True
    assert body['session']['login_verified'] is True
    assert body['session']['login_check_status'] == 'passed'


def test_whatsapp_approval_account_runtime_can_stop_dedicated_worker():
    client = make_client({
        'LARK_APP_ID': 'cli_test_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-stop', json={
        'account_name': 'WA Admin Stop',
        'responsible_type': 'registration_group',
        'group_link_bindings': [
            {
                'link': 'https://chat.whatsapp.com/group-stop',
                'area': 'Indonesia',
                'notify_profile_name': 'wa-approval-broadcast',
                'approval_count_threshold': 30,
                'approval_timeout_minutes': 30,
                'auto_recover_worker': True,
                'schedule_windows': [{'start': '00:00', 'end': '23:59'}],
            },
        ],
        'enabled': True,
        'notes': 'runtime stop account',
    })
    assert saved.status_code == 200

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    health_payload = {
        'status': 'warm',
        'ready': True,
        'authenticated': True,
        'auth_strategy': 'LocalAuth',
        'client_id': 'wa-approval-wa-admin-stop',
        'auth_path': '/tmp/auth',
        'approval_client': {
            'status': 'warm',
            'ready': True,
            'authenticated': True,
            'auth_strategy': 'LocalAuth',
            'client_id': 'wa-approval-wa-admin-stop-approval',
            'auth_path': '/tmp/auth',
        },
    }

    killed = []

    def fake_get(url, timeout=10.0):
        return FakeResponse(health_payload)

    def fake_post(url, json=None, timeout=15.0):
        return FakeResponse(health_payload)

    def fake_popen(command, *args, **kwargs):
        class Proc:
            pid = 54321
        return Proc()

    def fake_run(command, *args, **kwargs):
        cmd0 = command[0] if isinstance(command, (list, tuple)) and command else ''
        if cmd0 == 'node':
            class Result:
                returncode = 0
                stdout = 'ASCII-QR'
                stderr = ''
            return Result()
        if cmd0 == 'ps':
            class Result:
                returncode = 0
                stdout = ''
                stderr = ''
            return Result()
        raise AssertionError(f'unexpected subprocess command: {command}')

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    with patch('app.main.requests.get', side_effect=fake_get), patch('app.main.requests.post', side_effect=fake_post), patch('app.main.subprocess.Popen', side_effect=fake_popen), patch('app.main.subprocess.run', side_effect=fake_run), patch('app.main.os.kill', side_effect=fake_kill), patch('app.main.Service._pick_whatsapp_approval_runtime_port', return_value=18788):
        started = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-stop/runtime/start')
        assert started.status_code == 200
        stopped = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-stop/runtime/stop')
        assert stopped.status_code == 200
        stopped_body = stopped.json()
        assert stopped_body['stopped'] is True
        assert stopped_body['runtime']['active'] is False
        assert killed[0][0] == 54321


def test_whatsapp_approval_account_runtime_stop_kills_orphan_browser_for_auth_path():
    client = make_client({
        'LARK_APP_ID': 'cli_test_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-orphan', json={
        'account_name': 'WA Admin Orphan',
        'responsible_type': 'registration_group',
        'group_link_bindings': [
            {
                'link': 'https://chat.whatsapp.com/group-orphan',
                'area': 'Indonesia',
                'notify_profile_name': 'wa-approval-broadcast',
                'approval_count_threshold': 30,
                'approval_timeout_minutes': 30,
                'auto_recover_worker': True,
                'schedule_windows': [{'start': '00:00', 'end': '23:59'}],
            },
        ],
        'enabled': True,
        'notes': 'runtime orphan account',
    })
    assert saved.status_code == 200

    auth_path = '/Users/chauncey/work/mcn-ai-automation/webjs-approval-worker/.wwebjs_auth_accounts/wa-admin-orphan'
    meta_path = Path('/Users/chauncey/work/mcn-ai-automation/data/whatsapp_approval_worker_runtimes/wa-admin-orphan.json')
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({
        'account_key': 'wa-admin-orphan',
        'pid': 61001,
        'port': 18891,
        'base_url': 'http://127.0.0.1:18891',
        'auth_path': auth_path,
        'client_id': 'wa-approval-wa-admin-orphan-approval',
        'log_path': '/tmp/wa-admin-orphan.log',
        'started_at': '2026-04-28T10:00:00Z',
    }), encoding='utf-8')

    killed = []

    def fake_run(command, *args, **kwargs):
        cmd0 = command[0] if isinstance(command, (list, tuple)) and command else ''
        if cmd0 == 'ps':
            class Result:
                returncode = 0
                stdout = f'61002 /tmp/chrome --user-data-dir={auth_path}/session-wa-approval-wa-admin-orphan-approval\n'
                stderr = ''
            return Result()
        raise AssertionError(f'unexpected subprocess command: {command}')

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    with patch('app.main.subprocess.run', side_effect=fake_run), patch('app.main.os.kill', side_effect=fake_kill):
        stopped = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-orphan/runtime/stop')

    assert stopped.status_code == 200
    stopped_body = stopped.json()
    assert stopped_body['stopped'] is True
    assert stopped_body['runtime']['active'] is False
    assert (61001, 15) in killed
    assert (61002, 15) in killed


def test_whatsapp_approval_account_delete_removes_account_without_runtime_meta():
    client = make_client({
        'LARK_APP_ID': 'cli_test_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-delete-1', json={
        'account_name': 'WA Delete 1',
        'responsible_type': 'registration_group',
        'group_link_bindings': [{
            'link': 'https://chat.whatsapp.com/group-delete-1',
            'area': 'Indonesia',
            'notify_profile_name': 'wa-approval-broadcast',
            'approval_count_threshold': 30,
            'approval_timeout_minutes': 30,
            'auto_recover_worker': True,
            'schedule_windows': [],
        }],
        'notify_profile_name': 'wa-approval-broadcast',
        'approval_count_threshold': 30,
        'approval_timeout_minutes': 30,
        'auto_recover_worker': True,
        'schedule_windows': [],
        'enabled': True,
        'notes': 'delete me',
    })
    assert saved.status_code == 200

    deleted = client.delete('/api/ops/whatsapp-approval-accounts/wa-delete-1')
    assert deleted.status_code == 200
    assert deleted.json()['deleted'] is True

    listed = client.get('/api/ops/whatsapp-approval-accounts')
    assert listed.status_code == 200
    assert all(row['account_key'] != 'wa-delete-1' for row in listed.json()['rows'])


def test_whatsapp_approval_account_requires_area_for_each_group_link_binding():
    client = make_client({
        'LARK_APP_ID': 'cli_test_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })

    invalid = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-invalid', json={
        'account_name': 'WA Invalid',
        'responsible_type': 'registration_group',
        'group_link_bindings': [
            {'link': 'https://chat.whatsapp.com/group-a', 'area': '', 'notify_profile_name': 'wa-approval-broadcast'},
        ],
        'notify_profile_name': 'wa-approval-broadcast',
        'approval_count_threshold': 25,
        'approval_timeout_minutes': 28,
        'auto_recover_worker': True,
        'schedule_windows': [
            {'start': '00:00', 'end': '23:59'},
        ],
        'enabled': True,
        'notes': 'invalid binding',
    })
    assert invalid.status_code == 400
    assert invalid.json()['detail'] == 'group link #1 must select an area'


def test_whatsapp_approval_candidates_marks_registration_group_probe_ready_when_live_group_state_exists():
    executor = StubRegistrationGroupApprovalExecutor(group_state_result={
        'group_name': '🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎',
        'group_id': '120363425215002840@g.us',
        'pending_count': 2,
        'member_count': 419,
        'requester_ids': ['req-1@lid', 'req-2@lid'],
        'requesters': [
            {'requesterId': 'req-1@lid', 'requestedAtUnix': 100},
            {'requesterId': 'req-2@lid', 'requestedAtUnix': 101},
        ],
    })
    client = make_client({
        'LARK_APP_ID': 'cli_test_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-admin-live', json={
        'account_name': 'WA Admin Live',
        'responsible_type': 'registration_group',
        'group_link_bindings': [
            {
                'link': 'https://chat.whatsapp.com/group-live-a',
                'area': 'Indonesia',
                'notify_profile_name': 'wa-approval-broadcast',
                'registration_group': '🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎',
                'group_id': '120363425215002840@g.us',
                'approval_count_threshold': 30,
                'approval_timeout_minutes': 30,
                'auto_recover_worker': True,
                'schedule_windows': [{'start': '00:00', 'end': '23:59'}],
            },
        ],
        'enabled': True,
        'notes': 'live verifier account',
    })
    assert saved.status_code == 200

    client.app.state.service.get_production_ops_daemon_config = lambda: {
        'config': {'enabled': True},
        'runtime': {
            'launch_agent_installed': True,
            'status': {
                'worker_state': {
                    'ok': True,
                    'payload': {
                        'group_name': '🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎',
                        'group_id': '120363425215002840@g.us',
                        'pending_count': 2,
                        'member_count': 419,
                        'requester_ids': ['req-1@lid', 'req-2@lid'],
                        'requesters': [
                            {'requesterId': 'req-1@lid', 'requestedAtUnix': 100},
                            {'requesterId': 'req-2@lid', 'requestedAtUnix': 101},
                        ],
                    },
                },
            },
        },
    }

    listed = client.get('/api/ops/whatsapp-approval-accounts')
    assert listed.status_code == 200
    row = listed.json()['rows'][0]
    admin_check = next(item for item in row['verification_checks'] if item['code'] == 'admin_membership_verification')
    assert admin_check['ok'] is True
    assert '已接入真实注册群状态探针' in admin_check['detail']
    assert row['membership_verifier']['ready'] is True
    assert row['membership_verifier']['requires_manual_seed'] is False
    assert row['group_binding_runtimes'][0]['membership_verifier']['ready'] is True
    assert row['group_binding_runtimes'][0]['membership_verifier']['status'] == 'mapped_live_probe_ready'

    candidates = client.get('/api/ops/whatsapp-approval-candidates')
    assert candidates.status_code == 200
    candidate_body = candidates.json()
    assert candidate_body['summary']['verifier_ready_count'] == 1
    assert candidate_body['rows'][0]['verification_scope']['real_membership_check_ready'] is True
    assert candidate_body['rows'][0]['verification_scope']['requires_manual_seed'] is False
    assert candidate_body['verifier_framework']['status'] == 'live_probe_ready'
    assert candidate_body['verifier_framework']['real_membership_check_ready'] is True



def test_guild_executor_api_returns_proxy_region_dropdown_options_and_validates_values():
    client = make_client({'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})

    listed = client.get('/api/ops/guild-executors')
    assert listed.status_code == 200
    body = listed.json()
    assert body['rows'] == []
    assert len(body['proxy_region_options']) == 15
    assert body['proxy_region_options'][0] == {'value': '北京', 'label': '北京'}
    assert {'value': '厦门', 'label': '厦门'} in body['proxy_region_options']
    assert {'value': '福州', 'label': '福州'} in body['proxy_region_options']

    invalid = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_region': 'Xiamen',
    })
    assert invalid.status_code == 400
    assert invalid.json()['detail'] == 'proxy_region must be one of the configured city options'



def test_guild_executor_api_enforces_unique_proxy_region_per_guild():
    client = make_client({'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})

    first = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_region': '厦门',
    })
    assert first.status_code == 200

    duplicate = client.post('/api/ops/guild-executors/Piso', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'piso@example.com',
        'password_secret_ref': 'secret_piso',
        'proxy_region': '厦门',
    })
    assert duplicate.status_code == 400
    assert duplicate.json()['detail'] == 'proxy_region is already assigned to guild Permata'



def test_guild_executor_api_deletes_executor_and_releases_proxy_region():
    client = make_client({'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})

    created = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_region': '厦门',
    })
    assert created.status_code == 200

    deleted = client.delete('/api/ops/guild-executors/Permata')
    assert deleted.status_code == 200
    assert deleted.json() == {'deleted': True, 'guild_name': 'Permata'}

    missing = client.get('/api/ops/guild-executors/Permata')
    assert missing.status_code == 404

    reused = client.post('/api/ops/guild-executors/Piso', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'piso@example.com',
        'password_secret_ref': 'secret_piso',
        'proxy_region': '厦门',
    })
    assert reused.status_code == 200
    assert reused.json()['proxy_region'] == '厦门'



def test_guild_executors_api_lists_and_updates_executor_config():
    client = make_client({'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})

    initial = client.get('/api/ops/guild-executors')
    assert initial.status_code == 200
    body = initial.json()
    assert body['rows'] == []

    saved = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_url': 'http://proxy-xm:8080',
        'proxy_region': '厦门',
        'proxy_type': 'http',
        'enabled': True,
        'browser_profile_key': 'permata-profile',
        'bind_concurrency': 3,
        'request_timeout_seconds': 45,
        'notes': 'permata executor',
    })
    assert saved.status_code == 200
    saved_body = saved.json()
    assert saved_body['saved'] is True
    assert saved_body['guild_name'] == 'Permata'
    assert saved_body['proxy_region'] == '厦门'
    assert saved_body['password_configured'] is True
    assert 'password_secret_ref' not in saved_body

    saved_minimal = client.post('/api/ops/guild-executors/Piso', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'piso@example.com',
        'password_secret_ref': 'secret_piso',
    })
    assert saved_minimal.status_code == 200
    saved_minimal_body = saved_minimal.json()
    assert saved_minimal_body['guild_name'] == 'Piso'
    assert saved_minimal_body['proxy_url'] == ''
    assert saved_minimal_body['proxy_region'] == ''
    assert saved_minimal_body['proxy_type'] == 'http'
    assert saved_minimal_body['browser_profile_key'] == 'guild-piso'
    assert saved_minimal_body['bind_concurrency'] == 1
    assert saved_minimal_body['request_timeout_seconds'] == 30
    assert saved_minimal_body['enabled'] is True

    resolved = client.get('/api/ops/guild-executors/Permata')
    assert resolved.status_code == 200
    resolved_body = resolved.json()
    assert resolved_body['guild_name'] == 'Permata'
    assert resolved_body['backend_url'] == 'https://guild.linke.ai/guild/addAnchor'
    assert resolved_body['login_username'] == 'permata@example.com'
    assert resolved_body['proxy_url'] == 'http://proxy-xm:8080'
    assert resolved_body['proxy_region'] == '厦门'
    assert resolved_body['password_configured'] is True
    assert 'password_secret_ref' not in resolved_body

    refreshed = client.get('/api/ops/guild-executors')
    assert refreshed.status_code == 200
    rows = refreshed.json()['rows']
    assert len(rows) == 2
    assert rows[0]['guild_name'] == 'Permata'
    assert rows[0]['backend_url'] == 'https://guild.linke.ai/guild/addAnchor'
    assert rows[0]['login_username'] == 'permata@example.com'
    assert rows[0]['proxy_url'] == 'http://proxy-xm:8080'
    assert rows[0]['proxy_region'] == '厦门'
    assert rows[0]['proxy_type'] == 'http'
    assert rows[0]['browser_profile_key'] == 'permata-profile'
    assert rows[0]['bind_concurrency'] == 3
    assert rows[0]['request_timeout_seconds'] == 45
    assert rows[0]['password_configured'] is True



def test_guild_executor_health_api_returns_latest_bind_status_and_human_action_flag():
    client = make_client({'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})

    created = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_region': '厦门',
        'proxy_type': 'http',
        'enabled': True,
        'browser_profile_key': 'permata-profile',
        'bind_concurrency': 2,
    })
    assert created.status_code == 200

    lead = client.post('/api/leads/upsert', json={
        'trace_id': 'trace-health-1',
        'source_platform': 'meta',
        'source_page_id': 'page-health-1',
        'country': 'Indonesia',
        'area_code': 62,
        'mobile': '81230001111',
        'pendaftaran_group': 'PERMATA-909',
        'app_name': 'Linky',
        'dept_name': 'Permata',
        'inviter_id': 'ABCDEF',
    }).json()
    submission = client.post('/api/account-submissions', json={
        'lead_id': lead['lead_id'],
        'submission_type': 'account_id',
        'account_id': '55667788',
        'account_id_type': 'platform_uid',
        'source_channel': 'manual_cs_lark',
        'submitted_by': 'customer_service',
        'submitted_at': '2026-04-21T12:10:00Z',
    }).json()
    with client.app.state.service.db.connect() as conn:
        conn.execute(
            "UPDATE automation_tasks SET status='failed', result_code=?, result_reason=?, raw_result=?, started_at=?, finished_at=? WHERE task_id=?",
            ('bind_unauthorized', 'HTTP 401: please re-login', json.dumps({'guild_code': 'Permata', 'auth_required': True}), '2026-04-21T12:10:05Z', '2026-04-21T12:10:10Z', submission['task_id'])
        )
        conn.commit()

    health = client.get('/api/ops/guild-executors/health')
    assert health.status_code == 200
    row = health.json()['rows'][0]
    assert row['guild_name'] == 'Permata'
    assert row['bind_concurrency'] == 2
    assert row['proxy_region'] == '厦门'
    assert row['last_bind_task_id'] == submission['task_id']
    assert row['last_bind_status'] == 'failed'
    assert row['last_bind_result_code'] == 'bind_unauthorized'
    assert row['requires_human_action'] is True
    assert row['human_action_type'] == 'auth_required'


def test_guild_executor_health_hides_stale_human_action_when_latest_bind_no_longer_requires_it():
    client = make_client({'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})
    created = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_region': '厦门',
        'proxy_type': 'http',
        'enabled': True,
        'browser_profile_key': 'permata-profile',
    })
    assert created.status_code == 200

    submission = client.post('/api/intake/manual-cs-submissions', json={
        'mobile': '+62 81234567123',
        'registration_group': 'Permata-90',
        'app_name': 'Linky',
        'dept_name': 'Permata',
        'submission_type': 'account_id',
        'account_id': '55667788',
        'invite_code': 'EKVFGQ',
        'submitted_by': 'tester',
        'source_channel': 'manual_cs_lark',
        'submitted_at': '2026-04-21T12:10:00Z',
    }).json()

    with client.app.state.service.db.connect() as conn:
        conn.execute(
            "UPDATE automation_tasks SET status='failed', result_code=?, result_reason=?, raw_result=?, started_at=?, finished_at=? WHERE task_id=?",
            ('bind_unauthorized', 'HTTP 401: please re-login', json.dumps({'guild_code': 'Permata', 'auth_required': True}), '2026-04-21T12:10:05Z', '2026-04-21T12:10:10Z', submission['task_id'])
        )
        newer_task_id = 'task_newer_perm'
        conn.execute(
            "INSERT INTO automation_tasks (task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status, result_code, result_reason, raw_result, started_at, finished_at) VALUES (?, ?, 'bind_check', 'normal', '{}', ?, 'tester', ?, 'failed', ?, ?, ?, ?, ?)",
            (newer_task_id, submission['lead_id'], f'dedupe:{newer_task_id}', '2026-04-21T12:12:00Z', 'bind_backend_http_error', 'HTTP 400: device limit', json.dumps({'guild_code': 'Permata'}), '2026-04-21T12:12:02Z', '2026-04-21T12:12:08Z')
        )
        conn.commit()

    health = client.get('/api/ops/guild-executors/health')
    assert health.status_code == 200
    row = health.json()['rows'][0]
    assert row['last_bind_task_id'] == 'task_newer_perm'
    assert row['last_bind_result_code'] == 'bind_backend_http_error'
    assert row['requires_human_action'] is False
    assert row['human_action_type'] is None



def test_intake_bot_presets_api_reports_unavailable_options_when_crm_is_not_ready():
    client = make_client({'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})

    initial = client.get('/api/ops/intake-bot-presets')
    assert initial.status_code == 200
    body = initial.json()
    rows = body['rows']
    assert len(rows) == 1
    assert rows[0]['app_id'] == 'cli_test_app'
    assert rows[0]['default_app'] == 'Linky'
    assert rows[0]['default_guild'] == 'Piso'
    assert body['app_options'] == []
    assert body['guild_options'] == []
    assert body['app_options_source'] == 'unavailable'
    assert body['guild_options_source'] == 'unavailable'



def test_intake_bot_presets_api_reads_live_dropdown_options_and_updates_defaults():
    client = make_client({'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})
    client.app.state.service.crm_adapter = StubCrmDropdownAdapter(
        apps=[{'id': 'app_1', 'name': 'Linky'}, {'id': 'app_2', 'name': 'FUMI'}],
        depts=[{'id': 'dept_1', 'deptName': 'Piso'}, {'id': 'dept_2', 'deptName': 'Permata'}],
    )

    initial = client.get('/api/ops/intake-bot-presets')
    assert initial.status_code == 200
    body = initial.json()
    assert body['app_options_source'] == 'live'
    assert body['guild_options_source'] == 'live'
    assert {'label': 'Linky', 'value': 'Linky'} in body['app_options']
    assert {'label': 'FUMI', 'value': 'FUMI'} in body['app_options']
    assert {'label': 'Piso', 'value': 'Piso'} in body['guild_options']
    assert {'label': 'Permata', 'value': 'Permata'} in body['guild_options']

    saved = client.post('/api/ops/intake-bot-presets/current', json={
        'robot_name': '旧机器人A',
        'default_app': 'FUMI',
        'default_guild': 'Permata',
    })
    assert saved.status_code == 200
    body = saved.json()
    assert body['saved'] is True
    assert body['robot_name'] == '旧机器人A'
    assert body['default_app'] == 'FUMI'
    assert body['default_guild'] == 'Permata'

    refreshed = client.get('/api/ops/intake-bot-presets/resolve?app_id=cli_test_app')
    assert refreshed.status_code == 200
    row = refreshed.json()
    assert row['robot_name'] == '旧机器人A'
    assert row['default_app'] == 'FUMI'
    assert row['default_guild'] == 'Permata'



def test_intake_bot_preset_update_rejects_values_outside_known_dropdown_options():
    client = make_client({'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})
    client.app.state.service.crm_adapter = StubCrmDropdownAdapter(
        apps=[{'id': 'app_1', 'name': 'Linky'}],
        depts=[{'id': 'dept_1', 'deptName': 'Piso'}],
    )

    saved = client.post('/api/ops/intake-bot-presets/current', json={
        'default_app': 'BADAPP',
        'default_guild': 'BADGUILD',
    })

    assert saved.status_code == 400
    assert 'default_app must be selected from CRM dropdown options' in saved.text



def test_intake_bot_presets_api_can_create_additional_bot_preset_and_resolve_by_app_id():
    client = make_client({'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})
    client.app.state.service.crm_adapter = StubCrmDropdownAdapter(
        apps=[{'id': 'app_1', 'name': 'Linky'}, {'id': 'app_2', 'name': 'FUMI'}],
        depts=[{'id': 'dept_1', 'deptName': 'Piso'}, {'id': 'dept_2', 'deptName': 'Permata'}],
    )

    created = client.post('/api/ops/intake-bot-presets/intake-a96f1cec', json={
        'robot_name': 'Permata Intake Bot',
        'app_id': 'cli_a96f1cec1a789e15',
        'default_app': 'FUMI',
        'default_guild': 'Permata',
    })
    assert created.status_code == 200
    body = created.json()
    assert body['saved'] is True
    assert body['profile_name'] == 'intake-a96f1cec'
    assert body['robot_name'] == 'Permata Intake Bot'
    assert body['app_id'] == 'cli_a96f1cec1a789e15'
    assert body['default_app'] == 'FUMI'
    assert body['default_guild'] == 'Permata'

    listing = client.get('/api/ops/intake-bot-presets')
    assert listing.status_code == 200
    rows = listing.json()['rows']
    assert len(rows) == 1
    assert rows[0]['profile_name'] == 'intake-a96f1cec'
    assert rows[0]['robot_name'] == 'Permata Intake Bot'
    assert rows[0]['app_id'] == 'cli_a96f1cec1a789e15'

    resolved = client.get('/api/ops/intake-bot-presets/resolve?app_id=cli_a96f1cec1a789e15')
    assert resolved.status_code == 200
    resolved_body = resolved.json()
    assert resolved_body['profile_name'] == 'intake-a96f1cec'
    assert resolved_body['robot_name'] == 'Permata Intake Bot'
    assert resolved_body['default_app'] == 'FUMI'
    assert resolved_body['default_guild'] == 'Permata'
    assert resolved_body['matched_by'] == 'app_id'

    fallback = client.get('/api/ops/intake-bot-presets/resolve?app_id=cli_unknown')
    assert fallback.status_code == 200
    fallback_body = fallback.json()
    assert fallback_body['profile_name'] == 'current'
    assert fallback_body['default_app'] == 'Linky'
    assert fallback_body['default_guild'] == 'Piso'
    assert fallback_body['matched_by'] == 'fallback_current'



def test_intake_bot_presets_api_uses_cached_dropdown_options_when_live_crm_is_unavailable(tmp_path):
    client = make_client({
        'DB_PATH': str(tmp_path / 'preset-cache.db'),
        'LARK_APP_ID': 'cli_test_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    service = client.app.state.service
    service.crm_adapter = StubCrmDropdownAdapter(
        apps=[{'id': 'app_1', 'name': 'Linky'}, {'id': 'app_2', 'name': 'FUMI'}],
        depts=[{'id': 'dept_1', 'deptName': 'Piso'}, {'id': 'dept_2', 'deptName': 'Permata'}],
    )

    seeded = client.get('/api/ops/intake-bot-presets')
    assert seeded.status_code == 200
    assert seeded.json()['app_options_source'] == 'live'

    service.crm_adapter = None

    cached = client.get('/api/ops/intake-bot-presets')
    assert cached.status_code == 200
    body = cached.json()
    assert body['app_options_source'] == 'cache'
    assert body['guild_options_source'] == 'cache'
    assert {'label': 'FUMI', 'value': 'FUMI'} in body['app_options']
    assert {'label': 'Permata', 'value': 'Permata'} in body['guild_options']

    saved = client.post('/api/ops/intake-bot-presets/current', json={
        'default_app': 'FUMI',
        'default_guild': 'Permata',
    })
    assert saved.status_code == 200


def test_persisted_current_preset_overrides_env_defaults_after_restart(tmp_path):
    db_path = str(tmp_path / 'persisted-presets.db')
    first = make_client({'DB_PATH': db_path, 'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})
    first.app.state.service.crm_adapter = StubCrmDropdownAdapter(
        apps=[{'id': 'app_1', 'name': 'Linky'}, {'id': 'app_2', 'name': 'FUMI'}],
        depts=[{'id': 'dept_1', 'deptName': 'Piso'}, {'id': 'dept_2', 'deptName': 'Permata'}],
    )
    saved = first.post('/api/ops/intake-bot-presets/current', json={
        'default_app': 'FUMI',
        'default_guild': 'Permata',
    })
    assert saved.status_code == 200

    restarted = make_client({'DB_PATH': db_path, 'LARK_APP_ID': 'cli_test_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})
    runtime = restarted.get('/api/ops/runtime-health')
    assert runtime.status_code == 200
    body = runtime.json()
    assert body['lark']['default_app'] == 'FUMI'
    assert body['lark']['default_guild'] == 'Permata'

    response = restarted.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_restart_preset'}},
            'message': {
                'message_id': 'om_restart_preset',
                'message_type': 'text',
                'chat_type': 'p2p',
                    'content': '{"text":"+62 784522998\n77889900\nPermata-31\nCode EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200
    payload = response.json()['parsed_payload']
    assert payload['app_name'] == 'FUMI'
    assert payload['dept_name'] == 'Permata'



def test_lark_event_uses_bot_specific_preset_when_gateway_passes_bot_app_id():
    client = make_client({'LARK_APP_ID': 'cli_default_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})
    client.app.state.service.crm_adapter = StubCrmDropdownAdapter(
        apps=[{'id': 'app_1', 'name': 'Linky'}, {'id': 'app_2', 'name': 'FUMI'}],
        depts=[{'id': 'dept_1', 'deptName': 'Piso'}, {'id': 'dept_2', 'deptName': 'Permata'}],
    )
    created = client.post('/api/ops/intake-bot-presets/intake-a96f1cec', json={
        'app_id': 'cli_a96f1cec1a789e15',
        'default_app': 'FUMI',
        'default_guild': 'Permata',
    })
    assert created.status_code == 200

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        '_bot_app_id': 'cli_a96f1cec1a789e15',
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_multi_bot'}},
            'message': {
                'message_id': 'om_multi_bot',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234560000\nPermata-31\n77889900\nCode EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is True
    assert body['parsed_payload']['app_name'] == 'FUMI'
    assert body['parsed_payload']['dept_name'] == 'Permata'



def test_bind_check_result_rejects_backend_guild_that_does_not_match_current_bot_preset():
    client = make_client({'LARK_APP_ID': 'cli_default_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})
    client.app.state.service.crm_adapter = StubCrmDropdownAdapter(
        apps=[{'id': 'app_1', 'name': 'Linky'}, {'id': 'app_2', 'name': 'FUMI'}],
        depts=[{'id': 'dept_1', 'deptName': 'Piso'}, {'id': 'dept_2', 'deptName': 'Permata'}],
    )
    created = client.post('/api/ops/intake-bot-presets/intake-a96f1cec', json={
        'app_id': 'cli_a96f1cec1a789e15',
        'default_app': 'FUMI',
        'default_guild': 'Permata',
    })
    assert created.status_code == 200

    intake = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        '_bot_app_id': 'cli_a96f1cec1a789e15',
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_bind_mismatch'}},
            'message': {
                'message_id': 'om_bind_mismatch',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234561111\nPermata-31\n77889901\nCode EKVFGQ"}'
            }
        }
    })
    assert intake.status_code == 200
    intake_body = intake.json()
    assert intake_body['accepted'] is True
    assert intake_body['task_id']
    client.app.state.service.crm_adapter = None

    bind = client.post(
        f"/api/tasks/{intake_body['task_id']}/bind-check-result",
        json={
            'status': 'success',
            'result_code': 'bind_ok',
            'result_reason': 'guild accepted',
            'finished_at': '2026-04-20T15:00:00Z',
            'raw_result': {'guild_code': 'Piso', 'deptName': 'Piso', 'deptId': 'dept_1'},
        },
    )
    assert bind.status_code == 200
    bind_body = bind.json()
    assert bind_body['lead_status'] == 'bind_failed'
    assert bind_body['next_action'] == 'queue_reengagement'
    assert bind_body['reason'] == 'bind_backend_guild_mismatch'
    assert 'Permata' in bind_body['result_reason']
    assert 'Piso' in bind_body['result_reason']

    timeline = client.get(f"/api/leads/{intake_body['lead_id']}/timeline").json()
    assert not [task for task in timeline['tasks'] if task['task_type'] == 'group_join']



def test_bind_check_result_uses_current_bot_preset_guild_after_preset_change_before_callback():
    client = make_client({'LARK_APP_ID': 'cli_default_app', 'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})
    client.app.state.service.crm_adapter = StubCrmDropdownAdapter(
        apps=[{'id': 'app_1', 'name': 'Linky'}, {'id': 'app_2', 'name': 'FUMI'}],
        depts=[{'id': 'dept_1', 'deptName': 'Piso'}, {'id': 'dept_2', 'deptName': 'Permata'}],
    )
    created = client.post('/api/ops/intake-bot-presets/intake-a96f1cec', json={
        'app_id': 'cli_a96f1cec1a789e15',
        'default_app': 'FUMI',
        'default_guild': 'Permata',
    })
    assert created.status_code == 200

    intake = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        '_bot_app_id': 'cli_a96f1cec1a789e15',
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_bind_current_preset'}},
            'message': {
                'message_id': 'om_bind_current_preset',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234562222\nPermata-31\n77889902\nCode EKVFGQ"}'
            }
        }
    })
    assert intake.status_code == 200
    intake_body = intake.json()
    assert intake_body['parsed_payload']['dept_name'] == 'Permata'

    changed = client.post('/api/ops/intake-bot-presets/intake-a96f1cec', json={
        'app_id': 'cli_a96f1cec1a789e15',
        'default_app': 'FUMI',
        'default_guild': 'Piso',
    })
    assert changed.status_code == 200
    client.app.state.service.crm_adapter = None

    bind = client.post(
        f"/api/tasks/{intake_body['task_id']}/bind-check-result",
        json={
            'status': 'success',
            'result_code': 'bind_ok',
            'result_reason': 'guild accepted',
            'finished_at': '2026-04-20T15:10:00Z',
            'raw_result': {'guild_code': 'Piso', 'deptName': 'Piso', 'deptId': 'dept_1'},
        },
    )
    assert bind.status_code == 200
    bind_body = bind.json()
    assert bind_body['lead_status'] == 'bind_success'
    assert bind_body['next_action'] == 'queue_group_join'



def test_lark_reply_templates_include_code_field_for_irrelevant_and_missing_cases():
    client = make_client()
    service = client.app.state.service

    irrelevant = service._format_lark_reply_text({
        'accepted': False,
        'reason': 'irrelevant_message',
        'reply_phone': '-',
        'reply_id': '-',
        'reply_group': '-',
        'reply_code': '-',
    })
    assert 'Code:' in irrelevant
    assert '**📮Send:**' in irrelevant
    assert 'Phone:' in irrelevant
    assert 'ID:' in irrelevant
    assert 'Group:' in irrelevant

    missing = service._format_lark_reply_text({
        'accepted': False,
        'reason': 'missing_required_fields',
        'reply_phone': '81234567890',
        'reply_area_code': 62,
        'reply_id': '55667788',
        'reply_group': 'Piso-12',
        'reply_code': '-',
        'reply_missing_fields': ['Code'],
    })
    assert 'Phone: +62 81234567890' in missing
    assert 'Code: -' in missing
    assert '**🚫 Missing: Code**' in missing



def test_lark_reply_templates_include_code_field_for_success_and_failures():
    client = make_client()
    service = client.app.state.service

    pending = service._format_lark_reply_text({
        'accepted': True,
        'next_action': 'queue_bind_check',
        'lead_status': 'bind_check_pending',
        'reply_phone': '+62 81234567890',
        'reply_id': '55667788',
        'reply_group': 'Piso-12',
        'reply_code': 'EKVFGQ',
    })
    assert pending.startswith('**❌ Failed**')
    assert 'Code: EKVFGQ' in pending

    final_success = service._format_lark_reply_text({
        'accepted': True,
        'next_action': 'queue_group_join',
        'lead_status': 'bind_success',
        'crm_verified': True,
        'reply_phone': '81234567890',
        'reply_area_code': 62,
        'reply_id': '55667788',
        'reply_group': 'Piso-12',
        'reply_code': 'EKVFGQ',
    })
    assert final_success.startswith('**✅ Success**')
    assert 'Phone: +62 81234567890' in final_success
    assert 'Code: EKVFGQ' in final_success

    not_verified_yet = service._format_lark_reply_text({
        'accepted': True,
        'next_action': 'queue_group_join',
        'lead_status': 'bind_success',
        'crm_verified': False,
        'reply_phone': '81234567890',
        'reply_area_code': 62,
        'reply_id': '55667788',
        'reply_group': 'Piso-12',
        'reply_code': 'EKVFGQ',
    })
    assert not_verified_yet.startswith('**❌ Failed**')

    bind_failed = service._format_lark_reply_text({
        'accepted': False,
        'reason': 'simulated_bind_failed',
        'result_reason': 'manual retry needed',
        'reply_phone': '+62 81234567890',
        'reply_id': '55667788',
        'reply_group': 'Piso-12',
        'reply_code': 'EKVFGQ',
    })
    assert 'Code: EKVFGQ' in bind_failed

    device_duplicate = service._format_lark_reply_text({
        'accepted': False,
        'reason': 'bind_check_failed',
        'result_reason': 'Perangkat ini telah mencapai batas maksimum guild yang dapat diikuti.',
        'reply_phone': '+62 81234567890',
        'reply_id': '55667788',
        'reply_group': 'Piso-12',
        'reply_code': 'EKVFGQ',
    })
    assert device_duplicate.startswith('**❌ Device Duplicate Registration**')
    assert 'Code: EKVFGQ' in device_duplicate

    bind_failed_401 = service._format_lark_reply_text({
        'accepted': False,
        'reason': 'simulated_bind_failed',
        'result_reason': 'AxiosError: Request failed with status code 401',
        'reply_phone': '+62 81234567890',
        'reply_id': '55667788',
        'reply_group': 'Piso-12',
        'reply_code': 'EKVFGQ',
    })
    assert bind_failed_401.startswith('**❌ Failed：Error Code Unable to Bind**')
    assert 'Code: EKVFGQ' in bind_failed_401

    bind_backend_guild_mismatch = service._format_lark_reply_text({
        'accepted': False,
        'reason': 'bind_backend_guild_mismatch',
        'result_reason': 'Configured guild Permata does not match backend guild Piso.',
        'reply_phone': '+62 81234567890',
        'reply_id': '55667788',
        'reply_group': 'Piso-12',
        'reply_code': 'EKVFGQ',
    })
    assert bind_backend_guild_mismatch.startswith('**🚫 I do not handle this app/agency.**')
    assert 'Code: EKVFGQ' in bind_backend_guild_mismatch

    bind_profile_unconfigured = service._format_lark_reply_text({
        'accepted': False,
        'reason': 'bind_check_failed',
        'result_code': 'bind_executor_profile_not_configured',
        'result_reason': 'No Chrome profile mapping configured for browser_profile_key=',
        'reply_phone': '+62 81234567890',
        'reply_id': '55667788',
        'reply_group': 'Piso-12',
        'reply_code': 'EKVFGQ',
    })
    assert bind_profile_unconfigured.startswith('**🚫 I do not handle this app/agency.**')
    assert 'Code: EKVFGQ' in bind_profile_unconfigured

    invalid_personal_code = service._format_lark_reply_text({
        'accepted': False,
        'reason': 'bind_check_failed',
        'result_reason': 'HTTP 400: {"error":{"code":400,"message":"invalid person code "}}',
        'bind_failure_category': 'invalid_personal_code',
        'reply_phone': '+62 81234567890',
        'reply_id': '55667788',
        'reply_group': 'Piso-12',
        'reply_code': 'EKVFGQ',
    })
    assert invalid_personal_code.startswith('**❌ Bind failed: Invalid personal code**')

    auth_manual_recovery = service._format_lark_reply_text({
        'accepted': False,
        'reason': 'bind_check_failed',
        'result_reason': 'HTTP 401: please re-login',
        'bind_failure_category': 'session_expired',
        'reply_phone': '+62 81234567890',
        'reply_id': '55667788',
        'reply_group': 'Piso-12',
        'reply_code': 'EKVFGQ',
    })
    assert auth_manual_recovery.startswith('**❌ Failed：Error Code Unable to Bind**')

    generic_failed = service._format_lark_reply_text({
        'accepted': False,
        'reason': 'unsupported_message_type',
        'reply_phone': '-',
        'reply_id': '-',
        'reply_group': '-',
        'reply_code': '-',
    })
    assert 'Code: -' in generic_failed



def test_lead_upsert_creates_lead_and_customer_stub():
    client = make_client()

    response = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-1",
            "source_platform": "meta",
            "source_campaign": "camp-a",
            "source_page_id": "LK_ID/fb_general",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81234567890",
            "pendaftaran_group": "MCN-11",
            "app_name": "Linky",
            "dept_name": "Permata",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["is_new"] is True
    assert body["current_status"] == "new"
    assert body["matched_customer_id"] is not None
    assert body["lead_id"] is not None


def test_lead_upsert_auto_detects_area_code_and_country_from_phone_prefix():
    client = make_client()

    response = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-1b",
            "source_platform": "meta",
            "source_campaign": "camp-auto-prefix",
            "source_page_id": "LK_ID/fb_auto_prefix",
            "country": "",
            "area_code": 0,
            "mobile": "+62 81234567890",
            "pendaftaran_group": "Piso-5",
            "app_name": "Linky",
            "dept_name": "Piso",
        },
    )

    assert response.status_code == 200
    body = response.json()
    timeline = client.get(f"/api/leads/{body['lead_id']}/timeline")
    assert timeline.status_code == 200
    lead = timeline.json()["lead"]
    assert lead["country"] == "Indonesia"
    assert lead["area_code"] == 62
    assert lead["mobile"] == "81234567890"


def test_lead_upsert_fills_country_from_known_area_code_when_country_missing():
    client = make_client()

    response = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-1c",
            "source_platform": "meta",
            "source_campaign": "camp-auto-country",
            "source_page_id": "LK_ID/fb_auto_country",
            "country": "",
            "area_code": 55,
            "mobile": "11987654321",
        },
    )

    assert response.status_code == 200
    body = response.json()
    timeline = client.get(f"/api/leads/{body['lead_id']}/timeline")
    assert timeline.status_code == 200
    lead = timeline.json()["lead"]
    assert lead["country"] == "Brazil"
    assert lead["area_code"] == 55
    assert lead["mobile"] == "11987654321"


def test_event_collect_persists_event_and_returns_event_id():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-2",
            "source_platform": "meta",
            "source_page_id": "page-1",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81111111111",
        },
    ).json()

    response = client.post(
        "/api/events/collect",
        json={
            "trace_id": "trace-2",
            "lead_id": lead["lead_id"],
            "event_type": "account_id_submitted",
            "event_source": "landing_page",
            "event_value": "45772164",
            "page_id": "page-1",
            "session_id": "sess-1",
            "happened_at": "2026-02-11T09:00:00Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["event_id"] is not None


def test_task_lifecycle_create_and_report_result():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-3",
            "source_platform": "meta",
            "source_page_id": "page-1",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "82222222222",
        },
    ).json()

    task = client.post(
        "/api/tasks/create",
        json={
            "lead_id": lead["lead_id"],
            "task_type": "crm_sync",
            "priority": "P0",
            "payload": {"mobile": "82222222222"},
            "dedupe_key": "crm-sync-trace-3",
            "created_by": "system",
            "created_at": "2026-02-11T09:05:00Z",
        },
    ).json()

    response = client.post(
        f"/api/tasks/{task['task_id']}/result",
        json={
            "status": "success",
            "result_code": "ok",
            "result_reason": "synced",
            "finished_at": "2026-02-11T09:06:00Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == task["task_id"]
    assert body["crm_sync_status"] == "pending"
    assert body["next_action"] == "sync_customer"


def test_customer_sync_upserts_customer_projection():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-4",
            "source_platform": "meta",
            "source_page_id": "page-1",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "83333333333",
        },
    ).json()

    task = client.post(
        "/api/tasks/create",
        json={
            "lead_id": lead["lead_id"],
            "task_type": "crm_sync",
            "priority": "P0",
            "payload": {},
            "dedupe_key": "crm-sync-trace-4",
            "created_by": "system",
            "created_at": "2026-02-11T09:10:00Z",
        },
    ).json()

    response = client.post(
        "/api/crm/customer-sync",
        json={
            "lead_id": lead["lead_id"],
            "task_id": task["task_id"],
            "mobile": "83333333333",
            "area_code": 62,
            "crm_patch": {
                "pendaftaran_group": "MCN-11",
                "payment_status": "Waiting For Payment Rp30000",
                "user_quality": "优质",
                "remark": "auto synced",
            },
            "sync_mode": "upsert",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] in {"insert", "update"}
    assert body["sync_status"] == "success"
    assert body["customer_id"] is not None


def test_daily_summary_returns_aggregated_counts():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-5",
            "source_platform": "meta",
            "source_page_id": "page-1",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "84444444444",
        },
    ).json()
    client.post(
        "/api/events/collect",
        json={
            "trace_id": "trace-5",
            "lead_id": lead["lead_id"],
            "event_type": "contact_clicked",
            "event_source": "landing_page",
            "event_value": "wa",
            "page_id": "page-1",
            "session_id": "sess-5",
            "happened_at": "2026-02-11T09:00:00Z",
        },
    )
    task = client.post(
        "/api/tasks/create",
        json={
            "lead_id": lead["lead_id"],
            "task_type": "crm_sync",
            "priority": "P0",
            "payload": {},
            "dedupe_key": "crm-sync-trace-5",
            "created_by": "system",
            "created_at": "2026-02-11T09:10:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{task['task_id']}/result",
        json={
            "status": "success",
            "result_code": "ok",
            "result_reason": "done",
            "finished_at": "2026-02-11T09:12:00Z",
        },
    )

    response = client.get("/api/reports/daily-summary")

    assert response.status_code == 200
    body = response.json()
    assert body["lead_count"] == 1
    assert body["engaged_count"] == 1
    assert body["success_count"] == 1
    assert body["failed_count"] == 0
    assert body["pending_count"] == 0


def test_manual_cs_submission_creates_submission_and_followup_task():
    client = make_client()

    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81234567890",
            "registration_group": "Piso-5",
            "app_name": "Linky",
            "dept_name": "Piso",
            "invite_code": "EKVFGQ",
            "submission_type": "account_id",
            "account_id": "45678901",
            "file_url": None,
            "submitted_by": "dewi01",
            "source_channel": "manual_cs_lark",
            "remark": "用户已确认",
            "submitted_at": "2026-04-14T18:00:00Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["lead_id"] is not None
    assert body["submission_id"] is not None
    assert body["task_id"] is not None
    assert body["next_action"] == "queue_bind_check"
    assert body["parsed_payload"]["mobile"] == "81234567890"
    assert body["parsed_payload"]["account_id"] == "45678901"
    assert body["parsed_payload"]["registration_group"] == "Piso-5"
    assert body["parsed_payload"]["app_name"] == "Linky"
    assert body["parsed_payload"]["dept_name"] == "Piso"
    assert body["parsed_payload"]["invite_code"] == "EKVFGQ"
    assert body["parsed_payload"]["confidence"] == 1.0



def test_manual_cs_submission_without_invite_code_still_accepts_structured_api_for_backward_compatibility():
    client = make_client()

    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81234567890",
            "registration_group": "Piso-5",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "account_id",
            "account_id": "45678901",
            "submitted_by": "dewi01",
            "source_channel": "manual_cs_lark",
            "submitted_at": "2026-04-14T18:00:00Z",
        },
    )

    assert response.status_code == 200



def test_manual_cs_submission_rejects_explicit_app_guild_mismatch_against_current_preset():
    client = make_client({
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })

    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81234567891",
            "registration_group": "Piso-5",
            "app_name": "FUMI",
            "dept_name": "Permata",
            "submission_type": "account_id",
            "account_id": "45678902",
            "app_name_explicit": True,
            "dept_name_explicit": True,
            "submitted_by": "dewi01",
            "source_channel": "manual_cs_lark",
            "remark": "explicit mismatch test",
            "submitted_at": "2026-04-14T18:00:10Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'app_agency_mismatch'
    assert body['reply_phone'] == '81234567891'
    assert body['reply_id'] == '45678902'
    assert body['reply_group'] == 'Piso-5'



def test_manual_cs_submission_rejects_explicit_app_guild_mismatch_found_in_remark_even_if_payload_uses_defaults():
    client = make_client({
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })

    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 12312332",
            "registration_group": "Piso-44",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "account_id",
            "account_id": "131111211",
            "submitted_by": "dewi01",
            "source_channel": "manual_cs_lark",
            "remark": "Phone:+62 12312332\nID:131111211\nGroup:Piso-44\nApp:Fumi\nAgency:PERMATA",
            "submitted_at": "2026-04-14T18:00:15Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'app_agency_mismatch'
    assert body['reply_id'] == '131111211'
    assert body['reply_group'] == 'Piso-44'



def test_manual_cs_submission_allows_case_insensitive_app_guild_match_against_current_preset():
    client = make_client({
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })

    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81234567892",
            "registration_group": "Piso-5",
            "app_name": "linky",
            "dept_name": "pIsO",
            "app_name_explicit": True,
            "dept_name_explicit": True,
            "submission_type": "account_id",
            "account_id": "45678903",
            "submitted_by": "dewi01",
            "source_channel": "manual_cs_lark",
            "remark": "case insensitive match test",
            "submitted_at": "2026-04-14T18:00:20Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is True
    assert body['next_action'] == 'queue_bind_check'



def test_manual_cs_submission_non_explicit_payload_uses_current_preset_immediately():
    client = make_client({
        'LARK_DEFAULT_APP_NAME': 'FUMI',
        'LARK_DEFAULT_DEPT_NAME': 'Permata',
    })
    client.app.state.service.crm_adapter = StubCrmDropdownAdapter(
        apps=[{'id': 'app_1', 'name': 'FUMI'}, {'id': 'app_2', 'name': 'Linky'}],
        depts=[{'id': 'dept_1', 'deptName': 'Permata'}, {'id': 'dept_2', 'deptName': 'Piso'}],
    )
    saved = client.post('/api/ops/intake-bot-presets/current', json={
        'default_app': 'Linky',
        'default_guild': 'Piso',
    })
    assert saved.status_code == 200

    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81234567892",
            "registration_group": "Piso-5",
            "app_name": "FUMI",
            "dept_name": "Permata",
            "submission_type": "account_id",
            "account_id": "45678903",
            "submitted_by": "bridge01",
            "source_channel": "manual_cs_feishu",
            "submitted_at": "2026-04-14T18:00:20Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is True
    assert body['parsed_payload']['app_name'] == 'Linky'
    assert body['parsed_payload']['dept_name'] == 'Piso'



def test_manual_cs_submission_dedupes_cross_channel_duplicate_and_reuses_first_success():
    class CountingCrmAdapter:
        def __init__(self):
            self.calls = []
            self.apps = [{"id": "app_1", "name": "Linky"}]
            self.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            return {
                "ywId": yw_id,
                "mobile": mobile,
                "appName": "Linky",
                "deptName": "Piso",
                "pendaftaranGroup": "Piso-5",
            }
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            return {"code": 0, "msg": "success", "data": None}
        def get_apps(self):
            self.calls.append(("get_apps", {}))
            return list(self.apps)
        def get_depts(self):
            self.calls.append(("get_depts", {}))
            return list(self.depts)

    crm = CountingCrmAdapter()
    client = make_client({
        "CRM_ADAPTER": crm,
        "AUTO_BIND_SIMULATION": True,
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
        "BIND_SIMULATOR": lambda context: {
            "status": "success",
            "result_code": "bind_ok_simulated",
            "result_reason": "simulated bind success",
            "raw_result": {"guild_code": context["dept_name"], "deptName": context["dept_name"], "deptId": "dept_1"},
        },
    })

    first = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81234567890",
            "registration_group": "Piso-5",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "account_id",
            "account_id": "55667788",
            "submitted_by": "lark:ou_first",
            "source_channel": "manual_cs_lark",
            "submitted_at": "2026-04-14T18:00:20Z",
        },
    )
    assert first.status_code == 200
    assert first.json()['accepted'] is True

    second = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81234567890",
            "registration_group": "Piso-5",
            "app_name": "FUMI",
            "dept_name": "Permata",
            "submission_type": "account_id",
            "account_id": "55667788",
            "submitted_by": "bridge01",
            "source_channel": "manual_cs_feishu",
            "submitted_at": "2026-04-14T18:01:00Z",
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body['accepted'] is True
    assert second_body['reply_text'].startswith('**✅ Success**')
    assert second_body['parsed_payload']['app_name'] == 'Linky'
    assert second_body['parsed_payload']['dept_name'] == 'Piso'
    assert second_body['deduped'] is True

    create_calls = [payload for name, payload in crm.calls if name == 'create_customer']
    assert len(create_calls) == 1



def test_manual_cs_submission_short_circuits_verified_duplicate_with_fast_failure():
    class CountingCrmAdapter:
        def __init__(self):
            self.calls = []
            self.apps = [{"id": "app_1", "name": "Linky"}]
            self.depts = [{"deptId": "dept_perm", "deptName": "Permata"}]
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            return {
                "ywId": yw_id,
                "mobile": mobile,
                "appName": "Linky",
                "deptName": "Permata",
                "pendaftaranGroup": "Permata-88",
            }
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            return {"code": 0, "msg": "success", "data": None}
        def get_apps(self):
            self.calls.append(("get_apps", {}))
            return list(self.apps)
        def get_depts(self):
            self.calls.append(("get_depts", {}))
            return list(self.depts)

    crm = CountingCrmAdapter()
    client = make_client({
        "CRM_ADAPTER": crm,
        "AUTO_BIND_SIMULATION": True,
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Permata",
        "BIND_SIMULATOR": lambda context: {
            "status": "success",
            "result_code": "bind_ok_simulated",
            "result_reason": "simulated bind success",
            "raw_result": {"guild_code": 'Permata', "deptName": 'Permata', "deptId": "dept_perm"},
        },
    })

    first = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 18812321188",
            "registration_group": "Permata-88",
            "app_name": "Linky",
            "dept_name": "Permata",
            "app_name_explicit": True,
            "dept_name_explicit": True,
            "submission_type": "account_id",
            "account_id": "88909200",
            "submitted_by": "lark:first",
            "source_channel": "manual_cs_lark",
            "submitted_at": "2026-04-14T18:00:20Z",
        },
    )
    assert first.status_code == 200
    assert first.json()['accepted'] is True

    second = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 18812321188",
            "registration_group": "Permata-88",
            "app_name": "Linky",
            "dept_name": "Permata",
            "app_name_explicit": True,
            "dept_name_explicit": True,
            "submission_type": "account_id",
            "account_id": "88909200",
            "submitted_by": "lark:second",
            "source_channel": "manual_cs_lark",
            "submitted_at": "2026-04-14T18:10:20Z",
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body['accepted'] is False
    assert body['reason'] == 'crm_sync_failed'
    assert body['result_reason'] == 'Data duplication.'
    assert body['deduped'] is True

    create_calls = [payload for name, payload in crm.calls if name == 'create_customer']
    assert len(create_calls) == 1



def test_manual_cs_submission_recovers_verified_duplicate_from_legacy_success_sync_log_when_verified_columns_are_blank():
    class NoCreateCrmAdapter:
        def __init__(self):
            self.calls = []
            self.apps = [{"id": "app_1", "name": "Linky"}]
            self.depts = [{"deptId": "dept_perm", "deptName": "Permata"}]
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            return None
        def create_customer(self, payload):
            self.calls.append(("create_customer", dict(payload)))
            return {"code": 10002, "msg": "数据库中已存在该记录", "data": None}
        def get_apps(self):
            self.calls.append(("get_apps", {}))
            return list(self.apps)
        def get_depts(self):
            self.calls.append(("get_depts", {}))
            return list(self.depts)

    client = make_client({"CRM_ADAPTER": NoCreateCrmAdapter()})
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-legacy-verified-duplicate",
            "source_platform": "meta",
            "source_page_id": "page-legacy-verified-duplicate",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "18812321189",
            "app_name": "Linky",
            "dept_name": "Permata",
            "pendaftaran_group": "Permata-88",
            "yw_id": "88909201",
        },
    ).json()

    with client.app.state.service.db.connect() as conn:
        conn.execute(
            """
            UPDATE leads
            SET crm_verified_payload = NULL,
                crm_verified_app_name = NULL,
                crm_verified_dept_name = NULL,
                crm_verified_registration_group = NULL,
                crm_verified_official_group = NULL,
                crm_verified_at = NULL
            WHERE lead_id = ?
            """,
            (lead["lead_id"],),
        )
        conn.execute(
            """
            INSERT INTO sync_logs (
                sync_log_id, lead_id, task_id, sync_type, target_system, status,
                request_snapshot, response_snapshot, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "sync_legacy_verified_duplicate",
                lead["lead_id"],
                "task_legacy_verified_duplicate",
                "customer_upsert",
                "crm",
                "success",
                json.dumps({
                    "ywId": "88909201",
                    "mobile": "18812321189",
                    "appName": "Linky",
                    "deptName": "Permata",
                    "pendaftaranGroup": "Permata-88",
                }),
                json.dumps({
                    "action": "create",
                    "crm_response": {"code": 0, "msg": "success"},
                }),
                "2026-04-14T18:00:20Z",
            ),
        )
        conn.commit()

    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 18812321189",
            "registration_group": "Permata-88",
            "app_name": "Linky",
            "dept_name": "Permata",
            "app_name_explicit": True,
            "dept_name_explicit": True,
            "submission_type": "account_id",
            "account_id": "88909201",
            "submitted_by": "lark:legacy-duplicate",
            "source_channel": "manual_cs_lark",
            "submitted_at": "2026-04-14T18:10:20Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'crm_sync_failed'
    assert body['result_reason'] == 'Data duplication.'
    assert body['deduped'] is True

    crm = client.app.state.service.crm_adapter
    create_calls = [payload for name, payload in crm.calls if name == 'create_customer']
    assert create_calls == []



def test_manual_cs_submission_verified_duplicate_does_not_short_circuit_when_dept_differs():
    class CountingCrmAdapter:
        def __init__(self):
            self.calls = []
            self.record = None
            self.apps = [{"id": "app_linky", "name": "Linky"}]
            self.depts = [
                {"deptId": "dept_piso", "deptName": "Piso"},
                {"deptId": "dept_permata", "deptName": "Permata"},
            ]
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            return dict(self.record) if self.record else None
        def create_customer(self, payload):
            self.calls.append(("create_customer", dict(payload)))
            self.record = dict(payload)
            return {"code": 0, "msg": "success", "data": None}
        def get_apps(self):
            self.calls.append(("get_apps", {}))
            return list(self.apps)
        def get_depts(self):
            self.calls.append(("get_depts", {}))
            return list(self.depts)

    crm = CountingCrmAdapter()
    client = make_client({
        "CRM_ADAPTER": crm,
        "AUTO_BIND_SIMULATION": True,
        "BIND_SIMULATOR": lambda context: {
            "status": "success",
            "result_code": "bind_ok_simulated",
            "result_reason": "simulated bind success",
            "raw_result": {"guild_code": context["dept_name"], "deptName": context["dept_name"]},
        },
    })

    first = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 18812320001",
            "registration_group": "Piso-88",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "account_id",
            "account_id": "88990011",
            "submitted_by": "lark:first",
            "source_channel": "manual_cs_lark",
            "submitted_at": "2026-04-14T18:00:20Z",
        },
    )
    assert first.status_code == 200
    assert first.json()['accepted'] is True

    second = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 18812320001",
            "registration_group": "Piso-88",
            "app_name": "Linky",
            "dept_name": "Permata",
            "submission_type": "account_id",
            "account_id": "88990011",
            "submitted_by": "lark:second",
            "source_channel": "manual_cs_lark",
            "submitted_at": "2026-04-14T18:10:20Z",
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body['accepted'] is True
    assert body.get('deduped') is not True
    create_calls = [payload for name, payload in crm.calls if name == 'create_customer']
    assert len(create_calls) == 2
    assert create_calls[-1]['deptName'] == 'Permata'



def test_bind_check_success_auto_resolves_prior_failed_notifications_for_same_lead():
    class SequenceCrmAdapter:
        def __init__(self):
            self.calls = []
            self.apps = [{"id": "app_1", "name": "Linky"}]
            self.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
            self.responses = [
                {"code": 500, "msg": "crm rejected write", "data": None},
                {"code": 0, "msg": "success", "data": None},
            ]
            self.created_success = False
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            if self.created_success:
                return {"ywId": yw_id, "mobile": mobile, "appName": "Linky", "deptName": "Piso", "pendaftaranGroup": "Piso-77"}
            return None
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            response = self.responses.pop(0)
            if response.get('code') == 0:
                self.created_success = True
            return response
        def get_apps(self):
            return list(self.apps)
        def get_depts(self):
            return list(self.depts)

    crm = SequenceCrmAdapter()
    client = make_client({
        "CRM_ADAPTER": crm,
        "AUTO_BIND_SIMULATION": True,
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
        "BIND_SIMULATOR": lambda context: {
            "status": "success",
            "result_code": "bind_ok_simulated",
            "result_reason": "simulated bind success",
            "raw_result": {"guild_code": context["dept_name"], "deptName": context["dept_name"], "deptId": "dept_1"},
        },
    })

    first = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81110000071",
            "registration_group": "Piso-77",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "account_id",
            "account_id": "71717171",
            "submitted_by": "lark:first",
            "source_channel": "manual_cs_lark",
            "submitted_at": "2026-04-14T18:00:20Z",
        },
    )
    assert first.status_code == 200
    assert first.json()['accepted'] is False

    second = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81110000071",
            "registration_group": "Piso-77",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "account_id",
            "account_id": "71717171",
            "submitted_by": "lark:second",
            "source_channel": "manual_cs_lark",
            "submitted_at": "2026-04-14T18:10:20Z",
        },
    )
    assert second.status_code == 200
    assert second.json()['accepted'] is True

    rows = client.get('/api/ops/operator-notifications').json()['rows']
    success_row = next(row for row in rows if row['notification_type'] == 'crm_record_success')
    assert success_row['is_read'] is False
    assert not [row for row in rows if row['notification_type'] == 'crm_record_failed']



def test_manual_cs_submission_can_auto_simulate_bind_success_and_sync_crm():
    crm = StubCrmAdapter()
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    client = make_client({
        "CRM_ADAPTER": crm,
        "AUTO_BIND_SIMULATION": True,
        "BIND_SIMULATOR": lambda context: {
            "status": "success",
            "result_code": "bind_ok_simulated",
            "result_reason": "simulated bind success",
            "raw_result": {"guild_code": context["dept_name"], "deptName": context["dept_name"]},
        },
    })

    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81234567890",
            "registration_group": "Piso-5",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "account_id",
            "account_id": "45678901",
            "submitted_by": "dewi01",
            "source_channel": "manual_cs_lark",
            "remark": "用户已确认",
            "submitted_at": "2026-04-14T18:00:00Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["simulation_applied"] is True
    assert body["simulated_bind_status"] == "success"
    assert body["lead_status"] == "bind_success"
    assert body["next_action"] == "queue_group_join"
    create_payload = next(payload for name, payload in crm.calls if name == "create_customer")
    assert create_payload["ywId"] == "45678901"
    assert create_payload["appId"] == "app_1"
    assert create_payload["deptId"] == "dept_1"



def test_manual_cs_submission_default_reply_text_is_submitted_not_success_before_crm_success():
    reply = StubLarkReplyAdapter()
    client = make_client({
        "LARK_APP_ID": "cli_test",
        "LARK_REPLY_ADAPTER": reply,
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
    })

    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_queue_only'}},
            'message': {
                'message_id': 'om_queue_only',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567890\\nPiso-19\\n45678901"}'
            }
        }
    })

    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is True
    assert body['next_action'] == 'queue_bind_check'
    assert body.get('reply_text', '') == ''
    assert reply.calls == []



def test_lark_event_does_not_reply_success_when_crm_sync_fails_after_bind_success():
    class FailingCreateCrmAdapter:
        def __init__(self):
            self.calls = []
            self.record = None
            self.apps = []
            self.depts = []
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            return self.record
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            return {"code": 500, "msg": "crm rejected write", "data": None}
        def update_customer(self, payload):
            self.calls.append(("update_customer", payload))
            self.record = payload
            return {"code": 0, "msg": "success", "data": None}
        def get_apps(self):
            return list(self.apps)
        def get_depts(self):
            return list(self.depts)

    reply = StubLarkReplyAdapter()
    crm = FailingCreateCrmAdapter()
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    client = make_client({
        "CRM_ADAPTER": crm,
        "LARK_APP_ID": "cli_test",
        "LARK_REPLY_ADAPTER": reply,
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
        "AUTO_BIND_SIMULATION": True,
        "BIND_SIMULATOR": lambda context: {
            "status": "success",
            "result_code": "bind_ok_simulated",
            "result_reason": "simulated bind success",
            "raw_result": {"guild_code": context["dept_name"], "deptName": context["dept_name"], "deptId": "dept_1"},
        },
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_crm_fail'}},
            'message': {
                'message_id': 'om_text_crm_fail',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567890\\nPiso-25\\n45678901"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'crm_sync_retry_pending'
    assert body['next_action'] == 'queue_crm_sync_retry'
    assert reply.calls == []



def test_process_next_automation_task_executes_pending_bind_task_and_updates_result():
    client = make_client({
        "LARK_APP_ID": "cli_test",
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
        "AUTO_BIND_SIMULATION": False,
        "BIND_SIMULATOR": lambda context: {
            "status": "failed",
            "result_code": "bind_unauthorized",
            "result_reason": "AxiosError: Request failed with status code 401",
            "raw_result": {"guild_code": context["dept_name"]},
        },
    })

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_bind_worker'}},
            'message': {
                'message_id': 'om_bind_worker',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567890\\nPiso-25\\n45678901\\nCode EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['next_action'] == 'queue_bind_check'

    processed = client.app.state.service.process_next_automation_task()
    assert processed is not None
    assert processed['lead_status'] == 'bind_failed'
    assert processed['result_reason'] == 'AxiosError: Request failed with status code 401'

    timeline = client.get(f"/api/leads/{body['lead_id']}/timeline").json()
    bind_task = next(task for task in timeline['tasks'] if task['task_id'] == body['task_id'])
    assert bind_task['status'] == 'failed'
    assert bind_task['result_reason'] == 'AxiosError: Request failed with status code 401'



def test_process_next_automation_task_retryable_crm_failure_queues_auto_retry_without_immediate_reply():
    class RetryableCrmAdapter:
        def __init__(self):
            self.calls = []
            self.apps = [{"id": "app_1", "name": "Linky"}]
            self.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
            self.create_attempts = 0
        def get_apps(self):
            return list(self.apps)
        def get_depts(self):
            return list(self.depts)
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            self.create_attempts += 1
            return {"code": 500, "msg": "服务器内部异常", "data": None}
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            return None

    reply = StubLarkReplyAdapter()
    crm = RetryableCrmAdapter()
    client = make_client({
        "CRM_ADAPTER": crm,
        "LARK_APP_ID": "cli_test",
        "LARK_REPLY_ADAPTER": reply,
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
        "CRM_RETRY_DELAYS_SECONDS": [0, 0, 0],
        "CRM_RETRY_MAX_ATTEMPTS": 3,
        "AUTO_BIND_SIMULATION": True,
        "BIND_SIMULATOR": lambda context: {
            "status": "success",
            "result_code": "bind_ok_simulated",
            "result_reason": "simulated bind success",
            "raw_result": {"guild_code": context["dept_name"], "deptName": context["dept_name"], "deptId": "dept_1"},
        },
    })

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_crm_retry_pending'}},
            'message': {
                'message_id': 'om_crm_retry_pending',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567890\\nPiso-25\\n45678901\\nCode EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['lead_status'] == 'bind_success'
    assert body['next_action'] == 'queue_crm_sync_retry'
    assert reply.calls == []

    timeline = client.get(f"/api/leads/{body['lead_id']}/timeline").json()
    retry_tasks = [task for task in timeline['tasks'] if task['task_type'] == 'crm_sync_retry']
    assert len(retry_tasks) == 1
    assert retry_tasks[0]['status'] == 'pending'

    processed = client.app.state.service.process_next_automation_task()
    assert processed is not None
    assert processed['lead_status'] == 'bind_success'
    assert processed['reason'] == 'crm_sync_retry_pending'
    assert processed['next_action'] == 'queue_crm_sync_retry'
    assert reply.calls == []

    timeline = client.get(f"/api/leads/{body['lead_id']}/timeline").json()
    retry_tasks = [task for task in timeline['tasks'] if task['task_type'] == 'crm_sync_retry']
    assert len(retry_tasks) == 1
    assert retry_tasks[0]['status'] == 'pending'



def test_process_next_automation_task_executes_due_crm_retry_and_replies_success_after_verification():
    class FlakyRetryableCrmAdapter:
        def __init__(self):
            self.calls = []
            self.apps = [{"id": "app_1", "name": "Linky"}]
            self.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
            self.create_attempts = 0
        def get_apps(self):
            return list(self.apps)
        def get_depts(self):
            return list(self.depts)
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            self.create_attempts += 1
            if self.create_attempts == 1:
                return {"code": 500, "msg": "服务器内部异常", "data": None}
            return {"code": 0, "msg": "success", "data": None}
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            if self.create_attempts >= 2:
                return {
                    "id": "crm_retry_ok_2",
                    "ywId": yw_id,
                    "mobile": mobile,
                    "appName": "Linky",
                    "deptName": "Piso",
                    "pendaftaranGroup": "Piso-25",
                }
            return None

    reply = StubLarkReplyAdapter()
    crm = FlakyRetryableCrmAdapter()
    client = make_client({
        "CRM_ADAPTER": crm,
        "LARK_APP_ID": "cli_test",
        "LARK_REPLY_ADAPTER": reply,
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
        "CRM_RETRY_DELAYS_SECONDS": [0, 0, 0],
        "CRM_RETRY_MAX_ATTEMPTS": 3,
        "AUTO_BIND_SIMULATION": True,
        "BIND_SIMULATOR": lambda context: {
            "status": "success",
            "result_code": "bind_ok_simulated",
            "result_reason": "simulated bind success",
            "raw_result": {"guild_code": context["dept_name"], "deptName": context["dept_name"], "deptId": "dept_1"},
        },
    })

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_crm_retry_ok'}},
            'message': {
                'message_id': 'om_crm_retry_ok',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567890\\nPiso-25\\n45678901\\nCode EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['lead_status'] == 'bind_success'
    assert body['next_action'] == 'queue_crm_sync_retry'
    assert reply.calls == []

    first = client.app.state.service.process_next_automation_task()
    assert first is not None
    assert first['next_action'] == 'queue_group_join'
    assert first['crm_verified'] is True
    assert len(reply.calls) == 1
    assert reply.calls[0]['text'].startswith('**✅ Success**')



def test_process_next_automation_task_replies_retry_exhausted_crm_message_after_all_retries_fail():
    class AlwaysFailRetryableCrmAdapter:
        def __init__(self):
            self.calls = []
            self.apps = [{"id": "app_1", "name": "Linky"}]
            self.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
        def get_apps(self):
            return list(self.apps)
        def get_depts(self):
            return list(self.depts)
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            return {"code": 500, "msg": "服务器内部异常", "data": None}
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            return None

    reply = StubLarkReplyAdapter()
    crm = AlwaysFailRetryableCrmAdapter()
    client = make_client({
        "CRM_ADAPTER": crm,
        "LARK_APP_ID": "cli_test",
        "LARK_REPLY_ADAPTER": reply,
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
        "CRM_RETRY_DELAYS_SECONDS": [0, 0, 0],
        "CRM_RETRY_MAX_ATTEMPTS": 3,
        "AUTO_BIND_SIMULATION": True,
        "BIND_SIMULATOR": lambda context: {
            "status": "success",
            "result_code": "bind_ok_simulated",
            "result_reason": "simulated bind success",
            "raw_result": {"guild_code": context["dept_name"], "deptName": context["dept_name"], "deptId": "dept_1"},
        },
    })

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_crm_retry_fail'}},
            'message': {
                'message_id': 'om_crm_retry_fail',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567890\\nPiso-25\\n45678901\\nCode EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['next_action'] == 'queue_crm_sync_retry'
    assert reply.calls == []

    first = client.app.state.service.process_next_automation_task()
    assert first['next_action'] == 'queue_crm_sync_retry'
    assert reply.calls == []

    second = client.app.state.service.process_next_automation_task()
    assert second['next_action'] == 'queue_crm_sync_retry'
    assert reply.calls == []

    third = client.app.state.service.process_next_automation_task()
    assert third['reason'] == 'crm_sync_failed'
    assert third['result_code'] in {'crm_retry_exhausted', 'crm_retry_failed'}
    assert len(reply.calls) == 1
    assert reply.calls[0]['text'].startswith('**❌ CRM sync failed: Bind succeeded but CRM retried.**')



def test_process_next_automation_task_device_limit_failure_uses_duplicate_registration_template():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_default_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Permata',
        'AUTO_BIND_SIMULATION': False,
        'BIND_SIMULATOR': lambda context: {
            'status': 'failed',
            'result_code': 'bind_backend_http_error',
            'result_reason': 'HTTP 400: {"error":{"code":-1,"message":"Perangkat ini telah mencapai batas maksimum guild yang dapat diikuti."}}',
            'raw_result': {'guild_code': context['dept_name']},
        },
        'LARK_REPLY_ADAPTER': reply,
    })
    executor = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_region': '厦门',
        'proxy_type': 'http',
        'enabled': True,
        'browser_profile_key': 'permata-profile',
    })
    assert executor.status_code == 200

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_bind_device_limit'}},
            'message': {
                'message_id': 'om_bind_device_limit',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 85220623938\\nPERMATA-909\\n51654982\\nCode QFHVFL"}'
            }
        }
    })
    assert response.status_code == 200

    processed = client.app.state.service.process_next_automation_task()
    assert processed is not None
    assert processed['lead_status'] == 'bind_failed'
    assert processed['result_reason'].startswith('HTTP 400:')
    assert processed['reply_text'].startswith('**❌ Device Duplicate Registration**')
    assert reply.calls[0]['text'].startswith('**❌ Device Duplicate Registration**')



def test_process_next_automation_task_uses_bot_specific_reply_adapter_when_app_id_provided():
    default_reply = StubLarkReplyAdapter()
    permata_reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_default_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Permata',
        'AUTO_BIND_SIMULATION': False,
        'BIND_SIMULATOR': lambda context: {
            'status': 'failed',
            'result_code': 'bind_backend_http_error',
            'result_reason': 'HTTP 400: {"error":{"code":-1,"message":"Perangkat ini telah mencapai batas maksimum guild yang dapat diikuti."}}',
            'raw_result': {'guild_code': context['dept_name']},
        },
        'LARK_REPLY_ADAPTER': default_reply,
        'LARK_REPLY_ADAPTER_BY_APP_ID': {
            'cli_a96f1cec1a789e15': permata_reply,
        },
    })
    executor = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_region': '厦门',
        'proxy_type': 'http',
        'enabled': True,
        'browser_profile_key': 'permata-profile',
    })
    assert executor.status_code == 200

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        '_bot_app_id': 'cli_a96f1cec1a789e15',
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_bind_device_limit_bot_specific'}},
            'message': {
                'message_id': 'om_bind_device_limit_bot_specific',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 85220623938\\nPERMATA-909\\n51654982\\nCode QFHVFL"}'
            }
        }
    })
    assert response.status_code == 200

    processed = client.app.state.service.process_next_automation_task()
    assert processed is not None
    assert processed['reply_text'].startswith('**❌ Device Duplicate Registration**')
    assert default_reply.calls == []
    assert permata_reply.calls
    assert permata_reply.calls[0]['message_id'] == 'om_bind_device_limit_bot_specific'



def test_process_next_automation_task_flags_auth_required_for_human_action_and_runtime_health_exposes_it():
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Permata',
        'AUTO_BIND_SIMULATION': False,
        'BIND_SIMULATOR': lambda context: {
            'status': 'failed',
            'result_code': 'bind_unauthorized',
            'result_reason': 'HTTP 401: please re-login',
            'raw_result': {'guild_code': context['dept_name'], 'auth_required': True},
        },
    })
    executor = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_region': '厦门',
        'proxy_type': 'http',
        'enabled': True,
        'browser_profile_key': 'permata-profile',
    })
    assert executor.status_code == 200

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_auth_required'}},
            'message': {
                'message_id': 'om_auth_required',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567123\\nPERMATA-909\\n55667788\\nCode EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200

    processed = client.app.state.service.process_next_automation_task()
    assert processed is not None
    assert processed['requires_human_action'] is True
    assert processed['human_action_type'] == 'auth_required'

    health = client.get('/api/ops/runtime-health').json()
    assert health['ingress']['pending_bind_human_action_count'] >= 1
    assert health['ingress']['pending_bind_human_actions'][0]['human_action_type'] == 'auth_required'
    assert health['ingress']['pending_bind_human_actions'][0]['task_id'] == processed['task_id']



def test_process_next_automation_task_schedules_bind_retry_for_retryable_failure_without_reply():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Permata',
        'AUTO_BIND_SIMULATION': False,
        'LARK_REPLY_ADAPTER': reply,
        'BIND_SIMULATOR': lambda context: {
            'status': 'failed',
            'result_code': 'bind_execution_error',
            'result_reason': 'connection reset by peer',
            'raw_result': {'guild_code': context['dept_name']},
        },
    })
    executor = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_region': '厦门',
        'proxy_type': 'http',
        'enabled': True,
        'browser_profile_key': 'permata-profile',
    })
    assert executor.status_code == 200

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_bind_retry_pending'}},
            'message': {
                'message_id': 'om_bind_retry_pending',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567124\\nPERMATA-910\\n55667789\\nCode EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200

    processed = client.app.state.service.process_next_automation_task()
    assert processed is not None
    assert processed['next_action'] == 'queue_bind_retry'
    assert processed['reason'] == 'bind_retry_pending'
    assert processed['retry_count'] == 1
    assert reply.calls == []

    with client.app.state.service.db.connect() as conn:
        rows = conn.execute(
            "SELECT task_id, status, retry_count, result_code FROM automation_tasks WHERE lead_id = ? AND task_type = 'bind_check' ORDER BY created_at ASC",
            (response.json()['lead_id'],),
        ).fetchall()
        lead_row = conn.execute("SELECT current_status FROM leads WHERE lead_id = ?", (response.json()['lead_id'],)).fetchone()
    assert [row['status'] for row in rows] == ['failed', 'pending']
    assert [row['retry_count'] for row in rows] == [0, 1]
    assert lead_row['current_status'] == 'bind_check_pending'



def test_bind_check_result_stops_after_two_retries_and_notifies_operator_with_business_reason():
    client = make_client({'BIND_RETRY_MAX_ATTEMPTS': 2})
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-bind-retry-exhausted",
            "source_platform": "manual_cs",
            "source_campaign": "lark",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000088",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-88",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "88888888",
            "account_id_type": "platform_uid",
            "source_channel": "manual_cs_lark",
            "submitted_by": "cs_retry",
            "submitted_at": "2026-04-15T09:10:00Z",
        },
    ).json()

    first = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "failed",
            "result_code": "bind_execution_error",
            "result_reason": "connection reset by peer",
            "finished_at": "2026-04-15T09:12:00Z",
            "raw_result": {},
        },
    ).json()
    assert first['next_action'] == 'queue_bind_retry'
    assert first['retry_count'] == 1

    with client.app.state.service.db.connect() as conn:
        retry_one = conn.execute(
            "SELECT task_id FROM automation_tasks WHERE lead_id = ? AND task_type = 'bind_check' AND retry_count = 1 ORDER BY created_at DESC LIMIT 1",
            (lead['lead_id'],),
        ).fetchone()
    assert retry_one is not None

    second = client.post(
        f"/api/tasks/{retry_one['task_id']}/bind-check-result",
        json={
            "status": "failed",
            "result_code": "bind_execution_error",
            "result_reason": "connection reset by peer",
            "finished_at": "2026-04-15T09:13:00Z",
            "raw_result": {},
        },
    ).json()
    assert second['next_action'] == 'queue_bind_retry'
    assert second['retry_count'] == 2

    with client.app.state.service.db.connect() as conn:
        retry_two = conn.execute(
            "SELECT task_id FROM automation_tasks WHERE lead_id = ? AND task_type = 'bind_check' AND retry_count = 2 ORDER BY created_at DESC LIMIT 1",
            (lead['lead_id'],),
        ).fetchone()
    assert retry_two is not None

    final = client.post(
        f"/api/tasks/{retry_two['task_id']}/bind-check-result",
        json={
            "status": "failed",
            "result_code": "bind_execution_error",
            "result_reason": "connection reset by peer",
            "finished_at": "2026-04-15T09:14:00Z",
            "raw_result": {},
        },
    ).json()
    assert final['lead_status'] == 'bind_failed'
    assert final['reason'] == 'bind_check_failed'
    assert final['bind_failure_category'] == 'technical_retryable'

    rows = client.get('/api/ops/operator-notifications').json()['rows']
    assert rows[0]['notification_type'] == 'bind_check_failed'
    assert rows[0]['reason'] == 'Bind failed after 2 retries. Check guild executor/network manually.'



def test_process_next_automation_task_uses_real_bind_executor_when_configured():
    captured = {}

    def real_bind_executor(context):
        captured.update(context)
        return {
            'status': 'success',
            'result_code': 'bind_success',
            'result_reason': 'live bind ok',
            'raw_result': {'guild_code': context['dept_name'], 'executor_mode': 'real'},
        }

    client = make_client({
        'LARK_APP_ID': 'cli_default_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Permata',
        'AUTO_BIND_SIMULATION': False,
        'REAL_BIND_EXECUTOR': real_bind_executor,
    })
    executor = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_url': 'http://proxy-xm:8080',
        'proxy_region': '厦门',
        'proxy_type': 'http',
        'enabled': True,
        'browser_profile_key': 'permata-profile',
        'bind_concurrency': 3,
        'request_timeout_seconds': 45,
        'notes': 'permata executor',
    })
    assert executor.status_code == 200

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_bind_executor_real'}},
            'message': {
                'message_id': 'om_bind_executor_real',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567890\\nPermata-25\\n45678901\\nCode EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200

    processed = client.app.state.service.process_next_automation_task()
    assert processed is not None
    assert captured['dept_name'] == 'Permata'
    assert captured['invite_code'] == 'EKVFGQ'
    assert captured['executor_browser_profile_key'] == 'permata-profile'
    assert processed['lead_status'] == 'bind_success'

    timeline = client.get(f"/api/leads/{response.json()['lead_id']}/timeline").json()
    bind_task = next(task for task in timeline['tasks'] if task['task_id'] == response.json()['task_id'])
    assert bind_task['status'] == 'success'
    assert bind_task['result_reason'] == 'live bind ok'


def test_process_next_automation_task_prefers_registration_group_executor_when_preset_guild_has_no_executor():
    captured = {}

    def real_bind_executor(context):
        captured.update(context)
        return {
            'status': 'success',
            'result_code': 'bind_success',
            'result_reason': 'live bind ok',
            'raw_result': {'guild_code': context['dept_name'], 'executor_mode': 'real'},
        }

    client = make_client({
        'LARK_APP_ID': 'cli_default_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'AUTO_BIND_SIMULATION': False,
        'REAL_BIND_EXECUTOR': real_bind_executor,
    })
    preset = client.app.state.service._upsert_intake_bot_preset_row(
        profile_name='intake',
        app_id='cli_a955df8b1e38de17',
        robot_name='Lk-Piso',
        default_app='Linky',
        default_guild='Piso',
        enabled=1,
    )
    assert preset['profile_name'] == 'intake'
    executor = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_region': '厦门',
        'proxy_type': 'http',
        'enabled': True,
        'browser_profile_key': 'permata-profile',
    })
    assert executor.status_code == 200

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        '_bot_app_id': 'cli_a955df8b1e38de17',
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_bind_executor_preset_fallback'}},
            'message': {
                'message_id': 'om_bind_executor_preset_fallback',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567890\\nPermata-90\\n45678901\\nCode EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200

    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'app_guild_mismatch'
    assert body['reply_text'].startswith('**🚫 I do not handle this app/agency.**')
    assert captured == {}


def test_intake_bot_rejects_group_owned_by_other_guild_before_bind_duplicate():
    client = make_client({
        'LARK_APP_ID': 'cli_default_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    client.app.state.service._upsert_intake_bot_preset_row(
        profile_name='intake',
        app_id='cli_a955df8b1e38de17',
        robot_name='Lk-Piso',
        default_app='Linky',
        default_guild='Piso',
        enabled=1,
    )
    executor = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_region': '厦门',
        'proxy_type': 'http',
        'enabled': True,
        'browser_profile_key': 'permata-profile',
    })
    assert executor.status_code == 200

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        '_bot_app_id': 'cli_a955df8b1e38de17',
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_scope_guard'}},
            'message': {
                'message_id': 'om_scope_guard',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+86 13860640933\\n51797757\\n6JL9MC\\nPermata-90"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'app_guild_mismatch'
    assert body['reply_text'].startswith('**🚫 I do not handle this app/agency.**')



def test_process_next_automation_task_resolves_matching_guild_executor_config():
    captured = {}

    def bind_simulator(context):
        captured.update(context)
        return {
            'status': 'failed',
            'result_code': 'bind_unauthorized',
            'result_reason': 'AxiosError: Request failed with status code 401',
            'raw_result': {'guild_code': context['dept_name']},
        }

    client = make_client({
        'LARK_APP_ID': 'cli_default_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'AUTO_BIND_SIMULATION': False,
        'BIND_SIMULATOR': bind_simulator,
    })
    client.app.state.service.crm_adapter = StubCrmDropdownAdapter(
        apps=[{'id': 'app_1', 'name': 'Linky'}, {'id': 'app_2', 'name': 'FUMI'}],
        depts=[{'id': 'dept_1', 'deptName': 'Piso'}, {'id': 'dept_2', 'deptName': 'Permata'}],
    )
    created = client.post('/api/ops/intake-bot-presets/intake-a96f1cec', json={
        'app_id': 'cli_a96f1cec1a789e15',
        'default_app': 'FUMI',
        'default_guild': 'Permata',
    })
    assert created.status_code == 200
    executor = client.post('/api/ops/guild-executors/Permata', json={
        'backend_url': 'https://guild.linke.ai/guild/addAnchor',
        'login_username': 'permata@example.com',
        'password_secret_ref': 'secret_perm',
        'proxy_url': 'http://proxy-xm:8080',
        'proxy_region': '厦门',
        'proxy_type': 'http',
        'enabled': True,
        'browser_profile_key': 'permata-profile',
        'bind_concurrency': 3,
        'request_timeout_seconds': 45,
        'notes': 'permata executor',
    })
    assert executor.status_code == 200

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        '_bot_app_id': 'cli_a96f1cec1a789e15',
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_bind_executor'}},
            'message': {
                'message_id': 'om_bind_executor',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567890\\nPermata-25\\n45678901\\nCode EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200

    processed = client.app.state.service.process_next_automation_task()
    assert processed is not None
    assert captured['dept_name'] == 'Permata'
    assert captured['invite_code'] == 'EKVFGQ'
    assert captured['executor_backend_url'] == 'https://guild.linke.ai/guild/addAnchor'
    assert captured['executor_login_username'] == 'permata@example.com'
    assert captured['executor_proxy_url'] == 'http://proxy-xm:8080'
    assert captured['executor_proxy_region'] == '厦门'
    assert captured['executor_browser_profile_key'] == 'permata-profile'
    assert captured['executor_bind_concurrency'] == 3
    assert captured['executor_request_timeout_seconds'] == 45
    assert processed['executor']['guild_name'] == 'Permata'
    assert processed['executor']['proxy_region'] == '厦门'
    assert processed['executor']['password_configured'] is True



def test_bind_success_crm_failure_does_not_create_group_join_task():
    class RejectingCrmAdapter:
        def __init__(self):
            self.apps = [{"id": "app_1", "name": "Linky"}]
            self.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
        def get_apps(self):
            return list(self.apps)
        def get_depts(self):
            return list(self.depts)
        def create_customer(self, payload):
            return {"code": 500, "msg": "crm rejected write", "data": None}
        def find_customer(self, *, yw_id=None, mobile=None):
            return None

    client = make_client({"CRM_ADAPTER": RejectingCrmAdapter()})
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-no-group-task-on-crm-fail",
            "source_platform": "manual_cs",
            "source_campaign": "lark",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000999",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-99",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "99990011",
            "account_id_type": "platform_uid",
            "source_channel": "manual_cs_lark",
            "submitted_by": "cs_bind",
            "submitted_at": "2026-04-15T09:10:00Z",
        },
    ).json()

    body = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "guild accepted",
            "finished_at": "2026-04-15T09:12:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    ).json()

    assert body['reason'] == 'crm_sync_retry_pending'
    assert body['next_action'] == 'queue_crm_sync_retry'
    assert body['group_join_task_type'] is None
    timeline = client.get(f"/api/leads/{lead['lead_id']}/timeline").json()
    assert not [task for task in timeline['tasks'] if task['task_type'] == 'group_join']



def test_ops_retry_bind_requeues_bind_without_creating_new_submission():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-retry-bind-1",
            "source_platform": "manual_cs",
            "source_campaign": "lark",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000888",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-88",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "88880011",
            "account_id_type": "platform_uid",
            "source_channel": "manual_cs_lark",
            "submitted_by": "cs_retry",
            "submitted_at": "2026-04-15T10:10:00Z",
        },
    ).json()

    retried = client.post(f"/api/ops/submissions/{submission['submission_id']}/retry-bind")
    assert retried.status_code == 200
    body = retried.json()
    assert body['accepted'] is True
    assert body['retry_type'] == 'bind'
    assert body['created_new_submission'] is False
    assert body['next_action'] == 'queue_bind_check'

    timeline = client.get(f"/api/leads/{lead['lead_id']}/timeline").json()
    assert len(timeline['account_submissions']) == 1
    bind_tasks = [task for task in timeline['tasks'] if task['task_type'] == 'bind_check']
    assert len(bind_tasks) == 2
    assert bind_tasks[-1]['task_id'] == body['task_id']
    assert bind_tasks[-1]['status'] == 'pending'



def test_ops_retry_crm_replays_crm_sync_and_queues_group_join_after_success():
    class FlakyCrmAdapter:
        def __init__(self):
            self.calls = []
            self.apps = [{"id": "app_1", "name": "Linky"}]
            self.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
            self.create_attempts = 0
        def get_apps(self):
            return list(self.apps)
        def get_depts(self):
            return list(self.depts)
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            self.create_attempts += 1
            if self.create_attempts == 1:
                return {"code": 500, "msg": "crm rejected write", "data": None}
            return {"code": 0, "msg": "success", "data": None}
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            if self.create_attempts >= 2:
                return {
                    "id": "crm_retry_ok_1",
                    "ywId": yw_id,
                    "mobile": mobile,
                    "appName": "Linky",
                    "deptName": "Piso",
                    "pendaftaranGroup": "Piso-66",
                }
            return None

    crm = FlakyCrmAdapter()
    client = make_client({"CRM_ADAPTER": crm})
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-retry-crm-1",
            "source_platform": "manual_cs",
            "source_campaign": "lark",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000777",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-66",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "77770011",
            "account_id_type": "platform_uid",
            "source_channel": "manual_cs_lark",
            "submitted_by": "cs_retry_crm",
            "submitted_at": "2026-04-15T11:10:00Z",
        },
    ).json()
    first = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "guild accepted",
            "finished_at": "2026-04-15T11:12:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    ).json()
    assert first['reason'] == 'crm_sync_retry_pending'
    assert first['next_action'] == 'queue_crm_sync_retry'

    retried = client.post(f"/api/ops/submissions/{submission['submission_id']}/retry-crm")
    assert retried.status_code == 200
    body = retried.json()
    assert body['accepted'] is True
    assert body['retry_type'] == 'crm'
    assert body['created_new_submission'] is False
    assert body['crm_verified'] is True
    assert body['group_join_task_id'] is not None

    timeline = client.get(f"/api/leads/{lead['lead_id']}/timeline").json()
    assert len(timeline['account_submissions']) == 1
    assert any(task['task_type'] == 'group_join' and task['task_id'] == body['group_join_task_id'] for task in timeline['tasks'])



def test_ops_resubmit_creates_new_submission_and_correction_history():
    client = make_client({'LARK_DEFAULT_APP_NAME': 'Linky', 'LARK_DEFAULT_DEPT_NAME': 'Piso'})
    first = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81234560001",
            "registration_group": "Piso-10",
            "app_name": "Linky",
            "dept_name": "Piso",
            "invite_code": "ABCDEF",
            "submission_type": "account_id",
            "account_id": "12345678",
            "submitted_by": "cs_original",
            "source_channel": "manual_cs_lark",
            "submitted_at": "2026-04-15T12:00:00Z",
            "remark": "orig",
        },
    ).json()
    resubmitted = client.post(f"/api/ops/submissions/{first['submission_id']}/resubmit", json={
        'corrected_by': 'ops_fix',
        'submitted_at': '2026-04-15T12:05:00Z',
        'mobile': '+62 81234560009',
        'registration_group': 'Piso-11',
        'invite_code': 'ZZZZZZ',
        'account_id': '87654321',
        'remark': 'fixed',
    })
    assert resubmitted.status_code == 200
    body = resubmitted.json()
    assert body['created_new_submission'] is True
    assert body['original_submission_id'] == first['submission_id']
    assert body['submission_id'] != first['submission_id']

    timeline = client.get(f"/api/leads/{body['lead_id']}/timeline").json()
    assert len(timeline['account_submissions']) == 1
    assert len(timeline['correction_history']) >= 1
    field_names = {row['field_name'] for row in timeline['correction_history']}
    assert {'mobile', 'registration_group', 'invite_code', 'account_id'} <= field_names

    original_timeline = client.get(f"/api/leads/{first['lead_id']}/timeline").json()
    assert len(original_timeline['account_submissions']) == 1



def test_ops_exception_queue_and_sla_summary_aggregate_core_failures():
    client = make_client()
    lead = client.post('/api/leads/upsert', json={
        'trace_id': 'trace-exc-1',
        'source_platform': 'manual_cs',
        'source_campaign': 'lark',
        'source_page_id': 'lark',
        'country': 'Indonesia',
        'area_code': 62,
        'mobile': '81115550001',
        'app_name': 'Linky',
        'dept_name': 'Permata',
        'pendaftaran_group': 'Permata-1',
    }).json()
    submission = client.post('/api/account-submissions', json={
        'lead_id': lead['lead_id'],
        'submission_type': 'account_id',
        'account_id': '11112222',
        'account_id_type': 'platform_uid',
        'source_channel': 'manual_cs_lark',
        'submitted_by': 'ops',
        'submitted_at': '2026-04-15T12:10:00Z',
    }).json()
    with client.app.state.service.db.connect() as conn:
        conn.execute("UPDATE leads SET current_status = ? WHERE lead_id = ?", ('bind_failed', lead['lead_id']))
        conn.execute("UPDATE automation_tasks SET status='failed', result_code=?, result_reason=?, finished_at=? WHERE task_id=?", ('bind_unauthorized', 'HTTP 401: please re-login', '2026-04-15T12:12:00Z', submission['task_id']))
        conn.execute("INSERT INTO operator_notifications (notification_id, lead_id, notification_type, mobile, yw_id, write_result, reason, is_read, read_at, read_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ('notify_exc_1', lead['lead_id'], 'crm_record_failed', '81115550001', '11112222', 'failed', 'CRM write was rejected.', 0, None, None, '2026-04-15T12:13:00Z'))
        conn.commit()
    exc = client.get('/api/ops/exception-queue')
    assert exc.status_code == 200
    exception_types = {row['exception_type'] for row in exc.json()['rows']}
    assert 'crm_failure' in exception_types
    assert 'auth_required' in exception_types or 'session_expired' in exception_types or 'bind_failure' in exception_types

    sla = client.get('/api/ops/sla-summary')
    assert sla.status_code == 200
    body = sla.json()
    assert body['submission_total'] >= 1
    assert body['failed_count'] >= 1
    assert isinstance(body['top_failure_reasons'], list)



def test_lark_event_does_not_reply_success_when_crm_create_returns_success_but_query_back_finds_nothing():
    class UnverifiableCreateCrmAdapter:
        def __init__(self):
            self.calls = []
            self.apps = [{"id": "app_1", "name": "Linky"}]
            self.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            return None
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            return {"code": 0, "msg": "success", "data": None}
        def update_customer(self, payload):
            self.calls.append(("update_customer", payload))
            return {"code": 0, "msg": "success", "data": None}
        def get_apps(self):
            return list(self.apps)
        def get_depts(self):
            return list(self.depts)

    reply = StubLarkReplyAdapter()
    crm = UnverifiableCreateCrmAdapter()
    client = make_client({
        "CRM_ADAPTER": crm,
        "LARK_APP_ID": "cli_test",
        "LARK_REPLY_ADAPTER": reply,
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
        "AUTO_BIND_SIMULATION": True,
        "BIND_SIMULATOR": lambda context: {
            "status": "success",
            "result_code": "bind_ok_simulated",
            "result_reason": "simulated bind success",
            "raw_result": {"guild_code": context["dept_name"], "deptName": context["dept_name"], "deptId": "dept_1"},
        },
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_crm_unverified'}},
            'message': {
                'message_id': 'om_text_crm_unverified',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567895\\nPiso-29\\n45678905"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'crm_sync_failed'
    assert body['result_reason'] == 'CRM write could not be verified.'
    assert reply.calls[0]['text'].startswith('**❌ CRM Failed**')


def test_lark_event_does_not_treat_mismatched_query_back_row_as_verified_success():
    class MismatchedQueryBackCrmAdapter:
        def __init__(self):
            self.calls = []
            self.apps = [{"id": "app_1", "name": "Linky"}]
            self.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            return {
                'id': 'crm_other_1',
                'ywId': yw_id,
                'mobile': mobile,
                'appName': 'FUMI',
                'deptName': 'Permata',
                'pendaftaranGroup': 'Permata-99',
            }
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            return {"code": 0, "msg": "success", "data": None}
        def update_customer(self, payload):
            self.calls.append(("update_customer", payload))
            return {"code": 0, "msg": "success", "data": None}
        def get_apps(self):
            return list(self.apps)
        def get_depts(self):
            return list(self.depts)

    reply = StubLarkReplyAdapter()
    crm = MismatchedQueryBackCrmAdapter()
    client = make_client({
        "CRM_ADAPTER": crm,
        "LARK_APP_ID": "cli_test",
        "LARK_REPLY_ADAPTER": reply,
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
        "AUTO_BIND_SIMULATION": True,
        "BIND_SIMULATOR": lambda context: {
            "status": "success",
            "result_code": "bind_ok_simulated",
            "result_reason": "simulated bind success",
            "raw_result": {"guild_code": context["dept_name"], "deptName": context["dept_name"], "deptId": "dept_1"},
        },
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_crm_mismatch'}},
            'message': {
                'message_id': 'om_text_crm_mismatch',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567896\\nPiso-30\\n45678906"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'crm_sync_failed'
    assert body['result_reason'] == 'CRM write could not be verified.'
    assert reply.calls[0]['text'].startswith('**❌ CRM Failed**')


def test_lark_event_uses_english_duplicate_conflict_message_when_crm_reports_duplicate_without_lookup_hit():
    class DuplicateConflictCrmAdapter:
        def __init__(self):
            self.calls = []
            self.record = None
            self.apps = [{"id": "app_1", "name": "Linky"}]
            self.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            return None
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            return {"code": 10002, "msg": "数据库中已存在该记录", "data": None}
        def update_customer(self, payload):
            self.calls.append(("update_customer", payload))
            return {"code": 0, "msg": "success", "data": None}
        def get_apps(self):
            return list(self.apps)
        def get_depts(self):
            return list(self.depts)

    reply = StubLarkReplyAdapter()
    crm = DuplicateConflictCrmAdapter()
    client = make_client({
        "CRM_ADAPTER": crm,
        "LARK_APP_ID": "cli_test",
        "LARK_REPLY_ADAPTER": reply,
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
        "AUTO_BIND_SIMULATION": True,
        "BIND_SIMULATOR": lambda context: {
            "status": "success",
            "result_code": "bind_ok_simulated",
            "result_reason": "simulated bind success",
            "raw_result": {"guild_code": context["dept_name"], "deptName": context["dept_name"], "deptId": "dept_1"},
        },
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_crm_dup'}},
            'message': {
                'message_id': 'om_text_crm_dup',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567891\\nPiso-26\\n45678902"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'crm_sync_failed'
    assert body['result_reason'] == 'Data duplication.'
    assert reply.calls[0]['text'].startswith('**❌ CRM Failed**')


def test_lark_event_reuses_cached_crm_app_mapping_when_get_apps_temporarily_fails():
    class FlakyAppsCrmAdapter:
        def __init__(self):
            self.calls = []
            self.record = None
            self.apps = [{"id": "app_1", "name": "Linky"}]
            self.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
            self.fail_next_get_apps = False
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            return None
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            return {"code": 0, "msg": "success", "data": None}
        def update_customer(self, payload):
            self.calls.append(("update_customer", payload))
            return {"code": 0, "msg": "success", "data": None}
        def get_apps(self):
            self.calls.append(("get_apps", {}))
            if self.fail_next_get_apps:
                self.fail_next_get_apps = False
                raise RuntimeError('CRM get_apps returned non-JSON response: status=502 body=')
            return list(self.apps)
        def get_depts(self):
            self.calls.append(("get_depts", {}))
            return list(self.depts)

    crm = FlakyAppsCrmAdapter()
    app = create_app({"DB_PATH": ":memory:", "CRM_ADAPTER": crm})
    service = app.state.service

    first = service._resolve_crm_app_mapping('Linky')
    assert first['appId'] == 'app_1'

    crm.fail_next_get_apps = True
    second = service._resolve_crm_app_mapping('Linky')
    assert second['appId'] == 'app_1'
    assert second.get('mapping_source') == 'cache'


def test_list_intake_bot_presets_warms_crm_mapping_cache_for_later_write_path():
    class FlakyPresetCrmAdapter:
        def __init__(self):
            self.calls = []
            self.apps = [{"id": "app_fumi", "appName": "FUMI"}]
            self.depts = [{"id": "dept_perm", "name": "Permata"}]
            self.fail_next_get_apps = False
            self.fail_next_get_depts = False
        def get_apps(self):
            self.calls.append(("get_apps", {}))
            if self.fail_next_get_apps:
                self.fail_next_get_apps = False
                raise RuntimeError('CRM get_apps returned non-JSON response: status=502 body=')
            return list(self.apps)
        def get_depts(self):
            self.calls.append(("get_depts", {}))
            if self.fail_next_get_depts:
                self.fail_next_get_depts = False
                raise RuntimeError('CRM get_depts returned non-JSON response: status=502 body=')
            return list(self.depts)

    app = create_app({
        "DB_PATH": ":memory:",
        "CRM_ADAPTER": FlakyPresetCrmAdapter(),
        "LARK_DEFAULT_APP_NAME": "FUMI",
        "LARK_DEFAULT_DEPT_NAME": "Permata",
    })
    service = app.state.service
    crm = service.crm_adapter

    presets = service.list_intake_bot_presets()
    assert any(item['value'] == 'FUMI' for item in presets['app_options'])
    assert any(item['value'] == 'Permata' for item in presets['guild_options'])

    crm.fail_next_get_apps = True
    crm.fail_next_get_depts = True
    app_mapping = service._resolve_crm_app_mapping('FUMI')
    dept_mapping = service._resolve_crm_dept_mapping('Permata')

    assert app_mapping['appId'] == 'app_fumi'
    assert app_mapping['mapping_source'] == 'cache'
    assert dept_mapping['deptId'] == 'dept_perm'
    assert dept_mapping['mapping_source'] == 'cache'



def test_persisted_crm_mapping_cache_survives_restart(tmp_path):
    db_path = str(tmp_path / 'crm-cache.db')

    class FirstCrmAdapter:
        def get_apps(self):
            return [{"id": "app_fumi", "appName": "FUMI"}]
        def get_depts(self):
            return [{"id": "dept_perm", "name": "Permata"}]

    first = create_app({
        "DB_PATH": db_path,
        "CRM_ADAPTER": FirstCrmAdapter(),
        "LARK_DEFAULT_APP_NAME": "FUMI",
        "LARK_DEFAULT_DEPT_NAME": "Permata",
    })
    first.state.service.list_intake_bot_presets()

    class FailingCrmAdapter:
        def get_apps(self):
            raise RuntimeError('CRM get_apps returned non-JSON response: status=502 body=')
        def get_depts(self):
            raise RuntimeError('CRM get_depts returned non-JSON response: status=502 body=')

    restarted = create_app({
        "DB_PATH": db_path,
        "CRM_ADAPTER": FailingCrmAdapter(),
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
    })
    service = restarted.state.service

    app_mapping = service._resolve_crm_app_mapping('FUMI')
    dept_mapping = service._resolve_crm_dept_mapping('Permata')

    assert app_mapping['appId'] == 'app_fumi'
    assert app_mapping['mapping_source'] == 'cache'
    assert dept_mapping['deptId'] == 'dept_perm'
    assert dept_mapping['mapping_source'] == 'cache'



def test_lark_event_replies_in_english_retry_once_when_crm_app_mapping_is_temporarily_unavailable():
    class AlwaysFailAppsCrmAdapter:
        def __init__(self):
            self.calls = []
            self.record = None
            self.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            return None
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            return {"code": 0, "msg": "success", "data": None}
        def update_customer(self, payload):
            self.calls.append(("update_customer", payload))
            return {"code": 0, "msg": "success", "data": None}
        def get_apps(self):
            self.calls.append(("get_apps", {}))
            raise RuntimeError('CRM get_apps returned non-JSON response: status=502 body=')
        def get_depts(self):
            self.calls.append(("get_depts", {}))
            return list(self.depts)

    reply = StubLarkReplyAdapter()
    crm = AlwaysFailAppsCrmAdapter()
    client = make_client({
        "CRM_ADAPTER": crm,
        "LARK_APP_ID": "cli_test",
        "LARK_REPLY_ADAPTER": reply,
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
        "AUTO_BIND_SIMULATION": True,
        "BIND_SIMULATOR": lambda context: {
            "status": "success",
            "result_code": "bind_ok_simulated",
            "result_reason": "simulated bind success",
            "raw_result": {"guild_code": context["dept_name"], "deptName": context["dept_name"], "deptId": "dept_1"},
        },
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_crm_app_retry'}},
            'message': {
                'message_id': 'om_text_crm_app_retry',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567892\\nPiso-27\\n45678903"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'crm_sync_failed'
    assert body['result_reason'] == 'Please retry once.'
    assert reply.calls[0]['text'].startswith('**❌ CRM Failed**')
    assert not [name for name, _ in crm.calls if name == 'create_customer']


def test_lark_event_can_auto_simulate_bind_failure_and_reply_reason():
    reply = StubLarkReplyAdapter()
    client = make_client({
        "LARK_APP_ID": "cli_test",
        "LARK_REPLY_ADAPTER": reply,
        "LARK_DEFAULT_APP_NAME": "Linky",
        "LARK_DEFAULT_DEPT_NAME": "Piso",
        "AUTO_BIND_SIMULATION": True,
        "BIND_SIMULATOR": lambda context: {
            "status": "failed",
            "result_code": "already_joined_other_guild",
            "result_reason": "already joined another guild",
            "raw_result": {"guild_code": context["dept_name"]},
        },
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_sim_1'}},
            'message': {
                'message_id': 'om_text_sim_fail_1',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"手机号 +62 81234567890\\n注册群组 Piso-25\\nID 55667788\\nCode EKVFGQ"}'
            }
        }
    })

    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'simulated_bind_failed'
    assert body['simulation_applied'] is True
    assert body['lead_status'] == 'bind_failed'
    assert body['next_action'] == 'queue_reengagement'
    assert reply.calls[0]['text'] == (
        '**❌ Bind failed: already joined another guild**\n'
        'Phone: +62 81234567890\n'
        'ID: 55667788\n'
        'Group: Piso-25\n'
        'Code: EKVFGQ'
    )

    translated = client.app.state.service._format_lark_reply_text({
        'accepted': False,
        'reason': 'bind_check_failed',
        'result_reason': 'HTTP 400: {"error":{"code":-1,"message":"The streamer was in other guild "}}',
        'reply_phone': '+62 81234567890',
        'reply_id': '55667788',
        'reply_group': 'Piso-25',
        'reply_code': 'EKVFGQ',
    })
    assert translated == (
        '**❌ Bind failed: The streamer was in another agency**\n'
        'Phone: +62 81234567890\n'
        'ID: 55667788\n'
        'Group: Piso-25\n'
        'Code: EKVFGQ'
    )



def test_manual_cs_submission_returns_parser_conflicts_and_routes_to_manual_review():
    client = make_client()

    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "081234567893",
            "registration_group": "Piso-18",
            "app_name": "Linky",
            "dept_name": "Permata",
            "submission_type": "screenshot",
            "account_id": None,
            "file_url": "https://cdn.example.com/ocr-shot.png",
            "file_type": "image/png",
            "submitted_by": "dewi02",
            "source_channel": "manual_cs_lark",
            "remark": "手机号 081234567893，Linky，公会Permata，注册群组 Piso-18，ID 88888888",
            "submitted_at": "2026-04-14T18:05:00Z",
            "image_ocr_text": "UID 99999999\nGroup Piso-18",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["next_action"] == "manual_review"
    assert body["routing_decision"] == "manual_review"
    assert "account_id_conflict" in body["parsed_payload"]["conflicts"]
    assert body["parsed_payload"]["account_id"] == "99999999"
    assert body["parsed_payload"]["dept_name"] == "Permata"

    timeline = client.get(f"/api/leads/{body['lead_id']}/timeline")
    assert timeline.status_code == 200
    lead = timeline.json()["lead"]
    assert lead["parser_confidence"] > 0
    assert "account_id_conflict" in lead["parser_conflicts"]
    assert "account_id_conflict" in lead["review_reason_codes"]
    assert lead["routing_decision"] == "manual_review"
    assert lead["parser_missing_fields"] == ["invite_code"]
    assert "UID 99999999" in lead["parser_raw_ocr_text"]



def test_ops_bind_queue_exposes_parser_summary_fields():
    client = make_client()
    body = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "081234567894",
            "registration_group": "Piso-19",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "screenshot",
            "file_url": "https://cdn.example.com/ocr-shot-2.png",
            "file_type": "image/png",
            "submitted_by": "dewi03",
            "source_channel": "manual_cs_lark",
            "remark": "Linky，Piso组，截图里有ID",
            "submitted_at": "2026-04-14T18:06:00Z",
            "image_ocr_text": "UID 77778888\nGroup Piso-19",
        },
    ).json()

    queue = client.get('/api/ops/bind-queue')
    assert queue.status_code == 200
    row = next(r for r in queue.json()['rows'] if r['lead_id'] == body['lead_id'])
    assert row['parser_confidence'] > 0
    assert row['parser_missing_fields'] == ['invite_code']
    assert row['parser_conflicts'] == []
    assert row['current_status'] == 'recognition_pending'


def test_registration_group_batching_ready_when_reaches_30():
    client = make_client()
    response = client.post(
        "/api/ops/approval-batches/evaluate",
        json={
            "approval_type": "registration_group",
            "registration_group": "Piso-30",
            "pending_count": 30,
            "oldest_pending_at": "2026-04-15T10:00:00Z",
            "now": "2026-04-15T10:19:00Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["release_count"] == 30
    assert body["reason_code"] == "batch_size_reached"



def test_registration_group_batching_flushes_after_30_minutes_even_if_under_30():
    client = make_client()
    response = client.post(
        "/api/ops/approval-batches/evaluate",
        json={
            "approval_type": "registration_group",
            "registration_group": "Piso-31",
            "pending_count": 12,
            "oldest_pending_at": "2026-04-15T10:00:00Z",
            "now": "2026-04-15T10:31:00Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["release_count"] == 12
    assert body["reason_code"] == "timeout_flush"



def test_official_group_batching_ready_when_reaches_10():
    client = make_client()
    response = client.post(
        "/api/ops/approval-batches/evaluate",
        json={
            "approval_type": "official_group",
            "registration_group": "Official-A",
            "pending_count": 10,
            "oldest_pending_at": "2026-04-15T10:00:00Z",
            "now": "2026-04-15T10:15:00Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["release_count"] == 10
    assert body["reason_code"] == "batch_size_reached"



def test_official_group_batching_flushes_after_30_minutes_even_if_under_10():
    client = make_client()
    response = client.post(
        "/api/ops/approval-batches/evaluate",
        json={
            "approval_type": "official_group",
            "registration_group": "Official-B",
            "pending_count": 4,
            "oldest_pending_at": "2026-04-15T10:00:00Z",
            "now": "2026-04-15T10:31:00Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["release_count"] == 4
    assert body["reason_code"] == "timeout_flush"



def test_approval_batching_not_ready_before_threshold_or_timeout():
    client = make_client()
    response = client.post(
        "/api/ops/approval-batches/evaluate",
        json={
            "approval_type": "official_group",
            "registration_group": "Official-C",
            "pending_count": 3,
            "oldest_pending_at": "2026-04-15T10:00:00Z",
            "now": "2026-04-15T10:20:00Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["release_count"] == 0
    assert body["reason_code"] == "waiting_for_batch"



def test_ops_approval_batch_queue_returns_ready_and_waiting_groups():
    client = make_client()
    for idx in range(30):
        client.post(
            "/api/leads/upsert",
            json={
                "trace_id": f"trace-batch-old-{idx}",
                "source_platform": "manual_cs",
                "source_page_id": "lark",
                "country": "Indonesia",
                "area_code": 62,
                "mobile": f"8999900{idx:04d}",
                "app_name": "Linky",
                "dept_name": "Piso",
                "pendaftaran_group": "Piso-30",
            },
        )
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-batch-official-old",
            "source_platform": "manual_cs",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999111111",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Official-A",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "90909090",
            "account_id_type": "platform_uid",
            "source_channel": "manual_cs_lark",
            "submitted_by": "ops_batch_old",
            "submitted_at": "2026-04-15T11:00:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "bind success",
            "finished_at": "2026-04-15T11:01:00Z",
            "raw_result": {"guild_code": "Piso"},
        },
    )

    response = client.get('/api/ops/approval-batch-queue')
    assert response.status_code == 200
    body = response.json()
    registration = next(row for row in body['registration_groups'] if row['registration_group'] == 'Piso-30')
    assert registration['ready'] is True
    assert registration['pending_count'] == 30
    assert registration['reason_code'] == 'batch_size_reached'
    official = next(row for row in body['official_groups'] if row['registration_group'] == 'Official-A')
    assert official['pending_count'] == 1



def test_official_group_batch_queue_uses_real_group_request_list_instead_of_leads_aggregate(monkeypatch):
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-official-runtime-queue",
            "source_platform": "manual_cs",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999222222",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Official-A",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "91919191",
            "account_id_type": "platform_uid",
            "source_channel": "manual_cs_lark",
            "submitted_by": "ops_official_queue",
            "submitted_at": "2026-04-15T11:00:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "bind success",
            "finished_at": "2026-04-15T11:01:00Z",
            "raw_result": {"guild_code": "Piso"},
        },
    )
    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-official-runtime-1', json={
        'account_name': 'WA Official Runtime 1',
        'responsible_type': 'official_group',
        'group_link_bindings': [
            {
                'link': 'https://chat.whatsapp.com/official-group-a',
                'group_name': '官方群01',
                'area': 'Indonesia',
                'notify_profile_name': 'wa-approval-broadcast',
                'approval_count_threshold': 10,
                'approval_timeout_minutes': 10,
                'auto_recover_worker': True,
                'schedule_windows': [],
            },
        ],
        'notify_profile_name': 'wa-approval-broadcast',
        'approval_count_threshold': 10,
        'approval_timeout_minutes': 10,
        'auto_recover_worker': True,
        'schedule_windows': [],
        'enabled': True,
        'notes': 'official runtime queue test',
    })
    assert saved.status_code == 200

    service = client.app.state.service
    monkeypatch.setattr(service, '_build_whatsapp_approval_runtime_state', lambda account_key, **kwargs: {
        'account_key': account_key,
        'active': True,
        'base_url': 'http://127.0.0.1:53637',
    })
    monkeypatch.setattr(service, '_request_whatsapp_approval_group_state', lambda base_url, registration_group: {
        'group_id': '120363400000000001@g.us',
        'group_name': '官方群01',
        'pending_count': 2,
        'member_count': 33,
        'requesters': [
            {'requesterId': 'r1', 'requestedAtIso': '2026-04-15T10:00:00Z'},
            {'requesterId': 'r2', 'requestedAtIso': '2026-04-15T10:05:00Z'},
        ],
    })

    response = client.get('/api/ops/approval-batch-queue')
    assert response.status_code == 200
    body = response.json()
    official = next(row for row in body['official_groups'] if row['registration_group'] == '官方群01')
    assert official['pending_count'] == 2
    assert official['source'] == 'official_runtime_group_state'
    assert official['group_name'] == '官方群01'
    assert official['target_group'] == 'https://chat.whatsapp.com/official-group-a'



def test_ops_page_includes_approval_batch_queue_section():
    client = make_client()
    response = client.get('/ops')
    assert response.status_code == 200
    body = response.text
    assert '/api/ops/approval-batch-queue' in body
    assert '审批批次队列' in body
    assert '注册群批次' in body
    assert '官方群批次' in body



def test_manual_cs_submission_rejects_account_id_type_without_account_id():
    client = make_client()

    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81234567891",
            "registration_group": "Piso-5",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "account_id",
            "submitted_by": "dewi01",
            "source_channel": "manual_cs_lark",
            "submitted_at": "2026-04-14T18:01:00Z",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "account_id is required when submission_type=account_id"



def test_manual_cs_submission_rejects_screenshot_without_file_url():
    client = make_client()

    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81234567892",
            "registration_group": "Piso-5",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "screenshot",
            "submitted_by": "dewi01",
            "source_channel": "manual_cs_lark",
            "submitted_at": "2026-04-14T18:02:00Z",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "file_url is required when submission_type=screenshot"


def test_manual_cs_submission_rejects_screenshot_without_file_type():
    client = make_client()

    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "+62 81234567893",
            "registration_group": "Piso-5",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "screenshot",
            "file_url": "https://cdn.example.com/shot.png",
            "submitted_by": "dewi01",
            "source_channel": "manual_cs_lark",
            "submitted_at": "2026-04-14T18:03:00Z",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "file_type is required when submission_type=screenshot"


def test_manual_cs_submission_rejects_blank_required_fields():
    client = make_client()

    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "   ",
            "registration_group": "",
            "app_name": "",
            "dept_name": "",
            "submission_type": "account_id",
            "account_id": "12345678",
            "submitted_by": "",
            "source_channel": "manual_cs_lark",
            "submitted_at": "",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "mobile, registration_group, app_name, dept_name, submitted_by, submitted_at are required"

def test_account_submission_numeric_id_queues_bind_check():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-6",
            "source_platform": "meta",
            "source_page_id": "page-6",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "85555555555",
        },
    ).json()

    response = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "45772164",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:00:00Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["normalized_account_id"] == "45772164"
    assert body["next_action"] == "queue_bind_check"
    assert body["task_type"] == "bind_check"
    assert body["recognition_status"] == "not_needed"


def test_account_submission_screenshot_queues_recognition():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-7",
            "source_platform": "meta",
            "source_page_id": "page-7",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "86666666666",
        },
    ).json()

    response = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "screenshot",
            "file_url": "https://cdn.example.com/account-shot/abc.png",
            "file_type": "image/png",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:03:00Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["next_action"] == "queue_account_recognition"
    assert body["task_type"] == "account_recognition"
    assert body["recognition_status"] == "pending"


def test_recognition_result_success_promotes_submission_and_queues_bind_check():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-8",
            "source_platform": "meta",
            "source_page_id": "page-8",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "87777777777",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "screenshot",
            "file_url": "https://cdn.example.com/account-shot/ocr.png",
            "file_type": "image/png",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:03:00Z",
        },
    ).json()

    response = client.post(
        f"/api/tasks/{submission['task_id']}/recognition-result",
        json={
            "status": "success",
            "recognized_account_id": "77889911",
            "result_code": "recognized",
            "result_reason": "ocr success",
            "finished_at": "2026-04-14T12:05:00Z",
            "raw_result": {"confidence": 0.98},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["lead_status"] == "account_submitted"
    assert body["next_action"] == "queue_bind_check"
    assert body["bind_task_type"] == "bind_check"
    assert body["recognized_account_id"] == "77889911"


def test_bind_check_result_success_promotes_to_group_join_pending():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-9",
            "source_platform": "meta",
            "source_page_id": "page-9",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "88888888888",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "55667788",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:10:00Z",
        },
    ).json()

    response = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:12:00Z",
            "raw_result": {"guild_code": "MCN-11"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["lead_status"] == "bind_success"
    assert body["next_action"] == "queue_group_join"
    assert body["group_join_task_type"] == "group_join"


def test_group_join_result_success_closes_group_join_flow():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-10",
            "source_platform": "meta",
            "source_page_id": "page-10",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999999999",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "66778899",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:15:00Z",
        },
    ).json()
    bind_result = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:17:00Z",
            "raw_result": {"guild_code": "MCN-11"},
        },
    ).json()

    response = client.post(
        f"/api/tasks/{bind_result['group_join_task_id']}/group-join-result",
        json={
            "status": "success",
            "result_code": "join_ok",
            "result_reason": "joined official group",
            "finished_at": "2026-04-14T12:18:00Z",
            "raw_result": {"target_group": "official-group-a"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["lead_status"] == "group_join_success"
    assert body["next_action"] == "close_or_education"



def test_official_group_approval_decision_executes_executor_and_closes_group_join_flow():
    from app.main import create_app

    class StubOfficialGroupApprovalExecutor:
        def __init__(self):
            self.calls = []

        def approve(self, *, target_group, lead, crm_snapshot, task):
            self.calls.append({
                "target_group": target_group,
                "lead_id": lead.get("lead_id"),
                "crm_snapshot": crm_snapshot,
                "task_id": task.get("task_id"),
            })
            return {
                "status": "success",
                "result_code": "approval_ok",
                "result_reason": "approved by automation executor",
                "raw_result": {
                    "target_group": target_group,
                    "executor": "stub_official_group_executor",
                },
            }

    crm = StubCrmAdapter()
    crm.record = {
        "id": "crm_decision_1",
        "mobile": "89999999995",
        "ywId": "66778895",
        "appId": "app_1",
        "appName": "Linky",
        "deptId": "dept_1",
        "deptName": "Piso",
        "pendaftaranGroup": "Piso-5",
        "wa": "",
        "joinGroup": 0,
    }
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    executor = StubOfficialGroupApprovalExecutor()
    app = create_app({
        "DB_PATH": ":memory:",
        "CRM_ADAPTER": crm,
        "OFFICIAL_GROUP_APPROVAL_EXECUTOR": executor,
    })
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-official-decision",
            "source_platform": "meta",
            "source_page_id": "page-official-decision",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999999995",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "66778895",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:15:00Z",
        },
    ).json()
    bind_result = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:17:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    ).json()

    response = client.post(
        "/api/official-groups/approval-decisions",
        json={
            "lead_id": lead["lead_id"],
            "target_group": "official-group-a",
            "decision": "approve",
            "decided_at": "2026-04-14T12:18:00Z",
            "decided_by": "operator_1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["executed"] is True
    assert body["eligible"] is True
    assert body["task_id"] == bind_result["group_join_task_id"]
    assert body["decision_result"]["lead_status"] == "group_join_success"
    assert executor.calls[0]["task_id"] == bind_result["group_join_task_id"]
    assert executor.calls[0]["target_group"] == "official-group-a"

    timeline = client.get(f"/api/leads/{lead['lead_id']}/timeline").json()
    group_sync = [row for row in timeline['sync_logs'] if row['sync_type'] == 'official_group_update']
    assert group_sync[-1]['status'] == 'success'
    audit_rows = client.get('/api/ops/operator-audit-log').json()['rows']
    assert any(row['event_type'] == 'official_group_approval_decision_executed' for row in audit_rows)



def test_official_group_approval_decision_prefers_matching_account_runtime_executor():
    from app.main import create_app

    class StubOfficialGroupApprovalExecutor:
        def __init__(self):
            self.calls = []

        def approve(self, *, target_group, lead, crm_snapshot, task):
            self.calls.append({"target_group": target_group, "lead_id": lead.get("lead_id")})
            return {"status": "success", "result_code": "approval_ok", "result_reason": "ok", "raw_result": {"target_group": target_group, "executor": "fallback"}}

    crm = StubCrmAdapter()
    crm.record = {
        "id": "crm_decision_runtime_1",
        "mobile": "89999999993",
        "ywId": "66778893",
        "appId": "app_1",
        "appName": "Linky",
        "deptId": "dept_1",
        "deptName": "Piso",
        "pendaftaranGroup": "Piso-5",
        "wa": "",
        "joinGroup": 0,
    }
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    fallback_executor = StubOfficialGroupApprovalExecutor()
    runtime_executor = StubRegistrationGroupApprovalExecutor({
        "status": "success",
        "verified": True,
        "result_code": "approval_ok",
        "result_reason": "approved via dedicated runtime",
        "approved_count": 1,
        "target_member": {"name": "runtime-member", "phone_raw": "89999999993"},
        "raw_result": {"executor": "runtime"},
    }, group_state_result={
        "group_name": "official-group-runtime",
        "group_id": "official-runtime-group-id",
        "pending_count": 1,
        "member_count": 9,
        "requester_ids": ["official-req@lid"],
        "requesters": [{"requesterId": "official-req@lid", "requestedAtUnix": 100}],
    })
    app = create_app({
        "DB_PATH": ":memory:",
        "CRM_ADAPTER": crm,
        "OFFICIAL_GROUP_APPROVAL_EXECUTOR": fallback_executor,
    })
    client = TestClient(app)

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-official-runtime', json={
        'account_name': 'WA Official Runtime',
        'responsible_type': 'official_group',
        'group_link_bindings': [{
            'link': 'https://chat.whatsapp.com/official-runtime',
            'area': 'Indonesia',
            'notify_profile_name': 'wa-approval-broadcast',
            'registration_group': 'official-group-runtime',
            'group_id': 'official-runtime-group-id',
            'approval_count_threshold': 30,
            'approval_timeout_minutes': 30,
            'auto_recover_worker': True,
            'schedule_windows': [{'start': '00:00', 'end': '23:59'}],
        }],
        'enabled': True,
        'notes': 'official runtime route',
    })
    assert saved.status_code == 200

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-official-runtime",
            "source_platform": "meta",
            "source_page_id": "page-official-runtime",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999999993",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "66778893",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:15:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:17:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    ).json()

    def fake_runtime_state(self, account_key, *, worker_health=None, allow_shared_fallback=True):
        if account_key == 'wa-official-runtime':
            return {
                'account_key': account_key,
                'active': True,
                'base_url': 'http://127.0.0.1:18889',
                'source': 'dedicated',
                'status': 'warm',
                'ready': True,
                'authenticated': True,
                'session_target_match': True,
                'status_text': 'dedicated runtime ready',
            }
        return {'account_key': account_key, 'active': False, 'base_url': None, 'source': 'shared', 'status': 'shared'}

    with patch('app.main.Service._build_whatsapp_approval_runtime_state', new=fake_runtime_state), patch('app.main.Service._build_runtime_registration_group_executor', return_value=runtime_executor):
        response = client.post(
            "/api/official-groups/approval-decisions",
            json={
                "lead_id": lead["lead_id"],
                "target_group": "official-group-runtime",
                "decision": "approve",
                "decided_at": "2026-04-14T12:18:00Z",
                "decided_by": "operator_1",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body['executed'] is True
    assert runtime_executor.calls[0]['registration_group'] == 'official-group-runtime'
    assert fallback_executor.calls == []


def test_official_group_approval_decision_skips_executor_when_not_eligible():
    from app.main import create_app

    class StubOfficialGroupApprovalExecutor:
        def __init__(self):
            self.calls = []

        def approve(self, *, target_group, lead, crm_snapshot, task):
            self.calls.append({"target_group": target_group, "lead_id": lead.get("lead_id")})
            return {"status": "success", "result_code": "approval_ok", "result_reason": "ok", "raw_result": {"target_group": target_group}}

    crm = StubCrmAdapter()
    crm.record = {
        "id": "crm_decision_2",
        "mobile": "89999999994",
        "ywId": "66778894",
        "appId": "app_1",
        "appName": "Linky",
        "deptId": "dept_1",
        "deptName": "Piso",
        "pendaftaranGroup": "Piso-5",
        "wa": "official-group-a",
        "joinGroup": 1,
    }
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    executor = StubOfficialGroupApprovalExecutor()
    app = create_app({
        "DB_PATH": ":memory:",
        "CRM_ADAPTER": crm,
        "OFFICIAL_GROUP_APPROVAL_EXECUTOR": executor,
    })
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-official-skip",
            "source_platform": "meta",
            "source_page_id": "page-official-skip",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999999994",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "66778894",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:15:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:17:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    )
    crm.record["wa"] = "official-group-a"
    crm.record["joinGroup"] = 1

    response = client.post(
        "/api/official-groups/approval-decisions",
        json={
            "lead_id": lead["lead_id"],
            "target_group": "official-group-a",
            "decision": "approve",
            "decided_at": "2026-04-14T12:18:00Z",
            "decided_by": "operator_1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["executed"] is False
    assert body["eligible"] is False
    assert body["reason_code"] == "already_in_target_group"
    assert executor.calls == []



def test_official_group_approval_decision_marks_retryable_follow_up_when_executor_requests_retry():
    from app.main import create_app

    class StubOfficialGroupApprovalExecutor:
        def approve(self, *, target_group, lead, crm_snapshot, task):
            return {
                "status": "failed",
                "result_code": "upstream_timeout",
                "result_reason": "bridge timeout",
                "raw_result": {
                    "target_group": target_group,
                    "execution_disposition": "retryable_failed",
                    "retryable": True,
                },
            }

    crm = StubCrmAdapter()
    crm.record = {
        "id": "crm_decision_retry",
        "mobile": "89999999989",
        "ywId": "66778889",
        "appId": "app_1",
        "appName": "Linky",
        "deptId": "dept_1",
        "deptName": "Piso",
        "pendaftaranGroup": "Piso-5",
        "wa": "",
        "joinGroup": 0,
    }
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    app = create_app({
        "DB_PATH": ":memory:",
        "CRM_ADAPTER": crm,
        "OFFICIAL_GROUP_APPROVAL_EXECUTOR": StubOfficialGroupApprovalExecutor(),
    })
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-official-retry",
            "source_platform": "meta",
            "source_page_id": "page-official-retry",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999999989",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "66778889",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:15:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:17:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    )

    response = client.post(
        "/api/official-groups/approval-decisions",
        json={
            "lead_id": lead["lead_id"],
            "target_group": "official-group-a",
            "decision": "approve",
            "decided_at": "2026-04-14T12:18:00Z",
            "decided_by": "operator_1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["executed"] is True
    assert body["follow_up_action"] == "retry_official_group_approval"
    assert body["retryable"] is True
    assert body["requires_human_action"] is False
    assert body["decision_result"]["lead_status"] == "group_join_failed"



def test_official_group_approval_decision_marks_manual_follow_up_when_executor_requires_human_action():
    from app.main import create_app

    class StubOfficialGroupApprovalExecutor:
        def approve(self, *, target_group, lead, crm_snapshot, task):
            return {
                "status": "failed",
                "result_code": "captcha_required",
                "result_reason": "captcha required",
                "raw_result": {
                    "target_group": target_group,
                    "execution_disposition": "manual_required",
                    "requires_human_action": True,
                },
            }

    crm = StubCrmAdapter()
    crm.record = {
        "id": "crm_decision_manual",
        "mobile": "89999999988",
        "ywId": "66778888",
        "appId": "app_1",
        "appName": "Linky",
        "deptId": "dept_1",
        "deptName": "Piso",
        "pendaftaranGroup": "Piso-5",
        "wa": "",
        "joinGroup": 0,
    }
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    app = create_app({
        "DB_PATH": ":memory:",
        "CRM_ADAPTER": crm,
        "OFFICIAL_GROUP_APPROVAL_EXECUTOR": StubOfficialGroupApprovalExecutor(),
    })
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-official-manual",
            "source_platform": "meta",
            "source_page_id": "page-official-manual",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999999988",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "66778888",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:15:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:17:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    )

    response = client.post(
        "/api/official-groups/approval-decisions",
        json={
            "lead_id": lead["lead_id"],
            "target_group": "official-group-a",
            "decision": "approve",
            "decided_at": "2026-04-14T12:18:00Z",
            "decided_by": "operator_1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["executed"] is True
    assert body["follow_up_action"] == "manual_continue_official_group_approval"
    assert body["retryable"] is False
    assert body["requires_human_action"] is True
    assert body["human_action_type"] == "captcha_required"
    assert body["decision_result"]["lead_status"] == "group_join_failed"



def test_official_group_approval_executor_health_reports_configured_executor():
    from app.main import create_app

    class StubOfficialGroupApprovalExecutor:
        def health(self):
            return {
                "status": "healthy",
                "provider": "stub-whatsapp",
                "supports": ["approve"],
            }

    app = create_app({
        "DB_PATH": ":memory:",
        "OFFICIAL_GROUP_APPROVAL_EXECUTOR": StubOfficialGroupApprovalExecutor(),
    })
    client = TestClient(app)

    response = client.get('/api/ops/official-group-approval-executor-health')
    assert response.status_code == 200
    body = response.json()
    assert body['configured'] is True
    assert body['status'] == 'healthy'
    assert body['provider'] == 'stub-whatsapp'
    assert body['supports'] == ['approve']



def test_runtime_health_includes_official_group_approval_executor_snapshot():
    from app.main import create_app

    class StubOfficialGroupApprovalExecutor:
        def health(self):
            return {
                'status': 'healthy',
                'provider': 'stub-whatsapp',
                'supports': ['approve'],
                'schema_version': 'official-group-webhook-v1',
            }

    app = create_app({
        'DB_PATH': ':memory:',
        'OFFICIAL_GROUP_APPROVAL_EXECUTOR': StubOfficialGroupApprovalExecutor(),
    })
    client = TestClient(app)

    health = client.get('/api/ops/runtime-health')
    assert health.status_code == 200
    body = health.json()
    assert body['official_group_approval']['configured'] is True
    assert body['official_group_approval']['provider'] == 'stub-whatsapp'
    assert body['official_group_approval']['schema_version'] == 'official-group-webhook-v1'



def test_official_group_approval_summary_counts_pending_approved_and_skipped_duplicates():
    from app.main import create_app

    class StubOfficialGroupApprovalExecutor:
        def approve(self, *, target_group, lead, crm_snapshot, task):
            mobile = str(lead.get('mobile') or '')
            if mobile == '89999999990':
                return {
                    "status": "failed",
                    "result_code": "upstream_timeout",
                    "result_reason": "bridge timeout",
                    "raw_result": {
                        "target_group": target_group,
                        "execution_disposition": "retryable_failed",
                        "retryable": True,
                    },
                }
            if mobile == '89999999987':
                return {
                    "status": "failed",
                    "result_code": "captcha_required",
                    "result_reason": "captcha required",
                    "raw_result": {
                        "target_group": target_group,
                        "execution_disposition": "manual_required",
                        "requires_human_action": True,
                    },
                }
            return {
                "status": "success",
                "result_code": "approval_ok",
                "result_reason": "approved by automation executor",
                "raw_result": {
                    "target_group": target_group,
                },
            }

    crm = StubCrmAdapter()
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    app = create_app({
        "DB_PATH": ":memory:",
        "CRM_ADAPTER": crm,
        "OFFICIAL_GROUP_APPROVAL_EXECUTOR": StubOfficialGroupApprovalExecutor(),
    })
    client = TestClient(app)

    def create_bound_lead(trace_id: str, mobile: str, account_id: str, *, current_wa: str = ""):
        crm.record = {
            "id": f"crm_{account_id}",
            "mobile": mobile,
            "ywId": account_id,
            "appId": "app_1",
            "appName": "Linky",
            "deptId": "dept_1",
            "deptName": "Piso",
            "pendaftaranGroup": "Piso-5",
            "wa": current_wa,
            "joinGroup": 1 if current_wa else 0,
        }
        lead = client.post(
            "/api/leads/upsert",
            json={
                "trace_id": trace_id,
                "source_platform": "meta",
                "source_page_id": f"page-{trace_id}",
                "country": "Indonesia",
                "area_code": 62,
                "mobile": mobile,
                "app_name": "Linky",
                "dept_name": "Piso",
                "pendaftaran_group": "Piso-5",
            },
        ).json()
        submission = client.post(
            "/api/account-submissions",
            json={
                "lead_id": lead["lead_id"],
                "submission_type": "account_id",
                "account_id": account_id,
                "account_id_type": "platform_uid",
                "source_channel": "whatsapp",
                "submitted_by": "customer_service",
                "submitted_at": "2026-04-14T12:15:00Z",
            },
        ).json()
        bind_result = client.post(
            f"/api/tasks/{submission['task_id']}/bind-check-result",
            json={
                "status": "success",
                "result_code": "bind_ok",
                "result_reason": "manual backend bind success",
                "finished_at": "2026-04-14T12:17:00Z",
                "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
            },
        ).json()
        return lead, bind_result

    create_bound_lead('trace-og-pending', '89999999993', '66778893')

    lead_approved, _ = create_bound_lead('trace-og-approved', '89999999992', '66778892')
    client.post(
        "/api/official-groups/approval-decisions",
        json={
            "lead_id": lead_approved['lead_id'],
            "target_group": "official-group-a",
            "decision": "approve",
            "decided_at": "2026-04-14T12:18:00Z",
            "decided_by": "operator_1",
        },
    )

    lead_skipped, _ = create_bound_lead('trace-og-skipped', '89999999991', '66778891', current_wa='official-group-a')
    crm.record['wa'] = 'official-group-a'
    crm.record['joinGroup'] = 1
    client.post(
        "/api/official-groups/approval-decisions",
        json={
            "lead_id": lead_skipped['lead_id'],
            "target_group": "official-group-a",
            "decision": "approve",
            "decided_at": "2026-04-14T12:18:00Z",
            "decided_by": "operator_1",
        },
    )

    lead_retry, _ = create_bound_lead('trace-og-retry', '89999999990', '66778890')
    client.post(
        "/api/official-groups/approval-decisions",
        json={
            "lead_id": lead_retry['lead_id'],
            "target_group": "official-group-a",
            "decision": "approve",
            "decided_at": "2026-04-14T12:18:00Z",
            "decided_by": "operator_1",
        },
    )

    lead_manual, _ = create_bound_lead('trace-og-manual', '89999999987', '66778887')
    client.post(
        "/api/official-groups/approval-decisions",
        json={
            "lead_id": lead_manual['lead_id'],
            "target_group": "official-group-a",
            "decision": "approve",
            "decided_at": "2026-04-14T12:18:00Z",
            "decided_by": "operator_1",
        },
    )

    response = client.get('/api/ops/official-group-approval-summary')
    assert response.status_code == 200
    body = response.json()
    assert body['pending_count'] >= 1
    assert body['approved_count'] >= 1
    assert body['skipped_duplicate_count'] >= 1
    assert body['retryable_failed_count'] >= 1
    assert body['manual_required_count'] >= 1
    assert body['by_target_group']['official-group-a']['approved_count'] >= 1



def test_run_ready_official_group_batches_executes_ready_leads_using_target_map():
    from app.main import create_app

    class StubOfficialGroupApprovalExecutor:
        def __init__(self):
            self.calls = []

        def approve(self, *, target_group, lead, crm_snapshot, task):
            self.calls.append({'target_group': target_group, 'lead_id': lead.get('lead_id')})
            return {
                'status': 'success',
                'result_code': 'approval_ok',
                'result_reason': 'approved',
                'raw_result': {'target_group': target_group},
            }

    class MultiRecordCrmAdapter(StubCrmAdapter):
        def __init__(self):
            super().__init__()
            self.records = {}
            self._seed_record = None

        def seed(self, record):
            self._seed_record = dict(record)
            self.record = dict(record)
            for key in (record.get('ywId'), record.get('mobile')):
                normalized = str(key or '').strip()
                if normalized:
                    self.records[normalized] = dict(record)

        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            key = str(yw_id or '').strip() or str(mobile or '').strip()
            if key and key in self.records:
                return dict(self.records[key])
            if self._seed_record is None:
                return None
            return dict(self._seed_record)

        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            record = {"id": f"crm_{payload.get('ywId') or payload.get('mobile')}", **payload}
            for key in (payload.get('ywId'), payload.get('mobile')):
                normalized = str(key or '').strip()
                if normalized:
                    self.records[normalized] = dict(record)
            self.record = record
            return {"code": 0, "msg": "success", "data": None}

        def update_customer(self, payload):
            self.calls.append(("update_customer", payload))
            record = dict(payload)
            for key in (payload.get('ywId'), payload.get('mobile')):
                normalized = str(key or '').strip()
                if normalized:
                    self.records[normalized] = dict(record)
            self.record = record
            return {"code": 0, "msg": "success", "data": None}

    crm = MultiRecordCrmAdapter()
    crm.apps = [{'id': 'app_1', 'name': 'Linky'}]
    crm.depts = [{'deptId': 'dept_1', 'deptName': 'Piso'}]
    executor = StubOfficialGroupApprovalExecutor()
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'OFFICIAL_GROUP_APPROVAL_EXECUTOR': executor,
        'OFFICIAL_GROUP_TARGET_MAP': {'registration_group_prefix:piso': 'official-group-a'},
    })
    client = TestClient(app)

    for idx in range(10):
        mobile = f'899999991{idx:02d}'
        account_id = f'700010{idx:02d}'
        crm.seed({
            'id': f'crm_{account_id}',
            'mobile': mobile,
            'ywId': account_id,
            'appId': 'app_1',
            'appName': 'Linky',
            'deptId': 'dept_1',
            'deptName': 'Piso',
            'pendaftaranGroup': 'Piso-5',
            'wa': '',
            'joinGroup': 0,
        })
        lead = client.post('/api/leads/upsert', json={
            'trace_id': f'trace-batch-{idx}',
            'source_platform': 'meta',
            'source_page_id': f'page-batch-{idx}',
            'country': 'Indonesia',
            'area_code': 62,
            'mobile': mobile,
            'app_name': 'Linky',
            'dept_name': 'Piso',
            'pendaftaran_group': 'Piso-5',
        }).json()
        submission = client.post('/api/account-submissions', json={
            'lead_id': lead['lead_id'],
            'submission_type': 'account_id',
            'account_id': account_id,
            'account_id_type': 'platform_uid',
            'source_channel': 'whatsapp',
            'submitted_by': 'customer_service',
            'submitted_at': '2026-04-14T12:15:00Z',
        }).json()
        response = client.post(f"/api/tasks/{submission['task_id']}/bind-check-result", json={
            'status': 'success',
            'result_code': 'bind_ok',
            'result_reason': 'manual backend bind success',
            'finished_at': f'2026-04-14T12:{17 + idx:02d}:00Z',
            'raw_result': {'guild_code': 'Piso', 'deptName': 'Piso', 'deptId': 'dept_1'},
        })
        assert response.status_code == 200

    run = client.post('/api/ops/official-group-approval-batches/run-ready', json={
        'decided_at': '2026-04-14T13:00:00Z',
        'decided_by': 'batch_runner',
    })
    assert run.status_code == 200
    body = run.json()
    assert body['ready_group_count'] == 1
    assert body['executed_count'] == 10
    assert body['unresolved_count'] == 0
    assert len(executor.calls) == 10
    assert executor.calls[0]['target_group'] == 'official-group-a'



def test_run_ready_official_group_batches_executes_ready_runtime_queue_using_target_group(monkeypatch):
    from app.main import create_app

    class StubRuntimeExecutor:
        def __init__(self):
            self.calls = []

        def approve(self, context):
            self.calls.append(dict(context))
            return {
                'status': 'success',
                'result_code': 'approval_ok',
                'result_reason': 'approved',
                'raw_result': {'target_group': context.get('registration_group')},
            }

    class MultiRecordCrmAdapter(StubCrmAdapter):
        def __init__(self):
            super().__init__()
            self.records = {}
            self._seed_record = None

        def seed(self, record):
            self._seed_record = dict(record)
            self.record = dict(record)
            for key in (record.get('ywId'), record.get('mobile')):
                normalized = str(key or '').strip()
                if normalized:
                    self.records[normalized] = dict(record)

        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            key = str(yw_id or '').strip() or str(mobile or '').strip()
            if key and key in self.records:
                return dict(self.records[key])
            if self._seed_record is None:
                return None
            return dict(self._seed_record)

        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            record = {"id": f"crm_{payload.get('ywId') or payload.get('mobile')}", **payload}
            for key in (payload.get('ywId'), payload.get('mobile')):
                normalized = str(key or '').strip()
                if normalized:
                    self.records[normalized] = dict(record)
            self.record = record
            return {"code": 0, "msg": "success", "data": None}

        def update_customer(self, payload):
            self.calls.append(("update_customer", payload))
            record = dict(payload)
            for key in (payload.get('ywId'), payload.get('mobile')):
                normalized = str(key or '').strip()
                if normalized:
                    self.records[normalized] = dict(record)
            self.record = record
            return {"code": 0, "msg": "success", "data": None}

    crm = MultiRecordCrmAdapter()
    crm.apps = [{'id': 'app_1', 'name': 'Linky'}]
    crm.depts = [{'deptId': 'dept_1', 'deptName': 'Piso'}]
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'OFFICIAL_GROUP_TARGET_MAP': {'registration_group_prefix:piso': 'official-group-piso'},
    })
    client = TestClient(app)

    for idx in range(2):
        mobile = f'87777000{idx}'
        account_id = f'7700000{idx}'
        crm.seed({
            'id': f'crm_{account_id}',
            'mobile': mobile,
            'ywId': account_id,
            'appId': 'app_1',
            'appName': 'Linky',
            'deptId': 'dept_1',
            'deptName': 'Piso',
            'pendaftaranGroup': 'Piso-5',
            'wa': '',
            'joinGroup': 0,
        })
        lead = client.post('/api/leads/upsert', json={
            'trace_id': f'trace-runtime-ready-{idx}',
            'source_platform': 'meta',
            'source_page_id': f'page-runtime-ready-{idx}',
            'country': 'Indonesia',
            'area_code': 62,
            'mobile': mobile,
            'app_name': 'Linky',
            'dept_name': 'Piso',
            'pendaftaran_group': 'Piso-5',
        }).json()
        submission = client.post('/api/account-submissions', json={
            'lead_id': lead['lead_id'],
            'submission_type': 'account_id',
            'account_id': account_id,
            'account_id_type': 'platform_uid',
            'source_channel': 'whatsapp',
            'submitted_by': 'customer_service',
            'submitted_at': '2026-04-14T12:15:00Z',
        }).json()
        response = client.post(f"/api/tasks/{submission['task_id']}/bind-check-result", json={
            'status': 'success',
            'result_code': 'bind_ok',
            'result_reason': 'manual backend bind success',
            'finished_at': f'2026-04-14T12:{17 + idx:02d}:00Z',
            'raw_result': {'guild_code': 'Piso', 'deptName': 'Piso', 'deptId': 'dept_1'},
        })
        assert response.status_code == 200

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-official-runtime-ready', json={
        'account_name': 'WA Official Runtime Ready',
        'responsible_type': 'official_group',
        'group_link_bindings': [{
            'link': 'https://chat.whatsapp.com/official-group-a',
            'group_name': '官方群01',
            'registration_group': 'official-group-piso',
            'area': 'Indonesia',
            'notify_profile_name': 'wa-approval-broadcast',
            'approval_count_threshold': 10,
            'approval_timeout_minutes': 10,
            'auto_recover_worker': True,
            'schedule_windows': [],
        }],
        'notify_profile_name': 'wa-approval-broadcast',
        'approval_count_threshold': 10,
        'approval_timeout_minutes': 10,
        'auto_recover_worker': True,
        'schedule_windows': [],
        'enabled': True,
        'notes': 'runtime ready test',
    })
    assert saved.status_code == 200

    service = client.app.state.service
    runtime_executor = StubRuntimeExecutor()
    monkeypatch.setattr(service, 'approval_batch_queue', lambda: {
        'registration_groups': [],
        'official_groups': [{
            'approval_type': 'official_group',
            'registration_group': '官方群01',
            'target_group': 'official-group-piso',
            'pending_count': 2,
            'release_count': 2,
            'ready': True,
            'source': 'official_runtime_group_state',
            'binding_link': 'https://chat.whatsapp.com/official-group-a',
            'group_name': '官方群01',
            'account_key': 'wa-official-runtime-ready',
        }],
    })
    monkeypatch.setattr(service, '_build_whatsapp_approval_runtime_state', lambda account_key, **kwargs: {
        'account_key': account_key,
        'active': True,
        'base_url': 'http://127.0.0.1:53637',
    })
    monkeypatch.setattr(service, '_build_runtime_registration_group_executor', lambda base_url: runtime_executor)

    run = client.post('/api/ops/official-group-approval-batches/run-ready', json={
        'decided_at': '2026-04-14T13:00:00Z',
        'decided_by': 'batch_runner',
    })
    assert run.status_code == 200
    body = run.json()
    assert body['ready_group_count'] == 1
    assert body['executed_count'] == 2
    assert body['unresolved_count'] == 0
    assert len(runtime_executor.calls) == 2
    assert runtime_executor.calls[0]['registration_group'] == 'official-group-piso'
    assert runtime_executor.calls[0]['target_phone_hint']



def test_run_ready_official_group_batches_runtime_queue_matches_requesters_by_phone_and_skips_unmatched(monkeypatch):
    from app.main import create_app

    class StubRuntimeExecutor:
        def __init__(self):
            self.calls = []

        def approve(self, context):
            self.calls.append(dict(context))
            return {
                'status': 'success',
                'result_code': 'approval_ok',
                'result_reason': 'approved',
                'raw_result': {'target_group': context.get('registration_group')},
            }

    class MultiRecordCrmAdapter(StubCrmAdapter):
        def __init__(self):
            super().__init__()
            self.records = {}

        def seed(self, record):
            for key in (record.get('ywId'), record.get('mobile')):
                normalized = str(key or '').strip()
                if normalized:
                    self.records[normalized] = dict(record)

        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            key = str(yw_id or '').strip() or str(mobile or '').strip()
            if key and key in self.records:
                return dict(self.records[key])
            return None

        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            record = {"id": f"crm_{payload.get('ywId') or payload.get('mobile')}", **payload}
            for key in (payload.get('ywId'), payload.get('mobile')):
                normalized = str(key or '').strip()
                if normalized:
                    self.records[normalized] = dict(record)
            self.record = record
            return {"code": 0, "msg": "success", "data": None}

        def update_customer(self, payload):
            self.calls.append(("update_customer", payload))
            return {"code": 0, "msg": "success", "data": None}

    crm = MultiRecordCrmAdapter()
    crm.apps = [{'id': 'app_1', 'name': 'Linky'}]
    crm.depts = [{'deptId': 'dept_1', 'deptName': 'Piso'}]
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'OFFICIAL_GROUP_TARGET_MAP': {'registration_group_prefix:piso': 'official-group-piso'},
    })
    client = TestClient(app)

    for idx, mobile in enumerate(['877770010', '877770011']):
        account_id = f'7711000{idx}'
        crm.seed({
            'id': f'crm_{account_id}',
            'mobile': mobile,
            'ywId': account_id,
            'appId': 'app_1',
            'appName': 'Linky',
            'deptId': 'dept_1',
            'deptName': 'Piso',
            'pendaftaranGroup': 'Piso-5',
            'wa': '',
            'joinGroup': 0,
        })
        lead = client.post('/api/leads/upsert', json={
            'trace_id': f'trace-runtime-match-{idx}',
            'source_platform': 'meta',
            'source_page_id': f'page-runtime-match-{idx}',
            'country': 'Indonesia',
            'area_code': 62,
            'mobile': mobile,
            'app_name': 'Linky',
            'dept_name': 'Piso',
            'pendaftaran_group': 'Piso-5',
        }).json()
        submission = client.post('/api/account-submissions', json={
            'lead_id': lead['lead_id'],
            'submission_type': 'account_id',
            'account_id': account_id,
            'account_id_type': 'platform_uid',
            'source_channel': 'whatsapp',
            'submitted_by': 'customer_service',
            'submitted_at': '2026-04-14T12:15:00Z',
        }).json()
        response = client.post(f"/api/tasks/{submission['task_id']}/bind-check-result", json={
            'status': 'success',
            'result_code': 'bind_ok',
            'result_reason': 'manual backend bind success',
            'finished_at': f'2026-04-14T12:{17 + idx:02d}:00Z',
            'raw_result': {'guild_code': 'Piso', 'deptName': 'Piso', 'deptId': 'dept_1'},
        })
        assert response.status_code == 200

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-official-runtime-match', json={
        'account_name': 'WA Official Runtime Match',
        'responsible_type': 'official_group',
        'group_link_bindings': [{
            'link': 'https://chat.whatsapp.com/official-group-a',
            'group_name': '官方群01',
            'registration_group': 'official-group-piso',
            'area': 'Indonesia',
            'notify_profile_name': 'wa-approval-broadcast',
            'approval_count_threshold': 10,
            'approval_timeout_minutes': 10,
            'auto_recover_worker': True,
            'schedule_windows': [],
        }],
        'notify_profile_name': 'wa-approval-broadcast',
        'approval_count_threshold': 10,
        'approval_timeout_minutes': 10,
        'auto_recover_worker': True,
        'schedule_windows': [],
        'enabled': True,
        'notes': 'runtime requester matching test',
    })
    assert saved.status_code == 200

    service = client.app.state.service
    runtime_executor = StubRuntimeExecutor()
    monkeypatch.setattr(service, 'approval_batch_queue', lambda: {
        'registration_groups': [],
        'official_groups': [{
            'approval_type': 'official_group',
            'registration_group': '官方群01',
            'target_group': 'official-group-piso',
            'pending_count': 2,
            'release_count': 2,
            'ready': True,
            'source': 'official_runtime_group_state',
            'binding_link': 'https://chat.whatsapp.com/official-group-a',
            'binding_registration_group': 'official-group-piso',
            'group_name': '官方群01',
            'account_key': 'wa-official-runtime-match',
            'requesters': [
                {
                    'requesterId': '62877770011@c.us',
                    'phoneRaw': '+62877770011',
                    'phoneNormalized': '+62877770011',
                    'displayName': 'matched-user',
                    'requestedAtUnix': 1713103200,
                    'requestedAtIso': '2026-04-14T12:00:00Z',
                },
                {
                    'requesterId': '62888888888@c.us',
                    'phoneRaw': '+62888888888',
                    'phoneNormalized': '+62888888888',
                    'displayName': 'stranger',
                    'requestedAtUnix': 1713103260,
                    'requestedAtIso': '2026-04-14T12:01:00Z',
                },
            ],
        }],
    })
    monkeypatch.setattr(service, '_build_whatsapp_approval_runtime_state', lambda account_key, **kwargs: {
        'account_key': account_key,
        'active': True,
        'base_url': 'http://127.0.0.1:53637',
    })
    monkeypatch.setattr(service, '_build_runtime_registration_group_executor', lambda base_url: runtime_executor)

    run = client.post('/api/ops/official-group-approval-batches/run-ready', json={
        'decided_at': '2026-04-14T13:00:00Z',
        'decided_by': 'batch_runner',
    })
    assert run.status_code == 200
    body = run.json()
    assert body['ready_group_count'] == 1
    assert body['executed_count'] == 1
    assert body['skipped_count'] == 1
    assert len(runtime_executor.calls) == 1
    assert runtime_executor.calls[0]['target_phone_hint'] == '877770011'
    unmatched_rows = [row for row in body['results'] if row.get('reason_code') == 'official_group_requester_unmatched']
    assert len(unmatched_rows) == 1
    assert unmatched_rows[0]['target_group'] == 'official-group-piso'



def test_run_ready_official_group_batches_records_unresolved_target_group_when_mapping_missing():

    class StubOfficialGroupApprovalExecutor:
        def __init__(self):
            self.calls = []

        def approve(self, *, target_group, lead, crm_snapshot, task):
            self.calls.append({'target_group': target_group, 'lead_id': lead.get('lead_id')})
            return {
                'status': 'success',
                'result_code': 'approval_ok',
                'result_reason': 'approved',
                'raw_result': {'target_group': target_group},
            }

    crm = StubCrmAdapter()
    crm.apps = [{'id': 'app_1', 'name': 'Linky'}]
    crm.depts = [{'deptId': 'dept_1', 'deptName': 'Piso'}]
    executor = StubOfficialGroupApprovalExecutor()
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'OFFICIAL_GROUP_APPROVAL_EXECUTOR': executor,
    })
    client = TestClient(app)

    for idx in range(10):
        mobile = f'899999992{idx:02d}'
        account_id = f'710010{idx:02d}'
        crm.record = {
            'id': f'crm_{account_id}',
            'mobile': mobile,
            'ywId': account_id,
            'appId': 'app_1',
            'appName': 'Linky',
            'deptId': 'dept_1',
            'deptName': 'Piso',
            'pendaftaranGroup': 'Piso-5',
            'wa': '',
            'joinGroup': 0,
        }
        lead = client.post('/api/leads/upsert', json={
            'trace_id': f'trace-unresolved-{idx}',
            'source_platform': 'meta',
            'source_page_id': f'page-unresolved-{idx}',
            'country': 'Indonesia',
            'area_code': 62,
            'mobile': mobile,
            'app_name': 'Linky',
            'dept_name': 'Piso',
            'pendaftaran_group': 'Piso-5',
        }).json()
        submission = client.post('/api/account-submissions', json={
            'lead_id': lead['lead_id'],
            'submission_type': 'account_id',
            'account_id': account_id,
            'account_id_type': 'platform_uid',
            'source_channel': 'whatsapp',
            'submitted_by': 'customer_service',
            'submitted_at': '2026-04-14T12:15:00Z',
        }).json()
        response = client.post(f"/api/tasks/{submission['task_id']}/bind-check-result", json={
            'status': 'success',
            'result_code': 'bind_ok',
            'result_reason': 'manual backend bind success',
            'finished_at': f'2026-04-14T12:{17 + idx:02d}:00Z',
            'raw_result': {'guild_code': 'Piso', 'deptName': 'Piso', 'deptId': 'dept_1'},
        })
        assert response.status_code == 200

    run = client.post('/api/ops/official-group-approval-batches/run-ready', json={
        'decided_at': '2026-04-14T13:00:00Z',
        'decided_by': 'batch_runner',
    })
    assert run.status_code == 200
    body = run.json()
    assert body['ready_group_count'] == 1
    assert body['executed_count'] == 0
    assert body['unresolved_count'] == 10
    assert executor.calls == []
    assert body['results'][0]['reason_code'] == 'official_group_target_unresolved'



def test_exception_queue_surfaces_retryable_and_manual_group_join_follow_up_actions():
    from app.main import create_app

    class StubOfficialGroupApprovalExecutor:
        def approve(self, *, target_group, lead, crm_snapshot, task):
            mobile = str(lead.get('mobile') or '')
            if mobile == '89999999986':
                return {
                    'status': 'failed',
                    'result_code': 'upstream_timeout',
                    'result_reason': 'bridge timeout',
                    'raw_result': {
                        'target_group': target_group,
                        'execution_disposition': 'retryable_failed',
                        'retryable': True,
                    },
                }
            return {
                'status': 'failed',
                'result_code': 'captcha_required',
                'result_reason': 'captcha required',
                'raw_result': {
                    'target_group': target_group,
                    'execution_disposition': 'manual_required',
                    'requires_human_action': True,
                },
            }

    crm = StubCrmAdapter()
    crm.apps = [{'id': 'app_1', 'name': 'Linky'}]
    crm.depts = [{'deptId': 'dept_1', 'deptName': 'Piso'}]
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'OFFICIAL_GROUP_APPROVAL_EXECUTOR': StubOfficialGroupApprovalExecutor(),
    })
    client = TestClient(app)

    def create_and_fail(trace_id: str, mobile: str, account_id: str):
        crm.record = {
            'id': f'crm_{account_id}',
            'mobile': mobile,
            'ywId': account_id,
            'appId': 'app_1',
            'appName': 'Linky',
            'deptId': 'dept_1',
            'deptName': 'Piso',
            'pendaftaranGroup': 'Piso-5',
            'wa': '',
            'joinGroup': 0,
        }
        lead = client.post('/api/leads/upsert', json={
            'trace_id': trace_id,
            'source_platform': 'meta',
            'source_page_id': f'page-{trace_id}',
            'country': 'Indonesia',
            'area_code': 62,
            'mobile': mobile,
            'app_name': 'Linky',
            'dept_name': 'Piso',
            'pendaftaran_group': 'Piso-5',
        }).json()
        submission = client.post('/api/account-submissions', json={
            'lead_id': lead['lead_id'],
            'submission_type': 'account_id',
            'account_id': account_id,
            'account_id_type': 'platform_uid',
            'source_channel': 'whatsapp',
            'submitted_by': 'customer_service',
            'submitted_at': '2026-04-14T12:15:00Z',
        }).json()
        client.post(f"/api/tasks/{submission['task_id']}/bind-check-result", json={
            'status': 'success',
            'result_code': 'bind_ok',
            'result_reason': 'manual backend bind success',
            'finished_at': '2026-04-14T12:17:00Z',
            'raw_result': {'guild_code': 'Piso', 'deptName': 'Piso', 'deptId': 'dept_1'},
        })
        client.post('/api/official-groups/approval-decisions', json={
            'lead_id': lead['lead_id'],
            'target_group': 'official-group-a',
            'decision': 'approve',
            'decided_at': '2026-04-14T12:18:00Z',
            'decided_by': 'operator_1',
        })
        return lead

    retry_lead = create_and_fail('trace-og-exc-retry', '89999999986', '66778886')
    manual_lead = create_and_fail('trace-og-exc-manual', '89999999985', '66778885')

    rows = client.get('/api/ops/exception-queue').json()['rows']
    retry_row = next(row for row in rows if row['lead_id'] == retry_lead['lead_id'])
    manual_row = next(row for row in rows if row['lead_id'] == manual_lead['lead_id'])
    assert retry_row['latest_action'] == 'retry_official_group_approval'
    assert manual_row['latest_action'] == 'manual_continue_official_group_approval'



def test_retry_official_group_approval_reuses_latest_failed_group_join_task_and_executes_again():
    from app.main import create_app

    class CountingExecutor:
        def __init__(self):
            self.calls = 0
        def approve(self, *, target_group, lead, crm_snapshot, task):
            self.calls += 1
            if self.calls == 1:
                return {
                    'status': 'failed',
                    'result_code': 'upstream_timeout',
                    'result_reason': 'bridge timeout',
                    'raw_result': {'target_group': target_group, 'execution_disposition': 'retryable_failed', 'retryable': True},
                }
            return {
                'status': 'success',
                'result_code': 'approval_ok',
                'result_reason': 'approved on retry',
                'raw_result': {'target_group': target_group},
            }

    crm = StubCrmAdapter()
    crm.record = {
        'id': 'crm_retry_approval_1',
        'mobile': '89999999984',
        'ywId': '66778884',
        'appId': 'app_1',
        'appName': 'Linky',
        'deptId': 'dept_1',
        'deptName': 'Piso',
        'pendaftaranGroup': 'Piso-5',
        'wa': '',
        'joinGroup': 0,
    }
    crm.apps = [{'id': 'app_1', 'name': 'Linky'}]
    crm.depts = [{'deptId': 'dept_1', 'deptName': 'Piso'}]
    executor = CountingExecutor()
    app = create_app({'DB_PATH': ':memory:', 'CRM_ADAPTER': crm, 'OFFICIAL_GROUP_APPROVAL_EXECUTOR': executor})
    client = TestClient(app)

    lead = client.post('/api/leads/upsert', json={
        'trace_id': 'trace-retry-official-api',
        'source_platform': 'meta',
        'source_page_id': 'page-retry-official-api',
        'country': 'Indonesia',
        'area_code': 62,
        'mobile': '89999999984',
        'app_name': 'Linky',
        'dept_name': 'Piso',
        'pendaftaran_group': 'Piso-5',
    }).json()
    submission = client.post('/api/account-submissions', json={
        'lead_id': lead['lead_id'],
        'submission_type': 'account_id',
        'account_id': '66778884',
        'account_id_type': 'platform_uid',
        'source_channel': 'whatsapp',
        'submitted_by': 'customer_service',
        'submitted_at': '2026-04-14T12:15:00Z',
    }).json()
    client.post(f"/api/tasks/{submission['task_id']}/bind-check-result", json={
        'status': 'success',
        'result_code': 'bind_ok',
        'result_reason': 'manual backend bind success',
        'finished_at': '2026-04-14T12:17:00Z',
        'raw_result': {'guild_code': 'Piso', 'deptName': 'Piso', 'deptId': 'dept_1'},
    })
    first = client.post('/api/official-groups/approval-decisions', json={
        'lead_id': lead['lead_id'],
        'target_group': 'official-group-a',
        'decision': 'approve',
        'decided_at': '2026-04-14T12:18:00Z',
        'decided_by': 'operator_1',
    }).json()
    assert first['follow_up_action'] == 'retry_official_group_approval'

    retried = client.post(f"/api/ops/leads/{lead['lead_id']}/retry-official-group-approval", json={
        'target_group': 'official-group-a',
        'decided_at': '2026-04-14T12:19:00Z',
        'decided_by': 'operator_2',
    })
    assert retried.status_code == 200
    body = retried.json()
    assert body['executed'] is True
    assert body['decision_result']['lead_status'] == 'group_join_success'
    assert executor.calls == 2



def test_create_app_can_build_webhook_official_group_executor_from_settings():
    from app.main import create_app

    class StubWebhookSession:
        pass

    app = create_app({
        "DB_PATH": ":memory:",
        "OFFICIAL_GROUP_APPROVAL_EXECUTOR_KIND": "webhook",
        "OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL": "https://example.test/approve",
        "OFFICIAL_GROUP_APPROVAL_WEBHOOK_TOKEN": "secret-token",
        "OFFICIAL_GROUP_APPROVAL_WEBHOOK_SESSION": StubWebhookSession(),
    })
    client = TestClient(app)

    response = client.get('/api/ops/official-group-approval-executor-health')
    assert response.status_code == 200
    body = response.json()
    assert body['configured'] is True
    assert body['provider'] == 'webhook'
    assert 'approve' in body['supports']



def test_webhook_official_group_executor_posts_expected_payload():
    from app.official_group_executor import WebhookOfficialGroupApprovalExecutor

    class StubResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class StubWebhookSession:
        def __init__(self):
            self.calls = []

        def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append({
                'url': url,
                'json': json,
                'headers': headers,
                'timeout': timeout,
            })
            return StubResponse({
                'status': 'success',
                'result_code': 'approval_ok',
                'result_reason': 'approved by webhook',
                'raw_result': {'target_group': json['target_group']},
            })

    session = StubWebhookSession()
    executor = WebhookOfficialGroupApprovalExecutor(
        webhook_url='https://example.test/approve',
        token='secret-token',
        session=session,
    )
    result = executor.approve(
        target_group='official-group-a',
        lead={'lead_id': 'lead_123', 'mobile': '89999999990'},
        crm_snapshot={'id': 'crm_123', 'wa': ''},
        task={'task_id': 'task_123'},
    )

    assert result['status'] == 'success'
    call = session.calls[0]
    assert call['url'] == 'https://example.test/approve'
    assert call['headers']['Authorization'] == 'Bearer secret-token'
    assert call['json']['target_group'] == 'official-group-a'
    assert call['json']['lead']['lead_id'] == 'lead_123'
    assert call['json']['task']['task_id'] == 'task_123'



def test_webhook_official_group_executor_normalizes_retryable_failed_response():
    from app.official_group_executor import WebhookOfficialGroupApprovalExecutor

    class StubResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class StubWebhookSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return StubResponse({
                'status': 'retryable_failed',
                'result_code': 'upstream_timeout',
                'result_reason': 'webhook upstream timeout',
                'raw_result': {'target_group': json['target_group']},
            })

    executor = WebhookOfficialGroupApprovalExecutor(
        webhook_url='https://example.test/approve',
        session=StubWebhookSession(),
    )
    result = executor.approve(
        target_group='official-group-a',
        lead={'lead_id': 'lead_123'},
        crm_snapshot={},
        task={'task_id': 'task_123'},
    )

    assert result['status'] == 'failed'
    assert result['result_code'] == 'upstream_timeout'
    assert result['raw_result']['execution_disposition'] == 'retryable_failed'
    assert result['raw_result']['retryable'] is True



def test_webhook_official_group_executor_normalizes_manual_required_response():
    from app.official_group_executor import WebhookOfficialGroupApprovalExecutor

    class StubResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class StubWebhookSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return StubResponse({
                'status': 'manual_required',
                'result_code': 'captcha_required',
                'result_reason': 'captcha required by upstream bridge',
                'raw_result': {'target_group': json['target_group']},
            })

    executor = WebhookOfficialGroupApprovalExecutor(
        webhook_url='https://example.test/approve',
        session=StubWebhookSession(),
    )
    result = executor.approve(
        target_group='official-group-a',
        lead={'lead_id': 'lead_123'},
        crm_snapshot={},
        task={'task_id': 'task_123'},
    )

    assert result['status'] == 'failed'
    assert result['result_code'] == 'captcha_required'
    assert result['raw_result']['execution_disposition'] == 'manual_required'
    assert result['raw_result']['requires_human_action'] is True



def test_group_join_result_success_updates_crm_official_group_only_after_join():
    from app.main import create_app

    crm = StubCrmAdapter()
    crm.record = {
        "id": "crm_join_1",
        "mobile": "89999999999",
        "ywId": "66778899",
        "appId": "app_1",
        "appName": "Linky",
        "deptId": "dept_1",
        "deptName": "Piso",
        "pendaftaranGroup": "Piso-5",
        "wa": "",
        "joinGroup": 0,
        "qbType": "",
        "qbAccout": "",
        "dbHolder": "",
    }
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    app = create_app({"DB_PATH": ":memory:", "CRM_ADAPTER": crm})
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-10b",
            "source_platform": "meta",
            "source_page_id": "page-10b",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999999999",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "66778899",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:15:00Z",
        },
    ).json()
    bind_result = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:17:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    ).json()

    crm.calls.clear()
    response = client.post(
        f"/api/tasks/{bind_result['group_join_task_id']}/group-join-result",
        json={
            "status": "success",
            "result_code": "join_ok",
            "result_reason": "joined official group",
            "finished_at": "2026-04-14T12:18:00Z",
            "raw_result": {"target_group": "official-group-a"},
        },
    )
    assert response.status_code == 200
    update_payload = next(payload for name, payload in crm.calls if name == "update_customer")
    assert update_payload["id"] == "crm_1"
    assert update_payload["pendaftaranGroup"] == "Piso-5"
    assert update_payload["wa"] == "official-group-a"



def test_official_group_approval_check_returns_eligible_when_crm_verified_and_target_group_not_yet_joined():
    from app.main import create_app

    crm = StubCrmAdapter()
    crm.record = {
        "id": "crm_join_eligible_1",
        "mobile": "89999999997",
        "ywId": "66778897",
        "appId": "app_1",
        "appName": "Linky",
        "deptId": "dept_1",
        "deptName": "Piso",
        "pendaftaranGroup": "Piso-5",
        "wa": "",
        "joinGroup": 0,
    }
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    app = create_app({"DB_PATH": ":memory:", "CRM_ADAPTER": crm})
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-official-eligible",
            "source_platform": "meta",
            "source_page_id": "page-official-eligible",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999999997",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "66778897",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:15:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:17:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    )

    response = client.post(
        "/api/official-groups/approval-checks",
        json={
            "lead_id": lead["lead_id"],
            "target_group": "official-group-a",
            "checked_at": "2026-04-14T12:18:00Z",
            "checked_by": "operator_1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["eligible"] is True
    assert body["reason_code"] == "eligible"
    assert body["next_action"] == "approve_official_group"
    assert body["crm_customer_found"] is True
    assert body["crm_verified"] is True
    assert body["crm_snapshot"]["id"] == "crm_1"
    assert body["crm_snapshot"]["wa"] == ""

    audit_rows = client.get('/api/ops/operator-audit-log').json()['rows']
    assert any(
        row['event_type'] == 'official_group_approval_eligibility_checked'
        and row['lead_id'] == lead['lead_id']
        and '"eligible": true' in row['payload']
        for row in audit_rows
    )



def test_official_group_approval_check_restores_verified_state_from_success_sync_logs_when_lead_columns_are_blank():
    from app.main import create_app

    crm = StubCrmAdapter()
    crm.record = {
        "id": "crm_join_restore_1",
        "mobile": "89999999995",
        "ywId": "66778895",
        "appId": "app_1",
        "appName": "Linky",
        "deptId": "dept_1",
        "deptName": "Piso",
        "pendaftaranGroup": "Piso-5",
        "wa": "",
        "joinGroup": 0,
    }
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    app = create_app({"DB_PATH": ":memory:", "CRM_ADAPTER": crm})
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-official-restore",
            "source_platform": "meta",
            "source_page_id": "page-official-restore",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999999995",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "66778895",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:15:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:17:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    )

    with app.state.service.db.connect() as conn:
        conn.execute(
            """
            UPDATE leads
            SET crm_verified_payload = NULL,
                crm_verified_app_name = NULL,
                crm_verified_dept_name = NULL,
                crm_verified_registration_group = NULL,
                crm_verified_official_group = NULL,
                crm_verified_at = NULL
            WHERE lead_id = ?
            """,
            (lead["lead_id"],),
        )
        conn.commit()

    response = client.post(
        "/api/official-groups/approval-checks",
        json={
            "lead_id": lead["lead_id"],
            "target_group": "official-group-a",
            "checked_at": "2026-04-14T12:18:00Z",
            "checked_by": "operator_1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["eligible"] is True
    assert body["crm_verified"] is True

    with app.state.service.db.connect() as conn:
        restored = conn.execute(
            "SELECT crm_verified_at, crm_verified_app_name, crm_verified_registration_group FROM leads WHERE lead_id = ?",
            (lead["lead_id"],),
        ).fetchone()
    assert restored["crm_verified_at"]
    assert restored["crm_verified_app_name"] == "Linky"
    assert restored["crm_verified_registration_group"] == "Piso-5"



def test_official_group_approval_check_restores_verified_state_from_legacy_success_sync_logs_without_verified_flag():
    from app.main import create_app

    crm = StubCrmAdapter()
    crm.record = {
        "id": "crm_join_restore_legacy_1",
        "mobile": "89999999994",
        "ywId": "66778894",
        "appId": "app_1",
        "appName": "Linky",
        "deptId": "dept_1",
        "deptName": "Piso",
        "pendaftaranGroup": "Piso-5",
        "wa": "",
        "joinGroup": 0,
    }
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    app = create_app({"DB_PATH": ":memory:", "CRM_ADAPTER": crm})
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-official-restore-legacy",
            "source_platform": "meta",
            "source_page_id": "page-official-restore-legacy",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999999994",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "66778894",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:15:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:17:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    )

    with app.state.service.db.connect() as conn:
        conn.execute(
            """
            UPDATE leads
            SET crm_verified_payload = NULL,
                crm_verified_app_name = NULL,
                crm_verified_dept_name = NULL,
                crm_verified_registration_group = NULL,
                crm_verified_official_group = NULL,
                crm_verified_at = NULL
            WHERE lead_id = ?
            """,
            (lead["lead_id"],),
        )
        conn.execute(
            """
            UPDATE sync_logs
            SET response_snapshot = ?
            WHERE lead_id = ? AND sync_type = 'customer_upsert'
            """,
            (json.dumps({"action": "create", "crm_response": {"code": 0, "msg": "success"}}), lead["lead_id"]),
        )
        conn.commit()

    response = client.post(
        "/api/official-groups/approval-checks",
        json={
            "lead_id": lead["lead_id"],
            "target_group": "official-group-a",
            "checked_at": "2026-04-14T12:18:00Z",
            "checked_by": "operator_1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["eligible"] is True
    assert body["crm_verified"] is True

    with app.state.service.db.connect() as conn:
        restored = conn.execute(
            "SELECT crm_verified_at, crm_verified_app_name, crm_verified_registration_group FROM leads WHERE lead_id = ?",
            (lead["lead_id"],),
        ).fetchone()
    assert restored["crm_verified_at"]
    assert restored["crm_verified_app_name"] == "Linky"
    assert restored["crm_verified_registration_group"] == "Piso-5"



def test_official_group_approval_check_uses_local_verified_cache_when_live_crm_lookup_misses():
    from app.main import create_app

    class MissingCrmAdapter(StubCrmAdapter):
        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
            return None

    crm = MissingCrmAdapter()
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    app = create_app({"DB_PATH": ":memory:", "CRM_ADAPTER": crm})
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-official-local-cache",
            "source_platform": "meta",
            "source_page_id": "page-official-local-cache",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999999994",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "66778894",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:15:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:17:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    )
    with app.state.service.db.connect() as conn:
        conn.execute(
            """
            UPDATE leads
            SET crm_verified_payload = ?,
                crm_verified_app_name = ?,
                crm_verified_dept_name = ?,
                crm_verified_registration_group = ?,
                crm_verified_at = ?
            WHERE lead_id = ?
            """,
            (
                json.dumps({
                    "mobile": "89999999994",
                    "ywId": "66778894",
                    "appName": "Linky",
                    "deptName": "Piso",
                    "pendaftaranGroup": "Piso-5",
                    "wa": "",
                    "joinGroup": 0,
                }),
                "Linky",
                "Piso",
                "Piso-5",
                "2026-04-14T12:17:30Z",
                lead["lead_id"],
            ),
        )
        conn.commit()

    response = client.post(
        "/api/official-groups/approval-checks",
        json={
            "lead_id": lead["lead_id"],
            "target_group": "official-group-a",
            "checked_at": "2026-04-14T12:18:00Z",
            "checked_by": "operator_1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["eligible"] is True
    assert body["crm_verified"] is True
    assert body["crm_customer_found"] is False
    assert body["reason_code"] == "eligible"
    assert body["crm_snapshot"]["source"] == "local_verified_cache"
    assert body["crm_snapshot"]["ywId"] == "66778894"
    assert body["crm_snapshot"]["pendaftaranGroup"] == "Piso-5"


def test_official_group_approval_check_rejects_when_crm_already_points_to_target_group():
    from app.main import create_app

    crm = StubCrmAdapter()
    crm.record = {
        "id": "crm_join_already_1",
        "mobile": "89999999996",
        "ywId": "66778896",
        "appId": "app_1",
        "appName": "Linky",
        "deptId": "dept_1",
        "deptName": "Piso",
        "pendaftaranGroup": "Piso-5",
        "wa": "official-group-a",
        "joinGroup": 1,
    }
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    app = create_app({"DB_PATH": ":memory:", "CRM_ADAPTER": crm})
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-official-already",
            "source_platform": "meta",
            "source_page_id": "page-official-already",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999999996",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "66778896",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:15:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:17:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    )
    crm.record["wa"] = "official-group-a"
    crm.record["joinGroup"] = 1

    response = client.post(
        "/api/official-groups/approval-checks",
        json={
            "lead_id": lead["lead_id"],
            "target_group": "official-group-a",
            "checked_at": "2026-04-14T12:18:00Z",
            "checked_by": "operator_1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["eligible"] is False
    assert body["reason_code"] == "already_in_target_group"
    assert body["next_action"] == "skip_duplicate_group_approval"
    assert body["crm_customer_found"] is True
    assert body["crm_snapshot"]["wa"] == "official-group-a"



def test_group_join_result_records_failed_crm_sync_when_update_cannot_be_verified():
    from app.main import create_app

    class UnverifiableUpdateCrmAdapter(StubCrmAdapter):
        def __init__(self):
            super().__init__()
            self.find_calls = 0
        def find_customer(self, *, yw_id=None, mobile=None):
            self.find_calls += 1
            if self.find_calls == 1:
                return dict(self.record) if self.record else None
            return {
                "id": "crm_join_2",
                "mobile": mobile,
                "ywId": yw_id,
                "appId": "app_1",
                "appName": "Linky",
                "deptId": "dept_1",
                "deptName": "Piso",
                "pendaftaranGroup": "Piso-5",
                "wa": "",
            }

    crm = UnverifiableUpdateCrmAdapter()
    crm.record = {
        "id": "crm_join_2",
        "mobile": "89999999998",
        "ywId": "66778898",
        "appId": "app_1",
        "appName": "Linky",
        "deptId": "dept_1",
        "deptName": "Piso",
        "pendaftaranGroup": "Piso-5",
        "wa": "",
    }
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    app = create_app({"DB_PATH": ":memory:", "CRM_ADAPTER": crm})
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-10c",
            "source_platform": "meta",
            "source_page_id": "page-10c",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "89999999998",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "66778898",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:15:00Z",
        },
    ).json()
    bind_result = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:17:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    ).json()

    response = client.post(
        f"/api/tasks/{bind_result['group_join_task_id']}/group-join-result",
        json={
            "status": "success",
            "result_code": "join_ok",
            "result_reason": "joined official group",
            "finished_at": "2026-04-14T12:18:00Z",
            "raw_result": {"target_group": "official-group-b"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body['lead_status'] == 'group_join_success'
    assert body['crm_sync_status'] == 'failed'
    assert body['crm_result_reason'] == 'CRM write could not be verified.'

    timeline = client.get(f"/api/leads/{lead['lead_id']}/timeline").json()
    group_sync = [row for row in timeline['sync_logs'] if row['sync_type'] == 'official_group_update']
    assert group_sync[-1]['status'] == 'failed'



def test_lead_timeline_returns_events_and_tasks():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-11",
            "source_platform": "meta",
            "source_page_id": "page-11",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81112223333",
        },
    ).json()
    client.post(
        "/api/events/collect",
        json={
            "trace_id": "trace-11",
            "lead_id": lead["lead_id"],
            "event_type": "contact_clicked",
            "event_source": "landing_page",
            "event_value": "wa",
            "page_id": "page-11",
            "session_id": "sess-11",
            "happened_at": "2026-04-14T12:20:00Z",
        },
    )
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "99112233",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:21:00Z",
        },
    ).json()

    response = client.get(f"/api/leads/{lead['lead_id']}/timeline")

    assert response.status_code == 200
    body = response.json()
    assert body["lead"]["lead_id"] == lead["lead_id"]
    assert len(body["events"]) >= 1
    assert len(body["tasks"]) >= 1
    assert len(body["status_history"]) >= 2
    assert body["tasks"][0]["task_id"] == submission["task_id"]


def test_funnel_report_aggregates_by_platform_campaign_country():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-12",
            "source_platform": "meta",
            "source_campaign": "camp-a",
            "source_page_id": "page-12",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "82223334444",
        },
    ).json()
    client.post(
        "/api/events/collect",
        json={
            "trace_id": "trace-12",
            "lead_id": lead["lead_id"],
            "event_type": "contact_clicked",
            "event_source": "landing_page",
            "event_value": "wa",
            "page_id": "page-12",
            "session_id": "sess-12",
            "happened_at": "2026-04-14T12:30:00Z",
        },
    )
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "12345678",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T12:31:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T12:32:00Z",
            "raw_result": {"guild_code": "MCN-11"},
        },
    )

    response = client.get("/api/reports/funnel")
    assert response.status_code == 200
    body = response.json()
    assert len(body["rows"]) >= 1
    row = body["rows"][0]
    assert row["source_platform"] == "meta"
    assert row["source_campaign"] == "camp-a"
    assert row["country"] == "Indonesia"
    assert row["lead_count"] == 1
    assert row["engaged_count"] == 1
    assert row["account_submitted_count"] == 1
    assert row["bind_success_count"] == 1


class StubCrmAdapter:
    def __init__(self):
        self.calls = []
        self.record = None
        self.apps = []
        self.depts = []

    def find_customer(self, *, yw_id=None, mobile=None):
        self.calls.append(("find_customer", {"yw_id": yw_id, "mobile": mobile}))
        return self.record

    def create_customer(self, payload):
        self.calls.append(("create_customer", payload))
        self.record = {"id": "crm_1", **payload}
        return {"code": 0, "msg": "success", "data": None}

    def update_customer(self, payload):
        self.calls.append(("update_customer", payload))
        self.record = payload
        return {"code": 0, "msg": "success", "data": None}

    def create_registration_group_batch(self, payload):
        self.calls.append(("create_registration_group_batch", payload))
        return {"code": 0, "msg": "success", "data": None}

    def get_apps(self):
        self.calls.append(("get_apps", {}))
        return list(self.apps)

    def get_depts(self):
        self.calls.append(("get_depts", {}))
        return list(self.depts)

    def upload_voucher(self, *, customer_id, image_path):
        self.calls.append(("upload_voucher", {"customer_id": customer_id, "image_path": image_path}))
        return "http://oss/test.png"

    def attach_voucher(self, record, image_url, *, remark_suffix=None):
        payload = dict(record)
        payload["fileUrl"] = image_url
        payload["pzStatus"] = 1
        if remark_suffix:
            payload["remark"] = f"{payload.get('remark','')} | {remark_suffix}".strip(" |")
        self.calls.append(("attach_voucher", {"record": record, "image_url": image_url, "remark_suffix": remark_suffix}))
        self.record = payload
        return {"code": 0, "msg": "success", "data": None}


def test_service_syncs_bind_success_to_crm_create():
    from app.main import create_app

    crm = StubCrmAdapter()
    crm.apps = [
        {"id": "app_1", "name": "Linky"},
        {"id": "app_2", "ywName": "FUMI"},
    ]
    crm.depts = [
        {"deptId": "dept_1", "deptName": "Piso"},
        {"id": "dept_2", "name": "Permata"},
    ]
    app = create_app({"DB_PATH": ":memory:", "CRM_ADAPTER": crm})
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-13",
            "source_platform": "meta",
            "source_campaign": "camp-b",
            "source_page_id": "page-13",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "83334445555",
            "app_name": "Linky",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "88889999",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:00:00Z",
        },
    ).json()
    bind = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T13:01:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "2010885372469563394"},
        },
    )
    assert bind.status_code == 200
    call_names = [name for name, _ in crm.calls]
    assert "create_customer" in call_names
    create_payload = next(payload for name, payload in crm.calls if name == "create_customer")
    assert create_payload["appName"] == "Linky"
    assert create_payload["appId"] == "app_1"
    assert create_payload["deptName"] == "Piso"
    assert create_payload["deptId"] == "2010885372469563394"
    assert create_payload["pendaftaranGroup"] == "Piso-5"
    assert create_payload["wa"] == ""

    timeline = client.get(f"/api/leads/{lead['lead_id']}/timeline")
    assert timeline.status_code == 200
    sync_logs = timeline.json()["sync_logs"]
    assert len(sync_logs) == 1
    assert sync_logs[0]["target_system"] == "crm"
    assert sync_logs[0]["sync_type"] == "customer_upsert"
    assert sync_logs[0]["status"] == "success"
    assert '88889999' in sync_logs[0]["request_snapshot"]


def test_service_always_uses_create_customer_for_bind_success_even_if_find_customer_returns_existing_record():
    from app.main import create_app

    crm = StubCrmAdapter()
    crm.record = {
        "id": "crm_9",
        "mobile": "83334445556",
        "ywId": "88889998",
        "appId": "old_app",
        "joinGroup": 0,
        "qbType": "",
        "qbAccout": "",
        "dbHolder": "",
    }
    crm.apps = [
        {"id": "app_1", "name": "Linky"},
        {"id": "app_2", "ywName": "FUMI"},
    ]
    crm.depts = [
        {"deptId": "dept_1", "deptName": "Piso"},
        {"id": "dept_2", "name": "Permata"},
    ]
    app = create_app({"DB_PATH": ":memory:", "CRM_ADAPTER": crm})
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-13b",
            "source_platform": "meta",
            "source_campaign": "camp-b",
            "source_page_id": "page-13b",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "83334445556",
            "app_name": "FUMI",
            "dept_name": "Permata",
            "pendaftaran_group": "Permata-7",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "88889998",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:00:00Z",
        },
    ).json()
    bind = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T13:01:00Z",
            "raw_result": {"guild_code": "Permata", "deptName": "Permata"},
        },
    )
    assert bind.status_code == 200
    create_payload = next(payload for name, payload in crm.calls if name == "create_customer")
    assert create_payload["appName"] == "FUMI"
    assert create_payload["appId"] == "app_2"
    assert create_payload["deptName"] == "Permata"
    assert create_payload["deptId"] == "dept_2"
    assert create_payload["pendaftaranGroup"] == "Permata-7"
    assert create_payload["wa"] == ""
    assert not [payload for name, payload in crm.calls if name == "update_customer"]

    timeline = client.get(f"/api/leads/{lead['lead_id']}/timeline")
    assert timeline.status_code == 200
    sync_logs = timeline.json()["sync_logs"]
    assert len(sync_logs) == 1
    assert sync_logs[0]["target_system"] == "crm"
    assert sync_logs[0]["sync_type"] == "customer_upsert"
    assert sync_logs[0]["status"] == "success"
    assert '"action": "create"' in sync_logs[0]["response_snapshot"]


def test_service_returns_crm_failure_when_repeated_submission_create_is_rejected_as_duplicate():
    from app.main import create_app

    class DuplicateCreateCrmAdapter(StubCrmAdapter):
        def create_customer(self, payload):
            self.calls.append(("create_customer", payload))
            return {"code": 10002, "msg": "数据库中已存在该记录", "data": None}

    crm = DuplicateCreateCrmAdapter()
    crm.record = {
        "id": "crm_dup_1",
        "joinGroup": 0,
        "qbType": "",
        "qbAccout": "",
        "dbHolder": "",
    }
    crm.apps = [{"id": "app_1", "name": "Linky"}]
    crm.depts = [{"deptId": "dept_1", "deptName": "Piso"}]
    app = create_app({"DB_PATH": ":memory:", "CRM_ADAPTER": crm})
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-13c",
            "source_platform": "meta",
            "source_campaign": "camp-c",
            "source_page_id": "page-13c",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "83334445557",
            "app_name": "Linky",
            "pendaftaran_group": "Piso-8",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "88889997",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:00:00Z",
        },
    ).json()
    bind = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T13:01:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "dept_1"},
        },
    )
    assert bind.status_code == 200
    body = bind.json()
    assert body["next_action"] == "retry_crm_sync"
    assert body["reason"] == "crm_sync_failed"
    assert body["result_reason"] == "Data duplication."
    assert not [payload for name, payload in crm.calls if name == "update_customer"]

    timeline = client.get(f"/api/leads/{lead['lead_id']}/timeline")
    assert timeline.status_code == 200
    sync_logs = timeline.json()["sync_logs"]
    assert len(sync_logs) == 1
    assert sync_logs[0]["status"] == "failed"
    assert '"action": "create"' in sync_logs[0]["response_snapshot"]


def test_service_attaches_voucher_to_existing_crm_record():
    from app.main import create_app

    crm = StubCrmAdapter()
    crm.record = {
        "id": "crm_2",
        "mobile": "84445556666",
        "ywId": "99990000",
        "fileUrl": "",
        "pzStatus": 0,
        "remark": ""
    }
    app = create_app({"DB_PATH": ":memory:", "CRM_ADAPTER": crm})
    client = TestClient(app)

    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-14",
            "source_platform": "meta",
            "source_page_id": "page-14",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "84445556666",
            "app_name": "Linky",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "99990000",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:02:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T13:03:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "2010885372469563394"},
        },
    )
    response = client.post(
        f"/api/leads/{lead['lead_id']}/voucher-attach",
        json={
            "image_path": "/tmp/proof.png",
            "remark_suffix": "uploaded by flow"
        },
    )

    assert response.status_code == 200
    names = [name for name, _ in crm.calls]
    assert "upload_voucher" in names
    assert "attach_voucher" in names


def test_registration_group_approval_batch_syncs_to_crm():
    from app.main import create_app

    crm = StubCrmAdapter()
    app = create_app({"DB_PATH": ":memory:", "CRM_ADAPTER": crm})
    client = TestClient(app)

    response = client.post(
        "/api/registration-groups/approval-batches",
        json={
            "registration_group": "Piso-5",
            "approved_count": 30,
            "approved_by": "cs_001",
            "approved_by_name": "注册客服A",
            "source_platform": "meta",
            "source_campaign": "indo-campaign-1",
            "source_adset": "adset-a",
            "source_ad": "creative-3",
            "approved_at": "2026-04-14T16:59:03Z",
            "area": "Indonesia",
            "remark": "manual approval batch"
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["crm_sync_status"] == "success"
    assert body["crm_payload"] == {
        "area": "Indonesia",
        "groupNo": "Piso-5",
        "groupPeopleNum": "30"
    }
    assert ("create_registration_group_batch", {"area": "Indonesia", "groupNo": "Piso-5", "groupPeopleNum": "30"}) in crm.calls


def test_registration_group_approval_batch_is_idempotent_per_approval_run_id(tmp_path):
    import sqlite3
    from app.main import create_app

    db_path = tmp_path / "registration-group-approval-batch-idempotent.db"
    crm = StubCrmAdapter()
    app = create_app({"DB_PATH": str(db_path), "CRM_ADAPTER": crm})
    client = TestClient(app)
    payload = {
        "registration_group": "Piso-5",
        "approved_count": 2,
        "approved_by": "cs_001",
        "approved_by_name": "注册客服A",
        "approved_at": "2026-04-14T16:59:03Z",
        "area": "Indonesia",
        "remark": "manual approval batch",
        "approval_run_id": "registration_group_approval_batch_dedupe_1",
    }

    first = client.post("/api/registration-groups/approval-batches", json=payload)
    second = client.post("/api/registration-groups/approval-batches", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert first_body["approval_run_id"] == payload["approval_run_id"]
    assert second_body["approval_run_id"] == payload["approval_run_id"]
    assert first_body["crm_payload"] == second_body["crm_payload"]
    assert sum(1 for name, _ in crm.calls if name == "create_registration_group_batch") == 1

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT request_snapshot FROM sync_logs WHERE sync_type = 'registration_group_approval_batch'"
        ).fetchall()
    matching = [row for row in rows if payload["approval_run_id"] in str(row[0] or "")]
    assert len(matching) == 1


def test_registration_group_approval_batch_is_atomic_under_concurrent_same_run_id(tmp_path):
    import sqlite3
    import threading
    import time
    from app.main import RegistrationGroupApprovalBatchRequest, create_app

    class SlowStubCrmAdapter(StubCrmAdapter):
        def __init__(self):
            super().__init__()
            self._lock = threading.Lock()

        def create_registration_group_batch(self, payload):
            time.sleep(0.05)
            with self._lock:
                return super().create_registration_group_batch(payload)

    db_path = tmp_path / "registration-group-approval-batch-concurrent-idempotent.db"
    crm = SlowStubCrmAdapter()
    app_a = create_app({"DB_PATH": str(db_path), "CRM_ADAPTER": crm, "INGRESS_WORKER_ENABLED": False})
    app_b = create_app({"DB_PATH": str(db_path), "CRM_ADAPTER": crm, "INGRESS_WORKER_ENABLED": False})
    services = [app_a.state.service, app_b.state.service]
    payload = RegistrationGroupApprovalBatchRequest(
        registration_group="Piso-5",
        approved_count=2,
        approved_by="cs_001",
        approved_by_name="注册客服A",
        approved_at="2026-04-14T16:59:03Z",
        area="Indonesia",
        remark="manual approval batch",
        approval_run_id="registration_group_approval_batch_concurrent_dedupe_1",
    )

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker(service):
        try:
            barrier.wait(timeout=2)
            results.append(service.create_registration_group_approval_batch(payload))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert len(results) == 2
    assert sum(1 for name, _ in crm.calls if name == "create_registration_group_batch") == 1

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT request_snapshot FROM sync_logs WHERE sync_type = 'registration_group_approval_batch'"
        ).fetchall()
    matching = [row for row in rows if payload.approval_run_id in str(row[0] or "")]
    assert len(matching) == 1


def test_database_file_connections_enable_busy_timeout_and_wal(tmp_path):
    from app.main import Database

    db_path = tmp_path / "sqlite-pragmas.db"
    db = Database(str(db_path))
    with db.connect() as conn:
        busy_timeout = conn.execute('PRAGMA busy_timeout').fetchone()[0]
        journal_mode = conn.execute('PRAGMA journal_mode').fetchone()[0]

    assert int(busy_timeout) == 30000
    assert str(journal_mode).lower() == 'wal'


def test_registration_group_approval_batch_allows_retry_after_failed_attempt(tmp_path):
    import sqlite3
    from app.main import RegistrationGroupApprovalBatchRequest, create_app

    class FailOnceStubCrmAdapter(StubCrmAdapter):
        def __init__(self):
            super().__init__()
            self._attempts = 0

        def create_registration_group_batch(self, payload):
            self._attempts += 1
            self.calls.append(("create_registration_group_batch", payload))
            if self._attempts == 1:
                return {"code": 502, "msg": "temporary failure", "data": None}
            return {"code": 0, "msg": "success", "data": None}

    db_path = tmp_path / "registration-group-approval-batch-retry-after-fail.db"
    crm = FailOnceStubCrmAdapter()
    app = create_app({"DB_PATH": str(db_path), "CRM_ADAPTER": crm, "INGRESS_WORKER_ENABLED": False})
    service = app.state.service
    payload = RegistrationGroupApprovalBatchRequest(
        registration_group="Piso-5",
        approved_count=2,
        approved_by="cs_001",
        approved_by_name="注册客服A",
        approved_at="2026-04-14T16:59:03Z",
        area="Indonesia",
        remark="manual approval batch",
        approval_run_id="registration_group_approval_batch_retry_after_fail_1",
    )

    first = service.create_registration_group_approval_batch(payload)
    second = service.create_registration_group_approval_batch(payload)

    assert first["crm_sync_status"] == "failed"
    assert second["crm_sync_status"] == "success"
    assert sum(1 for name, _ in crm.calls if name == "create_registration_group_batch") == 2

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT request_snapshot, response_snapshot FROM sync_logs WHERE sync_type = 'registration_group_approval_batch'"
        ).fetchall()
    matching = [row for row in rows if payload.approval_run_id in str(row[0] or "")]
    assert len(matching) == 2
    assert any('"code": 0' in str(row[1] or '') for row in matching)


def test_registration_group_approval_batch_recovers_after_crm_exception(tmp_path):
    import sqlite3
    from app.main import RegistrationGroupApprovalBatchRequest, create_app

    class RaiseOnceStubCrmAdapter(StubCrmAdapter):
        def __init__(self):
            super().__init__()
            self._attempts = 0

        def create_registration_group_batch(self, payload):
            self._attempts += 1
            self.calls.append(("create_registration_group_batch", payload))
            if self._attempts == 1:
                raise RuntimeError('crm gateway timeout')
            return {"code": 0, "msg": "success", "data": None}

    db_path = tmp_path / "registration-group-approval-batch-retry-after-exception.db"
    crm = RaiseOnceStubCrmAdapter()
    app = create_app({"DB_PATH": str(db_path), "CRM_ADAPTER": crm, "INGRESS_WORKER_ENABLED": False})
    service = app.state.service
    payload = RegistrationGroupApprovalBatchRequest(
        registration_group="Piso-5",
        approved_count=2,
        approved_by="cs_001",
        approved_by_name="注册客服A",
        approved_at="2026-04-14T16:59:03Z",
        area="Indonesia",
        remark="manual approval batch",
        approval_run_id="registration_group_approval_batch_retry_after_exception_1",
    )

    first = service.create_registration_group_approval_batch(payload)
    second = service.create_registration_group_approval_batch(payload)

    assert first["crm_sync_status"] == "failed"
    assert 'crm gateway timeout' in str(first["crm_response"])
    assert second["crm_sync_status"] == "success"
    assert sum(1 for name, _ in crm.calls if name == "create_registration_group_batch") == 2

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT status, response_snapshot FROM registration_group_approval_batch_runs WHERE approval_run_id = ?",
            (payload.approval_run_id,),
        ).fetchall()
    assert rows[0][0] == 'success'
    assert 'success' in str(rows[0][1] or '')


def test_registration_group_approval_batch_duplicate_processing_returns_processing(tmp_path):
    import sqlite3
    from app.main import RegistrationGroupApprovalBatchRequest, create_app, utc_now

    db_path = tmp_path / "registration-group-approval-batch-processing-duplicate.db"
    crm = StubCrmAdapter()
    app = create_app({"DB_PATH": str(db_path), "CRM_ADAPTER": crm, "INGRESS_WORKER_ENABLED": False})
    service = app.state.service
    payload = RegistrationGroupApprovalBatchRequest(
        registration_group="Piso-5",
        approved_count=2,
        approved_by="cs_001",
        approved_by_name="注册客服A",
        approved_at="2026-04-14T16:59:03Z",
        area="Indonesia",
        remark="manual approval batch",
        approval_run_id="registration_group_approval_batch_processing_duplicate_1",
    )

    now = utc_now()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO registration_group_approval_batch_runs (approval_run_id, sync_log_id, status, request_snapshot, response_snapshot, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (payload.approval_run_id, None, 'processing', '{"approval_run_id": "registration_group_approval_batch_processing_duplicate_1"}', '{}', now, now),
        )
        conn.commit()

    service._wait_for_registration_group_approval_batch_run = lambda approval_run_id, timeout_seconds=5.0, poll_interval_seconds=0.05: {
        'approval_run_id': approval_run_id,
        'status': 'processing',
        'request_snapshot_dict': {'approval_run_id': approval_run_id},
        'response_snapshot_dict': {},
    }
    result = service.create_registration_group_approval_batch(payload)

    assert result['crm_sync_status'] == 'processing'
    assert result.get('duplicate') is True
    assert sum(1 for name, _ in crm.calls if name == 'create_registration_group_batch') == 0


def test_process_next_ingress_job_claim_is_atomic_under_concurrent_workers(tmp_path):
    import sqlite3
    import threading
    import time
    from app.main import create_app

    db_path = tmp_path / "ingress-claim-atomic.db"
    app = create_app({"DB_PATH": str(db_path), "AUTO_LARK_REPLY": False, "INGRESS_WORKER_ENABLED": False})
    client = TestClient(app)
    service = app.state.service

    payload = {
        'registration_group': '8️⃣5️⃣',
        'decision': 'approve',
        'decided_at': '2026-04-27T07:26:05.137945+00:00',
        'decided_by': 'Hermes',
        'decided_by_name': 'Song Yuqi',
        'approved_count': 2,
        'area': 'Indonesia',
        'remark': 'atomic ingress claim test',
        'force_immediate': True,
    }
    accepted = client.post('/api/registration-groups/approval-decisions', json=payload)
    assert accepted.status_code == 200
    approval_run_id = accepted.json()['approval_run_id']

    original_sync = service._registration_group_approval_decision_sync
    calls = []

    def wrapped_sync(*args, **kwargs):
        calls.append(kwargs.get('approval_run_id'))
        time.sleep(0.05)
        return {
            'registration_group': '8️⃣5️⃣',
            'decision': 'approve',
            'approval_run_id': kwargs.get('approval_run_id'),
            'executed': True,
            'verified': True,
            'verification_pending': False,
            'crm_recorded': True,
            'status': 'success',
            'result_code': 'approved',
            'result_reason': 'stubbed success',
            'approved_count': 2,
            'approved_at': '2026-04-27T07:26:31.077Z',
            'elapsed_seconds': 0.05,
            'crm_elapsed_seconds': 0.0,
            'total_elapsed_seconds': 0.05,
            'force_immediate': True,
            'target_member': {},
            'evidence_summary': {},
            'raw_result': {},
            'crm_batch': {'accepted': True},
        }

    service._registration_group_approval_decision_sync = wrapped_sync
    results = []
    errors = []

    def worker():
        try:
            results.append(service.process_next_ingress_job())
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    service._registration_group_approval_decision_sync = original_sync

    assert not errors
    assert sum(1 for row in results if row) == 1
    assert calls == [approval_run_id]

    with sqlite3.connect(str(db_path)) as conn:
        job = conn.execute(
            "SELECT attempt_count, status FROM ingress_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        audit_rows = conn.execute(
            "SELECT event_type FROM operator_audit_log ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    assert job[0] == 1
    assert job[1] == 'done'
    assert sum(1 for row in audit_rows if row[0] == 'ingress_event_processed') == 1


class StubRegistrationGroupApprovalExecutor:
    def __init__(self, result=None, *, group_state_result=None):
        self.calls = []
        self.group_state_calls = []
        self.result = result or {
            'status': 'success',
            'verified': True,
            'result_code': 'approved',
            'result_reason': 'verified',
            'finished_at': '2026-04-22T07:03:11.784759+00:00',
            'approved_at': '2026-04-22T07:03:11.784759+00:00',
            'approved_count': 1,
            'elapsed_seconds': 8.4,
            'queue_delta': True,
            'member_confirmed': True,
            'target_member': {
                'name': '~Eastion',
                'phone_raw': '+86 138 6064 0933',
                'phone_normalized': '+861****0933',
            },
            'raw_result': {
                'pending_before': 1,
                'pending_after': 0,
                'member_count_before': 4,
                'member_count_after': 5,
            },
        }
        self.group_state_result = group_state_result or {
            'group_name': '8️⃣5️⃣',
            'group_id': 'group_stub',
            'pending_count': 1,
            'member_count': 5,
            'requester_ids': ['req-1@lid'],
            'requesters': [
                {'requesterId': 'req-1@lid', 'requestedAtUnix': 100},
            ],
        }

    def health(self):
        return {
            'configured': True,
            'status': 'warm',
            'provider': 'stub',
            'schema_version': 'stub-v1',
            'supports': ['approve', 'strict_queue_and_member_verify'],
        }

    def group_state(self, registration_group):
        self.group_state_calls.append(registration_group)
        return dict(self.group_state_result)

    def approve(self, context):
        self.calls.append(context)
        return dict(self.result)


def test_registration_group_approval_decision_prefers_matching_account_runtime_executor():
    from app.main import create_app

    crm = StubCrmAdapter()
    fallback_executor = StubRegistrationGroupApprovalExecutor()
    runtime_executor = StubRegistrationGroupApprovalExecutor(
        group_state_result={
            'group_name': 'RG-RUNTIME',
            'group_id': 'rg-runtime-group-id',
            'pending_count': 2,
            'member_count': 10,
            'requester_ids': ['runtime-req@lid'],
            'requesters': [{'requesterId': 'runtime-req@lid', 'requestedAtUnix': 100}],
        }
    )
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': fallback_executor,
    })
    client = TestClient(app)

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-reg-runtime', json={
        'account_name': 'WA Reg Runtime',
        'responsible_type': 'registration_group',
        'group_link_bindings': [{
            'link': 'https://chat.whatsapp.com/reg-runtime',
            'area': 'Indonesia',
            'notify_profile_name': 'wa-approval-broadcast',
            'registration_group': 'RG-RUNTIME',
            'group_id': 'rg-runtime-group-id',
            'approval_count_threshold': 30,
            'approval_timeout_minutes': 30,
            'auto_recover_worker': True,
            'schedule_windows': [{'start': '00:00', 'end': '23:59'}],
        }],
        'enabled': True,
        'notes': 'registration runtime route',
    })
    assert saved.status_code == 200

    def fake_runtime_state(self, account_key, *, worker_health=None, allow_shared_fallback=True):
        if account_key == 'wa-reg-runtime':
            return {
                'account_key': account_key,
                'active': True,
                'base_url': 'http://127.0.0.1:18888',
                'source': 'dedicated',
                'status': 'warm',
                'ready': True,
                'authenticated': True,
                'session_target_match': True,
                'status_text': 'dedicated runtime ready',
            }
        return {'account_key': account_key, 'active': False, 'base_url': None, 'source': 'shared', 'status': 'shared'}

    with patch('app.main.Service._build_whatsapp_approval_runtime_state', new=fake_runtime_state), patch('app.main.Service._build_runtime_registration_group_executor', return_value=runtime_executor):
        response = client.post(
            '/api/registration-groups/approval-decisions',
            json={
                'registration_group': 'RG-RUNTIME',
                'decided_at': '2026-04-22T07:00:36.073643+00:00',
                'decided_by': 'system:test',
                'decided_by_name': 'Hermes Test',
                'approved_count': 1,
                'area': 'Indonesia',
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body['executed'] is True
    assert runtime_executor.group_state_calls == ['RG-RUNTIME']
    assert runtime_executor.calls[0]['registration_group'] == 'RG-RUNTIME'
    assert fallback_executor.group_state_calls == []
    assert fallback_executor.calls == []


def test_registration_group_approval_decision_ignores_unmonitored_binding_for_runtime_routing():
    from app.main import create_app

    crm = StubCrmAdapter()
    fallback_executor = StubRegistrationGroupApprovalExecutor({
        'status': 'success',
        'verified': True,
        'result_code': 'approved',
        'result_reason': 'fallback executor used',
        'approved_count': 1,
        'raw_result': {'executor': 'fallback'},
    }, group_state_result={
        'group_name': 'RG-DISABLED',
        'group_id': 'rg-disabled-group-id',
        'pending_count': 1,
        'member_count': 8,
        'requester_ids': ['fallback-req@lid'],
        'requesters': [{'requesterId': 'fallback-req@lid', 'requestedAtUnix': 100}],
    })
    runtime_executor = StubRegistrationGroupApprovalExecutor(
        group_state_result={
            'group_name': 'RG-DISABLED',
            'group_id': 'rg-disabled-group-id',
            'pending_count': 2,
            'member_count': 10,
            'requester_ids': ['runtime-req@lid'],
            'requesters': [{'requesterId': 'runtime-req@lid', 'requestedAtUnix': 100}],
        }
    )
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': fallback_executor,
    })
    client = TestClient(app)

    saved = client.post('/api/ops/whatsapp-approval-accounts/wa-reg-disabled', json={
        'account_name': 'WA Reg Disabled',
        'responsible_type': 'registration_group',
        'group_link_bindings': [{
            'link': 'https://chat.whatsapp.com/reg-disabled',
            'area': 'Indonesia',
            'notify_profile_name': 'wa-approval-broadcast',
            'enabled': False,
            'registration_group': 'RG-DISABLED',
            'group_id': 'rg-disabled-group-id',
            'approval_count_threshold': 30,
            'approval_timeout_minutes': 30,
            'auto_recover_worker': True,
            'schedule_windows': [{'start': '00:00', 'end': '23:59'}],
        }],
        'enabled': True,
        'notes': 'registration runtime route disabled binding',
    })
    assert saved.status_code == 200

    def fake_runtime_state(self, account_key, *, worker_health=None, allow_shared_fallback=True):
        if account_key == 'wa-reg-disabled':
            return {
                'account_key': account_key,
                'active': True,
                'base_url': 'http://127.0.0.1:18890',
                'source': 'dedicated',
                'status': 'warm',
                'ready': True,
                'authenticated': True,
                'session_target_match': True,
                'status_text': 'dedicated runtime ready',
            }
        return {'account_key': account_key, 'active': False, 'base_url': None, 'source': 'shared', 'status': 'shared'}

    with patch('app.main.Service._build_whatsapp_approval_runtime_state', new=fake_runtime_state), patch('app.main.Service._build_runtime_registration_group_executor', return_value=runtime_executor):
        response = client.post(
            '/api/registration-groups/approval-decisions',
            json={
                'registration_group': 'RG-DISABLED',
                'decided_at': '2026-04-22T07:00:36.073643+00:00',
                'decided_by': 'system:test',
                'decided_by_name': 'Hermes Test',
                'approved_count': 1,
                'area': 'Indonesia',
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body['executed'] is True
    assert runtime_executor.group_state_calls == []
    assert runtime_executor.calls == []
    assert fallback_executor.group_state_calls == ['RG-DISABLED']
    assert fallback_executor.calls[0]['registration_group'] == 'RG-DISABLED'


def test_registration_group_approval_decision_executes_executor_and_records_crm_batch():
    from app.main import create_app

    crm = StubCrmAdapter()
    executor = StubRegistrationGroupApprovalExecutor({
        'status': 'success',
        'verified': True,
        'result_code': 'approved',
        'result_reason': 'verified',
        'finished_at': '2026-04-22T07:03:11.784759+00:00',
        'approved_at': '2026-04-22T07:03:11.784759+00:00',
        'approved_count': 1,
        'elapsed_seconds': 8.4,
        'queue_delta': True,
        'member_confirmed': True,
        'target_member': {
            'name': '~Eastion',
            'phone_raw': '+86 138 6064 0933',
            'phone_normalized': '+861****0933',
        },
        'raw_result': {
            'pending_before': 2,
            'pending_after': 0,
            'member_count_before': 4,
            'member_count_after': 6,
        },
    })
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })
    client = TestClient(app)

    response = client.post(
        '/api/registration-groups/approval-decisions',
        json={
            'registration_group': '8️⃣5️⃣',
            'decided_at': '2026-04-22T07:00:36.073643+00:00',
            'decided_by': 'system:test',
            'decided_by_name': 'Hermes Test',
            'source_platform': 'whatsapp',
            'source_campaign': 'registration_group_live_prod_test_force_approve',
            'source_adset': '8️⃣5️⃣',
            'source_ad': '~Eastion +86 138 6064 0933',
            'target_name_hint': '~Eastion',
            'target_phone_hint': '+86 138 6064 0933',
            'approved_count': 1,
            'area': 'Indonesia',
            'remark': 'forced approval by operator instruction',
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['executed'] is True
    assert body['verified'] is True
    assert body['crm_recorded'] is True
    assert body['crm_batch']['crm_sync_status'] == 'success'
    assert body['elapsed_seconds'] == 8.4
    assert body['approval_run_id'].startswith('registration_group_approval_')
    assert body['crm_batch']['approval_run_id'] == body['approval_run_id']
    assert body['crm_batch']['request_snapshot']['approval_run_id'] == body['approval_run_id']
    assert executor.calls[0]['registration_group'] == '8️⃣5️⃣'
    assert executor.calls[0]['approval_run_id'] == body['approval_run_id']
    assert executor.calls[0]['target_name_hint'] == '~Eastion'
    assert executor.calls[0]['target_phone_hint'] == '+86 138 6064 0933'
    assert body['approved_count'] == 2
    assert ('create_registration_group_batch', {'area': 'Indonesia', 'groupNo': '8️⃣5️⃣', 'groupPeopleNum': '2'}) in crm.calls


def test_registration_group_approval_decision_uses_resolved_group_name_for_crm_batch_when_request_uses_invite_link():
    from app.main import create_app

    crm = StubCrmAdapter()
    executor = StubRegistrationGroupApprovalExecutor(
        result={
            'status': 'success',
            'verified': True,
            'result_code': 'approved',
            'result_reason': 'verified',
            'finished_at': '2026-04-29T05:20:10.311452+00:00',
            'approved_at': '2026-04-29T05:20:10.311452+00:00',
            'approved_count': 2,
            'elapsed_seconds': 9.1,
            'queue_delta': True,
            'member_confirmed': True,
            'raw_result': {
                'group_name': '8️⃣5️⃣',
                'pending_before': 2,
                'pending_after': 0,
                'member_count_before': 4,
                'member_count_after': 6,
            },
        },
        group_state_result={
            'group_name': '8️⃣5️⃣',
            'group_id': '120363423424902684@g.us',
            'pending_count': 2,
            'member_count': 4,
            'requester_ids': ['req-a@lid', 'req-b@lid'],
            'requesters': [
                {'requesterId': 'req-a@lid', 'requestedAtUnix': 100},
                {'requesterId': 'req-b@lid', 'requestedAtUnix': 101},
            ],
        },
    )
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })
    client = TestClient(app)

    response = client.post(
        '/api/registration-groups/approval-decisions',
        json={
            'registration_group': 'https://chat.whatsapp.com/Bp1WKsmpcbC2RkAyIACeRv',
            'decided_at': '2026-04-29T05:20:01.693968+00:00',
            'approved_count': 2,
            'area': 'Indonesia',
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['verified'] is True
    assert body['crm_recorded'] is True
    assert body['crm_batch']['crm_payload']['groupNo'] == '8️⃣5️⃣'
    assert body['crm_batch']['request_snapshot']['registration_group'] == 'https://chat.whatsapp.com/Bp1WKsmpcbC2RkAyIACeRv'
    assert body['crm_batch']['request_snapshot']['registration_group_name'] == '8️⃣5️⃣'
    assert ('create_registration_group_batch', {'area': 'Indonesia', 'groupNo': '8️⃣5️⃣', 'groupPeopleNum': '2'}) in crm.calls


def test_registration_group_approval_decision_fails_closed_when_expected_requester_fingerprint_changes_before_execute():
    from app.main import create_app

    crm = StubCrmAdapter()
    executor = StubRegistrationGroupApprovalExecutor(
        group_state_result={
            'group_name': '8️⃣5️⃣',
            'group_id': 'group_stub',
            'pending_count': 2,
            'member_count': 4,
            'requester_ids': ['live-a@lid', 'live-b@lid'],
            'requesters': [
                {'requesterId': 'live-a@lid', 'requestedAtUnix': 200},
                {'requesterId': 'live-b@lid', 'requestedAtUnix': 300},
            ],
        }
    )
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })
    client = TestClient(app)

    response = client.post(
        '/api/registration-groups/approval-decisions',
        json={
            'registration_group': '8️⃣5️⃣',
            'decided_at': '2026-04-28T08:30:00+00:00',
            'approved_count': 2,
            'area': 'Indonesia',
            'expected_pending_count': 2,
            'expected_member_count': 4,
            'expected_requester_ids': ['old-a@lid', 'old-b@lid'],
            'expected_requesters': [
                {'requesterId': 'old-a@lid', 'requestedAtUnix': 100},
                {'requesterId': 'old-b@lid', 'requestedAtUnix': 101},
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['executed'] is False
    assert body['verified'] is False
    assert body['verification_pending'] is False
    assert body['crm_recorded'] is False
    assert body['result_code'] == 'requester_fingerprint_changed_before_approval'
    assert body['raw_result']['expected_group_state']['requester_ids'] == ['old-a@lid', 'old-b@lid']
    assert body['raw_result']['current_group_state']['requester_ids'] == ['live-a@lid', 'live-b@lid']
    assert executor.group_state_calls == ['8️⃣5️⃣']
    assert executor.calls == []
    assert all(name != 'create_registration_group_batch' for name, _ in crm.calls)


def test_registration_group_approval_decision_writes_crm_when_queue_is_fully_consumed_despite_short_success_count():
    from app.main import create_app

    crm = StubCrmAdapter()
    executor = StubRegistrationGroupApprovalExecutor({
        'status': 'success',
        'verified': True,
        'result_code': 'approved',
        'result_reason': 'verified',
        'finished_at': '2026-04-27T06:28:07.893Z',
        'approved_at': '2026-04-27T06:28:07.893Z',
        'approved_count': 1,
        'elapsed_seconds': 16.962,
        'queue_delta': True,
        'member_confirmed': True,
        'target_member': {
            'phone_raw': '+216****9549@lid',
            'phone_normalized': '+216****9549@lid',
            'requester_id': '216067590889549@lid',
        },
        'raw_result': {
            'pending_before': 2,
            'pending_after': 0,
            'member_count_before': 4,
            'member_count_after': 5,
            'approval_results': [
                {'requesterId': '216067590889549@lid', 'message': 'Approved successfully'},
                {'requesterId': '64163187581105@lid', 'error': 404, 'message': 'ParticipantRequestNotFoundError'},
            ],
        },
    })
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })
    client = TestClient(app)

    response = client.post(
        '/api/registration-groups/approval-decisions',
        json={
            'registration_group': '8️⃣5️⃣',
            'decided_at': '2026-04-27T06:27:50.786630+00:00',
            'decided_by': 'Hermes',
            'decided_by_name': 'Song Yuqi',
            'approved_count': 2,
            'area': 'Indonesia',
            'remark': 'post-dedupe live verify',
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['executed'] is True
    assert body['verified'] is True
    assert body['verification_pending'] is False
    assert body['crm_recorded'] is True
    assert body['status'] == 'success'
    assert body['result_code'] == 'approved'
    assert body['approved_count'] == 2
    assert body['raw_result']['verification_consistency_error'] == 'batch_success_count_mismatch'
    assert body['raw_result']['verification_consistency_detail'] == {
        'requested_approved_count': 2,
        'approved_success_count': 1,
        'resolved_approved_count': 2,
        'resolution': 'queue_consumed',
    }
    create_batch_payload = next(payload for name, payload in crm.calls if name == 'create_registration_group_batch')
    assert create_batch_payload['groupPeopleNum'] == '2'


def test_file_backed_registration_group_approval_decision_queues_for_async_processing_and_exposes_status(tmp_path):
    from app.main import create_app

    crm = StubCrmAdapter()
    executor = StubRegistrationGroupApprovalExecutor()
    app = create_app({
        'DB_PATH': str(tmp_path / 'registration-approval.db'),
        'CRM_ADAPTER': crm,
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
        'INGRESS_WORKER_ENABLED': False,
    })
    client = TestClient(app)

    queued = client.post(
        '/api/registration-groups/approval-decisions',
        json={
            'registration_group': '8️⃣5️⃣',
            'decided_at': '2026-04-22T07:00:36.073643+00:00',
            'source_platform': 'whatsapp',
            'target_phone_hint': '+86 138 6064 0933',
        },
    )

    assert queued.status_code == 200
    queued_body = queued.json()
    assert queued_body['accepted'] is True
    assert queued_body['queued'] is True
    assert queued_body['next_action'] == 'queued_for_processing'
    assert queued_body['status'] == 'queued'
    assert queued_body['approval_run_id'].startswith('registration_group_approval_')
    assert queued_body['ingress_event_id']

    queue_rows = client.get('/api/ops/ingress-queue').json()['rows']
    assert queue_rows[0]['ingress_type'] == 'registration_group_approval_decision'
    assert queue_rows[0]['status'] == 'queued'

    processed = client.post('/api/ops/ingress-queue/run-next').json()
    assert processed['status'] == 'done'
    assert processed['result']['verified'] is True
    assert processed['result']['crm_recorded'] is True
    assert processed['result']['approval_run_id'] == queued_body['approval_run_id']

    status = client.get(f"/api/registration-groups/approval-decisions/{queued_body['approval_run_id']}")
    assert status.status_code == 200
    status_body = status.json()
    assert status_body['status'] == 'done'
    assert status_body['result']['approval_run_id'] == queued_body['approval_run_id']
    assert status_body['result']['verified'] is True


def test_registration_group_approval_decision_does_not_write_crm_when_verification_fails():
    from app.main import create_app

    crm = StubCrmAdapter()
    executor = StubRegistrationGroupApprovalExecutor({
        'status': 'failed',
        'verified': False,
        'result_code': 'approval_not_verified',
        'result_reason': 'strict verification failed after approve click',
        'finished_at': '2026-04-22T07:03:11.784759+00:00',
        'approved_count': 1,
        'elapsed_seconds': 9.1,
        'queue_delta': False,
        'member_confirmed': False,
        'target_member': {
            'name': '~Eastion',
            'phone_raw': '+86 138 6064 0933',
            'phone_normalized': '+861****0933',
        },
        'raw_result': {
            'pending_before': 1,
            'pending_after': 1,
            'member_count_before': 4,
            'member_count_after': 4,
        },
    })
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })
    client = TestClient(app)

    response = client.post(
        '/api/registration-groups/approval-decisions',
        json={
            'registration_group': '8️⃣5️⃣',
            'decided_at': '2026-04-22T07:00:36.073643+00:00',
            'source_platform': 'whatsapp',
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['executed'] is True
    assert body['verified'] is False
    assert body['crm_recorded'] is False
    assert body['verification_pending'] is False
    assert all(name != 'create_registration_group_batch' for name, _ in crm.calls)


def test_registration_group_approval_decision_does_not_write_crm_when_review_target_is_ambiguous():
    from app.main import create_app

    crm = StubCrmAdapter()
    executor = StubRegistrationGroupApprovalExecutor({
        'status': 'failed',
        'verified': False,
        'result_code': 'ambiguous_review_target',
        'result_reason': 'multiple actionable review rows remained without a unique exact match',
        'finished_at': '2026-04-22T07:03:11.784759+00:00',
        'approved_count': 1,
        'elapsed_seconds': 7.3,
        'queue_delta': False,
        'member_confirmed': False,
        'raw_result': {
            'pending_before': 2,
            'member_count_before': 4,
            'candidate_rows': [
                {'index': 0, 'display_name': '~G2', 'phones': ['+852****8277'], 'actionable': True},
                {'index': 1, 'display_name': '~G3', 'phones': ['+852****8899'], 'actionable': True},
            ],
        },
    })
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })
    client = TestClient(app)

    response = client.post(
        '/api/registration-groups/approval-decisions',
        json={
            'registration_group': '8️⃣5️⃣',
            'decided_at': '2026-04-22T07:00:36.073643+00:00',
            'source_platform': 'whatsapp',
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['verified'] is False
    assert body['verification_pending'] is False
    assert body['result_code'] == 'ambiguous_review_target'
    assert body['crm_recorded'] is False
    assert all(name != 'create_registration_group_batch' for name, _ in crm.calls)


def test_registration_group_approval_decision_does_not_write_crm_when_review_surface_is_stale():
    from app.main import create_app

    crm = StubCrmAdapter()
    executor = StubRegistrationGroupApprovalExecutor({
        'status': 'failed',
        'verified': False,
        'result_code': 'stale_review_surface',
        'result_reason': 'review surface candidates do not match current pending candidates',
        'finished_at': '2026-04-22T07:03:11.784759+00:00',
        'approved_count': 1,
        'elapsed_seconds': 6.8,
        'queue_delta': False,
        'member_confirmed': False,
        'raw_result': {
            'pending_before': 2,
            'member_count_before': 4,
            'candidate_rows': [
                {'index': 0, 'display_name': '~Old1', 'phones': ['+861****0933'], 'actionable': True},
                {'index': 1, 'display_name': '~Old2', 'phones': ['+852****8277'], 'actionable': True},
            ],
        },
    })
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })
    client = TestClient(app)

    response = client.post(
        '/api/registration-groups/approval-decisions',
        json={
            'registration_group': '8️⃣5️⃣',
            'decided_at': '2026-04-22T07:00:36.073643+00:00',
            'source_platform': 'whatsapp',
            'target_phone_hint': '+852 6775 5475',
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['verified'] is False
    assert body['verification_pending'] is False
    assert body['result_code'] == 'stale_review_surface'
    assert body['crm_recorded'] is False
    assert all(name != 'create_registration_group_batch' for name, _ in crm.calls)


def test_registration_group_approval_decision_marks_consumed_waiting_verification_when_queue_was_consumed():
    from app.main import create_app

    crm = StubCrmAdapter()
    executor = StubRegistrationGroupApprovalExecutor({
        'status': 'failed',
        'verified': False,
        'result_code': 'approval_not_verified',
        'result_reason': 'strict verification failed after approve click',
        'finished_at': '2026-04-22T07:03:11.784759+00:00',
        'approved_count': 2,
        'elapsed_seconds': 11.2,
        'queue_delta': True,
        'member_confirmed': False,
        'target_member': {
            'name': '~Eastion',
            'phone_raw': '+86 138 6064 0933',
            'phone_normalized': '+861****0933',
        },
        'raw_result': {
            'pending_before': 2,
            'pending_after': 0,
            'member_count_before': 4,
            'member_count_after': 6,
            'verification_excerpt': 'queue drained but target phone not yet visible',
        },
    })
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })
    client = TestClient(app)

    response = client.post(
        '/api/registration-groups/approval-decisions',
        json={
            'registration_group': '8️⃣5️⃣',
            'decided_at': '2026-04-22T07:00:36.073643+00:00',
            'source_platform': 'whatsapp',
            'approved_count': 2,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['verified'] is False
    assert body['verification_pending'] is True
    assert body['status'] == 'pending_verification'
    assert body['result_code'] == 'approval_consumed_waiting_verification'
    assert body['crm_recorded'] is False
    assert body['evidence_summary']['queue_delta'] is True
    assert body['evidence_summary']['member_count_delta'] == 2
    assert all(name != 'create_registration_group_batch' for name, _ in crm.calls)


def test_registration_group_approval_decision_records_crm_when_timeout_still_salvages_full_verification_evidence():
    from app.main import create_app

    crm = StubCrmAdapter()
    executor = StubRegistrationGroupApprovalExecutor({
        'status': 'success',
        'verified': True,
        'result_code': 'approved',
        'result_reason': 'queue delta and member confirmation verified after timeout salvage',
        'finished_at': '2026-04-22T07:03:11.784759+00:00',
        'approved_count': 2,
        'elapsed_seconds': 18.4,
        'queue_delta': True,
        'member_confirmed': True,
        'target_member': {
            'name': '~G2',
            'phone_raw': '+852 4456 8277',
            'phone_normalized': '+852****8277',
        },
        'raw_result': {
            'pending_before': 2,
            'pending_after': 0,
            'member_count_before': 4,
            'member_count_after': 6,
            'verification_excerpt': '联系人信息\n+852 4456 8277\n6位成员',
        },
    })
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })
    client = TestClient(app)

    response = client.post(
        '/api/registration-groups/approval-decisions',
        json={
            'registration_group': '8️⃣5️⃣',
            'decided_at': '2026-04-22T07:00:36.073643+00:00',
            'source_platform': 'whatsapp',
            'approved_count': 2,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['verified'] is True
    assert body['verification_pending'] is False
    assert body['status'] == 'success'
    assert body['result_code'] == 'approved'
    assert body['crm_recorded'] is True
    assert body['evidence_summary']['queue_delta'] is True
    assert body['evidence_summary']['member_confirmed'] is True
    assert body['evidence_summary']['member_count_delta'] == 2
    assert ('create_registration_group_batch', {'area': 'Indonesia', 'groupNo': '8️⃣5️⃣', 'groupPeopleNum': '2'}) in crm.calls


class SlowStubCrmAdapter(StubCrmAdapter):
    def __init__(self, delay_seconds=0.65):
        super().__init__()
        self.delay_seconds = delay_seconds

    def create_registration_group_batch(self, payload):
        import time
        time.sleep(self.delay_seconds)
        return super().create_registration_group_batch(payload)


class SlowStubRegistrationGroupApprovalExecutor(StubRegistrationGroupApprovalExecutor):
    def __init__(self, delay_seconds=0.55):
        super().__init__()
        self.delay_seconds = delay_seconds

    def approve(self, context):
        import time
        time.sleep(self.delay_seconds)
        return super().approve(context)


def test_registration_group_approval_decision_reports_total_elapsed_including_crm_write():
    from app.main import create_app

    crm = SlowStubCrmAdapter(delay_seconds=0.35)
    executor = SlowStubRegistrationGroupApprovalExecutor(delay_seconds=0.25)
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })
    client = TestClient(app)

    response = client.post(
        '/api/registration-groups/approval-decisions',
        json={
            'registration_group': '8️⃣5️⃣',
            'decided_at': '2026-04-22T07:00:36.073643+00:00',
            'area': 'Indonesia',
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body['verified'] is True
    assert body['crm_recorded'] is True
    assert body['total_elapsed_seconds'] >= 0.55
    assert body['total_elapsed_seconds'] < 2.5
    assert body['crm_elapsed_seconds'] >= 0.3


def test_create_app_warms_registration_group_executor_when_supported():
    from app.main import create_app

    class WarmableExecutor(StubRegistrationGroupApprovalExecutor):
        def __init__(self):
            super().__init__()
            self.warmup_calls = 0

        def warmup(self):
            self.warmup_calls += 1
            return {'warmed': True}

    executor = WarmableExecutor()
    app = create_app({
        'DB_PATH': ':memory:',
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })

    assert app.state.service.registration_group_approval_executor is executor
    assert executor.warmup_calls == 1


def test_create_app_warms_registration_group_executor_from_background_thread_inside_asyncio_loop():
    from app.main import create_app

    class LoopSensitiveExecutor(StubRegistrationGroupApprovalExecutor):
        def __init__(self):
            super().__init__()
            self.warmup_contexts = []
            self.warmup_event = threading.Event()

        def warmup(self):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                self.warmup_contexts.append('outside_asyncio_loop')
            else:
                self.warmup_contexts.append('inside_asyncio_loop')
            self.warmup_event.set()
            return {'warmed': True}

    executor = LoopSensitiveExecutor()

    async def build_app_inside_asyncio_loop():
        return create_app({
            'DB_PATH': ':memory:',
            'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
        })

    holder = {}

    def run_inside_thread():
        holder['app'] = asyncio.run(build_app_inside_asyncio_loop())

    worker = threading.Thread(target=run_inside_thread)
    worker.start()
    worker.join(timeout=2)

    assert worker.is_alive() is False
    app = holder['app']

    assert app.state.service.registration_group_approval_executor is executor
    assert executor.warmup_event.wait(timeout=2) is True
    assert executor.warmup_contexts == ['outside_asyncio_loop']


def test_registration_group_approval_executor_health_reports_configured_executor():
    from app.main import create_app

    app = create_app({
        'DB_PATH': ':memory:',
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': StubRegistrationGroupApprovalExecutor(),
    })
    client = TestClient(app)

    response = client.get('/api/ops/registration-group-approval-executor-health')
    assert response.status_code == 200
    body = response.json()
    assert body['configured'] is True
    assert body['status'] == 'warm'
    assert body['provider'] == 'stub'


def test_registration_group_approval_executor_warmup_endpoint_calls_executor_warmup():
    from app.main import create_app

    class WarmableExecutor(StubRegistrationGroupApprovalExecutor):
        def __init__(self):
            super().__init__({'status': 'idle', 'provider': 'warmable_stub'})
            self.warmup_calls = 0

        def warmup(self):
            self.warmup_calls += 1
            self.result['status'] = 'warm'
            return self.health()

    executor = WarmableExecutor()
    app = create_app({
        'DB_PATH': ':memory:',
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })
    client = TestClient(app)

    response = client.post('/api/ops/registration-group-approval-executor-warmup')
    assert response.status_code == 200
    body = response.json()
    assert body['configured'] is True
    assert body['status'] == 'warm'
    assert body['warmed'] is True
    assert executor.warmup_calls >= 1


def test_live_whatsapp_registration_group_executor_uses_fast_default_timing_profile(monkeypatch):
    import types
    import sys
    from app.main import create_app

    class FakeExecutor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def health(self):
            return {
                'configured': True,
                'status': 'idle',
                'provider': 'fake_live_whatsapp',
                'timing_profile': {
                    'initial_wait_ms': self.kwargs.get('initial_wait_ms'),
                    'navigation_wait_ms': self.kwargs.get('navigation_wait_ms'),
                    'post_click_wait_ms': self.kwargs.get('post_click_wait_ms'),
                    'verify_timeout_ms': self.kwargs.get('verify_timeout_ms'),
                    'verify_poll_ms': self.kwargs.get('verify_poll_ms'),
                    'strict_reload_verify': self.kwargs.get('strict_reload_verify'),
                },
            }

    fake_module = types.ModuleType('app.registration_group_executor')
    fake_module.LiveWarmWhatsAppRegistrationGroupApprovalExecutor = FakeExecutor
    monkeypatch.setitem(sys.modules, 'app.registration_group_executor', fake_module)

    app = create_app({
        'DB_PATH': ':memory:',
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR_KIND': 'live_whatsapp',
        'AUTO_LARK_REPLY': False,
    })
    client = TestClient(app)

    response = client.get('/api/ops/registration-group-approval-executor-health')
    assert response.status_code == 200
    body = response.json()
    timing = body['timing_profile']
    assert timing['initial_wait_ms'] <= 600
    assert timing['navigation_wait_ms'] <= 200
    assert timing['post_click_wait_ms'] <= 150
    assert timing['verify_timeout_ms'] <= 1800
    assert timing['strict_reload_verify'] is False


def test_webjs_bridge_registration_group_executor_posts_context_and_reports_health():
    class StubResponse:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def json(self):
            return self._payload

    class StubSession:
        def __init__(self):
            self.calls = []

        def get(self, url, timeout):
            self.calls.append(('get', url, timeout, None, None))
            return StubResponse({
                'status': 'warm',
                'provider': 'whatsapp_webjs_bridge',
                'supports': ['approve', 'strict_queue_and_member_verify', 'crm_batch_writeback_ready'],
            })

        def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append(('post', url, timeout, json, headers))
            if url.endswith('/approve'):
                return StubResponse({
                    'status': 'success',
                    'verified': True,
                    'result_code': 'approved',
                    'result_reason': 'bridge approved',
                    'approved_count': 2,
                    'approved_at': '2026-04-27T03:20:00+00:00',
                    'elapsed_seconds': 1.8,
                    'target_member': {'name': '~G2', 'phone_raw': '+852 6775 5475'},
                    'raw_result': {'pending_before': 2, 'pending_after': 0},
                })
            return StubResponse({'status': 'warm', 'provider': 'whatsapp_webjs_bridge'})

    from app.registration_group_webjs_executor import WebjsBridgeRegistrationGroupApprovalExecutor

    session = StubSession()
    executor = WebjsBridgeRegistrationGroupApprovalExecutor(
        base_url='http://127.0.0.1:8787',
        token='secret-token',
        session=session,
        timeout_seconds=9,
    )

    health = executor.health()
    assert health['configured'] is True
    assert health['status'] == 'warm'
    assert health['provider'] == 'whatsapp_webjs_bridge'

    result = executor.approve({'registration_group': '8️⃣5️⃣', 'approval_run_id': 'registration_group_approval_bridge_1'})
    assert result['verified'] is True
    assert result['approved_count'] == 2
    assert result['raw_result']['pending_after'] == 0
    assert session.calls[0][0] == 'get'
    assert session.calls[1][0] == 'post'
    assert session.calls[1][1] == 'http://127.0.0.1:8787/approve'
    assert session.calls[1][3]['approval_run_id'] == 'registration_group_approval_bridge_1'
    assert session.calls[1][4]['Authorization'] == 'Bearer secret-token'


def test_webjs_bridge_registration_group_executor_fetches_group_state():
    class StubResponse:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def json(self):
            return self._payload

    class StubSession:
        def __init__(self):
            self.calls = []

        def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append(('post', url, timeout, json, headers))
            return StubResponse({
                'group_id': '120363423424902684@g.us',
                'group_name': '8️⃣5️⃣',
                'pending_count': 2,
                'member_count': 4,
                'requester_ids': ['216067590889549@lid', '64163187581105@lid'],
            })

    from app.registration_group_webjs_executor import WebjsBridgeRegistrationGroupApprovalExecutor

    session = StubSession()
    executor = WebjsBridgeRegistrationGroupApprovalExecutor(
        base_url='http://127.0.0.1:8787',
        token='secret-token',
        session=session,
        timeout_seconds=9,
    )

    result = executor.group_state('8️⃣5️⃣')
    assert result['pending_count'] == 2
    assert result['member_count'] == 4
    assert result['group_name'] == '8️⃣5️⃣'
    assert session.calls[0][1] == 'http://127.0.0.1:8787/group-state'
    assert session.calls[0][3]['registration_group'] == '8️⃣5️⃣'
    assert session.calls[0][4]['Authorization'] == 'Bearer secret-token'


def test_create_app_supports_webjs_bridge_registration_group_executor_kind(monkeypatch):
    import types
    import sys
    from app.main import create_app

    class FakeBridgeExecutor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def health(self):
            return {
                'configured': True,
                'status': 'warm',
                'provider': 'fake_webjs_bridge',
                'base_url': self.kwargs.get('base_url'),
                'timeout_seconds': self.kwargs.get('timeout_seconds'),
            }

        def warmup(self):
            return self.health()

    fake_module = types.ModuleType('app.registration_group_webjs_executor')
    fake_module.WebjsBridgeRegistrationGroupApprovalExecutor = FakeBridgeExecutor
    monkeypatch.setitem(sys.modules, 'app.registration_group_webjs_executor', fake_module)

    app = create_app({
        'DB_PATH': ':memory:',
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR_KIND': 'webjs_bridge',
        'REGISTRATION_GROUP_APPROVAL_WEBJS_BASE_URL': 'http://127.0.0.1:8787',
        'REGISTRATION_GROUP_APPROVAL_WEBJS_TIMEOUT_SECONDS': 12,
        'AUTO_LARK_REPLY': False,
    })
    client = TestClient(app)

    response = client.get('/api/ops/registration-group-approval-executor-health')
    assert response.status_code == 200
    body = response.json()
    assert body['provider'] == 'fake_webjs_bridge'
    assert body['base_url'] == 'http://127.0.0.1:8787'
    assert body['timeout_seconds'] == 12.0


def test_create_app_webjs_bridge_uses_safer_default_timeout(monkeypatch):
    import types
    import sys
    from app.main import create_app

    class FakeBridgeExecutor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def health(self):
            return {
                'configured': True,
                'status': 'warm',
                'provider': 'fake_webjs_bridge',
                'base_url': self.kwargs.get('base_url'),
                'timeout_seconds': self.kwargs.get('timeout_seconds'),
            }

        def warmup(self):
            return self.health()

    fake_module = types.ModuleType('app.registration_group_webjs_executor')
    fake_module.WebjsBridgeRegistrationGroupApprovalExecutor = FakeBridgeExecutor
    monkeypatch.setitem(sys.modules, 'app.registration_group_webjs_executor', fake_module)

    app = create_app({
        'DB_PATH': ':memory:',
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR_KIND': 'webjs_bridge',
        'REGISTRATION_GROUP_APPROVAL_WEBJS_BASE_URL': 'http://127.0.0.1:8787',
        'AUTO_LARK_REPLY': False,
    })
    client = TestClient(app)

    response = client.get('/api/ops/registration-group-approval-executor-health')
    assert response.status_code == 200
    body = response.json()
    assert body['timeout_seconds'] == 35.0


def test_registration_group_approval_executor_group_state_endpoint_calls_executor():
    from app.main import create_app

    class GroupStateExecutor(StubRegistrationGroupApprovalExecutor):
        def __init__(self):
            super().__init__({'status': 'warm', 'provider': 'group_state_stub'})
            self.group_state_calls = []

        def group_state(self, registration_group: str):
            self.group_state_calls.append(registration_group)
            return {
                'group_name': registration_group,
                'group_id': 'group-123',
                'pending_count': 2,
                'member_count': 4,
                'requester_ids': ['a', 'b'],
            }

    executor = GroupStateExecutor()
    app = create_app({
        'DB_PATH': ':memory:',
        'REGISTRATION_GROUP_APPROVAL_EXECUTOR': executor,
    })
    client = TestClient(app)

    response = client.get('/api/ops/registration-group-approval-executor-group-state', params={'registration_group': '8️⃣5️⃣'})
    assert response.status_code == 200
    body = response.json()
    assert body['group_name'] == '8️⃣5️⃣'
    assert body['pending_count'] == 2
    assert executor.group_state_calls == ['8️⃣5️⃣']


def test_ops_bind_queue_returns_bind_related_leads():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-15",
            "source_platform": "meta",
            "source_page_id": "page-15",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "85556667777",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-5",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "15151515",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:10:00Z",
        },
    ).json()

    response = client.get('/api/ops/bind-queue')
    assert response.status_code == 200
    body = response.json()
    assert len(body['rows']) >= 1
    row = body['rows'][0]
    assert row['lead_id'] == lead['lead_id']
    assert row['current_status'] == 'account_submitted'
    assert row['task_id'] == submission['task_id']


def test_ops_group_queue_returns_bind_success_leads():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-16",
            "source_platform": "meta",
            "source_page_id": "page-16",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "86667778888",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-6",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "16161616",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:11:00Z",
        },
    ).json()
    bind = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T13:12:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "2010885372469563394"},
        },
    ).json()

    response = client.get('/api/ops/group-queue')
    assert response.status_code == 200
    body = response.json()
    assert len(body['rows']) >= 1
    row = body['rows'][0]
    assert row['lead_id'] == lead['lead_id']
    assert row['current_status'] == 'bind_success'
    assert row['task_id'] == bind['group_join_task_id']


def test_ops_dashboard_summary_counts_core_states():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-17",
            "source_platform": "meta",
            "source_page_id": "page-17",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "87778889999",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-7",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "17171717",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:13:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T13:14:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "2010885372469563394"},
        },
    )

    response = client.get('/api/ops/dashboard/summary')
    assert response.status_code == 200
    body = response.json()
    assert body['bind_queue_count'] >= 0
    assert body['group_queue_count'] >= 1
    assert body['bind_success_count'] >= 1


def test_ops_page_serves_minimal_console_html():
    client = make_client()
    response = client.get('/ops')
    assert response.status_code == 200
    assert 'text/html' in response.headers['content-type']
    body = response.text
    assert '运营工作台' in body
    assert 'page-shell' in body
    assert 'shell-nav' in body
    assert '工作台总览' in body
    assert '处理队列' in body
    assert '客服通知' in body
    assert 'queue-overview-grid' in body
    assert 'queue-layout' in body
    assert '/api/ops/bind-queue' in body
    assert '/api/ops/group-queue' in body
    assert '/api/ops/dashboard/summary' in body
    assert '/api/ops/operator-notifications' in body
    assert '绑定成功' in body
    assert '绑定失败' in body
    assert '入群成功' in body
    assert '入群失败' in body
    assert '查看详情' in body
    assert '客服通知列表' in body


def test_ops_next_bind_task_returns_top_bind_item():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-18",
            "source_platform": "meta",
            "source_page_id": "page-18",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000001",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-8",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "18181818",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:20:00Z",
        },
    ).json()

    response = client.get('/api/ops/next-bind-task')
    assert response.status_code == 200
    body = response.json()
    assert body['row']['lead_id'] == lead['lead_id']
    assert body['row']['task_id'] == submission['task_id']
    assert body['kind'] == 'bind'


def test_ops_next_group_task_returns_top_group_item():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-19",
            "source_platform": "meta",
            "source_page_id": "page-19",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000002",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-9",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead["lead_id"],
            "submission_type": "account_id",
            "account_id": "19191919",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:21:00Z",
        },
    ).json()
    bind = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T13:22:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "2010885372469563394"},
        },
    ).json()

    response = client.get('/api/ops/next-group-task')
    assert response.status_code == 200
    body = response.json()
    assert body['row']['lead_id'] == lead['lead_id']
    assert body['row']['task_id'] == bind['group_join_task_id']
    assert body['kind'] == 'group'


def test_ops_next_action_prefers_bind_before_group():
    client = make_client()
    lead_bind = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-20",
            "source_platform": "meta",
            "source_page_id": "page-20",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000003",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-10",
        },
    ).json()
    client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead_bind['lead_id'],
            "submission_type": "account_id",
            "account_id": "20202020",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:23:00Z",
        },
    )
    lead_group = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-21",
            "source_platform": "meta",
            "source_page_id": "page-21",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000004",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-11",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead_group['lead_id'],
            "submission_type": "account_id",
            "account_id": "21212121",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:24:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T13:25:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "2010885372469563394"},
        },
    )

    response = client.get('/api/ops/next-action')
    assert response.status_code == 200
    body = response.json()
    assert body['kind'] == 'bind'
    assert body['row']['lead_id'] == lead_bind['lead_id']
    assert body['reason']
    assert body['score'] > 0


def test_ops_next_action_prefers_bind_failed_before_plain_bind_pending():
    client = make_client()
    failed_lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-24",
            "source_platform": "meta",
            "source_page_id": "page-24",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000007",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-14",
        },
    ).json()
    failed_submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": failed_lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "24242424",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:31:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{failed_submission['task_id']}/bind-check-result",
        json={
            "status": "failed",
            "result_code": "bind_failed",
            "result_reason": "id invalid or already in other guild",
            "finished_at": "2026-04-14T13:32:00Z",
            "raw_result": {}
        },
    )

    pending_lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-25",
            "source_platform": "meta",
            "source_page_id": "page-25",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000008",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-15",
        },
    ).json()
    client.post(
        "/api/account-submissions",
        json={
            "lead_id": pending_lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "25252525",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:33:00Z",
        },
    )

    response = client.get('/api/ops/next-action')
    body = response.json()
    assert body['kind'] == 'bind'
    assert body['row']['lead_id'] == failed_lead['lead_id']
    assert '失败' in body['reason'] or 'failed' in body['reason'].lower()


def test_ops_next_action_prefers_crm_sync_after_bind_success_without_crm_record():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-22",
            "source_platform": "meta",
            "source_page_id": "page-22",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000005",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-12",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "22222222",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:26:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T13:27:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "2010885372469563394"},
        },
    )

    response = client.get('/api/ops/next-action')
    assert response.status_code == 200
    body = response.json()
    assert body['kind'] == 'crm_sync'
    assert body['row']['lead_id'] == lead['lead_id']
    assert 'CRM' in body['reason']

def test_ops_next_action_falls_back_to_group_when_no_bind_or_crm_sync_needed():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-next-action-group",
            "source_platform": "manual_cs",
            "source_campaign": "lark",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000006",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-13",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "23232323",
            "account_id_type": "platform_uid",
            "source_channel": "whatsapp",
            "submitted_by": "customer_service",
            "submitted_at": "2026-04-14T13:28:00Z",
        },
    ).json()
    bind = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "manual backend bind success",
            "finished_at": "2026-04-14T13:29:00Z",
            "raw_result": {"guild_code": "Piso", "deptName": "Piso", "deptId": "2010885372469563394"},
        },
    ).json()
    # mark CRM synced so group becomes the next actionable item
    client.post(
        "/api/crm/customer-sync",
        json={
            "lead_id": lead['lead_id'],
            "task_id": bind['task_id'],
            "yw_id": "23232323",
            "mobile": "81110000006",
            "area_code": 62,
            "crm_patch": {"file_url": "http://oss/test.png", "pz_status": 1},
            "sync_mode": "upsert"
        },
    )
    client.post(
        f"/api/tasks/{bind['group_join_task_id']}/group-join-result",
        json={
            "status": "failed",
            "result_code": "join_failed",
            "result_reason": "pending admin approval",
            "finished_at": "2026-04-14T13:30:00Z",
            "raw_result": {}
        },
    )

    response = client.get('/api/ops/next-action')
    assert response.status_code == 200
    body = response.json()
    assert body['kind'] == 'group'


def test_ops_operator_notifications_returns_success_after_crm_sync():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-notify-success",
            "source_platform": "manual_cs",
            "source_campaign": "lark",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000007",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-14",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "24242424",
            "account_id_type": "platform_uid",
            "source_channel": "manual_cs_lark",
            "submitted_by": "cs_a",
            "submitted_at": "2026-04-15T09:00:00Z",
        },
    ).json()
    bind = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "bind success",
            "finished_at": "2026-04-15T09:02:00Z",
            "raw_result": {"guild_code": "Piso"},
        },
    ).json()
    client.post(
        "/api/crm/customer-sync",
        json={
            "lead_id": lead['lead_id'],
            "task_id": bind['task_id'],
            "yw_id": "24242424",
            "mobile": "81110000007",
            "area_code": 62,
            "crm_patch": {"pz_status": 1},
            "sync_mode": "upsert"
        },
    )

    response = client.get('/api/ops/operator-notifications')
    assert response.status_code == 200
    rows = response.json()['rows']
    assert rows[0]['notification_type'] == 'crm_record_success'
    assert rows[0]['mobile'] == '81110000007'
    assert rows[0]['yw_id'] == '24242424'
    assert rows[0]['write_result'] == 'success'
    assert rows[0]['message_text'] == '用户手机: 81110000007\n用户ID: 24242424\n写入结果: success'
    assert rows[0]['message_title'] == 'Lark收口通知'



def test_success_notification_auto_resolves_prior_failed_notification_for_same_lead():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-notify-reconcile",
            "source_platform": "manual_cs",
            "source_campaign": "lark",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000070",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-70",
        },
    ).json()
    service = client.app.state.service
    with service.db.connect() as conn:
        service._queue_operator_notification(
            conn,
            lead_id=lead['lead_id'],
            notification_type='crm_record_failed',
            mobile='81110000070',
            yw_id='70707070',
            write_result='failed',
            reason='old crm failure',
        )
        conn.commit()

    client.post(
        "/api/crm/customer-sync",
        json={
            "lead_id": lead['lead_id'],
            "task_id": "task_reconcile",
            "yw_id": "70707070",
            "mobile": "81110000070",
            "area_code": 62,
            "crm_patch": {"pz_status": 1},
            "sync_mode": "upsert"
        },
    )

    rows = client.get('/api/ops/operator-notifications').json()['rows']
    success_row = next(row for row in rows if row['notification_type'] == 'crm_record_success')
    failed_row = next(row for row in rows if row['reason'] == 'old crm failure')
    assert success_row['is_read'] is False
    assert failed_row['is_read'] is True
    assert failed_row['read_by'] == 'system:auto_resolved'



def test_ops_operator_notifications_returns_bind_failure_reason():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-notify-failed",
            "source_platform": "manual_cs",
            "source_campaign": "lark",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000008",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-15",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "25252525",
            "account_id_type": "platform_uid",
            "source_channel": "manual_cs_lark",
            "submitted_by": "cs_b",
            "submitted_at": "2026-04-15T09:10:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "failed",
            "result_code": "bind_failed",
            "result_reason": "guild rejected",
            "finished_at": "2026-04-15T09:12:00Z",
            "raw_result": {},
        },
    )

    response = client.get('/api/ops/operator-notifications')
    assert response.status_code == 200
    rows = response.json()['rows']
    assert rows[0]['notification_type'] == 'bind_check_failed'
    assert rows[0]['mobile'] == '81110000008'
    assert rows[0]['yw_id'] == '25252525'
    assert rows[0]['write_result'] == 'failed'
    assert rows[0]['reason'] == 'guild rejected'
    assert rows[0]['is_read'] is False
    assert rows[0]['message_text'] == '用户手机: 81110000008\n用户ID: 25252525\n写入结果: failed\n失败原因: guild rejected'



def test_operator_notifications_dedupe_same_lead_type_and_reason_within_window():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-notify-dedupe",
            "source_platform": "manual_cs",
            "source_campaign": "lark",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000018",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-18",
        },
    ).json()
    service = client.app.state.service
    with service.db.connect() as conn:
        service._queue_operator_notification(
            conn,
            lead_id=lead['lead_id'],
            notification_type='crm_record_failed',
            mobile='81110000018',
            yw_id='18181818',
            write_result='failed',
            reason='Data duplication.',
        )
        service._queue_operator_notification(
            conn,
            lead_id=lead['lead_id'],
            notification_type='crm_record_failed',
            mobile='81110000018',
            yw_id='18181818',
            write_result='failed',
            reason='Data duplication.',
        )
        conn.commit()

    rows = client.get('/api/ops/operator-notifications').json()['rows']
    deduped = [row for row in rows if row['lead_id'] == lead['lead_id']]
    assert len(deduped) == 1



def test_ops_operator_notifications_supports_unread_filter_and_mark_read():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-notify-read",
            "source_platform": "manual_cs",
            "source_campaign": "lark",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000009",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-16",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "26262626",
            "account_id_type": "platform_uid",
            "source_channel": "manual_cs_lark",
            "submitted_by": "cs_c",
            "submitted_at": "2026-04-15T09:20:00Z",
        },
    ).json()
    bind = client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "bind success",
            "finished_at": "2026-04-15T09:21:00Z",
            "raw_result": {"guild_code": "Piso"},
        },
    ).json()
    client.post(
        "/api/crm/customer-sync",
        json={
            "lead_id": lead['lead_id'],
            "task_id": bind['task_id'],
            "yw_id": "26262626",
            "mobile": "81110000009",
            "area_code": 62,
            "crm_patch": {"pz_status": 1},
            "sync_mode": "upsert"
        },
    )

    unread = client.get('/api/ops/operator-notifications?status=unread')
    assert unread.status_code == 200
    rows = unread.json()['rows']
    target = next(row for row in rows if row['mobile'] == '81110000009')
    assert target['is_read'] is False

    marked = client.post(f"/api/ops/operator-notifications/{target['notification_id']}/read", json={"read_by": "ops_a"})
    assert marked.status_code == 200
    assert marked.json()['updated'] is True

    unread_after = client.get('/api/ops/operator-notifications?status=unread').json()['rows']
    assert all(row['notification_id'] != target['notification_id'] for row in unread_after)

    read_rows = client.get('/api/ops/operator-notifications?status=read').json()['rows']
    marked_row = next(row for row in read_rows if row['notification_id'] == target['notification_id'])
    assert marked_row['is_read'] is True
    assert marked_row['read_by'] == 'ops_a'


def test_ops_operator_notifications_supports_search_keyword():
    client = make_client()
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-notify-search",
            "source_platform": "manual_cs",
            "source_campaign": "lark",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "81110000010",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Piso-17",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "27272727",
            "account_id_type": "platform_uid",
            "source_channel": "manual_cs_lark",
            "submitted_by": "cs_d",
            "submitted_at": "2026-04-15T09:30:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "failed",
            "result_code": "bind_failed",
            "result_reason": "duplicate guild member",
            "finished_at": "2026-04-15T09:32:00Z",
            "raw_result": {},
        },
    )

    by_mobile = client.get('/api/ops/operator-notifications?query=81110000010')
    assert by_mobile.status_code == 200
    assert by_mobile.json()['rows'][0]['mobile'] == '81110000010'

    by_yw_id = client.get('/api/ops/operator-notifications?query=27272727')
    assert by_yw_id.status_code == 200
    assert by_yw_id.json()['rows'][0]['yw_id'] == '27272727'


def test_manual_cs_conflict_routes_to_manual_review_queue_before_account_submission():
    client = make_client()
    response = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "081234567899",
            "registration_group": "Piso-20",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "screenshot",
            "file_url": "https://cdn.example.com/review-shot.png",
            "file_type": "image/png",
            "submitted_by": "cs_review_a",
            "source_channel": "manual_cs_lark",
            "remark": "手机号 081234567899，Linky，Piso，ID 88888888",
            "submitted_at": "2026-04-15T10:00:00Z",
            "image_ocr_text": "UID 99999999\nGroup Piso-20",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["next_action"] == "manual_review"
    assert body["routing_decision"] == "manual_review"
    assert "account_id_conflict" in body["review_reason_codes"]

    queue = client.get('/api/ops/manual-review-queue')
    assert queue.status_code == 200
    row = next(r for r in queue.json()['rows'] if r['lead_id'] == body['lead_id'])
    assert row['current_status'] == 'manual_review_pending'
    assert row['routing_decision'] == 'manual_review'
    assert 'account_id_conflict' in row['review_reason_codes']
    assert row['recommended_next_action']

    timeline = client.get(f"/api/leads/{body['lead_id']}/timeline")
    assert timeline.status_code == 200
    lead = timeline.json()['lead']
    assert lead['parser_status'] == 'conflict'
    assert lead['routing_decision'] == 'manual_review'
    assert 'account_id_conflict' in lead['review_reason_codes']


def test_manual_review_resolution_creates_bind_task_and_correction_history():
    client = make_client()
    intake = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "081234567898",
            "registration_group": "Piso-21",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "screenshot",
            "file_url": "https://cdn.example.com/review-fix.png",
            "file_type": "image/png",
            "submitted_by": "cs_review_b",
            "source_channel": "manual_cs_lark",
            "remark": "手机号 081234567898，Linky，Piso，ID 66666666",
            "submitted_at": "2026-04-15T10:05:00Z",
            "image_ocr_text": "UID 77777777\nGroup Piso-21",
        },
    ).json()

    resolved = client.post(
        f"/api/ops/manual-review/{intake['lead_id']}/resolve",
        json={
            "decision": "approve_bind",
            "reviewed_by": "ops_reviewer_a",
            "review_note": "采用人工确认 ID",
            "account_id": "66666666",
            "dept_name": "Piso",
            "app_name": "Linky",
            "registration_group": "Piso-21",
            "submitted_at": "2026-04-15T10:08:00Z"
        },
    )

    assert resolved.status_code == 200
    body = resolved.json()
    assert body['accepted'] is True
    assert body['decision'] == 'approve_bind'
    assert body['next_action'] == 'queue_bind_check'
    assert body['task_id']
    assert body['correction_count'] >= 1

    bind_queue = client.get('/api/ops/bind-queue').json()['rows']
    bind_row = next(r for r in bind_queue if r['lead_id'] == intake['lead_id'])
    assert bind_row['current_status'] == 'account_submitted'

    timeline = client.get(f"/api/leads/{intake['lead_id']}/timeline").json()
    assert timeline['lead']['review_status'] == 'approved'
    assert timeline['lead']['correction_count'] >= 1
    assert len(timeline['review_history']) >= 1
    assert len(timeline['correction_history']) >= 1
    assert timeline['correction_history'][0]['field_name'] == 'account_id'


def test_ops_approval_batch_queue_aggregates_real_pending_data():
    client = make_client()
    for idx in range(30):
        client.post(
            "/api/leads/upsert",
            json={
                "trace_id": f"trace-reg-{idx}",
                "source_platform": "manual_cs",
                "source_page_id": "lark",
                "country": "Indonesia",
                "area_code": 62,
                "mobile": f"8123400{idx:04d}",
                "app_name": "Linky",
                "dept_name": "Piso",
                "pendaftaran_group": "Piso-30",
                "parser_confidence": 0.95,
                "parser_missing_fields": [],
                "parser_conflicts": [],
            },
        )
    lead = client.post(
        "/api/leads/upsert",
        json={
            "trace_id": "trace-official-1",
            "source_platform": "manual_cs",
            "source_page_id": "lark",
            "country": "Indonesia",
            "area_code": 62,
            "mobile": "82220000001",
            "app_name": "Linky",
            "dept_name": "Piso",
            "pendaftaran_group": "Official-A",
        },
    ).json()
    submission = client.post(
        "/api/account-submissions",
        json={
            "lead_id": lead['lead_id'],
            "submission_type": "account_id",
            "account_id": "30303030",
            "account_id_type": "platform_uid",
            "source_channel": "manual_cs_lark",
            "submitted_by": "cs_batch",
            "submitted_at": "2026-04-15T10:10:00Z",
        },
    ).json()
    client.post(
        f"/api/tasks/{submission['task_id']}/bind-check-result",
        json={
            "status": "success",
            "result_code": "bind_ok",
            "result_reason": "bind success",
            "finished_at": "2026-04-15T10:11:00Z",
            "raw_result": {"guild_code": "Piso"},
        },
    )

    queue = client.get('/api/ops/approval-batch-queue')
    assert queue.status_code == 200
    body = queue.json()
    registration = next(row for row in body['registration_groups'] if row['registration_group'] == 'Piso-30')
    assert registration['pending_count'] == 30
    assert registration['ready'] is True
    assert registration['reason_code'] == 'batch_size_reached'
    official = next(row for row in body['official_groups'] if row['registration_group'] == 'Official-A')
    assert official['pending_count'] == 1
    assert official['reason_code'] in {'waiting_for_batch', 'timeout_flush'}


def test_ops_parser_quality_summary_counts_reviews_conflicts_and_corrections():
    client = make_client()
    intake = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "081234567897",
            "registration_group": "Piso-22",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "screenshot",
            "file_url": "https://cdn.example.com/quality-shot.png",
            "file_type": "image/png",
            "submitted_by": "cs_quality",
            "source_channel": "manual_cs_lark",
            "remark": "手机号 081234567897，Linky，Piso，ID 11112222",
            "submitted_at": "2026-04-15T10:20:00Z",
            "image_ocr_text": "UID 33334444\nGroup Piso-22",
        },
    ).json()
    client.post(
        f"/api/ops/manual-review/{intake['lead_id']}/resolve",
        json={
            "decision": "approve_bind",
            "reviewed_by": "ops_quality",
            "review_note": "人工确认文本 ID",
            "account_id": "11112222",
            "submitted_at": "2026-04-15T10:21:00Z"
        },
    )

    summary = client.get('/api/ops/parser-quality-summary')
    assert summary.status_code == 200
    body = summary.json()
    assert body['manual_review_count'] >= 1
    assert body['parser_conflict_count'] >= 1
    assert body['correction_count'] >= 1
    assert body['approved_review_count'] >= 1


def test_manual_review_can_request_recognition_retry_and_requeue_recognition_task():
    client = make_client()
    intake = client.post(
        "/api/intake/manual-cs-submissions",
        json={
            "mobile": "081234567896",
            "registration_group": "Piso-23",
            "app_name": "Linky",
            "dept_name": "Piso",
            "submission_type": "screenshot",
            "file_url": "https://cdn.example.com/retry-shot.png",
            "file_type": "image/png",
            "submitted_by": "cs_retry",
            "source_channel": "manual_cs_lark",
            "remark": "手机号 081234567896，Linky，Piso，ID 44445555",
            "submitted_at": "2026-04-15T10:30:00Z",
            "image_ocr_text": "UID 99990000\nGroup Piso-23",
        },
    ).json()

    resolved = client.post(
        f"/api/ops/manual-review/{intake['lead_id']}/resolve",
        json={
            "decision": "request_recognition_retry",
            "reviewed_by": "ops_retry",
            "review_note": "截图需要重新识别",
            "submitted_at": "2026-04-15T10:31:00Z"
        },
    )

    assert resolved.status_code == 200
    body = resolved.json()
    assert body['accepted'] is True
    assert body['decision'] == 'request_recognition_retry'
    assert body['next_action'] == 'queue_account_recognition'
    assert body['task_id']

    timeline = client.get(f"/api/leads/{intake['lead_id']}/timeline").json()
    assert timeline['lead']['current_status'] == 'recognition_pending'
    assert timeline['lead']['review_status'] == 'retry_requested'
    assert any(task['task_type'] == 'account_recognition' for task in timeline['tasks'])
    assert any(item['decision'] == 'request_recognition_retry' for item in timeline['review_history'])


def test_ops_page_includes_manual_review_action_controls():
    client = make_client()
    response = client.get('/ops')
    assert response.status_code == 200
    body = response.text
    assert '/api/ops/manual-review-queue' in body
    assert '/api/ops/manual-review/' in body
    assert '/api/tasks/' in body
    assert '人工复核队列' in body
    assert 'approveManualReview' in body
    assert 'retryRecognition' in body
    assert 'rejectManualReview' in body
    assert 'runNativeOcr' in body
    assert 'review-field' in body
    assert 'manualReviewFieldValue' in body
    assert 'manualReviewNote' in body
    assert 'renderManualReviewEditor' in body
    assert 'renderRecognitionCodeSummary' in body
    assert 'renderLeadDetail' in body
    assert '用户个人绑定码' in body
    assert '公会固定邀请码' in body
    assert 'reloadManualReviewQueue' in body
    assert 'showToast' in body
    assert 'manualReviewToast' in body


def test_native_ocr_run_executes_screenshot_task_and_queues_bind_check():
    ocr = StubOcrAdapter(raw_text='SID Saya 45691735\nkode gabung agensi EKVFGQ\nAgensi saya Permata')
    client = make_client({'OCR_ADAPTER': ocr})
    lead = client.post(
        '/api/leads/upsert',
        json={
            'trace_id': 'trace-ocr-1',
            'source_platform': 'manual_cs',
            'source_page_id': 'lark',
            'country': 'Indonesia',
            'area_code': 62,
            'mobile': '81119990001',
            'app_name': 'Linky',
            'dept_name': 'Permata',
            'pendaftaran_group': 'Permata',
        },
    ).json()
    submission = client.post(
        '/api/account-submissions',
        json={
            'lead_id': lead['lead_id'],
            'submission_type': 'screenshot',
            'file_url': 'https://cdn.example.com/ocr-1.png',
            'file_type': 'image/png',
            'source_channel': 'manual_cs_lark',
            'submitted_by': 'cs_ocr',
            'submitted_at': '2026-04-15T18:00:00Z',
        },
    ).json()

    response = client.post(f"/api/tasks/{submission['task_id']}/native-ocr-run")
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'success'
    assert body['recognized_account_id'] == '45691735'
    assert body['person_code'] == 'EKVFGQ'
    assert body['guild_invite_code'] is None
    assert body['next_action'] == 'queue_bind_check'
    assert ocr.calls == ['https://cdn.example.com/ocr-1.png']

    timeline = client.get(f"/api/leads/{lead['lead_id']}/timeline").json()
    assert timeline['lead']['current_status'] == 'account_submitted'
    assert any(task['task_type'] == 'bind_check' for task in timeline['tasks'])
    recognized = next(sub for sub in timeline['account_submissions'] if sub['recognition_status'] == 'success')
    assert recognized['recognition_raw']['person_code'] == 'EKVFGQ'
    assert recognized['recognition_raw']['guild_invite_code'] is None


def test_native_ocr_run_returns_guild_invite_code_for_invite_success_toast():
    ocr = StubOcrAdapter(raw_text='Isi kode undangan\nKode Undangan KK9J8D\nSelamat datang! Kamu berhasil bergabung!\nSID kamu: 45689309, Nama Guild: Permata')
    client = make_client({'OCR_ADAPTER': ocr})
    lead = client.post(
        '/api/leads/upsert',
        json={
            'trace_id': 'trace-ocr-1b',
            'source_platform': 'manual_cs',
            'source_page_id': 'lark',
            'country': 'Indonesia',
            'area_code': 62,
            'mobile': '81119990003',
            'app_name': 'Linky',
            'dept_name': 'Permata',
            'pendaftaran_group': 'Permata',
        },
    ).json()
    submission = client.post(
        '/api/account-submissions',
        json={
            'lead_id': lead['lead_id'],
            'submission_type': 'screenshot',
            'file_url': 'https://cdn.example.com/ocr-1b.png',
            'file_type': 'image/png',
            'source_channel': 'manual_cs_lark',
            'submitted_by': 'cs_ocr',
            'submitted_at': '2026-04-15T18:05:00Z',
        },
    ).json()

    response = client.post(f"/api/tasks/{submission['task_id']}/native-ocr-run")
    assert response.status_code == 200
    body = response.json()
    assert body['recognized_account_id'] == '45689309'
    assert body['guild_invite_code'] == 'KK9J8D'
    assert body['person_code'] is None

    timeline = client.get(f"/api/leads/{lead['lead_id']}/timeline").json()
    recognized = next(sub for sub in timeline['account_submissions'] if sub['recognition_status'] == 'success')
    assert recognized['recognition_raw']['guild_invite_code'] == 'KK9J8D'
    assert recognized['recognition_raw']['person_code'] is None


def test_manual_review_queue_exposes_distinct_recognition_codes():
    client = make_client()
    response = client.post(
        '/api/intake/manual-cs-submissions',
        json={
            'mobile': '081119990004',
            'registration_group': 'Permata-1',
            'app_name': 'Linky',
            'dept_name': 'Permata',
            'submission_type': 'screenshot',
            'file_url': 'https://cdn.example.com/manual-review-ocr.png',
            'file_type': 'image/png',
            'source_channel': 'manual_cs_lark',
            'submitted_by': 'cs_ops',
            'submitted_at': '2026-04-15T18:20:00Z',
            'account_id': '99999999',
            'image_ocr_text': 'ID: 45691735\nkode gabung agensi EKVFGQ\nAgensi saya Permata',
            'remark': '人工录入ID与OCR识别不一致，触发复核',
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body['routing_decision'] == 'manual_review'

    queue = client.get('/api/ops/manual-review-queue')
    assert queue.status_code == 200
    row = next(r for r in queue.json()['rows'] if r['lead_id'] == body['lead_id'])
    assert row['person_code'] == 'EKVFGQ'
    assert row['guild_invite_code'] is None


def test_native_ocr_run_returns_503_when_ocr_adapter_missing():
    client = make_client()
    lead = client.post(
        '/api/leads/upsert',
        json={
            'trace_id': 'trace-ocr-2',
            'source_platform': 'manual_cs',
            'source_page_id': 'lark',
            'country': 'Indonesia',
            'area_code': 62,
            'mobile': '81119990002',
        },
    ).json()
    submission = client.post(
        '/api/account-submissions',
        json={
            'lead_id': lead['lead_id'],
            'submission_type': 'screenshot',
            'file_url': 'https://cdn.example.com/ocr-2.png',
            'file_type': 'image/png',
            'source_channel': 'manual_cs_lark',
            'submitted_by': 'cs_ocr',
            'submitted_at': '2026-04-15T18:10:00Z',
        },
    ).json()

    response = client.post(f"/api/tasks/{submission['task_id']}/native-ocr-run")
    assert response.status_code == 503
    assert response.json()['detail'] == 'ocr adapter not configured'


def test_lark_image_event_downloads_once_and_reuses_cache(tmp_path):
    media = StubLarkMediaAdapter(payload=b'image-1')
    client = make_client({'LARK_MEDIA_ADAPTER': media, 'MEDIA_CACHE_DIR': str(tmp_path)})
    payload = {
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_test'}},
            'message': {
                'message_id': 'om_test_1',
                'chat_type': 'p2p',
                'message_type': 'image',
                'content': '{"image_key":"img_key_1"}'
            }
        }
    }

    first = client.post('/api/intake/lark/events', json=payload)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body['accepted'] is True
    assert first_body['cached'] is True
    assert first_body['downloaded'] is True
    assert first_body['next_action'] == 'await_text_context'

    second = client.post('/api/intake/lark/events', json=payload)
    assert second.status_code == 200
    second_body = second.json()
    assert second_body['cached'] is True
    assert second_body['downloaded'] is False
    assert media.calls == [('om_test_1', 'img_key_1')]
    assert second_body['cached_file_url'] == first_body['cached_file_url']


def test_lark_event_callback_supports_url_verification():
    client = make_client({'LARK_APP_ID': 'cli_test'})
    response = client.post('/api/intake/lark/events', json={
        'type': 'url_verification',
        'challenge': 'abc123challenge',
        'token': 'token-test'
    })
    assert response.status_code == 200
    assert response.json()['challenge'] == 'abc123challenge'


def test_lark_private_message_bridges_to_manual_intake():
    reply = StubLarkReplyAdapter()
    client = make_client({'LARK_APP_ID': 'cli_test', 'LARK_REPLY_ADAPTER': reply})
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_1'}},
            'message': {
                'message_id': 'om_text_1',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"手机号 +62 81234567890\n注册群组 Piso-25\n应用 Linky\n公会 Piso\nID 55667788\nCode EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is True
    assert body['source'] == 'lark_event_bridge'
    assert body['next_action'] == 'queue_bind_check'
    assert body.get('reply_text', '') == ''
    assert reply.calls == []


def test_lark_group_at_message_bridges_to_manual_intake():
    reply = StubLarkReplyAdapter()
    client = make_client({'LARK_APP_ID': 'cli_test', 'LARK_REPLY_ADAPTER': reply})
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_2'}},
            'message': {
                'message_id': 'om_text_2',
                'message_type': 'text',
                'chat_type': 'group',
                'mentions': [{'name': '收口机器人', 'id': {'open_id': 'ou_bot_x'}}],
                'content': '{"text":"@收口机器人 手机号 +62 81234567891 注册群组 Piso-26 应用 Linky 公会 Piso ID 66778899 Code EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is True
    assert body['source'] == 'lark_event_bridge'
    assert body['next_action'] == 'queue_bind_check'
    assert body.get('reply_text', '') == ''
    assert reply.calls == []


def test_lark_event_replies_with_failure_template_when_required_fields_missing():
    reply = StubLarkReplyAdapter()
    client = make_client({'LARK_APP_ID': 'cli_test', 'LARK_REPLY_ADAPTER': reply})
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_3'}},
            'message': {
                'message_id': 'om_text_3',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"只有手机号 +62 81234567892"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'missing_required_fields'
    assert body['reply_text'] == (
        '**🚫 Missing: Group, ID**\n'
        'Phone: +62 81234567892\n'
        'ID: -\n'
        'Group: -\n'
        'Code: -'
    )
    assert reply.calls
    assert reply.calls[0]['message_id'] == 'om_text_3'
    assert reply.calls[0]['text'] == body['reply_text']


def test_lark_event_uses_host_registration_template_for_irrelevant_text():
    reply = StubLarkReplyAdapter()
    client = make_client({'LARK_APP_ID': 'cli_test', 'LARK_REPLY_ADAPTER': reply})
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_irrelevant'}},
            'message': {
                'message_id': 'om_text_irrelevant',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"今天你吃吗烤鱼"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'irrelevant_message'
    assert body['reply_text'] == (
        '**🚫I only register host information**\n'
        '**📮Send:**\n'
        'Phone:\n'
        'ID:\n'
        'Group:\n'
        'Code:\n'
        '**📌Example:**\n'
        'Phone: +62 13800000000  ID: 123456  Group: Group-1  Code: EKVFGQ'
    )
    assert reply.calls
    assert reply.calls[0]['text'] == body['reply_text']


def test_lark_event_returns_reply_text_without_sending_when_gateway_direct_mode_enabled():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_direct'}},
            'message': {
                'message_id': 'om_text_direct',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 126165399\\nPiso-4\\n901124"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'invalid_account_id_format'
    assert body['reply_text'] == (
        '**🚫 Invalid ID. Linky requires exactly 8 digits.**\n'
        'Phone: +62 126165399\n'
        'ID: 901124\n'
        'Group: Piso-4\n'
        'Code: -'
    )
    assert reply.calls == []


def test_lark_event_keeps_id_first_line_as_id_not_phone_candidate():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_id_first_phone_second'}},
            'message': {
                'message_id': 'om_text_id_first_phone_second',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"3886115721\\n+62 724411989\\nPermata-12"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'invalid_account_id_format'
    assert body['reply_text'] == (
        '**🚫 Invalid ID. Linky requires exactly 8 digits.**\n'
        'Phone: +62 724411989\n'
        'ID: 3886115721\n'
        'Group: Permata-12\n'
        'Code: -'
    )
    assert reply.calls == []


def test_lark_text_event_with_media_urls_uses_ocr_text_for_image_recognition():
    ocr = StubOcrAdapter(raw_text='SID Saya 45691735\nAgensi saya Permata-7')
    reply = StubLarkReplyAdapter()
    client = make_client({
        'OCR_ADAPTER': ocr,
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_media_text'}},
            'message': {
                'message_id': 'om_text_media',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 7898998989\\nPiso-21\\n[Image]","media_urls":["/tmp/fake-image.png"]}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is True
    assert body['reply_id'] == '45691735'
    assert body['reply_group'] == 'Piso-21'
    assert body['parsed_payload']['dept_name'] == 'Permata-7'
    assert body['parsed_payload']['account_id'] == '45691735'
    assert ocr.calls == ['/tmp/fake-image.png']


def test_lark_post_text_unescapes_group_and_phone_before_parsing():
    ocr = StubOcrAdapter(raw_text='SID Saya 45691735\nAgensi saya Permata-7')
    client = make_client({
        'OCR_ADAPTER': ocr,
        'LARK_APP_ID': 'cli_test',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_media_text_escaped'}},
            'message': {
                'message_id': 'om_text_media_escaped',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"[Image]\\n\\\\+62 784498989\\nPiso\\\\-22","media_urls":["/tmp/fake-image.png"]}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is True
    assert body['reply_phone'] == '+62 784498989'
    assert body['reply_group'] == 'Piso-22'
    assert body['reply_id'] == '45691735'


def test_lark_event_rejects_phone_without_country_code_space_before_bind():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'AUTO_BIND_SIMULATION': True,
        'AUTO_BIND_SIMULATION_SUCCESS_RATE': 1.0,
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_phone'}},
            'message': {
                'message_id': 'om_text_phone_invalid',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"Phone: +621****9911\nGroup: Piso-4\nID: 90144211"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'invalid_phone_format'
    assert reply.calls[0]['text'].startswith('**🚫 Invalid phone format. Use +<country code> <number>.**\n')
    assert 'Phone:' in reply.calls[0]['text']
    assert 'ID: 90144211' in reply.calls[0]['text']
    assert 'Group: Piso-4' in reply.calls[0]['text']
    assert reply.calls[0]['text'].endswith('Code: -')


def test_lark_event_accepts_grouped_space_phone_before_bind():
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'AUTO_LARK_REPLY': False,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'AUTO_BIND_SIMULATION': True,
        'AUTO_BIND_SIMULATION_SUCCESS_RATE': 1.0,
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_phone_grouped'}},
            'message': {
                'message_id': 'om_text_phone_grouped',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"Phone: +62 899 9999 9999\nGroup: Piso-4\nID: 90144211\nCode: EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body.get('reason') != 'invalid_phone_format'
    assert body['reply_phone'] == '+62 89999999999'



def test_lark_event_accepts_us_parenthesized_phone_before_bind():
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'AUTO_LARK_REPLY': False,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'AUTO_BIND_SIMULATION': True,
        'AUTO_BIND_SIMULATION_SUCCESS_RATE': 1.0,
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_phone_us'}},
            'message': {
                'message_id': 'om_text_phone_us',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"Phone: +1 (650) 555-1212\nGroup: Piso-4\nID: 90144211\nCode: EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body.get('reason') != 'invalid_phone_format'
    assert body['reply_phone'] == '+1 6505551212'



def test_lark_event_rejects_linky_id_that_is_not_8_digits_before_bind():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'AUTO_BIND_SIMULATION': True,
        'AUTO_BIND_SIMULATION_SUCCESS_RATE': 1.0,
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_id'}},
            'message': {
                'message_id': 'om_text_id_invalid',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"Phone: +62 1235539911\nGroup: Piso-4\nID: 9014421"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'invalid_account_id_format'
    assert reply.calls[0]['text'] == (
        '**🚫 Invalid ID. Linky requires exactly 8 digits.**\n'
        'Phone: +62 1235539911\n'
        'ID: 9014421\n'
        'Group: Piso-4\n'
        'Code: -'
    )


def test_lark_event_understands_bare_multiline_fields_and_then_rejects_invalid_linky_id():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'AUTO_BIND_SIMULATION': True,
        'AUTO_BIND_SIMULATION_SUCCESS_RATE': 1.0,
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_bare_invalid'}},
            'message': {
                'message_id': 'om_text_bare_invalid',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 1261215399\nPiso-4\n9011211"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'invalid_account_id_format'
    assert reply.calls[0]['text'] == (
        '**🚫 Invalid ID. Linky requires exactly 8 digits.**\n'
        'Phone: +62 1261215399\n'
        'ID: 9011211\n'
        'Group: Piso-4\n'
        'Code: -'
    )


def test_lark_event_understands_bare_multiline_fields_and_accepts_valid_input():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_bare_valid'}},
            'message': {
                'message_id': 'om_text_bare_valid',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 1261215399\nPiso-4\n90112111"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is True
    assert body['next_action'] == 'queue_bind_check'
    assert body.get('reply_text', '') == ''
    assert reply.calls == []


def test_lark_event_exact_user_case_returns_invalid_id_not_irrelevant():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'AUTO_BIND_SIMULATION': True,
        'AUTO_BIND_SIMULATION_SUCCESS_RATE': 1.0,
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_exact_case'}},
            'message': {
                'message_id': 'om_text_exact_case',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 126165399\nPiso-4\n901124"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'invalid_account_id_format'
    assert reply.calls[0]['text'] == (
        '**🚫 Invalid ID. Linky requires exactly 8 digits.**\n'
        'Phone: +62 126165399\n'
        'ID: 901124\n'
        'Group: Piso-4\n'
        'Code: -'
    )


def test_lark_event_missing_group_does_not_merge_id_into_phone():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_missing_group'}},
            'message': {
                'message_id': 'om_text_missing_group',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 1261215399\n90112111"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'missing_required_fields'
    assert body['reply_phone'] == '+62 1261215399'
    assert body['reply_id'] == '90112111'
    assert body['reply_group'] == '-'


def test_lark_event_recognizes_generic_english_dash_number_group_names():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_generic_group'}},
            'message': {
                'message_id': 'om_text_generic_group',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 1261215399\nWhisky-7\n90112111"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is True
    assert body['reply_group'] == 'Whisky-7'
    assert body.get('reply_text', '') == ''
    assert reply.calls == []


def test_lark_event_does_not_treat_plain_english_word_as_group_name():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_plain_english'}},
            'message': {
                'message_id': 'om_text_plain_english',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 1261215399\nWhisky\n90112111"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'missing_required_fields'
    assert body['reply_group'] == '-'


def test_lark_event_replies_with_invalid_group_format_when_group_candidate_misses_dash():
    assert extract_invalid_group_candidate('90965721\n+62 784311989\nPiso12') == 'Piso12'
    app = create_app({
        'DB_PATH': ':memory:',
        'LARK_APP_ID': 'cli_test',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    result = app.state.service._format_lark_reply_text({
        'accepted': False,
        'reason': 'invalid_group_format',
        'reply_phone': '+62 784311989',
        'reply_id': '90965721',
        'reply_group': 'Piso12',
    })
    assert result == (
        '**🚫 Invalid group format. Use English-Number, e.g. Piso-12.**\n'
        'Phone: +62 784311989\n'
        'ID: 90965721\n'
        'Group: Piso12\n'
        'Code: -'
    )


def test_lark_event_rejects_fumi_id_when_not_exactly_8_digits():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'FUMI',
        'LARK_DEFAULT_DEPT_NAME': 'Permata',
    })
    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_fumi_invalid_id'}},
            'message': {
                'message_id': 'om_text_fumi_invalid_id',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 711122233\nPermata-12\n1234567"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'invalid_account_id_format'
    assert body['reply_text'] == (
        '**🚫 Invalid ID. FUMI requires exactly 8 digits.**\n'
        'Phone: +62 711122233\n'
        'ID: 1234567\n'
        'Group: Permata-12\n'
        'Code: -'
    )
    assert reply.calls == []


def test_lark_event_replies_with_app_guild_mismatch_when_explicit_values_conflict_with_preset():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_4'}},
            'message': {
                'message_id': 'om_text_4',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"手机号 +62 81234567893 注册群组 Piso-25 应用 FUMI 公会 Permata ID 55667788"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'app_guild_mismatch'
    assert reply.calls
    assert reply.calls[0]['message_id'] == 'om_text_4'
    assert reply.calls[0]['text'] == (
        '**🚫 I do not handle this app/agency.**\n'
        'Phone: +62 81234567893\n'
        'ID: 55667788\n'
        'Group: Piso-25\n'
        'Code: -'
    )


def test_lark_event_rejects_bare_multiline_explicit_agency_conflict_against_preset():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_5'}},
            'message': {
                'message_id': 'om_text_5',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 4261833388\\n90142228\\nPiso-6\\nLinky\\nPERMATA"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'app_guild_mismatch'
    assert reply.calls
    assert reply.calls[0]['message_id'] == 'om_text_5'
    assert reply.calls[0]['text'] == (
        '**🚫 I do not handle this app/agency.**\n'
        'Phone: +62 4261833388\n'
        'ID: 90142228\n'
        'Group: Piso-6\n'
        'Code: -'
    )


def test_lark_event_replies_missing_when_invite_code_absent():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'REQUIRE_INVITE_CODE': True,
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_missing_code'}},
            'message': {
                'message_id': 'om_missing_code',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567893\\nPiso-25\\n55667788"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is False
    assert body['reason'] == 'missing_required_fields'
    assert 'Code' in body['reply_missing_fields']



def test_lark_event_uses_default_app_and_guild_when_not_provided():
    reply = StubLarkReplyAdapter()
    client = make_client({
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    response = client.post('/api/intake/lark/events', json={
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_cs_4'}},
            'message': {
                'message_id': 'om_text_4',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"手机号 +62 81234567893\\n注册群组 Piso-25\\nID 55667788\\nCode EKVFGQ"}'
            }
        }
    })
    assert response.status_code == 200
    body = response.json()
    assert body['accepted'] is True
    assert body['next_action'] == 'queue_bind_check'
    assert body.get('reply_text', '') == ''
    assert reply.calls == []


def test_intake_bot_presets_api_returns_crm_dropdown_options():
    from app.main import create_app

    crm = StubCrmAdapter()
    crm.apps = [
        {'id': 'app_1', 'name': 'Linky'},
        {'id': 'app_2', 'ywName': 'FUMI'},
    ]
    crm.depts = [
        {'deptId': 'dept_1', 'deptName': 'Piso'},
        {'id': 'dept_2', 'name': 'Permata'},
    ]
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    client = TestClient(app)

    response = client.get('/api/ops/intake-bot-presets')

    assert response.status_code == 200
    body = response.json()
    assert body['app_options'] == [
        {'label': 'FUMI', 'value': 'FUMI'},
        {'label': 'Linky', 'value': 'Linky'},
    ]
    assert body['guild_options'] == [
        {'label': 'Permata', 'value': 'Permata'},
        {'label': 'Piso', 'value': 'Piso'},
    ]


def test_intake_bot_presets_page_uses_dropdown_selects_for_app_and_guild():
    from app.main import create_app

    crm = StubCrmAdapter()
    crm.apps = [{'id': 'app_1', 'name': 'Linky'}]
    crm.depts = [{'deptId': 'dept_1', 'deptName': 'Piso'}]
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    client = TestClient(app)

    response = client.get('/ops/intake-bot-presets')

    assert response.status_code == 200
    body = response.text
    assert '收口配置中心' in body
    assert 'page-shell' in body
    assert 'shell-nav' in body
    assert '配置概况' in body
    assert '机器人配置列表' in body
    assert '执行器总览' in body
    assert 'config-workspace' in body
    assert 'presetFieldHtml(' in body
    assert 'data.app_options' in body
    assert 'data.guild_options' in body
    assert 'data.app_options_source' in body
    assert 'data.guild_options_source' in body
    assert 'setInterval(() => {' in body
    assert 'reloadPresets().catch(err => showToast(err.message, \'error\'))' in body
    assert 'Using live CRM dropdown options only.' in body
    assert 'placeholder="手动输入"' not in body


def test_intake_bot_presets_page_disables_save_when_crm_options_unavailable():
    from app.main import create_app

    app = create_app({
        'DB_PATH': ':memory:',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    client = TestClient(app)

    response = client.get('/ops/intake-bot-presets')

    assert response.status_code == 200
    body = response.text
    assert 'CRM dropdown options are currently unavailable. Saving is disabled.' in body
    assert 'throw new Error(\'CRM dropdown options are unavailable. Saving is disabled.\')' in body
    assert 'placeholder="手动输入"' not in body
    assert 'const manualInput = document.getElementById(`default_app_manual_${profileName}`);' not in body
    assert 'const manualGuildInput = document.getElementById(`default_guild_manual_${profileName}`);' not in body
    assert 'default_app: appField.value.trim(),' in body
    assert 'default_guild: guildField.value.trim(),' in body


def test_intake_bot_presets_api_rejects_manual_fallback_values_when_crm_options_unavailable():
    from app.main import create_app

    app = create_app({
        'DB_PATH': ':memory:',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    client = TestClient(app)

    saved = client.post('/api/ops/intake-bot-presets/current', json={
        'default_app': 'ManualApp',
        'default_guild': 'ManualGuild',
    })
    assert saved.status_code == 400
    assert 'CRM dropdown options are unavailable' in saved.text

    response = client.get('/api/ops/intake-bot-presets')
    assert response.status_code == 200
    body = response.json()
    assert body['app_options'] == []
    assert body['guild_options'] == []
    assert body['rows'][0]['default_app'] == 'Linky'
    assert body['rows'][0]['default_guild'] == 'Piso'


def test_intake_bot_presets_api_reflects_live_crm_option_updates_without_restart():
    from app.main import create_app

    crm = StubCrmAdapter()
    crm.apps = [{'id': 'app_1', 'name': 'Linky'}]
    crm.depts = [{'deptId': 'dept_1', 'deptName': 'Piso'}]
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
    })
    client = TestClient(app)

    initial = client.get('/api/ops/intake-bot-presets')
    assert initial.status_code == 200
    assert initial.json()['app_options'] == [{'label': 'Linky', 'value': 'Linky'}]
    assert initial.json()['guild_options'] == [{'label': 'Piso', 'value': 'Piso'}]

    crm.apps = [{'id': 'app_9', 'name': 'Halo'}]
    crm.depts = [{'deptId': 'dept_9', 'deptName': 'Garuda'}]

    refreshed = client.get('/api/ops/intake-bot-presets')
    assert refreshed.status_code == 200
    assert {'label': 'Halo', 'value': 'Halo'} in refreshed.json()['app_options']
    assert {'label': 'Garuda', 'value': 'Garuda'} in refreshed.json()['guild_options']


def test_runtime_health_reports_crm_and_simulation_status():
    from app.main import create_app

    crm = StubCrmAdapter()
    app = create_app({
        'DB_PATH': ':memory:',
        'CRM_ADAPTER': crm,
        'CRM_BASE_URL': 'http://47.236.9.71:8310/enterprise-admin',
        'CRM_USERNAME': 'Hermes',
        'CRM_PASSWORD': '@Hermes123',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'AUTO_BIND_SIMULATION': True,
        'AUTO_BIND_SIMULATION_SUCCESS_RATE': 1.0,
    })
    client = TestClient(app)

    response = client.get('/api/ops/runtime-health')
    assert response.status_code == 200
    body = response.json()
    assert body['crm']['enabled'] is True
    assert body['crm']['base_url'] == 'http://47.236.9.71:8310/enterprise-admin'
    assert body['crm']['username'] == 'Hermes'
    assert body['lark']['default_app'] == 'Linky'
    assert body['lark']['default_guild'] == 'Piso'
    assert body['simulation']['auto_bind_simulation'] is True
    assert body['simulation']['success_rate'] == 1.0



def test_async_lark_ingress_queues_and_run_next_processes_job(tmp_path):
    reply = StubLarkReplyAdapter()
    app = create_app({
        'DB_PATH': str(tmp_path / 'async-queue.db'),
        'AUTO_LARK_REPLY': False,
        'LARK_APP_ID': 'cli_test',
        'LARK_REPLY_ADAPTER': reply,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Piso',
        'INGRESS_WORKER_ENABLED': False,
    })
    client = TestClient(app)

    payload = {
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_async_1'}},
            'message': {
                'message_id': 'om_async_1',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567999\\nPiso-31\\n55667789\\nCode EKVFGQ"}'
            }
        }
    }
    queued = client.post('/api/intake/lark/events', json=payload)
    assert queued.status_code == 200
    queued_body = queued.json()
    assert queued_body['accepted'] is True
    assert queued_body['queued'] is True
    assert queued_body['next_action'] == 'queued_for_processing'

    queue_rows = client.get('/api/ops/ingress-queue').json()['rows']
    assert queue_rows[0]['ingress_type'] == 'lark_event'
    assert queue_rows[0]['status'] == 'queued'

    processed = client.post('/api/ops/ingress-queue/run-next').json()
    assert processed['status'] == 'done'
    assert processed['result']['accepted'] is True
    assert processed['result']['next_action'] == 'queue_bind_check'

    audit_rows = client.get('/api/ops/operator-audit-log').json()['rows']
    assert any(row['event_type'] == 'ingress_event_enqueued' for row in audit_rows)
    assert any(row['event_type'] == 'ingress_event_processed' for row in audit_rows)



def test_async_lark_ingress_reuses_idempotent_event(tmp_path):
    app = create_app({
        'DB_PATH': str(tmp_path / 'async-idempotent.db'),
        'AUTO_LARK_REPLY': False,
        'INGRESS_WORKER_ENABLED': False,
    })
    client = TestClient(app)
    payload = {
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_async_dup'}},
            'message': {
                'message_id': 'om_async_dup',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567001\\nPiso-32\\n55667790"}'
            }
        }
    }
    first = client.post('/api/intake/lark/events', json=payload).json()
    second = client.post('/api/intake/lark/events', json=payload).json()
    assert first['ingress_event_id'] == second['ingress_event_id']
    assert second['duplicate'] is True



def test_ops_run_next_drains_bind_tasks_after_ingress_queue(tmp_path):
    def bind_simulator(context):
        return {
            'status': 'success',
            'result_code': 'bind_ok',
            'result_reason': f"simulated for {context['dept_name']}",
            'raw_result': {'guild_code': context['dept_name'], 'simulated': True},
        }

    app = create_app({
        'DB_PATH': str(tmp_path / 'async-drain.db'),
        'AUTO_LARK_REPLY': False,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Permata',
        'BIND_SIMULATOR': bind_simulator,
        'INGRESS_WORKER_ENABLED': False,
    })
    client = TestClient(app)
    payload = {
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_async_drain'}},
            'message': {
                'message_id': 'om_async_drain',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567021\\nPermata-77\\n55667721\\nCode EKVFGQ"}'
            }
        }
    }
    queued = client.post('/api/intake/lark/events', json=payload)
    assert queued.status_code == 200
    first = client.post('/api/ops/ingress-queue/run-next')
    assert first.status_code == 200
    assert first.json()['status'] == 'done'

    second = client.post('/api/ops/ingress-queue/run-next')
    assert second.status_code == 200
    second_body = second.json()
    assert second_body['status'] == 'success'
    assert second_body['task_type'] == 'bind_check'

    health = client.get('/api/ops/runtime-health').json()
    assert health['ingress']['pending_bind_tasks'] == 0
    assert health['ingress']['processing_bind_tasks'] == 0

    lead_id = first.json()['result']['lead_id']
    timeline = client.get(f"/api/leads/{lead_id}/timeline").json()
    bind_task = next(row for row in timeline['tasks'] if row['task_type'] == 'bind_check')
    assert bind_task['status'] == 'success'

    queue_rows = client.get('/api/ops/ingress-queue').json()['rows']
    assert queue_rows[0]['status'] == 'done'



def test_runtime_health_reports_configured_ingress_worker_count(tmp_path):
    app = create_app({
        'DB_PATH': str(tmp_path / 'async-workers.db'),
        'AUTO_LARK_REPLY': False,
        'INGRESS_WORKER_ENABLED': True,
        'INGRESS_WORKER_COUNT': 3,
    })
    client = TestClient(app)

    health = client.get('/api/ops/runtime-health').json()
    assert health['ingress']['worker_enabled'] is True
    assert health['ingress']['worker_count'] == 3
    assert health['ingress']['active_worker_threads'] >= 1



def test_process_next_automation_task_respects_guild_bind_concurrency_limits():
    captured = []

    def bind_simulator(context):
        captured.append(context)
        return {
            'status': 'failed',
            'result_code': 'bind_unauthorized',
            'result_reason': f"simulated for {context['dept_name']}",
            'raw_result': {'guild_code': context['dept_name']},
        }

    client = make_client({
        'LARK_APP_ID': 'cli_default_app',
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Permata',
        'AUTO_BIND_SIMULATION': False,
        'BIND_SIMULATOR': bind_simulator,
    })
    client.app.state.service.crm_adapter = StubCrmDropdownAdapter(
        apps=[{'id': 'app_1', 'name': 'Linky'}],
        depts=[{'id': 'dept_1', 'deptName': 'Permata'}, {'id': 'dept_2', 'deptName': 'Piso'}],
    )
    created = client.post('/api/ops/intake-bot-presets/intake-piso', json={
        'app_id': 'cli_piso_app',
        'default_app': 'Linky',
        'default_guild': 'Piso',
    })
    assert created.status_code == 200
    for guild_name, bind_concurrency in [('Permata', 1), ('Piso', 2)]:
        executor = client.post(f'/api/ops/guild-executors/{guild_name}', json={
            'backend_url': 'https://guild.linke.ai/guild/addAnchor',
            'login_username': f'{guild_name.lower()}@example.com',
            'password_secret_ref': f'secret_{guild_name.lower()}',
            'proxy_region': '厦门' if guild_name == 'Permata' else '福州',
            'proxy_type': 'http',
            'enabled': True,
            'browser_profile_key': f'{guild_name.lower()}-profile',
            'bind_concurrency': bind_concurrency,
        })
        assert executor.status_code == 200

    def submit(message_id: str, bot_app_id=None, phone_suffix='90', account_id='45678901', registration_group='Permata-25'):
        payload = {
            '_gateway_direct': True,
            'schema': '2.0',
            'header': {'event_type': 'im.message.receive_v1'},
            'event': {
                'sender': {'sender_id': {'open_id': f'ou_{message_id}'}},
                'message': {
                    'message_id': message_id,
                    'message_type': 'text',
                    'chat_type': 'p2p',
                    'content': json.dumps({'text': f'+62 812345678{phone_suffix}\n{registration_group}\n{account_id}\nCode EKVFGQ'})
                }
            }
        }
        if bot_app_id:
            payload['_bot_app_id'] = bot_app_id
        response = client.post('/api/intake/lark/events', json=payload)
        assert response.status_code == 200
        return response.json()

    first_perm = submit('om_perm_1', phone_suffix='90', account_id='45678901', registration_group='Permata-25')
    second_perm = submit('om_perm_2', phone_suffix='91', account_id='45678902', registration_group='Permata-25')
    piso = submit('om_piso_1', 'cli_piso_app', phone_suffix='92', account_id='45678903', registration_group='Piso-25')
    assert 'task_id' in first_perm, first_perm
    assert 'task_id' in second_perm, second_perm
    assert 'task_id' in piso, piso

    with client.app.state.service.db.connect() as conn:
        conn.execute(
            "UPDATE automation_tasks SET status = 'processing', started_at = ? WHERE task_id = ?",
            ('2026-04-20T10:00:05+00:00', first_perm['task_id']),
        )
        conn.commit()

    processed = client.app.state.service.process_next_automation_task()
    assert processed is not None
    assert captured[-1]['dept_name'] == 'Piso'
    assert processed['result_reason'] == 'simulated for Piso'

    timeline = client.get(f"/api/leads/{second_perm['lead_id']}/timeline").json()
    second_perm_task = next(task for task in timeline['tasks'] if task['task_id'] == second_perm['task_id'])
    assert second_perm_task['status'] == 'pending'



def test_runtime_health_reports_bind_latency_metrics(tmp_path):
    db_path = tmp_path / 'bind-latency.db'
    app = create_app({
        'DB_PATH': str(db_path),
        'AUTO_LARK_REPLY': False,
        'INGRESS_WORKER_ENABLED': False,
        'LARK_DEFAULT_APP_NAME': 'Linky',
        'LARK_DEFAULT_DEPT_NAME': 'Permata',
    })
    client = TestClient(app)

    response = client.post('/api/intake/lark/events', json={
        '_gateway_direct': True,
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_latency_metrics'}},
            'message': {
                'message_id': 'om_latency_metrics',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 85220623938\\nPERMATA-909\\n51654982\\nCode QFHVFL"}'
            }
        }
    })
    assert response.status_code == 200
    task_id = response.json()['task_id']
    lead_id = response.json()['lead_id']

    with client.app.state.service.db.connect() as conn:
        conn.execute(
            "UPDATE automation_tasks SET status = 'failed', created_at = ?, started_at = ?, finished_at = ?, result_code = ?, result_reason = ? WHERE task_id = ?",
            ('2026-04-20T10:00:00+00:00', '2026-04-20T10:00:05+00:00', '2026-04-20T10:00:11+00:00', 'bind_unauthorized', 'simulated', task_id),
        )
        conn.execute(
            "INSERT INTO sync_logs (sync_log_id, lead_id, task_id, sync_type, target_system, status, request_snapshot, response_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                'sync_test_1',
                lead_id,
                task_id,
                'customer_upsert',
                'crm',
                'success',
                json.dumps({'appName': 'Linky', 'deptName': 'Permata', 'pendaftaranGroup': 'PERMATA-909'}),
                json.dumps({'action': 'create', 'verified_after_write': True, 'crm_response': {'code': 0}}),
                '2026-04-20T10:00:12+00:00',
            ),
        )
        conn.commit()

    health = client.get('/api/ops/runtime-health').json()
    bind_metrics = health['ingress']['bind_metrics']
    assert bind_metrics['recent_completed_count'] >= 1
    assert bind_metrics['avg_queue_wait_seconds'] == 5.0
    assert bind_metrics['avg_execution_seconds'] == 6.0
    assert bind_metrics['avg_end_to_end_seconds'] == 11.0
    recent_bind = health['ingress']['recent_bind_traces'][0]
    assert recent_bind['task_id'] == task_id
    assert recent_bind['queue_wait_seconds'] == 5.0
    assert recent_bind['execution_seconds'] == 6.0
    recent_crm = health['ingress']['recent_crm_traces'][0]
    assert recent_crm['lead_id'] == lead_id
    assert recent_crm['verified_after_write'] is True
    assert recent_crm['crm_response_code'] == 0



def test_async_lark_ingress_rate_limits_by_sender(tmp_path):
    app = create_app({
        'DB_PATH': str(tmp_path / 'async-rate.db'),
        'AUTO_LARK_REPLY': False,
        'INGRESS_WORKER_ENABLED': False,
        'INGRESS_RATE_LIMIT_PER_MINUTE': 1,
    })
    client = TestClient(app)
    payload = {
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_async_rate'}},
            'message': {
                'message_id': 'om_async_rate_1',
                'message_type': 'text',
                'chat_type': 'p2p',
                'content': '{"text":"+62 81234567011\\nPiso-33\\n55667791"}'
            }
        }
    }
    first = client.post('/api/intake/lark/events', json=payload)
    assert first.status_code == 200
    payload['event']['message']['message_id'] = 'om_async_rate_2'
    second = client.post('/api/intake/lark/events', json=payload)
    assert second.status_code == 429
