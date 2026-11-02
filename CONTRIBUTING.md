# Contributing

Compatibility reports, documentation improvements and original code contributions
are welcome. Search existing issues and use the repository's bug, feature or
compatibility forms where available. Include model, firmware, HA/integration
versions, reproducible steps and the features actually tested.

## Before starting

Read [AGENTS.md](AGENTS.md) for implementation guardrails and
[architecture](docs/reference/architecture.md) for component responsibilities.
Do not post credentials, account/device identifiers, email codes, keys, signed
URLs, private images, raw captures or HA backups. Do not contribute vendor
binaries or proprietary source. Report vulnerabilities privately through
[SECURITY.md](SECURITY.md).

## Development

Fork the repository and create a focused branch from `main`. Follow
[development and testing](docs/development/testing.md) for the test environment,
commands and dependency sources. Use synthetic camera/service peers for tests;
CI must not require real accounts or devices. Keep physical-device observations
separate from automated test results.

Preserve config entries and entity identities across upgrades. Cover behavior
changes, migrations, cancellation and resource cleanup with meaningful regression
tests. Keep logs and diagnostics private-data-safe. Preserve license notices.

## Documentation

Write public documentation and code comments in English. Use
[docs/README.md](docs/README.md) to choose a location: `guides/` for usage,
`reference/` for current architecture, security and attribution, `development/`
for testing, CI and releases. Update incoming links when moving a guide.

Keep documentation aligned with the implemented behavior. Keep supported migration
steps and compatibility limits; put release history in [CHANGELOG.md](CHANGELOG.md)
and test results in PRs or release notes. Do not add investigation diaries,
app-analysis instructions, local lab setups or obsolete plans.

## Pull requests and releases

Explain the user-visible problem, resulting behavior and checks performed.
Keep changes focused and resolve failing checks and review findings before merge.
Repository settings and workflows define the current required checks/reviews;
do not bypass them or weaken assertions to obtain passing results.

Dependency updates must preserve HA compatibility and refresh relevant locks.
They do not publish releases automatically. Follow
[CI and analysis](docs/development/ci.md) and
[release preparation](docs/development/releasing.md).

By participating, follow the [Code of Conduct](CODE_OF_CONDUCT.md). Contribute
only work you are authorized to publish under the project's [license](LICENSE).
