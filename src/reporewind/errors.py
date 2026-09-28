"""Typed error hierarchy for RepoRewind.

Every error the pipeline raises on purpose derives from :class:`RepoRewindError`
and carries a stable process ``exit_code``, so the CLI can map failures to exit
codes without string matching and callers can catch a whole stage at once::

    RepoRewindError                 1  unexpected pipeline failure
    +-- ConfigError                 2  bad user input (also a ValueError)
    |   +-- InvalidRepoRefError        unparseable repository reference
    |   +-- InvalidRevisionError       revision string that is unsafe to pass to git
    |   +-- InvalidPathError           path that escapes the repository or touches .git
    +-- ToolNotFoundError           3  git / uv / docker missing from PATH
    +-- CommandError                4  an external command failed
    |   +-- CommandTimeoutError        ... or ran past its timeout
    |   +-- GitError                   a git command failed
    |       +-- RevisionNotFoundError  revision does not name a commit
    |       +-- PatchApplyError        patch does not apply to the work tree
    +-- ResolveError               10  commit resolution / diff splitting
    |   +-- PatchParseError            a diff that cannot be split into file patches
    +-- RecipeError                11  build recipe detection or validation
    +-- PinError                   12  Python / dependency / base image pinning
    +-- BuildError                 13  Docker image build
    +-- VerifyError                14  fail-to-pass verification
    +-- BundleError                15  task bundle export or validation
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from typing import ClassVar

_STDERR_TAIL_LINES = 20


class RepoRewindError(Exception):
    """Base class for every error RepoRewind raises deliberately."""

    exit_code: ClassVar[int] = 1


class ConfigError(RepoRewindError, ValueError):
    """Invalid user input. Also a ``ValueError`` so pydantic validators can raise it."""

    exit_code: ClassVar[int] = 2


class InvalidRepoRefError(ConfigError):
    """A repository reference that is not ``owner/repo``, HTTPS or SSH form."""

    def __init__(self, value: str, reason: str) -> None:
        self.value = value
        self.reason = reason
        super().__init__(f"invalid repository reference {value!r}: {reason}")


class InvalidRevisionError(ConfigError):
    """A revision string that could be misread by git as an option or is malformed."""

    def __init__(self, value: str, reason: str) -> None:
        self.value = value
        self.reason = reason
        super().__init__(f"invalid revision {value!r}: {reason}")


class InvalidPathError(ConfigError):
    """A repository path that is absolute, escapes the root or points into ``.git``."""

    def __init__(self, value: str, reason: str) -> None:
        self.value = value
        self.reason = reason
        super().__init__(f"invalid repository path {value!r}: {reason}")


class ToolNotFoundError(RepoRewindError):
    """A required executable is not installed or not on ``PATH``."""

    exit_code: ClassVar[int] = 3

    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__(f"required tool {tool!r} was not found on PATH")


class CommandError(RepoRewindError):
    """An external command exited non-zero.

    The full ``argv``, exit status and captured output are kept on the exception
    so callers (and bug reports) see exactly what ran without re-running it.
    """

    exit_code: ClassVar[int] = 4

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        *,
        message: str | None = None,
    ) -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        headline = message or f"command exited with status {returncode}"
        # Some tools (git commit, for one) explain failures on stdout only.
        detail = stderr_tail(stderr) or stderr_tail(stdout)
        text = f"{headline}: {shlex.join(self.argv)}"
        super().__init__(f"{text}\n{detail}" if detail else text)


class CommandTimeoutError(CommandError):
    """An external command was killed after running past its timeout."""

    def __init__(
        self, argv: Sequence[str], timeout: float, stdout: str = "", stderr: str = ""
    ) -> None:
        self.timeout = timeout
        super().__init__(argv, -1, stdout, stderr, message=f"command timed out after {timeout:g}s")


class GitError(CommandError):
    """A git command failed."""


class RevisionNotFoundError(GitError):
    """A revision does not resolve to a commit in the repository."""

    def __init__(self, argv: Sequence[str], revision: str, stderr: str = "") -> None:
        self.revision = revision
        super().__init__(
            argv, 1, "", stderr, message=f"revision {revision!r} does not name a commit"
        )


class PatchApplyError(GitError):
    """A patch does not apply cleanly to the target work tree."""


class ResolveError(RepoRewindError):
    """Commit resolution or diff splitting failed (root commit, no test changes, ...)."""

    exit_code: ClassVar[int] = 10


class PatchParseError(ResolveError):
    """A diff could not be split into per-file patches (malformed or not re-applicable)."""


class RecipeError(RepoRewindError):
    """A build recipe could not be detected, merged or validated."""

    exit_code: ClassVar[int] = 11


class PinError(RepoRewindError):
    """Python version, dependency or base image pinning failed."""

    exit_code: ClassVar[int] = 12


class BuildError(RepoRewindError):
    """Building the environment image failed or the build lock timed out."""

    exit_code: ClassVar[int] = 13


class VerifyError(RepoRewindError):
    """Fail-to-pass verification could not be carried out."""

    exit_code: ClassVar[int] = 14


class BundleError(RepoRewindError):
    """A task bundle could not be written or failed validation."""

    exit_code: ClassVar[int] = 15


def stderr_tail(stderr: str, lines: int = _STDERR_TAIL_LINES) -> str:
    """Return the last ``lines`` non-blank lines of ``stderr`` for error messages."""
    kept = [line for line in stderr.splitlines() if line.strip()]
    return "\n".join(kept[-lines:])
