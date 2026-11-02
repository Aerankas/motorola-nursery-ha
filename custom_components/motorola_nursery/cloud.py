"""Authenticated 5GenCare text protocol; secrets never appear in exceptions."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import ssl
import time
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import unquote

from .protocol import MAX_SID_DEVICE_LENGTH, MIN_SID_DEVICE_LENGTH

DEFAULT_HOST = "9.moto.5gencare.com"
PORT = 3388
MAX_LINE = 65536
TIMEOUT = 15
MAX_OTP_REDIRECTS = 2
_LOGGER = logging.getLogger(__name__)


def _response_summary(fields: list[str]) -> tuple[str, str]:
    """Classify responses without logging identifiers or arbitrary vendor text."""
    if not fields:
        return "empty", "none"
    first = fields[0]
    if first.startswith("-"):
        # Only short negative numeric statuses can be exported. Positive numbers
        # may be account IDs; text may echo email addresses, codes or tokens.
        status = (
            first if re.fullmatch(r"-[1-9]\d{0,3}", first, flags=re.ASCII) else "other"
        )
        return "negative_status", status
    return ("numeric" if first.isdigit() else "text"), "none"


class CloudError(Exception):
    """Malformed or rejected cloud operation."""


class DeviceListError(CloudError):
    """Camera-list contents do not match the supported framing or credentials."""


class AuthenticationError(CloudError):
    """The session or email code was rejected."""


def _network_category(error: BaseException) -> str:
    """Never format network exception text: it can contain private endpoints."""
    if isinstance(error, ssl.SSLCertVerificationError):
        return "tls_certificate"
    if isinstance(error, ssl.SSLError):
        return "tls"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, socket.gaierror):
        return "dns"
    if isinstance(error, ConnectionRefusedError):
        return "connection_refused"
    return "connection"


def atom(value: str) -> str:
    """Prevent protocol injection, including whitespace and non-ASCII input."""
    if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("Invalid protocol field")
    return value


def vendor_host(value: str) -> str:
    """Only follow routing responses within the vendor's camera domain."""
    if not re.fullmatch(r"[a-z0-9-]+\.moto\.5gencare\.com", value):
        raise CloudError("Unexpected authentication server")
    return value


