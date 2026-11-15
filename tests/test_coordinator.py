"""Tests for the cloud coordinator (Tapo #1)."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.tapo_cloud.coordinator import TapoCloudCoordinator


async def test_offline_device_skip_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A device the cloud reports with status 0 is skipped with a DEBUG line."""
    bridge = MagicMock()
    bridge.async_get_devices = AsyncMock(
        return_value=[
            {"deviceId": "dev1", "alias": "Lounge", "deviceModel": "L510E", "status": 0}
        ]
    )
    bridge.async_device_request = AsyncMock()

    coordinator = MagicMock()
    coordinator.entry.options = {"local_probe": False}
    coordinator.bridge = bridge

    caplog.set_level(logging.DEBUG, logger="custom_components.tapo_cloud.coordinator")
    devices = await TapoCloudCoordinator._async_update_data(coordinator)

    assert not devices["dev1"].online
    bridge.async_device_request.assert_not_called()
    assert "Skipping Lounge (L510E): cloud reports status 0, not online" in caplog.text


# ---- Tapo devices behind the cloud (Tapo #1, part 2) ------------------------

import base64  # noqa: E402

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.tapo_cloud import coordinator as coordinator_module  # noqa: E402
from custom_components.tapo_cloud.api import (  # noqa: E402
    TPLinkCloudError,
    TPLinkDeviceOfflineError,
)
from custom_components.tapo_cloud.coordinator import CloudDevice  # noqa: E402


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


L530_INFO = {
    "device_on": True,
    "brightness": 40,
    "hue": 120,
    "saturation": 80,
    "color_temp": 0,
    "color_temp_range": [2500, 6500],
    "nickname": _b64("Lounge lamp"),
}


def _real_coordinator(bridge: MagicMock) -> TapoCloudCoordinator:
    """A coordinator without the DataUpdateCoordinator machinery."""
    coordinator = TapoCloudCoordinator.__new__(TapoCloudCoordinator)
    coordinator.entry = MagicMock()
    coordinator.entry.options = {"local_probe": False}
    coordinator.entry.data = {}
    coordinator.bridge = bridge
    coordinator.hass = MagicMock()
    coordinator.data = None
    coordinator._emeter_support = {}
    coordinator._protocols = {}
    coordinator._probe_after = {}
    coordinator._emeter_data_support = {}
    return coordinator


def _bridge(devices: list[dict], responder) -> MagicMock:
    bridge = MagicMock()
    bridge.async_get_devices = AsyncMock(side_effect=lambda: [dict(d) for d in devices])
    bridge.async_device_request = AsyncMock(side_effect=responder)
    return bridge


def _smart_ok(result: dict) -> dict:
    return {"error_code": 0, "result": result}


async def test_tapo_bulb_state_is_read_with_get_device_info() -> None:
    bulb = {
        "deviceId": "bulb",
        "alias": _b64("Lounge lamp"),
        "deviceModel": "L530E",
        "deviceType": "SMART.TAPOBULB",
        "status": 1,
        "cloud_type": "tapo",
    }
    bridge = _bridge([bulb], lambda info, request: _smart_ok(L530_INFO))
    coordinator = _real_coordinator(bridge)

    device = (await coordinator._async_update_data())["bulb"]

    bridge.async_device_request.assert_awaited_once()
    assert bridge.async_device_request.await_args.args[1] == {"method": "get_device_info"}
    assert device.protocol == "smart"
    assert device.alias == "Lounge lamp"
    assert device.sysinfo["relay_state"] == 1
    assert device.sysinfo["light_state"] == {
        "on_off": 1,
        "brightness": 40,
        "hue": 120,
        "saturation": 80,
        "color_temp": 0,
    }
    assert device.sysinfo["is_color"] and device.sysinfo["is_variable_color_temp"]
    assert device.sysinfo["color_temp_range"] == [2500, 6500]


async def test_offline_listed_tapo_bulb_is_probed_and_shown_online() -> None:
    """The reporter's case: listed with status 0, but reachable."""
    bulb = {
        "deviceId": "bulb",
        "alias": "Lounge",
        "deviceModel": "L510E",
        "deviceType": "SMART.TAPOBULB",
        "status": 0,
        "cloud_type": "kasa",
    }
    bridge = _bridge([bulb], lambda info, request: _smart_ok({"device_on": False, "brightness": 70}))
    coordinator = _real_coordinator(bridge)

    device = (await coordinator._async_update_data())["bulb"]

    assert device.online and device.reachable
    assert device.sysinfo["light_state"] == {"on_off": 0, "brightness": 70}
    assert device.sysinfo["is_dimmable"] and "is_color" not in device.sysinfo


