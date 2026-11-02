"""Certificate-verified Motorola camera telemetry and controls.

This service is separate from the local video tunnel and account-login service.
Only explicitly allowlisted getters and controls are sent.
"""

from __future__ import annotations

import asyncio
import re
import ssl
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

from .cloud import atom, vendor_host
from .protocol import Credentials

CONTROL_PORT = 2288
MAX_LINE = 65536
TIMEOUT = 15
SONG_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,96}\.mp3")

STRING_FIELDS = frozenset(
    {
        "hardware_version",
        "firmware_version",
        "playing",
        "existsongs",
        "pan",
        "tilt",
        "ptz",
    }
)
COMMAND_FIELDS = frozenset({"existsongs", "pan", "tilt", "ptz"})

WRITABLE_FIELDS = frozenset(
    {
        "video_bitrate",
        "video_brightness",
        "video_light_frequency",
        "speaker_volume",
        "motion",
        "sound",
        "night_vision",
        "ceiling_mount",
        "temperature_low",
        "temperature_high",
        "lowtemp_switch",
        "hightemp_switch",
        "video[0].motion_zone",
        *(f"video[0].motion.zone[{index}].enable" for index in range(4)),
    }
)

# Deliberately exclude identity, network names, playlists and all write commands.
READABLE_FIELDS = frozenset(
    {
        "temperature_reading",
        "wifi_signal",
        "hardware_version",
        "firmware_version",
        "playing",
        "audio_control",
        "video_bitrate",
        "video_light_frequency",
        "videoKbps",
        "video_brightness",
        "speaker_volume",
        "motion",
        "sound",
        "night_vision",
        "ceiling_mount",
        "temperature_low",
        "temperature_high",
        "lowtemp_switch",
        "hightemp_switch",
        "video[0].motion_zone",
        *(f"video[0].motion.zone[{index}].enable" for index in range(4)),
    }
)


CONTROL_VALUES = {
    "night_vision": frozenset(range(3)),
    "video_bitrate": frozenset(range(4)),
    "video_light_frequency": frozenset(range(2)),
    **{
        key: frozenset({0, 1})
        for key in WRITABLE_FIELDS
        if key in {"ceiling_mount", "lowtemp_switch", "hightemp_switch"}
        or key.startswith("video[0].motion")
    },
}


class TelemetryError(Exception):
    """A safe error without wire data or credentials."""


class PlaylistValidationError(TelemetryError):
    """A complete playlist reply is unusable, but stream framing remains valid."""


@dataclass(frozen=True, slots=True)
class Capability:
    """Advertised type, bounds and access for a known protocol field."""

    kind: str
    minimum: int
    maximum: int
    writable: bool = False


@dataclass(frozen=True, slots=True)
class TelemetryData:
    """One complete read, containing no account or network identifiers."""

    capabilities: dict[str, Capability] = field(default_factory=dict)
    values: dict[str, int | str | None] = field(default_factory=dict)
    songs: tuple[str, ...] | None = None


def parse_capabilities(parts: list[str]) -> dict[str, Capability]:
    """Validate count and five-field rows before selecting known capabilities."""
    try:
        count = int(parts[1])
        if parts[0] != "caplist" or not 0 <= count <= 256:
            raise ValueError
        if len(parts) != 2 + count * 5:
            raise ValueError
        result = {}
        seen = set()
        for index in range(2, len(parts), 5):
            key, access, kind, low, high = parts[index : index + 5]
            if key in seen:
                raise ValueError
            seen.add(key)
            if key not in READABLE_FIELDS | COMMAND_FIELDS:
                continue
            minimum, maximum = int(low), int(high)
            expected = "str" if key in STRING_FIELDS else "int"
            if access not in {"r", "w"} or kind != expected or minimum > maximum:
                raise ValueError
            result[key] = Capability(kind, minimum, maximum, access == "w")
        return result
    except (ValueError, IndexError):
        raise TelemetryError("Invalid capability response") from None


def parse_values(
    parts: list[str], capabilities: dict[str, Capability]
) -> dict[str, int | str | None]:
    """Reject incomplete responses; isolate invalid individual sensor values."""
    try:
        count = int(parts[1])
        if parts[0] != "get" or count != len(capabilities):
            raise ValueError
        if len(parts) != 2 + count * 2:
            raise ValueError
        raw = dict(zip(parts[2::2], parts[3::2], strict=True))
        if len(raw) != count or raw.keys() != capabilities.keys():
            raise ValueError
    except (ValueError, IndexError):
        raise TelemetryError("Invalid sensor response") from None

    return {
        key: _parse_value(key, value, capabilities[key]) for key, value in raw.items()
    }


