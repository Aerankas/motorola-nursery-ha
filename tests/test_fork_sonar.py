"""Fork analysis never executes contributor code or trusts stale approvals."""

import json
import stat
import zipfile
from copy import deepcopy
from unittest.mock import Mock

import pytest

from scripts import fork_sonar as scan

SHA = "a" * 40
REPO = "example/nursery"
EXPECTED_JOBS = {"Tests (current)", "Tests (minimum)", "HACS", "Hassfest"}
XML = (
    b"<coverage><sources><source>.</source></sources><packages><package>"
    b'<classes><class filename="scripts/example.py"/></classes>'
    b"</package></packages></coverage>"
)


@pytest.fixture
def context(tmp_path, monkeypatch):
    """Provide trusted workflow metadata and an explicit protected environment."""
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("BASE_BRANCH", "main")
    monkeypatch.delenv("BASE_SHA", raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.setenv(
        "ANALYSIS_RUN_URL", "https://github.com/example/nursery/actions/runs/2"
    )
    monkeypatch.setenv("GH_TOKEN", "synthetic-github-token")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")
    pr = {
        "number": 17,
        "state": "open",
        "head": {
            "sha": SHA,
            "repo": {"full_name": "contributor/nursery"},
            "ref": "feature",
        },
        "base": {"repo": {"full_name": REPO}, "ref": "main", "sha": "c" * 40},
    }
    environment = {
        "protection_rules": [{"type": "required_reviewers", "reviewers": [{"id": 7}]}],
        "can_admins_bypass": False,
        "deployment_branch_policy": {"protected_branches": True},
    }
    event = {
        "repository": {"default_branch": "main"},
        "workflow_run": {
            "id": 12,
            "event": "pull_request",
            "conclusion": "success",
            "head_repository": {"full_name": "contributor/nursery"},
            "head_sha": SHA,
            "path": ".github/workflows/validate.yml",
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    for name, value in {"PR_NUMBER": "17", "HEAD_SHA": SHA, "CHECK_ID": "88"}.items():
        monkeypatch.setenv(name, value)
    return {"pr": pr, "environment": environment, "event": event, "path": event_path}


def archive(tmp_path, raw=XML, member="coverage.xml", symlink=False):
    """Build synthetic coverage data without touching a real Actions artifact."""
    path = tmp_path / "coverage.zip"
    with zipfile.ZipFile(path, "w") as package:
        info = zipfile.ZipInfo(member)
        if symlink:
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        package.writestr(info, raw)
    return path


def candidate(tmp_path):
    """Create an isolated source checkout with deliberately hostile settings."""
    root = tmp_path / "candidate"
    (root / ".git").mkdir(parents=True)
    (root / ".git/HEAD").write_text(SHA)
    (root / "scripts").mkdir()
    (root / "scripts/example.py").write_text(
        "raise AssertionError('MUST NOT EXECUTE')\n"
    )
    (root / "sonar-project.properties").write_text(
        "sonar.host.url=https://invalid.example\n"
    )
    module = root / "custom_components/motorola_nursery"
    module.mkdir(parents=True)
    (module / "manifest.json").write_text(json.dumps({"version": "0.8.0"}))
    return root


def test_prepare_queues_only_exact_successful_fork_run(context, monkeypatch):
    """Empty workflow_run PR arrays are resolved through the current PR head."""

    def pages(path, key=None):
        if "/pulls?" in path:
            return iter([context["pr"]])
        if key == "jobs":
            return iter(
                {"name": name, "conclusion": "success"} for name in EXPECTED_JOBS
            )
        return iter([{"id": 55, "name": "coverage-python", "expired": False}])

    calls = []

    def api(path, method="GET", data=None):
        calls.append((path, method, data))
        return context["environment"] if "/environments/" in path else {"id": 88}

    monkeypatch.setattr(scan, "pages", pages)
    monkeypatch.setattr(scan, "api", api)
    scan.prepare()
    check = calls[-1][2]
    assert check["head_sha"] == SHA
    assert check["status"] == "queued"
    assert check["name"] == "SonarQube scan"
    assert "sha=" + SHA in (context["path"].parent / "outputs").read_text()


@pytest.mark.parametrize("change", ["failed", "push", "internal", "workflow"])
def test_prepare_rejects_wrong_event_before_api_access(context, monkeypatch, change):
    """Only the intended successful fork validation workflow may request approval."""
    event = context["event"]
    if change == "failed":
        event["workflow_run"]["conclusion"] = "failure"
    elif change == "push":
        event["workflow_run"]["event"] = "push"
    elif change == "internal":
        event["workflow_run"]["head_repository"]["full_name"] = REPO
    else:
        event["workflow_run"]["path"] = ".github/workflows/other.yml"
    context["path"].write_text(json.dumps(event))
    api = Mock()
    monkeypatch.setattr(scan, "api", api)
    with pytest.raises(scan.ReviewError):
        scan.prepare()
    api.assert_not_called()


@pytest.mark.parametrize("change", ["closed", "sha", "target"])
def test_head_change_or_closed_pr_invalidates_approval(context, change):
    """An old approval cannot authorize a different commit or repository."""
    pr = context["pr"]
    if change == "closed":
        pr["state"] = "closed"
    elif change == "sha":
        pr["head"]["sha"] = "b" * 40
    else:
        pr["base"]["repo"]["full_name"] = "other/repo"
    with pytest.raises(scan.ReviewError):
        scan.require_head(pr, SHA)


@pytest.mark.parametrize("change", ["missing_reviewers", "bypass", "branches"])
def test_environment_configuration_fails_closed(context, change):
    """An absent approval rule must not silently enable privileged analysis."""
    environment = context["environment"]
    if change == "missing_reviewers":
        environment["protection_rules"] = []
    elif change == "bypass":
        environment["can_admins_bypass"] = True
    else:
        environment["deployment_branch_policy"] = None
    with pytest.raises(scan.ReviewError):
        scan.require_environment(environment)


def test_stage_uses_trusted_settings_and_never_imports_contributor_code(
    context, monkeypatch, tmp_path
):
    """Hostile Python and scanner settings remain data; credentials are not written."""
    root = candidate(tmp_path)
    monkeypatch.setenv("SOURCE_DIR", str(root))
    monkeypatch.setenv("COVERAGE_ARCHIVE", str(archive(tmp_path)))
    monkeypatch.setenv("SONAR_TOKEN", "synthetic-sonar-secret")
    api = Mock(
        side_effect=lambda path, *_args: (
            context["environment"] if "/environments/" in path else context["pr"]
        )
    )
    monkeypatch.setattr(scan, "api", api)
    scan.stage()
    settings = (root / "sonar-project.properties").read_text()
    assert "invalid.example" not in settings
    assert "sonar.projectKey=eduardobittencourt_motorola-nursery-ha" in settings
    assert "sonar.pullrequest.key=17" in settings
    assert "sonar.scm.revision=" + SHA in settings
    assert "sonar.qualitygate.wait=true" in settings
    assert "synthetic-sonar-secret" not in settings
    assert (root / "coverage.xml").read_bytes() == XML
    assert api.call_args.args == (
        "/repos/example/nursery/check-runs/88",
        "PATCH",
        {"status": "in_progress"},
    )


@pytest.mark.parametrize("link", ["file", "directory", "git", "submodule"])
def test_source_links_and_submodule_configuration_are_rejected(
    context, tmp_path, monkeypatch, link
):
    """The scanner must not follow source links into credentials or trusted code."""
    root = candidate(tmp_path)
    if link == "file":
        (root / "leak.py").symlink_to("/proc/self/environ")
    elif link == "directory":
        (root / "linked").symlink_to(tmp_path, target_is_directory=True)
    elif link == "git":
        (root / ".git/HEAD").unlink()
        (root / ".git").rmdir()
        (root / ".git").symlink_to(tmp_path, target_is_directory=True)
    else:
        (root / ".gitmodules").write_text("[submodule]\n")
    monkeypatch.setenv("SOURCE_DIR", str(root))
    monkeypatch.setenv("COVERAGE_ARCHIVE", str(archive(tmp_path)))
    monkeypatch.setattr(
        scan,
        "api",
        Mock(
            side_effect=lambda path, *_args: (
                context["environment"] if "/environments/" in path else context["pr"]
            )
        ),
    )
    expected = {"git": "isolated", "submodule": "submodule"}.get(link, "Source link")
    with pytest.raises(scan.ReviewError, match=expected):
        scan.stage()


@pytest.mark.parametrize(
    "raw",
    [
        XML.replace(b"scripts/example.py", b"../outside.py"),
        XML.replace(b"scripts/example.py", b"/proc/self/environ"),
        XML.replace(b"scripts/example.py", b"untracked.py"),
        XML.replace(b"<source>.</source>", b"<source>/home/runner</source>"),
        b'<!DOCTYPE coverage [<!ENTITY x SYSTEM "file:///proc/self/environ">]>' + XML,
        b"<other/>",
        b"<coverage/>",
        XML.decode().encode("utf-16"),
    ],
)
def test_untrusted_xml_cannot_escape_source_or_expand_entities(tmp_path, raw):
    """Coverage paths and XML declarations cannot read arbitrary runner files."""
    report = archive(tmp_path, raw)
    with pytest.raises((scan.ReviewError, UnicodeError)):
        scan.coverage_xml(report, {"scripts/example.py"})


@pytest.mark.parametrize(
    "member,link", [("../coverage.xml", False), ("coverage.xml", True)]
)
def test_coverage_zip_never_extracts_paths_or_symlinks(tmp_path, member, link):
    """Only the expected regular XML member is read from the archive."""
    report = archive(tmp_path, member=member, symlink=link)
    with pytest.raises(scan.ReviewError):
        scan.coverage_xml(report, {"scripts/example.py"})


def test_xml_size_is_bounded_before_decompression(tmp_path, monkeypatch):
    """A compressed oversized report is rejected before parsing."""
    monkeypatch.setattr(scan, "MAX_XML", 10)
    report = archive(tmp_path)
    with pytest.raises(scan.ReviewError, match="Invalid coverage"):
        scan.coverage_xml(report, {"scripts/example.py"})


@pytest.mark.parametrize(
    "outcome,changed,expected",
    [
        ("success", False, "success"),
        ("failure", False, "failure"),
        ("skipped", False, "failure"),
        ("success", True, "cancelled"),
    ],
)
def test_original_pr_check_reflects_scan_and_head(
    context, monkeypatch, outcome, changed, expected
):
    """Skipped scans never pass, and results cannot authorize a newer head."""
    pr = deepcopy(context["pr"])
    if changed:
        pr["head"]["sha"] = "b" * 40
    api = Mock(return_value=pr)
    monkeypatch.setattr(scan, "api", api)
    monkeypatch.setenv("SCAN_OUTCOME", outcome)
    scan.finish()
    assert api.call_args.args[2]["conclusion"] == expected


def test_branch_names_are_properties_data_not_scanner_arguments():
    """Line breaks and separators cannot inject additional analysis properties."""
    value = scan.properties_value("feature\nsonar.host.url=https://invalid.example")
    assert "\n" not in value
    assert "\\nsonar.host.url\\=" in value


def test_api_does_not_follow_redirects_or_leak_headers(context, monkeypatch):
    """A credential-bearing API request cannot redirect to another server."""
    connection = Mock()
    connection.getresponse.return_value.status = 302
    factory = Mock(return_value=connection)
    monkeypatch.setattr(scan.http.client, "HTTPSConnection", factory)
    with pytest.raises(scan.ReviewError, match="HTTP 302"):
        scan.api("/repos/example/nursery/pulls/17")
    factory.assert_called_once_with("api.github.com", timeout=30)
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    "origin", ["http://api.github.com", "https://invalid.example", "file:///etc/passwd"]
)
def test_api_rejects_non_github_origins(context, monkeypatch, origin):
    """Tokens may only be sent to GitHub's HTTPS API origin."""
    monkeypatch.setenv("GITHUB_API_URL", origin)
    with pytest.raises(scan.ReviewError, match="GitHub.com"):
        scan.api("/repos/example/nursery/pulls/17")


def test_changed_base_invalidates_previously_approved_scan(context, monkeypatch):
    """A moved target branch requires fresh validation and approval."""
    monkeypatch.setenv("BASE_SHA", "d" * 40)
    with pytest.raises(scan.ReviewError, match="Target branch changed"):
        scan.require_head(context["pr"], SHA)


def test_retargeted_pr_invalidates_approval(context):
    """Keeping the same head does not permit silently changing target branches."""
    context["pr"]["base"]["ref"] = "other-target"
    with pytest.raises(scan.ReviewError, match="target branch changed"):
        scan.require_head(context["pr"], SHA)


@pytest.mark.parametrize("problem", ["jobs", "artifact", "expired", "head"])
def test_prepare_does_not_queue_incomplete_or_stale_validation(
    context, monkeypatch, problem
):
    """A successful workflow label alone cannot authorize scanner access."""

    def pages(path, key=None):
        if "/pulls?" in path:
            return iter([] if problem == "head" else [context["pr"]])
        if key == "jobs":
            names = {"HACS"} if problem == "jobs" else EXPECTED_JOBS
            return iter({"name": name, "conclusion": "success"} for name in names)
        return iter(
            []
            if problem == "artifact"
            else [
                {"id": 55, "name": "coverage-python", "expired": problem == "expired"}
            ]
        )

    api = Mock()
    monkeypatch.setattr(scan, "pages", pages)
    monkeypatch.setattr(scan, "api", api)
    with pytest.raises(scan.ReviewError):
        scan.prepare()
    api.assert_not_called()


def test_stage_rechecks_environment_after_approval(context, monkeypatch, tmp_path):
    """Removing environment protection cannot silently release the scanner step."""
    root = candidate(tmp_path)
    monkeypatch.setenv("SOURCE_DIR", str(root))
    monkeypatch.setattr(scan, "api", Mock(return_value={}))
    with pytest.raises(scan.ReviewError, match="required reviewers"):
        scan.stage()
    assert "invalid.example" in (root / "sonar-project.properties").read_text()


def test_stage_rejects_wrong_checkout_even_when_pr_head_matches(
    context, monkeypatch, tmp_path
):
    """Analysis data must come from the approved commit, not a moving branch."""
    root = candidate(tmp_path)
    (root / ".git/HEAD").write_text("b" * 40)
    monkeypatch.setenv("SOURCE_DIR", str(root))
    monkeypatch.setattr(
        scan,
        "api",
        Mock(
            side_effect=lambda path, *_args: (
                context["environment"] if "/environments/" in path else context["pr"]
            )
        ),
    )
    with pytest.raises(scan.ReviewError, match="approved commit"):
        scan.stage()


def test_api_returns_json_and_closes_connection(context, monkeypatch):
    """Normal API requests retain fixed HTTPS routing and bounded response reads."""
    connection = Mock()
    response = connection.getresponse.return_value
    response.status = 200
    response.read.return_value = b'{"state":"open"}'
    monkeypatch.setattr(
        scan.http.client, "HTTPSConnection", Mock(return_value=connection)
    )
    assert scan.api("/repos/example/nursery/pulls/17") == {"state": "open"}
    connection.close.assert_called_once()


def test_coverage_supports_relative_source_prefixes(tmp_path):
    """Real coverage reports may split source roots from class filenames."""
    raw = XML.replace(b"<source>.</source>", b"<source>scripts</source>").replace(
        b"scripts/example.py", b"example.py"
    )
    assert scan.coverage_xml(archive(tmp_path, raw), {"scripts/example.py"}) == raw


@pytest.mark.parametrize(
    "path",
    [
        "/repos/example/nursery/actions/runs/../../secrets/jobs",
        "/repos/example/nursery/actions/runs/%2e%2e/jobs",
        "/repos/other/repository/pulls/17",
        "/repos/example/nursery/pulls/17#ignored",
        "/repos/example/nursery/pulls?state=closed",
        "/repos/example/nursery/pulls?per_page=100&evil=1",
        "/repos/example/nursery/pulls?per_page=100&per_page=10",
        "/repos/example/nursery/pulls?page=../secrets",
        "https://invalid.example/repos/example/nursery/pulls/17",
        "/repos/example/nursery/environments/other-environment",
    ],
)
def test_api_paths_cannot_traverse_or_change_resources(context, monkeypatch, path):
    """Workflow metadata cannot select a different API endpoint with our token."""
    connection = Mock()
    monkeypatch.setattr(
        scan.http.client, "HTTPSConnection", Mock(return_value=connection)
    )
    with pytest.raises(scan.ReviewError):
        scan.api(path)
    connection.request.assert_not_called()


def test_api_canonicalizes_only_allowlisted_pagination(context):
    """Expected API paths remain usable with validated pagination values."""
    path = "/repos/example/nursery/actions/runs/12/artifacts?per_page=100&page=2"
    assert scan.api_path(path) == path


@pytest.mark.parametrize("version", ["0.8.0", "0.9.0b6", "0.8.0+test"])
def test_valid_release_versions_remain_accepted_in_scan(
    context, monkeypatch, tmp_path, version
):
    """Version validation stays ASCII and accepts ordinary stable/beta metadata."""
    root = candidate(tmp_path)
    (root / "custom_components/motorola_nursery/manifest.json").write_text(
        json.dumps({"version": version})
    )
    monkeypatch.setenv("SOURCE_DIR", str(root))
    monkeypatch.setenv("COVERAGE_ARCHIVE", str(archive(tmp_path)))
    monkeypatch.setattr(
        scan,
        "api",
        Mock(
            side_effect=lambda path, *_args: (
                context["environment"] if "/environments/" in path else context["pr"]
            )
        ),
    )
    scan.stage()
    assert (
        "sonar.projectVersion=" + version
        in (root / "sonar-project.properties").read_text()
    )


@pytest.mark.parametrize("missing", sorted(EXPECTED_JOBS))
def test_each_required_validation_job_is_independently_required(
    context, monkeypatch, missing
):
    """Removing either HA job or another required validator cannot pass preparation."""

    def pages(path, key=None):
        if "/pulls?" in path:
            return iter([context["pr"]])
        if key == "jobs":
            return iter(
                {"name": name, "conclusion": "success"}
                for name in EXPECTED_JOBS - {missing}
            )
        return iter([{"id": 55, "name": "coverage-python", "expired": False}])

    api = Mock()
    monkeypatch.setattr(scan, "pages", pages)
    monkeypatch.setattr(scan, "api", api)
    with pytest.raises(scan.ReviewError, match="validation jobs"):
        scan.prepare()
    api.assert_not_called()