async def test_failed_offline_probe_backs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    plug = {
        "deviceId": "plug",
        "alias": "Heater",
        "deviceModel": "P100",
        "deviceType": "SMART.TAPOPLUG",
        "status": 0,
        "cloud_type": "kasa",
    }

    def offline(info, request):
        raise TPLinkDeviceOfflineError("Device is offline (-20571)")

    bridge = _bridge([plug], offline)
    coordinator = _real_coordinator(bridge)
    now = [1000.0]
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: now[0])

    device = (await coordinator._async_update_data())["plug"]
    assert not device.online
    # Offline is not a protocol problem: no get_sysinfo retry.
    assert bridge.async_device_request.await_count == 1

    now[0] += coordinator_module.OFFLINE_PROBE_BACKOFF - 1
    await coordinator._async_update_data()
    assert bridge.async_device_request.await_count == 1

    now[0] += 2
    await coordinator._async_update_data()
    assert bridge.async_device_request.await_count == 2


async def test_smart_device_falls_back_to_iot_sysinfo() -> None:
    plug = {
        "deviceId": "plug",
        "alias": "Heater",
        "deviceModel": "KP125M",
        "deviceType": "SMART.KASAPLUG",
        "status": 1,
        "cloud_type": "kasa",
    }

    def responder(info, request):
        if request == {"method": "get_device_info"}:
            return {"error_code": -1, "msg": "unsupported"}
        return {"system": {"get_sysinfo": {"relay_state": 1, "alias": "Heater"}}}

    bridge = _bridge([plug], responder)
    coordinator = _real_coordinator(bridge)

    device = (await coordinator._async_update_data())["plug"]
    assert device.protocol == "iot"
    assert device.sysinfo["relay_state"] == 1

    # The working protocol is asked first from now on.
    bridge.async_device_request.reset_mock()
    await coordinator._async_update_data()
    first_request = bridge.async_device_request.await_args_list[0].args[1]
    assert first_request == {"system": {"get_sysinfo": None}}


async def test_iot_devices_only_get_get_sysinfo() -> None:
    plug = {
        "deviceId": "hs100",
        "alias": "Fan",
        "deviceModel": "HS100",
        "deviceType": "IOT.SMARTPLUGSWITCH",
        "status": 1,
        "cloud_type": "kasa",
    }
    bridge = _bridge([plug], lambda info, request: {"system": {"get_sysinfo": {"relay_state": 0}}})
    coordinator = _real_coordinator(bridge)

    device = (await coordinator._async_update_data())["hs100"]
    assert [call.args[1] for call in bridge.async_device_request.await_args_list] == [
        {"system": {"get_sysinfo": None}}
    ]
    assert device.protocol == "iot" and device.alias == "Fan"


async def test_smart_energy_reading() -> None:
    plug = {
        "deviceId": "p110",
        "alias": "Washer",
        "deviceModel": "P110(EU)",
        "deviceType": "SMART.TAPOPLUG",
        "status": 1,
        "cloud_type": "tapo",
    }

    def responder(info, request):
        if request == {"method": "get_energy_usage"}:
            return _smart_ok({"current_power": 12500, "today_energy": 30})
        return _smart_ok({"device_on": True})

    coordinator = _real_coordinator(_bridge([plug], responder))
    device = (await coordinator._async_update_data())["p110"]
    # get_emeter_data answered without readings: power comes from usage.
    assert device.emeter == {"power": 12.5, "today": 0.03}
    assert device.emeter_supported


def _p410m(responder) -> tuple[TapoCloudCoordinator, MagicMock]:
    plug = {
        "deviceId": "p410",
        "alias": _b64("Balcony"),
        "deviceModel": "P410M(EU)",
        "deviceType": "SMART.TAPOPLUG",
        "status": 1,
        "cloud_type": "tapo",
    }
    bridge = _bridge([plug], responder)
    return _real_coordinator(bridge), bridge


def _methods(bridge: MagicMock) -> list[str]:
    return [
        call.args[1].get("method", "iot")
        for call in bridge.async_device_request.await_args_list
    ]


async def test_unlisted_tapo_plug_reports_full_energy_readings() -> None:
    """P410M is not in the model list; the plug is asked anyway."""

    def responder(info, request):
        method = request.get("method")
        if method == "get_energy_usage":
            return _smart_ok(
                {"current_power": 230000, "today_energy": 1250, "month_energy": 41000}
            )
        if method == "get_emeter_data":
            return _smart_ok(
                {"power_mw": 231500, "voltage_mv": 231200, "current_ma": 1003, "energy_wh": 98765}
            )
        return _smart_ok({"device_on": True})

    coordinator, _ = _p410m(responder)
    device = (await coordinator._async_update_data())["p410"]
    assert device.emeter == {
        "power": 231.5,
        "voltage": 231.2,
        "current": 1.003,
        "total": 98.765,
        "today": 1.25,
        "month": 41.0,
    }
    assert device.emeter_supported


