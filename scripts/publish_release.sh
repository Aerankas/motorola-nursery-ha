#!/usr/bin/env bash
# A failed create/upload leaves no newly published, asset-less HACS release.
set -euo pipefail

[[ "$GITHUB_REPOSITORY" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
[[ "$RELEASE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+_-]*$ ]]
[[ "$RELEASE_PRERELEASE" == true || "$RELEASE_PRERELEASE" == false ]]
[[ -s "$1" ]]

# No --clobber and no update path: existing draft/published releases are preserved.
gh release create "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" \
  --verify-tag --draft --generate-notes --prerelease="$RELEASE_PRERELEASE"
gh release upload "$RELEASE_TAG" "$1" --repo "$GITHUB_REPOSITORY"
if [[ "$RELEASE_PRERELEASE" == true ]]; then
  gh release edit "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" \
    --draft=false --prerelease --latest=false
else
  gh release edit "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" \
    --draft=false --prerelease=false --latest
fi
