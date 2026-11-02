# Development and maintenance

[Documentation index](README.md) · [Contribution guidelines](../CONTRIBUTING.md)

Run the commands below from the repository root. Module names refer to
[`custom_components/motorola_nursery`](../custom_components/motorola_nursery/).

## Development setup

The integration keeps three independent paths:

| Path | Modules | Responsibility |
| --- | --- | --- |
| Local video | `protocol.py`, `bridge.py`, `transcoder.py`, `camera.py` | MagicP2P tunnel, loopback RTSP bridge and on-demand FFmpeg relay. |
| Account access | `cloud.py`, `auth.py`, `config_flow.py` | Email-code login, session storage and credential recovery. |
| Camera data | `telemetry.py`, `coordinator.py`, `events.py`, entity platforms | Settings, polling and detection notifications. |
| Incoming LAN data | `f3.py`, `local.py` | Shared authenticated TCP listener, readings, controls and notifications; no vendor fallback. |

`__init__.py` owns setup and teardown through the entry's `RuntimeData`. Cloud
reads run in the background so they do not delay local camera setup. Keep
pending tasks tied to the entry/entity lifecycle and cancel them on unload.
See the [protocol reference](protocol.md) for wire formats and compatibility
boundaries.

Use Python 3.14 and a virtual environment:

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
```

The pinned HA test plugin selects compatible Home Assistant, pytest,
pytest-asyncio and coverage versions. Update that plugin rather than adding
independent pins for those packages. Shared tools live in
`requirements-test-tools.txt`.

CI tests the minimum supported HA and the version selected by
`requirements-test.txt`. The minimum plugin pin lives in
`requirements-test-minimum.txt`; update it only when intentionally raising the
documented minimum HA version. Both CI environments use complete, hash-locked
Linux x86_64 / Python 3.14 dependencies. See
[lock maintenance](sonarqube.md#dependency-lock-maintenance) before updating pins.
To reproduce either CI environment, create a separate virtual environment:

```sh
python -m pip install --require-hashes --only-binary=:all: -r requirements-test-build.lock
python -m pip install --require-hashes --only-binary=:all: --no-binary=mock-open,pyric --no-build-isolation -r requirements-test-current.lock
python -m pip check
```

Replace `current` with `minimum` for the compatibility suite. The two explicitly
allowed source packages have no published wheels; their archives and build tools
are hash-locked, and build isolation is disabled to prevent unpinned build-time
downloads. Do not widen that exception without reviewing the dependency.

For code changes, run the complete local validation before opening a pull request:

```sh
.venv/bin/ruff check custom_components tests scripts
.venv/bin/ruff format --check custom_components tests scripts
.venv/bin/bandit -q -r custom_components/motorola_nursery scripts
.venv/bin/coverage run -m pytest -q
.venv/bin/coverage report --fail-under=80
```

Tests must not contact Motorola services, send email, control a real camera, or
depend on personal credentials. Use synthetic protocol fixtures and reserved
documentation addresses.

Cover failure and cancellation paths as well as successful responses. In
particular, check that cloud failures preserve local video, delayed discovery
creates no duplicate entities, viewer disconnects do not trigger credential
recovery, and unload closes clients, background tasks and FFmpeg processes.

## Dependency pull requests

Dependabot updates for pip and GitHub Actions are eligible for squash auto-merge,
including major updates. The privileged `dependabot-automerge.yml` workflow only
enables GitHub's native auto-merge for non-draft Dependabot PRs originating in this
repository; it never checks out or executes PR code.

The `Protect main` repository ruleset requires an up-to-date branch, passing HACS,
Hassfest, both HA test environments, both CodeQL analyses, the CodeQL result and
CodeRabbit, resolved review conversations, and one approving review. CodeRabbit
is configured to review Dependabot PRs and automatically approve after its feedback
is resolved and the latest commit is reviewed. Missing or failing required checks
block merging. There are no bypass actors. These merge requirements apply to all
PRs; automatic enabling of auto-merge is limited to Dependabot.

The required check names and application bindings live in GitHub repository
settings. Keep them aligned when adding or renaming CI jobs. If a dependency PR
falls behind `main`, update its branch and wait for checks and review again.

## HACS distribution and releases

The repository is included in the [default HACS catalog](https://github.com/hacs/default/blob/master/integration).
Users can find Motorola Nursery in HACS without adding a custom repository.

Keep HACS and Hassfest validation enabled without ignored checks. The scheduled
validation also checks for changes in upstream requirements.

For a release, update `custom_components/motorola_nursery/manifest.json` and
`CHANGELOG.md`, let validation pass for the release commit, then publish a GitHub
release with a matching version tag (`vX.Y.Z` for manifest version `X.Y.Z`).
[HACS uses published releases](https://www.hacs.xyz/docs/publish/start/#versions);
pushing a tag alone does not publish an update. Normal releases do not require
another catalog submission.

Use beta prereleases for diagnostic iterations that still need device-owner
testing. Keep manifest and tag aligned (for example `0.5.6b1` and `v0.5.6b1`),
publish with GitHub's prerelease flag and do not mark the beta as latest.
Increment `b2`, `b3`, etc. for further iterations; publish the final patch version
after validation. Beta releases follow the same PR review and CI requirements.
Include beta installation and stable rollback instructions in the release notes;
see [troubleshooting](troubleshooting.md#testing-a-beta-release).

When changing the minimum Home Assistant version, keep `hacs.json`, the README
and the minimum CI test environment aligned. Preserve the integration domain
and repository path used by existing installations.

## Live validation

Only test against a camera you own and when it is free for testing. Back up the
integration files and config entry privately before installation. Check startup
automations before restarting HA; they can trigger lights or audio.

- Verify snapshot/HLS, telemetry after a polling interval and detection-channel
  availability. Entities should share one device and retain their unique IDs.
- Reload the entry and confirm video, telemetry and events recover. Use
  diagnostics to check connection availability and process counts.
- Simulate outages and invalid credentials in automated tests. Do not force a
  real token expiry or disable connectivity while the camera is monitoring.
- Exercise settings, PTZ and lullabies only in an explicit control-testing
  window, restoring original values afterward. Do not exercise firmware, reset,
  storage formatting, shell access or arbitrary commands.

On HAOS, an auxiliary HA process needs a separate config directory and must
remove all `SUPERVISOR*` and `HASSIO*` environment variables before loading HA,
including `SUPERVISOR_CORE_API_SOCKET`. Load only required integrations and
ensure `hassio` is absent, so the test cannot register with the real Supervisor.

For rollback, restore the previous integration directory and restart HA. Restore
config-entry data only with HA stopped and only if necessary: an old saved cloud
token cannot undo vendor-side rotation and may require a new email login.

Report tested HA/model/firmware versions and untested paths in the PR. Keep
session transcripts, timings and local deployment notes outside the repository.

## SonarQube Cloud

See [SonarQube Cloud and coverage](sonarqube.md) for CI analysis, coverage artifacts and external-contribution limits.
