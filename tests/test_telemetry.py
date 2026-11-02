"""Synthetic control service: bounded reads, framing, TLS and safe setters."""

import asyncio
import ssl
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from custom_components.motorola_nursery.cloud import CloudError
from custom_components.motorola_nursery.protocol import Credentials
from custom_components.motorola_nursery.telemetry import (
    Capability,
    TelemetryClient,
    TelemetryError,
    parse_capabilities,
    parse_values,
)

CREDENTIALS = Credentials(
    123456, b"synthetic-device", b"synthetic-token", "user", "pass", "access"
)
CAPS = {
    "temperature_reading": Capability("int", -200, 500),
    "wifi_signal": Capability("int", 0, 100),
}
CAP_LINE = (
    b"caplist 3 temperature_reading r int -200 500 wifi_signal r int 0 100 "
    b"wifi_ssid r str 1 64\n"
)


def test_capabilities_filter_personal_data_and_validate_frames():
    """Discover only known capabilities after checking complete row framing."""
    assert parse_capabilities(CAP_LINE.decode().split()) == CAPS
    for message in (
        "caplist -1",
        "caplist 1",
        "caplist 257",
        "caplist 1 wifi_signal r int 100 0",
        "caplist 2 wifi_signal r int 0 100 wifi_signal r int 0 100",
        "caplist 1 wifi_signal r str 0 100",
    ):
        parts = message.split()
        with pytest.raises(TelemetryError, match="Invalid capability"):
            parse_capabilities(parts)


@pytest.mark.parametrize("value", ["501", "-201", "nan", "secret-value"])
def test_invalid_sensor_value_does_not_hide_other_sensors(value):
    result = parse_values(
        f"get 2 temperature_reading {value} wifi_signal 94".split(), CAPS
    )
    assert result == {"temperature_reading": None, "wifi_signal": 94}


@pytest.mark.parametrize(
    "message",
    [
        "get 1 wifi_signal 94",
        "get 2 temperature_reading 241 wifi_signal",
        "get 2 wifi_signal 94 wifi_signal 90",
        "get 2 temperature_reading 241 wifi_ssid private-network",
        "get -1",
    ],
)
def test_partial_or_unexpected_response_is_rejected(message):
    """Partial or mismatched sensor frames must fail without private payloads."""
    parts = message.split()
    with pytest.raises(TelemetryError, match="Invalid sensor response") as error:
        parse_values(parts, CAPS)
    assert "private-network" not in str(error.value)


def make_transport(
    *, auth=b"app 1 OK\n", values=b"get 2 temperature_reading 241 wifi_signal 94\n"
):
    reader = asyncio.StreamReader()
    writer = Mock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    commands = []

    def write(data):
        commands.append(data)
        if data.startswith(b"app "):
            response = b"ping\n" + auth
        elif data == b"caplist\n":
            response = CAP_LINE
        elif data.startswith(b"get "):
            response = values
        else:
            assert data in {b"pong\n", b"pang\n"}
            return
        # Exercise TCP fragmentation and multiple complete lines in one read.
        loop = asyncio.get_running_loop()
        for index in range(0, len(response), 3):
            loop.call_soon(reader.feed_data, response[index : index + 3])

    writer.write.side_effect = write
    return reader, writer, commands


async def test_read_uses_verified_tls_and_only_allowed_getters():
    """Telemetry must verify vendor TLS and query only allowlisted readings."""
    context = ssl.create_default_context()
    client = TelemetryClient("9.moto.5gencare.com", context)
    first, second = make_transport(), make_transport()
    with patch(
        "asyncio.open_connection", AsyncMock(side_effect=[first[:2], second[:2]])
    ) as connect:
        for _ in range(2):
            data = await client.async_read(CREDENTIALS)
            assert data.values == {"temperature_reading": 241, "wifi_signal": 94}
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert connect.call_args.kwargs["ssl"] is context
    assert connect.call_args.kwargs["server_hostname"] == "9.moto.5gencare.com"
    assert connect.call_args.args[1] == 2288
    assert b"caplist\n" in first[2]
    assert b"caplist\n" not in second[2]
    for _, writer, commands in (first, second):
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()
        assert b"get 2 temperature_reading wifi_signal\n" in commands
        assert all(
            line.split()[0] in {b"app", b"caplist", b"get", b"pong"}
            for line in commands
        )


async def test_auth_rejection_does_not_query_or_disclose_credentials():
    reader, writer, commands = make_transport(auth=b"app -1 secret-value\n")
    client = TelemetryClient("9.moto.5gencare.com", ssl.create_default_context())
    with (
        patch("asyncio.open_connection", AsyncMock(return_value=(reader, writer))),
        pytest.raises(TelemetryError, match="authentication") as error,
    ):
        await client.async_read(CREDENTIALS)
    assert "secret-value" not in str(error.value)
    assert "synthetic-token" not in str(error.value)
    assert all(line.split()[0] in {b"app", b"pong"} for line in commands)
    writer.close.assert_called_once()