def _parse_value(key: str, value: str, cap: Capability) -> int | str | None:
    """Validate one reading independently of the rest of the frame."""
    if cap.kind == "str":
        if key == "playing":
            return value if value == "(none)" or SONG_PATTERN.fullmatch(value) else None
        return value if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value) else None
    try:
        number = int(value)
        return number if cap.minimum <= number <= cap.maximum else None
    except ValueError:
        return None


def _matches_response(parts, expected, field, value):
    """Match the operation and optional field/value without indexing short frames."""
    if not parts or parts[0] != expected:
        return False
    if field is None:
        return True
    index = 2 if expected == "get" else 1
    return parts[index : index + 1] == [field] and (
        value is None or parts[index + 1 : index + 2] == [value]
    )


class TelemetryClient:
    """Use saved per-camera credentials without rotating the account session."""

    def __init__(self, host: str, context: ssl.SSLContext) -> None:
        self.host = vendor_host(host)
        self._context = context
        self._capabilities: dict[str, Capability] | None = None
        self._songs: tuple[str, ...] | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, message: str) -> None:
        writer.write(message.encode("ascii") + b"\n")
        await writer.drain()

    async def _exchange(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        message: str | None,
        expected: str,
        field: str | None = None,
        value: str | None = None,
    ) -> list[str]:
        """Match replies while servicing keepalives and bounding skipped frames."""
        if message is not None:
            await self._send(writer, message)
        for _ in range(64):
            line = await reader.readline()
            if not line or len(line) > MAX_LINE or not line.endswith(b"\n"):
                raise TelemetryError("Incomplete control response")
            parts = line.decode("ascii").split()
            if parts == ["ping"]:
                await self._send(writer, "pong")
            elif parts == ["pong"]:
                await self._send(writer, "pang")
            elif _matches_response(parts, expected, field, value):
                return parts
        raise TelemetryError("Too many unrelated control messages")

    @asynccontextmanager
    async def _session(self, credentials: Credentials):
        """Authenticate a bounded TLS control session and always close its writer."""
        owner = atom(str(credentials.sid_user_id))
        token = atom(credentials.magic_token.decode("ascii"))
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(TIMEOUT):
                reader, writer = await asyncio.open_connection(
                    self.host,
                    CONTROL_PORT,
                    ssl=self._context,
                    server_hostname=self.host,
                    limit=MAX_LINE,
                )
                auth = await self._exchange(
                    reader, writer, f"app {owner} {token}", "app"
                )
                if auth != ["app", "1", "OK"]:
                    raise TelemetryError("Camera telemetry authentication unavailable")
                yield reader, writer
        except TelemetryError:
            raise
        except (OSError, EOFError, ValueError):
            raise TelemetryError("Unable to communicate with camera") from None
        finally:
            if writer is not None:
                writer.close()
                with suppress(OSError):
                    async with asyncio.timeout(2):
                        await writer.wait_closed()

    async def _get_capabilities(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> dict[str, Capability]:
        caps = self._capabilities
        if caps is None:
            caps = parse_capabilities(
                await self._exchange(reader, writer, "caplist", "caplist")
            )
            self._capabilities = caps
        return caps

    @staticmethod
    def _require_write(caps, key, kind, value=None):
        cap = caps.get(key)
        if kind == "str" and value is not None:
            value = len(value)
        if (
            cap is None
            or not cap.writable
            or cap.kind != kind
            or (value is not None and not cap.minimum <= value <= cap.maximum)
        ):
            raise TelemetryError("Camera control is not supported")

    async def async_read(self, credentials: Credentials) -> TelemetryData:
        """Open one bounded TLS session and read only allowlisted properties."""
        async with self._lock:
            try:
                async with self._session(credentials) as (reader, writer):
                    caps = await self._get_capabilities(reader, writer)
                    if not caps:
                        self._capabilities = caps
                        return TelemetryData()
                    keys = sorted(caps.keys() & READABLE_FIELDS)
                    if not keys:
                        return TelemetryData(dict(caps), {})
                    parts = await self._exchange(
                        reader,
                        writer,
                        f"get {len(keys)} {' '.join(keys)}",
                        "get",
                    )
                    values = parse_values(parts, {key: caps[key] for key in keys})
                    self._capabilities = caps
                    return TelemetryData(dict(caps), values)
            except TelemetryError:
                self._capabilities = None
                self._songs = None
                raise

    async def async_set(self, credentials: Credentials, key: str, value: int) -> int:
        """Set one explicitly supported integer field and read it back."""
        if (
            key not in WRITABLE_FIELDS
            or isinstance(value, bool)
            or not isinstance(value, int)
        ):
            raise TelemetryError("Unsupported camera setting")
        if key in CONTROL_VALUES and value not in CONTROL_VALUES[key]:
            raise TelemetryError("Invalid camera setting value")
        async with self._lock:
            try:
                async with self._session(credentials) as (reader, writer):
                    caps = await self._get_capabilities(reader, writer)
                    cap = caps.get(key)
                    if (
                        cap is None
                        or not cap.writable
                        or cap.kind != "int"
                        or not cap.minimum <= value <= cap.maximum
                    ):
                        raise TelemetryError("Invalid camera setting value")
                    response = await self._exchange(
                        reader,
                        writer,
                        f"set {key} {value}",
                        "set",
                        key,
                        str(value),
                    )
                    if response != ["set", key, str(value)]:
                        raise TelemetryError("Camera setting was not confirmed")
                    result = parse_values(
                        await self._exchange(
                            reader, writer, f"get 1 {key}", "get", key
                        ),
                        {key: cap},
                    )[key]
                    if result != value:
                        raise TelemetryError("Camera setting was not confirmed")
                    self._capabilities = caps
                    return result
            except TelemetryError:
                self._capabilities = None
                self._songs = None
                raise

    async def async_list_songs(self, credentials: Credentials) -> tuple[str, ...]:
        """Return the validated built-in playlist advertised by the camera."""
        if self._songs is not None:
            return self._songs
        async with self._lock, self._session(credentials) as (reader, writer):
            caps = await self._get_capabilities(reader, writer)
            if "existsongs" not in caps:
                raise TelemetryError("Camera playlist is not supported")
            response = await self._exchange(
                reader, writer, "set existsongs 1", "set", "existsongs"
            )
            payload = " ".join(response[2:])
            songs = tuple(item for item in payload.split(",") if item)
            if (
                not 1 <= len(songs) <= 64
                or len(set(songs)) != len(songs)
                or any(not SONG_PATTERN.fullmatch(song) for song in songs)
            ):
                raise PlaylistValidationError("Invalid camera playlist response")
            self._songs = songs
            return songs

    async def async_play(self, credentials: Credentials, song: str) -> None:
        """Play one camera-advertised built-in lullaby."""
        if song not in await self.async_list_songs(credentials):
            raise TelemetryError("Unknown camera lullaby")
        async with self._lock, self._session(credentials) as (reader, writer):
            caps = await self._get_capabilities(reader, writer)
            self._require_write(caps, "playing", "str", song)
            if self._songs is None or song not in self._songs:
                raise TelemetryError("Camera playlist unavailable")
            response = await self._exchange(
                reader,
                writer,
                f"set playing {song}",
                "set",
                "playing",
                song,
            )
            if response != ["set", "playing", song]:
                raise TelemetryError("Lullaby playback was not confirmed")

    async def async_stop(self, credentials: Credentials) -> None:
        """Stop camera-side lullaby playback."""
        async with self._lock, self._session(credentials) as (reader, writer):
            caps = await self._get_capabilities(reader, writer)
            self._require_write(caps, "audio_control", "int", 0)
            response = await self._exchange(
                reader,
                writer,
                "set audio_control 0",
                "set",
                "audio_control",
                "0",
            )
            if response != ["set", "audio_control", "0"]:
                raise TelemetryError("Lullaby stop was not confirmed")

    async def async_ptz(
        self,
        credentials: Credentials,
        axis: str,
        direction: str,
        duration: float = 0.4,
    ) -> None:
        """Move briefly with an unconditional stop, or return to origin."""
        allowed = {"pan": {"left", "right"}, "tilt": {"up", "down"}}
        if axis == "ptz" and direction == "origin":
            async with self._lock, self._session(credentials) as (reader, writer):
                caps = await self._get_capabilities(reader, writer)
                self._require_write(caps, "ptz", "str", "origin")
                response = await self._exchange(
                    reader, writer, "set ptz origin", "set", "ptz"
                )
                if response != ["set", "ptz", "origin"]:
                    raise TelemetryError("PTZ origin was not confirmed")
            return
        if direction not in allowed.get(axis, set()) or not 0.1 <= duration <= 2:
            raise TelemetryError("Unsupported PTZ command")
        async with self._lock, self._session(credentials) as (reader, writer):
            caps = await self._get_capabilities(reader, writer)
            self._require_write(caps, axis, "str", direction)
            self._require_write(caps, axis, "str", "stop")
            try:
                response = await self._exchange(
                    reader, writer, f"set {axis} {direction}", "set", axis
                )
                if response != ["set", axis, direction]:
                    raise TelemetryError("PTZ direction was not confirmed")
                await asyncio.sleep(duration)
            finally:
                await self._send(writer, f"set {axis} stop")
            response = await self._exchange(reader, writer, None, "set", axis, "stop")
            if response[:3] != ["set", axis, "stop"]:
                raise TelemetryError("PTZ stop was not confirmed")
