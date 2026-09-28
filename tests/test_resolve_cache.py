import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from reporewind.errors import InvalidRevisionError
from reporewind.models import RepoRef
from reporewind.proc import CommandResult, SubprocessRunner
from reporewind.resolve import RepoCache, dump_resolved_fix, home_dir, resolve_fix
from reporewind.resolve.cache import PIN_REF_PREFIX
from reporewind.testing import BugfixRepo

REPO = RepoRef.parse("Example/Calc")


class CountingRunner(SubprocessRunner):
    """A real runner that records every git subcommand it runs."""

    def __init__(self) -> None:
        self.subcommands: list[str] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        self.subcommands.append(argv[1])
        return super().run(argv, cwd=cwd, env=env, stdin=stdin, timeout=timeout)


def test_home_dir_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert home_dir({"REPOREWIND_HOME": "/srv/rr"}) == Path("/srv/rr")
    assert home_dir({"REPOREWIND_HOME": ""}) == Path(".reporewind")
    assert home_dir({}) == Path(".reporewind")
    monkeypatch.setenv("HOME", "/home/someone")
    assert home_dir({"REPOREWIND_HOME": "~/rr"}) == Path("/home/someone/rr")
    monkeypatch.setenv("REPOREWIND_HOME", "/from/env")
    assert home_dir() == Path("/from/env")


def test_cache_layout_is_one_bare_repo_per_hosted_repo(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path)
    path = cache.path_for(REPO)
    assert path == tmp_path / "repos" / "github.com" / "example__calc.git"
    git = cache.open(REPO)
    assert git.is_repo()
    assert git.run("rev-parse", "--is-bare-repository").stdout_text.strip() == "true"
    assert cache.open(REPO).repo_dir == path.resolve()


def test_ensure_commit_fetches_once_then_works_offline(bugfix: BugfixRepo, tmp_path: Path) -> None:
    runner = CountingRunner()
    cache = RepoCache(tmp_path / "home", runner=runner)
    source = tmp_path / "mirror"
    shutil.copytree(bugfix.repo.path, source)

    git = cache.ensure_commit(REPO, bugfix.fix.upper(), source=str(source))
    assert runner.subcommands.count("fetch") == 1
    assert git.has_commit(bugfix.fix)
    assert git.has_commit(bugfix.base)
    assert git.rev_parse(PIN_REF_PREFIX + bugfix.fix) == bugfix.fix
    assert git.shallow_commits() == {bugfix.base}

    shutil.rmtree(source)
    again = cache.ensure_commit(REPO, bugfix.fix, source=str(source))
    assert again.repo_dir == git.repo_dir
    assert runner.subcommands.count("fetch") == 1


def test_a_shallow_boundary_is_deepened_when_it_becomes_the_fix(
    bugfix: BugfixRepo, tmp_path: Path
) -> None:
    cache = RepoCache(tmp_path / "home")
    source = str(bugfix.repo.path)
    git = cache.ensure_commit(REPO, bugfix.fix, source=source)
    assert not cache.is_complete(git, bugfix.base)
    cache.ensure_commit(REPO, bugfix.base, source=source)
    assert cache.is_complete(git, bugfix.base)
    assert git.has_commit(git.parents(bugfix.base)[0])


def test_ensure_commit_needs_a_full_sha(bugfix: BugfixRepo, tmp_path: Path) -> None:
    cache = RepoCache(tmp_path / "home")
    with pytest.raises(InvalidRevisionError, match="full 40 or 64 character SHA"):
        cache.ensure_commit(REPO, bugfix.fix[:12], source=str(bugfix.repo.path))
    with pytest.raises(InvalidRevisionError):
        cache.ensure_commit(REPO, "HEAD", source=str(bugfix.repo.path))


def test_resolving_from_the_shallow_cache_matches_the_full_clone(
    bugfix: BugfixRepo, tmp_path: Path
) -> None:
    cache = RepoCache(tmp_path / "home")
    shallow = cache.ensure_commit(REPO, bugfix.fix, source=str(bugfix.repo.path))
    from_cache = resolve_fix(shallow, REPO, bugfix.fix)
    from_clone = resolve_fix(bugfix.repo.git, REPO, bugfix.fix)
    assert dump_resolved_fix(from_cache.fix) == dump_resolved_fix(from_clone.fix)
    assert from_cache.proof == from_clone.proof
