import urllib.request

from app.live_bind_executor import LiveChromeBindExecutor
from app.main import Database, Service


class FakeCmsExecutor(LiveChromeBindExecutor):
    def __init__(self, responses):
        super().__init__(profile_map={})
        self.responses = list(responses)
        self.calls = []

    def _cms_request_json(self, *, method, url, authorization, body=None, proxy_url='', timeout_seconds=8.0):
        self.calls.append({"method": method, "url": url, "authorization": authorization, "body": body, "proxy_url": proxy_url, "timeout_seconds": timeout_seconds})
        assert authorization == "Bearer secret-token"
        if not self.responses:
            raise AssertionError("unexpected CMS call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_cms_id_bind_does_not_treat_already_in_target_guild_as_success():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "3432", "guild_name": "Carote"}]}},
        {"code": 1000, "data": {"records": []}},
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
        "executor_browser_profile_key": "guild-carote",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "already_in_target_guild"
    assert result["result_reason"] == "Previously registered in this agency"
    assert result["raw_result"]["executor_mode"] == "cms_id"
    assert result["raw_result"]["precheck"] == "already_in_target_guild"
    assert all("addAnchor" not in call["url"] for call in executor.calls)


def test_cms_id_bind_calls_add_anchor_only_when_sid_exists_without_guild_and_requires_post_bind_verification():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "data": {"records": [{"sid": "12123121", "user_id": "9007199", "guild_id": "0", "guild_name": ""}]}},
        {"code": 1000, "success": True},
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "3432", "guild_name": "Carote"}]}},
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
    })

    assert result["status"] == "success"
    assert result["result_reason"] == "CMS bind verified"
    add_calls = [call for call in executor.calls if "addAnchor" in call["url"]]
    assert add_calls == [{
        "method": "POST",
        "url": "https://cms.linke.ai/api/admin/linky/industrial/streamer_detail/addAnchor",
        "authorization": "Bearer secret-token",
        "body": {"sids": ["12123121"], "guild_id": 3432},
        "proxy_url": "",
        "timeout_seconds": 8.0,
    }]



def test_cms_id_bind_uses_longer_cms_request_timeout_to_avoid_transient_timeouts():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "message": "success", "data": {"success_count": 1, "fail_count": 0, "fail_items": []}},
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
        "executor_request_timeout_seconds": 30,
    })

    assert result["status"] == "success"
    assert executor.calls
    assert {call["timeout_seconds"] for call in executor.calls} == {30.0}

def test_live_bind_http_html_error_is_normalized_before_reaching_customer_card():
    html_404 = """<html> <head><title>404 Not Found</title></head>
    <body bgcolor=\"white\"><center><h1>404 Not Found</h1></center>
    <hr><center>nginx/1.14.1</center></body></html>
    <!-- a padding to disable MSIE and Chrome friendly error page -->"""
    executor = LiveChromeBindExecutor(profile_map={})

    result = executor._interpret_result(
        context={"dept_name": "Carote"},
        invite_code="",
        guild_name="Carote",
        retained_before="",
        retained_after="",
        requests=[{"status": 404, "body": html_404}],
        final_page={"title": "404 Not Found", "url": "https://example.invalid/missing", "body": html_404},
    )

    assert result["status"] == "failed"
    assert result["result_code"] == "bind_backend_http_error"
    assert result["result_reason"] == "Binding upstream returned HTTP 404 Not Found; check executor URL or nginx route."
    assert "<html" not in result["result_reason"].lower()
    assert "nginx/1.14.1" not in result["result_reason"]


def test_lark_reply_never_surfaces_raw_html_error_to_customer_service():
    service = Service(Database(':memory:'))
    html_404 = """HTTP 404: <html> <head><title>404 Not Found</title></head>
    <body bgcolor=\"white\"><center><h1>404 Not Found</h1></center>
    <hr><center>nginx/1.14.1</center></body></html>"""

    reply = service._format_lark_reply_text({
        "reason": "bind_check_failed",
        "result_code": "bind_backend_http_error",
        "result_reason": html_404,
        "reply_phone": "+62 877-2209-0497",
        "reply_id": "53322723",
        "reply_group": "-",
        "reply_code_display": "-",
    })

    assert "<html" not in reply.lower()
    assert "nginx/1.14.1" not in reply
    assert "Binding upstream returned HTTP 404 Not Found" in reply


