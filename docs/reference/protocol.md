# Implemented protocol

[Documentation index](../README.md)

This reference describes the implemented transport subset, not a vendor API
contract. Source modules define exact framing and limits; model and firmware
compatibility remain subject to the [compatibility guide](../guides/compatibility.md).

## Connections

| Path | Endpoint | Format and purpose |
| --- | --- | --- |
| Local video | Camera TCP 77 to internal RTSP 6667 | MagicP2P tunnel carrying RTSP and media. |
| Account | Vendor TCP 3388 with certificate-verified TLS | Newline-framed ASCII login, session renewal and device listing. |
| Default camera data | Routed vendor server TCP 2288 with certificate-verified TLS | Telemetry, settings, controls and detection events. |
| Incoming LAN data | Camera-initiated TCP 2278 redirected to HA | Authenticated registration, readings, allowlisted commands and detections. |

Account routes are restricted to supported `*.moto.5gencare.com` hosts. Video
and the incoming LAN mode do not depend on an active cloud telemetry connection.

## Local video

[`protocol.py`](../../custom_components/motorola_nursery/protocol.py) performs the
credential-derived tunnel handshake. It accepts the ten-byte `ok 0 dconn`
acknowledgement and strips an optional SID echo before decoding media bytes.
Send and receive maintain independent stateful transforms with ordering preserved.
The initial acknowledgement and first decoded response have bounded deadlines.
Private loopback RTSP and HTTP listeners feed HA's FFmpeg/Stream components.

## Account and camera data

[`cloud.py`](../../custom_components/motorola_nursery/cloud.py) implements
`v3_otp`, `v3_loginset`, `v3_session` and `v3_dlist`. Session resumption rotates
the token; the replacement is saved before the next request. Responses and
redirects are bounded and validated without logging credentials or raw payloads.

[`telemetry.py`](../../custom_components/motorola_nursery/telemetry.py) and
[`events.py`](../../custom_components/motorola_nursery/events.py) handle camera
capabilities, allowlisted writes and detections. Writes require advertised access
and usable bounds, then readback. Playlist discovery is independent of readings.
Default detections use a persistent connection rather than waiting for polling.

## Incoming LAN framing

[`f3.py`](../../custom_components/motorola_nursery/f3.py) accepts a 72-byte greeting
with a version header, ephemeral X25519 public key and nonce. NaCl Box protects
the random 1024-byte rolling table. Subsequent bytes use independent directional
rolling transforms and bounded newline framing; the format does not authenticate
each command. See [security boundaries](security.md).

[`local.py`](../../custom_components/motorola_nursery/local.py) authenticates saved
owner/camera identities, dispatches keepalives and detections, and reuses the
allowlisted telemetry parser. Framing/synchronization errors close the peer;
an unusable, fully consumed playlist response can retry without disconnecting
readings. Local mode never creates a cloud fallback session.
