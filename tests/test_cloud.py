"""Real stream framing tests against a synthetic in-process vendor server."""

import asyncio
import logging
import socket
import ssl
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from custom_components.motorola_nursery.cloud import (
    AuthenticationError,
    CloudClient,
    CloudError,
    DeviceListError,
    Session,
    atom,
    parse_devices,
)


@pytest.mark.asyncio
async def test_login_resume_devices_and_idle_keepalive(socket_enabled, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    observed = []
    peer_done = asyncio.Event()

    async def server(reader, writer):
        try:
            observed.append(await reader.readline())
            writer.write(b"v3_otp 123456 opaque 9.moto.5gencare.com\n")
            await writer.drain()
            writer.write(b"pi")
            await writer.drain()
            await asyncio.sleep(0)
            writer.write(b"ng\n")
            await writer.drain()
            assert await reader.readline() == b"pong\n"
            observed.append(await reader.readline())
            writer.write(
                b"v3_loginset 123456 synthetic-token unused-long-token "
                b"1234567 9.moto.5gencare.com\n"
            )
            await writer.drain()
            observed.append(await reader.readline())
            writer.write(
                b"v3_session 123456 rotated-test-token 1234567 "
                b"9.moto.5gencare.com\nv3_unrelated 1\n"
            )
            await writer.drain()
            observed.append(await reader.readline())
            writer.write(
                b"v3_dlist 1 123456 camera-identifier VM65 Nursery%20Test "
                b"test-magic-token abcdefghijklmnopqrst -1\n"
            )
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            peer_done.set()

    listener = await asyncio.start_server(server, "127.0.0.1", 0)
    address = listener.sockets[0].getsockname()
    open_connection = asyncio.open_connection

    async def connect(*args, **kwargs):
        assert isinstance(kwargs["ssl"], ssl.SSLContext)
        assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["server_hostname"] == "9.moto.5gencare.com"
        return await open_connection(*address)

    client = CloudClient(ssl.create_default_context())
    try:
        with patch(
            "custom_components.motorola_nursery.cloud.asyncio.open_connection", connect
        ):
            await client.connect()
        challenge = await client.request_code("SYNTHETICUUID", "owner@example.test")
        await asyncio.sleep(
            0.01
        )  # The reader answers ping while no request is running.
        session = await client.submit_code(
            challenge, "SYNTHETICUUID", "owner@example.test", "123456"
        )
        renewed = await client.resume(session)
        devices = await client.devices()
        assert renewed.token == "rotated-test-token"
        assert client.session == renewed
        assert devices[0].name == "Nursery Test"
        assert "test-magic-token" not in repr(devices[0])
        assert "rotated-test-token" not in repr(renewed)
    finally:
        await client.close()
        await asyncio.wait_for(peer_done.wait(), 2)
        listener.close()
        await listener.wait_closed()
    assert observed[0] == b"v3_otp SYNTHETICUUID email owner@example.test 6\n"
    assert (
        observed[1]
        == b"v3_loginset 123456 SYNTHETICUUID email owner@example.test 123456\n"
    )
    assert "operation=v3_otp fields=3 first=numeric status=none" in caplog.text
    assert "operation=v3_loginset fields=5" in caplog.text
    assert "operation=v3_session fields=4" in caplog.text
    assert "Account device listing accepted: devices=1" in caplog.text
    for secret in (
        "123456",
        "SYNTHETICUUID",
        "owner@example.test",
        "synthetic-token",
        "rotated-test-token",
        "test-magic-token",
        "camera-identifier",
        "abcdefghijklmnopqrst",
        "Nursery%20Test",
    ):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("fields", "summary", "reason"),
    [
        (
            ["-10", "private@example.test"],
            "first=negative_status status=-10",
            "server_rejected",
        ),
        (["-123456789"], "first=negative_status status=other", "server_rejected"),
        (["-private-token"], "first=negative_status status=other", "server_rejected"),
        ([], "first=empty status=none", "unexpected_response"),
        (["private@example.test"], "first=text status=none", "unexpected_response"),
        (["654321", "private-token"], "fields=2 first=numeric", "unexpected_response"),
        (
            ["654321", "private-token", "private.invalid"],
            "fields=3",
            "unsupported_routing_host",
        ),
    ],
)
@pytest.mark.asyncio
async def test_code_request_logs_are_actionable_without_payload(
    fields, summary, reason, caplog
):
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    client = CloudClient(ssl.create_default_context())
    client._writer = Mock(drain=AsyncMock())
    client._messages.put_nowait(["v3_otp", *fields])
    with pytest.raises(CloudError):
        await client.request_code("PRIVATEUUID", "private@example.test")
    assert summary in caplog.text
    assert f"reason={reason}" in caplog.text
    assert "elapsed=" in caplog.text
    for secret in (
        "PRIVATEUUID",
        "private@example.test",
        "private-token",
        "private.invalid",
        "654321",
        "123456789",
    ):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (socket.gaierror("private.invalid"), "dns"),
        (ssl.SSLCertVerificationError("private.invalid"), "tls_certificate"),
        (ssl.SSLError("private.invalid"), "tls"),
        (ConnectionRefusedError("private.invalid"), "connection_refused"),
        (TimeoutError("private.invalid"), "timeout"),
    ],
)
@pytest.mark.asyncio
async def test_connection_failure_logs_only_safe_category(error, category, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    client = CloudClient(ssl.create_default_context(), "other.moto.5gencare.com")
    with (
        patch("asyncio.open_connection", AsyncMock(side_effect=error)),
        pytest.raises(type(error)),
    ):
        await client.connect()
    assert f"route=vendor_routed category={category}" in caplog.text
    assert "private.invalid" not in caplog.text
    assert "other.moto.5gencare.com" not in caplog.text


@pytest.mark.asyncio
async def test_request_timeout_and_reader_failure_are_distinct(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    client = CloudClient(ssl.create_default_context())
    client._writer = Mock(drain=AsyncMock())
    with (
        patch("custom_components.motorola_nursery.cloud.TIMEOUT", 0.001),
        pytest.raises(TimeoutError),
    ):
        await client.request_code("PRIVATEUUID", "private@example.test")
    assert "operation=v3_otp category=timeout" in caplog.text
    reader = asyncio.StreamReader()
    reader.feed_data(b"v3_otp private-token \xff\n")
    await client._receive(reader)
    with pytest.raises(ConnectionError):
        await client.request_code("PRIVATEUUID", "private@example.test")
    assert "reader stopped: category=protocol_frame" in caplog.text
    assert "operation=v3_otp category=connection" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.asyncio
async def test_routing_and_later_authentication_failures(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    client = CloudClient(ssl.create_default_context())
    client._writer = Mock(drain=AsyncMock())
    client._messages.put_nowait(
        ["v3_otp", "654321", "private-token", "other.moto.5gencare.com"]
    )
    challenge = await client.request_code("PRIVATEUUID", "private@example.test")
    assert "accepted: redirect=True" in caplog.text
    client.host = challenge.host
    client._messages.put_nowait(["v3_loginset", "-10", "private-token"])
    with pytest.raises(AuthenticationError):
        await client.submit_code(
            challenge, "PRIVATEUUID", "private@example.test", "654321"
        )
    assert "code submission failed: reason=server_rejected" in caplog.text
    client.session = Session("654321", "private-token", "654322", client.host)
    client._messages.put_nowait(["v3_dlist", "1"])
    with pytest.raises(CloudError):
        await client.devices()
    assert "device listing failed: reason=invalid_device_response" in caplog.text
    for secret in ("654321", "PRIVATEUUID", "private-token", "private@example.test"):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_session_rejection_contains_no_payload():
    """Rejected sessions must not persist or disclose the response payload."""
    client = CloudClient(ssl.create_default_context())
    from unittest.mock import AsyncMock

    client._exchange = AsyncMock(return_value=["-10"])
    session = Session("123456", "private-token", "1234567", client.host)
    with pytest.raises(AuthenticationError, match="Session rejected"):
        await client.resume(session)
    assert client.session is None


def test_device_framing_and_injection():
    row = ["123456", "opaque", "VM65", "Nursery", "test-token", "test-device", "-1"]
    assert len(parse_devices(["2", *row, *row])) == 2
    for fields in [["-1"], ["99"], ["2", *row], ["1", *row, "extra"]]:
        with pytest.raises(CloudError):
            parse_devices(fields)
    for value in ["a@example.test\nv3_dlist", "white space", "a\x00b"]:
        with pytest.raises(ValueError):
            atom(value)


@pytest.mark.asyncio
async def test_otp_redirect_reconnects_and_preserves_identity(socket_enabled, caplog):
    """A real routed exchange discards bootstrap EOF and keeps one OTP identity."""
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    observed = []
    finished = asyncio.Event()

    async def server(reader, writer):
        """Redirect the first connection and accept the unchanged second request."""
        try:
            request = await reader.readline()
            observed.append(request)
            if len(observed) == 1:
                writer.write(b"v3_otp -6 other.moto.5gencare.com\nv3_stale opaque\n")
            else:
                writer.write(b"v3_otp 654321 opaque other.moto.5gencare.com\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            if len(observed) == 2:
                finished.set()

    listener = await asyncio.start_server(server, "127.0.0.1", 0)
    address = listener.sockets[0].getsockname()
    original_connect = asyncio.open_connection
    hosts = []

    async def connect(host, port, **kwargs):
        """Check TLS routing arguments while using the synthetic local server."""
        hosts.append(host)
        assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["server_hostname"] == host
        return await original_connect(*address)

    client = CloudClient(ssl.create_default_context())
    try:
        with patch("asyncio.open_connection", connect):
            await client.connect()
            challenge = await client.request_code("PRIVATEUUID", "private@example.test")
        assert challenge.host == client.host == "other.moto.5gencare.com"
        assert hosts == ["9.moto.5gencare.com", "other.moto.5gencare.com"]
        assert len(observed) == 2
        assert observed[0] == observed[1]
        assert "routing: redirect=1" in caplog.text
        assert "code request accepted" in caplog.text
        for secret in ("PRIVATEUUID", "private@example.test", "654321", challenge.host):
            assert secret not in caplog.text
        await asyncio.wait_for(finished.wait(), 1)
    finally:
        await client.close()
        listener.close()
        await listener.wait_closed()


@pytest.mark.parametrize(
    ("replies", "connections"),
    [
        ([["-6", "9.moto.5gencare.com"]], 0),
        ([["-6", "other.moto.5gencare.com"], ["-6", "9.moto.5gencare.com"]], 1),
        (
            [
                ["-6", "a.moto.5gencare.com"],
                ["-6", "b.moto.5gencare.com"],
                ["-6", "c.moto.5gencare.com"],
            ],
            2,
        ),
        ([["-6", "private.invalid"]], 0),
        ([["-6", "other.moto.5gencare.com.evil.invalid"]], 0),
        ([["-6"]], 0),
        ([["-6", "other.moto.5gencare.com", "extra"]], 0),
        ([["-10", "other.moto.5gencare.com"]], 0),
    ],
)
@pytest.mark.asyncio
async def test_otp_routing_rejects_loops_limits_and_untrusted_hosts(
    replies, connections
):
    """Only the exact vendor routing response can cause a bounded reconnect."""
    client = CloudClient(ssl.create_default_context())
    client._exchange = AsyncMock(side_effect=replies)
    client.connect = AsyncMock()
    client.close = AsyncMock()
    with pytest.raises(CloudError):
        await client.request_code("PRIVATEUUID", "private@example.test")
    assert client.connect.await_count == connections
    assert client.close.await_count == connections
    assert client._exchange.await_count == len(replies)
    expected = call("v3_otp", "PRIVATEUUID", "email", "private@example.test", "6")
    assert client._exchange.await_args_list == [expected] * len(replies)


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.asyncio
async def test_otp_routing_connect_failure_and_cancellation(cancel):
    """A redirect never retries an unavailable destination or swallows cancellation."""
    client = CloudClient(ssl.create_default_context())
    client._exchange = AsyncMock(return_value=["-6", "other.moto.5gencare.com"])
    client.close = AsyncMock()
    error = asyncio.CancelledError() if cancel else ConnectionRefusedError()
    client.connect = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await client.request_code("PRIVATEUUID", "private@example.test")
    client.close.assert_awaited_once()
    client.connect.assert_awaited_once()
    client._exchange.assert_awaited_once_with(
        "v3_otp", "PRIVATEUUID", "email", "private@example.test", "6"
    )


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ([], "invalid_count"),
        (["private-count@example.test"], "invalid_count"),
        (["9" * 5000], "invalid_count"),
        (["-1"], "count_out_of_range"),
        (["999999"], "count_out_of_range"),
        (["1"], "field_count_mismatch"),
    ],
)
def test_device_list_framing_logs_distinct_safe_reasons(fields, reason, caplog):
    """Count/framing failures never export arbitrary or oversized count values."""
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    with pytest.raises(DeviceListError) as caught:
        parse_devices(fields)
    assert f"reason={reason}" in caplog.text
    assert "received_fields=" in caplog.text
    for secret in ("private-count@example.test", "999999", "9" * 5000):
        assert secret not in caplog.text
        assert secret not in str(caught.value)


@pytest.mark.parametrize(
    ("index", "value", "reason"),
    [
        (0, "private-owner", "owner_id_not_decimal"),
        (0, "4294967296", "owner_id_out_of_u32_range"),
        (0, "9" * 5000, "owner_id_out_of_u32_range"),
        (4, "ab", "magic_token_length"),
        (4, "private-token-too-long-for-v1-credential", "magic_token_length"),
        (5, "ab", "device_id_length"),
        (5, "private-device-id-too-long-for-v1", "device_id_length"),
    ],
)
def test_device_list_record_logs_failed_rule_without_values(
    index, value, reason, caplog
):
    """Expose field positions/lengths and the exact rule, never credential contents."""
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    row = [
        "654321",
        "private-identifier",
        "VM85CONNECT",
        "private-camera-name",
        "private-token",
        "private-device-id",
        "private-extra",
    ]
    row[index] = value
    with pytest.raises(DeviceListError) as caught:
        parse_devices(["1", *row])
    assert "declared_count=1 received_fields=8 expected_fields=8" in caplog.text
    assert "model_family=VM85" in caplog.text
    assert "field_lengths=" in caplog.text
    assert "field_kinds=" in caplog.text
    assert f"reasons={reason}" in caplog.text
    for secret in (
        "654321",
        "private-identifier",
        "private-camera-name",
        "private-token",
        "private-device-id",
        "private-extra",
        "private-owner",
        "4294967296",
        "private-token-too-long-for-v1-credential",
        "9" * 5000,
    ):
        assert secret not in caplog.text
        assert secret not in str(caught.value)


def test_device_list_records_multiple_rejections_and_redacts_unknown_model(caplog):
    """A shifted schema can fail several rules without leaking its model field."""
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    row = [
        "private-owner",
        "private-id",
        "private-model@example.test",
        "private-name",
        "-1",
        "-1",
        "private-extra",
    ]
    with pytest.raises(DeviceListError):
        parse_devices(["1", *row])
    assert "model_family=other" in caplog.text
    assert (
        "reasons=owner_id_not_decimal,magic_token_length,device_id_length"
        in caplog.text
    )
    assert "negative_decimal" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize(
    "model", ["VM65", "VM65CONNECT", "VM85", "VM85CONNECT", "other-private-model"]
)
@pytest.mark.parametrize(
    "owner,token_length,device_length",
    [("0", 3, 3), ("4294967295", 27, 20), ("123456", 27, 30)],
)
def test_device_list_existing_credential_boundaries_remain_supported(
    model, owner, token_length, device_length, caplog
):
    """Accept original and 30-byte credentials without model gating or truncation."""
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    row = [
        owner,
        "private-id",
        model,
        "private-name",
        "x" * token_length,
        "y" * device_length,
        "-1",
    ]
    devices = parse_devices(["1", *row])
    assert len(devices) == 1
    assert devices[0].model == model
    assert devices[0].sid_device == row[5]
    assert "owner_fits_u32=True" in caplog.text
    assert "magic_token_length_valid=True device_id_length_valid=True" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize(
    "payload", [b"\xff\n", b"incomplete", b"reply private\n" * 65, b""]
)
async def test_reader_failure_keeps_a_bounded_queue_and_safe_error(payload, caplog):
    """Malformed or excessive cloud frames must end with a sanitized failure."""
    client = CloudClient(ssl.create_default_context())
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    caplog.set_level(logging.DEBUG, logger="custom_components.motorola_nursery")
    async with asyncio.timeout(1):
        await client._receive(reader)
    assert 1 <= client._messages.qsize() <= 64
    messages = []
    while not client._messages.empty():
        messages.append(client._messages.get_nowait())
    assert isinstance(messages[-1], ConnectionError)
    assert str(messages[-1]) == "Cloud connection closed"
    assert "private" not in caplog.text


@pytest.mark.parametrize("code", ["１２３４５６", "١٢٣٤٥٦", "12345", "123456\n"])
async def test_email_code_requires_exactly_six_ascii_digits_without_network(code):
    """Invalid email codes must be rejected before any network operation."""
    from custom_components.motorola_nursery.cloud import Challenge

    client = CloudClient(ssl.create_default_context())
    client._exchange = AsyncMock()
    challenge = Challenge("123456", client.host)
    with pytest.raises(ValueError, match="six-digit"):
        await client.submit_code(challenge, "SYNTHETICUUID", "owner@example.test", code)
    client._exchange.assert_not_called()
