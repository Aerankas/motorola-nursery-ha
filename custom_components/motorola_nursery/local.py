"""Authenticated incoming camera connections; no cloud fallback or proxy."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .events import CameraEvent, parse_notification
from .f3 import F3Connection
from .protocol import Credentials
from .telemetry import PlaylistValidationError, TelemetryClient, TelemetryError

_LOGGER = logging.getLogger(__name__)
LOCAL_PORT = 2278
TIMEOUT = 20
INITIALIZATION_TIMEOUT = 30
SHUTDOWN_TIMEOUT = 5
DISCONNECTED = "Local control disconnected"


@dataclass(repr=False)
class Registration:
    credentials: Callable[[], Credentials]
    peer: LocalPeer | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    connection_callbacks: set = field(default_factory=set)
    event_callbacks: set = field(default_factory=set)
    seen: deque = field(default_factory=lambda: deque(maxlen=128))

    def connection_changed(self, connected: bool) -> None:
        for callback in tuple(self.connection_callbacks):
            callback(connected)


class LocalPeer:
    """Multiplex commands, keepalives and unsolicited camera detections."""

    def __init__(self, connection: F3Connection, registration: Registration):
        self.connection = connection
        self.registration = registration
        self.responses: asyncio.Queue[bytes | None] = asyncio.Queue(64)
        self.command_lock = asyncio.Lock()
        self.request_pending = False
        self.closed = False

    async def send(self, data: bytes) -> None:
        """Reject sends after closure before touching the encrypted connection."""
        if self.closed:
            raise EOFError(DISCONNECTED)
        await self.connection.send(data)

    async def readline(self) -> bytes:
        """Deliver queued replies or propagate the peer shutdown sentinel."""
        line = await self.responses.get()
        if line is None:
            raise EOFError(DISCONNECTED)
        return line

    def close(self) -> None:
        self.closed = True
        self.connection.close()
        while not self.responses.empty():
            self.responses.get_nowait()
        self.responses.put_nowait(None)

    async def run(self) -> None:
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            while True:
                async with asyncio.timeout(45):
                    line = await self.connection.readline()
                await self.handle_line(line)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError, OSError):
                await heartbeat
            self.close()

    async def handle_line(self, line: bytes) -> None:
        """Dispatch one authenticated camera frame, including during setup."""
        parts = line.decode("ascii").split()
        if not parts:
            return
        _LOGGER.debug(
            "Local camera message: kind=%s fields=%d pending=%s",
            parts[0]
            if parts[0]
            in {"ping", "pong", "get", "caplist", "notify", "dev", "magicp2p"}
            else "other",
            len(parts),
            self.request_pending,
        )
        if parts == ["ping"]:
            await self.send(b"pong\n")
        elif parts[0] == "pong":
            return
        elif parts[0] == "notify":
            await self._handle_notification(parts)
        elif self.request_pending and parts[0] in ("caplist", "get", "set"):
            self.responses.put_nowait(line)

    async def _handle_notification(self, parts: list[str]) -> None:
        """Acknowledge authenticated events and deduplicate them across reconnects."""
        credentials = self.registration.credentials()
        # Camera-originated notifications may append an opaque upload hint.
        # The app-facing channel uses four fields. Never expose this hint.
        notification = parts
        if len(parts) == 5 and 1 <= len(parts[4]) <= 1024:
            notification = parts[:4]
        event_type = parse_notification(
            notification, credentials.magic_token.decode("ascii")
        )
        if len(notification) == 4 and hmac.compare_digest(
            notification[1].encode(), credentials.magic_token
        ):
            await self.send(b"notify 1 1\n")
        if event_type is None:
            return
        fingerprint = hashlib.sha256((parts[2] + "\0" + parts[3]).encode()).digest()
        if fingerprint in self.registration.seen:
            return
        self.registration.seen.append(fingerprint)
        event = CameraEvent(event_type, datetime.now(UTC))
        for callback in tuple(self.registration.event_callbacks):
            callback(event)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                await self.send(b"ping\n")
            except (OSError, EOFError):
                self.close()
                return


class LocalControlServer:
    """One listener shared by entries, matching owner and per-camera token."""

    def __init__(self, port: int = LOCAL_PORT):
        self.port = port
        self._server = None
        self._entries: dict[str, Registration] = {}
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    async def register(self, identifier: str, credentials) -> Registration:
        async with self._lock:
            if self._server is None:
                self._server = await asyncio.start_server(
                    # The authenticated device connects from its LAN interface.
                    self._accept,
                    "0.0.0.0",
                    self.port,
                    limit=65536,  # nosec B104
                )
                self.port = self._server.sockets[0].getsockname()[1]
            registration = Registration(credentials)
            self._entries[identifier] = registration
            return registration

    async def unregister(self, identifier: str) -> None:
        async with self._lock:
            registration = self._entries.pop(identifier, None)
            if registration and registration.peer:
                registration.peer.close()
            if not self._entries:
                await self._stop()

    async def _stop(self) -> None:
        server, self._server = self._server, None
        if server:
            server.close()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server:
            try:
                async with asyncio.timeout(SHUTDOWN_TIMEOUT):
                    await server.wait_closed()
            except TimeoutError:
                # Closing can stall while flushing buffered data to a peer.
                server.abort_clients()

    def _accept(self, reader, writer) -> None:
        if not self._entries or len(self._tasks) >= len(self._entries) + 8:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _identify(self, parts: list[str]) -> Registration:
        if len(parts) != 4 or parts[0] != "dev" or len(parts[1]) > 10:
            raise ValueError("Invalid local camera registration")
        matches = []
        for registration in self._entries.values():
            credentials = registration.credentials()
            if hmac.compare_digest(parts[1], str(credentials.sid_user_id)) and (
                hmac.compare_digest(parts[2].encode(), credentials.magic_token)
            ):
                matches.append(registration)
        if len(matches) != 1:
            raise ValueError(
                "Unknown local camera" if not matches else "Ambiguous local camera"
            )
        return matches[0]

    async def _handle(self, reader, writer) -> None:
        """Authenticate one camera and retire only its own active registration."""
        registration = peer = None
        stage = "handshake"
        try:
            connection = await F3Connection.accept(reader, writer)
            stage = "registration"
            async with asyncio.timeout(INITIALIZATION_TIMEOUT):
                registration = self._identify(
                    (await connection.readline()).decode("ascii").split()
                )
                credentials = registration.credentials()
                credentials.validate()
                # BusyBox date syntax used by the device's registration handler.
                clock = datetime.now(UTC).strftime("%m%d%H%M%Y.%S")
                device = credentials.sid_device.decode("ascii")
                stage = "initialization"
                await connection.send(
                    f"dev 2 {clock}\nmagicp2p 1 {device} {device}\n".encode()
                )
                peer = LocalPeer(connection, registration)
                # The camera acknowledges after installing its command handlers.
                # Querying properties earlier can lose the first getter.
                for _ in range(64):
                    line = await connection.readline()
                    parts = line.decode("ascii").split()
                    if parts == ["magicp2p", "1"]:
                        break
                    if parts[:1] == ["magicp2p"]:
                        raise ValueError("Local camera initialization rejected")
                    await peer.handle_line(line)
                else:
                    raise ValueError("Local camera initialization unavailable")
                if registration.peer:
                    registration.peer.close()
                registration.peer = peer
                registration.ready.set()
                registration.connection_changed(True)
            stage = "connected"
            await peer.run()
        except asyncio.CancelledError:
            raise
        except (
            OSError,
            EOFError,
            ValueError,
            RuntimeError,
            asyncio.QueueFull,
        ) as error:
            _LOGGER.debug(
                "Local camera control closed: stage=%s category=%s",
                stage,
                type(error).__name__,
            )
        finally:
            if peer is not None and not peer.closed:
                peer.close()
            writer.close()
            with suppress(OSError):
                async with asyncio.timeout(2):
                    await writer.wait_closed()
            if registration is not None and registration.peer is peer:
                registration.peer = None
                registration.ready.clear()
                registration.connection_changed(False)


class LocalWriter:
    """Adapt the existing allowlisted telemetry parser to a persistent socket."""

    def __init__(self, peer: LocalPeer):
        self.peer = peer
        self.pending = bytearray()

    def write(self, data: bytes) -> None:
        self.pending.extend(data)

    async def drain(self) -> None:
        data = bytes(self.pending)
        self.pending.clear()
        await self.peer.send(data)


class LocalTelemetryClient(TelemetryClient):
    """The local mode cannot instantiate a cloud control session."""

    def __init__(self, registration: Registration):
        # Share allowlisted commands and caches; replace only the session transport.
        self.registration = registration
        self._capabilities = None
        self._songs = None
        self._last_peer = None
        self._lock = asyncio.Lock()

    async def async_list_songs(self, credentials):
        """Invalidate the previous peer's playlist before any cache fast path."""
        if self.registration.peer is not self._last_peer:
            self._songs = None
        return await super().async_list_songs(credentials)

    @asynccontextmanager
    async def _session(self, credentials):
        """Serialize local commands and discard sessions with uncertain framing."""
        peer = None
        try:
            async with asyncio.timeout(TIMEOUT):
                await self.registration.ready.wait()
            # A first poll includes both capability discovery and value reads.
            async with asyncio.timeout(2 * TIMEOUT):
                peer = self.registration.peer
                if peer is None or peer.closed:
                    raise EOFError(DISCONNECTED)
                async with peer.command_lock:
                    if peer is not self._last_peer:
                        self._capabilities = None
                        self._songs = None
                        self._last_peer = peer
                    while not peer.responses.empty():
                        peer.responses.get_nowait()
                    peer.request_pending = True
                    try:
                        yield peer, LocalWriter(peer)
                    finally:
                        peer.request_pending = False
        except PlaylistValidationError:
            # The reply was fully consumed; telemetry/events can reuse this peer.
            raise
        except asyncio.CancelledError:
            if peer:
                peer.close()
            raise
        except (OSError, EOFError, ValueError, TelemetryError):
            if peer:
                peer.close()
            raise TelemetryError("Local camera control unavailable") from None


class LocalEventListener:
    """Bind existing event entities to the authenticated LAN connection."""

    def __init__(self, registration, event_callback, connection_callback):
        self.registration = registration
        self.event_callback = event_callback
        self.connection_callback = connection_callback

    def start(self):
        self.registration.event_callbacks.add(self.event_callback)
        self.registration.connection_callbacks.add(self.connection_callback)
        self.connection_callback(bool(self.registration.peer))

    async def stop(self):
        self.registration.event_callbacks.discard(self.event_callback)
        self.registration.connection_callbacks.discard(self.connection_callback)
        self.connection_callback(False)