async def test_plug_without_metering_is_asked_once() -> None:
    def responder(info, request):
        if request.get("method") == "get_device_info":
            return _smart_ok({"device_on": True})
        return {"error_code": -40106, "msg": "method not supported"}

    coordinator, bridge = _p410m(responder)
    device = (await coordinator._async_update_data())["p410"]
    assert device.emeter is None and device.emeter_supported is False
    assert _methods(bridge) == ["get_device_info", "get_energy_usage"]

    bridge.async_device_request.reset_mock()
    await coordinator._async_update_data()
    assert _methods(bridge) == ["get_device_info"]


async def test_cloud_hiccup_does_not_disable_energy_readings() -> None:
    fail = [True]

    def responder(info, request):
        method = request.get("method")
        if method == "get_device_info":
            return _smart_ok({"device_on": True})
        if fail[0]:
            raise TPLinkCloudError("timeout")
        if method == "get_energy_usage":
            return _smart_ok({"current_power": 5000})
        return {"error_code": -40106}

    coordinator, bridge = _p410m(responder)
    device = (await coordinator._async_update_data())["p410"]
    assert not device.emeter_supported

    fail[0] = False
    device = (await coordinator._async_update_data())["p410"]
    assert device.emeter == {"power": 5.0}

    # get_emeter_data is unsupported on this plug: not asked again.
    bridge.async_device_request.reset_mock()
    await coordinator._async_update_data()
    assert _methods(bridge) == ["get_device_info", "get_energy_usage"]


def _with_device(device: CloudDevice, responder) -> TapoCloudCoordinator:
    bridge = _bridge([], responder)
    coordinator = _real_coordinator(bridge)
    coordinator.data = {device.device_id: device}
    return coordinator


async def test_commands_to_smart_devices_are_translated() -> None:
    device = CloudDevice(
        info={"deviceId": "bulb", "deviceType": "SMART.TAPOBULB", "status": 1},
        sysinfo={},
        protocol="smart",
    )
    coordinator = _with_device(device, lambda info, request: {"error_code": 0})

    await coordinator.async_send_command("bulb", {"system": {"set_relay_state": {"state": 0}}})
    await coordinator.async_send_command(
        "bulb",
        {
            "smartlife.iot.smartbulb.lightingservice": {
                "transition_light_state": {
                    "on_off": 1,
                    "ignore_default": 1,
                    "transition_period": 500,
                    "brightness": 30,
                    "hue": 200,
                    "saturation": 50,
                    "color_temp": 0,
                }
            }
        },
    )
    sent = [call.args[1] for call in coordinator.bridge.async_device_request.await_args_list]
    assert sent == [
        {"method": "set_device_info", "params": {"device_on": False}},
        {
            "method": "set_device_info",
            "params": {
                "device_on": True,
                "brightness": 30,
                "hue": 200,
                "saturation": 50,
                "color_temp": 0,
            },
        },
    ]


async def test_smart_command_errors_are_reported() -> None:
    device = CloudDevice(
        info={"deviceId": "bulb", "deviceType": "SMART.TAPOBULB", "status": 1},
        protocol="smart",
    )
    coordinator = _with_device(device, lambda info, request: {"error_code": -1008})
    with pytest.raises(HomeAssistantError):
        await coordinator.async_send_command(
            "bulb", {"system": {"set_relay_state": {"state": 1}}}
        )


async def test_cloud_command_errors_become_home_assistant_errors() -> None:
    device = CloudDevice(
        info={"deviceId": "hs100", "deviceType": "IOT.SMARTPLUGSWITCH", "status": 1},
        protocol="iot",
    )

    def fail(info, request):
        raise TPLinkCloudError("Device is offline (-20571)")

    coordinator = _with_device(device, fail)
    with pytest.raises(HomeAssistantError):
        await coordinator.async_send_command(
            "hs100", {"system": {"set_relay_state": {"state": 1}}}
        )


async def test_iot_commands_are_sent_unchanged() -> None:
    device = CloudDevice(
        info={"deviceId": "hs100", "deviceType": "IOT.SMARTPLUGSWITCH", "status": 1},
        protocol="iot",
    )
    coordinator = _with_device(device, lambda info, request: {"ok": 1})
    request = {"system": {"set_relay_state": {"state": 1}}, "context": {"child_ids": ["1"]}}
    assert await coordinator.async_send_command("hs100", request) == {"ok": 1}
    assert coordinator.bridge.async_device_request.await_args.args[1] == request


