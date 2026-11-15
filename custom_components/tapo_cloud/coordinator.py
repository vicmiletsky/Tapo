"""Data update coordinator polling device state through the TP-Link cloud."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    TPLinkAuthError,
    TPLinkCloudBridge,
    TPLinkCloudError,
    TPLinkDeviceOfflineError,
    TPLinkMFARequiredError,
    TPLinkUnsupportedRequestError,
)
from .discovery import LocalInfo, async_probe_local, normalize_mac
from .const import (
    CONF_KASA_HOST,
    CONF_KASA_REFRESH_TOKEN,
    CONF_LOCAL_PROBE,
    CONF_TAPO_HOST,
    CONF_TAPO_REFRESH_TOKEN,
    DEFAULT_LOCAL_PROBE,
    DOMAIN,
    EMETER_MODEL_PREFIXES,
    EVENT_DEVICE_OFFLINE,
    EVENT_DEVICE_ONLINE,
    LIGHT_DEVICE_TYPES,
    STATUS_ONLINE,
    SWITCH_DEVICE_TYPES,
)
from .smart import (
    PROTOCOL_IOT,
    PROTOCOL_SMART,
    SMART_EMETER_DATA_REQUEST,
    SMART_ENERGY_REQUEST,
    SMART_INFO_REQUEST,
    SmartDeviceError,
    UnsupportedCommandError,
    device_alias,
    iot_request_to_smart,
    is_smart_device_type,
    smart_energy_to_emeter,
    smart_info_to_sysinfo,
    smart_result,
)

_LOGGER = logging.getLogger(__name__)

# Limit concurrent cloud requests per refresh cycle.
_MAX_CONCURRENCY = 6

# A controllable device the device list reports as not online is still
# asked for its state (the list's status is not always right, e.g. for
# Tapo devices only listed by the Kasa cloud); after a failed attempt,
# wait this long before asking again.
OFFLINE_PROBE_BACKOFF = 300

SYSINFO_REQUEST = {"system": {"get_sysinfo": None}}
EMETER_REQUEST = {"emeter": {"get_realtime": {}}}


@dataclass
class CloudDevice:
    """State snapshot for one cloud device."""

    info: dict[str, Any]
    sysinfo: dict[str, Any] | None = None
    emeter: dict[str, Any] | None = None
    emeter_supported: bool | None = None
    local: LocalInfo | None = None
    # "iot" or "smart", once a state request has succeeded.
    protocol: str | None = None
    # True when the device answered a request this refresh.
    reachable: bool = False

    @property
    def device_id(self) -> str:
        return self.info["deviceId"]

    @property
    def alias(self) -> str:
        # Device-list alias can lag behind; sysinfo alias is authoritative.
        if self.sysinfo and self.sysinfo.get("alias"):
            return self.sysinfo["alias"]
        return (
            device_alias(self.info.get("alias"), self.device_type)
            or self.info.get("deviceName")
            or self.device_id
        )

    @property
    def model(self) -> str:
        return self.info.get("deviceModel") or "Unknown"

    @property
    def device_type(self) -> str:
        return self.info.get("deviceType") or ""

    @property
    def online(self) -> bool:
        return self.info.get("status") == STATUS_ONLINE or self.reachable

    @property
    def children(self) -> list[dict[str, Any]]:
        if self.sysinfo:
            return self.sysinfo.get("children") or []
        return []


def _normalize_emeter(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize emeter get_realtime fields across firmware generations.

    Older firmware reports base units (power, voltage, current, total);
    newer firmware reports milli-units (power_mw, voltage_mv, ...).
    """
    normalized: dict[str, Any] = {}
    conversions = (
        ("power", "power_mw", 1000),
        ("voltage", "voltage_mv", 1000),
        ("current", "current_ma", 1000),
        ("total", "total_wh", 1000),
    )
    for base_key, milli_key, factor in conversions:
        if raw.get(milli_key) is not None:
            normalized[base_key] = raw[milli_key] / factor
        elif raw.get(base_key) is not None:
            normalized[base_key] = raw[base_key]
    return normalized


