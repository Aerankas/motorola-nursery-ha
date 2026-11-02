"""Motorola Nursery integration."""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import quote

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from .auth import async_refresh_credentials, credentials_from_data
from .bridge import RtspBridge
from .cloud import AuthenticationError, CloudError
from .const import CONF_LOCAL_CONTROL, CONF_SESSION, DOMAIN, PLATFORMS
from .coordinator import MotorolaTelemetryCoordinator
from .entity import async_remove_setting_mirrors
from .events import MotorolaEventManager
from .local import LocalControlServer, LocalEventListener, LocalTelemetryClient
from .protocol import Credentials, MagicP2PTunnel
from .telemetry import TelemetryClient
from .transcoder import FfmpegRelay

_LOGGER = logging.getLogger(__name__)


def stream_url(credentials: Credentials, port: int) -> str:
    user = quote(credentials.rtsp_username, safe="")
    password = quote(credentials.rtsp_password, safe="")
    token = quote(credentials.access_token, safe="")
    return (
        f"rtsp://{user}:{password}@127.0.0.1:{port}/owner/streaming?accessToken={token}"
    )


@dataclass(slots=True)
class RuntimeData:
    """Local playback with serialized, on-demand cloud credential recovery."""

    hass: HomeAssistant
    entry: ConfigEntry
    bridge: RtspBridge
    relay: FfmpegRelay
    credentials: Credentials = field(repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_check: float = 0
    next_refresh: float = 0
    recovery_task: asyncio.Task | None = None
    stopping: bool = False
    telemetry: MotorolaTelemetryCoordinator | None = None
    telemetry_task: asyncio.Task[None] | None = None
    events: MotorolaEventManager | None = None
    local_server: LocalControlServer | None = None

    async def async_prepare_stream(self, *, force: bool = False) -> None:
        """Probe local video and recover account credentials with bounded retries."""
        # Existing imported entries remain fully compatible and local.
        if CONF_SESSION not in self.entry.data or self.entry.data.get(
            CONF_LOCAL_CONTROL, False
        ):
            return
        async with self.lock:
            now = time.monotonic()
            if not force and now < self.next_check:
                return
            try:
                tunnel = await MagicP2PTunnel.connect(
                    self.entry.data["host"], self.credentials
                )
                await tunnel.close()
            except (OSError, EOFError):
                if now < self.next_refresh:
                    raise HomeAssistantError("Camera connection unavailable") from None
                # Bound cloud attempts when the actual problem is Wi-Fi/power.
                self.next_refresh = now + 60
                try:
                    credentials = await async_refresh_credentials(self.hass, self.entry)
                except AuthenticationError:
                    self.entry.async_start_reauth(self.hass)
                    raise HomeAssistantError(
                        "Motorola account needs authentication"
                    ) from None
                except (OSError, EOFError, CloudError):
                    raise HomeAssistantError(
                        "Unable to refresh camera credentials"
                    ) from None
                self.credentials = credentials
                self.bridge.update_credentials(credentials)
                self.relay.update_input_url(stream_url(credentials, self.bridge.port))
                try:
                    tunnel = await MagicP2PTunnel.connect(
                        self.entry.data["host"], credentials
                    )
                    await tunnel.close()
                except (OSError, EOFError):
                    raise HomeAssistantError("Camera connection unavailable") from None
            self.next_check = time.monotonic() + 60

    def recover_after_tunnel_error(self) -> None:
        """Also recover when HA reuses an already-created stream source."""
        if self.stopping or CONF_SESSION not in self.entry.data:
            return
        if self.recovery_task is None or self.recovery_task.done():
            self.recovery_task = self.hass.async_create_task(self._async_recover())

    async def _async_recover(self) -> None:
        try:
            await self.async_prepare_stream(force=True)
        except HomeAssistantError:
            _LOGGER.debug("Local camera recovery deferred")

    async def async_stop(self) -> None:
        self.stopping = True
        if self.telemetry_task is not None:
            self.telemetry_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.telemetry_task
        if self.telemetry is not None:
            await self.telemetry.async_shutdown()
        if self.events is not None:
            await self.events.stop()
        if self.local_server is not None:
            await self.local_server.unregister(self.entry.entry_id)
        if self.recovery_task is not None:
            self.recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.recovery_task
        await self.relay.stop()
        await self.bridge.stop()


def credentials_from_entry(entry: ConfigEntry) -> Credentials:
    credentials = credentials_from_data(entry.data)
    credentials.validate()
    return credentials


async def _async_setup_telemetry(hass, entry, runtime):
    """Configure exactly one control transport without delaying local video."""
    if entry.data.get(CONF_LOCAL_CONTROL, False):
        server = hass.data.setdefault(DOMAIN, {}).setdefault(
            "local_control_server", LocalControlServer()
        )
        runtime.local_server = server
        try:
            registration = await server.register(
                entry.entry_id, lambda: credentials_from_data(entry.data)
            )
        except OSError:
            raise ConfigEntryNotReady("Local control listener unavailable") from None
        client = LocalTelemetryClient(registration)
        runtime.telemetry = MotorolaTelemetryCoordinator(hass, entry, client)

        def local_connection_changed(connected):
            """Refresh recovered peers and expose disconnects without cloud fallback."""
            if runtime.stopping:
                return
            if connected:
                entry.async_create_background_task(
                    hass,
                    runtime.telemetry.async_request_refresh(),
                    "Motorola local telemetry connected",
                )
            else:
                runtime.telemetry.async_set_update_error(
                    UpdateFailed("Local camera control unavailable")
                )

        registration.connection_callbacks.add(local_connection_changed)
        runtime.events = MotorolaEventManager(
            hass,
            entry,
            None,
            lambda _entry, _context, on_event, on_connection: LocalEventListener(
                registration, on_event, on_connection
            ),
        )
    elif CONF_SESSION in entry.data:
        context = await hass.async_add_executor_job(ssl.create_default_context)
        client = TelemetryClient(entry.data[CONF_SESSION]["host"], context)
        runtime.telemetry = MotorolaTelemetryCoordinator(hass, entry, client)
        runtime.events = MotorolaEventManager(hass, entry, context)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start locally using cached credentials; cloud access is demand-driven."""
    credentials = credentials_from_entry(entry)
    runtime: RuntimeData | None = None

    def log_connection_error(err: Exception) -> None:
        _LOGGER.debug("Local camera tunnel closed: %s", type(err).__name__)
        if runtime is not None:
            runtime.recover_after_tunnel_error()

    bridge = RtspBridge(entry.data["host"], credentials, log_connection_error)
    await bridge.start()

    def log_process_error(return_code: int) -> None:
        _LOGGER.debug("Local audio transcoder exited with code %d", return_code)

    relay = FfmpegRelay(
        get_ffmpeg_manager(hass).binary,
        stream_url(credentials, bridge.port),
        log_process_error,
    )
    try:
        await relay.start()
        runtime = RuntimeData(hass, entry, bridge, relay, credentials)
        entry.runtime_data = runtime
        await _async_setup_telemetry(hass, entry, runtime)
        async_remove_setting_mirrors(hass, entry)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        # Register the camera and delayed-discovery listeners before any cloud
        # request. Background refresh cannot hold up HA or local video startup.
        if runtime.telemetry is not None:
            runtime.telemetry_task = entry.async_create_background_task(
                hass, runtime.telemetry.async_refresh(), "Motorola initial telemetry"
            )
        if runtime.events is not None:
            runtime.events.start()
    except BaseException:
        if runtime is not None:
            await runtime.async_stop()
        else:
            await relay.stop()
            await bridge.stop()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_stop()
    return True
