# Installation and configuration

[Documentation index](../README.md)

## Installation

Requires Home Assistant 2026.9.3 or newer.

Motorola Nursery is included in the default HACS catalog; no custom repository
is needed. [Open Motorola Nursery in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=eduardobittencourt&repository=motorola-nursery-ha&category=integration)
or follow these steps:

1. Open HACS and search for **Motorola Nursery**.
2. Download **Motorola Nursery** and restart Home Assistant.
3. Open **Settings > Devices & services > Add integration > Motorola Nursery**
   and follow [account setup](configuration.md#account-setup).

For manual installation, copy `custom_components/motorola_nursery` into the
Home Assistant configuration directory and restart Home Assistant.

## Updates and removal

Update through HACS and restart Home Assistant. See [migration notes](migrations.md)
for retired setting-mirror sensors. Delete the entry in HA before uninstalling
its files through HACS; protect any backups containing account credentials.

After installation, follow [configuration](configuration.md).