def test_non_cms_route_still_reports_missing_chrome_profile_mapping():
    executor = LiveChromeBindExecutor(profile_map={})
    result = executor({
        "bind_route": "guild_invite_code",
        "executor_browser_profile_key": "guild-carote",
        "executor_backend_url": "https://guild.linke.ai/guild",
        "invite_code": "ABC123",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "bind_executor_profile_not_configured"


def test_cms_id_bind_fails_closed_when_precheck_business_response_is_not_success():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1001, "message": "状态码: 400, 错误信息: invalid arguments"},
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "cms_precheck_untrusted"
    assert all("addAnchor" not in call["url"] for call in executor.calls)


def test_cms_id_bind_rejects_query_rows_without_matching_sid_as_untrusted():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "data": {"records": [{"guild_id": "3432", "guild_name": "Carote"}]}},
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "cms_precheck_untrusted"
    assert all("addAnchor" not in call["url"] for call in executor.calls)


def test_cms_id_bind_rejects_unknown_target_without_configured_cms_guild_lock():
    executor = FakeCmsExecutor([
        [{"id": "9000", "guild_name": "UnknownGuild", "sid": "90000000"}],
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "UnknownGuild",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "cms_target_guild_lock_missing"
    assert executor.calls == []


def test_cms_id_bind_rejects_ambiguous_contains_guild_match():
    executor = FakeCmsExecutor([
        [
            {"id": "3432", "guild_name": "Carote Main", "sid": "43536425"},
            {"id": "5078", "guild_name": "Carote2", "sid": "50781344"},
        ],
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "cms_target_guild_mismatch"
    assert all("addAnchor" not in call["url"] for call in executor.calls)


def test_cms_id_bind_uses_configured_guild_id_and_sid_to_disambiguate_same_name():
    executor = FakeCmsExecutor([
        [
            {"id": "9000", "guild_name": "Carote", "sid": "90000000"},
            {"id": "3432", "guild_name": "Carote", "sid": "43536425"},
        ],
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "3432", "guild_name": "Carote"}]}},
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
        "executor_cms_guild_id": "3432",
        "executor_cms_guild_sid": "43536425",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "already_in_target_guild"
    assert result["raw_result"]["cms_guild_id"] == "3432"
    assert result["raw_result"]["cms_guild_sid"] == "43536425"


def test_cms_id_bind_uses_configured_guild_lock_when_guild_list_endpoint_is_forbidden():
    executor = FakeCmsExecutor([
        urllib.error.HTTPError(
            url="https://cms.linke.ai/api/admin/linky/industrial/industrial/getGuildIdAndName",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        ),
        {"code": 1000, "data": {"records": [{"sid": "53367380", "guild_id": "1423", "guild_name": "Nova"}]}},
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "53367380",
        "dept_name": "Nova",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
        "executor_cms_guild_id": "1423",
        "executor_cms_guild_sid": "31350499",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "already_in_target_guild"
    assert result["raw_result"]["cms_guild_id"] == "1423"
    assert result["raw_result"]["cms_guild_sid"] == "31350499"
    assert all("addAnchor" not in call["url"] for call in executor.calls)


def test_cms_id_bind_classifies_add_anchor_403_as_scope_denied_after_sid_precheck():
    executor = FakeCmsExecutor([
        urllib.error.HTTPError(
            url="https://cms.linke.ai/api/admin/linky/industrial/industrial/getGuildIdAndName",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        ),
        {"code": 1000, "data": {"records": [{"sid": "53367380", "guild_id": "0", "guild_name": ""}]}},
        urllib.error.HTTPError(
            url="https://cms.linke.ai/api/admin/linky/industrial/streamer_detail/addAnchor",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        ),
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "53367380",
        "dept_name": "Nova",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
        "executor_cms_guild_id": "1423",
        "executor_cms_guild_sid": "31350499",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "cms_authorization_scope_denied"
    assert result["raw_result"]["precheck"] == "sid_found_without_guild"
    assert result["raw_result"]["cms_submit_http_status"] == 403


def test_cms_id_bind_rejects_configured_guild_id_sid_mismatch():
    executor = FakeCmsExecutor([
        [
            {"id": "3432", "guild_name": "Carote", "sid": "99999999"},
        ],
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
        "executor_cms_guild_id": "3432",
        "executor_cms_guild_sid": "43536425",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "cms_target_guild_not_visible"
    assert all("addAnchor" not in call["url"] for call in executor.calls)


def test_cms_id_bind_retries_postcheck_before_success():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "0", "guild_name": ""}]}},
        {"code": 1000, "success": True},
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "3432", "guild_name": "Carote"}]}},
    ])
    executor.cms_postcheck_retry_delay_seconds = 0

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
    })

    assert result["status"] == "success"
    assert result["result_code"] == "bind_success"
    assert result["raw_result"]["postcheck_attempts"] == 2


