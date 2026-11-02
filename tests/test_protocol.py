"""Protocol tests independent from a Home Assistant installation."""

from __future__ import annotations

import asyncio
import errno
import importlib.util
import json
import logging
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "motorola_nursery" / "protocol.py"
)
SPEC = importlib.util.spec_from_file_location("motorola_protocol", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = protocol
SPEC.loader.exec_module(protocol)


@pytest.fixture
def local_credentials():
    """Use recognizable synthetic secrets to detect accidental disclosure."""
    return protocol.Credentials(
        123456,
        b"private-device-value",
        b"private-magic-token",
        "private-user",
        "private-password",
        "private-access",
    )


@pytest.mark.parametrize(
    "error,category",
    [
        (ConnectionRefusedError("private-endpoint"), "connection_refused"),
        (socket.gaierror(-2, "private-endpoint"), "dns"),
        (OSError(errno.EHOSTUNREACH, "private-endpoint"), "unreachable"),
        (OSError(errno.EACCES, "private-endpoint"), "permission"),
        (OSError("private-endpoint"), "connection"),
        (TimeoutError("private-endpoint"), "timeout"),
    ],
)
async def test_local_tcp_failure_logs_safe_category(
    monkeypatch, caplog, local_credentials, error, category
):
    """TCP failures remain distinguishable without endpoint or exception text."""
    caplog.set_level(logging.DEBUG, logger=protocol.__name__)
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(side_effect=error))
    with pytest.raises(type(error)) as caught:
        await protocol.MagicP2PTunnel.connect("private-host", local_credentials)
    assert caught.value is error
    assert f"stage=tcp_connect category={category}" in caplog.text
    assert "private" not in caplog.text
    assert not any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize(
    "kind,stage,category,shape",
    [
        ("empty", "handshake_receive", "incomplete_response", "empty"),
        ("negative", "handshake_validate", "unexpected_response", "negative_status"),
        ("partial_timeout", "handshake_receive", "timeout", "other"),
        ("reset", "handshake_receive", "connection_reset", "empty"),
        ("wrong_prefix", "handshake_validate", "unexpected_response", "other"),
        ("truncated", "handshake_receive", "incomplete_response", "other"),
        ("send", "handshake_send", "connection", None),
    ],
)
async def test_local_handshake_failures_preserve_stage_and_redact_response(
    monkeypatch, caplog, local_credentials, kind, stage, category, shape
):
    """Different handshake failures expose framing only and close the socket."""
    caplog.set_level(logging.DEBUG, logger=protocol.__name__)
    sid = protocol.generate_sid(local_credentials)
    replies = {
        "empty": [b""],
        "negative": [b"-7 private-response\n", b""],
        "partial_timeout": [b"ok 0 dcon"],
        "reset": [ConnectionResetError("private-response")],
        "wrong_prefix": [b"private".ljust(11 + len(sid), b"x") + b"\n"],
        "truncated": [b"ok 0 dcon", b""],
        "send": [],
    }
    chunks = iter(replies[kind])
    remainder = b""

    async def read(size):
        """Model bounded socket reads and injected receive failures."""
        nonlocal remainder
        chunk = remainder or next(chunks, None)
        if chunk is None:
            await asyncio.Event().wait()
        if isinstance(chunk, BaseException):
            raise chunk
        remainder = chunk[size:]
        return chunk[:size]

    reader = Mock(read=AsyncMock(side_effect=read))
    writer = Mock(
        drain=AsyncMock(), wait_closed=AsyncMock(), is_closing=Mock(return_value=False)
    )
    if kind == "send":
        writer.drain.side_effect = OSError("private-send-error")
    # Teardown errors must not obscure the original failure.
    writer.wait_closed.side_effect = ConnectionResetError("private-cleanup-error")
    monkeypatch.setattr(protocol, "CONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(
        asyncio, "open_connection", AsyncMock(return_value=(reader, writer))
    )
    with pytest.raises((OSError, EOFError)):
        await protocol.MagicP2PTunnel.connect("private-host", local_credentials)
    assert f"stage={stage} category={category}" in caplog.text
    if shape is not None:
        assert f"shape={shape}" in caplog.text
        assert "expected_bytes=10" in caplog.text
    if kind == "partial_timeout":
        assert "received_bytes=9" in caplog.text
    assert "private" not in caplog.text
    assert sid.decode() not in caplog.text
    assert not any(record.exc_info for record in caplog.records)
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


