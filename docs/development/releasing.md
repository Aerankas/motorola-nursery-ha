# Release preparation

The repository is included in the [default HACS catalog](https://github.com/hacs/default/blob/master/integration).
Users can find Motorola Nursery in HACS without adding a custom repository.

Keep HACS and Hassfest validation enabled without ignored checks.

For a release, update `custom_components/motorola_nursery/manifest.json` and
`CHANGELOG.md`, let validation pass for the release commit, then push the matching
version tag (`vX.Y.Z` for manifest version `X.Y.Z`). Run **Publish integration
release** from the `main` branch in Actions, enter the existing tag and choose
whether it is a prerelease. The workflow builds the ZIP, creates a draft release,
attaches the package, and publishes only after the upload succeeds.
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

## HACS release asset and download count

`hacs.json` selects `motorola_nursery.zip` from each new release. The manual
**Publish integration release** workflow checks that the existing tag matches
the manifest and builds a ZIP containing only the integration files, with
`manifest.json` at the archive root. Use this workflow instead of publishing an
asset-less release through the GitHub release editor. It never creates tags,
replaces assets or modifies an existing release.

If package creation fails, no release is created. If upload fails, the release
stays a draft and is not offered by HACS. Inspect and remove only that unpublished
draft before retrying the workflow; existing releases/assets are never overwritten.
Do not manually publish the draft while the workflow is running.

HACS derives the displayed download count from the selected release asset's
GitHub download counter. This is an asset-download count, not an installation
count or a historical total. Older source-only releases do not gain retrospective
counts; they retain their original HACS manifests and installation behavior.
The default branch is hidden from the HACS version picker because it has no
published ZIP asset. Use stable releases for normal installations and the beta
selector for prereleases. Download source directly for development.

References:

- [HACS integration distribution](https://www.hacs.xyz/docs/publish/integration/)
- [HACS release-asset counter implementation](https://github.com/hacs/integration/blob/main/custom_components/hacs/repositories/base.py)
