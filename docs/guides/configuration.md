# Configuration

[Documentation index](../README.md)

## Account setup

1. Open **Settings > Devices & services > Add integration > Motorola Nursery**.
2. Enter the email used in Motorola Nursery, then the six-digit email code.
3. Select your camera and enter its local IP address or hostname.

The integration obtains the camera credentials and checks the local connection.
No packet capture, phone proxy, or manually copied token is needed.

For an existing installation, open the entry's **Reconfigure** menu and choose
account login. This preserves the entry and camera entity. Existing manually
configured entries continue working without account login. The same menu lets
you change the camera address.

Session and camera credentials are stored in Home Assistant's config entry;
the email code is not saved. Protect your HA configuration and backups as you
would other account credentials. Diagnostics omit account and device data.

## Playback and renewal

Video travels locally: TCP port 77 on the camera tunnels its internal RTSP
service on port 6667. Two private loopback listeners feed an on-demand FFmpeg
relay and Home Assistant's native Stream integration. H.264 video is copied;
PCMA audio is converted to AAC. No third-party WebRTC integration is required.

Startup uses saved camera credentials. In the default transport, if a local
video connection fails, an account-connected entry attempts cloud credential
renewal, with a cooldown
between attempts. Each successful session resumption rotates the cloud token;
the replacement is saved in the config entry before fetching devices.
A rejected session starts Home Assistant reauthentication. Sending another
email code always requires submitting the email form. Incoming LAN mode disables
automatic account renewal; credentials can be refreshed manually by account setup.

For optional incoming LAN readings, see [local readings](local-readings.md).
For older entity layouts, see [migration notes](migrations.md).
