from app.live_bind_executor import LiveChromeBindExecutor


class FakeCmsExecutor(LiveChromeBindExecutor):
    def __init__(self, responses):
        super().__init__(profile_map={})
        self.responses = list(responses)
        self.calls = []

    def _cms_request_json(self, *, method, url, authorization, body=None, proxy_url=''):
        self.calls.append({"method": method, "url": url, "authorization": authorization, "body": body, "proxy_url": proxy_url})
        assert authorization == "Bearer secret-token"
        if not self.responses:
            raise AssertionError("unexpected CMS call")
        return self.responses.pop(0)


def test_cms_id_bind_does_not_require_chrome_profile_mapping_when_already_in_target_guild():
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

    assert result["status"] == "success"
    assert result["result_code"] == "bind_success"
    assert result["raw_result"]["executor_mode"] == "cms_id"
    assert all("addAnchor" not in call["url"] for call in executor.calls)


def test_cms_id_bind_calls_add_anchor_and_requires_post_bind_verification():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": []}},
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
        "body": {"sids": [12123121], "guild_id": 3432},
        "proxy_url": "",
    }]


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
    assert result["result_code"] == "cms_target_guild_ambiguous"
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

    assert result["status"] == "success"
    assert result["result_code"] == "bind_success"
    assert result["raw_result"]["cms_guild_id"] == "3432"
    assert result["raw_result"]["cms_guild_sid"] == "43536425"


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
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": []}},
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


def test_cms_id_bind_classifies_add_anchor_invalid_arguments_when_sid_stays_missing():
    executor = FakeCmsExecutor([
        [{"id": "3432", "guild_name": "Carote", "sid": "43536425"}],
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": []}},
        {"code": 1001, "message": "状态码: 400, 错误信息: invalid arguments"},
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": []}},
        {"code": 1000, "data": {"records": []}},
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
    assert result["result_code"] == "cms_add_anchor_invalid_arguments"
    assert result["raw_result"]["postcheck"] == "sid_not_found_or_not_anchor"


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

    assert result["status"] == "success"
    assert executor.calls
    assert {call["proxy_url"] for call in executor.calls} == {"http://proxy-xa:8080"}