@dataclass(frozen=True, slots=True)
class Session:
    """Replace the saved token after every successful session resumption."""

    user_id: str = field(repr=False)
    token: str = field(repr=False)
    session_id: str = field(repr=False)
    host: str

    def as_dict(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "token": self.token,
            "session_id": self.session_id,
            "host": self.host,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> Session:
        return cls(
            atom(data["user_id"]),
            atom(data["token"]),
            atom(data["session_id"]),
            vendor_host(data["host"]),
        )


@dataclass(frozen=True, slots=True)
class Challenge:
    """OTP request information, used only while the config flow is active."""

    user_id: str = field(repr=False)
    host: str


@dataclass(frozen=True, slots=True)
class Device:
    """Device credentials from the authenticated list; seven fields per row."""

    owner_id: str = field(repr=False)
    identifier: str = field(repr=False)
    model: str
    encoded_name: str = field(repr=False)
    magic_token: str = field(repr=False)
    sid_device: str = field(repr=False)
    trailing_field: str

    @property
    def name(self) -> str:
        return unquote(self.encoded_name) or "Motorola camera"


def _field_kind(value: str) -> str:
    """Describe field structure without exporting any vendor-supplied value."""
    if not value:
        return "empty"
    if value.isascii() and value.isdecimal():
        return "decimal"
    if value.startswith("-") and value[1:].isascii() and value[1:].isdecimal():
        return "negative_decimal"
    return "text"


def _model_family(value: str) -> str:
    """Only export known model families, never arbitrary model field contents."""
    if value in {"VM65", "VM65CONNECT"}:
        return "VM65"
    if value in {"VM85", "VM85CONNECT"}:
        return "VM85"
    return "other"


def parse_devices(fields: list[str]) -> list[Device]:
    """Validate camera rows and log only counts, shapes and failed rule names."""
    try:
        count = int(fields[0])
    except (IndexError, ValueError):
        _LOGGER.debug(
            "Account device list rejected: reason=invalid_count "
            "received_fields=%d count_kind=%s",
            len(fields),
            _field_kind(fields[0]) if fields else "missing",
        )
        raise DeviceListError("Invalid device count") from None
    if not 0 <= count <= 32:
        _LOGGER.debug(
            "Account device list rejected: reason=count_out_of_range "
            "received_fields=%d supported_count=0..32",
            len(fields),
        )
        raise DeviceListError("Unrecognized device list framing")
    expected = 1 + count * 7
    _LOGGER.debug(
        "Account device list framing: declared_count=%d "
        "received_fields=%d expected_fields=%d fields_per_device=7",
        count,
        len(fields),
        expected,
    )
    if len(fields) != expected:
        _LOGGER.debug("Account device list rejected: reason=field_count_mismatch")
        raise DeviceListError("Unrecognized device list framing")
    result = []
    for index in range(count):
        row = fields[1 + index * 7 : 8 + index * 7]
        result.append(_parse_device(row, index))
    return result


def _parse_device(row: list[str], index: int) -> Device:
    """Validate a single record without logging its private values."""
    owner_decimal = _field_kind(row[0]) == "decimal"
    owner_fits = False
    if owner_decimal:
        # Oversized decimal strings may exceed Python's conversion cap.
        with suppress(ValueError):
            owner_fits = 0 <= int(row[0]) <= 0xFFFFFFFF
    failures = []
    if not owner_decimal:
        failures.append("owner_id_not_decimal")
    elif not owner_fits:
        failures.append("owner_id_out_of_u32_range")
    if not 3 <= len(row[4]) <= 27:
        failures.append("magic_token_length")
    device_length_valid = MIN_SID_DEVICE_LENGTH <= len(row[5]) <= MAX_SID_DEVICE_LENGTH
    if not device_length_valid:
        failures.append("device_id_length")
    _LOGGER.debug(
        "Account device record: index=%d model_family=%s "
        "field_lengths=%s field_kinds=%s owner_fits_u32=%s "
        "magic_token_length_valid=%s device_id_length_valid=%s",
        index,
        _model_family(row[2]),
        tuple(map(len, row)),
        tuple(map(_field_kind, row)),
        owner_fits,
        3 <= len(row[4]) <= 27,
        device_length_valid,
    )
    if failures:
        _LOGGER.debug(
            "Account device record rejected: index=%d reasons=%s "
            "supported_magic_token_length=3..27 supported_device_id_length=%d..%d",
            index,
            ",".join(failures),
            MIN_SID_DEVICE_LENGTH,
            MAX_SID_DEVICE_LENGTH,
        )
        raise DeviceListError("Invalid device credentials")
    return Device(*row)


class CloudClient:
    """One serialized session; a reader answers keepalives during UI code entry."""

    def __init__(self, context: ssl.SSLContext, host: str = DEFAULT_HOST) -> None:
        self.host = vendor_host(host)
        self.session: Session | None = None
        self._context = context
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._messages: asyncio.Queue[list[str] | Exception] = asyncio.Queue(64)
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open one bounded TLS connection to the validated vendor endpoint."""
        if self._writer is not None:
            raise RuntimeError("Client is already connected")
        route = "bootstrap" if self.host == DEFAULT_HOST else "vendor_routed"
        _LOGGER.debug(
            "Account cloud connect: route=%s port=%d TLS=verified", route, PORT
        )
        started = time.monotonic()
        try:
            async with asyncio.timeout(TIMEOUT):
                reader, self._writer = await asyncio.open_connection(
                    self.host,
                    PORT,
                    ssl=self._context,
                    server_hostname=self.host,
                    limit=MAX_LINE,
                )
        except OSError as err:
            _LOGGER.debug(
                "Account cloud connect failed: route=%s category=%s elapsed=%.3fs",
                route,
                _network_category(err),
                time.monotonic() - started,
            )
            raise
        _LOGGER.debug(
            "Account cloud connected: route=%s elapsed=%.3fs",
            route,
            time.monotonic() - started,
        )
        self._reader_task = asyncio.create_task(self._receive(reader))

    async def _receive(self, reader: asyncio.StreamReader) -> None:
        """Validate frames and report queue failure without exposing payloads."""
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    raise ConnectionError("Cloud connection closed")
                if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
                    raise CloudError("Invalid protocol frame")
                await self._dispatch_message(raw.decode("ascii").split())
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, CloudError, asyncio.QueueFull) as err:
            if isinstance(err, (ValueError, CloudError)):
                category = "protocol_frame"
            elif isinstance(err, asyncio.QueueFull):
                category = "queue_full"
            else:
                category = _network_category(err)
            _LOGGER.debug("Account cloud reader stopped: category=%s", category)
            if self._messages.full():
                self._messages.get_nowait()
            self._messages.put_nowait(ConnectionError("Cloud connection closed"))

    async def _dispatch_message(self, parts: list[str]) -> None:
        """Answer keepalives and queue application replies within the size limit."""
        if parts == ["ping"]:
            if self._writer is not None:
                self._writer.write(b"pong\n")
                await self._writer.drain()
        elif parts:
            self._messages.put_nowait(parts)

    async def close(self) -> None:
        """Cancel the reader and bound the wait for TLS socket shutdown."""
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._writer is not None:
            self._writer.close()
            with suppress(OSError):
                async with asyncio.timeout(5):
                    await self._writer.wait_closed()
            self._writer = None

    async def _exchange(self, command: str, *fields: str) -> list[str]:
        """Send one validated command and skip a bounded number of unrelated replies."""
        if self._writer is None:
            raise ConnectionError("Client is not connected")
        message = " ".join([atom(command), *(atom(value) for value in fields)]) + "\n"
        operation = (
            command
            if command in {"v3_otp", "v3_loginset", "v3_session", "v3_dlist"}
            else "other"
        )
        started = time.monotonic()
        _LOGGER.debug("Account request: operation=%s", operation)
        try:
            async with asyncio.timeout(TIMEOUT):
                self._writer.write(message.encode("ascii"))
                await self._writer.drain()
                for skipped in range(128):
                    reply = await self._messages.get()
                    if isinstance(reply, Exception):
                        raise reply
                    if reply[0] == command:
                        result = reply[1:]
                        kind, status = _response_summary(result)
                        _LOGGER.debug(
                            "Account response: operation=%s fields=%d first=%s "
                            "status=%s unrelated=%d elapsed=%.3fs",
                            operation,
                            len(result),
                            kind,
                            status,
                            skipped,
                            time.monotonic() - started,
                        )
                        return result
                _LOGGER.debug(
                    "Account request failed: operation=%s "
                    "reason=too_many_unrelated_frames",
                    operation,
                )
                raise CloudError("Too many unrelated protocol frames")
        except (OSError, EOFError) as err:
            _LOGGER.debug(
                "Account request failed: operation=%s category=%s elapsed=%.3fs",
                operation,
                _network_category(err),
                time.monotonic() - started,
            )
            raise

    async def request_code(self, login_uuid: str, email: str) -> Challenge:
        """Follow bounded vendor OTP routing after an explicit email submission."""
        async with self._lock:
            visited = {self.host}
            for redirects in range(MAX_OTP_REDIRECTS + 1):
                fields = await self._exchange("v3_otp", login_uuid, "email", email, "6")
                if len(fields) == 2 and fields[0] == "-6":
                    await self._redirect_otp(fields[1], visited, redirects)
                    continue
                if fields and fields[0].startswith("-"):
                    _LOGGER.debug("Account code request failed: reason=server_rejected")
                    raise CloudError("Email code request was not accepted")
                if len(fields) != 3 or not fields[0].isdigit():
                    _LOGGER.debug(
                        "Account code request failed: reason=unexpected_response "
                        "expected_fields=3"
                    )
                    raise CloudError("Email code request was not accepted")
                try:
                    host = vendor_host(fields[2])
                except CloudError:
                    _LOGGER.debug(
                        "Account code request failed: reason=unsupported_routing_host"
                    )
                    raise
                _LOGGER.debug(
                    "Account code request accepted: redirect=%s", host != self.host
                )
                return Challenge(fields[0], host)
            raise CloudError("Email code routing did not complete")

    async def _redirect_otp(
        self, value: str, visited: set[str], redirects: int
    ) -> None:
        """Follow only a new vendor route, replacing all old connection state."""
        try:
            host = vendor_host(value)
        except CloudError:
            _LOGGER.debug(
                "Account code request failed: reason=unsupported_routing_host"
            )
            raise
        if host in visited or redirects == MAX_OTP_REDIRECTS:
            _LOGGER.debug("Account code request failed: reason=routing_loop_or_limit")
            raise CloudError("Email code routing did not complete")
        visited.add(host)
        _LOGGER.debug("Account code request routing: redirect=%d", redirects + 1)
        await self.close()
        # Discard old-server frames/EOF before opening the new route.
        self._messages = asyncio.Queue(64)
        self.host = host
        await self.connect()

    async def submit_code(
        self, challenge: Challenge, login_uuid: str, email: str, code: str
    ) -> Session:
        """Validate an ASCII email code before exchanging it for a saved session."""
        if not re.fullmatch(r"\d{6}", code, flags=re.ASCII):
            _LOGGER.debug("Account code submission failed: reason=invalid_code_format")
            raise ValueError("Expected a six-digit email code")
        if challenge.host != self.host:
            raise CloudError("Incorrect challenge server")
        async with self._lock:
            fields = await self._exchange(
                "v3_loginset", challenge.user_id, login_uuid, "email", email, code
            )
            if fields and fields[0].startswith("-"):
                _LOGGER.debug("Account code submission failed: reason=server_rejected")
                raise AuthenticationError("Email code rejected")
            if len(fields) != 5 or fields[0] != challenge.user_id:
                _LOGGER.debug(
                    "Account code submission failed: "
                    "reason=unexpected_response expected_fields=5"
                )
                raise CloudError("Unexpected login response")
            self.session = Session(
                fields[0], atom(fields[1]), atom(fields[3]), vendor_host(fields[4])
            )
            return self.session

    async def resume(self, session: Session) -> Session:
        if session.host != self.host:
            raise CloudError("Incorrect session server")
        async with self._lock:
            fields = await self._exchange(
                "v3_session", session.user_id, session.token, session.session_id
            )
            if fields and fields[0].startswith("-"):
                _LOGGER.debug(
                    "Account session resumption failed: reason=server_rejected"
                )
                raise AuthenticationError("Session rejected")
            if len(fields) != 4 or fields[0] != session.user_id:
                _LOGGER.debug(
                    "Account session resumption failed: "
                    "reason=unexpected_response expected_fields=4"
                )
                raise CloudError("Unexpected session response")
            self.session = Session(
                fields[0], atom(fields[1]), atom(fields[2]), vendor_host(fields[3])
            )
            return self.session

    async def devices(self) -> list[Device]:
        if self.session is None:
            raise AuthenticationError("Authenticate before listing devices")
        async with self._lock:
            fields = await self._exchange("v3_dlist")
            try:
                devices = parse_devices(fields)
            except CloudError:
                _LOGGER.debug(
                    "Account device listing failed: reason=invalid_device_response"
                )
                raise
            _LOGGER.debug("Account device listing accepted: devices=%d", len(devices))
            return devices
