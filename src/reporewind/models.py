"""Shared pydantic v2 domain models.

These are the values that flow between pipeline stages and end up in task
bundles, so they are immutable (``frozen``), reject unknown fields and validate
their own invariants: a ``ResolvedFix`` whose base is not a parent of the fix,
or a ``FilePatch`` that writes outside the repository, cannot be constructed.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from reporewind.errors import InvalidRepoRefError

DEFAULT_HOST = "github.com"

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# scp-like SSH form: git@github.com:owner/repo.git
_SCP_RE = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>[^/].*)$")
_URL_SCHEMES = frozenset({"https", "http", "ssh", "git"})
_CONTROL_OR_SPACE = re.compile(r"[\s\x00-\x1f\x7f]")


def is_full_sha(value: str) -> bool:
    """True if ``value`` is a full lowercase SHA-1 (40) or SHA-256 (64) hex object id."""
    return bool(_SHA_RE.fullmatch(value))


def _lower_str(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


Sha = Annotated[
    str,
    BeforeValidator(_lower_str),
    StringConstraints(pattern=_SHA_RE.pattern),
]
"""A full git object id; input is trimmed and lowercased before validation."""


def _check_repo_path(value: str) -> str:
    if not value:
        raise ValueError("path must not be empty")
    if "\x00" in value:
        raise ValueError("path must not contain NUL")
    if value.startswith("/"):
        raise ValueError(f"path {value!r} must be relative to the repository root")
    segments = value.split("/")
    if any(seg in {"", ".", ".."} for seg in segments):
        raise ValueError(f"path {value!r} must not contain empty, '.' or '..' segments")
    if any(seg.lower() == ".git" for seg in segments):
        raise ValueError(f"path {value!r} must not point inside a .git directory")
    return value


RepoPath = Annotated[str, AfterValidator(_check_repo_path)]
"""A normalized POSIX path relative to the repository root that cannot escape it."""


def split_repo_ref(value: str) -> tuple[str, str, str]:
    """Split a repository reference into ``(host, owner, name)``.

    Accepted forms (a trailing ``.git`` and ``/`` are ignored)::

        owner/repo
        github.com/owner/repo
        https://github.com/owner/repo
        ssh://git@github.com[:22]/owner/repo
        git@github.com:owner/repo

    Anything else (extra path segments, embedded credentials, query strings,
    whitespace) is rejected with :class:`InvalidRepoRefError` rather than guessed.
    """
    raw = value
    value = value.strip()
    if not value:
        raise InvalidRepoRefError(raw, "empty reference")
    if _CONTROL_OR_SPACE.search(value):
        raise InvalidRepoRefError(raw, "whitespace or control characters are not allowed")

    host = DEFAULT_HOST
    if "://" in value:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        if scheme not in _URL_SCHEMES:
            raise InvalidRepoRefError(raw, f"unsupported URL scheme {parts.scheme!r}")
        if parts.query or parts.fragment:
            raise InvalidRepoRefError(raw, "query strings and fragments are not allowed")
        if parts.password is not None or (scheme in {"https", "http"} and parts.username):
            raise InvalidRepoRefError(
                raw, "credentials in URLs are not accepted; set GITHUB_TOKEN instead"
            )
        if not parts.hostname:
            raise InvalidRepoRefError(raw, "URL has no host")
        host = parts.hostname
        path = parts.path
    elif match := _SCP_RE.fullmatch(value):
        host = match["host"].lower()
        path = match["path"]
    else:
        path = value
        segments = path.strip("/").split("/")
        if len(segments) == 3 and "." in segments[0]:
            host, path = segments[0].lower(), "/".join(segments[1:])

    path = path.strip("/")
    path = path.removesuffix(".git")
    segments = path.split("/")
    if len(segments) != 2 or not all(segments):
        raise InvalidRepoRefError(raw, "expected exactly two path segments: owner/repo")
    owner, name = segments
    if not _HOST_RE.fullmatch(host):
        raise InvalidRepoRefError(raw, f"invalid host {host!r}")
    if not _OWNER_RE.fullmatch(owner):
        raise InvalidRepoRefError(raw, f"invalid owner {owner!r}")
    if not _NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise InvalidRepoRefError(raw, f"invalid repository name {name!r}")
    return host, owner, name


class RepoRef(BaseModel):
    """Identity of a hosted git repository: ``host``, ``owner`` and ``name``.

    Validating a plain string parses it with :func:`split_repo_ref`, so models
    that contain a ``RepoRef`` accept ``"owner/repo"`` or a clone URL directly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(default=DEFAULT_HOST, pattern=_HOST_RE.pattern)
    owner: str = Field(pattern=_OWNER_RE.pattern)
    name: str = Field(pattern=_NAME_RE.pattern)

    @model_validator(mode="before")
    @classmethod
    def _parse_string(cls, data: Any) -> Any:
        if isinstance(data, str):
            host, owner, name = split_repo_ref(data)
            return {"host": host, "owner": owner, "name": name}
        return data

    @field_validator("name")
    @classmethod
    def _reject_dot_names(cls, value: str) -> str:
        if value in {".", ".."} or value.endswith(".git"):
            raise ValueError(f"invalid repository name {value!r}")
        return value

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse any accepted reference form; raises :class:`InvalidRepoRefError`."""
        host, owner, name = split_repo_ref(value)
        return cls(host=host, owner=owner, name=name)

    @property
    def slug(self) -> str:
        """``owner/name``."""
        return f"{self.owner}/{self.name}"

    @property
    def clone_url(self) -> str:
        """Canonical anonymous HTTPS clone URL."""
        return f"https://{self.host}/{self.owner}/{self.name}.git"

    @property
    def web_url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.name}"

    @property
    def recipe_stem(self) -> str:
        """File stem for ``recipes/<owner>__<repo>.yaml`` (lowercased, hosts are case-blind)."""
        return f"{self.owner}__{self.name}".lower()

    def __str__(self) -> str:
        return self.slug if self.host == DEFAULT_HOST else f"{self.host}/{self.slug}"


class CommitInfo(BaseModel):
    """Metadata of one commit as recorded by git."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: Sha
    parents: tuple[Sha, ...] = ()
    author_date: AwareDatetime
    committer_date: AwareDatetime
    subject: str = ""

    @model_validator(mode="after")
    def _check_parents(self) -> Self:
        if self.sha in self.parents:
            raise ValueError("a commit cannot be its own parent")
        if len(set(self.parents)) != len(self.parents):
            raise ValueError("parent list contains duplicates")
        return self

    @property
    def is_root(self) -> bool:
        return not self.parents

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