@pytest.mark.parametrize("delimiter", [b"\n", b"\r", b"\0", b" "])
async def test_local_handshake_fragmented_success(
    monkeypatch, caplog, local_credentials, delimiter
):
    """All original terminators work without consuming post-handshake data."""
    caplog.set_level(logging.DEBUG, logger=protocol.__name__)
    sid = protocol.generate_sid(local_credentials)
    plaintext = b"RTSP/1.0 200 OK\r\n\r\n"
    encrypted = protocol.MagicCipher(local_credentials.magic_token).encode(plaintext)
    reply = b"ok 0 dconn " + sid + delimiter
    reader = asyncio.StreamReader()
    writer = Mock(
        drain=AsyncMock(), wait_closed=AsyncMock(), is_closing=Mock(return_value=False)
    )

    # Feed fragments over separate loop turns, followed by tunnel data.
    async def feed():
        """Deliver the extended ACK and cipher payload in separate chunks."""
        for chunk in (reply[:4], reply[4:21], reply[21:] + encrypted):
            reader.feed_data(chunk)
            await asyncio.sleep(0)

    monkeypatch.setattr(
        asyncio, "open_connection", AsyncMock(return_value=(reader, writer))
    )
    feeder = asyncio.create_task(feed())
    tunnel = await protocol.MagicP2PTunnel.connect("private-host", local_credentials)
    await feeder
    assert await tunnel.receive() == plaintext
    assert "received_bytes=10 expected_bytes=10" in caplog.text
    assert "Local tunnel authenticated" in caplog.text
    assert "private" not in caplog.text
    assert sid.decode() not in caplog.text
    writer.close.assert_not_called()
    await tunnel.close()