async def test_timeout_and_cancellation_close_connection():
    client = TelemetryClient("9.moto.5gencare.com", ssl.create_default_context())
    for cancel in (False, True):
        reader = asyncio.StreamReader()
        writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
        with (
            patch("asyncio.open_connection", AsyncMock(return_value=(reader, writer))),
            patch(
                "custom_components.motorola_nursery.telemetry.TIMEOUT",
                0.02 if not cancel else 10,
            ),
        ):
            task = asyncio.create_task(client.async_read(CREDENTIALS))
            await asyncio.sleep(0)
            if cancel:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else TelemetryError):
                await task
        writer.close.assert_called_once()


def test_no_arbitrary_control_host():
    """Control connections must reject hosts outside the vendor domain."""
    context = ssl.create_default_context()
    with pytest.raises(CloudError, match="Unexpected authentication server"):
        TelemetryClient("example.com", context)


def make_control_transport(state, commands):
    reader = asyncio.StreamReader()
    writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())

    def write(data):
        commands.append(data)
        parts = data.decode().split()
        response = None
        if parts[:1] == ["app"]:
            response = b"app 1 OK\n"
        elif parts == ["caplist"]:
            response = (
                b"caplist 9 video_brightness w int 0 4 playing w str 1 128 "
                b"audio_control w int 0 1 speaker_volume w int 1 6 "
                b"wifi_ssid r str 1 64 existsongs r str 1 1024 pan w str 1 16 "
                b"tilt w str 1 16 ptz w str 1 16\n"
            )
        elif parts[:2] == ["set", "existsongs"]:
            response = b"set existsongs Twinkle_Little_Star.mp3,White_Noise.mp3\n"
        elif parts[:2] == ["set", "audio_timer"]:
            return
        elif parts[:1] == ["set"]:
            key, value = parts[1:3]
            if key in state:
                state[key] = value
            response = f"set {key} {value}\n".encode()
        elif parts[:1] == ["get"]:
            keys = parts[2:]
            response = (
                f"get {len(keys)} "
                + " ".join(f"{key} {state[key]}" for key in keys)
                + "\n"
            ).encode()
        if response is not None:
            asyncio.get_running_loop().call_soon(reader.feed_data, response)

    writer.write.side_effect = write
    return reader, writer


