"""Native HA onboarding, reconfiguration and safe failure behavior."""

import asyncio
import errno
import logging
from dataclasses import astuple, replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.motorola_nursery.auth import device_data
from custom_components.motorola_nursery.cloud import (
    AuthenticationError,
    Challenge,
    CloudError,
    Device,
    DeviceListError,
    Session,
    parse_devices,
)
from custom_components.motorola_nursery.config_flow import ConfigFlow
from custom_components.motorola_nursery.const import DOMAIN
from custom_components.motorola_nursery.protocol import build_handshake

DEVICE = Device(
    "123456",
    "device-identifier",
    "VM65",
    "Nursery%20Test",
    "test-magic-token",
    "abcdefghijklmnopqrst",
    "-1",
)
SESSION = Session("123456", "synthetic-token", "1234567", "9.moto.5gencare.com")


@pytest.mark.parametrize(
    ("error", "category", "ui_error"),
    [
        (CloudError("private-token"), "protocol", "code_request_failed"),
        (TimeoutError("private-endpoint"), "network", "cannot_connect_cloud"),
    ],
)
@pytest.mark.asyncio
async def test_first_setup_failure_has_safe_logs_and_version_context(
    hass, caplog, error, category, ui_error
):
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    client = make_client()
    client.request_code.side_effect = error
    with (
        patch(
            "custom_components.motorola_nursery.config_flow.async_client",
            AsyncMock(return_value=client),
        ),
        patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock()),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "private@example.test"}
        )
    assert result["errors"] == {"base": ui_error}
    assert "Account setup started: integration=" in caplog.text
    assert "home_assistant=" in caplog.text
    assert f"stage=code_request category={category}" in caplog.text
    assert "private" not in caplog.text
    client.close.assert_awaited_once()
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


def make_client():
    client = AsyncMock()
    client.host = SESSION.host
    client.request_code.return_value = Challenge("123456", SESSION.host)
    client.submit_code.return_value = SESSION
    client.devices.return_value = [DEVICE]
    return client


def make_flow(hass, source="user", entry=None):
    flow = ConfigFlow()
    flow.hass = hass
    flow.context = {"source": source}
    if entry:
        flow.context["entry_id"] = entry.entry_id
    return flow


