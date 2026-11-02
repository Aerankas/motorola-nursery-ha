# Migration notes

[Documentation index](../README.md)

## Upgrading from duplicated setting sensors

This applies when upgrading from releases before **0.8.0** to **0.8.0 or newer**
with setting-mirror entities still registered. Entries already migrated do not
need this cleanup again.

The integration removes its 16 obsolete setting-mirror sensor/binary-sensor
registry entries automatically (bitrate only after telemetry confirms its
replacement control has a supported value). Existing control, measurement and
detection entity IDs remain unchanged; recorder/history data is not deleted.
Update references to the old mirrors before upgrading: use the corresponding
number, select or switch entity. In particular, configured bitrate is now shown
by the video-quality select (for example `640_kbit_s`) instead of the numeric
bitrate sensor. Cameras without a usable bitrate control keep their read-only
`videoKbps` sensor. The sensor returns if the replacement becomes unusable.
Both telemetry transports support advertised writable controls. Unsupported
legacy controls are retired after successful discovery; temporary telemetry
outages do not remove established controls.
