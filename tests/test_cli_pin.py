import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fakes import FakeRunner, failed, ok
from reporewind import cli
from reporewind.cli import app
from reporewind.pinning.image import FALLBACK_DIGESTS
from reporewind.pinning.pin import load_pin_result
from reporewind.testing import RepoFactory

runner = CliRunner()
DIGEST = "sha256:" + "ef" * 32
PYPROJECT = """[project]
name = "calc"
requires-python = ">=3.7"
classifiers = ["Programming Language :: Python :: 3.8"]
dependencies = ["six>=1.10"]
"""
LOCK = "pytest==6.2.2\nsix==1.15.0\n"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    scripted = FakeRunner({"docker buildx": ok(json.dumps({"digest": DIGEST})), "uv pip": ok(LOCK)})
    monkeypatch.setattr(cli, "command_runner", lambda: scripted)
    monkeypatch.delenv("REPOREWIND_OFFLINE", raising=False)
    return scripted


def calc_repo(repo_factory: RepoFactory) -> tuple[Path, str, str]:
    repo = repo_factory.create(
        files={
            "pyproject.toml": PYPROJECT,
            "src/calc/__init__.py": "",
            "tests/test_core.py": "def test_x():\n    assert True\n",
        }
    )
    base = repo.head
    fix = repo.commit("fix", {"src/calc/__init__.py": "x = 1\n"})
    return repo.path, base, fix


def test_pin_writes_the_lock_and_pin_result(
    repo_factory: RepoFactory, tmp_path: Path, fake: FakeRunner
) -> None:
    path, base, fix = calc_repo(repo_factory)
    out = tmp_path / "pin"
    args = ["pin", "example/calc", "HEAD", "--repo-dir", str(path), "-o", str(out)]
    result = runner.invoke(app, [*args, "--recipes-dir", str(tmp_path / "none")])
    assert result.exit_code == 0, result.output
    pinned = load_pin_result(out / "pin.json")
    assert json.loads(result.stdout) == json.loads((out / "pin.json").read_text())
    assert pinned.commit == base
    assert pinned.python.version == "3.8"
    assert pinned.base_image.digest == DIGEST
    assert pinned.requirements == ("six>=1.10", "pytest")
    assert pinned.packages == ("pytest==6.2.2", "six==1.15.0")
    assert (out / "requirements.lock").read_text().endswith(LOCK)
    err = result.stderr
    assert f"pinned example/calc at {base[:12]} (parent 1 of 1 of {fix[:12]})" in err
    assert "committed 2021-03-04T05:06:07Z" in err
    assert "  python   3.8 (newest classifier version released by 2021-03-04" in err
    assert f"  image    python:3.8-slim@{DIGEST} (registry)" in err
    assert "  lock     2 packages published up to 2021-03-04T05:06:07Z, x86_64-unknown" in err
    assert "  note     no recipe file at" in err
    assert f"  wrote    {out / 'pin.json'}" in err


def test_pin_uses_the_stored_recipe_and_default_location(
    repo_factory: RepoFactory, tmp_path: Path, fake: FakeRunner
) -> None:
    path, _base, fix = calc_repo(repo_factory)
    recipes = tmp_path / "recipes"
    detect = ["recipe", "detect", "example/calc", "HEAD", "--repo-dir", str(path)]
    assert runner.invoke(app, [*detect, "--recipes-dir", str(recipes)]).exit_code == 0
    home = tmp_path / "home"
    args = ["pin", "example/calc", "HEAD", "--repo-dir", str(path), "--at-rev", "-q"]
    result = runner.invoke(
        app, [*args, "--recipes-dir", str(recipes), "--cache-dir", str(home), "--python", "3.7"]
    )
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    pin_file = home / "pins" / "github.com" / "example__calc" / fix / "pin.json"
    pinned = load_pin_result(pin_file)
    assert pinned.commit == fix
    assert pinned.python.version == "3.7"
    assert pinned.python.reason == "given explicitly"
    assert pinned.notes[0].startswith("the recipe was detected at ")


def test_pin_offline_from_the_environment(
    repo_factory: RepoFactory, tmp_path: Path, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _, _ = calc_repo(repo_factory)
    monkeypatch.setenv("REPOREWIND_OFFLINE", "1")
    args = ["pin", "example/calc", "HEAD", "--repo-dir", str(path), "-o", str(tmp_path / "o")]
    result = runner.invoke(
        app, [*args, "--recipes-dir", str(tmp_path), "--exclude-newer", "2021-01-01"]
    )
    assert result.exit_code == 0, result.output
    pinned = load_pin_result(tmp_path / "o" / "pin.json")
    assert pinned.base_image.digest == FALLBACK_DIGESTS["3.8"]
    assert pinned.exclude_newer.isoformat() == "2021-01-01T00:00:00+00:00"
    assert [call.argv[:2] for call in fake.calls] == [("uv", "pip")]
    assert "--offline" in fake.calls[0].argv


@pytest.mark.parametrize(
    ("extra", "message", "code"),
    [
        (["--exclude-newer", "yesterday"], "is not an ISO 8601 date", 2),
        (["--at-rev", "-m", "1"], "cannot be used with --at-rev", 2),
        (["--python", "2.7"], "Python 2.7 is not supported", 2),
    ],
)
def test_pin_rejects_bad_input(
    repo_factory: RepoFactory,
    tmp_path: Path,
    fake: FakeRunner,
    extra: list[str],
    message: str,
    code: int,
) -> None:
    path, _, _ = calc_repo(repo_factory)
    args = ["pin", "example/calc", "HEAD", "--repo-dir", str(path), "-o", str(tmp_path / "o")]
    result = runner.invoke(app, [*args, "--recipes-dir", str(tmp_path), *extra])
    assert result.exit_code == code, result.output
    assert message in result.stderr


def test_pin_reports_resolver_failures_with_exit_12(
    repo_factory: RepoFactory, tmp_path: Path, fake: FakeRunner
) -> None:
    path, _, _ = calc_repo(repo_factory)
    fake.handlers["uv pip"] = failed("  x No solution found when resolving dependencies\n")
    args = ["pin", "example/calc", "HEAD", "--repo-dir", str(path), "-o", str(tmp_path / "o")]
    result = runner.invoke(app, [*args, "--recipes-dir", str(tmp_path)])
    assert result.exit_code == 12
    assert "error: uv pip compile failed (exit 1):" in result.stderr
    assert not (tmp_path / "o").exists()


def test_command_runner_is_a_subprocess_runner() -> None:
    from reporewind.proc import SubprocessRunner

    assert isinstance(cli.command_runner(), SubprocessRunner)