def test_cms_id_bind_prefers_join_record_when_top_level_guild_is_empty():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "data": {"records": [{"sid": "12123121", "user_id": "9007199", "guild_id": "0", "guild_name": "", "joinRecord": {"guild_id": "3432"}}]}},
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "already_in_target_guild"
    assert result["raw_result"]["precheck"] == "already_in_target_guild"
    assert all("addAnchor" not in call["url"] for call in executor.calls)


def test_cms_id_bind_treats_zero_empty_guild_as_unbound_not_other_agency():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "data": {"records": [{"sid": "12123121", "user_id": "9007199", "guild_id": "0", "guild_name": ""}]}},
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "success": True},
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "3432", "guild_name": "Carote"}]}},
    ])
    executor.cms_postcheck_retry_delay_seconds = 0

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
    })

    assert result["status"] == "success"
    assert result["result_code"] == "bind_success"
    assert result["raw_result"]["cms_submit_code"] == 1000
    assert any("addAnchor" in call["url"] for call in executor.calls)


def test_cms_id_bind_treats_existing_sid_unverified_submit_as_retryable_processing():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "data": {"records": [{"sid": "53273321", "user_id": "9007199", "guild_id": "0", "guild_name": ""}]}},
        {"code": 1001, "message": "状态码: 400, 错误信息: invalid arguments"},
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": []}},
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "53273321",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "cms_postcheck_timeout"
    assert "postcheck did not verify" in result["result_reason"]
    assert result["raw_result"]["precheck"] == "sid_found_without_guild"
    assert result["raw_result"]["cms_submit_code"] == 1001
    assert result["raw_result"]["postcheck_attempts"] == 3
    assert any("addAnchor" in call["url"] for call in executor.calls)


def test_cms_id_bind_reports_invalid_sid_without_calling_add_anchor_when_sid_is_not_found():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": []}},
    ])
    executor.cms_postcheck_retry_delay_seconds = 0

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "cms_sid_not_found"
    assert result["raw_result"]["precheck"] == "sid_not_found"
    assert all("addAnchor" not in call["url"] for call in executor.calls)


def test_cms_id_bind_sends_cms_requests_through_executor_proxy_url():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "3432", "guild_name": "Carote"}]}},
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
        "executor_proxy_url": "http://proxy-xa:8080",
        "executor_proxy_region": "西安",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "already_in_target_guild"
    assert executor.calls
    assert {call["proxy_url"] for call in executor.calls} == {"http://proxy-xa:8080"}


def test_cms_id_bind_maps_add_anchor_permission_error_to_authorization_scope_failure():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "0", "guild_name": ""}]}},
        {"code": 1003, "message": "permission denied: guild scope mismatch"},
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "0", "guild_name": ""}]}},
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "0", "guild_name": ""}]}},
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "0", "guild_name": ""}]}},
    ])
    executor.cms_postcheck_retry_delay_seconds = 0

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "cms_authorization_scope_denied"
    assert result["raw_result"]["cms_submit_error_category"] == "authorization_scope_denied"


