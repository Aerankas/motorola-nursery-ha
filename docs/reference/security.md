# Security boundaries

[Documentation index](../README.md)

Session and camera credentials are stored in the private HA config entry. Email
codes are not saved. Protect HA configuration and backups. Cloud TLS certificates
are verified; local camera protocol compatibility does not establish modern
end-to-end media encryption.

Internal media listeners use loopback. The optional incoming LAN listener uses
TCP 2278 and must remain private to the camera network. Limit router redirects
to the intended camera connection; do not publish this listener to the internet.
The initial NaCl Box authenticates the session rolling table, but the subsequent
legacy rolling transform does not authenticate each command. Session setup
therefore does not establish per-message integrity; restrict access to the
trusted camera network. See [protocol reference](protocol.md).

Only known, advertised and bounded controls are exposed. Firmware updates, reset,
formatting, shell access and arbitrary commands are blocked. Account renewal is
serialized and disabled in local-only readings mode. No telemetry outage should
trigger unrelated camera actions or break local video.

Logs and diagnostics omit account/device credentials, identifiers and private
addresses. Review excerpts before publication; other HA integrations can log
private data. Never attach raw captures, vendor binaries or private media.
Report vulnerabilities through [SECURITY.md](../../SECURITY.md).
