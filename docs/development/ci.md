# CI and analysis

[Documentation index](../README.md)

`.github/workflows/validate.yml` defines HACS, hassfest and the minimum/current
HA test matrix, including hash-locked dependency installation, Ruff, Bandit and
coverage. Use the workflow and requirements files for exact pins and check names.
CodeQL analyzes Python and Actions in its own workflow.

Sonar imports current-HA coverage from the same workflow run and reads the
project version from the integration manifest.
Minimum-HA tests remain mandatory compatibility checks. Analysis settings live
in `sonar-project.properties`; review settings live in `.coderabbit.yaml`.
Credentials belong in GitHub secrets and must not be exposed to untrusted PR
code. Fork PRs need a safe test path without scanner credentials.

Dependabot updates are reviewed and merged manually; no workflow enables
dependency auto-merge. These merges do not publish releases. Repository settings
are authoritative for required reviews, branch freshness and merge protection.
Resolve conflicts, failing checks and review findings before merge.

Keep analyzer findings reviewable without weakening assertions or excluding
runtime modules to improve metrics. HA callbacks must preserve their awaitable
framework contract. Protocol constants and legacy formats need contextual
review; see [security](../reference/security.md). Synthetic tests must not depend
on real accounts, email delivery, camera credentials or private captures.

Use [testing](testing.md) to reproduce checks and maintain dependency locks.
Changes to minimum HA support must keep `hacs.json`, README and matrix aligned.