async def test_reporters_account_end_to_end() -> None:
    """HallyAus/Tapo#1, against a fake cloud that answers like the report.

    The Kasa cloud lists the Tapo devices with status 0 and calls them
    offline; the Tapo cloud rejects the Kasa-style device list with
    -20104 but serves the paged list and V2 passthrough.
    """
    from custom_components.tapo_cloud.api import (
        PATH_TAPO_DEVICE_LIST,
        PATH_TAPO_PASSTHROUGH,
        TPLinkCloudBridge,
    )

    kasa_entries = [
        {"deviceId": "l510", "alias": _b64("Hall"), "deviceModel": "L510E",
         "deviceType": "SMART.TAPOBULB", "status": 0, "appServerUrl": "https://kasa"},
        {"deviceId": "p100", "alias": _b64("Heater"), "deviceModel": "P100",
         "deviceType": "SMART.TAPOPLUG", "status": 0, "appServerUrl": "https://kasa"},
    ]
    tapo_entries = [dict(entry, status=1) for entry in kasa_entries]
    states = {"l510": {"device_on": True, "brightness": 60}, "p100": {"device_on": False}}
    tapo_requests: list[tuple[str, dict]] = []

    async def kasa_post(base_url, url_path, body, with_token=True):
        if body.get("method") == "getDeviceList":
            return {"error_code": 0, "result": {"deviceList": [dict(e) for e in kasa_entries]}}
        return {"error_code": -20571, "msg": "Device is offline"}

    async def tapo_post(base_url, url_path, body, with_token=True):
        tapo_requests.append((url_path, body))
        if url_path == PATH_TAPO_DEVICE_LIST:
            return {"error_code": 0, "result": {"totalNum": 2, "deviceList": [dict(e) for e in tapo_entries]}}
        if url_path == PATH_TAPO_PASSTHROUGH and isinstance(body["requestData"], dict):
            request = body["requestData"]
            if request["method"] == "set_device_info":
                states[body["deviceId"]].update(request["params"])
                return {"error_code": 0, "result": {"responseData": {"error_code": 0}}}
            return {"error_code": 0, "result": {"responseData": {
                "error_code": 0, "result": states[body["deviceId"]]}}}
        return {"error_code": -20104, "msg": "Parameter doesn't exist"}

    bridge = TPLinkCloudBridge(MagicMock(), None, "user", "password", "term-id")
    bridge.kasa.token = bridge.tapo.token = "token"
    bridge.kasa._post = kasa_post
    bridge.tapo._post = tapo_post
    coordinator = _real_coordinator(bridge)

    devices = await coordinator._async_update_data()
    coordinator.data = devices

    assert {d.alias for d in devices.values()} == {"Hall", "Heater"}
    assert all(d.online and d.info["cloud_type"] == "tapo" for d in devices.values())
    assert devices["l510"].sysinfo["light_state"] == {"on_off": 1, "brightness": 60}
    assert devices["p100"].sysinfo["relay_state"] == 0

    await coordinator.async_send_command("p100", {"system": {"set_relay_state": {"state": 1}}})
    assert states["p100"]["device_on"] is True
    assert bridge.diagnostics()["tapo_device_list_mode"] == "paged"
    assert bridge.diagnostics()["tapo_passthrough_mode"] == "v2_object"


async def test_reporters_account_without_tapo_device_list() -> None:
    """Even if no Tapo device-list request works, Kasa-listed Tapo devices
    are relayed through the Tapo cloud and show up online."""
    from custom_components.tapo_cloud.api import PATH_TAPO_PASSTHROUGH, TPLinkCloudBridge

    entry = {"deviceId": "l510", "alias": _b64("Hall"), "deviceModel": "L510E",
             "deviceType": "SMART.TAPOBULB", "status": 0, "appServerUrl": "https://kasa"}

    async def kasa_post(base_url, url_path, body, with_token=True):
        if body.get("method") == "getDeviceList":
            return {"error_code": 0, "result": {"deviceList": [dict(entry)]}}
        return {"error_code": -20571, "msg": "Device is offline"}

    async def tapo_post(base_url, url_path, body, with_token=True):
        if url_path == PATH_TAPO_PASSTHROUGH:
            assert base_url != "https://kasa"
            return {"error_code": 0, "result": {"responseData": {
                "error_code": 0, "result": {"device_on": False, "brightness": 5}}}}
        return {"error_code": -20104, "msg": "Parameter doesn't exist"}

    bridge = TPLinkCloudBridge(MagicMock(), None, "user", "password", "term-id")
    bridge.kasa.token = bridge.tapo.token = "token"
    bridge.kasa._post = kasa_post
    bridge.tapo._post = tapo_post
    bridge.tapo.async_lookup_host = AsyncMock(return_value=bridge.tapo.host)

    device = (await _real_coordinator(bridge)._async_update_data())["l510"]
    assert device.online and device.protocol == "smart"
    assert device.sysinfo["light_state"] == {"on_off": 0, "brightness": 5}
