"""Unload must close active clients before waiting for server shutdown."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.motorola_nursery.bridge import RtspBridge, default_rtsp_port
from custom_components.motorola_nursery.protocol import Credentials, MagicP2PTunnel


@pytest.mark.asyncio
async def test_stop_closes_active_tunnel_and_forwarding_tasks(socket_enabled):
    receiving = asyncio.Event()
    cancelled = asyncio.Event()

    async def receive():
        receiving.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    tunnel = AsyncMock()
    tunnel.receive.side_effect = receive
    credentials = Credentials(
        123456, b"abcdefghijklmnopqrst", b"test-token", "u", "p", "t"
    )
    bridge = RtspBridge("192.0.2.10", credentials, lambda err: None)
    with patch(
        "custom_components.motorola_nursery.bridge.MagicP2PTunnel.connect",
        AsyncMock(return_value=tunnel),
    ):
        await bridge.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        try:
            await asyncio.wait_for(receiving.wait(), 1)
            await asyncio.wait_for(bridge.stop(), 2)
            assert cancelled.is_set()
            assert await reader.read() == b""
            tunnel.close.assert_awaited_once()
        finally:
            writer.close()
            await writer.wait_closed()
            await bridge.stop()


@pytest.mark.parametrize("failure", ["receive", "send", "eof", "client_close"])
async def test_forwarding_failures_trigger_recovery_but_client_close_does_not(
    socket_enabled, failure
):
    async def pending_receive():
        await asyncio.Future()

    tunnel = AsyncMock()
    tunnel.receive.side_effect = pending_receive
    if failure == "receive":
        tunnel.receive.side_effect = ConnectionResetError("Synthetic camera reset")
    elif failure == "send":
        tunnel.send.side_effect = OSError("Synthetic send failure")
    elif failure == "eof":
        tunnel.receive.side_effect = None
        tunnel.receive.return_value = b""
    errors = Mock()
    credentials = Credentials(123456, b"synthetic-device", b"test-token", "u", "p", "t")
    bridge = RtspBridge("192.0.2.10", credentials, errors)
    with patch(
        "custom_components.motorola_nursery.bridge.MagicP2PTunnel.connect",
        AsyncMock(return_value=tunnel),
    ):
        await bridge.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        try:
            if failure == "client_close":
                writer.write_eof()
            else:
                writer.write(b"synthetic request")
                await writer.drain()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            await bridge.stop()

            assert bridge.active_connections == 0
            tunnel.close.assert_awaited_once()
            if failure == "client_close":
                errors.assert_not_called()
            else:
                errors.assert_called_once()
                assert isinstance(errors.call_args.args[0], (OSError, EOFError))
        finally:
            writer.close()
            await writer.wait_closed()
            await bridge.stop()


async def test_partial_echo_timeout_releases_bridge_slot(socket_enabled, monkeypatch):
    """A client left open cannot retain a bridge slot after an echo stalls."""
    monkeypatch.setattr(
        "custom_components.motorola_nursery.protocol.CONNECT_TIMEOUT", 0.02
    )
    credentials = Credentials(123456, b"synthetic-device", b"test-token", "u", "p", "t")
    camera_reader = asyncio.StreamReader()
    camera_reader.feed_data(b" 0001")
    camera_writer = Mock(
        drain=AsyncMock(), wait_closed=AsyncMock(), is_closing=Mock(return_value=False)
    )
    tunnel = MagicP2PTunnel(camera_reader, camera_writer, credentials.magic_token)
    tunnel._handshake_echo = b" 0001synthetic-echo"
    errors = Mock()
    bridge = RtspBridge("192.0.2.10", credentials, errors)
    with patch(
        "custom_components.motorola_nursery.bridge.MagicP2PTunnel.connect",
        AsyncMock(return_value=tunnel),
    ):
        await bridge.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        try:
            writer.write(b"OPTIONS rtsp://camera/ RTSP/1.0\r\n\r\n")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            await asyncio.wait_for(asyncio.gather(*tuple(bridge._tasks)), 1)
            assert bridge.active_connections == 0
            assert bridge._slots._value == 4
            camera_writer.wait_closed.assert_awaited_once()
            errors.assert_called_once()
            assert isinstance(errors.call_args.args[0], TimeoutError)
        finally:
            writer.close()
            await writer.wait_closed()
            await bridge.stop()


async def test_configured_and_ephemeral_loopback_port_selection(socket_enabled):
    """Bridge uses ephemeral port by default and binds configured static port."""
    assert default_rtsp_port("192.0.2.10") == 8510
    assert default_rtsp_port("192.0.2.255") == 8554
    assert default_rtsp_port("camera.local") == 8554

    credentials = Credentials(123456, b"synthetic-device", b"test-token", "u", "p", "t")
    ephemeral_bridge = RtspBridge("192.0.2.10", credentials, lambda err: None)
    try:
        await ephemeral_bridge.start()
        assert ephemeral_bridge.port > 0
        await ephemeral_bridge.start()
        occupied_port = ephemeral_bridge.port

        conflict_bridge = RtspBridge(
            "192.0.2.11", credentials, lambda err: None, port=occupied_port
        )
        with pytest.raises(OSError):
            await conflict_bridge.start()
    finally:
        await ephemeral_bridge.stop()

    static_bridge = RtspBridge(
        "192.0.2.10", credentials, lambda err: None, port=occupied_port
    )
    try:
        await static_bridge.start()
        assert static_bridge.port == occupied_port
    finally:
        await static_bridge.stop()


async def test_streaming_request_injects_current_access_token(socket_enabled):
    """Bridge injects or replaces accessToken on /owner/streaming requests."""
    sent = asyncio.Event()
    tunnel = AsyncMock()

    async def record_send(data: bytes) -> None:
        if b"/owner/streaming" in data:
            sent.set()

    async def pending_receive() -> bytes:
        await asyncio.Future()
        return b""

    tunnel.send.side_effect = record_send
    tunnel.receive.side_effect = pending_receive
    credentials = Credentials(
        123456, b"synthetic-device", b"test-token", "u", "p", "active-token"
    )
    bridge = RtspBridge("192.0.2.10", credentials, lambda err: None)
    with patch(
        "custom_components.motorola_nursery.bridge.MagicP2PTunnel.connect",
        AsyncMock(return_value=tunnel),
    ):
        await bridge.start()
        _, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        try:
            writer.write(b"DESCRIBE rtsp://127.0.0.1/owner/streaming RTSP/1.0\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(sent.wait(), 1)
            tunnel.send.assert_awaited_with(
                b"DESCRIBE rtsp://127.0.0.1/owner/streaming"
                b"?accessToken=active-token RTSP/1.0\r\n\r\n"
            )
        finally:
            writer.close()
            await writer.wait_closed()
            await bridge.stop()
