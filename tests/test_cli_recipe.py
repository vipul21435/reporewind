import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from reporewind.cli import app
from reporewind.models import RepoRef
from reporewind.recipes import RecipeStore, load_recipe_file
from reporewind.testing import BugfixRepo, RepoFactory

runner = CliRunner()

PYPROJECT = '[project]\nname = "calc"\nrequires-python = ">=3.8"\n'


def calc_repo(repo_factory: RepoFactory) -> tuple[Path, str, str]:
    repo = repo_factory.create(
        files={
            "pyproject.toml": PYPROJECT,
            "src/calc/__init__.py": "",
            "tests/test_core.py": "def test_x():\n    assert True\n",
        }
    )
    base = repo.head
    fix = repo.commit(
        "fix", {"pyproject.toml": PYPROJECT.replace("3.8", "3.9"), "tox.ini": "[pytest]\n"}
    )
    return repo.path, base, fix


def test_detect_reads_the_parent_and_writes_the_recipe(
    repo_factory: RepoFactory, tmp_path: Path
) -> None:
    path, base, fix = calc_repo(repo_factory)
    recipes = tmp_path / "recipes"
    args = ["recipe", "detect", "example/calc", "HEAD", "--repo-dir", str(path)]
    result = runner.invoke(app, [*args, "--recipes-dir", str(recipes)])
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    stored = load_recipe_file(recipes / "example__calc.yaml")
    assert stored.detected.commit == base
    assert stored.recipe().python == ">=3.8"
    err = result.stderr
    assert f"detected example/calc recipe at {base[:12]} (parent 1 of 1 of {fix[:12]})" in err
    assert "  backend  setuptools" in err
    assert "  sources  pyproject.toml" in err
    assert "  note     pytest: test files define plain test functions" in err
    assert f"  hash     {stored.recipe_hash()}\n" in err
    assert f"  wrote    {recipes / 'example__calc.yaml'}" in err

    at_rev = runner.invoke(app, [*args, "--at-rev", "--recipes-dir", str(recipes), "-q"])
    assert at_rev.exit_code == 0, at_rev.output
    assert at_rev.stderr == ""
    assert load_recipe_file(recipes / "example__calc.yaml").recipe().python == ">=3.9"


def test_detect_keeps_overrides_and_supports_dry_run(
    repo_factory: RepoFactory, tmp_path: Path
) -> None:
    path, _, _ = calc_repo(repo_factory)
    recipes = tmp_path / "recipes"
    args = ["recipe", "detect", "example/calc", "HEAD", "--repo-dir", str(path)]
    args += ["--recipes-dir", str(recipes)]
    assert runner.invoke(app, args).exit_code == 0
    target = recipes / "example__calc.yaml"
    target.write_text(
        target.read_text().replace("overrides: {}", "overrides:\n  env:\n    TZ: UTC\n")
    )

    dry = runner.invoke(app, [*args, "--dry-run", "--at-rev"])
    assert dry.exit_code == 0, dry.output
    printed = yaml.safe_load(dry.stdout)
    assert printed["overrides"] == {"env": {"TZ": "UTC"}}
    assert printed["detected"]["recipe"]["python"] == ">=3.9"
    assert "(1 override kept: env)" in dry.stderr
    assert "wrote" not in dry.stderr
    # The dry run wrote nothing: the stored recipe is still the parent's.
    assert load_recipe_file(target).recipe().python == ">=3.8"

    target.write_text(target.read_text().replace("TZ: UTC", "TZ: [1]"))
    broken = runner.invoke(app, args)
    assert broken.exit_code == 11
    assert "env.TZ: Input should be a valid string" in broken.stderr