@pytest.mark.parametrize("step", ["host", "change_host"])
@pytest.mark.parametrize("failure", ["connect", "close"])
async def test_local_probe_failure_logs_context_and_allows_retry(
    hass, caplog, step, failure
):
    """Local failures preserve setup state and never expose private values."""
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    original = {**device_data(DEVICE), "host": "192.0.2.10"}
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE.sid_device, data=original)
    entry.add_to_hass(hass)
    flow = make_flow(hass, config_entries.SOURCE_RECONFIGURE, entry)
    flow._selected = DEVICE
    flow._session = SESSION
    tunnel = AsyncMock()
    connect = AsyncMock(return_value=tunnel)
    if failure == "connect":
        connect.side_effect = ConnectionRefusedError("private-host")
    else:
        tunnel.close.side_effect = ConnectionResetError("private-host")
    with patch(
        "custom_components.motorola_nursery.config_flow.MagicP2PTunnel.connect", connect
    ):
        result = await getattr(flow, f"async_step_{step}")({"host": "192.0.2.11"})
    source = "account_setup" if step == "host" else "change_host"
    assert f"Local camera probe started: source={source}" in caplog.text
    assert "Local camera probe failed: category=connection" in caplog.text
    assert ("Local camera probe closing:" in caplog.text) == (failure == "close")
    assert result["step_id"] == step
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data == original
    assert flow._session == SESSION
    assert flow._selected == DEVICE
    assert "private" not in caplog.text
    assert "192.0.2" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,sid_device",
    [("VM65", "abcdefghijklmnopqrst"), ("VM85CONNECT", "x" * 30)],
)
async def test_full_ui_flow(hass, model, sid_device):
    flow = make_flow(hass)
    client = make_client()
    device = replace(DEVICE, model=model, sid_device=sid_device)
    client.devices.return_value = parse_devices(["1", *astuple(device)])

    async def connect(host, credentials):
        assert host == "192.0.2.10"
        assert credentials.sid_device == sid_device.encode()
        build_handshake(credentials)
        return AsyncMock()

    with (
        patch(
            "custom_components.motorola_nursery.config_flow.async_client",
            AsyncMock(return_value=client),
        ),
        patch(
            "custom_components.motorola_nursery.config_flow.MagicP2PTunnel.connect",
            AsyncMock(side_effect=connect),
        ),
    ):
        result = await flow.async_step_user()
        assert result["step_id"] == "user"
        client.request_code.assert_not_called()
        result = await flow.async_step_user({"email": "owner@example.test"})
        assert result["step_id"] == "code"
        result = await flow.async_step_code({"code": "123456"})
        assert result["step_id"] == "device"
        result = await flow.async_step_device({"device": sid_device})
        assert result["step_id"] == "host"
        result = await flow.async_step_host({"host": "192.0.2.10"})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["cloud_session"]["token"] == SESSION.token
    assert result["data"]["sid_device"] == sid_device
    assert "code" not in result["data"]
    client.request_code.assert_awaited_once()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_code_retry_does_not_send_new_email(hass):
    flow = make_flow(hass)
    client = make_client()
    client.submit_code.side_effect = [AuthenticationError(), SESSION]
    with patch(
        "custom_components.motorola_nursery.config_flow.async_client",
        AsyncMock(return_value=client),
    ):
        await flow.async_step_user({"email": "owner@example.test"})
        result = await flow.async_step_code({"code": "000000"})
        assert result["errors"] == {"code": "invalid_code"}
        assert "default" not in str(result["data_schema"])
        result = await flow.async_step_code({"code": "123456"})
    assert result["step_id"] == "device"
    client.request_code.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconfigure_preserves_entry_and_id(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE.sid_device,
        data={**device_data(DEVICE), "host": "192.0.2.10"},
        title="Existing camera",
    )
    entry.add_to_hass(hass)
    flow = make_flow(hass, config_entries.SOURCE_RECONFIGURE, entry)
    client = make_client()
    with (
        patch(
            "custom_components.motorola_nursery.config_flow.async_client",
            AsyncMock(return_value=client),
        ),
        patch(
            "custom_components.motorola_nursery.config_flow.MagicP2PTunnel.connect",
            AsyncMock(return_value=AsyncMock()),
        ),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        assert (await flow.async_step_reconfigure())["type"] == FlowResultType.MENU
        await flow.async_step_user({"email": "owner@example.test"})
        assert (await flow.async_step_code({"code": "123456"}))["step_id"] == "host"
        result = await flow.async_step_host({"host": "192.0.2.11"})
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == DEVICE.sid_device
    assert entry.title == "Existing camera"
    assert entry.data["host"] == "192.0.2.11"
    assert entry.data["cloud_session"]["token"] == SESSION.token


@pytest.mark.asyncio
async def test_wrong_account_does_not_modify_entry(hass):
    original = {**device_data(DEVICE), "host": "192.0.2.10"}
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE.sid_device, data=original)
    entry.add_to_hass(hass)
    flow = make_flow(hass, config_entries.SOURCE_REAUTH, entry)
    client = make_client()
    client.devices.return_value = []
    with patch(
        "custom_components.motorola_nursery.config_flow.async_client",
        AsyncMock(return_value=client),
    ):
        assert (await flow.async_step_reauth(entry.data))["step_id"] == "user"
        await flow.async_step_user({"email": "other@example.test"})
        result = await flow.async_step_code({"code": "123456"})
    assert result["reason"] == "wrong_account"
    assert entry.data == original


@pytest.mark.asyncio
async def test_device_list_retry_does_not_replay_code(hass):
    flow = make_flow(hass)
    first, second = make_client(), make_client()
    first.devices.side_effect = TimeoutError()
    second.resume.return_value = Session(
        "123456", "rotated-token", "1234567", SESSION.host
    )
    with patch(
        "custom_components.motorola_nursery.config_flow.async_client",
        AsyncMock(side_effect=[first, second]),
    ):
        await flow.async_step_user({"email": "owner@example.test"})
        result = await flow.async_step_code({"code": "123456"})
        assert result["step_id"] == "devices"
        result = await flow.async_step_devices({})
    assert result["step_id"] == "device"
    assert flow._session.token == "rotated-token"
    first.submit_code.assert_awaited_once()
    second.submit_code.assert_not_called()
    second.request_code.assert_not_called()


