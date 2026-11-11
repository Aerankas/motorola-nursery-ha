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

The minimum-HA requirements file is excluded from Dependabot version updates
because its test-plugin pin defines the supported compatibility baseline. Update
that pin deliberately when changing minimum HA support. Current-HA and shared
test-tool updates remain enabled; regenerate affected locks with their inputs.

Keep analyzer findings reviewable without weakening assertions or excluding
runtime modules to improve metrics. HA callbacks must preserve their awaitable
framework contract. Protocol constants and legacy formats need contextual
review; see [security](../reference/security.md). Synthetic tests must not depend
on real accounts, email delivery, camera credentials or private captures.

Use [testing](testing.md) to reproduce checks and maintain dependency locks.
Changes to minimum HA support must keep `hacs.json`, README and matrix aligned.

Required Sonar checks are `SonarQube scan` from GitHub Actions (app ID `15368`)
and `SonarCloud Code Analysis` from the SonarQube Cloud GitHub App (app ID
`12526`). The scanner uses `sonar.qualitygate.wait=true`, so analysis failures
and rejected quality gates fail the job. The native check independently reports
the quality gate on the PR. Keep both required check names and their expected
apps aligned with the published checks. Fork PRs run tests without scanner credentials. The scan in `Validate` is
skipped for forks; **Approved fork analysis** supplies a separate, protected
analysis on the original PR commit after maintainer approval.

## SonarQube Cloud setup

Use CI-based analysis with coverage and leave automatic analysis disabled.
The built-in **Sonar way** quality gate and the `previous_version` new-code
period are configured in SonarQube Cloud. CI supplies the integration manifest
version, a full Git history and coverage from the same workflow run. The
scanner's `sonar.qualitygate.wait=true` makes rejected gates fail the job.

Keep the SonarQube Cloud project bound to the correct GitHub repository and
allow the SonarQube Cloud GitHub App to access it. The native app publishes
`SonarCloud Code Analysis` and PR summaries separately from the Actions job.
`SonarCloud Code Analysis` is required by the merge rules.
If it stops appearing, investigate the binding and app access; a successful
scanner job alone does not prove that GitHub decoration works.

## Approved fork analysis

The `workflow_run` workflow in `.github/workflows/fork-sonar.yml` starts after
successful fork validation. It verifies the exact current head, the expected
validation jobs and the coverage artifact from that run, then creates a queued
`SonarQube scan` check on the original PR commit. No replacement PR is needed.

Configure the **sonar-fork-analysis** GitHub environment before enabling this
workflow: add a trusted maintainer as a required reviewer, disable administrator
bypass, and restrict deployment branches to protected branches. The preparation
job fails closed if this protection is absent. The workflow and helper execute
from the trusted default-branch revision, so the environment's branch restriction
applies to the workflow, not the fork. Keep `SONAR_TOKEN` in the existing GitHub
repository secret; do not copy it to a contributor or their fork.

When GitHub requests environment approval, review the PR and the exact SHA shown
in the run summary, including workflow, dependency and analysis-setting changes.
Approve the environment job to allow the scan. A new head requires new validation
and new approval. Closed PRs and changed heads are rejected again after approval;
results are checked once more before the original PR check is completed.

The privileged job treats the contribution as source data only. It does not
install its dependencies, import its Python modules, run its tests, execute its
scripts or restore its caches. Sources are fetched anonymously with hooks, filesystem monitors and LFS
filters disabled before materializing tracked files. No checkout action receives
a fork ref, and the source repository has no credentials.
The scanner uses `sonar-project.properties` from the trusted default branch,
explicit PR metadata and the approved SHA, overwriting any fork-supplied scanner
settings. Changes to that configuration require separate maintainer review and
will take effect after merge. The token is exposed only to the pinned scanner
step, after approval.

Coverage XML is produced by the unprivileged current-HA test job. Only the
expected bounded XML member is read from its artifact: no general extraction,
SQLite loading, DTDs, entities, absolute paths or paths outside the checkout are
accepted. Source symlinks and submodule configuration are rejected. Coverage is
still contributor-controlled evidence, so maintainer review of the tests and
workflow remains necessary; XML validation does not establish honest coverage.

The scanner waits for the quality gate. Its real outcome is published as
`SonarQube scan` on the approved SHA; skipped/failed scans never become successful
checks. The native Sonar app separately publishes `SonarCloud Code Analysis` on
that PR. Preserve both required checks and their app bindings. An environment
rejection or cancelled run can leave the queued check pending; reject/close the
PR or rerun validation for a new approved attempt, rather than marking it passed.
This path is available only after the trusted workflow has landed on the default
branch. Validate it on a controlled fork PR before relying on it for a merge.

PNG files in the integration's brand directory are excluded from textual
analysis to prevent UTF-8 warnings on binary images. Runtime source and
workflow files remain in scope; existing coverage and license boundaries are
unchanged.

References:

- [SonarQube Cloud with GitHub Actions](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/ci-based-analysis/github-actions-for-sonarcloud)
- [GitHub project integration](https://docs.sonarsource.com/sonarqube-cloud/managing-your-projects/administering-your-projects/devops-platform-integration/github)