def test_detect_argument_errors(bugfix: BugfixRepo, repo_factory: RepoFactory) -> None:
    base = ["recipe", "detect", "example/calc", "HEAD", "--repo-dir", str(bugfix.repo.path)]
    both = runner.invoke(app, [*base, "--at-rev", "--mainline", "1"])
    assert both.exit_code == 2
    assert "cannot be used with --at-rev" in both.stderr

    repo = repo_factory.create("merge", files={"a.py": "a\n"})
    repo.branch("topic")
    repo.commit("docs", {"README.md": "r\n"})
    repo.switch("topic")
    repo.commit("fix", {"a.py": "b\n"})
    repo.switch("main")
    repo.merge("topic", "merge topic")
    merge = ["recipe", "detect", "example/calc", "HEAD", "--repo-dir", str(repo.path), "-n"]
    assert runner.invoke(app, merge).exit_code == 10
    assert runner.invoke(app, [*merge, "-m", "2"]).exit_code == 0

    not_repo = runner.invoke(app, ["recipe", "detect", "a/b", "HEAD", "--repo-dir", "/"])
    assert not_repo.exit_code == 2


def test_detect_through_the_cache(bugfix: BugfixRepo, tmp_path: Path) -> None:
    args = ["recipe", "detect", "example/calc", bugfix.fix, "--source", str(bugfix.repo.path)]
    args += ["--cache-dir", str(tmp_path / "home"), "--recipes-dir", str(tmp_path / "r")]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    stored = load_recipe_file(tmp_path / "r" / "example__calc.yaml")
    assert stored.detected.commit == bugfix.base
    assert "no packaging metadata or requirements files found" in result.stderr


def test_show_prints_the_effective_recipe(repo_factory: RepoFactory, tmp_path: Path) -> None:
    path, base, _ = calc_repo(repo_factory)
    recipes = tmp_path / "recipes"
    detect = ["recipe", "detect", "example/calc", "HEAD", "--repo-dir", str(path)]
    assert runner.invoke(app, [*detect, "--recipes-dir", str(recipes), "-q"]).exit_code == 0
    target = recipes / "example__calc.yaml"
    target.write_text(target.read_text().replace("overrides: {}", "overrides:\n  python: null\n"))

    shown = runner.invoke(app, ["recipe", "show", "example/calc", "--recipes-dir", str(recipes)])
    assert shown.exit_code == 0, shown.output
    data = yaml.safe_load(shown.stdout)
    stored = RecipeStore(recipes).load(RepoRef.parse("example/calc"))
    assert data["recipe_hash"] == stored.recipe_hash()
    assert data["detected_at"] == base
    assert data["overridden"] == ["python"]
    assert data["recipe"]["python"] is None
    assert data["recipe"]["test_command"] == ["python", "-m", "pytest", "-rA"]

    show_json = ["recipe", "show", "https://github.com/example/calc", "--json"]
    as_json = runner.invoke(app, [*show_json, "--recipes-dir", str(recipes)])
    assert as_json.exit_code == 0
    assert json.loads(as_json.stdout) == data

    missing = runner.invoke(app, ["recipe", "show", "a/b", "--recipes-dir", str(recipes)])
    assert missing.exit_code == 11
    assert "no recipe for a/b" in missing.stderr


def test_validate(tmp_path: Path) -> None:
    recipes = tmp_path / "recipes"
    empty = runner.invoke(app, ["recipe", "validate", "--recipes-dir", str(recipes)])
    assert empty.exit_code == 2
    assert "no recipe files to validate" in empty.stderr

    recipes.mkdir()
    (recipes / "a__b.yaml").write_text("repo: a/b\n")
    (recipes / "c__d.yaml").write_text("repo: c/d\noverrides:\n  install: requirements\n")
    (recipes / "wrong.yaml").write_text("repo: e/f\n")
    result = runner.invoke(app, ["recipe", "validate", "--recipes-dir", str(recipes)])
    assert result.exit_code == 11
    assert f"ok       {recipes / 'a__b.yaml'}  sha256:" in result.stdout
    assert "needs at least one requirements file" in result.stderr
    assert "must be named e__f.yaml" in result.stderr
    assert "2 recipe files failed validation" in result.stderr

    one = runner.invoke(app, ["recipe", "validate", str(recipes / "a__b.yaml")])
    assert one.exit_code == 0, one.output
