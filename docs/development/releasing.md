# Release preparation

The repository is included in the [default HACS catalog](https://github.com/hacs/default/blob/master/integration).
Users can find Motorola Nursery in HACS without adding a custom repository.

Keep HACS and Hassfest validation enabled without ignored checks.

For a release, update `custom_components/motorola_nursery/manifest.json` and
`CHANGELOG.md`, let validation pass for the release commit, then publish a GitHub
release with a matching version tag (`vX.Y.Z` for manifest version `X.Y.Z`).
[HACS uses published releases](https://www.hacs.xyz/docs/publish/start/#versions);
pushing a tag alone does not publish an update. Normal releases do not require
another catalog submission.

Use beta prereleases for diagnostic iterations that still need device-owner
testing. Keep manifest and tag aligned (manifest `X.Y.ZbN`, tag `vX.Y.ZbN`),
publish with GitHub's prerelease flag and do not mark the beta as latest.
Increment `b2`, `b3`, etc. for further iterations; publish the final patch version
after validation. Beta releases follow the same PR review and CI requirements.
Include beta installation and stable rollback instructions in the release notes;
see [troubleshooting](../guides/troubleshooting.md#testing-a-beta-release).

When changing the minimum Home Assistant version, keep `hacs.json`, the README
and the minimum CI test environment aligned. Preserve the integration domain
and repository path used by existing installations.

## Device checks

Use an available camera and a private backup. Check playback/audio, polling,
detection availability, reload and preserved entity IDs. Exercise speaker or
setting controls only in an agreed testing window and restore their values.
Use synthetic tests for destructive failures, invalid credentials and outages.
Report model, firmware and limits in the PR or release notes; keep deployment
transcripts and captures outside the public documentation.