async def test_write_playlist_playback_and_ptz_are_strictly_allowlisted():
    state = {
        "video_brightness": "1",
        "playing": "(none)",
        "audio_control": "0",
        "speaker_volume": "6",
    }
    commands = []
    transports = [make_control_transport(state, commands) for _ in range(7)]
    client = TelemetryClient("9.moto.5gencare.com", ssl.create_default_context())
    with (
        patch("asyncio.open_connection", AsyncMock(side_effect=transports)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        assert await client.async_set(CREDENTIALS, "video_brightness", 2) == 2
        assert await client.async_list_songs(CREDENTIALS) == (
            "Twinkle_Little_Star.mp3",
            "White_Noise.mp3",
        )
        await client.async_play(CREDENTIALS, "White_Noise.mp3")
        await client.async_stop(CREDENTIALS)
        await client.async_ptz(CREDENTIALS, "pan", "left")
        await client.async_ptz(CREDENTIALS, "ptz", "origin")

    assert state["video_brightness"] == "2"
    assert b"set playing White_Noise.mp3\n" in commands
    assert b"set audio_timer 120\n" not in commands
    assert b"set audio_control 0\n" in commands
    assert b"set pan left\n" in commands
    assert b"set pan stop\n" in commands
    assert b"set ptz origin\n" in commands
    assert not any(b"ota_update" in command for command in commands)


async def test_rejects_unknown_writes_songs_and_ptz_without_network():
    client = TelemetryClient("9.moto.5gencare.com", ssl.create_default_context())
    client._songs = ("White_Noise.mp3",)
    with patch("asyncio.open_connection", AsyncMock()) as connect:
        with pytest.raises(TelemetryError, match="Unsupported camera setting"):
            await client.async_set(CREDENTIALS, "ota_update", 1)
        with pytest.raises(TelemetryError, match="Unknown camera lullaby"):
            await client.async_play(CREDENTIALS, "../../private.mp3")
        with pytest.raises(TelemetryError, match="Unsupported PTZ"):
            await client.async_ptz(CREDENTIALS, "pan", "origin")
    connect.assert_not_awaited()


def test_access_and_command_capabilities_are_retained_without_reading_commands():
    caps = parse_capabilities(
        [
            "caplist",
            "3",
            "video_brightness",
            "r",
            "int",
            "0",
            "4",
            "pan",
            "w",
            "str",
            "1",
            "16",
            "existsongs",
            "r",
            "str",
            "1",
            "1024",
        ]
    )
    assert caps["video_brightness"].writable is False
    assert caps["pan"].writable is True
    assert "existsongs" in caps


async def test_read_only_setting_and_unadvertised_ptz_never_send_writes():
    client = TelemetryClient("9.moto.5gencare.com", ssl.create_default_context())
    reader, writer, commands = make_transport()
    client._capabilities = {"video_brightness": Capability("int", 0, 4)}
    with patch("asyncio.open_connection", AsyncMock(return_value=(reader, writer))):
        with pytest.raises(TelemetryError, match="Invalid camera setting"):
            await client.async_set(CREDENTIALS, "video_brightness", 2)
        with pytest.raises(TelemetryError, match="not supported"):
            await client.async_ptz(CREDENTIALS, "pan", "left")
    assert not any(command.startswith(b"set ") for command in commands)


async def test_command_capabilities_are_not_sent_as_getters():
    """Command capabilities must remain visible without becoming sensor queries."""
    client = TelemetryClient("9.moto.5gencare.com", ssl.create_default_context())
    reader, writer, commands = make_transport()
    client._capabilities = {
        **CAPS,
        "pan": Capability("str", 1, 16, True),
        "existsongs": Capability("str", 1, 1024),
    }
    with patch("asyncio.open_connection", AsyncMock(return_value=(reader, writer))):
        data = await client.async_read(CREDENTIALS)
    assert "pan" in data.capabilities
    assert "existsongs" in data.capabilities
    assert set(data.values) == set(CAPS)
    assert b"get 2 temperature_reading wifi_signal\n" in commands


async def test_cancelled_direction_still_sends_stop():
    state = {
        "video_brightness": "1",
        "playing": "(none)",
        "audio_control": "0",
        "speaker_volume": "4",
    }
    commands = []
    reader, writer = make_control_transport(state, commands)
    client = TelemetryClient("9.moto.5gencare.com", ssl.create_default_context())
    with (
        patch("asyncio.open_connection", AsyncMock(return_value=(reader, writer))),
        patch("asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await client.async_ptz(CREDENTIALS, "pan", "left")
    assert b"set pan left\n" in commands
    assert b"set pan stop\n" in commands
    writer.close.assert_called_once()


@pytest.mark.parametrize(
    "axis,direction,reply", [("pan", "left", "stop"), ("ptz", "origin", "0")]
)
async def test_false_ptz_acknowledgement_is_rejected(axis, direction, reply):
    commands = []
    state = {
        "video_brightness": "1",
        "playing": "(none)",
        "audio_control": "0",
        "speaker_volume": "4",
    }
    reader, writer = make_control_transport(state, commands)
    write = writer.write.side_effect

    def respond(data):
        if data == f"set {axis} {direction}\n".encode():
            commands.append(data)
            asyncio.get_running_loop().call_soon(
                reader.feed_data, f"set {axis} {reply}\n".encode()
            )
        else:
            write(data)

    writer.write.side_effect = respond
    client = TelemetryClient("9.moto.5gencare.com", ssl.create_default_context())
    with (
        patch("asyncio.open_connection", AsyncMock(return_value=(reader, writer))),
        pytest.raises(TelemetryError, match="not confirmed"),
    ):
        await client.async_ptz(CREDENTIALS, axis, direction)
    if axis == "pan":
        assert b"set pan stop\n" in commands


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("playing", "(none)", "(none)"),
        ("playing", "Song.mp3", "Song.mp3"),
        ("playing", "../private.mp3", None),
        ("firmware_version", "V1.6.37", "V1.6.37"),
        ("firmware_version", "private/value", None),
        ("firmware_version", "x" * 65, None),
    ],
)
def test_string_readings_remain_filtered_without_losing_other_sensors(
    key, value, expected
):
    """Unsafe string readings must not hide valid readings in the same frame."""
    capabilities = {key: Capability("str", 1, 128), **CAPS}
    parts = ["get", "3", key, value, "temperature_reading", "241", "wifi_signal", "94"]
    assert parse_values(parts, capabilities) == {
        key: expected,
        "temperature_reading": 241,
        "wifi_signal": 94,
    }


@pytest.mark.parametrize(
    "operation,field,value", [("get", "wifi_signal", None), ("set", "pan", "stop")]
)
async def test_exchange_skips_short_and_unrelated_replies_before_exact_match(
    operation, field, value
):
    """Keepalives and stale replies must not satisfy a different control request."""
    client = TelemetryClient("9.moto.5gencare.com", ssl.create_default_context())
    reader = asyncio.StreamReader()
    writer = Mock(drain=AsyncMock())
    reply = f"get 1 {field} 94" if operation == "get" else f"set {field} {value}"
    reader.feed_data(f"\nping\npong\n{operation}\nset pan left\n{reply}\n".encode())
    reader.feed_eof()
    result = await client._exchange(reader, writer, None, operation, field, value)
    assert result == reply.split()
    assert writer.write.call_args_list == [call(b"pong\n"), call(b"pang\n")]


async def test_exchange_rejects_a_flood_of_unrelated_replies():
    """A request must stop after its bounded allowance of unrelated replies."""
    client = TelemetryClient("9.moto.5gencare.com", ssl.create_default_context())
    reader = asyncio.StreamReader()
    reader.feed_data(b"get 1 private 1\n" * 65)
    reader.feed_eof()
    writer = Mock(drain=AsyncMock())
    with pytest.raises(TelemetryError, match="Too many unrelated"):
        await client._exchange(reader, writer, None, "set", "pan", "stop")
    assert await reader.readline() == b"get 1 private 1\n"
