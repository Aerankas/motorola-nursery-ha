# Changelog

## Unreleased

- Simplify control parsing, notification dispatch and transport setup while
  preserving credentials, entity identities and LAN/cloud isolation.
- Strengthen malformed-frame, queue-limit and input-validation regression tests.
- Install complete hash-locked HA test environments in CI, with reviewed source
  exceptions and pinned build tools.

## 0.8.0 — 2026-11-01

- Discover controls from individual camera capabilities, write access and bounds,
  including per-axis PTZ instead of inferring it from the VM65 model name.
- Retire unsupported legacy controls only after successful discovery; preserve
  established identities across transient failures and capability changes.
- Enable the shared setting and lullaby protocol on incoming LAN connections.
  Reconnects invalidate capability and playlist caches; there is no cloud fallback.
- Fix the empty LAN playlist: load advertised tracks separately from telemetry,
  retry failed discovery even when readings are unchanged, and expose the player
  only after a valid playlist. Derive playback/stop/volume features from capabilities.
- Keep LAN readings and events connected after a fully consumed but invalid
  playlist reply; continue closing the peer after transport/framing failures.
- Reject read-only writes and false PTZ acknowledgements; stop sending the
  unadvertised audio-timer command. No playback starts during discovery.

## 0.7.0 — 2026-10-31

- Accept the optional fifth metadata field on incoming camera detection
  notifications. Acknowledge these frames and dispatch the motion/sound event
  without exposing the opaque hint. Keep app-facing cloud framing unchanged.
- Remove 16 sensor/binary-sensor mirrors of camera settings. Number, select and
  switch entities continue to expose current configuration values.
- Retain the read-only bitrate sensor when no bitrate control is advertised.
- Keep temperature, Wi-Fi quality and real motion/sound detections. Retire only
  the current entry's obsolete registry identities; preserve control IDs and
  recorder history. References to removed mirrors must use their controls.

## 0.6.0 — 2026-10-31

- Add an opt-in incoming LAN transport for camera readings and detection events,
  using account credentials saved during setup without cloud fallback.
- Preserve existing entity identifiers and local video; add authenticated F3
  sessions, disconnect handling and reconnect coverage.
- Expose local mode in reconfiguration and document required TCP 2278 routing.
  Setting writes, PTZ and lullaby controls are unavailable in this mode.

## 0.5.6 — 2026-10-27

- Complete VM85CONNECT local setup and video compatibility: accept the ten-byte
  MagicP2P acknowledgement recognized by the Android app instead of requiring
  an echoed SID. A VM85 owner confirmed setup and live video on firmware
  V1.6.45 / Home Assistant 2026.10.4; temperature and motion appeared correct in
  an initial check. VM85 controls, PTZ, audio and prolonged operation remain
  unverified.
- Preserve extended VM65 acknowledgements and fragmented/coalesced cipher data.
  Bound the first decoded response wait to eight seconds and release stalled
  tunnel connections without imposing that deadline on established video.
- Include privacy-safe local connection diagnostics from the betas. Remove
  obsolete initial ACK SID-match/delimiter fields and share the ACK prefix
  constant between validation and diagnostics.
- Document model-specific validation and complementary public protocol research.
  The protocol candidate passed 176 tests in both HA environments and an isolated
  physical VM65 video regression before promotion; see the beta history below.

## 0.5.6b2 — 2026-10-27

- Accept the ten-byte local `ok 0 dconn` acknowledgement recognized by the
  Android app, without waiting for an echoed SID before sending RTSP.
- Preserve extended VM65 acknowledgements by stripping their optional SID echo
  before cipher decoding, including fragmented and coalesced stream data.
- Bound the first post-ACK receive to eight seconds, including partial SID
  echoes and cipher prefixes, without imposing a deadline on established video.
- Add short/long ACK loopback round trips with 30-byte device identifiers,
  delayed SID echoes and fragmented cipher prefixes. VM85 operation still
  requires confirmation on the reporter's camera.

## 0.5.6b1 — 2026-10-27

- Add debug diagnostics for local TCP connection, handshake send/receive and
  response validation, separating DNS, refusal, timeout, reset and incomplete
  responses without recording addresses, credentials or raw response bytes.
- Report bounded response lengths, field count, prefix shape, SID match and
  delimiter category, including partial replies before a timeout or EOF.
- Trace local setup/reconfiguration probes and cleanup; preserve the original
  failure when socket teardown also fails.
- Distribute this investigation as a beta prerelease. VM85 local connection
  and video remain unverified; no transport or authentication change is claimed.

## 0.5.5 — 2026-10-27

- Accept SID device values up to 30 bytes in account discovery and local tunnel
  validation, fixing the observed VM85CONNECT camera-list rejection.
- Preserve complete identifiers and the existing v1 SID algorithm, handshake
  and cipher; the native app uses 20 as a padding width, not a maximum size.
- Add synthetic native-app SID reference vectors, credential bounds and setup
  coverage for 20- and 30-byte identifiers. VM85 video remains unverified.

## 0.5.4 — 2026-10-27

- Add privacy-safe camera-list diagnostics: count/framing, field shapes and lengths,
  known model family, credential validation flags and exact failed rules.