async def test_local_connect_cancellation_cleans_up(
    monkeypatch, caplog, local_credentials
):
    """Cancellation remains cancellation and closes a partially opened tunnel."""
    caplog.set_level(logging.DEBUG, logger=protocol.__name__)
    reading = asyncio.Event()

    async def read(size):
        reading.set()
        await asyncio.Event().wait()

    reader = Mock(read=AsyncMock(side_effect=read))
    writer = Mock(
        drain=AsyncMock(), wait_closed=AsyncMock(), is_closing=Mock(return_value=False)
    )
    monkeypatch.setattr(
        asyncio, "open_connection", AsyncMock(return_value=(reader, writer))
    )
    task = asyncio.create_task(
        protocol.MagicP2PTunnel.connect("private-host", local_credentials)
    )
    await asyncio.wait_for(reading.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "Local tunnel cancelled: stage=handshake_receive" in caplog.text
    assert "Local tunnel failed" not in caplog.text
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


@pytest.mark.parametrize("ack", [b"ok 0 dconn", b"OK 0 DCONN"])
@pytest.mark.parametrize("long_ack", [False, True])
@pytest.mark.parametrize("split", [1, 10, 11, 42, 89, 90])
async def test_ack_and_cipher_fragmentation(
    monkeypatch, local_credentials, ack, long_ack, split
):
    """Short/long acknowledgements preserve cipher bytes at every boundary."""
    reader = asyncio.StreamReader()
    writer = Mock(
        drain=AsyncMock(), wait_closed=AsyncMock(), is_closing=Mock(return_value=False)
    )
    sid = protocol.generate_sid(local_credentials)
    plaintext = b"RTSP/1.0 200 OK\r\n\r\n"
    # A legitimate cipher prefix may start with a space, like the SID echo.
    monkeypatch.setattr(protocol.os, "urandom", lambda size: bytes([11]) * size)
    encrypted = protocol.MagicCipher(local_credentials.magic_token).encode(plaintext)
    assert encrypted.startswith(b" ")
    reply = ack + (b" " + sid + b"\n" if long_ack else b"") + encrypted

    async def feed():
        """Split the acknowledgement and payload at the selected boundary."""
        reader.feed_data(reply[:split])
        await asyncio.sleep(0)
        reader.feed_data(reply[split:])

    monkeypatch.setattr(
        asyncio, "open_connection", AsyncMock(return_value=(reader, writer))
    )
    feeder = asyncio.create_task(feed())
    tunnel = await protocol.MagicP2PTunnel.connect("private-host", local_credentials)
    received = b""
    while len(received) < len(plaintext):
        received += await asyncio.wait_for(tunnel.receive(), 1)
    assert received == plaintext
    await feeder
    await tunnel.close()


@pytest.mark.usefixtures("socket_enabled")
@pytest.mark.parametrize("long_ack", [False, True])
async def test_camera_waits_for_rtsp_after_ack(monkeypatch, long_ack):
    """A 30-byte-ID camera can wait for RTSP before sending anything else."""
    credentials = protocol.Credentials(
        123456, b"x" * 30, b"synthetic-token", "u", "p", "a"
    )
    request = b"OPTIONS rtsp://camera/ RTSP/1.0\r\n\r\n"
    response = b"RTSP/1.0 200 OK\r\n\r\n"
    finished = asyncio.get_running_loop().create_future()

    async def camera(reader, writer):
        """Wait for a client request before sending the optional echo and RTSP."""
        try:
            handshake = await reader.readexactly(139)
            writer.write(b"ok 0 dconn")
            await writer.drain()
            decoder = protocol.MagicCipher(credentials.magic_token)
            plain = b""
            while len(plain) < len(request):
                chunk = await asyncio.wait_for(reader.read(4096), 1)
                if not chunk:
                    raise EOFError("Client closed before RTSP")
                plain += decoder.decode(chunk)
            # Delay the long suffix until after RTSP, splitting it byte by byte.
            if long_ack:
                for value in b" " + handshake.split()[4] + b"\n":
                    writer.write(bytes([value]))
                    await writer.drain()
                    await asyncio.sleep(0)
            writer.write(protocol.MagicCipher(credentials.magic_token).encode(response))
            await writer.drain()
            finished.set_result(plain)
        except Exception as err:
            finished.set_exception(err)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(camera, "127.0.0.1", 0)
    monkeypatch.setattr(protocol, "CAMERA_PORT", server.sockets[0].getsockname()[1])
    tunnel = None
    try:
        tunnel = await asyncio.wait_for(
            protocol.MagicP2PTunnel.connect("127.0.0.1", credentials), 1
        )
        await tunnel.send(request)
        assert await asyncio.wait_for(tunnel.receive(), 1) == response
        assert await tunnel.receive() == b""
        assert await asyncio.wait_for(finished, 1) == request
    finally:
        if tunnel:
            await tunnel.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("suffix", [b"partial", b"bad_delimiter"])
async def test_invalid_optional_echo(monkeypatch, local_credentials, suffix):
    """A truncated echo or invalid delimiter fails without exposing its bytes."""
    sid = protocol.generate_sid(local_credentials)
    reader = asyncio.StreamReader()
    writer = Mock(
        drain=AsyncMock(), wait_closed=AsyncMock(), is_closing=Mock(return_value=False)
    )
    reader.feed_data(b"ok 0 dconn " + (sid[:4] if suffix == b"partial" else sid + b"!"))
    reader.feed_eof()
    monkeypatch.setattr(
        asyncio, "open_connection", AsyncMock(return_value=(reader, writer))
    )
    tunnel = await protocol.MagicP2PTunnel.connect("private-host", local_credentials)
    with pytest.raises((EOFError, ConnectionError)) as caught:
        await tunnel.receive()
    assert sid.decode() not in str(caught.value)
    await tunnel.close()


@pytest.mark.parametrize("stalled", ["empty", "partial_echo", "echo", "cipher_prefix"])
async def test_first_response_timeout(monkeypatch, caplog, local_credentials, stalled):
    """A stalled echo or cipher prefix cannot occupy a tunnel indefinitely."""
    caplog.set_level(logging.DEBUG, logger=protocol.__name__)
    sid = protocol.generate_sid(local_credentials)
    partial = {
        "empty": b"",
        "partial_echo": b" " + sid[:4],
        "echo": b" " + sid + b"\n",
        "cipher_prefix": b"\x80",
    }[stalled]
    reader = asyncio.StreamReader()
    reader.feed_data(b"ok 0 dconn" + partial)
    writer = Mock(
        drain=AsyncMock(), wait_closed=AsyncMock(), is_closing=Mock(return_value=False)
    )
    monkeypatch.setattr(protocol, "CONNECT_TIMEOUT", 0.02)
    monkeypatch.setattr(
        asyncio, "open_connection", AsyncMock(return_value=(reader, writer))
    )
    tunnel = await protocol.MagicP2PTunnel.connect("private-host", local_credentials)
    await tunnel.send(b"OPTIONS rtsp://camera/ RTSP/1.0\r\n\r\n")
    async with asyncio.timeout(1):
        with pytest.raises(TimeoutError):
            await tunnel.receive()
    writer.close.assert_called_once()
    assert "stage=first_response category=timeout" in caplog.text
    assert "private" not in caplog.text
    assert sid.decode() not in caplog.text
    await tunnel.close()


async def test_first_response_deadline_does_not_limit_established_stream(
    monkeypatch, local_credentials
):
    """Only the first decoded data has a deadline; later stream pauses work."""
    reader = asyncio.StreamReader()
    writer = Mock(
        drain=AsyncMock(), wait_closed=AsyncMock(), is_closing=Mock(return_value=False)
    )
    encoder = protocol.MagicCipher(local_credentials.magic_token)
    reader.feed_data(b"ok 0 dconn" + encoder.encode(b"first"))
    monkeypatch.setattr(protocol, "CONNECT_TIMEOUT", 0.01)
    monkeypatch.setattr(
        asyncio, "open_connection", AsyncMock(return_value=(reader, writer))
    )
    tunnel = await protocol.MagicP2PTunnel.connect("private-host", local_credentials)
    assert await tunnel.receive() == b"first"

    async def delayed_data():
        """Deliver stream data after the initial response budget has elapsed."""
        await asyncio.sleep(0.03)
        reader.feed_data(encoder.encode(b"second"))

    feeder = asyncio.create_task(delayed_data())
    assert await asyncio.wait_for(tunnel.receive(), 1) == b"second"
    await feeder
    writer.close.assert_not_called()
    await tunnel.close()


@pytest.mark.parametrize(
    "vector",
    json.loads((Path(__file__).parent / "fixtures" / "sid_v1.json").read_text())[
        "cases"
    ],
)
def test_sid_matches_native_app_vectors(vector) -> None:
    """Native app outputs cover both VM65-size and full 30-byte credentials."""
    credentials = protocol.Credentials(
        sid_user_id=vector["owner_id"],
        sid_device=vector["device"].encode(),
        magic_token=vector["token"].encode(),
        rtsp_username="synthetic",
        rtsp_password="synthetic",
        access_token="synthetic",
    )
    expected = vector["expected_sid"].encode()
    assert protocol.generate_sid(credentials) == expected
    request, sid = protocol.build_handshake(credentials)
    assert sid == expected
    assert expected in request


@pytest.mark.parametrize("device", [b"ab", b"x" * 31])
def test_sid_rejects_unsupported_lengths_without_values(device) -> None:
    """The widened input remains bounded and errors do not expose credentials."""
    credentials = protocol.Credentials(123456, device, b"synthetic", "u", "p", "a")
    with pytest.raises(ValueError, match="3 to 30 bytes") as caught:
        protocol.generate_sid(credentials)
    assert device.decode() not in str(caught.value)


class CipherTest(unittest.TestCase):
    """Exercise state and TCP fragmentation behavior."""

    def test_round_trip_with_fragmented_prefix_and_payload(self) -> None:
        token = b"a-local-test-token"
        plaintext = b"OPTIONS rtsp://127.0.0.1/ RTSP/1.0\r\n\r\n" * 4
        encoded = protocol.MagicCipher(token).encode(plaintext)
        decoder = protocol.MagicCipher(token)
        fragments = [encoded[:3], encoded[3:11], encoded[11:29], encoded[29:]]
        self.assertEqual(
            b"".join(decoder.decode(part) for part in fragments), plaintext
        )

    def test_sid_and_handshake_shapes(self) -> None:
        credentials = protocol.Credentials(
            sid_user_id=123456,
            sid_device=b"abcdefghijklmnopqrst",
            magic_token=b"token-for-test-123456789012",
            rtsp_username="user",
            rtsp_password="password",
            access_token="access",
        )
        sid = protocol.generate_sid(credentials)
        request, request_sid = protocol.build_handshake(credentials)
        self.assertEqual(len(sid), 78)
        self.assertEqual(request_sid, sid)
        self.assertTrue(request.startswith(b"v002 888 06667 078 "))
        self.assertIn(sid, request)

        request, _ = protocol.build_handshake(credentials, target_port=8080)
        self.assertTrue(request.startswith(b"v002 888 08080 078 "))

    def test_handshake_rejects_invalid_target_port(self) -> None:
        credentials = protocol.Credentials(
            sid_user_id=123456,
            sid_device=b"abcdefghijklmnopqrst",
            magic_token=b"token-for-test-123456789012",
            rtsp_username="user",
            rtsp_password="password",
            access_token="access",
        )
        with self.assertRaisesRegex(ValueError, "Target port"):
            protocol.build_handshake(credentials, target_port=65536)


@pytest.mark.usefixtures("socket_enabled")
class TunnelTest(unittest.IsolatedAsyncioTestCase):
    """Exercise the handshake delimiter and bidirectional cipher state."""

    async def test_tunnel_round_trip(self) -> None:
        credentials = protocol.Credentials(
            sid_user_id=123456,
            sid_device=b"abcdefghijklmnopqrst",
            magic_token=b"token-for-test-123456789012",
            rtsp_username="user",
            rtsp_password="password",
            access_token="access",
        )
        received: list[bytes] = []
        requested_ports: list[bytes] = []

        async def fake_camera(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            handshake = await reader.readexactly(139)
            requested_ports.append(handshake.split()[2])
            sid = handshake.split()[4]
            writer.write(b"ok 0 dconn " + sid + b"\n")
            await writer.drain()
            decoder = protocol.MagicCipher(credentials.magic_token)
            encoder = protocol.MagicCipher(credentials.magic_token)
            while not (plain := decoder.decode(await reader.read(4096))):
                pass
            received.append(plain)
            writer.write(encoder.encode(b"RTSP/1.0 200 OK\r\n\r\n"))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(fake_camera, "127.0.0.1", 0)
        original_port = protocol.CAMERA_PORT
        protocol.CAMERA_PORT = server.sockets[0].getsockname()[1]
        try:
            tunnel = await protocol.MagicP2PTunnel.connect(
                "127.0.0.1", credentials, target_port=8080
            )
            await tunnel.send(b"OPTIONS rtsp://camera/ RTSP/1.0\r\n\r\n")
            self.assertEqual(await tunnel.receive(), b"RTSP/1.0 200 OK\r\n\r\n")
            await tunnel.close()
        finally:
            protocol.CAMERA_PORT = original_port
            server.close()
            await server.wait_closed()
        self.assertEqual(received, [b"OPTIONS rtsp://camera/ RTSP/1.0\r\n\r\n"])
        self.assertEqual(requested_ports, [b"08080"])


@pytest.mark.parametrize("direction", ["send", "receive"])
async def test_transforms_run_off_loop_and_preserve_chunk_order(direction) -> None:
    """A blocked transform must leave the loop and other direction usable."""
    token = b"test-token"
    encoder = protocol.MagicCipher(token)
    chunks = [b"first" * 13108, b"second" * 100]
    reader = Mock(read=AsyncMock(side_effect=[encoder.encode(c) for c in chunks]))
    writer = Mock(drain=AsyncMock(), is_closing=Mock(return_value=False))
    tunnel = protocol.MagicP2PTunnel(reader, writer, token)
    cipher = tunnel._encoder if direction == "send" else tunnel._decoder
    method = "encode" if direction == "send" else "decode"
    transform = getattr(cipher, method)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()
    calls = []

    def blocked_transform(data):
        assert threading.get_ident() != loop_thread
        calls.append(data)
        if len(calls) == 1:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
        return transform(data)

    setattr(cipher, method, blocked_transform)

    async def transfer(chunk):
        if direction == "send":
            return await tunnel.send(chunk)
        return await tunnel.receive()

    first = asyncio.create_task(transfer(chunks[0]))
    second = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(transfer(chunks[1]))
        await asyncio.sleep(0)
        assert len(calls) == 1
        assert not first.done()
        assert not second.done()
        # Full duplex still works while one direction's worker is blocked.
        if direction == "send":
            assert await asyncio.wait_for(tunnel.receive(), 2) == chunks[0]
        else:
            await asyncio.wait_for(tunnel.send(b"request"), 2)
    finally:
        release.set()
        results = await asyncio.gather(first, *([second] if second else []))
    if direction == "send":
        decoder = protocol.MagicCipher(token)
        assert [
            decoder.decode(c.args[0]) for c in writer.write.call_args_list
        ] == chunks
    else:
        assert results == chunks
    assert len(calls) == 2


@pytest.mark.parametrize("direction", ["send", "receive"])
async def test_cancelled_transform_finishes_before_releasing_state(direction) -> None:
    """Cancellation closes the tunnel and waits for its running worker."""
    reader = Mock(read=AsyncMock(return_value=b"encrypted"))
    writer = Mock(drain=AsyncMock(), is_closing=Mock(return_value=False))
    writer.close.side_effect = lambda: setattr(writer.is_closing, "return_value", True)
    tunnel = protocol.MagicP2PTunnel(reader, writer, b"test-token")
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def blocked_transform(data):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return b"transformed"

    if direction == "send":
        tunnel._encoder.encode = blocked_transform
        task = asyncio.create_task(tunnel.send(b"plain"))
        lock = tunnel._send_lock
    else:
        tunnel._decoder.decode = blocked_transform
        task = asyncio.create_task(tunnel.receive())
        lock = tunnel._receive_lock
    try:
        await asyncio.wait_for(started.wait(), 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert lock.locked()
        writer.close.assert_called()
        writer.write.assert_not_called()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not lock.locked()
    with pytest.raises(ConnectionError, match="closed"):
        await tunnel.send(b"more")
    with pytest.raises(ConnectionError, match="closed"):
        await tunnel.receive()


if __name__ == "__main__":
    unittest.main()
