"""Loopback-only RTSP bridge for Motorola Nursery cameras."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import struct
from collections.abc import Callable

from .protocol import (
    INTERNAL_TALKBACK_PORT,
    Credentials,
    MagicP2PTunnel,
)

TALKBACK_CHUNK_BYTES = 640
TALKBACK_IDLE_TIMEOUT = 1.5

_STREAMING_PATH_RE = re.compile(
    rb"/owner/streaming(?:\?accessToken=[^ \r\n]+)?(?= RTSP/1\.0)"
)

_TALKBACK_SDP = (
    b"v=0\r\n"
    b"o=- 1 1 IN IP4 127.0.0.1\r\n"
    b"s=talkback\r\n"
    b"c=IN IP4 127.0.0.1\r\n"
    b"t=0 0\r\n"
    b"a=control:*\r\n"
    b"m=audio 0 RTP/AVP 8\r\n"
    b"a=rtpmap:8 PCMA/8000/1\r\n"
    b"a=sendonly\r\n"
    b"a=control:track0\r\n"
)


def _alaw_byte_to_s16(a_val: int) -> int:
    """Convert one ITU-T G.711 A-law byte to a signed 16-bit linear PCM sample."""
    a_val ^= 0x55
    linear = (a_val & 0x0F) << 4
    seg = (a_val & 0x70) >> 4
    if seg == 0:
        linear += 8
    elif seg == 1:
        linear += 0x108
    else:
        linear += 0x108
        linear <<= seg - 1
    return linear if (a_val & 0x80) else -linear


_ALAW_TO_S16LE: tuple[bytes, ...] = tuple(
    struct.pack("<h", _alaw_byte_to_s16(i)) for i in range(256)
)


def decode_alaw_to_s16le(payload: bytes) -> bytes:
    """Decode G.711 A-law bytes into 16-bit signed little-endian PCM (s16le)."""
    return b"".join(_ALAW_TO_S16LE[b] for b in payload)


def extract_rtp_payload(packet: bytes) -> bytes:
    """Strip the RTP header, CSRC list, header extension, and padding."""
    if len(packet) <= 12:
        return b""
    first_byte = packet[0]
    if (first_byte >> 6) != 2:
        return b""
    csrc_count = first_byte & 0x0F
    has_extension = bool(first_byte & 0x10)
    has_padding = bool(first_byte & 0x20)
    offset = 12 + csrc_count * 4
    if has_extension:
        if len(packet) < offset + 4:
            return b""
        ext_words = int.from_bytes(packet[offset + 2 : offset + 4], "big")
        offset += 4 + ext_words * 4
    end = len(packet)
    if has_padding and end > offset:
        pad_len = packet[-1]
        if 0 < pad_len <= end - offset:
            end -= pad_len
    if offset >= end:
        return b""
    return packet[offset:end]


def default_rtsp_port(camera_host: str) -> int:
    """Return 8500 + IPv4 final octet (when 1..254), or 8554 otherwise."""
    with contextlib.suppress(ipaddress.AddressValueError):
        last_octet = ipaddress.IPv4Address(camera_host).packed[3]
        if 1 <= last_octet <= 254:
            return 8500 + last_octet
    return 8554


class RtspBridge:
    """Expose MagicP2P as conventional RTSP on a loopback socket."""

    def __init__(
        self,
        camera_host: str,
        credentials: Credentials,
        on_connection_error: Callable[[Exception], None],
        *,
        port: int = 0,
    ) -> None:
        self._camera_host = camera_host
        self._credentials = credentials
        self._on_connection_error = on_connection_error
        self._preferred_port = port
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
        """Start the listener on an ephemeral or configured loopback port."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._accept,
            "127.0.0.1",
            self._preferred_port,
            limit=256 * 1024,
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
            initial = bytearray()
            with contextlib.suppress(TimeoutError, OSError):
                async with asyncio.timeout(0.05):
                    chunk = await reader.read(4096)
                    if chunk:
                        initial.extend(chunk)
            first_line = bytes(initial.split(b"\r\n", 1)[0])
            if b"/talkback" in first_line:
                await self._handle_talkback(reader, writer, bytes(initial))
            else:
                async with self._slots:
                    await self._forward(reader, writer, bytes(initial))
        finally:
            self._tasks.discard(task)

    async def _forward(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        initial_data: bytes = b"",
    ) -> None:
        """Forward both directions and release the tunnel after either side closes."""
        tunnel: MagicP2PTunnel | None = None
        directions: set[asyncio.Task] = set()
        try:
            tunnel = await MagicP2PTunnel.connect(self._camera_host, self._credentials)

            def _inject_token(payload: bytes) -> bytes:
                if b"/owner/streaming" in payload and self._credentials.access_token:
                    token_bytes = self._credentials.access_token.encode("ascii")
                    return _STREAMING_PATH_RE.sub(
                        b"/owner/streaming?accessToken=" + token_bytes,
                        payload,
                    )
                return payload

            async def client_to_camera() -> None:
                if initial_data:
                    await tunnel.send(_inject_token(initial_data))
                while data := await reader.read(65536):
                    await tunnel.send(_inject_token(data))

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

    async def _handle_talkback(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        initial_data: bytes,
    ) -> None:
        """Serve RTSP sendonly backchannel and stream s16le PCM to port 8999."""
        buf = bytearray(initial_data)
        pcm_buffer = bytearray()
        rtp_channel = 0
        talkback_tunnel: MagicP2PTunnel | None = None

        async def close_talkback_tunnel() -> None:
            nonlocal talkback_tunnel
            if talkback_tunnel is not None:
                if pcm_buffer:
                    if len(pcm_buffer) < TALKBACK_CHUNK_BYTES:
                        pcm_buffer.extend(
                            b"\x00" * (TALKBACK_CHUNK_BYTES - len(pcm_buffer))
                        )
                    with contextlib.suppress(OSError):
                        await talkback_tunnel.send(
                            bytes(pcm_buffer[:TALKBACK_CHUNK_BYTES])
                        )
                    pcm_buffer.clear()
                tunnel_to_close, talkback_tunnel = talkback_tunnel, None
                with contextlib.suppress(OSError):
                    await tunnel_to_close.close()

        try:
            while True:
                while buf:
                    if buf[0] == 0x24:  # '$' RTSP interleaved frame
                        if len(buf) < 4:
                            break
                        channel = buf[1]
                        frame_len = int.from_bytes(buf[2:4], "big")
                        if len(buf) < 4 + frame_len:
                            break
                        packet = bytes(buf[4 : 4 + frame_len])
                        del buf[: 4 + frame_len]
                        if channel == rtp_channel:
                            alaw_payload = extract_rtp_payload(packet)
                            if alaw_payload:
                                pcm_buffer.extend(decode_alaw_to_s16le(alaw_payload))
                                while len(pcm_buffer) >= TALKBACK_CHUNK_BYTES:
                                    if talkback_tunnel is None:
                                        talkback_tunnel = await MagicP2PTunnel.connect(
                                            self._camera_host,
                                            self._credentials,
                                            target_port=INTERNAL_TALKBACK_PORT,
                                        )
                                    chunk = bytes(pcm_buffer[:TALKBACK_CHUNK_BYTES])
                                    del pcm_buffer[:TALKBACK_CHUNK_BYTES]
                                    await talkback_tunnel.send(chunk)
                    else:
                        end_idx = buf.find(b"\r\n\r\n")
                        if end_idx == -1:
                            break
                        raw_req = bytes(buf[: end_idx + 4])
                        del buf[: end_idx + 4]
                        lines = raw_req.decode("latin-1", errors="replace").split(
                            "\r\n"
                        )
                        method = lines[0].split(" ", 1)[0].upper() if lines else ""
                        cseq = "1"
                        transport = "RTP/AVP/TCP;unicast;interleaved=0-1"
                        for line in lines[1:]:
                            lower = line.lower()
                            if lower.startswith("cseq:"):
                                cseq = line.split(":", 1)[1].strip()
                            elif lower.startswith("transport:"):
                                transport = line.split(":", 1)[1].strip()
                                match = re.search(r"interleaved=(\d+)", transport)
                                if match:
                                    rtp_channel = int(match.group(1))
                        if method == "DESCRIBE":
                            resp = (
                                f"RTSP/1.0 200 OK\r\n"
                                f"CSeq: {cseq}\r\n"
                                f"Content-Type: application/sdp\r\n"
                                f"Content-Length: {len(_TALKBACK_SDP)}\r\n\r\n"
                            ).encode("ascii") + _TALKBACK_SDP
                        elif method == "SETUP":
                            resp = (
                                f"RTSP/1.0 200 OK\r\n"
                                f"CSeq: {cseq}\r\n"
                                f"Transport: {transport}\r\n"
                                f"Session: 89990001\r\n\r\n"
                            ).encode("ascii")
                        elif method in ("PLAY", "PAUSE", "TEARDOWN"):
                            resp = (
                                f"RTSP/1.0 200 OK\r\n"
                                f"CSeq: {cseq}\r\n"
                                f"Session: 89990001\r\n\r\n"
                            ).encode("ascii")
                        else:
                            resp = (
                                f"RTSP/1.0 200 OK\r\n"
                                f"CSeq: {cseq}\r\n"
                                f"Public: OPTIONS, DESCRIBE, SETUP, PLAY, "
                                f"TEARDOWN, GET_PARAMETER, SET_PARAMETER\r\n\r\n"
                            ).encode("ascii")
                        writer.write(resp)
                        await writer.drain()
                        if method == "TEARDOWN":
                            return

                try:
                    chunk = await asyncio.wait_for(
                        reader.read(65536), timeout=TALKBACK_IDLE_TIMEOUT
                    )
                except TimeoutError:
                    await close_talkback_tunnel()
                    continue
                if not chunk:
                    break
                buf.extend(chunk)
        except asyncio.CancelledError:
            raise
        except (EOFError, OSError) as err:
            self._on_connection_error(err)
        finally:
            await close_talkback_tunnel()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
