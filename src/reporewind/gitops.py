"""A small, hermetic, no-shell wrapper around the ``git`` CLI.

Design rules, each of which exists because the opposite broke something:

* **No shell, no option injection.** Commands are argument lists; revisions,
  remotes and URLs that start with ``-`` are rejected before git sees them, and
  ``--`` / ``--end-of-options`` separate options from operands.
* **Hermetic.** The user's global and system git config are ignored, as are
  inherited ``GIT_DIR``-style variables (set by hooks) and config injected via
  the environment, so a diff or a SHA never depends on the machine it ran on.
  ``GIT_CEILING_DIRECTORIES`` stops git from walking up into an enclosing
  repository when the target directory is not one itself.
* **Safe transports only.** ``GIT_ALLOW_PROTOCOL`` limits clones and fetches to
  file, git, http(s) and ssh; ``GIT_TERMINAL_PROMPT=0`` makes a credential
  prompt fail fast instead of hanging.
* **Stable output.** Diffs always use ``a/`` and ``b/`` prefixes, full blob ids
  and binary patches, so every patch can be re-applied with ``git apply``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from reporewind.errors import ConfigError, GitError, InvalidRevisionError, RevisionNotFoundError
from reporewind.models import CommitInfo, check_repo_path, is_full_sha
from reporewind.proc import CommandResult, CommandRunner, SubprocessRunner

DEFAULT_TIMEOUT = 600.0
ALLOWED_PROTOCOLS = "file:git:http:https:ssh"

# Variables that redirect git to another repository or inject configuration.
_SCRUBBED_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_EXTERNAL_DIFF",
        "GIT_DIFF_OPTS",
        "GIT_GLOB_PATHSPECS",
        "GIT_NOGLOB_PATHSPECS",
        "GIT_ICASE_PATHSPECS",
    }
)
_SCRUBBED_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_UNSAFE_REV = re.compile(r"[\s\x00-\x1f\x7f]")
# NUL-separated so a subject line can contain any printable text.
_COMMIT_FORMAT = "%H%x00%P%x00%aI%x00%cI%x00%s"


def hermetic_env(
    base: Mapping[str, str] | None = None, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return an environment in which git ignores machine-specific configuration.

    ``base`` defaults to ``os.environ``; ``extra`` is applied last (used, for
    example, to set a commit identity and fixed dates in test fixtures).
    """
    source = os.environ if base is None else base
    env = {
        key: value
        for key, value in source.items()
        if key not in _SCRUBBED_ENV and not key.startswith(_SCRUBBED_PREFIXES)
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": ALLOWED_PROTOCOLS,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_LITERAL_PATHSPECS": "1",
            "LC_ALL": "C",
        }
    )
    if extra:
        env.update(extra)
    return env


def check_revision(rev: str) -> str:
    """Reject revision strings git could misread as options; return ``rev`` unchanged."""
    if not rev:
        raise InvalidRevisionError(rev, "revision is empty")
    if rev.startswith("-"):
        raise InvalidRevisionError(rev, "revision must not start with '-'")
    if _UNSAFE_REV.search(rev):
        raise InvalidRevisionError(rev, "whitespace and control characters are not allowed")
    return rev


def _check_operand(value: str, what: str) -> str:
    if not value or value.startswith("-") or "\x00" in value:
        raise ConfigError(f"invalid {what} {value!r}: must be non-empty and not start with '-'")
    return value


