# Documentation

Start with the [project README](../README.md) for the integration overview.
These guides describe the supported implementation in this checkout.

## User guides

- [Installation](guides/installation.md): requirements, HACS, manual setup and updates.
- [Configuration](guides/configuration.md): account/device setup and reconfiguration.
- [Compatibility](guides/compatibility.md): tested devices and current limits.
- [Entities and controls](guides/entities.md): available entities and capability rules.
- [Troubleshooting](guides/troubleshooting.md): common failures and safe reports.
- [Migration notes](guides/migrations.md): procedures still needed by older installations.
- [Incoming LAN readings](guides/local-readings.md): optional router-dependent mode.

## Technical reference

- [Architecture](reference/architecture.md): component boundaries and lifecycle.
- [Implemented protocol](reference/protocol.md): current transports, framing and limits.
- [Security boundaries](reference/security.md): credentials, listeners and protocol limits.
- [Brand assets](reference/branding.md): artwork ownership and attribution.

## Development

- [Testing](development/testing.md): local checks and test-environment sources.
- [Release preparation](development/releasing.md): packaging, validation and publication.
- [CI and analysis](development/ci.md): workflow responsibilities and analysis boundaries.
- [Contributing](../CONTRIBUTING.md) and [contributor guidance](../AGENTS.md).

## Project policies

[Security reporting](../SECURITY.md) · [Code of Conduct](../CODE_OF_CONDUCT.md) ·
[License](../LICENSE) · [Changelog](../CHANGELOG.md)

## Documentation maintenance

Keep this tree limited to current usage and implementation. Update the relevant
guide alongside behavior changes and use code, manifests, dependency files and
workflows as the sources for exact settings. Keep supported migration steps and
compatibility limits explicit. Release history belongs in the changelog; test
results belong in PRs or release notes. Do not add investigation diaries, local
lab setups, app-analysis instructions, private captures or obsolete plans.
Update incoming links when moving or removing a document.
