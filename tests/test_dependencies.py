"""Dependency input changes must regenerate the hash-locked CI environments."""

import subprocess
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts import lock_test_dependencies
from scripts.lock_test_dependencies import (
    record_input_digest,
    requirement_lines,
    verify_lock_inputs,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("environment", ["current", "minimum", "build"])
def test_ci_lock_matches_dependency_inputs(environment):
    """Every CI lock must match complete input metadata and direct version pins."""
    source = (
        "requirements-test.txt"
        if environment == "current"
        else f"requirements-test-{environment}.txt"
    )
    lock = ROOT / f"requirements-test-{environment}.lock"
    verify_lock_inputs(ROOT / source, lock)
    locked = {
        canonicalize_name(req.name): req.specifier
        for line in lock.read_text().splitlines()
        if line and not line[0].isspace() and not line.startswith("#")
        for req in [Requirement(line.removesuffix("\\").strip())]
    }
    for line in requirement_lines(ROOT / source):
        requirement = Requirement(line)
        assert locked[canonicalize_name(requirement.name)] == requirement.specifier


@pytest.mark.parametrize(
    "new_requirement",
    ["bandit[sarif]==1.9.4", 'bandit==1.9.4; python_version >= "3.14"'],
)
def test_nested_extra_or_marker_change_requires_regeneration(tmp_path, new_requirement):
    """Nested extras and marker edits must invalidate otherwise unchanged locks."""
    source = tmp_path / "requirements.txt"
    tools = tmp_path / "tools.txt"
    lock = tmp_path / "requirements.lock"
    source.write_text("-r tools.txt\n")
    tools.write_text("bandit==1.9.4\n")
    # uv strips extras from compiled entries; the input digest retains them.
    lock.write_text("bandit==1.9.4\n")
    record_input_digest(source, lock)
    verify_lock_inputs(source, lock)
    tools.write_text(new_requirement + "\n")
    with pytest.raises(ValueError, match="dependency inputs changed"):
        verify_lock_inputs(source, lock)
    record_input_digest(source, lock)
    verify_lock_inputs(source, lock)


def test_missing_input_digest_requires_lock_regeneration(tmp_path):
    """Unstamped locks cannot silently bypass complete input drift detection."""
    source = tmp_path / "requirements.txt"
    lock = tmp_path / "requirements.lock"
    source.write_text("bandit==1.9.4\n")
    lock.write_text("bandit==1.9.4\n")
    with pytest.raises(ValueError, match="Regenerate"):
        verify_lock_inputs(source, lock)


def test_cyclic_requirements_are_rejected(tmp_path):
    """Recursive include cycles must fail explicitly rather than exhaust recursion."""
    source = tmp_path / "requirements.txt"
    source.write_text("-r requirements.txt\n")
    lines = requirement_lines(source)
    with pytest.raises(ValueError, match="Cyclic requirements"):
        next(lines)


def test_generator_records_metadata_after_successful_compilation(tmp_path, monkeypatch):
    """Successful compilation stamps each selected lock with its full input digest."""
    for source in (
        "requirements-test.txt",
        "requirements-test-minimum.txt",
        "requirements-test-build.txt",
    ):
        (tmp_path / source).write_text("bandit[sarif]==1.9.4\n")

    def compile_success(command, *, cwd, check):
        assert check
        output = command[command.index("-o") + 1]
        (cwd / output).write_text("bandit==1.9.4\n")

    monkeypatch.setattr(lock_test_dependencies, "ROOT", tmp_path)
    monkeypatch.setattr(lock_test_dependencies.subprocess, "run", compile_success)
    monkeypatch.setattr(
        "sys.argv",
        ["lock_test_dependencies.py", "--upgrade", "--upgrade-package", "bandit"],
    )
    lock_test_dependencies.main()
    for environment in ("current", "minimum", "build"):
        source = (
            "requirements-test.txt"
            if environment == "current"
            else f"requirements-test-{environment}.txt"
        )
        verify_lock_inputs(
            tmp_path / source, tmp_path / f"requirements-test-{environment}.lock"
        )


def test_failed_compile_does_not_mark_changed_inputs_as_locked(tmp_path, monkeypatch):
    """A compiler failure must leave the previous metadata stale and detectable."""
    source = tmp_path / "requirements-test.txt"
    lock = tmp_path / "requirements-test-current.lock"
    source.write_text("bandit==1.9.4\n")
    lock.write_text("bandit==1.9.4\n")
    record_input_digest(source, lock)
    previous = lock.read_bytes()
    source.write_text("bandit[sarif]==1.9.4\n")
    error = subprocess.CalledProcessError(1, ["uv"])

    def compile_failure(command, *, cwd, check):
        assert check
        output = command[command.index("-o") + 1]
        (cwd / output).write_text("partial compiler output")
        raise error

    monkeypatch.setattr(lock_test_dependencies, "ROOT", tmp_path)
    monkeypatch.setattr(lock_test_dependencies.subprocess, "run", compile_failure)
    monkeypatch.setattr("sys.argv", ["lock_test_dependencies.py", "current"])
    with pytest.raises(subprocess.CalledProcessError):
        lock_test_dependencies.main()
    assert lock.read_bytes() == previous
    with pytest.raises(ValueError, match="dependency inputs changed"):
        verify_lock_inputs(source, lock)


def test_inputs_changed_during_compilation_do_not_replace_previous_lock(
    tmp_path, monkeypatch
):
    """An extras-only edit during compilation must not stamp or publish stale output."""
    source = tmp_path / "requirements-test.txt"
    tools = tmp_path / "tools.txt"
    lock = tmp_path / "requirements-test-current.lock"
    source.write_text("-r tools.txt\n")
    tools.write_text("bandit==1.9.4\n")
    lock.write_text("bandit==1.9.4\n")
    record_input_digest(source, lock)
    previous = lock.read_bytes()

    def compile_with_concurrent_edit(command, *, cwd, check):
        assert check
        output = command[command.index("-o") + 1]
        (cwd / output).write_text("bandit==1.9.4\n")
        tools.write_text("bandit[sarif]==1.9.4\n")

    monkeypatch.setattr(lock_test_dependencies, "ROOT", tmp_path)
    monkeypatch.setattr(
        lock_test_dependencies.subprocess, "run", compile_with_concurrent_edit
    )
    monkeypatch.setattr("sys.argv", ["lock_test_dependencies.py", "current"])
    with pytest.raises(ValueError, match="changed during compilation"):
        lock_test_dependencies.main()
    assert lock.read_bytes() == previous
    assert not list(tmp_path.glob(".lock-*"))


@pytest.mark.parametrize("failure", ["metadata", "publication"])
def test_failed_publication_preserves_previous_lock(tmp_path, monkeypatch, failure):
    """A staged-write or rename failure must preserve the entire previous lock."""
    source = tmp_path / "requirements-test.txt"
    lock = tmp_path / "requirements-test-current.lock"
    source.write_text("bandit==1.9.4\n")
    lock.write_text("bandit==1.9.4\n")
    record_input_digest(source, lock)
    previous = lock.read_bytes()

    def compile_success(command, *, cwd, check):
        assert check
        output = command[command.index("-o") + 1]
        (cwd / output).write_text("bandit==1.9.4\n")

    def fail_metadata(_source, staged, **_kwargs):
        staged.write_text("partial stamp")
        raise OSError("Synthetic metadata failure")

    def fail_rename(_self, _target):
        raise OSError("Synthetic publication failure")

    monkeypatch.setattr(lock_test_dependencies, "ROOT", tmp_path)
    monkeypatch.setattr(lock_test_dependencies.subprocess, "run", compile_success)
    monkeypatch.setattr("sys.argv", ["lock_test_dependencies.py", "current"])
    if failure == "metadata":
        monkeypatch.setattr(
            lock_test_dependencies, "record_input_digest", fail_metadata
        )
    else:
        monkeypatch.setattr(Path, "replace", fail_rename)
    with pytest.raises(OSError, match="Synthetic"):
        lock_test_dependencies.main()
    assert lock.read_bytes() == previous
    assert not list(tmp_path.glob(".lock-*"))