@pytest.mark.asyncio
async def test_real_ha_flow_manager_and_abort_cleanup(hass):
    client = make_client()
    with (
        patch(
            "custom_components.motorola_nursery.config_flow.async_client",
            AsyncMock(return_value=client),
        ),
        patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock()),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "owner@example.test"}
        )
        assert result["step_id"] == "code"
        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_device_response_keeps_authenticated_retry_without_new_otp(
    hass, caplog
):
    """Camera-list validation has its own UI error and never replays a used code."""
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    first, second = make_client(), make_client()
    first.devices.side_effect = DeviceListError("private-response")
    second.resume.return_value = SESSION
    second.devices.return_value = []
    flow = make_flow(hass)
    with patch(
        "custom_components.motorola_nursery.config_flow.async_client",
        AsyncMock(side_effect=[first, second]),
    ):
        await flow.async_step_user({"email": "private@example.test"})
        result = await flow.async_step_code({"code": "654321"})
        assert result["step_id"] == "devices"
        assert result["errors"] == {"base": "invalid_device_response"}
        assert flow._session == SESSION
        result = await flow.async_step_devices({})
    assert result["reason"] == "no_devices"
    first.request_code.assert_awaited_once()
    first.submit_code.assert_awaited_once()
    first.close.assert_awaited_once()
    second.request_code.assert_not_called()
    second.submit_code.assert_not_called()
    second.resume.assert_awaited_once_with(SESSION)
    second.close.assert_awaited_once()
    assert "stage=device_listing category=device_response" in caplog.text
    assert "private" not in caplog.text


async def test_local_control_reconfigure_preserves_account_and_identity(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE.sid_device,
        data={
            **device_data(DEVICE),
            "host": "192.0.2.10",
            "cloud_session": SESSION.as_dict(),
        },
    )
    entry.add_to_hass(hass)
    before = dict(entry.data)
    with (
        patch("homeassistant.config_entries.ConfigEntries.async_reload", AsyncMock()),
        patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock()),
    ):
        flow = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
        )
        flow = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"next_step_id": "local_control"}
        )
        assert flow["step_id"] == "local_control"
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"local_control": True}
        )
    assert result["reason"] == "reconfigure_successful"
    assert dict(entry.data) == {**before, "local_control": True}
    assert entry.unique_id == DEVICE.sid_device


@pytest.mark.asyncio
async def test_static_rtsp_port_onboarding_and_validation(hass, caplog, socket_enabled):
    """Onboarding supports optional static RTSP port with immediate validation."""
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    other_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="other-camera-device-id",
        data={**device_data(DEVICE), "host": "192.0.2.20", "rtsp_port": 8520},
    )
    other_entry.add_to_hass(hass)

    flow = make_flow(hass)
    assert (await flow.async_step_rtsp_port())["reason"] == "login_connection_lost"

    client = make_client()
    blocker = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    blocked_port = int(blocker.sockets[0].getsockname()[1])
    free_probe = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    free_port = int(free_probe.sockets[0].getsockname()[1])
    free_probe.close()
    await free_probe.wait_closed()

    try:
        with (
            patch(
                "custom_components.motorola_nursery.config_flow.async_client",
                AsyncMock(return_value=client),
            ),
            patch(
                "custom_components.motorola_nursery.config_flow.MagicP2PTunnel.connect",
                AsyncMock(return_value=AsyncMock()),
            ),
        ):
            await flow.async_step_user({"email": "owner@example.test"})
            await flow.async_step_code({"code": "123456"})
            await flow.async_step_device({"device": DEVICE.sid_device})
            step = await flow.async_step_host(
                {"host": "192.0.2.10", "use_static_rtsp_port": True}
            )
            assert step["step_id"] == "rtsp_port"

            invalid = await flow.async_step_rtsp_port(
                {"use_static_rtsp_port": True, "rtsp_port": 80}
            )
            assert invalid["errors"] == {"rtsp_port": "invalid_port"}
            assert "reason=out_of_range" in caplog.text

            duplicate = await flow.async_step_rtsp_port(
                {"use_static_rtsp_port": True, "rtsp_port": 8520}
            )
            assert duplicate["errors"] == {"rtsp_port": "port_in_use"}
            assert "reason=configured_by_another_camera" in caplog.text

            occupied = await flow.async_step_rtsp_port(
                {"use_static_rtsp_port": True, "rtsp_port": blocked_port}
            )
            assert occupied["errors"] == {"rtsp_port": "port_in_use"}
            assert "reason=address_in_use" in caplog.text
            assert "errno=EADDRINUSE" in caplog.text

            with patch(
                "custom_components.motorola_nursery.config_flow.asyncio.start_server",
                AsyncMock(
                    side_effect=PermissionError(errno.EACCES, "Permission denied")
                ),
            ):
                bind_err = await flow.async_step_rtsp_port(
                    {"use_static_rtsp_port": True, "rtsp_port": free_port}
                )
            assert bind_err["errors"] == {"rtsp_port": "cannot_bind_port"}
            assert "reason=bind_error" in caplog.text
            assert "errno=EACCES" in caplog.text

            created = await flow.async_step_rtsp_port(
                {"use_static_rtsp_port": True, "rtsp_port": free_port}
            )
            assert created["type"] == FlowResultType.CREATE_ENTRY
            assert created["data"]["rtsp_port"] == free_port
            assert (
                f"Local RTSP port validation succeeded: port={free_port}" in caplog.text
            )
    finally:
        blocker.close()
        await blocker.wait_closed()


