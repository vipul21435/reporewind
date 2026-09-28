import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from reporewind import cli
from reporewind.cli import app
from reporewind.models import RepoRef
from reporewind.resolve import GitHubClient, load_resolved_fix
from reporewind.testing import BugfixRepo, RepoFactory

runner = CliRunner()


def test_resolve_from_a_local_clone_prints_json(bugfix: BugfixRepo) -> None:
    result = runner.invoke(
        app, ["resolve", "example/calc", "HEAD", "--repo-dir", str(bugfix.repo.path)]
    )
    assert result.exit_code == 0, result.output
    fix = load_resolved_fix(result.stdout)
    assert (fix.fix.sha, fix.base.sha) == (bugfix.fix, bugfix.base)
    assert [p.path for p in fix.test_patches] == ["tests/test_core.py"]
    assert f"resolved example/calc fix {bugfix.fix[:12]} (fix: add really adds)" in result.stderr
    assert "parent 1 of 1" in result.stderr
    assert "source  2 files: CHANGELOG.md, src/calc/core.py" in result.stderr
    assert "tests   1 file: tests/test_core.py" in result.stderr


def test_resolve_through_the_cache_writes_a_file(bugfix: BugfixRepo, tmp_path: Path) -> None:
    out = tmp_path / "out" / "fix.json"
    args = [
        "resolve",
        "https://github.com/example/calc.git",
        bugfix.fix,
        "--source",
        str(bugfix.repo.path),
        "--cache-dir",
        str(tmp_path / "home"),
        "--output",
        str(out),
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert f"wrote   {out}" in result.stderr
    data = json.loads(out.read_text())
    assert data["fix"]["sha"] == bugfix.fix
    assert (tmp_path / "home" / "repos" / "github.com" / "example__calc.git").is_dir()

    quiet = runner.invoke(app, [*args, "--quiet"])
    assert quiet.exit_code == 0
    assert quiet.stderr == ""


def test_resolve_honours_split_rule_options(bugfix: BugfixRepo) -> None:
    base = ["resolve", "example/calc", "HEAD", "--repo-dir", str(bugfix.repo.path), "-q"]
    result = runner.invoke(app, [*base, "--include", "CHANGELOG.md"])
    assert result.exit_code == 0, result.output
    fix = load_resolved_fix(result.stdout)
    assert [p.path for p in fix.test_patches] == ["CHANGELOG.md", "tests/test_core.py"]

    result = runner.invoke(app, [*base, "--test-dir", "spec", "--test-file", "*_spec.py"])
    assert result.exit_code == 10
    assert "changes no test files" in result.stderr

    result = runner.invoke(app, [*base, "--exclude", "tests/test_core.py"])
    assert result.exit_code == 10


def test_resolve_merge_commit_with_mainline(repo_factory: RepoFactory) -> None:
    repo = repo_factory.create(files={"pkg/a.py": "a\n"})
    repo.branch("topic")
    repo.commit("docs", {"README.md": "r\n"})
    repo.switch("topic")
    repo.commit("fix", {"pkg/a.py": "b\n", "tests/test_a.py": "t\n"})
    repo.switch("main")
    repo.merge("topic", "merge topic")
    base = ["resolve", "example/calc", "HEAD", "--repo-dir", str(repo.path)]

    result = runner.invoke(app, base)
    assert result.exit_code == 10
    assert "pass a mainline (1-2)" in result.stderr

    result = runner.invoke(app, [*base, "-m", "1"])
    assert result.exit_code == 0, result.output
    assert "parent 1 of 2" in result.stderr


def test_resolve_errors_map_to_exit_codes(bugfix: BugfixRepo, tmp_path: Path) -> None:
    repo_dir = str(bugfix.repo.path)
    cases = [
        (["resolve", "not a repo", "HEAD", "--repo-dir", repo_dir], 2, "invalid repository"),
        (["resolve", "o/r", "HEAD~2", "--repo-dir", repo_dir], 10, "root commit"),
        (["resolve", "o/r", "HEAD", "--repo-dir", str(tmp_path)], 2, "not a git repository"),
        (["resolve", "o/r", "nope", "--repo-dir", repo_dir], 4, "does not name a commit"),
        (["resolve", "o/r", "HEAD", "--repo-dir", repo_dir, "--test-dir", "a/b"], 2, "split"),
        (["resolve", "o/r", "HEAD", "--cache-dir", str(tmp_path)], 2, "full 40"),
    ]
    for argv, code, message in cases:
        result = runner.invoke(app, argv)
        assert result.exit_code == code, (argv, result.output)
        assert result.stderr.startswith("error: ")
        assert message in result.stderr, (argv, result.stderr)


def _fake_github(monkeypatch: pytest.MonkeyPatch, bugfix: BugfixRepo, **pull: object) -> None:
    """Serve a synthetic merged PR whose squashed commit is the fixture's fix."""
    pull_json = {
        "number": 5,
        "title": "Fix add",
        "merged": True,
        "merged_at": "2021-03-04T09:00:00Z",
        "merge_commit_sha": bugfix.fix,
        "commits": 1,
        "base": {"sha": bugfix.base, "ref": "main"},
        "head": {"sha": "d" * 40, "ref": "fix-add"},
        **pull,
    }
    commit_json = {
        "sha": bugfix.fix,
        "parents": [{"sha": bugfix.base}],
        "commit": {"message": "fix: add really adds", "author": {"date": "2021-03-04T06:06:07Z"}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/example/calc/pulls/5":
            return httpx.Response(200, json=pull_json)
        if request.url.path == f"/repos/example/calc/commits/{bugfix.fix}":
            return httpx.Response(200, json=commit_json)
        return httpx.Response(404, json={"message": "Not Found"})

    def factory(repo: RepoRef) -> GitHubClient:
        return GitHubClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cli, "github_client", factory)


def test_resolve_a_pull_request(monkeypatch: pytest.MonkeyPatch, bugfix: BugfixRepo) -> None:
    _fake_github(monkeypatch, bugfix)
    result = runner.invoke(
        app, ["resolve", "example/calc", "--pr", "5", "--repo-dir", str(bugfix.repo.path)]
    )
    assert result.exit_code == 0, result.output
    fix = load_resolved_fix(result.stdout)
    assert fix.pr_number == 5
    assert (fix.fix.sha, fix.base.sha) == (bugfix.fix, bugfix.base)
    expected = f"pull request #5 (Fix add) landed as a single-commit {bugfix.fix[:12]}"
    assert expected in result.stderr


def test_resolve_pull_request_errors(monkeypatch: pytest.MonkeyPatch, bugfix: BugfixRepo) -> None:
    repo_dir = ["--repo-dir", str(bugfix.repo.path)]
    _fake_github(monkeypatch, bugfix, merged=False, merged_at=None)
    cases = [
        (["resolve", "example/calc", "--pr", "5", *repo_dir], 10, "is not merged"),
        (["resolve", "example/calc", "--pr", "6", *repo_dir], 10, "not found (404)"),
        (["resolve", "example/calc", "HEAD", "--pr", "5", *repo_dir], 2, "not both"),
        (["resolve", "example/calc", *repo_dir], 2, "give a fix commit or --pr"),
        (["resolve", "example/calc", "--pr", "5", "-m", "1", *repo_dir], 2, "drop it with --pr"),
    ]
    for argv, code, message in cases:
        result = runner.invoke(app, argv)
        assert result.exit_code == code, (argv, result.output)
        assert message in result.stderr, (argv, result.stderr)


def test_default_github_client_targets_the_repo_host() -> None:
    with cli.github_client(RepoRef.parse("example/calc")) as client:
        assert isinstance(client, GitHubClient)
