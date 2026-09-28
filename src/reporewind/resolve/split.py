"""Classify per-file patches as test changes or source changes.

A fix commit is split in two: the *test patch* (what proves the bug) and the
*source patch* (the fix an agent would have to write). The rule set is data,
not code, so a repository recipe can override it:

* ``test_dirs``: a file under a directory with one of these names is a test
  file (``tests/`` and ``test/`` by default, at any depth);
* ``test_files``: basename globs (``test_*.py``, ``*_test.py``, ``tests.py``,
  ``conftest.py``);
* ``include``: extra repository-path globs that always count as tests;
* ``exclude``: repository-path globs that never count as tests (wins over all).

Repository-path globs understand ``*`` (within one path segment), ``?``,
``**`` (any number of segments) and a trailing ``/`` (everything below a
directory). Matching is case-sensitive, like git.

A patch is a test change only if **every** path it touches is a test path, so
a rename that moves a file between test and source code lands in the source
patch: applying the test patch alone never edits source files.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, field_validator

from reporewind.models import FilePatch

DEFAULT_TEST_DIRS = ("tests", "test")
DEFAULT_TEST_FILES = ("test_*.py", "*_test.py", "tests.py", "conftest.py")


@lru_cache(maxsize=256)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a repository-path glob to an anchored regular expression."""
    if pattern.endswith("/"):
        pattern += "**"
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def glob_match(pattern: str, path: str) -> bool:
    """True if the repository path ``path`` matches ``pattern`` (see module docs)."""
    return glob_to_regex(pattern).fullmatch(path) is not None


def _check_globs(values: tuple[str, ...]) -> tuple[str, ...]:
    for value in values:
        if not value or value.startswith("/") or "\x00" in value:
            raise ValueError(f"invalid glob {value!r}: must be non-empty and relative")
    return values


class SplitRules(BaseModel):
    """Which repository paths count as tests when a fix diff is split."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    test_dirs: tuple[str, ...] = DEFAULT_TEST_DIRS
    test_files: tuple[str, ...] = DEFAULT_TEST_FILES
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @field_validator("test_dirs")
    @classmethod
    def _check_dirs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if not value or "/" in value or value in {".", ".."}:
                raise ValueError(f"invalid test directory name {value!r}: use a bare name")
        return values

    @field_validator("test_files")
    @classmethod
    def _check_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if "/" in value:
                raise ValueError(f"test file glob {value!r} matches basenames; use include")
        return _check_globs(values)

    @field_validator("include", "exclude")
    @classmethod
    def _check_path_globs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _check_globs(values)

    def is_test_path(self, path: str) -> bool:
        """True if ``path`` (relative to the repository root) is a test file."""
        if any(glob_match(pattern, path) for pattern in self.exclude):
            return False
        if any(glob_match(pattern, path) for pattern in self.include):
            return True
        *dirs, name = path.split("/")
        if any(part in self.test_dirs for part in dirs):
            return True
        return any(fnmatchcase(name, pattern) for pattern in self.test_files)

    def is_test_patch(self, patch: FilePatch) -> bool:
        """True if every path the patch touches is a test path."""
        return all(self.is_test_path(path) for path in patch.touched_paths)


@dataclass(frozen=True, slots=True)
class DiffSplit:
    """A fix diff split into source patches and test patches (diff order kept)."""

    source: tuple[FilePatch, ...]
    tests: tuple[FilePatch, ...]


def split_patches(patches: Iterable[FilePatch], rules: SplitRules | None = None) -> DiffSplit:
    """Partition ``patches`` into source and test changes using ``rules``."""
    active = rules or SplitRules()
    source: list[FilePatch] = []
    tests: list[FilePatch] = []
    for patch in patches:
        (tests if active.is_test_patch(patch) else source).append(patch)
    return DiffSplit(source=tuple(source), tests=tuple(tests))


__all__ = [
    "DEFAULT_TEST_DIRS",
    "DEFAULT_TEST_FILES",
    "DiffSplit",
    "SplitRules",
    "glob_match",
    "glob_to_regex",
    "split_patches",
]
