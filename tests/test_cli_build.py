import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fakes import FakeRunner, failed, ok
from reporewind import cli
from reporewind.cli import app
from reporewind.proc import CommandResult
from reporewind.testing import RepoFactory

runner = CliRunner()
DIGEST = "sha256:" + "ef" * 32
PYPROJECT = """[project]
name = "calc"
requires-python = ">=3.7"
classifiers = ["Programming Language :: Python :: 3.8"]
"""
LOCK = "pytest==6.2.2\n"


class FakeDocker(FakeRunner):
    """uv and buildx answers for `pin`, and a docker that remembers what it built."""

    def __init__(self) -> None:
        self.built: list[str] = []
        super().__init__(
            {
                "docker buildx": ok(json.dumps({"digest": DIGEST})),
                "uv pip": ok(LOCK),
                "docker build": self._build,
                "docker image": self._inspect,
            }
        )

    def _build(self, argv: tuple[str, ...], cwd: Path | None) -> CommandResult:
        context = Path(argv[-1])
        files = sorted(p.relative_to(context).as_posix() for p in context.rglob("*") if p.is_file())
        self.built.append(" ".join(files))
        return ok("")

    def _inspect(self, argv: tuple[str, ...], cwd: Path | None) -> CommandResult:
        return ok("sha256:" + "12" * 32 + "\n") if self.built else failed("No such image")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    scripted = FakeDocker()
    monkeypatch.setattr(cli, "command_runner", lambda: scripted)
    monkeypatch.delenv("REPOREWIND_OFFLINE", raising=False)
    return scripted


def pinned_repo(repo_factory: RepoFactory, state: Path) -> tuple[list[str], str]:
    repo = repo_factory.create(
        files={
            "pyproject.toml": PYPROJECT,
            "src/calc/__init__.py": "",
            "tests/test_core.py": "def test_x():\n    assert True\n",
        }
    )
    base = repo.head
    repo.commit("fix", {"src/calc/__init__.py": "x = 1\n"})
    common = [
        "example/calc",
        "HEAD",
        "--repo-dir",
        str(repo.path),
        "--cache-dir",
        str(state),
        "--recipes-dir",
        str(state / "no-recipes"),
    ]
    result = runner.invoke(app, ["pin", *common, "-q"])
    assert result.exit_code == 0, result.output
    return common, base


def test_dry_run_prints_the_dockerfile_and_tag_without_docker(
    repo_factory: RepoFactory, tmp_path: Path, fake: FakeDocker
) -> None:
    common, base = pinned_repo(repo_factory, tmp_path / "state")
    before = len(fake.calls)
    result = runner.invoke(app, ["build", *common, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("# syntax=docker/dockerfile:1\n")
    assert f"FROM python:3.8-slim@{DIGEST}\n" in result.stdout
    assert f"org.reporewind.commit={base}" in result.stdout
    assert "tag      reporewind/example__calc:env-" in result.stderr
    assert "(not built yet)" in result.stderr
    assert "docker run --rm --network none" in result.stderr
    assert len(fake.calls) == before  # a dry run never calls docker or uv


def test_build_then_cache_hit(repo_factory: RepoFactory, tmp_path: Path, fake: FakeDocker) -> None:
    state = tmp_path / "state"
    common, _ = pinned_repo(repo_factory, state)
    first = runner.invoke(app, ["build", *common])
    assert first.exit_code == 0, first.output
    built = json.loads(first.stdout)
    assert built["status"] == "built"
    assert fake.built == [
        "Dockerfile requirements.lock src/pyproject.toml src/src/calc/__init__.py"
        " src/tests/test_core.py"
    ]
    second = runner.invoke(app, ["build", *common])
    assert json.loads(second.stdout) == {**built, "status": "cached"}
    assert len(fake.built) == 1
    dry = runner.invoke(app, ["build", *common, "--dry-run"])
    assert f"(indexed as {built['image_id']})" in dry.stderr


def test_build_needs_a_matching_pin(
    repo_factory: RepoFactory, tmp_path: Path, fake: FakeDocker
) -> None:
    state = tmp_path / "state"
    common, _ = pinned_repo(repo_factory, state)
    missing = runner.invoke(app, ["build", *common, "--at-rev", "--dry-run"])
    assert missing.exit_code == 13
    assert "run `reporewind pin` first" in missing.stderr
    pins = next((state / "pins").rglob("pin.json")).parent
    (pins / "requirements.lock").write_text("tampered==1\n")
    tampered = runner.invoke(app, ["build", *common, "--dry-run"])
    assert tampered.exit_code == 13
    assert "does not match the sha256 recorded" in tampered.stderr
    wrong = runner.invoke(app, ["build", *common, "--at-rev", "--pin-dir", str(pins)])
    assert wrong.exit_code == 13
    assert "pins " in wrong.stderr
    both = runner.invoke(app, ["build", *common, "--at-rev", "-m", "1"])
    assert both.exit_code == 2
