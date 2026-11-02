# Architecture

[Documentation index](../README.md)

## Runtime ownership

`__init__.py` owns each entry's runtime and cleanup. Local video, account access
and camera data are independent paths. Initial telemetry and playlist requests
run in the background so delayed responses do not block local camera startup.

| Path | Modules | Responsibility |
| --- | --- | --- |
| Video | `protocol.py`, `bridge.py`, `transcoder.py`, `camera.py` | Camera TCP tunnel, loopback RTSP bridge and on-demand FFmpeg relay. |
| Account | `cloud.py`, `auth.py`, `config_flow.py` | Email-code login, session storage and credential renewal. |
| Camera data | `telemetry.py`, `coordinator.py`, `events.py`, entity platforms | Allowlisted settings, capability discovery, polling, playlists and detections. |
| Incoming LAN | `f3.py`, `local.py` | Shared authenticated listener and camera-initiated readings, controls and events. |

## Playback

The camera's TCP port 77 tunnels its internal RTSP service. Private loopback
listeners feed FFmpeg and Home Assistant Stream. H.264 video is copied and PCMA
audio becomes AAC. Viewers share managed resources; telemetry outages do not
interrupt local video. Startup uses saved camera credentials.

## Account and camera data

Default camera data uses cloud transport. Readings share a 60-second poll and
do not rotate account sessions; detections use a persistent connection. Commands
are allowlisted and validated against camera-advertised access and bounds.
Settings are read back before state updates. Playlist loading is separate from
readings and does not play audio during discovery.

Account-connected entries can renew credentials after a failed local connection,
with a cooldown. Rotated cloud tokens are saved before further requests. Rejected
sessions request HA reauthentication; another email code requires submitting the
email form. Manual-only entries retain local video.

## Incoming LAN transport

The camera connects to HA TCP 2278 through an explicit router redirect. One shared
listener authenticates and routes registered camera connections. The local
adapter reuses allowlisted command and capability handling without creating a
cloud session. A disconnect invalidates live availability and cached capabilities;
entities retain identities while the camera reconnects. Playlist failure after
a fully consumed response leaves readings/events connected; framing errors close
the peer to prevent request/response desynchronization.

Local mode disables automatic cloud renewal and has no cloud fallback. See
[local readings](../guides/local-readings.md) for routing requirements.

## Cleanup

Unload cancels entry-owned tasks, listeners, clients, peer connections and FFmpeg
processes. Pending telemetry and playlist work must stop with the entry. Keep
blocking work off the event loop and bound buffering and retry work. Automated
checks use synthetic peers; see [testing](../development/testing.md).

See the [implemented protocol](protocol.md) for current transport formats and limits.
