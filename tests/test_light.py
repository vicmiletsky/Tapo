"""Light entity behaviour for Tapo (SMART protocol) bulbs (Tapo #1)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.light import ColorMode, LightEntityFeature

from custom_components.tapo_cloud.coordinator import CloudDevice, TapoCloudCoordinator
from custom_components.tapo_cloud.light import TapoCloudLight
from custom_components.tapo_cloud.smart import smart_info_to_sysinfo


def _light(info: dict, model: str) -> tuple[TapoCloudLight, AsyncMock]:
    device = CloudDevice(
        info={
            "deviceId": "bulb",
            "deviceModel": model,
            "deviceType": "SMART.TAPOBULB",
            "status": 1,
        },
        sysinfo=smart_info_to_sysinfo(info),
        protocol="smart",
        reachable=True,
    )
    coordinator = TapoCloudCoordinator.__new__(TapoCloudCoordinator)
    coordinator.data = {"bulb": device}
    coordinator.last_update_success = True
    coordinator.bridge = MagicMock()
    coordinator.bridge.async_device_request = AsyncMock(return_value={"error_code": 0})
    coordinator.async_request_refresh = AsyncMock()
    return TapoCloudLight(coordinator, "bulb"), coordinator.bridge.async_device_request


async def test_dimmable_tapo_bulb() -> None:
    light, send = _light({"device_on": True, "brightness": 50}, "L510E")
    assert light.available
    assert light.is_on
    assert light.supported_color_modes == {ColorMode.BRIGHTNESS}
    assert light.brightness == round(50 * 255 / 100)
    assert light.supported_features == LightEntityFeature(0)

    await light.async_turn_on(brightness=255)
    assert send.await_args.args[1] == {
        "method": "set_device_info",
        "params": {"device_on": True, "brightness": 100},
    }
    await light.async_turn_off()
    assert send.await_args.args[1] == {
        "method": "set_device_info",
        "params": {"device_on": False},
    }


async def test_color_tapo_bulb() -> None:
    light, send = _light(
        {
            "device_on": False,
            "brightness": 80,
            "hue": 30,
            "saturation": 100,
            "color_temp": 0,
            "color_temp_range": [2500, 6500],
        },
        "L530E",
    )
    assert not light.is_on
    assert light.supported_color_modes == {ColorMode.HS, ColorMode.COLOR_TEMP}
    assert light.color_mode == ColorMode.HS
    assert light.hs_color == (30.0, 100.0)
    assert (light.min_color_temp_kelvin, light.max_color_temp_kelvin) == (2500, 6500)

    await light.async_turn_on(color_temp_kelvin=9000)
    assert send.await_args.args[1] == {
        "method": "set_device_info",
        "params": {"device_on": True, "color_temp": 6500},
    }
