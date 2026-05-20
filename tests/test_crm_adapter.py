from pathlib import Path
from unittest.mock import patch

from app.crm_adapter import LiveCrmAdapter


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.calls = []
        self.routes = {}

    def add(self, method, url, payload):
        key = (method.upper(), url)
        existing = self.routes.get(key)
        if existing is None:
            self.routes[key] = [payload]
        elif isinstance(existing, list):
            existing.append(payload)
        else:
            self.routes[key] = [existing, payload]

    def _next_payload(self, method, url):
        payloads = self.routes[(method.upper(), url)]
        if isinstance(payloads, list):
            if len(payloads) == 1:
                return payloads[0]
            return payloads.pop(0)
        return payloads

    def post(self, url, json=None, headers=None, files=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers, "files": bool(files)})
        return FakeResponse(self._next_payload("POST", url))

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return FakeResponse(self._next_payload("GET", url))

    def put(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": "PUT", "url": url, "json": json, "headers": headers})
        return FakeResponse(self._next_payload("PUT", url))


def test_login_stores_token():
    session = FakeSession()
    session.add("POST", "http://example.com/enterprise-admin/login", {"code": 0, "msg": "success", "data": {"token": "tok123", "expire": 43200}})
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)

    token = adapter.login()

    assert token == "tok123"
    assert adapter.token == "tok123"


def test_find_customer_passes_token_header_and_query_params():
    session = FakeSession()
    session.add("GET", "http://example.com/enterprise-admin/customer/ywcustomer/page", {"code": 0, "msg": "success", "data": {"total": 1, "list": [{"id": "1", "ywId": "456"}]}})
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)
    adapter.token = "tok123"

    row = adapter.find_customer(yw_id="456", mobile="123")

    assert row["id"] == "1"
    last = session.calls[-1]
    assert last["headers"]["token"] == "tok123"
    assert last["params"] == {"ywId": "456", "mobile": "123"}


class NonJsonResponse:
    def __init__(self, text="<html>502 Bad Gateway</html>", status_code=502):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": "text/html"}

    def json(self):
        raise ValueError("not json")


class NonJsonSession(FakeSession):
    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return NonJsonResponse()


def test_find_customer_returns_none_when_crm_response_is_not_json():
    session = NonJsonSession()
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)
    adapter.token = "tok123"

    row = adapter.find_customer(yw_id="456", mobile="123")

    assert row is None
    last = session.calls[-1]
    assert last["headers"]["token"] == "tok123"
    assert last["params"] == {"ywId": "456", "mobile": "123"}


def test_get_apps_can_lazy_login_before_first_request():
    session = FakeSession()
    session.add("POST", "http://example.com/enterprise-admin/login", {"code": 0, "msg": "success", "data": {"token": "***", "expire": 43200}})
    session.add("GET", "http://example.com/enterprise-admin/customer/ywapps/allList", {"code": 0, "msg": "success", "data": [{"id": "app_1", "name": "Linky"}]})
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)

    rows = adapter.get_apps()

    assert rows == [{"id": "app_1", "name": "Linky"}]
    assert adapter.token == "***"
    assert session.calls[0]["method"] == "POST"
    assert session.calls[1]["method"] == "GET"
    assert session.calls[1]["headers"]["token"] == "***"


def test_create_dept_posts_to_crm_dept_endpoint_with_token_header():
    session = FakeSession()
    session.add("POST", "http://example.com/enterprise-admin/sys/dept", {"code": 0, "msg": "success", "data": None})
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)
    adapter.token = "tok123"

    body = adapter.create_dept(name="Permata", pid=0, sort=0)

    assert body["code"] == 0
    last = session.calls[-1]
    assert last["url"].endswith("/sys/dept")
    assert last["json"] == {"pid": 0, "name": "Permata", "sort": 0}
    assert last["headers"]["token"] == "tok123"


