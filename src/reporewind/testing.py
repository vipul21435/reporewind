"""Deterministic throwaway git repositories for offline tests.

:class:`RepoFactory` builds small repositories in a temporary directory with a
fixed identity and a fixed, strictly increasing clock, so the same sequence of
calls yields the same commit SHAs on every machine (laptop or CI). Every later
pipeline stage (resolver, recipe detection, pinning, verification) is tested
against repositories built this way instead of the network.

Example::

    repo = RepoFactory(tmp_path).create(files={"pkg/core.py": "x = 1\\n"})
    fix = repo.commit("fix: handle zero", {"pkg/core.py": "x = 2\\n"})

The module has no pytest dependency; ``tests/conftest.py`` exposes it as the
``repo_factory`` fixture.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reporewind.gitops import Git, check_revision
from reporewind.models import check_repo_path

FIXTURE_EPOCH = datetime(2021, 3, 4, 5, 6, 7, tzinfo=UTC)
"""Date of the first commit in every fixture repository."""

FIXTURE_STEP = timedelta(hours=1)
"""Clock advance between consecutive commits."""

FIXTURE_NAME = "RepoRewind Fixture"
FIXTURE_EMAIL = "fixture@example.invalid"

FileContent = str | bytes | None
"""Text (written as UTF-8, no newline translation), raw bytes, or ``None`` to delete."""


class FixtureRepo:
    """A throwaway repository with a deterministic commit clock."""

    def __init__(self, git: Git, *, start: datetime = FIXTURE_EPOCH) -> None:
        self.git = git
        self._next = start

    @property
    def path(self) -> Path:
        return self.git.repo_dir

    @property
    def head(self) -> str:
        """Full SHA of ``HEAD``."""
        return self.git.rev_parse("HEAD")

    def write(self, files: Mapping[str, FileContent]) -> None:
        """Create, overwrite or (with ``None``) delete files in the work tree."""
        for rel, content in files.items():
            target = self.path / check_repo_path(rel)
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8") if isinstance(content, str) else content
            target.write_bytes(data)

    def commit(
        self,
        message: str,
        files: Mapping[str, FileContent] | None = None,
        *,
        when: datetime | None = None,
    ) -> str:
        """Write ``files``, stage everything and commit; return the new SHA."""
        if files:
            self.write(files)
        self.git.run("add", "--all")
        self.git.run("commit", "--quiet", "--no-verify", "-m", message, env=self._identity(when))
        return self.head

    def rename(self, old: str, new: str) -> None:
        """``git mv`` a file (commit afterwards to record the rename)."""
        target = self.path / check_repo_path(new)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.git.run("mv", "--", check_repo_path(old), new)

    def branch(self, name: str, start: str = "HEAD") -> None:
        """Create branch ``name`` at ``start`` without switching to it."""
        self.git.run("branch", "--", check_revision(name), self.git.rev_parse(start))

    def switch(self, name: str) -> None:
        """Check out an existing branch."""
        self.git.run("switch", "--quiet", check_revision(name))

    def merge(self, branch: str, message: str, *, when: datetime | None = None) -> str:
        """Create a true merge commit (``--no-ff``) of ``branch`` into the current branch."""
        self.git.run(
            "merge",
            "--quiet",
            "--no-ff",
            "--no-verify",
            "-m",
            message,
            check_revision(branch),
            env=self._identity(when),
        )
        return self.head

    def tag(self, name: str, rev: str = "HEAD") -> None:
        """Create a lightweight tag."""
        self.git.run("tag", "--", check_revision(name), self.git.rev_parse(rev))

    def _identity(self, when: datetime | None) -> dict[str, str]:
        if when is None:
            when = self._next
            self._next += FIXTURE_STEP
        if when.tzinfo is None:
            raise ValueError("commit dates must be timezone-aware")
        stamp = when.isoformat()
        return {
            "GIT_AUTHOR_NAME": FIXTURE_NAME,
            "GIT_AUTHOR_EMAIL": FIXTURE_EMAIL,
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": FIXTURE_NAME,
            "GIT_COMMITTER_EMAIL": FIXTURE_EMAIL,
            "GIT_COMMITTER_DATE": stamp,
        }


class RepoFactory:
    """Create :class:`FixtureRepo` instances under one root directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def create(
        self,
        name: str = "repo",
        files: Mapping[str, FileContent] | None = None,
        *,
        message: str = "chore: initial commit",
        start: datetime = FIXTURE_EPOCH,
    ) -> FixtureRepo:
        """Initialise ``root/name`` on branch ``main``; commit ``files`` if given."""
        path = self.root / check_repo_path(name)
        if path.exists():
            raise FileExistsError(f"fixture repository {path} already exists")
        repo = FixtureRepo(Git(path).init(), start=start)
        if files:
            repo.commit(message, files)
        return repo


BUGGY_ADD = "def add(a, b):\n    return a - b\n"
FIXED_ADD = "def add(a, b):\n    return a + b\n"
TEST_ADD = "from calc.core import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@dataclass(frozen=True, slots=True)
class BugfixRepo:
    """A repository whose HEAD (``fix``) is a typical bug fix on top of ``base``."""

    repo: FixtureRepo
    base: str
    fix: str


def make_bugfix(factory: RepoFactory, name: str = "upstream") -> BugfixRepo:
    """Build ``root -> base -> fix`` where the fix corrects ``src/calc/core.py``.

    The fix commit edits one source file, adds ``tests/test_core.py`` (which
    fails on ``base`` and passes on ``fix``) and adds a changelog entry, the
    usual shape of a real bug-fix commit.
    """
    repo = factory.create(
        name,
        files={
            "src/calc/__init__.py": "",
            "src/calc/core.py": BUGGY_ADD,
            "tests/conftest.py": "",
            "README.md": "calc\n",
        },
    )
    base = repo.commit("docs: usage", {"README.md": "calc\n\nadd(a, b)\n"})
    fix = repo.commit(
        "fix: add really adds",
        {
            "src/calc/core.py": FIXED_ADD,
            "tests/test_core.py": TEST_ADD,
            "CHANGELOG.md": "- add() adds\n",
        },
    )
    return BugfixRepo(repo=repo, base=base, fix=fix)


__all__ = [
    "BUGGY_ADD",
    "FIXED_ADD",
    "FIXTURE_EMAIL",
    "FIXTURE_EPOCH",
    "FIXTURE_NAME",
    "FIXTURE_STEP",
    "TEST_ADD",
    "BugfixRepo",
    "FileContent",
    "FixtureRepo",
    "RepoFactory",
    "make_bugfix",
]