class ChangeKind(StrEnum):
    """How a file changed between two commits (mode-only changes count as modified)."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"


class FilePatch(BaseModel):
    """The ``git diff`` of a single file, applicable on its own with ``git apply``.

    ``path`` is the file's path after the change (for deletions, the path that
    was removed); ``old_path`` is set only for renames and copies.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: RepoPath
    old_path: RepoPath | None = None
    change: ChangeKind
    binary: bool = False
    diff: str

    @field_validator("diff")
    @classmethod
    def _check_diff(cls, value: str) -> str:
        if not value.startswith("diff --git "):
            raise ValueError("diff must start with a 'diff --git' header")
        if not value.endswith("\n"):
            raise ValueError("diff must end with a newline so patches concatenate safely")
        return value

    @model_validator(mode="after")
    def _check_old_path(self) -> Self:
        needs_old = self.change in {ChangeKind.RENAMED, ChangeKind.COPIED}
        if needs_old and self.old_path is None:
            raise ValueError(f"{self.change.value} patch for {self.path!r} needs old_path")
        if not needs_old and self.old_path is not None:
            raise ValueError(f"old_path is only valid for renames and copies, not {self.change}")
        if self.old_path == self.path:
            raise ValueError("old_path must differ from path")
        return self

    @property
    def touched_paths(self) -> tuple[str, ...]:
        """Every repository path this patch reads or writes."""
        return (self.path,) if self.old_path is None else (self.old_path, self.path)


def join_patches(patches: tuple[FilePatch, ...] | list[FilePatch]) -> str:
    """Concatenate per-file patches into one patch accepted by ``git apply``."""
    return "".join(patch.diff for patch in patches)


class ResolvedFix(BaseModel):
    """A fix commit resolved against its base, with the diff split in two.

    ``source_patches`` is the fix itself (what an agent would have to write);
    ``test_patches`` are the test changes used to verify it. Both must be
    non-empty and no file may appear in both, which is what makes a
    fail-to-pass check meaningful.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: RepoRef
    fix: CommitInfo
    base: CommitInfo
    source_patches: tuple[FilePatch, ...]
    test_patches: tuple[FilePatch, ...]
    pr_number: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.base.sha not in self.fix.parents:
            raise ValueError(
                f"base {self.base.sha[:12]} is not a parent of fix {self.fix.sha[:12]}"
            )
        if not self.source_patches:
            raise ValueError("a fix needs at least one source (non-test) file change")
        if not self.test_patches:
            raise ValueError("a fix needs at least one test file change to verify it")
        seen: set[str] = set()
        for patch in (*self.source_patches, *self.test_patches):
            for path in patch.touched_paths:
                if path in seen:
                    raise ValueError(f"path {path!r} appears in more than one file patch")
                seen.add(path)
        return self

    @property
    def source_patch(self) -> str:
        """The fix as a single patch (source files only)."""
        return join_patches(self.source_patches)

    @property
    def test_patch(self) -> str:
        """The test changes as a single patch."""
        return join_patches(self.test_patches)

    @property
    def test_files(self) -> tuple[str, ...]:
        """Test files that exist after the fix (deleted test files excluded)."""
        return tuple(p.path for p in self.test_patches if p.change is not ChangeKind.DELETED)


__all__ = [
    "DEFAULT_HOST",
    "ChangeKind",
    "CommitInfo",
    "FilePatch",
    "RepoPath",
    "RepoRef",
    "ResolvedFix",
    "Sha",
    "is_full_sha",
    "join_patches",
    "split_repo_ref",
]
