"""Energy sensors appear as their readings arrive (Tapo #1)."""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.tapo_cloud.const import DOMAIN
from custom_components.tapo_cloud.coordinator import CloudDevice, TapoCloudCoordinator
from custom_components.tapo_cloud.sensor import async_setup_entry


async def test_sensors_are_added_when_their_readings_first_arrive() -> None:
    coordinator = TapoCloudCoordinator.__new__(TapoCloudCoordinator)
    coordinator.data = {}
    listeners = []
    coordinator.async_add_listener = lambda listener: listeners.append(listener)

    entry = MagicMock(entry_id="entry")
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry": coordinator}}
    added: list[str] = []
    await async_setup_entry(
        hass, entry, lambda entities: added.extend(e.unique_id for e in entities)
    )
    assert added == []  # plug offline at startup: nothing yet

    info = {"deviceId": "p410", "deviceType": "SMART.TAPOPLUG", "status": 1}
    coordinator.data = {
        "p410": CloudDevice(info=info, emeter={"power": 1.0, "today": 0.1}, emeter_supported=True)
    }
    listeners[0]()
    assert added == ["p410_current_power", "p410_today_energy"]

    coordinator.data["p410"].emeter = {"power": 1.0, "today": 0.1, "voltage": 230.0}
    listeners[0]()
    assert added[-1] == "p410_voltage" and len(added) == 3

    listeners[0]()
    assert len(added) == 3  # nothing added twice
