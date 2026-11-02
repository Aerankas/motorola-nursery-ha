"""5GenCare MagicP2P transport used by supported Motorola cameras."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import hmac
import logging
import os
import socket
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass

CAMERA_PORT = 77
INTERNAL_RTSP_PORT = 6667
CONNECT_TIMEOUT = 8
# Supported input sizes, not the minimum padding widths used in generate_sid.
MIN_SID_DEVICE_LENGTH = 3
MAX_SID_DEVICE_LENGTH = 30
_LOCAL_ACK = b"ok 0 dconn"
_LOGGER = logging.getLogger(__name__)


def _connection_category(error: BaseException) -> str:
    """Classify failures without exporting exception text or private endpoints."""
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, socket.gaierror):
        return "dns"
    if isinstance(error, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(error, ConnectionResetError):
        return "connection_reset"
    if isinstance(error, EOFError):
        return "incomplete_response"
    if isinstance(error, ValueError):
        return "invalid_parameters"
    if isinstance(error, OSError):
        if error.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            return "unreachable"
        if error.errno in (errno.EACCES, errno.EPERM):
            return "permission"
    return "connection"


def _log_handshake_response(response: bytes, expected: int) -> None:
    """Describe bounded framing only; never log response bytes or identifiers."""
    fields = response.split()
    first = fields[0] if fields else b""
    negative = first.startswith(b"-") and first[1:].isdigit()
    if response.lower().startswith(_LOCAL_ACK):
        shape = "expected_prefix"
    elif negative:
        shape = "negative_status"
    else:
        shape = "other" if response else "empty"

    _LOGGER.debug(
        "Local tunnel response: received_bytes=%d expected_bytes=%d fields=%d shape=%s",
        len(response),
        expected,
        len(fields),
        shape,
    )


@dataclass(frozen=True, slots=True, repr=False)
class Credentials:
    """Technical credentials required by the local camera tunnel."""

    sid_user_id: int
    sid_device: bytes
    magic_token: bytes
    rtsp_username: str
    rtsp_password: str
    access_token: str

    def validate(self) -> None:
        """Reject malformed data before opening a network connection."""
        if self.sid_user_id < 0 or self.sid_user_id > 0xFFFFFFFF:
            raise ValueError("SID user ID must fit in an unsigned 32-bit integer")
        if not MIN_SID_DEVICE_LENGTH <= len(self.sid_device) <= MAX_SID_DEVICE_LENGTH:
            raise ValueError(
                f"SID device value must contain {MIN_SID_DEVICE_LENGTH} "
                f"to {MAX_SID_DEVICE_LENGTH} bytes"
            )
        if not 3 <= len(self.magic_token) <= 27:
            raise ValueError("MagicP2P token must contain 3 to 27 bytes")
        if not self.rtsp_username or not self.rtsp_password or not self.access_token:
            raise ValueError("RTSP credentials and access token are required")


class MagicCipher:
    """Stateful byte transform used after the MagicP2P handshake."""

    def __init__(self, token: bytes) -> None:
        if not 1 <= len(token) < 254:
            raise ValueError("Invalid token length")
        self._token = token
        self._prefix = bytearray()
        self._key: bytearray | None = None
        self._index = 0

    def _initialize(self, prefix: bytes | bytearray) -> None:
        previous = 0xAA
        key = bytearray()
        for token_byte, random_byte in zip(self._token, prefix, strict=False):
            previous ^= (token_byte >> 1) ^ ((random_byte & 0x7F) << 1)
            key.append(previous)
        self._key = key
        self._index = prefix[len(self._token)] % len(self._token)

    def encode(self, data: bytes) -> bytes:
        """Encode a chunk; the first call prepends the random cipher state."""
        output = bytearray()
        if self._key is None:
            # The native implementation generates bytes in the inclusive range
            # 21..255 and stores token_length + 1 as the initial key index.
            prefix = bytes(21 + value % 235 for value in os.urandom(len(self._token)))
            prefix += bytes([len(self._token) + 1])
            self._initialize(prefix)
            output.extend(prefix)
        output.extend(self._transform(data, encode=True))
        return bytes(output)

    def decode(self, data: bytes) -> bytes:
        """Decode a chunk, accepting a cipher prefix split across TCP reads."""
        if self._key is None:
            needed = len(self._token) + 1 - len(self._prefix)
            consumed = min(len(data), needed)
            self._prefix.extend(data[:consumed])
            data = data[consumed:]
            if len(self._prefix) <= len(self._token):
                return b""
            self._initialize(self._prefix)
        return self._transform(data, encode=False)

    def _transform(self, data: bytes, *, encode: bool) -> bytes:
        if self._key is None:
            raise RuntimeError("Cipher is not initialized")
        output = bytearray()
        for value in data:
            key_byte = self._key[self._index]
            plain = value if encode else value ^ key_byte
            output.append(value ^ key_byte)
            self._index = (self._index + ((plain + key_byte) | 1)) % len(self._token)
        return bytes(output)


def generate_sid(credentials: Credentials) -> bytes:
    """Generate the 78-byte v1 device session identifier."""
    credentials.validate()
    identifier = f"{credentials.sid_user_id:08x}".encode()
    device = credentials.sid_device
    token = credentials.magic_token
    # The native v1 format uses minimum widths (%-20s/%-27s), not truncation.
    # Keep the full device value, including 30-byte IDs, in the key and HMAC.
    key = (
        identifier
        + device.ljust(20, b" ")
        + token.ljust(27, b" ")
        + device.ljust(20, b" ")
    )[:32]
    digest = hmac.new(key, device, hashlib.sha256).hexdigest().encode()
    return (identifier + device[:3] + token[:3] + digest).lower()


def build_handshake(
    credentials: Credentials, *, target_port: int = INTERNAL_RTSP_PORT
) -> tuple[bytes, bytes]:
    """Build a connection request and return it with the expected SID."""
    if not 0 <= target_port <= 0xFFFF:
        raise ValueError("Target port must fit in an unsigned 16-bit integer")
    sid = generate_sid(credentials)
    client_id = str(uuid.uuid4()).encode()
    request = (
        b"v002 888 "
        + f"{target_port:05d} {len(sid):03d} ".encode()
        + sid
        + f" {len(client_id):04d} ".encode()
        + client_id
    )
    return request, sid


class MagicP2PTunnel:
    """One authenticated, encrypted TCP tunnel to an internal camera service."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        token: bytes,
    ) -> None:
        """Initialize directional cipher state and optional ACK tracking."""
        self._reader = reader
        self._writer = writer
        self._encoder = MagicCipher(token)
        self._decoder = MagicCipher(token)
        self._send_lock = asyncio.Lock()
        self._receive_lock = asyncio.Lock()
        self._handshake_echo: bytes | None = None
        self._handshake_pending = bytearray()
        self._first_response = True

    def _consume_handshake_echo(self, data: bytes) -> bytes:
        """Strip an optional SID echo without losing fragmented cipher bytes."""
        if self._handshake_echo is None:
            return data
        self._handshake_pending.extend(data)
        echo = self._handshake_echo
        pending = self._handshake_pending
        compared = min(len(pending), len(echo))
        if pending[:compared] == echo[:compared]:
            if len(pending) <= len(echo):
                return b""
            if pending[len(echo) : len(echo) + 1] not in (b"\n", b"\r", b"\0", b" "):
                raise ConnectionError("Invalid MagicP2P acknowledgement delimiter")
            data = bytes(pending[len(echo) + 1 :])
            _LOGGER.debug("Local tunnel acknowledgement suffix: format=sid_echo")
        else:
            # A short ACK has no echo; these bytes belong to the cipher prefix.
            data = bytes(pending)
            _LOGGER.debug("Local tunnel acknowledgement suffix: format=none")
        self._handshake_echo = None
        pending.clear()
        return data

    async def _transform(
        self, cipher: MagicCipher, data: bytes, *, encode: bool
    ) -> bytes:
        """Finish executor work before releasing the direction's lock."""
        transform = cipher.encode if encode else cipher.decode
        future = asyncio.get_running_loop().run_in_executor(None, transform, data)
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(future)
                break
            except asyncio.CancelledError:
                if future.cancelled():
                    raise
                # Cancelling an await cannot stop a running worker. Keep the
                # state protected until it finishes, even on repeated cancels.
                self._writer.close()
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result

    @classmethod
    async def connect(
        cls,
        host: str,
        credentials: Credentials,
        *,
        target_port: int = INTERNAL_RTSP_PORT,
    ) -> MagicP2PTunnel:
        """Open and authenticate a local camera connection."""
        started = time.monotonic()
        stage = "credentials"
        writer = None
        connected = False
        response = bytearray()
        sid = b""
        expected = 0
        _LOGGER.debug(
            "Local tunnel connect: transport=magicp2p_v1 camera_port=%d "
            "target_port=%d timeout=%s",
            CAMERA_PORT,
            target_port,
            CONNECT_TIMEOUT,
        )
        try:
            credentials.validate()
            async with asyncio.timeout(CONNECT_TIMEOUT):
                stage = "tcp_connect"
                reader, writer = await asyncio.open_connection(host, CAMERA_PORT)
                _LOGGER.debug(
                    "Local tunnel TCP connected: elapsed=%.3fs",
                    time.monotonic() - started,
                )
                stage = "handshake_send"
                request, sid = build_handshake(credentials, target_port=target_port)
                writer.write(request)
                await writer.drain()
                _LOGGER.debug(
                    "Local tunnel handshake sent: request_bytes=%d sid_bytes=%d",
                    len(request),
                    len(sid),
                )
                stage = "handshake_receive"
                # The Android native LAN path compares these ten bytes only.
                # Do not wait for an SID echo: short-ACK peers wait for RTSP next.
                expected = len(_LOCAL_ACK)
                while len(response) < expected:
                    chunk = await reader.read(expected - len(response))
                    if not chunk:
                        raise asyncio.IncompleteReadError(bytes(response), expected)
                    response.extend(chunk)
                stage = "handshake_validate"
                _log_handshake_response(bytes(response), expected)
                if response.lower() != _LOCAL_ACK:
                    raise ConnectionError("Camera rejected the MagicP2P handshake")
            tunnel = cls(reader, writer, credentials.magic_token)
            # VM65 firmware appends a space, SID and delimiter. Consume it on
            # receive, after allowing the client to send its first RTSP request.
            tunnel._handshake_echo = b" " + sid
            connected = True
            _LOGGER.debug(
                "Local tunnel authenticated: elapsed=%.3fs", time.monotonic() - started
            )
            return tunnel
        except asyncio.CancelledError:
            _LOGGER.debug("Local tunnel cancelled: stage=%s", stage)
            raise
        except (OSError, EOFError, ValueError) as err:
            if stage == "handshake_receive":
                _log_handshake_response(bytes(response), expected)
            _LOGGER.debug(
                "Local tunnel failed: stage=%s category=%s elapsed=%.3fs",
                stage,
                "unexpected_response"
                if stage == "handshake_validate"
                else _connection_category(err),
                time.monotonic() - started,
            )
            raise
        finally:
            if writer is not None and not connected:
                writer.close()
                with suppress(OSError):
                    async with asyncio.timeout(CONNECT_TIMEOUT):
                        await writer.wait_closed()

    async def send(self, data: bytes) -> None:
        """Encode and forward plaintext bytes to the camera."""
        async with self._send_lock:
            if self._writer.is_closing():
                raise ConnectionError("Camera tunnel is closed")
            encrypted = await self._transform(self._encoder, data, encode=True)
            self._writer.write(encrypted)
            await self._writer.drain()

    async def receive(self) -> bytes:
        """Receive and decode at least one plaintext chunk."""
        async with self._receive_lock:
            if self._writer.is_closing():
                raise ConnectionError("Camera tunnel is closed")
            try:
                async with asyncio.timeout(
                    CONNECT_TIMEOUT if self._first_response else None
                ):
                    while encrypted := await self._reader.read(65536):
                        encrypted = self._consume_handshake_echo(encrypted)
                        if not encrypted:
                            continue
                        if plain := await self._transform(
                            self._decoder, encrypted, encode=False
                        ):
                            self._first_response = False
                            return plain
                    if self._handshake_pending:
                        raise EOFError("Incomplete MagicP2P acknowledgement suffix")
                    return b""
            except TimeoutError:
                self._writer.close()
                _LOGGER.debug(
                    "Local tunnel failed: stage=first_response category=timeout"
                )
                raise

    async def close(self) -> None:
        """Close the camera transport."""
        self._writer.close()
        await self._writer.wait_closed()
