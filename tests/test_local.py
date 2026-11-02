"""Real loopback F3 cameras: identity, readings, events and disconnects."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from nacl.public import Box, PrivateKey, PublicKey

from custom_components.motorola_nursery.f3 import (
    F3Cipher,
    F3Connection,
    transform_hello,
)
from custom_components.motorola_nursery.local import (
    LocalControlServer,
    LocalEventListener,
    LocalPeer,
    LocalTelemetryClient,
    Registration,
)
from custom_components.motorola_nursery.protocol import Credentials
from custom_components.motorola_nursery.telemetry import TelemetryError

CREDENTIALS = Credentials(
    123456, b"synthetic-device", b"synthetic-token", "user", "pass", "access"
)
CAPS = (
    b"caplist 3 temperature_reading r int -200 500 wifi_signal r int 0 100 "
    b"wifi_ssid r str 1 64\r\n"
)


async def camera(port, registration=b"dev 123456 synthetic-token 1.0.0\n"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    greeting = transform_hello(await reader.readexactly(72))
    assert b"V3" in greeting[:16]
    private = PrivateKey.generate()
    writer.write(
        transform_hello(greeting[:16] + bytes(private.public_key) + greeting[48:])
    )
    await writer.drain()
    box = Box(private, PublicKey(greeting[16:48]))
    table = box.decrypt(await reader.readexactly(1040), greeting[48:])
    connection = F3Connection(reader, writer, table)
    await connection.send(registration)
    return connection


async def connected(port, *, acknowledge=True):
    connection = await camera(port)
    reply = (await connection.readline()).decode().split()
    assert reply[:2] == ["dev", "2"]
    clock = datetime.strptime(reply[2], "%m%d%H%M%Y.%S")
    assert abs((clock.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds()) < 10
    assert await connection.readline() == (
        b"magicp2p 1 synthetic-device synthetic-device\n"
    )
    if acknowledge:
        await connection.send(b"magicp2p 1\n")
    return connection


async def test_local_read_events_and_reconnect(socket_enabled):
    server = LocalControlServer(0)
    registration = await server.register("camera-one", lambda: CREDENTIALS)
    client = LocalTelemetryClient(registration)
    events, states = [], []
    listener = LocalEventListener(registration, events.append, states.append)
    listener.start()
    await asyncio.sleep(0)
    assert states == [False]
    try:
        for _ in range(2):
            connection = await connected(server.port)

            async def respond(connection=connection):
                assert await connection.readline() == b"caplist\n"
                await connection.send(CAPS)
                assert await connection.readline() == (
                    b"get 2 temperature_reading wifi_signal\n"
                )
                await connection.send(
                    b"notify synthetic-token motiondetect synthetic-event\n"
                    b"notify wrong-token sounddetect ignored-event\n"
                    b"get 2 temperature_reading 239 wifi_signal 71\r\n"
                )
                assert await connection.readline() == b"notify 1 1\n"
                # Subsequent polls reuse capabilities on this socket.
                assert await connection.readline() == (
                    b"get 2 temperature_reading wifi_signal\n"
                )
                await connection.send(b"get 2 temperature_reading 240 wifi_signal 70\n")

            task = asyncio.create_task(respond())
            with patch(
                "asyncio.open_connection", side_effect=AssertionError("Cloud forbidden")
            ):
                data = await client.async_read(CREDENTIALS)
                second = await client.async_read(CREDENTIALS)
            await task
            assert second.values == {"temperature_reading": 240, "wifi_signal": 70}
            assert data.values == {"temperature_reading": 239, "wifi_signal": 71}
            assert len(data.capabilities) == 2
            connection.close()
            await connection.writer.wait_closed()
            await asyncio.sleep(0.01)
        assert len(events) == 1  # Replayed detection is deduplicated across sockets.
        assert events[0].event_type == "motion"
        assert True in states
    finally:
        await listener.stop()
        await server.unregister("camera-one")
    assert not server._tasks
    assert not registration.ready.is_set()


@pytest.mark.parametrize(
    "line",
    [
        b"dev 123456 private-wrong-token 1.0.0\n",
        b"dev 999999 synthetic-token 1.0.0\n",
        b"dev malformed\n",
        b"dev 999999999999999999999 synthetic-token 1.0.0\n",
    ],
)
async def test_unknown_camera_is_rejected_without_secret_logs(
    socket_enabled, caplog, line
):
    """Unknown identities must disconnect without logging credentials."""
    server = LocalControlServer(0)
    await server.register("camera-one", lambda: CREDENTIALS)
    try:
        connection = await camera(server.port, line)
        async with asyncio.timeout(1):
            with pytest.raises(EOFError):
                await connection.readline()
        connection.close()
        await connection.writer.wait_closed()
        assert "private-wrong-token" not in caplog.text
        assert "synthetic-token" not in caplog.text
    finally:
        await server.unregister("camera-one")


async def test_listener_shutdown_cancels_partial_handshakes(socket_enabled):
    server = LocalControlServer(0)
    await server.register("one", lambda: CREDENTIALS)
    await server.register("two", lambda: CREDENTIALS)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    await reader.readexactly(72)
    await server.unregister("one")
    assert server._server is not None
    await asyncio.wait_for(server.unregister("two"), 2)
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()
    assert not server._tasks


async def test_missing_camera_never_falls_back_to_cloud():
    client = LocalTelemetryClient(Registration(lambda: CREDENTIALS))
    with (
        patch("custom_components.motorola_nursery.local.TIMEOUT", 0.01),
        patch("asyncio.open_connection", side_effect=AssertionError("Cloud forbidden")),
        pytest.raises(TelemetryError, match="Local camera"),
    ):
        await client.async_read(CREDENTIALS)


async def test_cancelled_getter_closes_socket_to_discard_late_responses(socket_enabled):
    """Cancelled reads must close the peer so late replies cannot satisfy new reads."""
    server = LocalControlServer(0)
    registration = await server.register("one", lambda: CREDENTIALS)
    connection = await connected(server.port)
    client = LocalTelemetryClient(registration)
    task = asyncio.create_task(client.async_read(CREDENTIALS))
    try:
        assert await connection.readline() == b"caplist\n"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(1):
            with pytest.raises(EOFError):
                await connection.readline()
    finally:
        connection.close()
        await connection.writer.wait_closed()
        await server.unregister("one")


def test_cipher_rejects_invalid_sizes_and_preserves_fragmented_state():
    for key, index in ((b"short", 0), (bytes(1024), 1024)):
        with pytest.raises(ValueError):
            F3Cipher(key, index)
    with pytest.raises(ValueError):
        transform_hello(bytes(71))
    table = bytes(range(256)) * 4
    encoder, decoder = F3Cipher(table, 731), F3Cipher(table, 731)
    plaintext = b"caplist\nget 1 temperature_reading\n" * 60
    wire = encoder.transform(plaintext, encode=True)
    assert (
        b"".join(decoder.transform(wire[i : i + 3]) for i in range(0, len(wire), 3))
        == plaintext
    )


async def test_encrypted_line_limit_is_enforced():
    reader = asyncio.StreamReader()
    writer = Mock()
    connection = F3Connection(reader, writer, bytes(1024))
    reader.feed_data(bytes(65536))
    with pytest.raises(ValueError, match="line limit"):
        await connection.readline()


async def test_late_connection_gets_full_command_deadline():
    registration = Registration(lambda: CREDENTIALS)
    peer = Mock(
        closed=False,
        command_lock=asyncio.Lock(),
        responses=asyncio.Queue(),
    )
    client = LocalTelemetryClient(registration)

    async def connect_later():
        await asyncio.sleep(0.12)
        registration.peer = peer
        registration.ready.set()

    task = asyncio.create_task(connect_later())
    with patch("custom_components.motorola_nursery.local.TIMEOUT", 0.2):
        async with client._session(CREDENTIALS):
            await asyncio.sleep(0.12)
    await task
    peer.close.assert_not_called()


async def test_malformed_reply_invalidates_persistent_session(socket_enabled):
    """Malformed control frames must retire the shared persistent session."""
    server = LocalControlServer(0)
    registration = await server.register("one", lambda: CREDENTIALS)
    connection = await connected(server.port)
    client = LocalTelemetryClient(registration)
    task = asyncio.create_task(client.async_read(CREDENTIALS))
    try:
        assert await connection.readline() == b"caplist\n"
        await connection.send(b"caplist invalid\n")
        with pytest.raises(TelemetryError):
            await task
        async with asyncio.timeout(1):
            with pytest.raises(EOFError):
                await connection.readline()
    finally:
        connection.close()
        await connection.writer.wait_closed()
        await server.unregister("one")


async def test_setup_ack_required_before_getters(socket_enabled):
    server = LocalControlServer(0)
    registration = await server.register("one", lambda: CREDENTIALS)
    events = []
    registration.event_callbacks.add(events.append)
    client = LocalTelemetryClient(registration)
    connection = await connected(server.port, acknowledge=False)
    task = asyncio.create_task(client.async_read(CREDENTIALS))
    try:
        await connection.send(b"ping\n")
        assert await connection.readline() == b"pong\n"
        await connection.send(b"notify synthetic-token motiondetect setup-event\n")
        assert await connection.readline() == b"notify 1 1\n"
        assert len(events) == 1
        assert not registration.ready.is_set()
        assert not task.done()
        await connection.send(b"magicp2p 1\n")
        assert await connection.readline() == b"caplist\n"
        await connection.send(CAPS)
        assert (await connection.readline()).startswith(b"get 2 ")
        await connection.send(b"get 2 temperature_reading 240 wifi_signal 70\n")
        assert (await task).values["temperature_reading"] == 240
    finally:
        task.cancel()
        connection.close()
        await connection.writer.wait_closed()
        await server.unregister("one")


@pytest.mark.parametrize("acknowledgement", [None, b"magicp2p 0\n"])
async def test_missing_or_rejected_setup_never_becomes_ready(
    socket_enabled, acknowledgement
):
    """Only a complete successful initialization may publish a ready peer."""
    server = LocalControlServer(0)
    registration = await server.register("one", lambda: CREDENTIALS)
    try:
        with patch(
            "custom_components.motorola_nursery.local.INITIALIZATION_TIMEOUT", 0.1
        ):
            connection = await connected(server.port, acknowledge=False)
            if acknowledgement is not None:
                await connection.send(acknowledgement)
            async with asyncio.timeout(1):
                with pytest.raises(EOFError):
                    await connection.readline()
            assert not registration.ready.is_set()
            connection.close()
            await connection.writer.wait_closed()
    finally:
        await server.unregister("one")


async def test_shutdown_aborts_stalled_transports_within_deadline():
    closed = asyncio.Event()
    transport_server = Mock(spec=asyncio.Server)
    transport_server.wait_closed = AsyncMock(side_effect=closed.wait)
    transport_server.abort_clients.side_effect = closed.set
    server = LocalControlServer(0)
    server._server = transport_server
    with patch("custom_components.motorola_nursery.local.SHUTDOWN_TIMEOUT", 0.01):
        await asyncio.wait_for(server._stop(), 0.5)
    transport_server.close.assert_called_once()
    transport_server.abort_clients.assert_called_once()
    assert server._server is None


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_multiple_entries_require_unique_registration(socket_enabled, ambiguous):
    """Camera identity must select exactly one registration across entries."""
    second_credentials = replace(
        CREDENTIALS,
        sid_device=b"second-device",
        magic_token=CREDENTIALS.magic_token if ambiguous else b"second-token",
    )
    server = LocalControlServer(0)
    first = await server.register("one", lambda: CREDENTIALS)
    second = await server.register("two", lambda: second_credentials)
    connection = None
    try:
        if ambiguous:
            connection = await camera(server.port)
            async with asyncio.timeout(1):
                with pytest.raises(EOFError):
                    await connection.readline()
            assert first.peer is None
            assert not first.ready.is_set()
        else:
            connection = await connected(server.port)
            await asyncio.wait_for(first.ready.wait(), 1)
            assert first.peer is not None
        assert second.peer is None
        assert not second.ready.is_set()
    finally:
        if connection:
            connection.close()
            await connection.writer.wait_closed()
        await server.unregister("one")
        await server.unregister("two")


@pytest.mark.parametrize("hint", [b"", b" private-upload-hint"])
@pytest.mark.parametrize(
    "kind,event_type",
    [
        (b"motiondetect", "motion"),
        (b"sounddetect", "sound"),
        (b"sounddetect2", "sound"),
    ],
)
async def test_local_notifications_accept_optional_upload_hint(
    hint, kind, event_type, caplog
):
    registration = Registration(lambda: CREDENTIALS)
    events = []
    registration.event_callbacks.add(events.append)
    connection = Mock(send=AsyncMock())
    peer = LocalPeer(connection, registration)
    wire = b"notify synthetic-token " + kind + b" private-event-name" + hint + b"\n"
    await peer.handle_line(wire)
    await peer.handle_line(wire)
    assert [event.event_type for event in events] == [event_type]
    assert connection.send.await_count == 2
    assert all(
        call.args == (b"notify 1 1\n",) for call in connection.send.await_args_list
    )
    assert "private-upload-hint" not in caplog.text
    assert "private-event-name" not in caplog.text


@pytest.mark.parametrize(
    "line",
    [
        b"notify wrong-token sounddetect2 private-event private-hint\n",
        b"notify synthetic-token sounddetect2 private-event too many fields\n",
        b"notify synthetic-token sounddetect2 private-event " + b"x" * 1025 + b"\n",
    ],
)
async def test_invalid_local_notification_hint_or_identity_is_rejected(line):
    registration = Registration(lambda: CREDENTIALS)
    events = []
    registration.event_callbacks.add(events.append)
    connection = Mock(send=AsyncMock())
    peer = LocalPeer(connection, registration)
    await peer.handle_line(line)
    assert not events
    connection.send.assert_not_awaited()


async def test_unknown_notification_type_is_acked_without_using_hint_as_event():
    registration = Registration(lambda: CREDENTIALS)
    events = []
    registration.event_callbacks.add(events.append)
    connection = Mock(send=AsyncMock())
    peer = LocalPeer(connection, registration)
    await peer.handle_line(
        b"notify synthetic-token sdstatus opaque-name motiondetect\n"
    )
    assert not events
    connection.send.assert_awaited_once_with(b"notify 1 1\n")


@pytest.mark.parametrize("play_before_poll", [False, True])
async def test_local_settings_playlist_playback_and_cache_reconnect(
    socket_enabled, play_before_poll
):
    """Refresh a changed peer's playlist even when playback precedes polling."""
    server = LocalControlServer(0)
    registration = await server.register("controls", lambda: CREDENTIALS)
    client = LocalTelemetryClient(registration)
    commands = []
    state = {
        "temperature_reading": "239",
        "video_brightness": "1",
        "playing": "(none)",
        "audio_control": "0",
        "speaker_volume": "4",
    }
    capline = (
        b"caplist 7 temperature_reading r int -200 500 "
        b"video_brightness w int 0 4 playing w str 1 128 "
        b"audio_control w int 0 1 speaker_volume w int 1 6 "
        b"existsongs r str 1 1024 pan w str 1 16\n"
    )
    connections = []
    tasks = []

    async def respond(connection, song):
        try:
            while True:
                parts = (await connection.readline()).decode().split()
                commands.append(parts)
                if parts == ["caplist"]:
                    await connection.send(capline)
                elif parts[:1] == ["get"]:
                    keys = parts[2:]
                    assert not {"existsongs", "pan"} & set(keys)
                    await connection.send(
                        (
                            "get "
                            + str(len(keys))
                            + " "
                            + " ".join(f"{k} {state[k]}" for k in keys)
                            + "\n"
                        ).encode()
                    )
                elif parts[:2] == ["set", "existsongs"]:
                    await connection.send(f"set existsongs {song}\n".encode())
                elif parts[:1] == ["set"]:
                    key, value = parts[1:]
                    if key in state:
                        state[key] = value
                    await connection.send((" ".join(parts) + "\n").encode())
                elif parts == ["pong"]:
                    await connection.send(b"pong 1\n")
                else:
                    assert parts == ["notify", "1", "1"]
        except (EOFError, ConnectionError):
            return

    try:
        for index, song in enumerate(("First_Song.mp3", "Second_Song.mp3")):
            connection = await connected(server.port)
            connections.append(connection)
            task = asyncio.create_task(respond(connection, song))
            tasks.append(task)
            with patch(
                "asyncio.open_connection", side_effect=AssertionError("Cloud forbidden")
            ):
                if index == 1 and play_before_poll:
                    await asyncio.wait_for(registration.ready.wait(), 1)
                    assert registration.peer is not client._last_peer
                    assert client._songs == ("First_Song.mp3",)
                    await client.async_play(CREDENTIALS, song)
                    assert client._songs == (song,)
                data = await client.async_read(CREDENTIALS)
                assert len(data.capabilities) == 7
                assert len(data.values) == 5
                assert await client.async_list_songs(CREDENTIALS) == (song,)
                assert await client.async_list_songs(CREDENTIALS) == (song,)
                assert await client.async_set(CREDENTIALS, "video_brightness", 2) == 2
                if index == 0:
                    await client.async_play(CREDENTIALS, song)
                    await client.async_stop(CREDENTIALS)
                    await client.async_ptz(CREDENTIALS, "pan", "left", duration=0.1)
            connection.close()
            await connection.writer.wait_closed()
            await task
            for _ in range(10):
                if not registration.ready.is_set():
                    break
                await asyncio.sleep(0)
        assert commands.count(["set", "existsongs", "1"]) == 2
        assert ["set", "playing", "First_Song.mp3"] in commands
        assert ["set", "audio_control", "0"] in commands
        assert ["set", "pan", "stop"] in commands
        assert not any(p[:2] == ["set", "audio_timer"] for p in commands)
    finally:
        for connection in connections:
            connection.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.unregister("controls")


