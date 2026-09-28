"""Read-only views of a repository tree for recipe detection.

Detection never checks anything out and never runs repository code: it
reads blobs straight from the object store at one commit
(:class:`GitTreeSource`, the plumbing equivalent of ``git show REV:PATH``
without textconv filters), so it works in a bare cache and leaves the
user's work tree alone. :class:`MemoryTreeSource` serves the same
interface from a mapping, for callers that already hold the files.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from reporewind.gitops import Git
from reporewind.models import check_repo_path


class TreeSource(Protocol):
    """The files of one repository snapshot."""

    @property
    def commit(self) -> str | None:
        """Full SHA the snapshot was read from, if it came from git."""
        ...

    def files(self) -> tuple[str, ...]:
        """Every file path in the snapshot, sorted."""
        ...

    def read(self, path: str) -> bytes | None:
        """Contents of ``path``, or ``None`` if it is not a file in the snapshot."""
        ...


class GitTreeSource:
    """The tree of commit ``rev`` in a git repository (bare or not)."""

    def __init__(self, git: Git, rev: str) -> None:
        self.git = git
        self._commit = git.rev_parse(rev)
        self._files: tuple[str, ...] | None = None
        self._index: frozenset[str] = frozenset()

    @property
    def commit(self) -> str:
        return self._commit

    def files(self) -> tuple[str, ...]:
        if self._files is None:
            self._files = self.git.list_files(self._commit)
            self._index = frozenset(self._files)
        return self._files

    def read(self, path: str) -> bytes | None:
        check_repo_path(path)
        self.files()
        if path not in self._index:
            return None
        return self.git.read_file(self._commit, path)


class MemoryTreeSource:
    """A snapshot held in memory: ``{path: text or bytes}``."""

    def __init__(self, files: Mapping[str, str | bytes], *, commit: str | None = None) -> None:
        self._files = {
            check_repo_path(path): content.encode("utf-8") if isinstance(content, str) else content
            for path, content in files.items()
        }
        self._commit = commit

    @property
    def commit(self) -> str | None:
        return self._commit

    def files(self) -> tuple[str, ...]:
        return tuple(sorted(self._files))

    def read(self, path: str) -> bytes | None:
        return self._files.get(check_repo_path(path))


__all__ = ["GitTreeSource", "MemoryTreeSource", "TreeSource"]
