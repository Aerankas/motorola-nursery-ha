"""Real-time motion and sound notifications from the camera control channel."""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .auth import credentials_from_data
from .cloud import CloudError, atom, vendor_host
from .const import CONF_SESSION, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONTROL_PORT = 2288
CONNECT_TIMEOUT = 15
MAX_LINE = 65536
EVENT_TYPES = {
    "motiondetect": "motion",
    "sounddetect": "sound",
    "sounddetect2": "sound",
}


@dataclass(frozen=True, slots=True)
class CameraEvent:
    """A privacy-minimized detection notification."""

    event_type: str
    detected_at: datetime


def parse_notification(parts: list[str], token: str) -> str | None:
    """Select genuine detection notifications for this camera only."""
    if (
        len(parts) != 4
        or parts[0] != "notify"
        or parts[1] != token
        or not 1 <= len(parts[3]) <= 1024
    ):
        return None
    return EVENT_TYPES.get(parts[2])


class CameraEventListener:
    """Maintain one authenticated TLS socket and reconnect when it drops."""

    def __init__(
        self,
        entry: ConfigEntry,
        context: ssl.SSLContext,
        event_callback: Callable[[CameraEvent], None],
        connection_callback: Callable[[bool], None],
    ) -> None:
        self._entry = entry
        self._context = context
        self._event_callback = event_callback
        self._connection_callback = connection_callback
        self._task: asyncio.Task[None] | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._seen: deque[tuple[str, str]] = deque(maxlen=128)
        self._retry_delay = 1

    async def _send(self, writer: asyncio.StreamWriter, message: str) -> None:
        writer.write(message.encode("ascii") + b"\n")
        await writer.drain()

    async def _read(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> list[str]:
        while True:
            async with asyncio.timeout(90):
                line = await reader.readline()
            if not line or len(line) > MAX_LINE or not line.endswith(b"\n"):
                raise OSError("Incomplete camera event response")
            try:
                parts = line.decode("ascii").split()
            except UnicodeDecodeError:
                raise OSError("Invalid camera event response") from None
            if parts == ["ping"]:
                await self._send(writer, "pong")
                continue
            if parts == ["pong"]:
                await self._send(writer, "pang")
                continue
            return parts

    async def _listen_once(self) -> None:
        """Authenticate one detection channel and release its writer on every exit."""
        data = self._entry.data
        credentials = credentials_from_data(data)
        host = vendor_host(data[CONF_SESSION]["host"])
        token = atom(credentials.magic_token.decode("ascii"))
        owner = atom(str(credentials.sid_user_id))
        writer: asyncio.StreamWriter | None = None
        try:
            # Bound both connection setup and authentication, even if the server
            # keeps sending keepalives without ever accepting the camera.
            async with asyncio.timeout(CONNECT_TIMEOUT):
                reader, writer = await asyncio.open_connection(
                    host,
                    CONTROL_PORT,
                    ssl=self._context,
                    server_hostname=host,
                    limit=MAX_LINE,
                )
                self._writer = writer
                await self._send(writer, f"app {owner} {token}")
                for _ in range(64):
                    response = await self._read(reader, writer)
                    if response[:1] == ["app"]:
                        if response != ["app", "1", "OK"]:
                            raise OSError("Camera event authentication unavailable")
                        break
                else:
                    raise OSError("Camera event authentication unavailable")
            self._retry_delay = 1
            self._connection_callback(True)
            while True:
                parts = await self._read(reader, writer)
                event_type = parse_notification(parts, token)
                fingerprint = (parts[2], parts[3]) if event_type is not None else None
                if fingerprint is not None and fingerprint not in self._seen:
                    self._seen.append(fingerprint)
                    self._event_callback(CameraEvent(event_type, datetime.now(UTC)))
        finally:
            self._connection_callback(False)
            self._writer = None
            if writer is not None:
                writer.close()
                with suppress(OSError):
                    async with asyncio.timeout(2):
                        await writer.wait_closed()

    async def _run(self) -> None:
        """Reconnect detections with backoff while propagating cancellation."""
        while True:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                raise
            except (
                CloudError,
                KeyError,
                OSError,
                RuntimeError,
                ValueError,
            ):
                self._connection_callback(False)
                _LOGGER.debug("Camera event channel unavailable; reconnecting")
                await asyncio.sleep(self._retry_delay)
                self._retry_delay = min(self._retry_delay * 2, 60)

    def start(self) -> None:
        if self._task is None:
            self._retry_delay = 1
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        if self._writer is not None:
            self._writer.close()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None


class MotorolaEventManager:
    """Publish detections to entities and the Home Assistant event bus."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        context: ssl.SSLContext | None,
        client_factory=None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.connected = False
        self.latest: dict[str, datetime] = {}
        self._listeners: set[Callable[[CameraEvent | None], None]] = set()
        self._client = (client_factory or CameraEventListener)(
            entry, context, self._on_event, self._on_connection
        )

    def start(self) -> None:
        self._client.start()

    async def stop(self) -> None:
        await self._client.stop()

    @callback
    def async_add_listener(
        self, listener: Callable[[CameraEvent | None], None]
    ) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _notify(self, event: CameraEvent | None) -> None:
        for listener in tuple(self._listeners):
            listener(event)

    @callback
    def _on_connection(self, connected: bool) -> None:
        if self.connected != connected:
            self.connected = connected
            self.hass.loop.call_soon_threadsafe(self._notify, None)

    @callback
    def _on_event(self, event: CameraEvent) -> None:
        self.latest[event.event_type] = event.detected_at
        identifier = self.entry.unique_id or self.entry.entry_id
        self.hass.loop.call_soon_threadsafe(
            self.hass.bus.async_fire,
            f"{DOMAIN}_event",
            {
                "device_id": identifier,
                "event_type": event.event_type,
                "detected_at": event.detected_at.isoformat(),
            },
        )
        self.hass.loop.call_soon_threadsafe(self._notify, event)
