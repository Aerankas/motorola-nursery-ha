# Development and testing

[Documentation index](../README.md)

Run commands from the repository root. See [architecture](../reference/architecture.md)
for module responsibilities.

## Test environments

Use Python **3.14.8 or newer in the 3.14 series** on Linux x86-64 to reproduce
CI. The workflow defines minimum and current HA environments. Direct pins live
in `requirements-test*.txt`; the matching `.lock` files contain complete hashed
dependencies. The minimum plugin pin is in `requirements-test-minimum.txt`.
See [lock maintenance](#dependency-lock-maintenance) before updating pins.

Each environment installs hash-locked build tools first. Only `mock-open` and
`pyric` may build from source; their archives are hash-verified and build
isolation stays disabled. Other packages must have wheels.

### Current Home Assistant

Create `.venv`, install its dependencies and run the current-environment checks:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-test-build.lock
.venv/bin/python -m pip install --require-hashes --only-binary=:all: \
  --no-binary=mock-open,pyric --no-build-isolation -r requirements-test-current.lock
.venv/bin/python -m pip check
.venv/bin/ruff check custom_components tests scripts
.venv/bin/ruff format --check custom_components tests scripts
.venv/bin/bandit -q -r custom_components/motorola_nursery scripts
.venv/bin/coverage run -m pytest -q
.venv/bin/coverage report --fail-under=80
```

### Minimum Home Assistant

Before opening a code-change PR, also run the minimum-supported suite in
`.venv-minimum`. Every command below uses that environment's own executables:

```sh
python3.14 -m venv .venv-minimum
.venv-minimum/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-test-build.lock
.venv-minimum/bin/python -m pip install --require-hashes --only-binary=:all: \
  --no-binary=mock-open,pyric --no-build-isolation -r requirements-test-minimum.lock
.venv-minimum/bin/python -m pip check
.venv-minimum/bin/ruff check custom_components tests scripts
.venv-minimum/bin/ruff format --check custom_components tests scripts
.venv-minimum/bin/bandit -q -r custom_components/motorola_nursery scripts
.venv-minimum/bin/coverage run -m pytest -q
.venv-minimum/bin/coverage report --fail-under=80
```

Tests must not contact Motorola services, send email, control a real camera, or
depend on personal credentials. Use synthetic protocol fixtures and reserved
documentation addresses.

Cover failure and cancellation paths as well as successful responses. In
particular, check that cloud failures preserve local video, delayed discovery
creates no duplicate entities, viewer disconnects do not trigger credential
recovery, and unload closes clients, background tasks and FFmpeg processes.

## Dependency lock maintenance

Direct pins live in `requirements-test*.txt`; CI installs the complete hashed
`requirements-test-{current,minimum,build}.lock` files. Update inputs and locks
together. First install the complete [current HA environment](#current-home-assistant)
above, which includes the locked `uv` executable. The build-tool lock alone
contains only the tools needed for source-package builds; lock regeneration
requires the complete current environment. Activate it and regenerate with:

```sh
. .venv/bin/activate
python scripts/lock_test_dependencies.py
```

The generator targets Linux x86-64 and the Python patch level defined in its
source. Pass `current`, `minimum` or `build` for selected locks. Use `--upgrade`
only for an intentional full refresh or `--upgrade-package NAME` for a targeted
update. Review transitive changes and run both HA test environments.

Locks record a digest of recursively expanded inputs, including extras and
markers. The generator checks that inputs did not change during compilation and
publishes outputs atomically. Do not edit the digest manually. Metadata tests
reject input/lock drift. Only `mock-open` and `pyric` are source-build exceptions;
their archives and build tools stay hash-locked with build isolation disabled.
