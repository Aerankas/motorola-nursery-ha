"""Unload must close active clients before waiting for server shutdown."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.motorola_nursery.bridge import (
    TALKBACK_CHUNK_BYTES,
    RtspBridge,
    decode_alaw_to_s16le,
    default_rtsp_port,
    extract_rtp_payload,
)
from custom_components.motorola_nursery.protocol import (
    INTERNAL_TALKBACK_PORT,
    Credentials,
    MagicP2PTunnel,
)


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


def test_decode_alaw_to_s16le_and_extract_rtp_payload():
    """Verify G.711 A-law decoding and RTP header/extension/padding stripping."""
    pcm = decode_alaw_to_s16le(b"\xd5\x55")
    assert len(pcm) == 4
    assert pcm == b"\x08\x00\xf8\xff"

    assert extract_rtp_payload(b"short") == b""
    assert extract_rtp_payload(b"\x00" * 16) == b""
    rtp_basic = b"\x80\x08\x00\x01\x00\x00\x00\xa0\x12\x34\x56\x78" + b"\xd5" * 160
    assert extract_rtp_payload(rtp_basic) == b"\xd5" * 160

    rtp_ext_pad = (
        b"\xb0\x08\x00\x01\x00\x00\x00\xa0\x12\x34\x56\x78"
        b"\xbe\xde\x00\x01\x11\x22\x33\x44"
        b"payload"
        b"\x00\x02"
    )
    assert extract_rtp_payload(rtp_ext_pad) == b"payload"


@pytest.mark.asyncio
async def test_talkback_rtsp_and_pcm_streaming(socket_enabled):
    """Verify /talkback RTSP negotiation and lazy port 8999 PCM chunk forwarding."""
    talkback_tunnel = AsyncMock()
    credentials = Credentials(123456, b"synthetic-device", b"test-token", "u", "p", "t")
    bridge = RtspBridge("192.0.2.10", credentials, lambda err: None)
    connect_mock = AsyncMock(return_value=talkback_tunnel)
    with patch(
        "custom_components.motorola_nursery.bridge.MagicP2PTunnel.connect",
        connect_mock,
    ):
        await bridge.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        try:
            writer.write(
                b"OPTIONS rtsp://127.0.0.1/talkback RTSP/1.0\r\nCSeq: 1\r\n\r\n"
                b"DESCRIBE rtsp://127.0.0.1/talkback RTSP/1.0\r\nCSeq: 2\r\n\r\n"
                b"SETUP rtsp://127.0.0.1/talkback/track0 RTSP/1.0\r\nCSeq: 3\r\n"
                b"Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n"
                b"PLAY rtsp://127.0.0.1/talkback RTSP/1.0\r\nCSeq: 4\r\n\r\n"
            )
            await writer.drain()
            resp = await asyncio.wait_for(reader.read(4096), 1)
            assert b"a=sendonly" in resp
            assert b"PCMA/8000/1" in resp
            connect_mock.assert_not_awaited()

            rtp_pkt = (
                b"\x80\x08\x00\x01\x00\x00\x00\xa0\x12\x34\x56\x78" + b"\xd5" * 400
            )
            frame = b"$\x00" + len(rtp_pkt).to_bytes(2, "big") + rtp_pkt
            writer.write(
                frame
                + b"TEARDOWN rtsp://127.0.0.1/talkback RTSP/1.0\r\nCSeq: 5\r\n\r\n"
            )
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), 1)
            await asyncio.wait_for(asyncio.gather(*tuple(bridge._tasks)), 1)

            connect_mock.assert_awaited_once_with(
                "192.0.2.10", credentials, target_port=INTERNAL_TALKBACK_PORT
            )
            assert talkback_tunnel.send.await_count == 2
            for call in talkback_tunnel.send.await_args_list:
                assert len(call.args[0]) == TALKBACK_CHUNK_BYTES
            talkback_tunnel.close.assert_awaited_once()
        finally:
            writer.close()
            await writer.wait_closed()
            await bridge.stop()
