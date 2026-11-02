<p align="center">
  <img src="custom_components/motorola_nursery/brand/icon.png" alt="Motorola Nursery icon" width="128">
</p>

<h1 align="center">Motorola Nursery for Home Assistant</h1>

<p align="center">Local video and camera entities for compatible Motorola Nursery cameras.</p>

[![Validate](https://github.com/eduardobittencourt/motorola-nursery-ha/actions/workflows/validate.yml/badge.svg)](https://github.com/eduardobittencourt/motorola-nursery-ha/actions/workflows/validate.yml)
[![HACS Default](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=eduardobittencourt&repository=motorola-nursery-ha&category=integration)
[![License MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[Documentation](docs/README.md) · [Releases](https://github.com/eduardobittencourt/motorola-nursery-ha/releases) · [Changelog](CHANGELOG.md)

## Features

- Native camera playback and snapshots with local H.264 video and AAC audio.
- Temperature and Wi-Fi signal sensors, plus motion and sound detection entities.
- Camera-advertised settings such as night vision, brightness, video quality,
  speaker volume and detection sensitivity.
- Lullaby media player for supported built-in tracks.
- Optional experimental [incoming LAN readings](docs/guides/local-readings.md).

Video and audio travel over the LAN independently of telemetry. The default
transport uses the cloud for readings, detections and controls. Entity availability
follows the camera's reported capabilities; see [entities and controls](docs/guides/entities.md).

## Requirements

- **Home Assistant 2026.9.3 or newer**, with FFmpeg available.
- A compatible Motorola Nursery camera using the 5GenCare MagicP2P tunnel.
- A Motorola Nursery account and access to its email for setup.
- A known camera IP address or hostname reachable from HA on TCP port **77**.
- Internet access for account setup and the default telemetry transport.

## Installation

Available in the **default HACS catalog**.

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=eduardobittencourt&repository=motorola-nursery-ha&category=integration)

1. Open **HACS**, search for **Motorola Nursery** and download the latest stable release.
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Add integration → Motorola Nursery**.

See [installation](docs/guides/installation.md) for manual setup,
[configuration](docs/guides/configuration.md) for credential renewal and
[migration notes](docs/guides/migrations.md) for older entity layouts.

## Configuration

Enter the email used in Motorola Nursery, submit the six-digit email code,
select the camera and enter its local IP address or hostname. The integration
obtains camera credentials and checks the local connection.

Use **Reconfigure** to change the address or reconnect the account. Existing
manual-only entries retain local playback; connect an account to discover
additional entities. Experimental LAN readings require a camera-initiated
connection and explicit router configuration; ordinary camera IP access is
insufficient. Follow the [local readings guide](docs/guides/local-readings.md).

## Compatibility and limitations

Local playback has been verified on **VM65CONNECT** and reported by a
**VM85CONNECT** owner. Firmware and per-feature results are recorded in the
[compatibility guide](docs/guides/compatibility.md).

- Support varies by model and advertised capabilities. PTZ movement on models
  exposing writable axes remains unverified; talkback is not integrated.
- Camera address discovery is not implemented.
- Cloud outages affect the default telemetry transport; local video remains independent.
- Incoming LAN readings have no cloud fallback or automatic credential renewal.
  Cold boot and prolonged offline operation still need validation.
- Long-term session expiry, other regions, account sharing and concurrent phone
  or multiple-HA use remain unverified.
- Recovery checks the tunnel handshake; internal RTSP rejection alone does not
  trigger credential renewal.

## Support and security

Start with [troubleshooting](docs/guides/troubleshooting.md) and use the repository's
issue forms for reproducible reports. Remove account identifiers, camera
addresses, stream URLs, email codes, tokens and credentials from logs.

Session and camera credentials are stored in Home Assistant's config entry;
the email code is not saved. Protect HA configuration and backups. Diagnostics
omit account and device data. Report vulnerabilities privately through
[SECURITY.md](SECURITY.md).

## Contributing

Compatibility reports, documentation fixes and code contributions are welcome.
See [CONTRIBUTING.md](CONTRIBUTING.md), [development](docs/development/testing.md)
and the [Code of Conduct](CODE_OF_CONDUCT.md).

## License and attribution

Original code is [MIT licensed](LICENSE). This independent interoperability
project is not affiliated with Motorola, 5GenCare or Binatone. No APK, vendor
library, packet capture or personal credentials are included. Shared RTSP
application constants are used only to construct local stream credentials.

Motorola and its logo are trademarks of Motorola Trademark Holdings, LLC.
Artwork identifies compatible products; see [brand assets](docs/reference/branding.md).
