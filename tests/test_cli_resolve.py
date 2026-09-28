import json
from pathlib import Path

from typer.testing import CliRunner

from reporewind.cli import app
from reporewind.resolve import load_resolved_fix
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
