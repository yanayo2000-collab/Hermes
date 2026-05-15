from app.live_bind_executor import LiveChromeBindExecutor


class FakeCmsExecutor(LiveChromeBindExecutor):
    def __init__(self, responses):
        super().__init__(profile_map={})
        self.responses = list(responses)
        self.calls = []

    def _cms_request_json(self, *, method, url, authorization, body=None):
        self.calls.append({"method": method, "url": url, "authorization": authorization, "body": body})
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
