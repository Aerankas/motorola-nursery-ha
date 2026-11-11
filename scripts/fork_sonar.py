"""Analyze reviewed fork sources without executing code from the contribution."""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import urllib.parse
import xml.parsers.expat as XML
import zipfile
from pathlib import Path, PurePosixPath

COVERAGE_XML = "coverage.xml"
MAX_XML = 10 * 1024 * 1024
REQUIRED_JOBS = {"Tests (current)", "Tests (minimum)", "HACS", "Hassfest"}
ENVIRONMENT = "sonar-fork-analysis"


class ReviewError(Exception):
    """Reject an unapproved, stale or unsafe analysis input."""


def api_path(path: str) -> str:
    """Allow only explicit repository API routes and canonical pagination queries."""
    parsed = urllib.parse.urlsplit(path)
    prefix = repo_path() + "/"
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not parsed.path.startswith(prefix)
    ):
        raise ReviewError("Unexpected repository API route")
    route = parsed.path[len(prefix) :]
    if not re.fullmatch(
        r"(?:pulls(?:/\d+)?|actions/runs/\d+/(?:jobs|artifacts)|"
        r"environments/sonar-fork-analysis|check-runs(?:/\d+)?)",
        route,
        re.ASCII,
    ):
        raise ReviewError("Unexpected repository API route")
    query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    for name, values in query.items():
        if len(values) != 1 or name not in {"state", "page", "per_page"}:
            raise ReviewError("Unexpected API query")
        if name == "state":
            if values != ["open"]:
                raise ReviewError("Unexpected PR state query")
        elif not re.fullmatch(r"\d{1,3}", values[0], re.ASCII):
            raise ReviewError("Unexpected pagination query")
    encoded = "/".join(
        urllib.parse.quote(part, safe="") for part in parsed.path.split("/")
    )
    return encoded + ("?" + urllib.parse.urlencode(query, doseq=True) if query else "")


def api(path: str, method: str = "GET", data=None):
    """Call only the current GitHub API origin without logging credentials."""
    origin = urllib.parse.urlsplit(
        os.environ.get("GITHUB_API_URL", "https://api.github.com")
    )
    if (
        origin.scheme != "https"
        or origin.netloc != "api.github.com"
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
        or not path.startswith("/repos/")
        or "\r" in path
        or "\n" in path
    ):
        raise ReviewError("Expected a GitHub.com repository API URL")
    connection = http.client.HTTPSConnection("api.github.com", timeout=30)
    try:
        connection.request(
            method,
            api_path(path),
            body=json.dumps(data) if data is not None else None,
            headers={
                "Authorization": "Bearer " + os.environ["GH_TOKEN"],
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "motorola-nursery-approved-analysis",
            },
        )
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise ReviewError(f"GitHub API request failed: HTTP {response.status}")
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ReviewError("GitHub API response size limit exceeded")
        return json.loads(body)
    finally:
        connection.close()


def pages(path: str, key: str | None = None):
    """Read all pages, bounding an unexpectedly large API response."""
    separator = "&" if "?" in path else "?"
    for page in range(1, 21):
        data = api(f"{path}{separator}per_page=100&page={page}")
        rows = data[key] if key else data
        yield from rows
        if len(rows) < 100:
            return
    raise ReviewError("GitHub pagination limit exceeded")


def repo_path() -> str:
    """Use the event repository, never a fork-supplied API destination."""
    repository = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ReviewError("Invalid repository")
    return "/repos/" + repository


