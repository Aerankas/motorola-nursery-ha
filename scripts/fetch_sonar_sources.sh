#!/usr/bin/env bash
# Materialize a public PR commit as data; never run its checkout hooks or filters.
set -euo pipefail

[[ "$GITHUB_REPOSITORY" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
[[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$BASE_BRANCH" == main ]]
[[ "$1" == candidate ]]
[[ ! -e "$1" ]]

# The fresh repository has no credentials, inherited templates or contributor
# configuration. Only GitHub.com is used as its anonymous read-only remote.
mkdir "$1"
git -C "$1" -c init.templateDir= init --quiet
git -C "$1" config core.hooksPath /dev/null
git -C "$1" config core.fsmonitor false
git -C "$1" config submodule.recurse false
git -C "$1" config filter.lfs.process ''
git -C "$1" config filter.lfs.smudge ''
git -C "$1" config filter.lfs.clean ''
git -C "$1" config filter.lfs.required false
git -C "$1" config protocol.file.allow never
git -C "$1" remote add origin "https://github.com/$GITHUB_REPOSITORY.git"
git -C "$1" fetch --no-tags origin "$HEAD_SHA" \
  "+refs/heads/main:refs/remotes/origin/main"
git -C "$1" read-tree --reset -u "$HEAD_SHA"
git -C "$1" update-ref --no-deref HEAD "$HEAD_SHA"
