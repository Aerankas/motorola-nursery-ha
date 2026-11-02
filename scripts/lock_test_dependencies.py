"""Compile HA test locks and record their complete dependency input digest."""

from __future__ import annotations

import argparse
import hashlib
import shlex
import shutil

# The generator invokes trusted uv directly, without a shell.
import subprocess  # nosec B404
import tempfile
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENTS = ("current", "minimum", "build")
DIGEST_PREFIX = "# input-sha256: "


def requirement_lines(
    path: Path, parents: frozenset[Path] = frozenset()
) -> Iterator[str]:
    """Expand includes while retaining extras, markers and direct references."""
    path = path.resolve()
    if path in parents:
        raise ValueError("Cyclic requirements include")
    for raw in path.read_text().splitlines():
        line = raw.partition(" #")[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            yield from requirement_lines(
                path.parent / line[3:].strip(), parents | {path}
            )
        else:
            yield line


def input_digest(source: Path) -> str:
    """Hash expanded input lines, including extras, markers and direct references."""
    expanded = "\n".join(requirement_lines(source)) + "\n"
    return hashlib.sha256(expanded.encode()).hexdigest()


def record_input_digest(
    source: Path, lock: Path, *, expected_digest: str | None = None
) -> None:
    """Replace lock metadata only after its dependency compilation succeeds."""
    digest = input_digest(source)
    if expected_digest is not None and digest != expected_digest:
        raise ValueError("Dependency inputs changed during compilation")
    lines = [
        line
        for line in lock.read_text().splitlines()
        if not line.startswith(DIGEST_PREFIX)
    ]
    content = DIGEST_PREFIX + digest + "\n" + "\n".join(lines) + "\n"
    # Dependency text is file content, never an output path.
    with lock.open("w", encoding="utf-8") as output:
        output.write(content)


def verify_lock_inputs(source: Path, lock: Path) -> None:
    """Reject a missing or stale digest of the complete dependency inputs."""
    expected = DIGEST_PREFIX + input_digest(source)
    if expected not in lock.read_text().splitlines():
        raise ValueError(f"Regenerate {lock.name}: dependency inputs changed")


def compile_lock(source: Path, lock: Path, command: list[str]) -> None:
    """Publish one complete lock only if its inputs stay stable during compilation."""
    digest = input_digest(source)
    with tempfile.TemporaryDirectory(prefix=".lock-", dir=lock.parent) as temporary:
        staged = Path(temporary) / lock.name
        if lock.exists():
            # Preserve uv's existing resolution unless an upgrade was requested.
            shutil.copyfile(lock, staged)
        command = command.copy()
        command[command.index("-o") + 1] = str(staged)
        # Only the fixed uv command and explicit CLI options are passed; no shell.
        subprocess.run(command, cwd=ROOT, check=True)  # nosec B603
        record_input_digest(source, staged, expected_digest=digest)
        staged.replace(lock)


def main() -> None:
    """Compile selected CI locks with pinned target settings and fresh metadata."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environments", nargs="*", choices=ENVIRONMENTS)
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument("--upgrade-package", action="append", default=[])
    args = parser.parse_args()
    for environment in args.environments or ENVIRONMENTS:
        source = (
            "requirements-test.txt"
            if environment == "current"
            else f"requirements-test-{environment}.txt"
        )
        lock = f"requirements-test-{environment}.lock"
        regeneration = ["python", "scripts/lock_test_dependencies.py", environment]
        options = []
        if args.upgrade:
            options.append("--upgrade")
        for package in args.upgrade_package:
            options.extend(["--upgrade-package", package])
        command = [
            "uv",
            "pip",
            "compile",
            source,
            "--python-version",
            "3.14.8",
            "--python-platform",
            "x86_64-manylinux_2_39",
            "--generate-hashes",
            "--custom-compile-command",
            shlex.join(regeneration + options),
            "-o",
            lock,
            *options,
        ]
        compile_lock(ROOT / source, ROOT / lock, command)


if __name__ == "__main__":
    main()