def require_head(pr: dict, sha: str) -> None:
    """Bind approval and reporting to an open PR's exact current head."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ReviewError("Invalid head commit")
    if pr["state"] != "open" or pr["head"]["sha"] != sha:
        raise ReviewError("PR closed or head changed; a new CI run needs approval")
    if pr["base"]["repo"]["full_name"] != os.environ["GITHUB_REPOSITORY"]:
        raise ReviewError("PR targets another repository")
    if pr["base"]["ref"] != os.environ["BASE_BRANCH"]:
        raise ReviewError("PR target branch changed")
    if os.environ.get("BASE_SHA") and pr["base"]["sha"] != os.environ["BASE_SHA"]:
        raise ReviewError("Target branch changed; validate and approve again")


def require_environment(environment: dict) -> None:
    """Fail closed if environment protection was removed or never configured."""
    rules = environment.get("protection_rules", [])
    if not any(
        rule.get("type") == "required_reviewers" and rule.get("reviewers")
        for rule in rules
    ):
        raise ReviewError("Configure required reviewers on sonar-fork-analysis first")
    if environment.get("can_admins_bypass", True):
        raise ReviewError("Disable administrator bypass for sonar-fork-analysis")
    policy = environment.get("deployment_branch_policy") or {}
    if not policy.get("protected_branches"):
        raise ReviewError("Restrict sonar-fork-analysis to protected branches")


def output(name: str, value: str | int) -> None:
    """Write only validated, single-line workflow metadata."""
    value = str(value)
    if "\n" in value or "\r" in value:
        raise ReviewError("Invalid workflow output")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        stream.write(f"{name}={value}\n")


def prepare() -> None:
    """Validate a completed fork CI run and queue a check on the original PR."""
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    run = event["workflow_run"]
    repository = os.environ["GITHUB_REPOSITORY"]
    if (
        run["event"] != "pull_request"
        or run["conclusion"] != "success"
        or run["head_repository"]["full_name"] == repository
        or run["path"] != ".github/workflows/validate.yml"
    ):
        raise ReviewError("Only successful fork pull-request validation is eligible")
    base = repo_path()
    sha = run["head_sha"]
    run_id = int(run["id"])
    prs = [
        pr
        for pr in pages(base + "/pulls?state=open")
        if pr["head"]["sha"] == sha
        and pr["head"]["repo"]
        and pr["head"]["repo"]["full_name"] == run["head_repository"]["full_name"]
    ]
    if len(prs) != 1:
        raise ReviewError("Expected one open PR for this fork and exact commit")
    pr = prs[0]
    require_head(pr, sha)
    if pr["base"]["ref"] != event["repository"]["default_branch"]:
        raise ReviewError("Fork analysis supports the default target branch only")
    jobs = list(pages(f"{base}/actions/runs/{run_id}/jobs", "jobs"))
    passed = {job["name"] for job in jobs if job["conclusion"] == "success"}
    if not passed >= REQUIRED_JOBS:
        raise ReviewError("Required validation jobs did not all succeed")
    artifacts = list(pages(f"{base}/actions/runs/{run_id}/artifacts", "artifacts"))
    coverage = [a for a in artifacts if a["name"] == "coverage-python"]
    if len(coverage) != 1 or coverage[0]["expired"]:
        raise ReviewError("Current-HA coverage artifact missing or expired")
    require_environment(api(base + "/environments/" + ENVIRONMENT))
    check = api(
        base + "/check-runs",
        "POST",
        {
            "name": "SonarQube scan",
            "head_sha": sha,
            "status": "queued",
            "details_url": os.environ["ANALYSIS_RUN_URL"],
            "output": {
                "title": "Maintainer approval required",
                "summary": f"Review PR #{pr['number']} at `{sha}`. "
                "Approve the protected environment. "
                "Fork source is data; its tests and scripts receive no Sonar token.",
            },
        },
    )
    for name, value in {
        "pr": pr["number"],
        "sha": sha,
        "artifact": coverage[0]["id"],
        "base_sha": pr["base"]["sha"],
        "check": check["id"],
    }.items():
        output(name, value)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as stream:
        stream.write(
            f"Review PR #{pr['number']} at `{sha}` "
            f"before approving **{ENVIRONMENT}**.\n\n"
            "The scan uses the trusted default-branch workflow and settings. "
            "Changes to workflows and analysis settings require particular attention.\n"
        )


def source_file(root: Path, path: Path) -> tuple[str, int] | None:
    """Reject links and special files before the scanner can access their content."""
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ReviewError("Source link or path escapes checkout")
    if path.is_dir():
        return None
    if not path.is_file():
        raise ReviewError("Source is not a regular file")
    return path.relative_to(root).as_posix(), path.stat().st_size


def safe_sources(root: Path) -> set[str]:
    """Reject links, submodules and oversized sources before scanner access."""
    files = set()
    total = 0
    if (root / ".gitmodules").exists():
        raise ReviewError("Fork analysis does not accept submodule configuration")
    for directory, directories, names in os.walk(root, followlinks=False):
        if Path(directory) == root and ".git" in directories:
            directories.remove(".git")
        for name in directories + names:
            item = source_file(root, Path(directory) / name)
            if item is None:
                continue
            name, size = item
            total += size
            if total > 100 * 1024 * 1024:
                raise ReviewError("Source size limit exceeded")
            files.add(name)
    return files


def read_coverage(archive: Path) -> bytes:
    """Read one bounded XML member without extracting or loading a SQLite file."""
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        if names.count(COVERAGE_XML) != 1 or len(names) > 3:
            raise ReviewError("Unexpected coverage archive")
        if set(names) - {COVERAGE_XML, ".coverage.python"}:
            raise ReviewError("Unexpected coverage archive member")
        info = package.getinfo(COVERAGE_XML)
        if info.file_size > MAX_XML or stat.S_ISLNK(info.external_attr >> 16):
            raise ReviewError("Invalid coverage member")
        with package.open(info) as stream:
            raw = stream.read(MAX_XML + 1)
    if len(raw) > MAX_XML or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ReviewError("Unsafe coverage XML")
    return raw


def xml_paths(raw: bytes) -> tuple[list[str], list[str]]:
    """Parse coverage paths with DTD and entity handlers that reject declarations."""
    parser = XML.ParserCreate()
    parser.SetParamEntityParsing(XML.XML_PARAM_ENTITY_PARSING_NEVER)
    sources = []
    filenames = []
    tags = []

    def reject_declaration(*_args):
        raise ReviewError("DTD and entity declarations are forbidden")

    def start_element(name, attrs):
        tags.append(name)
        if len(tags) == 1 and name != "coverage":
            raise ReviewError("Expected a coverage report")
        if name == "class":
            filenames.append(attrs.get("filename", ""))
        if tags == ["coverage", "sources", "source"]:
            sources.append("")

    def text(data):
        if tags == ["coverage", "sources", "source"]:
            sources[-1] += data

    parser.StartDoctypeDeclHandler = reject_declaration
    parser.EntityDeclHandler = reject_declaration
    parser.ExternalEntityRefHandler = reject_declaration
    parser.StartElementHandler = start_element
    parser.EndElementHandler = lambda _name: tags.pop()
    parser.CharacterDataHandler = text
    parser.Parse(raw.decode("utf-8"), True)
    if not sources or not filenames:
        raise ReviewError("Coverage source paths or classes missing")
    return sources, filenames


def relative_path(value: str) -> PurePosixPath:
    """Permit only repository-relative coverage names."""
    path = PurePosixPath(value or ".")
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ReviewError("Coverage path escapes repository")
    return path


def coverage_xml(archive: Path, tracked: set[str]) -> bytes:
    """Accept bounded XML only when all coverage paths resolve to tracked source."""
    raw = read_coverage(archive)
    sources, filenames = xml_paths(raw)
    prefixes = [relative_path(source) for source in sources]
    for name in filenames:
        path = relative_path(name)
        if not any(str(prefix / path) in tracked for prefix in prefixes):
            raise ReviewError("Coverage references an untracked file")
    return raw


def properties_value(value: str) -> str:
    """Escape untrusted branch names as Java property values, never CLI flags."""
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("=", "\\=")
        .replace(":", "\\:")
    )


def stage() -> None:
    """Prepare data for the scanner after approval, without importing fork code."""
    base = repo_path()
    require_environment(api(base + "/environments/" + ENVIRONMENT))
    pr = api(f"{base}/pulls/{int(os.environ['PR_NUMBER'])}")
    sha = os.environ["HEAD_SHA"]
    require_head(pr, sha)
    root = Path(os.environ["SOURCE_DIR"]).resolve()
    git_directory = root / ".git"
    if not git_directory.is_dir() or git_directory.is_symlink():
        raise ReviewError("Expected an isolated checkout")
    actual = (git_directory / "HEAD").read_text().strip()
    if actual != sha:
        raise ReviewError("Checkout does not match the approved commit")
    tracked = safe_sources(root)
    xml = coverage_xml(Path(os.environ["COVERAGE_ARCHIVE"]), tracked)
    trusted = Path(__file__).resolve().parents[1]
    config = (trusted / "sonar-project.properties").read_text()
    version = json.loads(
        (root / "custom_components/motorola_nursery/manifest.json").read_text()
    )["version"]
    if len(version) > 64 or not re.fullmatch(
        r"\d++\.\d++\.\d++[A-Za-z0-9.+_-]*+", version, re.ASCII
    ):
        raise ReviewError("Invalid integration version")
    settings = {
        "sonar.pullrequest.key": str(pr["number"]),
        "sonar.pullrequest.branch": pr["head"]["ref"],
        "sonar.pullrequest.base": pr["base"]["ref"],
        "sonar.scm.revision": sha,
        "sonar.projectVersion": version,
        "sonar.qualitygate.wait": "true",
    }
    config += "\n" + "\n".join(
        key + "=" + properties_value(value) for key, value in settings.items()
    )
    (root / "sonar-project.properties").write_text(config + "\n")
    (root / COVERAGE_XML).write_bytes(xml)
    api(
        f"{base}/check-runs/{int(os.environ['CHECK_ID'])}",
        "PATCH",
        {"status": "in_progress"},
    )


def finish() -> None:
    """Publish the real scan outcome on the approved PR commit, never on main."""
    base = repo_path()
    conclusion = "success" if os.environ["SCAN_OUTCOME"] == "success" else "failure"
    pr = api(f"{base}/pulls/{int(os.environ['PR_NUMBER'])}")
    try:
        require_head(pr, os.environ["HEAD_SHA"])
    except ReviewError:
        conclusion = "cancelled"
    api(
        f"{base}/check-runs/{int(os.environ['CHECK_ID'])}",
        "PATCH",
        {
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": "Approved fork analysis: " + conclusion,
                "summary": "Scanner outcome: "
                + os.environ["SCAN_OUTCOME"]
                + ". The separate SonarCloud Code Analysis check remains required.",
            },
        },
    )


if __name__ == "__main__":
    import sys

    try:
        {"prepare": prepare, "stage": stage, "finish": finish}[sys.argv[1]]()
    except (
        ReviewError,
        KeyError,
        ValueError,
        OSError,
        zipfile.BadZipFile,
        XML.ExpatError,
        http.client.HTTPException,
    ):
        # Malformed fork data and API failures must not leak source or secrets.
        print(
            "::error::Fork analysis rejected; "
            "verify CI, approval protection and commit metadata."
        )
        raise SystemExit(1) from None
