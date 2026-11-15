"""Config flow handling of the Tapo cloud's emailed-code login (Tapo #1)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from custom_components.tapo_cloud.api import TPLinkCloudError, TPLinkMFARequiredError
from custom_components.tapo_cloud.config_flow import TapoCloudConfigFlow


def _flow() -> TapoCloudConfigFlow:
    flow = TapoCloudConfigFlow()
    flow._username = "user"
    flow._password = "password"
    flow._cloud_states = {"kasa": "pending", "tapo": "pending"}
    bridge = MagicMock()
    bridge.kasa.async_login = AsyncMock()
    bridge.tapo.async_login = AsyncMock(
        side_effect=TPLinkMFARequiredError("code needed", mfa_process_id="proc-1")
    )
    bridge.tapo.async_send_mfa_code = AsyncMock()
    bridge.cloud_for = lambda cloud_type: getattr(bridge, cloud_type)
    flow._bridge = bridge
    flow.async_step_mfa = AsyncMock(return_value={"step_id": "mfa"})
    return flow


async def test_tapo_code_is_emailed_before_asking_for_it() -> None:
    flow = _flow()
    assert await flow._async_advance() == {"step_id": "mfa"}
    flow._bridge.tapo.async_send_mfa_code.assert_awaited_once_with("user", "password")
    assert flow._mfa_cloud == "tapo"
    assert flow._cloud_states == {"kasa": "ok", "tapo": "mfa"}


async def test_code_is_still_asked_for_if_emailing_it_fails() -> None:
    """The login itself may already have emailed a code."""
    flow = _flow()
    flow._bridge.tapo.async_send_mfa_code.side_effect = TPLinkCloudError("nope")
    assert await flow._async_advance() == {"step_id": "mfa"}
    assert flow._cloud_states == {"kasa": "ok", "tapo": "mfa"}
