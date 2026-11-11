"""Exercise Git transport and release-publication boundaries without real services."""

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")
BASH = shutil.which("bash")


def clean_env():
    """Keep Git fixtures independent of personal credentials and global config."""
    return {
        **{
            key: value
            for key, value in os.environ.items()
            if key not in {"GH_TOKEN", "GITHUB_TOKEN", "SONAR_TOKEN"}
        },
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def git(*arguments, cwd, env=None):
    """Run real Git against synthetic repositories with bounded execution."""
    return subprocess.run(
        [GIT, *arguments],
        cwd=cwd,
        env=env or clean_env(),
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()


@pytest.fixture
def git_peer(tmp_path, socket_enabled):
    """Serve a synthetic bare Git repository exclusively over loopback."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-b", "main", cwd=seed)
    (seed / "payload.txt").write_text("synthetic source, not an LFS download\n")
    (seed / ".gitattributes").write_text("payload.txt filter=lfs\n")
    git("add", ".", cwd=seed)
    git(
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.test",
        "commit",
        "-m",
        "synthetic",
        cwd=seed,
    )
    sha = git("rev-parse", "HEAD", cwd=seed)
    export = tmp_path / "export"
    bare = export / "example/nursery.git"
    bare.parent.mkdir(parents=True)
    git("clone", "--bare", str(seed), str(bare), cwd=tmp_path)
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    daemon = subprocess.Popen(
        [
            GIT,
            "daemon",
            "--export-all",
            "--reuseaddr",
            "--listen=127.0.0.1",
            f"--port={port}",
            f"--base-path={export}",
            str(export),
        ],
        env=clean_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if daemon.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Synthetic Git daemon did not start") from None
                time.sleep(0.01)
        run = tmp_path / "run"
        run.mkdir()
        config = tmp_path / "gitconfig"
        # Rewrite only the transport in the fixture; production still constructs
        # its fixed anonymous GitHub URL, and file:// remains prohibited.
        git(
            "config",
            "--file",
            str(config),
            f"url.git://127.0.0.1:{port}/.insteadOf",
            "https://github.com/",
            cwd=tmp_path,
        )
        marker = tmp_path / "unexpected-code-execution"
        poison = tmp_path / "poison"
        poison.write_text(
            "#!/bin/sh\nprintf unexpected > " + shlex.quote(str(marker)) + "\nexit 1\n"
        )
        poison.chmod(0o700)
        templates = tmp_path / "templates"
        (templates / "hooks").mkdir(parents=True)
        shutil.copy2(poison, templates / "hooks/post-checkout")
        for key, value in {
            "init.templateDir": str(templates),
            "core.hooksPath": str(templates / "hooks"),
            "core.fsmonitor": str(poison),
            "filter.lfs.process": str(poison),
            "filter.lfs.smudge": str(poison),
            "filter.lfs.required": "true",
        }.items():
            git("config", "--file", str(config), key, value, cwd=tmp_path)
        env = {
            **clean_env(),
            "GIT_CONFIG_GLOBAL": str(config),
            "GITHUB_REPOSITORY": "example/nursery",
            "HEAD_SHA": sha,
            "BASE_BRANCH": "main",
        }
        yield {
            "run": run,
            "env": env,
            "sha": sha,
            "marker": marker,
            "config": config,
            "bare": bare,
        }
    finally:
        with suppress(ProcessLookupError):
            os.killpg(daemon.pid, signal.SIGTERM)
        daemon.wait(timeout=5)


def fetch(peer):
    """Execute the production script, not a mocked implementation of its guards."""
    return subprocess.run(
        [BASH, str(ROOT / "scripts/fetch_sonar_sources.sh"), "candidate"],
        cwd=peer["run"],
        env=peer["env"],
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_source_fetch_reads_real_git_blobs_without_running_hooks_or_filters(git_peer):
    """Poisoned templates, fsmonitor and LFS filters cannot execute during fetch."""
    result = fetch(git_peer)
    assert result.returncode == 0, result.stderr
    source = git_peer["run"] / "candidate"
    assert (source / ".git/HEAD").read_text().strip() == git_peer["sha"]
    assert (
        source / "payload.txt"
    ).read_text() == "synthetic source, not an LFS download\n"
    assert not git_peer["marker"].exists()
    assert not (source / ".git/hooks/post-checkout").exists()
    assert (
        git("config", "--get", "remote.origin.url", cwd=source)
        == "https://github.com/example/nursery.git"
    )
    assert git("config", "--get", "core.hooksPath", cwd=source) == "/dev/null"
    assert git("config", "--get", "protocol.file.allow", cwd=source) == "never"


def test_source_fetch_refuses_local_file_transport(git_peer):
    """A rewritten file URL cannot bypass the production transport restriction."""
    git(
        "config",
        "--file",
        str(git_peer["config"]),
        f"url.file://{git_peer['bare']}.insteadOf",
        "https://github.com/example/nursery.git",
        cwd=git_peer["run"],
    )
    result = fetch(git_peer)
    assert result.returncode != 0
    assert "transport 'file' not allowed" in result.stderr
    assert not (git_peer["run"] / "candidate/payload.txt").exists()


def test_source_fetch_does_not_materialize_an_unknown_commit(git_peer):
    """A valid-looking SHA absent from the remote cannot become scan input."""
    git_peer["env"]["HEAD_SHA"] = "f" * 40
    result = fetch(git_peer)
    assert result.returncode != 0
    assert not (git_peer["run"] / "candidate/payload.txt").exists()
    assert not git_peer["marker"].exists()


@pytest.mark.parametrize(
    "name,value",
    [
        ("GITHUB_REPOSITORY", "../outside/repo"),
        ("HEAD_SHA", "--upload-pack=bad"),
        ("BASE_BRANCH", "other"),
    ],
)
def test_invalid_fetch_inputs_fail_before_repository_creation(tmp_path, name, value):
    """Malformed inputs cannot start any network request or overwrite a path."""
    env = {
        **clean_env(),
        "GITHUB_REPOSITORY": "example/nursery",
        "HEAD_SHA": "a" * 40,
        "BASE_BRANCH": "main",
        name: value,
    }
    result = subprocess.run(
        [BASH, str(ROOT / "scripts/fetch_sonar_sources.sh"), "candidate"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert not (tmp_path / "candidate").exists()


@pytest.mark.parametrize("destination", ["../outside", "candidate"])
def test_fetch_never_replaces_an_existing_or_unexpected_destination(
    tmp_path, destination
):
    """The data checkout must be a fresh, fixed candidate directory."""
    source = tmp_path / "candidate"
    source.mkdir()
    marker = source / "keep"
    marker.write_text("preserve")
    env = {
        **clean_env(),
        "GITHUB_REPOSITORY": "example/nursery",
        "HEAD_SHA": "a" * 40,
        "BASE_BRANCH": "main",
    }
    result = subprocess.run(
        [BASH, str(ROOT / "scripts/fetch_sonar_sources.sh"), destination],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert marker.read_text() == "preserve"
    assert not (tmp_path.parent / "outside").exists()


@pytest.mark.parametrize("failure", ["none", "create", "upload"])
@pytest.mark.parametrize("prerelease", ["true", "false"])
def test_release_is_published_only_after_successful_asset_upload(
    tmp_path, failure, prerelease
):
    """A failed creation/upload cannot leave a published asset-less release."""
    binary = tmp_path / "bin"
    binary.mkdir()
    gh = binary / "gh"
    gh.write_text("""#!/usr/bin/env python3
import json,os,pathlib,sys
state=pathlib.Path(os.environ['RELEASE_TEST_STATE'])
rows=json.loads(state.read_text()) if state.exists() else []
operation=sys.argv[2]
rows.append(sys.argv[1:]);state.write_text(json.dumps(rows))
if operation==os.environ['RELEASE_TEST_FAILURE']:sys.exit(1)
if operation=='edit':pathlib.Path(os.environ['RELEASE_TEST_PUBLISHED']).touch()
""")
    gh.chmod(0o700)
    artifact = tmp_path / "motorola_nursery.zip"
    artifact.write_bytes(b"synthetic-package")
    state = tmp_path / "operations.json"
    published = tmp_path / "published"
    env = {
        **clean_env(),
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "GITHUB_REPOSITORY": "example/nursery",
        "RELEASE_TAG": "v0.8.1",
        "RELEASE_PRERELEASE": prerelease,
        "RELEASE_TEST_STATE": str(state),
        "RELEASE_TEST_PUBLISHED": str(published),
        "RELEASE_TEST_FAILURE": failure,
    }
    result = subprocess.run(
        [BASH, str(ROOT / "scripts/publish_release.sh"), str(artifact)],
        env=env,
        capture_output=True,
        timeout=5,
    )
    operations = json.loads(state.read_text())
    if failure == "none":
        assert result.returncode == 0
        assert published.exists()
        assert [row[1] for row in operations] == ["create", "upload", "edit"]
        assert "--draft" in operations[0]
        assert (
            "--latest=false" in operations[-1]
            if prerelease == "true"
            else "--latest" in operations[-1]
        )
    else:
        assert result.returncode != 0
        assert not published.exists()
        assert all(row[1] != "edit" for row in operations)
