"""Loopback-only RTSP bridge for Motorola Nursery cameras."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from .protocol import Credentials, MagicP2PTunnel


class RtspBridge:
    """Expose MagicP2P as conventional RTSP on a loopback socket."""

    def __init__(
        self,
        camera_host: str,
        credentials: Credentials,
        on_connection_error: Callable[[Exception], None],
    ) -> None:
        self._camera_host = camera_host
        self._credentials = credentials
        self._on_connection_error = on_connection_error
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._slots = asyncio.Semaphore(4)

    @property
    def port(self) -> int:
        """Return the selected loopback port."""
        if self._server is None or not self._server.sockets:
            raise RuntimeError("RTSP bridge has not been started")
        return int(self._server.sockets[0].getsockname()[1])

    def update_credentials(self, credentials: Credentials) -> None:
        """Apply renewed credentials to new tunnels without touching active ones."""
        self._credentials = credentials

    @property
    def active_connections(self) -> int:
        """Return the number of accepted local clients, without their addresses."""
        return len(self._tasks)

    async def start(self) -> None:
        """Start the listener on an ephemeral loopback port."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._accept, "127.0.0.1", 0, limit=256 * 1024
        )

    async def stop(self) -> None:
        """Stop accepting clients and close active tunnels."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            server.close_clients()
            await server.wait_closed()

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is None:
            writer.close()
            await writer.wait_closed()
            raise RuntimeError("Bridge connection has no owning task")
        self._tasks.add(task)
        try:
            async with self._slots:
                await self._forward(reader, writer)
        finally:
            self._tasks.discard(task)

    async def _forward(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Forward both directions and release the tunnel after either side closes."""
        tunnel: MagicP2PTunnel | None = None
        directions: set[asyncio.Task] = set()
        try:
            tunnel = await MagicP2PTunnel.connect(self._camera_host, self._credentials)

            async def client_to_camera() -> None:
                while data := await reader.read(65536):
                    await tunnel.send(data)

            async def camera_to_client() -> None:
                while data := await tunnel.receive():
                    writer.write(data)
                    await writer.drain()
                raise EOFError("Camera tunnel closed")

            directions = {
                asyncio.create_task(client_to_camera()),
                asyncio.create_task(camera_to_client()),
            }
            done, pending = await asyncio.wait(
                directions, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
            # Cleanup must not consume failures that trigger credential recovery.
            for completed in done:
                completed.result()
        except asyncio.CancelledError:
            raise
        except (EOFError, OSError) as err:
            self._on_connection_error(err)
        finally:
            for direction in directions:
                direction.cancel()
            if directions:
                await asyncio.gather(*directions, return_exceptions=True)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            if tunnel is not None:
                with contextlib.suppress(OSError):
                    await tunnel.close()
