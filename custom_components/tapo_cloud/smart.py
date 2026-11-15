"""SMART-protocol support for Tapo (and newer Kasa) devices.

Devices whose cloud deviceType starts with "SMART." do not speak the
legacy Kasa IOT protocol ({"system": {"get_sysinfo": null}}); they answer
{"method": "get_device_info"} and take {"method": "set_device_info"}.

To keep the entity platforms protocol-agnostic, SMART device info is
normalized into the IOT sysinfo shape they already read (relay_state,
light_state, capability flags), and the IOT-style commands they build are
translated back into SMART requests here.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any

PROTOCOL_IOT = "iot"
PROTOCOL_SMART = "smart"

LIGHT_SERVICE = "smartlife.iot.smartbulb.lightingservice"

SMART_INFO_REQUEST: dict[str, Any] = {"method": "get_device_info"}
SMART_ENERGY_REQUEST: dict[str, Any] = {"method": "get_energy_usage"}
# Newer energy-monitoring plugs also report voltage/current/energy here.
SMART_EMETER_DATA_REQUEST: dict[str, Any] = {"method": "get_emeter_data"}

# deviceType prefixes whose aliases the cloud returns base64-encoded.
_BASE64_ALIAS_TYPE_PREFIXES = ("SMART.TAPO", "SMART.IPCAMERA", "SMART.KASA")


class SmartDeviceError(Exception):
    """A SMART device answered with a non-zero error_code."""

    def __init__(self, error_code: Any) -> None:
        super().__init__(f"device returned error_code {error_code}")
        self.error_code = error_code


class UnsupportedCommandError(Exception):
    """An IOT-style command has no SMART equivalent here."""


def is_smart_device_type(device_type: str | None) -> bool:
    return (device_type or "").upper().startswith("SMART.")


def decode_base64_text(value: Any) -> str | None:
    """Decode a base64 string to text, or None if it is not base64 text."""
    if not isinstance(value, str) or not value:
        return None
    try:
        text = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not text or not text.isprintable():
        return None
    return text


def device_alias(raw_alias: Any, device_type: str | None) -> str | None:
    """Readable alias for a cloud device-list entry."""
    if not isinstance(raw_alias, str) or not raw_alias:
        return None
    if (device_type or "").upper().startswith(_BASE64_ALIAS_TYPE_PREFIXES):
        return decode_base64_text(raw_alias) or raw_alias
    return raw_alias


def smart_result(response: Any) -> dict[str, Any]:
    """The result of a SMART response, raising on device errors."""
    if not isinstance(response, dict):
        raise SmartDeviceError("no response")
    error_code = response.get("error_code", 0)
    if error_code != 0:
        raise SmartDeviceError(error_code)
    result = response.get("result")
    return result if isinstance(result, dict) else {}


def smart_info_to_sysinfo(info: dict[str, Any]) -> dict[str, Any]:
    """Normalize get_device_info into the IOT sysinfo shape."""
    sysinfo: dict[str, Any] = {"smart_info": info}
    alias = decode_base64_text(info.get("nickname"))
    if alias:
        sysinfo["alias"] = alias

    device_on = info.get("device_on")
    if device_on is not None:
        sysinfo["relay_state"] = 1 if device_on else 0

    has_brightness = info.get("brightness") is not None
    has_color = info.get("hue") is not None and info.get("saturation") is not None
    has_color_temp = "color_temp_range" in info or (
        has_color and info.get("color_temp") is not None
    )
    if has_brightness or has_color or has_color_temp:
        light_state: dict[str, Any] = {"on_off": 1 if device_on else 0}
        if has_brightness:
            sysinfo["is_dimmable"] = 1
            light_state["brightness"] = info["brightness"]
        if has_color:
            sysinfo["is_color"] = 1
            light_state["hue"] = info["hue"]
            light_state["saturation"] = info["saturation"]
        if has_color_temp:
            sysinfo["is_variable_color_temp"] = 1
            light_state["color_temp"] = info.get("color_temp") or 0
            temp_range = info.get("color_temp_range")
            if (
                isinstance(temp_range, list)
                and len(temp_range) == 2
                and all(isinstance(value, int) and value > 0 for value in temp_range)
            ):
                sysinfo["color_temp_range"] = temp_range
        sysinfo["light_state"] = light_state
    return sysinfo


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def smart_energy_to_emeter(
    usage: dict[str, Any], emeter_data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Normalize SMART energy readings to emeter units (W, V, A, kWh).

    get_energy_usage: current_power (mW), today_energy/month_energy (Wh).
    get_emeter_data: power_mw, voltage_mv, current_ma, energy_wh.
    """
    data = emeter_data or {}
    normalized: dict[str, Any] = {}
    if _number(data.get("power_mw")):
        normalized["power"] = data["power_mw"] / 1000
    elif _number(usage.get("current_power")):
        normalized["power"] = usage["current_power"] / 1000
    for key, source, values in (
        ("voltage", "voltage_mv", data),
        ("current", "current_ma", data),
        ("total", "energy_wh", data),
        ("today", "today_energy", usage),
        ("month", "month_energy", usage),
    ):
        if _number(values.get(source)):
            normalized[key] = values[source] / 1000
    return normalized


def iot_request_to_smart(request: dict[str, Any]) -> dict[str, Any]:
    """Translate an IOT-style command built by the platforms to SMART."""
    if "context" in request:
        raise UnsupportedCommandError("child outlets are not supported over SMART")

    relay = (request.get("system") or {}).get("set_relay_state")
    if isinstance(relay, dict) and "state" in relay:
        return {
            "method": "set_device_info",
            "params": {"device_on": bool(relay["state"])},
        }

    state = (request.get(LIGHT_SERVICE) or {}).get("transition_light_state")
    if isinstance(state, dict):
        params: dict[str, Any] = {}
        if "on_off" in state:
            params["device_on"] = bool(state["on_off"])
        if state.get("brightness") is not None:
            params["brightness"] = int(state["brightness"])
        if state.get("hue") is not None and state.get("saturation") is not None:
            params["hue"] = int(state["hue"])
            params["saturation"] = int(state["saturation"])
            params["color_temp"] = 0
        elif state.get("color_temp"):
            params["color_temp"] = int(state["color_temp"])
        if params:
            return {"method": "set_device_info", "params": params}

    raise UnsupportedCommandError(f"no SMART equivalent for {list(request)}")