- Distinguish unsupported camera-list data from network errors in setup, with
  English and Brazilian Portuguese guidance and authenticated retry preserved.
- Keep existing tunnel credential limits and camera compatibility unchanged;
  document VM85CONNECT manufacturer references and their protocol limits.
- Cover malformed counts, oversized IDs, credential boundaries, unknown model
  redaction and device-list retry without sending another email code.

## 0.5.3 — 2026-10-27

- Fix email-code requests for accounts routed to another Motorola server: treat
  the exact `-6 <vendor-host>` response as a redirect instead of a rejection.
- Reconnect with certificate-verified TLS and preserve the login identity, limit
  redirects, reject repeated/untrusted hosts, and discard old-server messages.
- Cover redirected requests, stale frames, loops, limits, failed connections,
  cancellation and log privacy with synthetic tests. Document account routing.

## 0.5.2 — 2026-10-26

- Add privacy-safe account debug logs for connection failures, response structure,
  vendor rejection statuses, login routing and request duration.
- Include integration and HA versions in setup debug logs and diagnostics.
- Replace the generic code-request cooldown advice with troubleshooting guidance;
  document log collection before first setup completes and region limitations.
- Organize protocol, development/validation and brand documentation under
  `docs/`, with an index and updated links from the README and contributor guide.
- Preserve the local telemetry investigation's results and limits in the protocol
  reference, replacing the link to a private research path with a public summary.
- Update the HACS badge and installation guidance to reflect inclusion in the
  default catalog, and remove outdated version announcements from the README.
- Document HACS distribution and release maintenance for contributors.

## 0.5.1 — 2026-10-20

- Transform camera stream chunks in executor workers instead of blocking the
  event loop, keeping cipher state and transport operations ordered per direction.
- Lead installation instructions with searching for Motorola Nursery in HACS.
- Recover from camera tunnel errors and EOF during an active video stream while
  keeping ordinary local viewer disconnects quiet.
- Reset event reconnection backoff after authentication and bound connection
  setup and authentication with a shared deadline.
- Load initial telemetry and lullaby playlists in the background so cloud
  delays do not hold up local camera startup; cancel pending work on unload.
- Add privacy-safe event connection and video process counters to diagnostics.
- Cover transport failures, reconnects, slow cloud requests and cancellation;
  run CI against both minimum-supported and current Home Assistant versions.
- Update compatible test dependencies and CodeQL together, and prevent
  conflicting dependency pins and separate CodeQL action updates.
- Consolidate contributor/testing guidance and remove development-session diaries
  from the maintained documentation.

## 0.5.0 — 2026-09-27

- Add real-time motion and sound detection over the camera's authenticated,
  certificate-verified push channel.
- Add pulse-style binary sensors and native Home Assistant event entities so
  closely spaced detections remain individually usable in automations.
- Fire privacy-minimized `motorola_nursery_event` bus events containing only the
  configured device identifier, detection type and receive timestamp.
- Reconnect with bounded backoff, answer protocol keepalives and deduplicate
  replayed vendor notifications without interrupting local video or telemetry.
- Add parser, authentication, keepalive, privacy and deduplication tests.
- Harden optimized-runtime error handling and document the protocol-mandated
  non-security SHA-1 identifier explicitly for static analysis.
- Add security reporting, contribution guidelines, issue/PR templates,
  dependency updates, pinned CI actions, Bandit, coverage gating and CodeQL.

## 0.4.0 — 2026-09-27

- Add validated controls for night vision, image settings, quality, alert
  thresholds, sensitivities, alert switches and motion-zone enable states.
- Add short-step pan/tilt buttons with an unconditional stop and a return-to-origin
  button for the verified VM65CONNECT command set.
- Add a camera-side lullaby player with its 20 dynamically discovered built-in
  tracks, stop control and normalized speaker volume.
- Confirm quality levels as 160, 480, 640 and 1000 kbit/s and keep the diagnostic
  bitrate sensor synchronized immediately after changes.
- Strictly allowlist writable fields, values, playlist names and PTZ commands;
  firmware, reset, debug shell, storage formatting and song mutation remain blocked.
- Add synthetic protocol and full Home Assistant service lifecycle tests.

## 0.3.0 — 2026-09-27

- Add capability-based discovery of 18 read-only sensors/configuration states on
  VM65CONNECT, plus hardware/firmware information on the existing camera device.
- Poll the certificate-verified Motorola telemetry service once per minute
  without rotating account tokens or changing camera settings.
- Keep local video available during cloud telemetry failures; recover sensor
  discovery after startup outages and cancel polling on unload.
- Add protocol framing, invalid-value, TLS, no-write and HA lifecycle tests.
- Document the cloud dependency separately from the local video transport.

## 0.2.0 — 2026-09-25

- Add email-code login, camera selection and host setup in the Home Assistant UI.
- Add reauthentication and on-demand cloud credential renewal.
- Preserve existing manual configurations and RTSP overrides during migration.
- Add privacy-safe diagnostics and English and Brazilian Portuguese translations.
- Fix integration reload while camera streams are active.

## 0.1.0 — 2026-09-22

- Add the local MagicP2P-to-RTSP bridge and Home Assistant camera entity.
- Add on-demand H.264/AAC FFmpeg relay for native snapshots and HLS playback.
