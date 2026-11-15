"""Async client for the TP-Link cloud V2 API (Kasa and Tapo clouds).

TP-Link runs two parallel cloud services for the same account:

  Kasa: https://n-wap.tplinkcloud.com     (appType Kasa_Android_Mix)
  Tapo: https://n-wap.i.tplinkcloud.com   (appType TP-Link_Tapo_Android)

Both use the same V2 protocol: every request is signed with HMAC-SHA1
using an app-level AccessKey/SecretKey pair, and device commands are
relayed to the device over its own cloud connection ("passthrough").

Because commands travel device->cloud, control keeps working even when
the device's local API is unreachable — e.g. after a firmware update
switched the device to the (still unsupported) TPAP local protocol.

The Tapo cloud differs from the Kasa cloud in a few ways that matter
(HallyAus/Tapo#1):

  * getAccountStatusAndUrl returns the V2 app server as appServerUrlV2;
    the legacy appServerUrl does not serve every V2 path.
  * The Tapo app lists devices with POST /api/v2/common/getDeviceListByPage
    (deviceTypeList + index/limit paging). The Kasa-style
    {"method": "getDeviceList"} is rejected with -20104 by some accounts.
  * Passthrough goes to /api/v2/common/passthrough with requestData as a
    JSON object.

Because none of this is officially documented, every Tapo request shape
is tried in order of preference, and the one the cloud accepts is
remembered.

Protocol details derived from the GPL-3.0 project
https://github.com/piekstra/tplink-cloud-api
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import ssl
import uuid
from pathlib import Path
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

KASA_HOST = "https://n-wap.tplinkcloud.com"
TAPO_HOST = "https://n-wap.i.tplinkcloud.com"

KASA_APP_TYPE = "Kasa_Android_Mix"
TAPO_APP_TYPE = "TP-Link_Tapo_Android"
APP_VER = "3.4.451"

# App-level signing keys from the official Android apps. These identify
# the client application, not the user account.
KASA_ACCESS_KEY = "e37525375f8845999bcc56d5e6faa76d"
KASA_SECRET_KEY = "314bc6700b3140ca80bc655e527cb062"
TAPO_ACCESS_KEY = "4d11b6b9d5ea4d19a829adbb9714b057"
TAPO_SECRET_KEY = "6ed7d97f3e73467f8a5bab90b577ba4c"

# Both official apps sign with this fixed timestamp.
SIGNING_TIMESTAMP = "9999999999"

PATH_ACCOUNT_STATUS = "/api/v2/account/getAccountStatusAndUrl"
PATH_LOGIN = "/api/v2/account/login"
PATH_REFRESH = "/api/v2/account/refreshToken"
PATH_MFA_LOGIN = "/api/v2/account/checkMFACodeAndLogin"
PATH_SEND_MFA_EMAIL = "/api/v2/account/getEmailVC4TerminalMFA"
PATH_TAPO_PASSTHROUGH = "/api/v2/common/passthrough"
PATH_TAPO_DEVICE_LIST = "/api/v2/common/getDeviceListByPage"

ERR_MFA_REQUIRED = -20677
ERR_TOKEN_EXPIRED = -20651
ERR_REFRESH_TOKEN_EXPIRED = -20655
ERR_WRONG_CREDENTIALS = -20601
ERR_ACCOUNT_LOCKED = -20675
# Outside of login, the Tapo app treats -20675 as an invalid token.
ERR_TOKEN_INVALID = -20675
ERR_METHOD_NOT_FOUND = -20103
ERR_PARAMETER_NOT_FOUND = -20104
ERR_DEVICE_OFFLINE = -20571
# Device-level SMART protocol error: the relayed requestData was not JSON
# the device could parse.
ERR_DEVICE_MALFORMED_REQUEST = -1003

_TOKEN_ERRORS = {ERR_TOKEN_EXPIRED, ERR_TOKEN_INVALID}
# The cloud answers these when it does not know a method or misses a
# parameter, i.e. when we used a request shape this server does not serve.
_UNSUPPORTED_REQUEST_ERRORS = {ERR_METHOD_NOT_FOUND, ERR_PARAMETER_NOT_FOUND}
_UNSUPPORTED_HTTP_STATUSES = {404, 405}

# Cloud device-list "status" value for a device connected to the cloud.
_STATUS_ONLINE = 1

# deviceType values requested from the Tapo cloud's paged device list.
TAPO_LIST_DEVICE_TYPES = (
    "SMART.TAPOPLUG",
    "SMART.TAPOBULB",
    "SMART.TAPOSWITCH",
    "SMART.TAPOHUB",
    "SMART.TAPOROBOVAC",
    "SMART.TAPOCHIME",
    "SMART.TAPODOORBELL",
    "SMART.IPCAMERA",
    "SMART.KASAPLUG",
    "SMART.KASASWITCH",
    "SMART.KASAHUB",
    "IOT.SMARTPLUGSWITCH",
    "IOT.SMARTBULB",
    "IOT.IPCAMERA",
    "IOT.RANGEEXTENDER.SMARTPLUG",
)
# Fallback list of the controllable Tapo types, in case the cloud rejects
# a type it does not know.
TAPO_LIST_BASIC_DEVICE_TYPES = (
    "SMART.TAPOPLUG",
    "SMART.TAPOBULB",
    "SMART.TAPOSWITCH",
)
DEVICE_LIST_PAGE_SIZE = 20
_MAX_DEVICE_LIST_PAGES = 50

# Tapo device-list request shapes, in order of preference.
DEVICE_LIST_PAGED = "paged"
DEVICE_LIST_PAGED_BASIC = "paged_basic"
DEVICE_LIST_LEGACY = "legacy"
_TAPO_DEVICE_LIST_MODES = (
    DEVICE_LIST_PAGED,
    DEVICE_LIST_PAGED_BASIC,
    DEVICE_LIST_LEGACY,
)

# Tapo passthrough request shapes, in order of preference.
PASSTHROUGH_V2_OBJECT = "v2_object"
PASSTHROUGH_V2_STRING = "v2_string"
PASSTHROUGH_V1_OBJECT = "v1_object"
_TAPO_PASSTHROUGH_MODES = (
    PASSTHROUGH_V2_OBJECT,
    PASSTHROUGH_V2_STRING,
    PASSTHROUGH_V1_OBJECT,
)

# Device types served by the Tapo cloud rather than the Kasa cloud. The
# Kasa cloud lists these too, but reports them offline (status 0) because
# they never connect to its relay.
TAPO_FAMILY_TYPE_PREFIXES = ("SMART.TAPO", "SMART.IPCAMERA")

_CA_CHAIN = Path(__file__).parent / "certs" / "tplink-ca-chain.pem"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class TPLinkCloudError(Exception):
    """Base error for cloud API failures."""

    def __init__(self, message: str, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class TPLinkAuthError(TPLinkCloudError):
    """Wrong credentials or locked account."""


class TPLinkMFARequiredError(TPLinkCloudError):
    """Login requires a multi-factor verification code."""

    def __init__(
        self,
        message: str,
        mfa_type: str | None = None,
        mfa_process_id: str | None = None,
    ) -> None:
        super().__init__(message, error_code=ERR_MFA_REQUIRED)
        self.mfa_type = mfa_type
        self.mfa_process_id = mfa_process_id


class TPLinkTokenExpiredError(TPLinkCloudError):
    """Auth or refresh token has expired."""


class TPLinkUnsupportedRequestError(TPLinkCloudError):
    """The cloud does not serve this request shape (unknown method/parameter)."""


class TPLinkDeviceOfflineError(TPLinkCloudError):
    """The cloud has no connection to the device."""


def is_tapo_family(device: dict[str, Any]) -> bool:
    """Whether a device-list entry belongs on the Tapo cloud."""
    device_type = (device.get("deviceType") or "").upper()
    return device_type.startswith(TAPO_FAMILY_TYPE_PREFIXES)


def _raise_for_error(envelope: dict[str, Any], action: str) -> None:
    """Raise the matching error for a non-zero cloud error_code."""
    error_code = envelope.get("error_code")
    if error_code == 0:
        return
    message = f"{action} failed: {envelope.get('msg') or 'error'} ({error_code})"
    if error_code in _TOKEN_ERRORS:
        raise TPLinkTokenExpiredError(message, error_code=error_code)
    if error_code in _UNSUPPORTED_REQUEST_ERRORS:
        raise TPLinkUnsupportedRequestError(message, error_code=error_code)
    if error_code == ERR_DEVICE_OFFLINE:
        raise TPLinkDeviceOfflineError(message, error_code=error_code)
    raise TPLinkCloudError(message, error_code=error_code)


def _ordered(modes: tuple[str, ...], preferred: str | None) -> list[str]:
    """Modes with the one known to work first."""
    ordered = list(modes)
    if preferred in ordered:
        ordered.remove(preferred)
        ordered.insert(0, preferred)
    return ordered


def build_ssl_context() -> ssl.SSLContext:
    """Build an SSL context trusting both public CAs and TP-Link's private CA.

    The n-*.tplinkcloud.com V2 endpoints present certificates issued by
    TP-Link's own CA, which is not in the system trust store. Blocking —
    call from an executor.
    """
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=str(_CA_CHAIN))
    return context


def _signing_headers(
    body_json: str, url_path: str, access_key: str, secret_key: str
) -> dict[str, str]:
    """Compute Content-MD5 and X-Authorization headers for a V2 request."""
    content_md5 = base64.b64encode(hashlib.md5(body_json.encode()).digest()).decode()
    nonce = str(uuid.uuid4())
    sig_string = f"{content_md5}\n{SIGNING_TIMESTAMP}\n{nonce}\n{url_path}"
    signature = hmac.new(
        secret_key.encode(), sig_string.encode(), hashlib.sha1
    ).hexdigest()
    return {
        "Content-MD5": content_md5,
        "X-Authorization": (
            f"Timestamp={SIGNING_TIMESTAMP}, Nonce={nonce}, "
            f"AccessKey={access_key}, Signature={signature}"
        ),
    }


class TPLinkCloud:
    """Client for one of the two TP-Link clouds ("kasa" or "tapo")."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        ssl_context: ssl.SSLContext,
        cloud_type: str,
        term_id: str,
    ) -> None:
        self._session = session
        self._ssl = ssl_context
        self.cloud_type = cloud_type
        self._term_id = term_id

        if cloud_type == "tapo":
            self._access_key = TAPO_ACCESS_KEY
            self._secret_key = TAPO_SECRET_KEY
            self._app_type = TAPO_APP_TYPE
            self._default_host = TAPO_HOST
        else:
            self._access_key = KASA_ACCESS_KEY
            self._secret_key = KASA_SECRET_KEY
            self._app_type = KASA_APP_TYPE
            self._default_host = KASA_HOST
        self.host = self._default_host

        self.token: str | None = None
        self.refresh_token: str | None = None
        # Set when a login answered with an MFA process instead of a token
        # (the Tapo cloud does this for terminals it has not seen before).
        self.mfa_process_id: str | None = None
        # Request shapes this cloud accepted (Tapo only).
        self.device_list_mode: str | None = None
        self.passthrough_mode: str | None = None

        self._query_params = {
            "appName": self._app_type,
            "appVer": APP_VER,
            "netType": "wifi",
            "termID": term_id,
            "ospf": "Android 14",
            "brand": "TPLINK",
            "locale": "en_US",
            "model": "Pixel",
            "termName": "Pixel",
            "termMeta": "Pixel",
        }
        self._headers = {
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 14; Pixel Build/UP1A)",
            "Content-Type": "application/json;charset=UTF-8",
        }

    async def _post(
        self,
        base_url: str,
        url_path: str,
        body: dict[str, Any],
        with_token: bool = True,
    ) -> dict[str, Any]:
        """POST a signed request and return the parsed JSON envelope."""
        body_json = json.dumps(body)
        params = dict(self._query_params)
        if with_token and self.token:
            params["token"] = self.token
        headers = {
            **self._headers,
            **_signing_headers(body_json, url_path, self._access_key, self._secret_key),
        }
        url = base_url if url_path == "/" else f"{base_url}{url_path}"
        try:
            async with self._session.post(
                url,
                data=body_json,
                params=params,
                headers=headers,
                ssl=self._ssl,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status in _UNSUPPORTED_HTTP_STATUSES:
                    raise TPLinkUnsupportedRequestError(
                        f"HTTP {response.status} from {url_path}: {response.reason}"
                    )
                if response.status != 200:
                    raise TPLinkCloudError(
                        f"HTTP {response.status} from {url_path}: {response.reason}"
                    )
                return await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise TPLinkCloudError(f"Connection error calling {url_path}: {err}") from err

    async def _get_regional_url(self, username: str) -> str:
        # Asked of the global endpoint, like the apps do: self.host may
        # already be a (possibly stale) regional server.
        body = {"appType": self._app_type, "cloudUserName": username}
        envelope = await self._post(
            self._default_host, PATH_ACCOUNT_STATUS, body, with_token=False
        )
        if envelope.get("error_code") != 0:
            return self.host
        result = envelope.get("result") or {}
        if self.cloud_type == "tapo" and result.get("appServerUrlV2"):
            # The current Tapo app talks to the V2 server; the legacy
            # appServerUrl does not serve every /api/v2 path.
            return result["appServerUrlV2"]
        return result.get("appServerUrl") or self.host

    async def async_lookup_host(self, username: str) -> str:
        """Return the regional app server the cloud assigns to this account."""
        return await self._get_regional_url(username)

    async def async_login(self, username: str, password: str) -> None:
        """Log in with account credentials.

        Raises TPLinkMFARequiredError when the account needs a
        verification code; complete the login with async_verify_mfa().
        """
        self.host = await self._get_regional_url(username)
        body = {
            "appType": self._app_type,
            "appVersion": APP_VER,
            "cloudPassword": password,
            "cloudUserName": username,
            "platform": "Android",
            "refreshTokenNeeded": True,
            "supportBindAccount": False,
            "terminalUUID": self._term_id,
            "terminalName": "Home Assistant",
            "terminalMeta": "Home Assistant",
        }
        envelope = await self._post(self.host, PATH_LOGIN, body, with_token=False)
        self._handle_login_result(envelope)

    async def async_send_mfa_code(self, username: str, password: str) -> None:
        """Ask TP-Link to email the code for a pending MFA process.

        Only needed when login answered with an mfaProcessId; the older
        -20677 challenge sends its code by itself.
        """
        if not self.mfa_process_id:
            return
        body = {
            "appType": self._app_type,
            "cloudUserName": username,
            "cloudPassword": password,
            "terminalUUID": self._term_id,
            "locale": "en_US",
        }
        envelope = await self._post(
            self.host, PATH_SEND_MFA_EMAIL, body, with_token=False
        )
        _raise_for_error(envelope, "Sending the verification code")

    async def async_verify_mfa(self, username: str, password: str, code: str) -> None:
        """Complete a login challenged with MFA."""
        if self.mfa_process_id:
            # The Tapo app's terminal-MFA check; binding the terminal keeps
            # later logins from this installation from asking again.
            body: dict[str, Any] = {
                "appType": self._app_type,
                "cloudUserName": username,
                "code": code,
                "mfaProcessId": self.mfa_process_id,
                "mfaType": 1,
                "terminalBindEnabled": True,
                "supportBindAccount": False,
            }
        else:
            body = {
                "appType": self._app_type,
                "cloudPassword": password,
                "cloudUserName": username,
                "code": code,
                "terminalUUID": self._term_id,
            }
        envelope = await self._post(self.host, PATH_MFA_LOGIN, body, with_token=False)
        error_code = envelope.get("error_code")
        if error_code != 0:
            raise TPLinkAuthError(
                envelope.get("msg") or f"MFA verification failed ({error_code})",
                error_code=error_code,
            )
        result = envelope.get("result") or {}
        if not result.get("token"):
            raise TPLinkCloudError("MFA verification returned no token")
        self.mfa_process_id = None
        self._store_tokens(result)

    def _handle_login_result(self, envelope: dict[str, Any]) -> None:
        error_code = envelope.get("error_code")
        result = envelope.get("result") or {}
        _LOGGER.debug(
            "%s cloud login answered error_code %s with fields %s",
            self.cloud_type,
            error_code,
            sorted(result),
        )
        if error_code == 0:
            if result.get("token"):
                self.mfa_process_id = None
                self._store_tokens(result)
                return
            if result.get("mfaProcessId"):
                # Not a failure code, but no token either: every request
                # sent without one is answered with -20104 "Parameter
                # doesn't exist" (Tapo #1).
                self.mfa_process_id = result["mfaProcessId"]
                raise TPLinkMFARequiredError(
                    f"{self.cloud_type} cloud needs an emailed verification code",
                    mfa_type=result.get("mfaType"),
                    mfa_process_id=self.mfa_process_id,
                )
            raise TPLinkCloudError(f"{self.cloud_type} cloud login returned no token")
        if error_code == ERR_MFA_REQUIRED:
            raise TPLinkMFARequiredError(
                "MFA verification required", mfa_type=result.get("mfaType")
            )
        if error_code in (ERR_WRONG_CREDENTIALS, ERR_ACCOUNT_LOCKED):
            raise TPLinkAuthError(
                envelope.get("msg") or "Authentication failed", error_code=error_code
            )
        raise TPLinkCloudError(
            envelope.get("msg") or f"Login failed ({error_code})", error_code=error_code
        )

    def _store_tokens(self, result: dict[str, Any]) -> None:
        self.token = result.get("token")
        self.refresh_token = result.get("refreshToken") or self.refresh_token

    async def async_refresh_login(self) -> None:
        """Get a fresh auth token using the stored refresh token."""
        if not self.refresh_token:
            raise TPLinkTokenExpiredError("No refresh token available")
        body = {
            "appType": self._app_type,
            "refreshToken": self.refresh_token,
            "terminalUUID": self._term_id,
        }
        envelope = await self._post(self.host, PATH_REFRESH, body, with_token=False)
        error_code = envelope.get("error_code")
        if error_code == 0:
            self._store_tokens(envelope.get("result") or {})
            return
        if error_code == ERR_REFRESH_TOKEN_EXPIRED:
            self.refresh_token = None
            raise TPLinkTokenExpiredError(
                "Refresh token expired", error_code=error_code
            )
        raise TPLinkCloudError(
            envelope.get("msg") or f"Token refresh failed ({error_code})",
            error_code=error_code,
        )

    async def async_get_device_list(self) -> list[dict[str, Any]]:
        """Return the raw device list registered to this cloud."""
        if self.cloud_type != "tapo":
            return await self._async_get_legacy_device_list()

        failures: list[str] = []
        for mode in _ordered(_TAPO_DEVICE_LIST_MODES, self.device_list_mode):
            try:
                if mode == DEVICE_LIST_LEGACY:
                    devices = await self._async_get_legacy_device_list()
                elif mode == DEVICE_LIST_PAGED_BASIC:
                    devices = await self._async_get_paged_device_list(
                        TAPO_LIST_BASIC_DEVICE_TYPES
                    )
                else:
                    devices = await self._async_get_paged_device_list(
                        TAPO_LIST_DEVICE_TYPES
                    )
            except TPLinkUnsupportedRequestError as err:
                failures.append(f"{mode}: {err}")
                continue
            if mode != self.device_list_mode:
                _LOGGER.debug("Tapo cloud accepted the %s device-list request", mode)
                self.device_list_mode = mode
            return devices
        raise TPLinkUnsupportedRequestError(
            "Tapo cloud rejected every device-list request ("
            + "; ".join(failures)
            + ")"
        )

    async def _async_get_legacy_device_list(self) -> list[dict[str, Any]]:
        """Kasa-style V1 getDeviceList on the root path."""
        envelope = await self._post(self.host, "/", {"method": "getDeviceList"})
        _raise_for_error(envelope, "getDeviceList")
        return (envelope.get("result") or {}).get("deviceList") or []

    async def _async_get_paged_device_list(
        self, device_types: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """The Tapo app's paged device list."""
        devices: list[dict[str, Any]] = []
        seen: set[str] = set()
        index = 0
        for _ in range(_MAX_DEVICE_LIST_PAGES):
            body = {
                "deviceTypeList": list(device_types),
                "index": index,
                "limit": DEVICE_LIST_PAGE_SIZE,
            }
            envelope = await self._post(self.host, PATH_TAPO_DEVICE_LIST, body)
            _raise_for_error(envelope, "getDeviceListByPage")
            result = envelope.get("result") or {}
            page = result.get("deviceList") or []
            new_devices = [
                device
                for device in page
                if device.get("deviceId") not in seen
            ]
            seen.update(device.get("deviceId") for device in new_devices)
            devices.extend(new_devices)
            index += len(page)
            total = result.get("totalNum")
            if (
                not new_devices
                or len(page) < DEVICE_LIST_PAGE_SIZE
                or (isinstance(total, int) and index >= total)
            ):
                break
        return devices

    async def async_passthrough(
        self, app_server_url: str, device_id: str, request_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Relay a command to a device over its cloud connection."""
        if self.cloud_type != "tapo":
            body: dict[str, Any] = {
                "method": "passthrough",
                "params": {
                    "deviceId": device_id,
                    "requestData": json.dumps(request_data),
                },
            }
            envelope = await self._post(app_server_url, "/", body)
            return self._passthrough_result(envelope)

        failures: list[str] = []
        for mode in _ordered(_TAPO_PASSTHROUGH_MODES, self.passthrough_mode):
            try:
                if mode == PASSTHROUGH_V1_OBJECT:
                    body = {
                        "method": "passthrough",
                        "params": {"deviceId": device_id, "requestData": request_data},
                    }
                    envelope = await self._post(app_server_url, "/", body)
                else:
                    body = {
                        "deviceId": device_id,
                        "requestData": request_data
                        if mode == PASSTHROUGH_V2_OBJECT
                        else json.dumps(request_data),
                    }
                    envelope = await self._post(
                        app_server_url, PATH_TAPO_PASSTHROUGH, body
                    )
                result = self._passthrough_result(envelope)
            except TPLinkUnsupportedRequestError as err:
                failures.append(f"{mode}: {err}")
                continue
            if mode != self.passthrough_mode:
                _LOGGER.debug("Tapo cloud accepted the %s passthrough request", mode)
                self.passthrough_mode = mode
            return result
        raise TPLinkUnsupportedRequestError(
            "Tapo cloud rejected every passthrough request ("
            + "; ".join(failures)
            + ")"
        )

    @staticmethod
    def _passthrough_result(envelope: dict[str, Any]) -> dict[str, Any] | None:
        _raise_for_error(envelope, "passthrough")
        response_data = (envelope.get("result") or {}).get("responseData")
        if isinstance(response_data, str):
            response_data = json.loads(response_data)
        if (
            isinstance(response_data, dict)
            and response_data.get("error_code") == ERR_DEVICE_MALFORMED_REQUEST
        ):
            raise TPLinkUnsupportedRequestError(
                "Device could not parse the relayed request "
                f"({ERR_DEVICE_MALFORMED_REQUEST})",
                error_code=ERR_DEVICE_MALFORMED_REQUEST,
            )
        return response_data


class TPLinkCloudBridge:
    """Manages both clouds for one TP-Link account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        ssl_context: ssl.SSLContext,
        username: str,
        password: str,
        term_id: str,
    ) -> None:
        self.username = username
        self._password = password
        self.kasa = TPLinkCloud(session, ssl_context, "kasa", term_id)
        self.tapo = TPLinkCloud(session, ssl_context, "tapo", term_id)
        self._lock = asyncio.Lock()
        # Cloud that last answered a request, per device id.
        self._device_routes: dict[str, str] = {}
        self._tapo_host_checked = False
        self._tapo_list_error: str | None = None

    def cloud_for(self, cloud_type: str) -> TPLinkCloud:
        return self.tapo if cloud_type == "tapo" else self.kasa

    async def _async_ensure_login(self, cloud: TPLinkCloud) -> None:
        """Make sure a cloud has a usable token (refresh first, then password)."""
        if cloud.token:
            return
        if cloud.refresh_token:
            try:
                await cloud.async_refresh_login()
                return
            except TPLinkTokenExpiredError:
                _LOGGER.debug(
                    "%s refresh token expired, falling back to password login",
                    cloud.cloud_type,
                )
        await cloud.async_login(self.username, self._password)

    async def _async_call(self, cloud: TPLinkCloud, coro_factory):
        """Run a cloud call, transparently re-authenticating once on expiry."""
        async with self._lock:
            await self._async_ensure_login(cloud)
        try:
            return await coro_factory()
        except TPLinkTokenExpiredError:
            async with self._lock:
                cloud.token = None
                await self._async_ensure_login(cloud)
            return await coro_factory()

    async def _async_get_tapo_device_list(self) -> list[dict[str, Any]]:
        """Tapo device list, moving off a stale stored app server once.

        Entries set up before appServerUrlV2 was preferred keep the legacy
        Tapo host in their config data; if that host serves no device list,
        ask the cloud which server the account belongs on and retry there.
        """
        try:
            return await self._async_call(self.tapo, self.tapo.async_get_device_list)
        except TPLinkUnsupportedRequestError:
            if self._tapo_host_checked:
                raise
            self._tapo_host_checked = True
            old_host = self.tapo.host
            try:
                new_host = await self.tapo.async_lookup_host(self.username)
            except TPLinkCloudError:
                new_host = old_host
            if not new_host or new_host == old_host:
                raise
            _LOGGER.debug("Retrying the Tapo device list on the account's V2 server")
            self.tapo.host = new_host
            try:
                return await self._async_call(
                    self.tapo, self.tapo.async_get_device_list
                )
            except TPLinkCloudError:
                self.tapo.host = old_host
                raise

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Merged device list from both clouds.

        Each device dict gains a "cloud_type" key. A device listed by both
        clouds keeps the entry of the cloud it is actually connected to:
        the Kasa cloud lists Tapo devices too, but always as offline.
        """
        kasa_devices: list[dict[str, Any]] = []
        tapo_devices: list[dict[str, Any]] = []
        try:
            kasa_devices = await self._async_call(
                self.kasa, lambda: self.kasa.async_get_device_list()
            )
        except (TPLinkAuthError, TPLinkMFARequiredError):
            raise
        except TPLinkCloudError as err:
            _LOGGER.warning("Kasa cloud device list failed: %s", err)

        try:
            tapo_devices = await self._async_get_tapo_device_list()
        except TPLinkMFARequiredError:
            # The account does use the Tapo cloud, which wants a code:
            # surface it (re-authentication) instead of hiding Tapo devices.
            raise
        except TPLinkAuthError:
            # The Tapo cloud rejects accounts that have never used the Tapo
            # app; that must not break Kasa-only setups.
            if not kasa_devices:
                raise
            _LOGGER.debug("Tapo cloud login rejected; continuing with Kasa only")
        except TPLinkCloudError as err:
            message = str(err)
            if message != self._tapo_list_error:
                _LOGGER.warning(
                    "Tapo cloud device list failed: %s. Tapo devices listed by "
                    "the Kasa cloud are still tried through the Tapo cloud",
                    message,
                )
            else:
                _LOGGER.debug("Tapo cloud device list failed: %s", message)
            self._tapo_list_error = message
        else:
            self._tapo_list_error = None

        return _merge_device_lists(kasa_devices, tapo_devices)

    def _routes(self, device: dict[str, Any]) -> list[str]:
        """Clouds to try for a device, best first."""
        primary = device.get("cloud_type") or "kasa"
        routes = [primary]
        if primary != "tapo" and is_tapo_family(device):
            # Only listed by the Kasa cloud (e.g. the Tapo device list
            # failed): the Tapo cloud is where the device is connected,
            # unless the Kasa cloud says otherwise.
            if device.get("status") == _STATUS_ONLINE:
                routes.append("tapo")
            else:
                routes.insert(0, "tapo")
        known = self._device_routes.get(device.get("deviceId", ""))
        if known in routes:
            routes.remove(known)
            routes.insert(0, known)
        return routes

    def _server_url(self, device: dict[str, Any], cloud: TPLinkCloud) -> str:
        """App server to relay a device's passthrough through."""
        listed_by = device.get("cloud_type") or "kasa"
        if cloud.cloud_type == "tapo":
            if device.get("appServerUrlV2"):
                return device["appServerUrlV2"]
            if listed_by == "tapo" and device.get("appServerUrl"):
                return device["appServerUrl"]
            return cloud.host
        if listed_by == "kasa" and device.get("appServerUrl"):
            return device["appServerUrl"]
        return cloud.host

    async def async_device_request(
        self, device: dict[str, Any], request_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Send a passthrough command to a device from the merged list."""
        device_id = device["deviceId"]
        routes = self._routes(device)
        errors: list[tuple[str, TPLinkCloudError]] = []
        for cloud_type in routes:
            cloud = self.cloud_for(cloud_type)
            app_server_url = self._server_url(device, cloud)
            try:
                response = await self._async_call(
                    cloud,
                    lambda cloud=cloud, url=app_server_url: cloud.async_passthrough(
                        url, device_id, request_data
                    ),
                )
            except TPLinkCloudError as err:
                errors.append((cloud_type, err))
                continue
            self._device_routes[device_id] = cloud_type
            return response

        if len(errors) == 1:
            raise errors[0][1]
        non_auth = [
            err
            for _, err in errors
            if not isinstance(err, (TPLinkAuthError, TPLinkMFARequiredError))
        ]
        if not non_auth:
            raise errors[0][1]
        error_class = (
            TPLinkDeviceOfflineError
            if all(isinstance(err, TPLinkDeviceOfflineError) for err in non_auth)
            else TPLinkCloudError
        )
        raise error_class(
            "; ".join(f"{cloud_type} cloud: {err}" for cloud_type, err in errors)
        ) from errors[-1][1]

    def diagnostics(self) -> dict[str, Any]:
        """Which request shapes each cloud accepted (no identifiers)."""
        return {
            "kasa_logged_in": bool(self.kasa.token),
            "tapo_logged_in": bool(self.tapo.token),
            "tapo_device_list_mode": self.tapo.device_list_mode,
            "tapo_passthrough_mode": self.tapo.passthrough_mode,
            "tapo_device_list_error": self._tapo_list_error,
            "device_routes": sorted(self._device_routes.values()),
        }


def _merge_device_lists(
    kasa_devices: list[dict[str, Any]], tapo_devices: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge both clouds' lists into one entry per device id.

    For a device listed by both clouds, the entry whose cloud reports it
    online wins; if both or neither do, Tapo-family devices keep the Tapo
    entry and everything else the Kasa entry.
    """
    merged: dict[str, dict[str, Any]] = {}
    for cloud_type, devices in (("kasa", kasa_devices), ("tapo", tapo_devices)):
        for device in devices:
            device_id = device.get("deviceId")
            if not device_id:
                continue
            device["cloud_type"] = cloud_type
            existing = merged.get(device_id)
            if existing is None or _prefer_tapo_entry(device, existing):
                merged[device_id] = device
    return list(merged.values())


def _prefer_tapo_entry(tapo: dict[str, Any], kasa: dict[str, Any]) -> bool:
    tapo_online = tapo.get("status") == _STATUS_ONLINE
    kasa_online = kasa.get("status") == _STATUS_ONLINE
    if tapo_online != kasa_online:
        return tapo_online
    return is_tapo_family(tapo) or is_tapo_family(kasa)
