# Contributor guidance

Start with [CONTRIBUTING.md](CONTRIBUTING.md) and
[Development and testing](docs/development/testing.md) for development and validation commands.
Use [docs/README.md](docs/README.md) to find user guides and technical references.
Use English for public code comments, documentation and review feedback.

- The product is the UI-configured Home Assistant integration in
  `custom_components/motorola_nursery`. Preserve local video independently of
  cloud telemetry and account availability.
- Keep incoming LAN and cloud semantics distinct; local mode must not silently
  fall back to cloud connections.
- Keep setup UI-driven. Preserve config entries and entity unique IDs across
  upgrades; cover persisted-data migrations with regression tests.
- Expose controls only for known fields with camera-advertised write access and
  usable bounds. Preserve command allowlists; do not add firmware, reset,
  formatting, debug-shell or arbitrary-command features.
- Keep blocking work off HA's event loop. Bound buffering and background work;
  close sockets, clients, tasks, threads and FFmpeg processes on cancellation and unload.
- Keep credentials, email codes, camera addresses and stream URLs out of logs,
  diagnostics, fixtures and contributions. Do not include vendor binaries or
  decompiled vendor source.
- Use synthetic camera/cloud peers for automated tests; do not contact real
  services or send login emails from CI. Record physical-device checks separately.
- Run Ruff, Bandit and the relevant pytest checks in the development guide and fix underlying failures
  without weakening assertions or bypassing CI requirements.

## Documentation maintenance

- Keep the root README focused on public Home Assistant use: features,
  requirements, installation, configuration, compatibility limits, support,
  contribution links and licensing. Use the existing brand icon and shared
  section order; preserve each project's actual capabilities and HACS route.
- Keep release announcements and version-specific implementation history in
  CHANGELOG.md. Keep supported upgrade procedures in docs/guides/. Report
  test results in PRs or release notes; do not keep investigation diaries,
  decompilation procedures, local lab setups or obsolete plans in docs/.
  Minimum supported HA versions and current limits belong in the README.
- Use the common docs/ layout: guides/ for users, reference/ for current
  architecture, security and branding, and development/ for testing, CI and
  releases. Use docs/README.md as the documentation map. Link to the authoritative guide
  rather than duplicating detailed configuration or developer commands.
- Keep AGENTS.md limited to contributor navigation and implementation guardrails.
  Keep local deployment notes, credentials, raw captures and work-session
  diaries out of public documentation.
- Update incoming links when moving material. Preserve required attribution
  and distinguish physical-device evidence from synthetic tests.
