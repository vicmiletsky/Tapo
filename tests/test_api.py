"""Tests for the TP-Link cloud client's Tapo handling (Tapo #1).

Every cloud call is mocked; nothing here signs in or talks to a device.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.tapo_cloud.api import (
    DEVICE_LIST_LEGACY,
    DEVICE_LIST_PAGE_SIZE,
    DEVICE_LIST_PAGED,
    PASSTHROUGH_V2_OBJECT,
    PASSTHROUGH_V2_STRING,
    PATH_TAPO_DEVICE_LIST,
    PATH_TAPO_PASSTHROUGH,
    TPLinkCloud,
    TPLinkCloudBridge,
    TPLinkCloudError,
    TPLinkDeviceOfflineError,
    TPLinkTokenExpiredError,
    TPLinkUnsupportedRequestError,
)

TAPO_HOST = "https://tapo-v2.example"
KASA_LEGACY_URL = "https://kasa-legacy.example"

PARAMETER_MISSING = {"error_code": -20104, "msg": "Parameter doesn't exist"}


class FakeCloud:
    """Records _post calls and answers them from a handler."""

    def __init__(self, cloud: TPLinkCloud, handler) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

        async def _post(base_url, url_path, body, with_token=True):
            self.calls.append((base_url, url_path, body))
            return handler(base_url, url_path, body)

        cloud._post = _post  # type: ignore[method-assign]


def _cloud(cloud_type: str = "tapo") -> TPLinkCloud:
    cloud = TPLinkCloud(MagicMock(), None, cloud_type, "term-id")
    cloud.host = TAPO_HOST if cloud_type == "tapo" else KASA_LEGACY_URL
    cloud.token = "token"
    return cloud


def _device(index: int, **extra: Any) -> dict[str, Any]:
    return {"deviceId": f"dev{index}", "deviceType": "SMART.TAPOBULB", "status": 1, **extra}


# ---- regional server -------------------------------------------------------


async def test_tapo_login_server_prefers_app_server_url_v2() -> None:
    tapo = _cloud("tapo")
    tapo.host = "https://stale-regional.example"
    fake = FakeCloud(
        tapo,
        lambda *_: {
            "error_code": 0,
            "result": {"appServerUrl": "https://legacy", "appServerUrlV2": "https://v2"},
        },
    )
    assert await tapo.async_lookup_host("user") == "https://v2"
    # Asked of the global endpoint, not the stored regional server.
    assert fake.calls[0][0] == "https://n-wap.i.tplinkcloud.com"


async def test_kasa_login_server_keeps_app_server_url() -> None:
    kasa = _cloud("kasa")
    FakeCloud(
        kasa,
        lambda *_: {
            "error_code": 0,
            "result": {"appServerUrl": "https://legacy", "appServerUrlV2": "https://v2"},
        },
    )
    assert await kasa.async_lookup_host("user") == "https://legacy"


# ---- Tapo device list --------------------------------------------------------


async def test_tapo_device_list_uses_paged_request() -> None:
    tapo = _cloud()
    devices = [_device(i) for i in range(DEVICE_LIST_PAGE_SIZE + 5)]

    def handler(base_url, url_path, body):
        assert url_path == PATH_TAPO_DEVICE_LIST
        start = body["index"]
        page = devices[start : start + body["limit"]]
        return {"error_code": 0, "result": {"totalNum": len(devices), "deviceList": page}}

    fake = FakeCloud(tapo, handler)
    result = await tapo.async_get_device_list()

    assert [d["deviceId"] for d in result] == [d["deviceId"] for d in devices]
    assert [call[2]["index"] for call in fake.calls] == [0, DEVICE_LIST_PAGE_SIZE]
    assert all(call[0] == TAPO_HOST for call in fake.calls)
    assert "SMART.TAPOBULB" in fake.calls[0][2]["deviceTypeList"]
    assert tapo.device_list_mode == DEVICE_LIST_PAGED


async def test_tapo_device_list_stops_when_server_ignores_paging() -> None:
    tapo = _cloud()
    page = [_device(i) for i in range(DEVICE_LIST_PAGE_SIZE)]
    fake = FakeCloud(
        tapo, lambda *_: {"error_code": 0, "result": {"deviceList": page}}
    )
    result = await tapo.async_get_device_list()
    assert len(result) == DEVICE_LIST_PAGE_SIZE
    assert len(fake.calls) == 2  # second page added nothing new


async def test_tapo_device_list_falls_back_to_legacy_and_remembers() -> None:
    tapo = _cloud()

    def handler(base_url, url_path, body):
        if url_path == PATH_TAPO_DEVICE_LIST:
            return PARAMETER_MISSING
        assert body == {"method": "getDeviceList"}
        return {"error_code": 0, "result": {"deviceList": [_device(1)]}}

    fake = FakeCloud(tapo, handler)
    assert [d["deviceId"] for d in await tapo.async_get_device_list()] == ["dev1"]
    assert tapo.device_list_mode == DEVICE_LIST_LEGACY
    assert len(fake.calls) == 3  # paged, paged_basic, legacy

    fake.calls.clear()
    await tapo.async_get_device_list()
    assert len(fake.calls) == 1  # the working request goes first now


async def test_tapo_device_list_reports_every_rejected_request() -> None:
    tapo = _cloud()
    FakeCloud(tapo, lambda *_: PARAMETER_MISSING)
    with pytest.raises(TPLinkUnsupportedRequestError) as err:
        await tapo.async_get_device_list()
    assert "paged" in str(err.value) and "legacy" in str(err.value)
    assert "-20104" in str(err.value)


async def test_device_list_token_errors_trigger_relogin() -> None:
    tapo = _cloud()
    FakeCloud(tapo, lambda *_: {"error_code": -20675, "msg": "Token invalid"})
    with pytest.raises(TPLinkTokenExpiredError):
        await tapo.async_get_device_list()


async def test_kasa_device_list_is_unchanged() -> None:
    kasa = _cloud("kasa")
    fake = FakeCloud(
        kasa, lambda *_: {"error_code": 0, "result": {"deviceList": [_device(1)]}}
    )
    await kasa.async_get_device_list()
    assert fake.calls == [(KASA_LEGACY_URL, "/", {"method": "getDeviceList"})]


async def test_http_404_is_an_unsupported_request() -> None:
    response = MagicMock(status=404, reason="Not Found")
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=context)
    cloud = TPLinkCloud(session, None, "tapo", "term-id")
    with pytest.raises(TPLinkUnsupportedRequestError):
        await cloud._post(TAPO_HOST, PATH_TAPO_PASSTHROUGH, {})


# ---- Tapo passthrough --------------------------------------------------------


async def test_tapo_passthrough_sends_request_data_as_object() -> None:
    tapo = _cloud()
    request = {"method": "get_device_info"}

    fake = FakeCloud(
        tapo,
        lambda *_: {
            "error_code": 0,
            "result": {"responseData": {"error_code": 0, "result": {"device_on": True}}},
        },
    )
    response = await tapo.async_passthrough(TAPO_HOST, "dev1", request)

    assert response == {"error_code": 0, "result": {"device_on": True}}
    assert fake.calls == [
        (TAPO_HOST, PATH_TAPO_PASSTHROUGH, {"deviceId": "dev1", "requestData": request})
    ]
    assert tapo.passthrough_mode == PASSTHROUGH_V2_OBJECT


async def test_tapo_passthrough_falls_back_to_string_request_data() -> None:
    tapo = _cloud()
    request = {"method": "get_device_info"}

    def handler(base_url, url_path, body):
        if not isinstance(body.get("requestData"), str):
            return PARAMETER_MISSING
        return {
            "error_code": 0,
            "result": {"responseData": json.dumps({"error_code": 0, "result": {}})},
        }

    fake = FakeCloud(tapo, handler)
    assert await tapo.async_passthrough(TAPO_HOST, "dev1", request) == {
        "error_code": 0,
        "result": {},
    }
    assert tapo.passthrough_mode == PASSTHROUGH_V2_STRING
    assert json.loads(fake.calls[-1][2]["requestData"]) == request


async def test_tapo_passthrough_device_offline() -> None:
    tapo = _cloud()
    FakeCloud(tapo, lambda *_: {"error_code": -20571, "msg": "Device is offline"})
    with pytest.raises(TPLinkDeviceOfflineError):
        await tapo.async_passthrough(TAPO_HOST, "dev1", {"method": "get_device_info"})


async def test_kasa_passthrough_is_unchanged() -> None:
    kasa = _cloud("kasa")
    request = {"system": {"get_sysinfo": None}}
    fake = FakeCloud(
        kasa,
        lambda *_: {"error_code": 0, "result": {"responseData": json.dumps({"ok": 1})}},
    )
    assert await kasa.async_passthrough(KASA_LEGACY_URL, "dev1", request) == {"ok": 1}
    base_url, url_path, body = fake.calls[0]
    assert (base_url, url_path, body["method"]) == (KASA_LEGACY_URL, "/", "passthrough")
    assert json.loads(body["params"]["requestData"]) == request


# ---- bridge ------------------------------------------------------------------


def _bridge() -> TPLinkCloudBridge:
    bridge = TPLinkCloudBridge(MagicMock(), None, "user", "password", "term-id")
    bridge.kasa.token = "kasa-token"
    bridge.tapo.token = "tapo-token"
    bridge.tapo.host = TAPO_HOST
    return bridge


async def test_merge_keeps_the_entry_of_the_cloud_the_device_is_on() -> None:
    bridge = _bridge()
    bridge.kasa.async_get_device_list = AsyncMock(
        return_value=[
            {"deviceId": "bulb", "deviceType": "SMART.TAPOBULB", "status": 0},
            {"deviceId": "hs100", "deviceType": "IOT.SMARTPLUGSWITCH", "status": 1},
        ]
    )
    bridge.tapo.async_get_device_list = AsyncMock(
        return_value=[
            {"deviceId": "bulb", "deviceType": "SMART.TAPOBULB", "status": 1},
            {"deviceId": "hs100", "deviceType": "IOT.SMARTPLUGSWITCH", "status": 0},
            {"deviceId": "p100", "deviceType": "SMART.TAPOPLUG", "status": 1},
        ]
    )
    devices = {d["deviceId"]: d for d in await bridge.async_get_devices()}

    assert (devices["bulb"]["cloud_type"], devices["bulb"]["status"]) == ("tapo", 1)
    assert (devices["hs100"]["cloud_type"], devices["hs100"]["status"]) == ("kasa", 1)
    assert devices["p100"]["cloud_type"] == "tapo"


async def test_merge_prefers_tapo_entry_for_tapo_devices_on_a_tie() -> None:
    bridge = _bridge()
    bridge.kasa.async_get_device_list = AsyncMock(
        return_value=[{"deviceId": "bulb", "deviceType": "SMART.TAPOBULB", "status": 0}]
    )
    bridge.tapo.async_get_device_list = AsyncMock(
        return_value=[{"deviceId": "bulb", "deviceType": "SMART.TAPOBULB", "status": 0}]
    )
    (device,) = await bridge.async_get_devices()
    assert device["cloud_type"] == "tapo"


async def test_tapo_list_failure_keeps_kasa_devices(caplog) -> None:
    bridge = _bridge()
    bridge.kasa.async_get_device_list = AsyncMock(
        return_value=[{"deviceId": "bulb", "deviceType": "SMART.TAPOBULB", "status": 0}]
    )
    bridge.tapo.async_get_device_list = AsyncMock(
        side_effect=TPLinkCloudError("boom (-1)")
    )
    (device,) = await bridge.async_get_devices()
    assert device["cloud_type"] == "kasa"
    assert "Tapo cloud device list failed: boom (-1)" in caplog.text


async def test_tapo_device_listed_only_by_kasa_is_relayed_through_tapo() -> None:
    bridge = _bridge()
    bridge.kasa.async_passthrough = AsyncMock()
    bridge.tapo.async_passthrough = AsyncMock(return_value={"error_code": 0})
    device = {
        "deviceId": "bulb",
        "deviceType": "SMART.TAPOBULB",
        "status": 0,
        "cloud_type": "kasa",
        "appServerUrl": KASA_LEGACY_URL,
    }
    request = {"method": "get_device_info"}

    assert await bridge.async_device_request(device, request) == {"error_code": 0}
    bridge.tapo.async_passthrough.assert_awaited_once_with(TAPO_HOST, "bulb", request)
    bridge.kasa.async_passthrough.assert_not_called()


async def test_device_request_falls_back_to_the_other_cloud_and_remembers() -> None:
    bridge = _bridge()
    bridge.tapo.async_passthrough = AsyncMock(side_effect=TPLinkCloudError("nope"))
    bridge.kasa.async_passthrough = AsyncMock(return_value={"ok": 1})
    device = {
        "deviceId": "bulb",
        "deviceType": "SMART.TAPOBULB",
        "status": 0,
        "cloud_type": "kasa",
        "appServerUrl": KASA_LEGACY_URL,
    }
    request = {"system": {"get_sysinfo": None}}

    assert await bridge.async_device_request(device, request) == {"ok": 1}
    bridge.kasa.async_passthrough.assert_awaited_once_with(
        KASA_LEGACY_URL, "bulb", request
    )

    bridge.tapo.async_passthrough.reset_mock()
    await bridge.async_device_request(device, request)
    bridge.tapo.async_passthrough.assert_not_called()


async def test_device_request_reports_offline_when_every_cloud_does() -> None:
    bridge = _bridge()
    bridge.tapo.async_passthrough = AsyncMock(
        side_effect=TPLinkDeviceOfflineError("offline (-20571)")
    )
    bridge.kasa.async_passthrough = AsyncMock(
        side_effect=TPLinkDeviceOfflineError("offline (-20571)")
    )
    device = {
        "deviceId": "bulb",
        "deviceType": "SMART.TAPOBULB",
        "status": 0,
        "cloud_type": "kasa",
    }
    with pytest.raises(TPLinkDeviceOfflineError) as err:
        await bridge.async_device_request(device, {"method": "get_device_info"})
    assert "tapo cloud" in str(err.value) and "kasa cloud" in str(err.value)


async def test_iot_device_uses_only_its_own_cloud() -> None:
    bridge = _bridge()
    bridge.tapo.async_passthrough = AsyncMock()
    bridge.kasa.async_passthrough = AsyncMock(side_effect=TPLinkCloudError("offline"))
    device = {
        "deviceId": "hs100",
        "deviceType": "IOT.SMARTPLUGSWITCH",
        "status": 1,
        "cloud_type": "kasa",
        "appServerUrl": KASA_LEGACY_URL,
    }
    with pytest.raises(TPLinkCloudError):
        await bridge.async_device_request(device, {"system": {"get_sysinfo": None}})
    bridge.tapo.async_passthrough.assert_not_called()


async def test_stale_stored_tapo_host_is_replaced_once() -> None:
    bridge = _bridge()
    bridge.kasa.async_get_device_list = AsyncMock(return_value=[])
    bridge.tapo.host = "https://legacy.example"

    async def device_list():
        if bridge.tapo.host != TAPO_HOST:
            raise TPLinkUnsupportedRequestError("rejected (-20104)")
        return [{"deviceId": "bulb", "deviceType": "SMART.TAPOBULB", "status": 1}]

    bridge.tapo.async_get_device_list = device_list
    bridge.tapo.async_lookup_host = AsyncMock(return_value=TAPO_HOST)

    (device,) = await bridge.async_get_devices()
    assert device["cloud_type"] == "tapo"
    assert bridge.tapo.host == TAPO_HOST


async def test_stale_tapo_host_is_restored_if_the_new_one_fails_too() -> None:
    bridge = _bridge()
    bridge.kasa.async_get_device_list = AsyncMock(return_value=[{"deviceId": "x"}])
    bridge.tapo.host = "https://legacy.example"
    bridge.tapo.async_get_device_list = AsyncMock(
        side_effect=TPLinkUnsupportedRequestError("rejected (-20104)")
    )
    bridge.tapo.async_lookup_host = AsyncMock(return_value=TAPO_HOST)

    await bridge.async_get_devices()
    assert bridge.tapo.host == "https://legacy.example"

    # Only looked up once per session.
    await bridge.async_get_devices()
    bridge.tapo.async_lookup_host.assert_awaited_once()


# ---- Tapo login that asks for an emailed code (Tapo #1) ---------------------

from custom_components.tapo_cloud.api import (  # noqa: E402
    PATH_LOGIN,
    PATH_MFA_LOGIN,
    PATH_SEND_MFA_EMAIL,
    TPLinkAuthError,
    TPLinkMFARequiredError,
)


async def test_tapo_login_without_token_but_mfa_process_needs_a_code() -> None:
    """error_code 0 with an mfaProcessId and no token is an MFA challenge.

    Treating it as success left the token empty, and every later request
    was answered with -20104 "Parameter doesn't exist".
    """
    tapo = _cloud()
    tapo.token = None

    def handler(base_url, url_path, body):
        if url_path == PATH_LOGIN:
            return {"error_code": 0, "result": {"mfaProcessId": "proc-1", "mfaType": 1}}
        return {"error_code": 0, "result": {"appServerUrlV2": TAPO_HOST}}

    FakeCloud(tapo, handler)
    with pytest.raises(TPLinkMFARequiredError) as err:
        await tapo.async_login("user", "password")
    assert err.value.mfa_process_id == "proc-1"
    assert tapo.mfa_process_id == "proc-1"
    assert tapo.token is None


async def test_login_without_token_or_mfa_process_fails() -> None:
    tapo = _cloud()
    tapo.token = None

    def handler(base_url, url_path, body):
        if url_path == PATH_LOGIN:
            return {"error_code": 0, "result": {}}
        return {"error_code": 0, "result": {"appServerUrlV2": TAPO_HOST}}

    FakeCloud(tapo, handler)
    with pytest.raises(TPLinkCloudError, match="no token"):
        await tapo.async_login("user", "password")
    assert tapo.token is None


async def test_tapo_mfa_code_is_requested_and_verified() -> None:
    tapo = _cloud()
    tapo.token = None
    tapo.mfa_process_id = "proc-1"

    def handler(base_url, url_path, body):
        if url_path == PATH_SEND_MFA_EMAIL:
            return {"error_code": 0, "result": {}}
        assert url_path == PATH_MFA_LOGIN
        if body["code"] != "123456":
            return {"error_code": -20607, "msg": "Wrong code"}
        return {"error_code": 0, "result": {"token": "tok", "refreshToken": "ref"}}

    fake = FakeCloud(tapo, handler)
    await tapo.async_send_mfa_code("user", "password")
    assert fake.calls[0][1:] == (
        PATH_SEND_MFA_EMAIL,
        {
            "appType": "TP-Link_Tapo_Android",
            "cloudUserName": "user",
            "cloudPassword": "password",
            "terminalUUID": "term-id",
            "locale": "en_US",
        },
    )

    fake.calls.clear()
    with pytest.raises(TPLinkAuthError):
        await tapo.async_verify_mfa("user", "password", "000000")
    assert len(fake.calls) == 1  # a wrong code spends a single attempt

    await tapo.async_verify_mfa("user", "password", "123456")
    body = fake.calls[-1][2]
    # Both known shapes in one request.
    assert body["mfaProcessId"] == "proc-1"
    assert body["terminalBindEnabled"] is True
    assert body["cloudPassword"] == "password"
    assert body["terminalUUID"] == "term-id"
    assert (tapo.token, tapo.refresh_token, tapo.mfa_process_id) == ("tok", "ref", None)


async def test_mfa_verification_falls_back_on_parameter_errors() -> None:
    tapo = _cloud()
    tapo.token = None
    tapo.mfa_process_id = "proc-1"

    def handler(base_url, url_path, body):
        if "mfaProcessId" in body:
            return PARAMETER_MISSING
        return {"error_code": 0, "result": {"token": "tok"}}

    fake = FakeCloud(tapo, handler)
    await tapo.async_verify_mfa("user", "password", "123456")
    assert tapo.token == "tok"
    assert len(fake.calls) == 2
    assert "mfaProcessId" not in fake.calls[-1][2]


async def test_mfa_verification_reads_nested_errors() -> None:
    tapo = _cloud()
    tapo.mfa_process_id = "proc-1"
    FakeCloud(
        tapo,
        lambda *_: {
            "error_code": 0,
            "result": {"errorCode": "-20607", "errorMsg": "Incorrect verification code"},
        },
    )
    with pytest.raises(TPLinkAuthError, match="Incorrect verification code"):
        await tapo.async_verify_mfa("user", "password", "000000")


def _login_answer(result: dict) -> Any:
    def handler(base_url, url_path, body):
        if url_path == PATH_LOGIN:
            return {"error_code": 0, "result": result}
        return {"error_code": 0, "result": {"appServerUrlV2": TAPO_HOST}}

    return handler


# The shape TP-Link was seen answering with (values made up): success on
# the outside, an MFA challenge inside, no token.
NESTED_MFA_RESULT = {
    "MFAProcessId": "proc-9",
    "email": "u***@example.com",
    "errorCode": "-20677",
    "errorMsg": "Two-step verification required",
    "failedAttempts": 0,
    "lockedMinutes": 0,
    "mfaEmail": "u***@example.com",
    "remainAttempts": 5,
    "supportedMFATypes": [1],
}


@pytest.mark.parametrize("cloud_type", ["kasa", "tapo"])
async def test_nested_mfa_challenge_is_recognised(cloud_type: str) -> None:
    cloud = _cloud(cloud_type)
    cloud.token = None
    FakeCloud(cloud, _login_answer(NESTED_MFA_RESULT))
    with pytest.raises(TPLinkMFARequiredError) as err:
        await cloud.async_login("user", "password")
    assert err.value.mfa_process_id == "proc-9"
    assert cloud.mfa_process_id == "proc-9"


async def test_nested_mfa_challenge_without_process_id() -> None:
    kasa = _cloud("kasa")
    kasa.token = None
    FakeCloud(kasa, _login_answer({"errorCode": -20677, "errorMsg": "MFA"}))
    with pytest.raises(TPLinkMFARequiredError):
        await kasa.async_login("user", "password")
    assert kasa.mfa_process_id is None


async def test_nested_wrong_password_is_an_auth_error() -> None:
    kasa = _cloud("kasa")
    kasa.token = None
    FakeCloud(
        kasa,
        _login_answer({"errorCode": "-20601", "errorMsg": "Incorrect email or password"}),
    )
    with pytest.raises(TPLinkAuthError, match="Incorrect email or password"):
        await kasa.async_login("user", "password")


async def test_tokenless_login_reports_the_fields_it_got() -> None:
    kasa = _cloud("kasa")
    kasa.token = None
    FakeCloud(kasa, _login_answer({"accountId": "1", "email": "x"}))
    with pytest.raises(TPLinkCloudError, match=r"fields \['accountId', 'email'\]"):
        await kasa.async_login("user", "password")


async def test_legacy_mfa_challenge_is_unchanged() -> None:
    kasa = _cloud("kasa")
    kasa.token = None
    fake = FakeCloud(
        kasa, lambda *_: {"error_code": 0, "result": {"token": "tok"}}
    )
    await kasa.async_send_mfa_code("user", "password")  # no process: no request
    assert fake.calls == []
    await kasa.async_verify_mfa("user", "password", "123456")
    assert fake.calls[0][2] == {
        "appType": "Kasa_Android_Mix",
        "cloudPassword": "password",
        "cloudUserName": "user",
        "code": "123456",
        "terminalUUID": "term-id",
    }


async def test_tapo_mfa_during_polling_is_surfaced_even_with_kasa_devices() -> None:
    bridge = _bridge()
    bridge.kasa.async_get_device_list = AsyncMock(return_value=[{"deviceId": "x"}])
    bridge.tapo.async_get_device_list = AsyncMock(
        side_effect=TPLinkMFARequiredError("code needed", mfa_process_id="p")
    )
    with pytest.raises(TPLinkMFARequiredError):
        await bridge.async_get_devices()


async def test_tapo_login_rejection_still_keeps_kasa_only_accounts_working() -> None:
    bridge = _bridge()
    bridge.kasa.async_get_device_list = AsyncMock(return_value=[{"deviceId": "x"}])
    bridge.tapo.async_get_device_list = AsyncMock(
        side_effect=TPLinkAuthError("no Tapo account")
    )
    assert [d["deviceId"] for d in await bridge.async_get_devices()] == ["x"]