def test_create_customer_retries_once_after_auth_error_by_relogging():
    session = FakeSession()
    session.add("POST", "http://example.com/enterprise-admin/login", {"code": 0, "msg": "success", "data": {"token": "***", "expire": 43200}})
    session.add("POST", "http://example.com/enterprise-admin/customer/ywcustomer", {"code": 401, "msg": "token expired", "data": None})
    session.add("POST", "http://example.com/enterprise-admin/login", {"code": 0, "msg": "success", "data": {"token": "***", "expire": 43200}})
    session.add("POST", "http://example.com/enterprise-admin/customer/ywcustomer", {"code": 0, "msg": "success", "data": None})
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)

    body = adapter.create_customer({"mobile": "177", "ywId": "199491", "appName": "Linky"})

    assert body["code"] == 0
    methods = [call["method"] for call in session.calls]
    assert methods == ["POST", "POST", "POST", "POST"]
    assert session.calls[1]["headers"]["token"] == "***"
    assert session.calls[3]["headers"]["token"] == "***"


def test_create_customer_raises_login_error_when_lazy_login_keeps_failing():
    class FailingLoginSession(FakeSession):
        def post(self, url, json=None, headers=None, files=None, timeout=None):
            self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers, "files": bool(files)})
            if url.endswith('/login'):
                return NonJsonResponse()
            return super().post(url, json=json, headers=headers, files=files, timeout=timeout)

    session = FailingLoginSession()
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)

    try:
        adapter.get_apps()
        assert False, 'expected RuntimeError'
    except RuntimeError as exc:
        assert 'CRM login returned non-JSON response' in str(exc)


def test_login_failure_backoff_skips_immediate_relogin_attempts():
    class FailingLoginSession(FakeSession):
        def post(self, url, json=None, headers=None, files=None, timeout=None):
            self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers, "files": bool(files)})
            if url.endswith('/login'):
                return NonJsonResponse()
            return super().post(url, json=json, headers=headers, files=files, timeout=timeout)

    session = FailingLoginSession()
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)
    adapter.login_retry_cooldown_seconds = 30

    with patch('app.crm_adapter.time.time', side_effect=[100.0, 100.0, 105.0]):
        for _ in range(2):
            try:
                adapter.get_apps()
            except RuntimeError:
                pass

    login_calls = [call for call in session.calls if call['url'].endswith('/login')]
    assert len(login_calls) == 1
    snapshot = adapter.health_snapshot()
    assert snapshot['status'] == 'degraded'
    assert snapshot['login_error'] is not None
    assert snapshot['login_retry_cooldown_seconds'] == 30



def test_health_snapshot_reports_healthy_after_successful_login():
    session = FakeSession()
    session.add("POST", "http://example.com/enterprise-admin/login", {"code": 0, "msg": "success", "data": {"token": "***", "expire": 43200}})
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)

    adapter.login()

    snapshot = adapter.health_snapshot()
    assert snapshot['status'] == 'healthy'
    assert snapshot['login_error'] is None
    assert snapshot['token_ready'] is True



def test_create_customer_posts_payload():
    session = FakeSession()
    session.add("POST", "http://example.com/enterprise-admin/customer/ywcustomer", {"code": 0, "msg": "success", "data": None})
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)
    adapter.token = "tok123"

    payload = {"mobile": "177", "ywId": "199491", "appName": "Linky"}
    body = adapter.create_customer(payload)

    assert body["code"] == 0
    last = session.calls[-1]
    assert last["json"] == payload
    assert last["headers"]["token"] == "tok123"


def test_create_customer_posts_to_automation_upsert_when_token_is_configured():
    session = FakeSession()
    session.add("POST", "http://example.com/enterprise-admin/customer/ywcustomer/automation/upsert", {
        "code": 0,
        "msg": "success",
        "data": {"success": True, "code": "SUCCESS", "customerId": 204, "ywId": 77123456},
    })
    adapter = LiveCrmAdapter(
        base_url="http://example.com/enterprise-admin",
        username="u",
        password="p",
        session=session,
        automation_token="auto-token",
    )

    payload = {"mobile": "933112345", "ywId": "77123456", "appName": "Linky", "deptName": "Piso", "pendaftaranGroup": "Piso-16"}
    body = adapter.create_customer(payload)

    assert body["code"] == 0
    assert body["automation"] is True
    last = session.calls[-1]
    assert last["url"].endswith("/customer/ywcustomer/automation/upsert")
    assert last["json"] == payload
    assert last["headers"]["X-Automation-Token"] == "auto-token"