@pytest.mark.parametrize(
    "method,args",
    [
        ("async_set", ("video_brightness", 2)),
        ("async_list_songs", ()),
        ("async_play", ("Song.mp3",)),
        ("async_stop", ()),
        ("async_ptz", ("pan", "left")),
    ],
)
async def test_missing_local_controls_never_fall_back_to_cloud(method, args):
    """Every local control operation must fail locally when its peer is absent."""
    client = LocalTelemetryClient(Registration(lambda: CREDENTIALS))
    control = getattr(client, method)
    with (
        patch("custom_components.motorola_nursery.local.TIMEOUT", 0.01),
        patch("asyncio.open_connection", side_effect=AssertionError("Cloud forbidden")),
        pytest.raises(TelemetryError, match="Local camera"),
    ):
        await control(CREDENTIALS, *args)


@pytest.mark.parametrize("payload", [b"", b"Song.mp3,Song.mp3", b"../private.mp3"])
async def test_invalid_complete_playlist_preserves_readings_events_and_retry(
    socket_enabled, payload
):
    """A bad playlist cannot disconnect an otherwise synchronized camera."""
    server = LocalControlServer(0)
    registration = await server.register("playlist", lambda: CREDENTIALS)
    client = LocalTelemetryClient(registration)
    events = []
    registration.event_callbacks.add(events.append)
    connection = await connected(server.port)

    async def respond():
        assert await connection.readline() == b"caplist\n"
        await connection.send(
            b"caplist 2 wifi_signal r int 0 100 existsongs r str 1 1024\n"
        )
        assert await connection.readline() == b"get 1 wifi_signal\n"
        await connection.send(b"get 1 wifi_signal 71\n")
        assert await connection.readline() == b"set existsongs 1\n"
        await connection.send(b"set existsongs " + payload + b"\n")
        await connection.send(b"notify synthetic-token motiondetect playlist-event\n")
        replies = {await connection.readline(), await connection.readline()}
        assert replies == {b"notify 1 1\n", b"get 1 wifi_signal\n"}
        await connection.send(b"get 1 wifi_signal 72\n")
        assert await connection.readline() == b"set existsongs 1\n"
        await connection.send(b"set existsongs Song.mp3\n")

    task = asyncio.create_task(respond())
    try:
        with patch(
            "asyncio.open_connection", side_effect=AssertionError("Cloud forbidden")
        ):
            assert (await client.async_read(CREDENTIALS)).values == {"wifi_signal": 71}
            peer = registration.peer
            with pytest.raises(
                TelemetryError, match="Invalid camera playlist"
            ) as error:
                await client.async_list_songs(CREDENTIALS)
            assert "private" not in str(error.value)
            assert registration.peer is peer
            assert not peer.closed
            assert (await client.async_read(CREDENTIALS)).values == {"wifi_signal": 72}
            assert await client.async_list_songs(CREDENTIALS) == ("Song.mp3",)
        await task
        assert [event.event_type for event in events] == ["motion"]
        assert registration.ready.is_set()
        assert registration.peer is peer
    finally:
        connection.close()
        await connection.writer.wait_closed()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.unregister("playlist")


