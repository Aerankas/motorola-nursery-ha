# Incoming LAN readings (experimental)

[Documentation index](../README.md)

After account setup, open **Reconfigure > Use local entity readings** and enable the
local transport. This requires network configuration: redirect only the camera's
outgoing TCP port **2278** to the Home Assistant host on TCP **2278**, and allow
its return traffic. The integration cannot install router rules. Ordinary access
to the camera's IP address is insufficient because the camera initiates this
connection. Both directions must pass through the redirecting gateway; a
separate HA return route may require source NAT for this connection. Keep the
listener private to your camera network.

This mode polls the same supported values every 60 seconds and receives detection
notifications over the authenticated camera connection. Existing entity IDs are
preserved. It reconnects using the credentials saved during account setup and
has no cloud fallback. Camera reset or account relinking can change those
credentials and require manual account setup again.

This mode supports readings, detection events and the same allowlisted setting
and lullaby commands as the cloud transport. Playlist discovery runs separately
from readings and retries after failures. Local video continues using the
camera's TCP port 77. LAN playback and volume were exercised on VM65CONNECT;
physical PTZ on models advertising those axes remains unverified.

See [architecture](../reference/architecture.md) for transport ownership and
[compatibility](compatibility.md) for model-specific limits.