class Git:
    """Run git commands against one repository directory.

    The directory does not have to exist yet: :meth:`init` and :meth:`clone_from`
    create it. All other methods require it to be a repository (a work tree, a
    linked worktree or a bare repository).
    """

    def __init__(
        self,
        repo_dir: Path | str,
        *,
        runner: CommandRunner | None = None,
        executable: str = "git",
        timeout: float = DEFAULT_TIMEOUT,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.repo_dir = Path(repo_dir).resolve()
        self.runner: CommandRunner = runner or SubprocessRunner()
        self.executable = executable
        self.timeout = timeout
        self._env = hermetic_env(
            extra={"GIT_CEILING_DIRECTORIES": str(self.repo_dir.parent), **(env or {})}
        )

    def __repr__(self) -> str:
        return f"Git({str(self.repo_dir)!r})"

    # -- low level -----------------------------------------------------------

    def run(
        self,
        *args: str,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        cwd: Path | None = None,
        error: type[GitError] = GitError,
    ) -> CommandResult:
        """Run ``git <args>`` in the repository and return the result.

        With ``check`` (the default) a non-zero exit raises ``error``. ``env``
        is layered over the hermetic environment for this one call.
        """
        argv = (self.executable, *args)
        result = self.runner.run(
            argv,
            cwd=cwd or self.repo_dir,
            env={**self._env, **env} if env else self._env,
            stdin=stdin,
            timeout=self.timeout,
        )
        if check and not result.ok:
            raise error(argv, result.returncode, result.stdout_text, result.stderr_text)
        return result

    def _text(self, *args: str) -> str:
        return self.run(*args).stdout_text

    # -- creating repositories -------------------------------------------------

    def init(self, *, bare: bool = False, initial_branch: str = "main") -> Git:
        """Create an empty repository in ``repo_dir`` (created if missing)."""
        check_revision(initial_branch)
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        args = ["init", "--quiet", f"--initial-branch={initial_branch}"]
        if bare:
            args.append("--bare")
        self.run(*args)
        return self

    def clone_from(
        self,
        url: str,
        *,
        bare: bool = False,
        no_checkout: bool = False,
        depth: int | None = None,
    ) -> Git:
        """Clone ``url`` (a URL or local path) into ``repo_dir``, which must not exist."""
        _check_operand(url, "clone source")
        if depth is not None and depth < 1:
            raise ValueError("depth must be >= 1")
        self.repo_dir.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--quiet", "--no-recurse-submodules"]
        if bare:
            args.append("--bare")
        if no_checkout:
            args.append("--no-checkout")
        if depth is not None:
            args += ["--depth", str(depth)]
        self.run(*args, "--", url, str(self.repo_dir), cwd=self.repo_dir.parent)
        return self

    def is_repo(self) -> bool:
        """True if ``repo_dir`` itself is a git repository (enclosing repos do not count)."""
        if not self.repo_dir.is_dir():
            return False
        return self.run("rev-parse", "--git-dir", check=False).ok

    # -- fetching --------------------------------------------------------------

    def fetch(self, remote: str, *refspecs: str, depth: int | None = None) -> None:
        """Fetch ``refspecs`` from ``remote`` (a remote name, URL or path) without tags."""
        _check_operand(remote, "remote")
        for refspec in refspecs:
            _check_operand(refspec, "refspec")
        if depth is not None and depth < 1:
            raise ValueError("depth must be >= 1")
        args = ["fetch", "--quiet", "--no-tags", "--no-recurse-submodules"]
        if depth is not None:
            args += ["--depth", str(depth)]
        self.run(*args, "--", remote, *refspecs)

    def fetch_commit(self, remote: str, sha: str, *, depth: int | None = None) -> str:
        """Fetch exactly one commit by full SHA (plus ``depth - 1`` ancestors).

        Hosts such as GitHub serve any reachable commit by id, so this avoids
        cloning the whole history. Returns the normalized SHA once the commit
        is present locally.
        """
        normalized = sha.strip().lower()
        if not is_full_sha(normalized):
            raise InvalidRevisionError(sha, "expected a full 40 or 64 character hex SHA")
        self.fetch(remote, normalized, depth=depth)
        return self.rev_parse(normalized)

    # -- queries ---------------------------------------------------------------

    def rev_parse(self, rev: str) -> str:
        """Resolve ``rev`` to the full SHA of a commit.

        Raises :class:`RevisionNotFoundError` if it names no commit and
        :class:`GitError` for other failures (for example, not a repository).
        """
        check_revision(rev)
        result = self.run(
            "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}", check=False
        )
        if result.returncode == 1:
            raise RevisionNotFoundError(result.argv, rev, result.stderr_text)
        if not result.ok:
            raise GitError(result.argv, result.returncode, result.stdout_text, result.stderr_text)
        return result.stdout_text.strip()

    def has_commit(self, rev: str) -> bool:
        """True if ``rev`` resolves to a commit present in this repository."""
        try:
            self.rev_parse(rev)
        except RevisionNotFoundError:
            return False
        return True

    def commit_info(self, rev: str) -> CommitInfo:
        """SHA, parents, author/committer dates and subject of one commit."""
        sha = self.rev_parse(rev)
        result = self.run(
            "show", "--no-patch", "--no-show-signature", f"--format={_COMMIT_FORMAT}", sha
        )
        # Subjects are for humans; replace undecodable bytes rather than failing.
        fields = result.stdout.decode("utf-8", "replace").rstrip("\n").split("\x00")
        full_sha, parents, author_date, committer_date, subject = fields
        return CommitInfo(
            sha=full_sha,
            parents=tuple(parents.split()),
            author_date=datetime.fromisoformat(author_date),
            committer_date=datetime.fromisoformat(committer_date),
            subject=subject,
        )

    def parents(self, rev: str) -> tuple[str, ...]:
        """Parent SHAs of a commit, in order (empty for a root commit)."""
        return self.commit_info(rev).parents

    def commit_date(self, rev: str) -> datetime:
        """Committer date (when the commit landed), timezone-aware."""
        return self.commit_info(rev).committer_date

    def diff(
        self,
        base: str,
        head: str,
        *,
        paths: Sequence[str] = (),
        find_renames: bool = True,
    ) -> str:
        """Unified diff from ``base`` to ``head`` that ``git apply`` accepts as-is.

        Undecodable bytes are kept via ``surrogateescape`` so the patch can be
        encoded back byte-for-byte before it is applied.
        """
        base_sha, head_sha = self.rev_parse(base), self.rev_parse(head)
        checked = [check_repo_path(path) for path in paths]
        args = [
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            "--full-index",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--find-renames" if find_renames else "--no-renames",
            base_sha,
            head_sha,
            "--",
            *checked,
        ]
        return self._text(*args)

    def read_file(self, rev: str, path: str) -> bytes | None:
        """Contents of ``path`` at ``rev``, or ``None`` if it is not a file there."""
        spec = f"{self.rev_parse(rev)}:{check_repo_path(path)}"
        kind = self.run("cat-file", "-t", spec, check=False)
        if not kind.ok or kind.stdout_text.strip() != "blob":
            return None
        return self.run("cat-file", "blob", spec).stdout

    def list_files(self, rev: str) -> tuple[str, ...]:
        """Every file path in the tree of ``rev``, sorted, relative to the root."""
        out = self._text("ls-tree", "-r", "-z", "--name-only", "--full-tree", self.rev_parse(rev))
        return tuple(sorted(name for name in out.split("\x00") if name))


__all__ = ["ALLOWED_PROTOCOLS", "DEFAULT_TIMEOUT", "Git", "check_revision", "hermetic_env"]