async def test_incomplete_playlist_response_still_closes_shared_peer(socket_enabled):
    """An unfinished response must be discarded before another request can start."""
    server = LocalControlServer(0)
    registration = await server.register("playlist", lambda: CREDENTIALS)
    client = LocalTelemetryClient(registration)
    connection = await connected(server.port)

    async def respond():
        assert await connection.readline() == b"caplist\n"
        await connection.send(b"caplist 1 existsongs r str 1 1024\n")
        assert await connection.readline() == b"set existsongs 1\n"
        await connection.send(b"set existsongs Song.mp3")
        with pytest.raises(EOFError):
            await connection.readline()

    task = asyncio.create_task(respond())
    try:
        with (
            patch("custom_components.motorola_nursery.local.TIMEOUT", 0.1),
            pytest.raises(TelemetryError, match="Local camera control unavailable"),
        ):
            await client.async_list_songs(CREDENTIALS)
        await task
        assert client._last_peer.closed
    finally:
        connection.close()
        await connection.writer.wait_closed()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.unregister("playlist")


async def test_response_queue_overflow_is_handled_and_releases_registration(
    socket_enabled,
):
    """A flooding authenticated peer disconnects without an escaped task exception."""
    server = LocalControlServer(0)
    registration = await server.register("flood", lambda: CREDENTIALS)
    connection = await connected(server.port)
    try:
        async with asyncio.timeout(1):
            await registration.ready.wait()
        peer = registration.peer
        assert peer is not None
        handler = next(iter(server._tasks))
        peer.request_pending = True
        await connection.send(b"get 1 wifi_signal 94\n" * 65)
        async with asyncio.timeout(1):
            await handler
        assert peer.closed
        assert registration.peer is None
        assert not registration.ready.is_set()
        assert peer.responses.qsize() == 1
        assert peer.responses.get_nowait() is None
    finally:
        connection.close()
        await connection.writer.wait_closed()
        await server.unregister("flood")
