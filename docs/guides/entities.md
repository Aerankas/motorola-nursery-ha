# Entities and controls

[Documentation index](../README.md)

## Sensors and camera information

The sensor entities expose temperature in °C and Wi-Fi signal quality in percent.
Hardware and firmware versions appear in the existing device information.
Configuration values are shown directly by their number, select and switch
entities; separate sensors no longer duplicate them. Motion and sound detection
binary sensors and event entities remain separate from sensitivity controls.

The sensors share one poll every 60 seconds, use the camera credentials already
stored in the entry and do not rotate the account session. The cloud client
validates TLS certificates; both transports send only allowlisted protocol
commands. Neither reads
Wi-Fi names, MAC addresses or sharing information.

Only advertised, supported capabilities become entities. Invalid individual
values become unavailable. Initial telemetry and playlist requests run in the
background so slow telemetry responses do not delay local camera startup.
Sensors appear when the selected telemetry transport becomes available. Existing manual-only
entries retain local playback; use **Reconfigure > Connect or sign in to the
Motorola account** to enable sensor discovery.

## Controls

The verified VM65CONNECT controls include:

- Night vision (off, on or automatic), 50/60 Hz anti-flicker mode and four video
  quality levels: 160, 480, 640 and 1000 kbit/s.
- Brightness, speaker volume, motion/sound sensitivity and low/high temperature
  thresholds.
- Image inversion, low/high temperature alerts, motion-zone mode and each of the
  four zone enable states.
- Pan/tilt and origin buttons only when their individual writable capabilities
  are advertised. The tested VM65CONNECT does not advertise them, and direct
  commands did not change its image; these buttons are not created for that model.
- A lullaby media player with 20 camera-advertised built-in tracks, volume and
  stop controls. Selecting a source starts playback on the camera speaker.

Controls require a known field, camera-advertised write access and usable
bounds. Numeric ranges and select options follow those bounds; model names
alone never enable PTZ. Existing controls become unavailable if their capability
is withdrawn. Every setting write is read back before HA updates its state.
The lullaby player appears after a valid playlist is loaded, and its selectable
sources, stop and volume features follow the corresponding capabilities.
Selecting a source starts playback; discovery itself does not play audio.

Firmware updates, reset/reboot, debug shell access, storage formatting, song
upload/removal and arbitrary protocol commands are deliberately blocked. The
microSD fields used by the app returned no data on the verified camera. Talkback
is not supported. See [compatibility](compatibility.md) for feature limits.

## Motion and sound detections

An account-linked camera exposes two binary sensors and two native event
entities for real motion and sound detections. Binary sensors turn on for ten
seconds and include a `last_detected` timestamp. Native event entities emit a
`detected` event for every notification, including repeated detections within
those ten seconds.

The integration also fires `motorola_nursery_event` on the Home Assistant event
bus with `event_type` equal to `motion` or `sound`. The payload contains no media
filename, URL, account token or camera token. Home Assistant automations can use
the binary sensors, native event entities or the bus event to send a mobile
notification. The integration itself does not choose a notification recipient.

In the default cloud mode, detections use one persistent certificate-verified
connection and normally arrive without waiting for the 60-second telemetry poll. The listener answers
camera keepalives, reconnects with bounded backoff and deduplicates replayed
notifications. Detection entities become unavailable while this cloud channel
is disconnected; local video remains independent.