class TapoCloudCoordinator(DataUpdateCoordinator[dict[str, CloudDevice]]):
    """Polls the TP-Link cloud for device presence and state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        bridge: TPLinkCloudBridge,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.entry = entry
        self.bridge = bridge
        self._emeter_support: dict[str, bool] = {}
        self._protocols: dict[str, str] = {}
        self._probe_after: dict[str, float] = {}
        self._emeter_data_support: dict[str, bool] = {}

    async def _async_update_data(self) -> dict[str, CloudDevice]:
        probe_enabled = self.entry.options.get(CONF_LOCAL_PROBE, DEFAULT_LOCAL_PROBE)
        probe_task = (
            asyncio.create_task(async_probe_local()) if probe_enabled else None
        )
        try:
            raw_devices = await self.bridge.async_get_devices()
        except (TPLinkAuthError, TPLinkMFARequiredError) as err:
            if probe_task:
                probe_task.cancel()
            raise ConfigEntryAuthFailed(str(err)) from err
        except TPLinkCloudError as err:
            if probe_task:
                probe_task.cancel()
            raise UpdateFailed(f"TP-Link cloud unreachable: {err}") from err

        devices = {
            raw["deviceId"]: CloudDevice(info=raw)
            for raw in raw_devices
            if raw.get("deviceId")
        }

        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

        async def fetch_state(device: CloudDevice) -> None:
            if not device.online and not (
                _is_controllable(device) and self._probe_due(device.device_id)
            ):
                _LOGGER.debug(
                    "Skipping %s (%s): cloud reports status %r, not online",
                    device.alias,
                    device.model,
                    device.info.get("status"),
                )
                return
            async with semaphore:
                listed_online = device.online
                try:
                    await self._async_fetch_sysinfo(device)
                except TPLinkCloudError as err:
                    _LOGGER.debug(
                        "State request failed for %s (%s, status %r): %s",
                        device.alias,
                        device.model,
                        device.info.get("status"),
                        err,
                    )
                    if not listed_online:
                        self._probe_after[device.device_id] = (
                            time.monotonic() + OFFLINE_PROBE_BACKOFF
                        )
                    return
                if device.sysinfo is not None:
                    device.reachable = True
                    self._probe_after.pop(device.device_id, None)
                    if not listed_online:
                        _LOGGER.debug(
                            "%s (%s) answered although the cloud lists status %r",
                            device.alias,
                            device.model,
                            device.info.get("status"),
                        )
                await self._async_fetch_emeter(device)

        await asyncio.gather(*(fetch_state(device) for device in devices.values()))

        if probe_task:
            try:
                local_results = await probe_task
            except Exception:  # noqa: BLE001 — probe is strictly best-effort
                local_results = {}
            for device in devices.values():
                mac = normalize_mac(device.info.get("deviceMac"))
                if mac and mac in local_results:
                    device.local = local_results[mac]

        self._fire_presence_events(devices)
        self._persist_refresh_tokens()
        return devices

    def _probe_due(self, device_id: str) -> bool:
        return time.monotonic() >= self._probe_after.get(device_id, 0.0)

    async def _async_fetch_sysinfo(self, device: CloudDevice) -> None:
        """Fetch device state, in the device's own protocol.

        SMART-family devices get get_device_info first and fall back to the
        IOT get_sysinfo; whichever answers is remembered for the device.
        """
        device_id = device.device_id
        if is_smart_device_type(device.device_type):
            protocols = [PROTOCOL_SMART, PROTOCOL_IOT]
            known = self._protocols.get(device_id)
            if known in protocols:
                protocols.remove(known)
                protocols.insert(0, known)
        else:
            protocols = [PROTOCOL_IOT]

        first_error: Exception | None = None
        for protocol in protocols:
            try:
                if protocol == PROTOCOL_SMART:
                    response = await self.bridge.async_device_request(
                        device.info, SMART_INFO_REQUEST
                    )
                    sysinfo = smart_info_to_sysinfo(smart_result(response))
                else:
                    response = await self.bridge.async_device_request(
                        device.info, SYSINFO_REQUEST
                    )
                    sysinfo = ((response or {}).get("system") or {}).get(
                        "get_sysinfo"
                    )
                    if len(protocols) > 1 and not isinstance(sysinfo, dict):
                        raise SmartDeviceError("no get_sysinfo in response")
            except (
                TPLinkAuthError,
                TPLinkMFARequiredError,
                TPLinkDeviceOfflineError,
                TPLinkUnsupportedRequestError,
            ):
                # Cloud-level failures: the other protocol would hit the same.
                raise
            except (TPLinkCloudError, SmartDeviceError) as err:
                first_error = first_error or err
                continue
            device.sysinfo = sysinfo
            device.protocol = protocol
            self._protocols[device_id] = protocol
            return

        if isinstance(first_error, TPLinkCloudError):
            raise first_error
        raise TPLinkCloudError(f"No usable state response: {first_error}")

    async def _async_fetch_emeter(self, device: CloudDevice) -> None:
        """Fetch energy readings for devices that support them.

        Support is probed once per device: known energy-monitoring models,
        a sysinfo feature string containing ENE, or any SMART-protocol plug
        (Tapo has too many metering models to list; one that is not
        answers the first request with an error).
        """
        device_id = device.device_id
        supported = self._emeter_support.get(device_id)
        if supported is None:
            feature = (device.sysinfo or {}).get("feature") or ""
            supported = (
                "ENE" in feature
                or device.model.upper().startswith(EMETER_MODEL_PREFIXES)
                or (
                    device.protocol == PROTOCOL_SMART
                    and device.device_type in SWITCH_DEVICE_TYPES
                )
            )
        if not supported:
            device.emeter_supported = False
            return
        transient_failure = False
        if device.protocol == PROTOCOL_SMART:
            try:
                realtime = await self._async_fetch_smart_energy(device) or None
            except SmartDeviceError:
                realtime = None
            except TPLinkCloudError:
                # A cloud hiccup says nothing about the plug's metering.
                realtime = None
                transient_failure = True
        else:
            try:
                response = await self.bridge.async_device_request(
                    device.info, EMETER_REQUEST
                )
            except TPLinkCloudError:
                response = None
            realtime = ((response or {}).get("emeter") or {}).get("get_realtime")
            if realtime and realtime.get("err_code", 0) == 0:
                realtime = _normalize_emeter(realtime)
            else:
                realtime = None
        if realtime is not None:
            device.emeter = realtime
            device.emeter_supported = True
            self._emeter_support[device_id] = True
        elif self._emeter_support.get(device_id):
            # Previously worked: keep the entity, report unknown this cycle.
            device.emeter_supported = True
        else:
            device.emeter_supported = False
            if not transient_failure:
                self._emeter_support[device_id] = False

    async def _async_fetch_smart_energy(self, device: CloudDevice) -> dict[str, Any]:
        """get_energy_usage, plus get_emeter_data where the plug has it.

        Raises SmartDeviceError when the plug has no energy monitoring.
        """
        device_id = device.device_id
        usage = smart_result(
            await self.bridge.async_device_request(device.info, SMART_ENERGY_REQUEST)
        )
        emeter_data: dict[str, Any] | None = None
        if self._emeter_data_support.get(device_id) is not False:
            try:
                emeter_data = smart_result(
                    await self.bridge.async_device_request(
                        device.info, SMART_EMETER_DATA_REQUEST
                    )
                )
            except SmartDeviceError:
                self._emeter_data_support[device_id] = False
            except TPLinkCloudError:
                pass
            else:
                self._emeter_data_support[device_id] = True
        return smart_energy_to_emeter(usage, emeter_data)

    def _fire_presence_events(self, devices: dict[str, CloudDevice]) -> None:
        """Fire events when devices join or drop off the TP-Link cloud."""
        if not self.data:
            return
        for device_id, device in devices.items():
            previous = self.data.get(device_id)
            if previous is None or previous.online == device.online:
                continue
            event = EVENT_DEVICE_ONLINE if device.online else EVENT_DEVICE_OFFLINE
            self.hass.bus.async_fire(
                event,
                {
                    "device_id": device_id,
                    "alias": device.alias,
                    "model": device.model,
                    "cloud_type": device.info.get("cloud_type"),
                },
            )
            _LOGGER.info(
                "%s (%s) went %s on the TP-Link cloud",
                device.alias,
                device.model,
                "online" if device.online else "offline",
            )

    def _persist_refresh_tokens(self) -> None:
        """Keep rotated refresh tokens and regional hosts in the config entry."""
        updates: dict[str, Any] = {}
        stored = self.entry.data
        current = {
            CONF_KASA_REFRESH_TOKEN: self.bridge.kasa.refresh_token,
            CONF_TAPO_REFRESH_TOKEN: self.bridge.tapo.refresh_token,
            CONF_KASA_HOST: self.bridge.kasa.host,
            CONF_TAPO_HOST: self.bridge.tapo.host,
        }
        for key, value in current.items():
            if value and value != stored.get(key):
                updates[key] = value
        if updates:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**stored, **updates}
            )

    async def async_send_command(
        self, device_id: str, request_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Send a command to a device and schedule a state refresh."""
        device = (self.data or {}).get(device_id)
        if device is None:
            raise UpdateFailed(f"Unknown device {device_id}")
        smart = device.protocol == PROTOCOL_SMART
        if smart:
            try:
                request_data = iot_request_to_smart(request_data)
            except UnsupportedCommandError as err:
                raise HomeAssistantError(
                    f"{device.alias} does not support this command: {err}"
                ) from err
        try:
            response = await self.bridge.async_device_request(
                device.info, request_data
            )
        except (TPLinkAuthError, TPLinkMFARequiredError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TPLinkCloudError as err:
            raise HomeAssistantError(
                f"Command to {device.alias} failed: {err}"
            ) from err
        if smart:
            try:
                smart_result(response)
            except SmartDeviceError as err:
                raise HomeAssistantError(
                    f"{device.alias} rejected the command: {err}"
                ) from err
        return response


def _is_controllable(device: CloudDevice) -> bool:
    return device.device_type in SWITCH_DEVICE_TYPES or (
        device.device_type in LIGHT_DEVICE_TYPES
    )
