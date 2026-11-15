# VM Tapo Cloud for Home Assistant

A [HACS](https://hacs.xyz) custom integration that controls **TP-Link Tapo and Kasa devices through TP-Link's cloud** — the same channel the official apps use.

## What this fork changes and why

This fork addresses the upstream bug where Tapo devices stay unavailable ([HallyAus/Tapo#1](https://github.com/HallyAus/Tapo/issues/1)).
The Kasa cloud lists Tapo devices as offline, and the integration trusted that copy.
Tapo's cloud also needs different server addresses and request formats than Kasa's, which the integration did not use.
Tapo devices also speak their own command set, not the Kasa commands the integration was sending.
This fork reaches Tapo devices through the Tapo cloud, using Tapo's own formats, so they should work again.

## Why this exists

TP-Link has been rolling out firmware updates that switch devices to a new local protocol called **TPAP** (SPAKE2+ handshake over TLS on port 4433, with device attestation certificates). The [python-kasa library](https://github.com/python-kasa/python-kasa/issues/1590) that Home Assistant's built-in TP-Link integration relies on **does not support TPAP**, and the [work to add it](https://github.com/python-kasa/python-kasa/pull/1592) is blocked on TP-Link's certificate infrastructure. When a device updates, it silently drops out of Home Assistant:

> `Unsupported device ... with encrypt_scheme EncryptionScheme(is_support_https=True, encrypt_type='TPAP', http_port=4433, lv=2)`

This has hit P100/P105/P110 plugs, P300 strips, wall switches, KH100 hubs, and more — and it will eventually reach every Tapo/Kasa device as firmware rolls out.

**The device still works from the Tapo app** because the app talks to the device through TP-Link's cloud. This integration uses that same cloud channel, so:

- ✅ Works on devices locked to TPAP firmware — no downgrade needed
- ✅ Works for both **Tapo** and **Kasa** devices under one account
- ✅ No local network access to the device required at all
- ⚠️ It is **cloud polling** — it needs internet, and state updates are polled (default every 60 s), not instant

## What you get

| Device type | Entities |
|---|---|
| Smart plugs & wall switches (P100/P105/P110/P115, HS1xx, HS2xx, KP1xx, KS2xx, …) | Switch |
| Power strips (HS300, KP303, KP400, EP40, …) | One switch per outlet |
| Bulbs (L510/L530, KL series, …) | Light (brightness, color, and color temperature where supported) |
| Energy-monitoring devices (P110/P115, P410M, HS110, KP115/KP125, …) | Power, voltage, current, total energy sensors; Tapo plugs also today's and this month's energy |
| **Every device on the account** (incl. hubs, cameras, vacuums) | Cloud connectivity binary sensor |

The integration also fires `tapo_cloud_device_offline` / `tapo_cloud_device_online` events on the Home Assistant event bus when a device drops off or rejoins the TP-Link cloud, so you can build automations that alert you the moment a firmware update knocks something out.

Each cloud-connectivity sensor exposes a `matter_capable` attribute flagging devices whose hardware is Matter-certified — those can be commissioned into Home Assistant's fully local Matter integration instead. See the [Matter migration guide](docs/MATTER.md) for the long-term exit strategy from TP-Link's protocol churn.

### Local protocol detection

On every poll the integration also broadcasts TP-Link's discovery packets on your LAN (toggleable in options) and reports each device's **local** protocol as sensor attributes:

- `local_protocol`: `iot` (legacy, fully local-capable), `klap`/`aes` (local-capable via the core `tplink` integration), or `tpap` (locked out of local control)
- `tpap_locked`: `true` when the device can **only** be reached through the cloud — i.e. exactly the devices this integration exists for
- `locally_controllable`: `true` when you could (also) use Home Assistant's built-in TP-Link integration for instant local control

This gives you a live inventory of which devices the TPAP rollout has claimed so far.

## Installation

### Via HACS (recommended)

1. In HACS, open **⋮ → Custom repositories**.
2. Add `https://github.com/vicmiletsky/Tapo` with category **Integration**.
3. Install **VM Tapo Cloud** and restart Home Assistant.

### Manual

Copy `custom_components/tapo_cloud` into your Home Assistant `config/custom_components/` directory and restart.

## Setup

1. Go to **Settings → Devices & Services → Add Integration** and search for **VM Tapo Cloud**.
2. Sign in with your TP-Link account (the one you use in the Tapo/Kasa app).
3. If your account has two-step verification, you'll be prompted for the code TP-Link emails you — once per cloud (Kasa and Tapo are separate clouds behind the same account).

The Tapo cloud may also ask for an emailed code when a new installation signs in. Installations set up before this was handled show **Re-authentication required** under Settings → Devices & Services; re-enter your password and the code TP-Link emails you.

All devices registered to the account are discovered automatically. The polling interval is adjustable under the integration's **Configure** option (15–600 seconds).

## How it works

TP-Link runs two parallel clouds for the same account:

| | Kasa cloud | Tapo cloud |
|---|---|---|
| Endpoint | `n-wap.tplinkcloud.com` → account's `appServerUrl` | `n-wap.i.tplinkcloud.com` → account's `appServerUrlV2` |
| Device list | V1 `{"method":"getDeviceList"}` on `/` | V2 `POST /api/v2/common/getDeviceListByPage` (paged, by `deviceTypeList`) |
| Passthrough | V1 `{"method":"passthrough"}` on `/` | V2 `POST /api/v2/common/passthrough` |
| Device protocol | IOT (`get_sysinfo`, `set_relay_state`, …) | SMART (`get_device_info`, `set_device_info`) |

Every request is HMAC-SHA1-signed with app-level keys (the same signing the official Android apps perform), and commands are relayed to the device over the device's own persistent cloud connection. Since the device authenticates *outward* to the cloud, the local TPAP lockout is irrelevant.

The Kasa cloud also lists Tapo devices, but always as offline (status 0), because they are connected to the Tapo cloud instead. The integration therefore keeps the entry from the cloud a device is actually connected to, and relays Tapo devices through the Tapo cloud. If the Tapo device list cannot be fetched, Tapo devices the Kasa cloud lists are still relayed through the Tapo cloud. A controllable device whose cloud reports it offline is still asked for its state, at most every 5 minutes after a failed attempt, so a wrong cloud status no longer hides a working device.

Neither cloud API is documented, so the Tapo device-list and passthrough requests are tried in a few known shapes, and the one the cloud accepts is remembered. The integration's diagnostics download shows which shapes were accepted, under `cloud`.

The cloud endpoints present certificates from TP-Link's private CA, which is bundled with the integration.

Protocol details were derived from the excellent GPL-3.0 [piekstra/tplink-cloud-api](https://github.com/piekstra/tplink-cloud-api) project.

## Recommendations alongside this integration

- **Keep local control where you still can:** for devices not yet on TPAP firmware, the built-in TP-Link integration (local, instant) remains better. Use this integration for the devices that have been locked out, and as the safety net for the rest.
- **Enable "Third-Party Compatibility"** in the Tapo app (Device → Settings) for every device that offers it — on many firmwares this re-enables the KLAP local protocol that Home Assistant supports natively.
- **Disable auto-update** in the Tapo/Kasa app for devices that still work locally.
- Watch [python-kasa#1590](https://github.com/python-kasa/python-kasa/issues/1590) — if native TPAP support lands upstream, local control returns via the core integration.

## Limitations

- Cloud polling: state changes made outside HA can take up to one polling interval to appear.
- Requires an internet connection and TP-Link's cloud to be up.
- Kasa bulbs get color/color temperature when they expose the Kasa lighting service through the cloud. Tapo bulbs get brightness, color and color temperature from `get_device_info`, but no transitions.
- Tapo plugs report voltage, current and total energy only if their firmware answers `get_emeter_data`; otherwise current power and today's/this month's energy. Voltage and current sensors are disabled by default.
- Tapo power strips (P300, P304) are exposed as one switch; per-outlet control is not supported yet.
- Hubs, sensors, cameras, and vacuums are presence-only (connectivity sensor, no control).
- TP-Link could change the cloud API at any time; this is an unofficial client.

## Credits & license

- Cloud protocol: [piekstra/tplink-cloud-api](https://github.com/piekstra/tplink-cloud-api) (GPL-3.0)
- TPAP tracking: [python-kasa/python-kasa#1590](https://github.com/python-kasa/python-kasa/issues/1590)

This project is licensed under the [GPL-3.0](LICENSE).

*This is an unofficial project, not affiliated with TP-Link. Tapo and Kasa are trademarks of TP-Link Corporation.*

*The Tapo icon and logo in `custom_components/tapo_cloud/brand/` are TP-Link trademarks, taken from Home Assistant's [brands repository](https://github.com/home-assistant/brands/tree/master/core_integrations/tplink_tapo). They are not covered by this project's GPL-3.0 licence.*