def test_cms_id_bind_maps_add_anchor_invalid_arguments_to_manual_check_not_sid_invalid():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "0", "guild_name": ""}]}},
        {"code": 1001, "message": "状态码: 400, 错误信息: invalid arguments"},
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "0", "guild_name": ""}]}},
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "0", "guild_name": ""}]}},
        {"code": 1000, "data": {"records": [{"sid": "12123121", "guild_id": "0", "guild_name": ""}]}},
    ])
    executor.cms_postcheck_retry_delay_seconds = 0

    result = executor({
        "bind_route": "cms_id",
        "account_id": "12123121",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "cms_add_anchor_invalid_arguments_manual_check"
    assert result["raw_result"]["cms_submit_error_category"] == "invalid_arguments_manual_check"


def test_cms_request_matches_browser_add_anchor_header_and_payload_parity(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"code":1000,"success":true}'

    class FakeOpener:
        def open(self, req, timeout=0):
            captured['url'] = req.full_url
            captured['method'] = req.get_method()
            captured['data'] = req.data
            captured['headers'] = dict(req.header_items())
            captured['timeout'] = timeout
            return FakeResponse()

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args, **kwargs: FakeOpener())

    executor = LiveChromeBindExecutor(profile_map={})
    response = executor._cms_request_json(
        method='POST',
        url='https://cms.linke.ai/api/admin/linky/industrial/streamer_detail/addAnchor',
        authorization='cms-jwt-token',
        body={'sids': ['53279170'], 'guild_id': 3432},
        timeout_seconds=8,
    )

    assert response == {'code': 1000, 'success': True}
    assert captured['method'] == 'POST'
    assert captured['data'] == b'{"sids":["53279170"],"guild_id":3432}'
    headers = {k.lower(): v for k, v in captured['headers'].items()}
    assert headers['authorization'] == 'cms-jwt-token'
    assert headers['content-type'] == 'application/json'
    assert headers['accept'] == 'application/json, text/plain, */*'
    assert headers['origin'] == 'https://cms.linke.ai'
    assert headers['referer'] == 'https://cms.linke.ai/anchorDetails'
    assert headers['cookie'] == 'locale=zh-cn'
    assert headers['accept-language'] == 'zh-CN,zh;q=0.9'
    assert 'Mozilla/5.0' in headers['user-agent']


def test_cms_id_bind_uses_ka_addanchor_only_and_classifies_already_in_target():
    executor = FakeCmsExecutor([
        [{"id": "1423", "guild_name": "Nova", "sid": "31350499"}],
        {"code": 1000, "message": "success", "data": {"fail_count": 1, "fail_items": [{"sid": "53341442", "reason": "already in this guild"}]}},
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "53341442",
        "dept_name": "Nova",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
        "executor_cms_guild_id": "1423",
        "executor_cms_guild_sid": "31350499",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "already_in_target_guild"
    assert result["raw_result"]["cms_bind_flow"] == "ka_addanchor_only"
    assert result["raw_result"]["cms_submit_fail_items"] == [{"sid": "53341442", "reason": "already in this guild"}]
    assert not any("streamer_detail/page" in call["url"] for call in executor.calls)
    assert any("streamer_detail/addAnchor" in call["url"] for call in executor.calls)


def test_cms_id_bind_uses_ka_addanchor_only_and_classifies_other_guild():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "message": "success", "data": {"fail_count": 1, "fail_items": [{"sid": "53367380", "reason": "already_joined_another_guild_455_1"}]}},
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "53367380",
        "dept_name": "Carote",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
        "executor_cms_guild_id": "3432",
        "executor_cms_guild_sid": "43536425",
    })

    assert result["status"] == "failed"
    assert result["result_code"] == "already_in_other_guild"
    assert result["result_reason"] == "The streamer was in another agency"
    assert not any("streamer_detail/page" in call["url"] for call in executor.calls)


def test_cms_id_bind_ka_addanchor_success_count_is_success_without_detail_postcheck():
    executor = FakeCmsExecutor([
        [{"id": "1423", "guild_name": "Nova", "sid": "31350499"}],
        {"code": 1000, "message": "success", "data": {"success_count": 1, "fail_count": 0, "fail_items": []}},
    ])

    result = executor({
        "bind_route": "cms_id",
        "account_id": "53341442",
        "dept_name": "Nova",
        "executor_platform_backend_url": "https://cms.linke.ai/",
        "executor_platform_authorization": "Bearer secret-token",
        "executor_cms_guild_id": "1423",
        "executor_cms_guild_sid": "31350499",
    })

    assert result["status"] == "success"
    assert result["result_code"] == "bind_success"
    assert result["result_reason"] == "CMS KA-AddAnchor accepted"
    assert not any("streamer_detail/page" in call["url"] for call in executor.calls)
