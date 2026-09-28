"""A local cache of bare repositories that holds only the commits asked for.

Resolving a fix needs the fix commit, its parents and their trees, not the
whole history. :class:`RepoCache` keeps one bare repository per hosted
repository under ``<root>/repos/<host>/<owner>__<name>.git`` and fetches a
single commit by SHA with ``--depth 2`` (the commit plus its parents). Each
fetched commit is pinned by a ref under ``refs/reporewind/commits/`` so
``git gc`` never prunes it, and a later request for a commit that is already
complete is answered offline.

A commit that is only present as the boundary of an earlier shallow fetch
(a parent whose own parents are missing) is fetched again, which deepens it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from reporewind.errors import InvalidRevisionError
from reporewind.gitops import DEFAULT_TIMEOUT, Git
from reporewind.models import RepoRef, is_full_sha
from reporewind.proc import CommandRunner

HOME_ENV = "REPOREWIND_HOME"
DEFAULT_HOME = ".reporewind"
PIN_REF_PREFIX = "refs/reporewind/commits/"


def home_dir(env: Mapping[str, str] | None = None) -> Path:
    """RepoRewind's state directory: ``$REPOREWIND_HOME`` or ``./.reporewind``."""
    source = os.environ if env is None else env
    return Path(source.get(HOME_ENV) or DEFAULT_HOME).expanduser()


class RepoCache:
    """Bare repositories under ``root``, one per hosted repository."""

    def __init__(
        self,
        root: Path | str,
        *,
        runner: CommandRunner | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.root = Path(root)
        self.runner = runner
        self.timeout = timeout

    def path_for(self, repo: RepoRef) -> Path:
        """Where the bare repository for ``repo`` lives (lowercased, hosts are case-blind)."""
        return self.root / "repos" / repo.host / f"{repo.recipe_stem}.git"

    def open(self, repo: RepoRef) -> Git:
        """The cached bare repository for ``repo``, created empty if missing."""
        git = Git(self.path_for(repo), runner=self.runner, timeout=self.timeout)
        if not git.is_repo():
            git.init(bare=True)
        return git

    def is_complete(self, git: Git, sha: str) -> bool:
        """True if ``sha`` and its parents are present (not a shallow boundary)."""
        return git.has_commit(sha) and sha not in git.shallow_commits()

    def ensure_commit(self, repo: RepoRef, sha: str, *, source: str | None = None) -> Git:
        """Make commit ``sha`` and its parents available locally; return the repository.

        ``source`` is the URL or path to fetch from (default: the canonical
        clone URL of ``repo``). A full SHA is required because hosts only
        serve commits by exact object id.
        """
        normalized = sha.strip().lower()
        if not is_full_sha(normalized):
            raise InvalidRevisionError(
                sha, "fetching a commit needs its full 40 or 64 character SHA"
            )
        git = self.open(repo)
        if not self.is_complete(git, normalized):
            git.fetch_commit(source or repo.clone_url, normalized, depth=2)
            git.update_ref(PIN_REF_PREFIX + normalized, normalized)
        return git


__all__ = ["DEFAULT_HOME", "HOME_ENV", "PIN_REF_PREFIX", "RepoCache", "home_dir"]
