"""Tests for the SMART-protocol helpers (Tapo #1)."""
from __future__ import annotations

import base64

import pytest

from custom_components.tapo_cloud.smart import (
    SmartDeviceError,
    UnsupportedCommandError,
    decode_base64_text,
    device_alias,
    iot_request_to_smart,
    smart_energy_to_emeter,
    smart_info_to_sysinfo,
    smart_result,
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest.mark.parametrize(
    ("raw", "device_type", "expected"),
    [
        (_b64("Smart Bulb"), "SMART.TAPOBULB", "Smart Bulb"),
        (_b64("Küche"), "SMART.TAPOPLUG", "Küche"),
        ("Lounge", "SMART.TAPOBULB", "Lounge"),  # not base64
        ("Desk", "SMART.TAPOBULB", "Desk"),  # base64, but not text
        (_b64("Smart Bulb"), "IOT.SMARTBULB", _b64("Smart Bulb")),  # Kasa: plain
        (None, "SMART.TAPOBULB", None),
    ],
)
def test_device_alias(raw, device_type, expected) -> None:
    assert device_alias(raw, device_type) == expected


def test_decode_base64_text_rejects_non_text() -> None:
    assert decode_base64_text(base64.b64encode(b"\x00\xff").decode()) is None
    assert decode_base64_text("") is None
    assert decode_base64_text(None) is None


def test_plug_info_maps_to_relay_state() -> None:
    sysinfo = smart_info_to_sysinfo({"device_on": False, "nickname": _b64("Heater")})
    assert sysinfo["relay_state"] == 0
    assert sysinfo["alias"] == "Heater"
    assert "light_state" not in sysinfo


def test_tunable_white_bulb() -> None:
    sysinfo = smart_info_to_sysinfo(
        {"device_on": True, "brightness": 10, "color_temp": 2700, "color_temp_range": [2500, 6500]}
    )
    assert sysinfo["light_state"] == {"on_off": 1, "brightness": 10, "color_temp": 2700}
    assert sysinfo["is_variable_color_temp"] and "is_color" not in sysinfo


def test_smart_result() -> None:
    assert smart_result({"error_code": 0, "result": {"a": 1}}) == {"a": 1}
    assert smart_result({"error_code": 0}) == {}
    with pytest.raises(SmartDeviceError):
        smart_result({"error_code": -1008})
    with pytest.raises(SmartDeviceError):
        smart_result(None)


def test_energy_is_converted_from_milliwatts() -> None:
    assert smart_energy_to_emeter({"current_power": 1500}) == {"power": 1.5}
    assert smart_energy_to_emeter({}) == {}


def test_emeter_data_is_preferred_for_power() -> None:
    assert smart_energy_to_emeter(
        {"current_power": 1500, "today_energy": 0},
        {"power_mw": 1600, "voltage_mv": 0, "current_ma": 0, "energy_wh": 0},
    ) == {"power": 1.6, "voltage": 0.0, "current": 0.0, "total": 0.0, "today": 0.0}


def test_color_temperature_command() -> None:
    request = {
        "smartlife.iot.smartbulb.lightingservice": {
            "transition_light_state": {"on_off": 1, "ignore_default": 1, "color_temp": 4000}
        }
    }
    assert iot_request_to_smart(request) == {
        "method": "set_device_info",
        "params": {"device_on": True, "color_temp": 4000},
    }


def test_light_off_command() -> None:
    request = {
        "smartlife.iot.smartbulb.lightingservice": {
            "transition_light_state": {"on_off": 0, "transition_period": 1000}
        }
    }
    assert iot_request_to_smart(request) == {
        "method": "set_device_info",
        "params": {"device_on": False},
    }


def test_child_outlet_commands_are_not_translated() -> None:
    with pytest.raises(UnsupportedCommandError):
        iot_request_to_smart(
            {"system": {"set_relay_state": {"state": 1}}, "context": {"child_ids": ["1"]}}
        )
    with pytest.raises(UnsupportedCommandError):
        iot_request_to_smart({"system": {"reboot": {"delay": 1}}})