def test_create_customer_maps_automation_duplicate_to_legacy_duplicate_code():
    session = FakeSession()
    session.add("POST", "http://example.com/enterprise-admin/customer/ywcustomer/automation/upsert", {
        "code": 0,
        "msg": "success",
        "data": {"success": False, "code": "DUPLICATE_SID", "message": "sid already exists", "customerId": 204},
    })
    adapter = LiveCrmAdapter(
        base_url="http://example.com/enterprise-admin",
        username="u",
        password="p",
        session=session,
        automation_token="auto-token",
    )

    body = adapter.create_customer({"mobile": "933112345", "ywId": "77123456", "appName": "Linky", "deptName": "Piso"})

    assert body["code"] == 10002
    assert body["msg"] == "Data duplication."
    assert body["data"]["code"] == "DUPLICATE_SID"


def test_verify_customer_uses_automation_verify_and_filters_empty_fields():
    session = FakeSession()
    session.add("POST", "http://example.com/enterprise-admin/customer/ywcustomer/automation/verify", {
        "code": 0,
        "msg": "success",
        "data": {"verified": True, "code": "SUCCESS", "customerId": 204, "ywId": 77123456},
    })
    adapter = LiveCrmAdapter(
        base_url="http://example.com/enterprise-admin",
        username="u",
        password="p",
        session=session,
        automation_token="auto-token",
    )

    body = adapter.verify_customer({
        "ywId": "77123456",
        "mobile": "933112345",
        "appName": "Linky",
        "deptName": "Piso",
        "pendaftaranGroup": "Piso-16",
        "wa": "",
        "remark": "ignored",
    })

    assert body["code"] == 0
    last = session.calls[-1]
    assert last["url"].endswith("/customer/ywcustomer/automation/verify")
    assert last["headers"]["X-Automation-Token"] == "auto-token"
    assert last["json"] == {
        "ywId": "77123456",
        "mobile": "933112345",
        "appName": "Linky",
        "deptName": "Piso",
        "pendaftaranGroup": "Piso-16",
    }


def test_update_customer_puts_payload():
    session = FakeSession()
    session.add("PUT", "http://example.com/enterprise-admin/customer/ywcustomer", {"code": 0, "msg": "success", "data": None})
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)
    adapter.token = "tok123"

    payload = {"id": "204", "deptName": "Piso"}
    body = adapter.update_customer(payload)

    assert body["code"] == 0
    assert session.calls[-1]["json"] == payload


def test_upload_voucher_requires_customer_id_and_returns_src(tmp_path: Path):
    image = tmp_path / "proof.png"
    image.write_bytes(b"fakepng")

    session = FakeSession()
    session.add("POST", "http://example.com/enterprise-admin/sys/oss/upload?id=204", {"code": 0, "msg": "success", "data": {"src": "http://oss/proof.png"}})
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)
    adapter.token = "tok123"

    src = adapter.upload_voucher(customer_id="204", image_path=str(image))

    assert src == "http://oss/proof.png"
    last = session.calls[-1]
    assert last["headers"]["token"] == "tok123"
    assert last["files"] is True


def test_attach_voucher_updates_file_url_and_pz_status():
    session = FakeSession()
    session.add("PUT", "http://example.com/enterprise-admin/customer/ywcustomer", {"code": 0, "msg": "success", "data": None})
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)
    adapter.token = "tok123"

    record = {"id": "204", "ywId": "45678991", "fileUrl": "", "pzStatus": 0}
    body = adapter.attach_voucher(record, "http://oss/proof.png", remark_suffix="uploaded")

    assert body["code"] == 0
    sent = session.calls[-1]["json"]
    assert sent["fileUrl"] == "http://oss/proof.png"
    assert sent["pzStatus"] == 1
    assert "uploaded" in sent["remark"]


def test_create_registration_group_batch_posts_expected_payload():
    session = FakeSession()
    session.add("POST", "http://example.com/enterprise-admin/customer/ywruquninfo", {"code": 0, "msg": "success", "data": None})
    adapter = LiveCrmAdapter(base_url="http://example.com/enterprise-admin", username="u", password="p", session=session)
    adapter.token = "tok123"

    payload = {"area": "Indonesia", "groupNo": "Piso-5", "groupPeopleNum": "30"}
    body = adapter.create_registration_group_batch(payload)

    assert body["code"] == 0
    last = session.calls[-1]
    assert last["headers"]["token"] == "tok123"
    assert last["json"] == payload