@pytest.mark.asyncio
async def test_rtsp_port_reconfigure_and_disable(hass, socket_enabled):
    """Reconfigure can enable, keep active bound port, and disable static port."""
    probe = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    port = int(probe.sockets[0].getsockname()[1])
    probe.close()
    await probe.wait_closed()

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE.sid_device,
        data={
            **device_data(DEVICE),
            "host": "192.0.2.10",
            "cloud_session": SESSION.as_dict(),
        },
    )
    entry.add_to_hass(hass)
    with (
        patch("homeassistant.config_entries.ConfigEntries.async_reload", AsyncMock()),
        patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock()),
    ):
        flow = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
        )
        flow = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"next_step_id": "rtsp_port"}
        )
        assert flow["step_id"] == "rtsp_port"
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"use_static_rtsp_port": True, "rtsp_port": port}
        )
        assert result["reason"] == "reconfigure_successful"
        assert entry.data["rtsp_port"] == port

        # Keep existing port while the entry's own bridge is bound to it.
        active = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", port)
        try:
            entry.runtime_data = type(
                "RuntimeStub",
                (),
                {"bridge": type("BridgeStub", (), {"port": port})()},
            )()
            flow2 = make_flow(hass, config_entries.SOURCE_RECONFIGURE, entry)
            keep = await flow2.async_step_rtsp_port(
                {"use_static_rtsp_port": True, "rtsp_port": port}
            )
            assert keep["reason"] == "reconfigure_successful"
            assert entry.data["rtsp_port"] == port

            # Disable via rtsp_port step during pending host flow and reconfigure.
            flow3 = make_flow(hass, config_entries.SOURCE_RECONFIGURE, entry)
            flow3._selected = DEVICE
            flow3._session = SESSION
            with patch(
                "custom_components.motorola_nursery.config_flow.MagicP2PTunnel.connect",
                AsyncMock(return_value=AsyncMock()),
            ):
                await flow3.async_step_host(
                    {"host": "192.0.2.10", "use_static_rtsp_port": True}
                )
                disabled = await flow3.async_step_rtsp_port(
                    {"use_static_rtsp_port": False, "rtsp_port": port}
                )
            assert disabled["reason"] == "reconfigure_successful"
            assert entry.data["rtsp_port"] == 0

            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "rtsp_port": port}
            )
            flow4 = make_flow(hass, config_entries.SOURCE_RECONFIGURE, entry)
            flow4._selected = DEVICE
            flow4._session = SESSION
            with patch(
                "custom_components.motorola_nursery.config_flow.MagicP2PTunnel.connect",
                AsyncMock(return_value=AsyncMock()),
            ):
                cleared = await flow4.async_step_host(
                    {"host": "192.0.2.10", "use_static_rtsp_port": False}
                )
            assert cleared["reason"] == "reconfigure_successful"
            assert entry.data["rtsp_port"] == 0

            flow5 = make_flow(hass, config_entries.SOURCE_RECONFIGURE, entry)
            reconf_off = await flow5.async_step_rtsp_port(
                {"use_static_rtsp_port": False, "rtsp_port": port}
            )
            assert reconf_off["reason"] == "reconfigure_successful"
            assert entry.data["rtsp_port"] == 0
        finally:
            active.close()
            await active.wait_closed()
