# SonarQube Cloud, CodeRabbit and coverage

Analysis settings are versioned alongside the code; credentials remain in
GitHub secrets. Keep the existing lint, tests and security checks enabled.

## SonarQube Cloud

The existing test workflow records branch coverage with relative source paths.
The Sonar job downloads coverage from the same run, combines it, exports
`coverage.xml` and invokes the scanner. The integration manifest supplies
`sonar.projectVersion`; never substitute a build number or commit SHA.
Intelbras and Motorola publish the current-HA suite; minimum-HA tests remain
mandatory compatibility checks. Tuya combines Python and synthetic-media suites.

Keep the new-code definition at **Previous version**, use the same Python
quality profile and **Sonar way** quality gate in all three HA repositories.
Keep existing global coverage floors; the gate focuses on changed code.
Vendored Tuya code is excluded from coverage/duplication metrics, not review.

Service setup, in this order:

1. Register `SONAR_TOKEN` with analysis access as both an Actions secret and a
   Dependabot secret. Do not put its value in files or PR discussions.
2. Disable Automatic Analysis under Sonar project Administration > Analysis
   Method before enabling CI analysis. These modes cannot run together.
3. Verify main and PR analysis, coverage import and project version. The first
   main analysis may have no computed gate until a baseline exists.
4. Require the app-owned `SonarCloud Code Analysis` check only after confirming
   it is reported for the relevant PRs; keep all existing required checks.

Fork PRs run the test suites without the scanner credential. Before enabling a
universal Sonar required check, establish a trusted maintainer-branch analysis
path for outside contributions; do not run untrusted PR code with secrets via
`pull_request_target`. Dependabot uses its own secret namespace.

## Validation

Run the repository's normal checks and inspect that coverage paths resolve to
`custom_components/motorola_nursery`. For Tuya, include the real synthetic-media suite and
package checks; coverage must include both test jobs. Validate analysis on
configuration PRs before changing merge rules. Keep the three repos aligned in
policy without replacing their own test matrices or protocol acceptance tests.

## CodeRabbit

`.coderabbit.yaml` enables automatic incremental reviews, including Dependabot
PRs, skips drafts and disables automatic review pauses after multiple commits. Use English for review feedback, public code comments and documentation. Review status blocks on unresolved findings; automatic approval
is limited to moderate merge risk. Path instructions focus on HA lifecycle,
protocol correctness, tests and workflow security. AGENTS.md owns integration
constraints. Keep the existing CodeRabbit required check and review protection.
Gitar is not used for these repositories.

## Dependency lock maintenance

The readable `requirements-test*.txt` files own direct pins. CI installs the
matching `requirements-test-{current,minimum,build}.lock` files with mandatory
hashes. `tests/test_dependencies.py` fails if an input pin changes without the
matching lock being regenerated, including Dependabot updates; regenerate and
commit the affected locks before merging. Dependency updates must run both HA
suites and the existing security checks.

Each lock records a SHA-256 digest of its recursively expanded input requirements,
including extras and markers. This detects input changes even when uv strips
extras from compiled entries. The generator updates that metadata only after a
successful compilation, rejecting inputs that changed while compilation ran.
Each output is staged beside its destination and atomically replaced only after
metadata validation, preserving the previous lock on compiler, metadata-write or
publication failure. Do not update the digest by hand.

The generator targets Linux x86_64 and Python 3.14.8, matching the validated
interpreter patch level; a minor-only target would include patches older than
HA supports. With uv 0.13.5, regenerate all locks with:

```sh
python scripts/lock_test_dependencies.py
```

Pass `current`, `minimum` or `build` to regenerate selected environments. Review
changed transitive dependencies and hashes. Use `--upgrade` only for an
intentional full refresh, or `--upgrade-package NAME` for a targeted update.
The generator is included in Ruff, Bandit, coverage and Sonar analysis. Its
subprocess exceptions are local annotations for invoking the trusted uv executable
with an argument list and no shell. Tests cover successful metadata generation
and preserving stale metadata after compiler failure.

The build lock provides setuptools and wheel before installing source-only
`mock-open` and `pyric`; all other packages must have wheels. The scanner's
standalone coverage tool remains pinned and wheel-only.

## Findings that need framework or protocol context

Keep analyzer findings reviewable; do not change APIs or exclude source files
just to raise a rating. In particular:

- Home Assistant requires `async_setup_entry` hooks and listener `stop` methods
  to remain awaitable even where their current body has no suspension point.
  The `hass` argument is part of the platform setup contract.
- `vendor.py` contains public RTSP application defaults, already documented as
  protocol constants. The private per-camera token is still required. Renaming
  or hiding these constants would not improve authentication.
- `SETTING_MIRRORS` has tuple keys; unpacking an iteration yields the domain and
  key without retrieving dictionary values. An `items()` recommendation here
  does not apply.

Review those exact findings in Sonar with this context. Keep security and
quality gates enabled and retain regression tests for private-data filtering,
bounded queues, cancellation, malformed frames and local/cloud isolation.
