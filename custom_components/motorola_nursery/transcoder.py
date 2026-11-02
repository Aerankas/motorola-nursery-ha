"""On-demand audio transcoding for Home Assistant's native stream worker."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Callable


class FfmpegRelay:
    """Serve an FFmpeg MPEG-TS stream on a private loopback endpoint."""

    def __init__(
        self,
        ffmpeg_binary: str,
        input_url: str,
        on_process_error: Callable[[int], None],
    ) -> None:
        self._ffmpeg_binary = ffmpeg_binary
        self._input_url = input_url
        self._on_process_error = on_process_error
        self._path = f"/{secrets.token_urlsafe(24)}.ts"
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._processes: set[asyncio.subprocess.Process] = set()

    @property
    def url(self) -> str:
        """Return the private MPEG-TS URL consumed by Home Assistant."""
        if self._server is None or not self._server.sockets:
            raise RuntimeError("FFmpeg relay has not been started")
        port = int(self._server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{port}{self._path}"

    @property
    def active_processes(self) -> int:
        """Return the number of running transcoders."""
        return len(self._processes)

    def update_input_url(self, input_url: str) -> None:
        """Use a refreshed local source when the next FFmpeg process starts."""
        self._input_url = input_url

    async def start(self) -> None:
        """Start the private HTTP listener on an ephemeral port."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._accept, "127.0.0.1", 0, limit=16 * 1024
        )

    async def stop(self) -> None:
        """Stop accepting streams and terminate every active transcoder."""
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

        processes = tuple(self._processes)
        for process in processes:
            await self._stop_process(process)

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve a validated stream request and clean up its process and socket."""
        task = asyncio.current_task()
        if task is None:
            writer.close()
            await writer.wait_closed()
            raise RuntimeError("Relay connection has no owning task")
        self._tasks.add(task)
        process: asyncio.subprocess.Process | None = None
        try:
            if not await self._valid_request(reader):
                writer.write(
                    b"HTTP/1.1 404 Not Found\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await writer.drain()
                return

            process = await asyncio.create_subprocess_exec(
                self._ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-rtsp_transport",
                "tcp",
                "-i",
                self._input_url,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "48k",
                "-f",
                "mpegts",
                "-mpegts_flags",
                "+resend_headers",
                "-muxdelay",
                "0",
                "pipe:1",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._processes.add(process)
            if process.stdout is None:
                raise RuntimeError("FFmpeg output pipe is unavailable")

            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: video/MP2T\r\n"
                b"Cache-Control: no-store\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()

            while data := await process.stdout.read(64 * 1024):
                writer.write(data)
                await writer.drain()

            return_code = await process.wait()
            if return_code != 0:
                self._on_process_error(return_code)
        except asyncio.CancelledError:
            raise
        except OSError:
            pass
        finally:
            if process is not None:
                await self._stop_process(process)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self._tasks.discard(task)

    async def _valid_request(self, reader: asyncio.StreamReader) -> bool:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return False
        request_line = request.split(b"\r\n", 1)[0]
        return request_line == f"GET {self._path} HTTP/1.1".encode()

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        self._processes.discard(process)
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
