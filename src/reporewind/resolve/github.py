"""Resolve a GitHub pull request number to its fix and base commits.

A merged pull request lands on the base branch in one of three shapes, and
only two of them are a single fix commit:

* **merge commit** (two parents): the fix is the merge commit and the base is
  its first parent, so it is resolved with ``mainline=1``;
* **squash merge**, or a rebase merge of a one-commit PR (one parent): the
  fix is that commit and the base is its parent;
* **rebase merge of several commits**: the change is spread over several
  commits on the base branch, and no single commit is the whole fix. This is
  detected (the landed commit repeats the message and author date of the
  PR's last commit) and rejected with a clear message instead of silently
  resolving only the last commit.

The client uses httpx with an injectable transport, so tests replay recorded
API responses and never touch the network. ``GITHUB_TOKEN`` is optional (the
anonymous API allows 60 requests an hour) and never appears in messages.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from reporewind import __version__
from reporewind.errors import GitHubAPIError, ResolveError
from reporewind.models import DEFAULT_HOST, RepoRef, Sha

DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
TOKEN_ENV = "GITHUB_TOKEN"  # noqa: S105 - the variable name, not a secret
PAGE_SIZE = 100
MAX_LISTED_COMMITS = 250  # the pulls/{n}/commits endpoint stops here


def api_url_for(host: str) -> str:
    """REST API root for a host: api.github.com, or ``/api/v3`` on GitHub Enterprise."""
    return DEFAULT_API_URL if host == DEFAULT_HOST else f"https://{host}/api/v3"


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Ref(_Payload):
    sha: Sha
    ref: str = ""


class _PullPayload(_Payload):
    number: int
    title: str = ""
    merged: bool = False
    merged_at: AwareDatetime | None = None
    merge_commit_sha: Sha | None = None
    commits: int = Field(default=1, ge=0)
    base: _Ref
    head: _Ref


class _Signature(_Payload):
    date: AwareDatetime


class _GitCommit(_Payload):
    message: str
    author: _Signature


class _Parent(_Payload):
    sha: Sha


class _CommitPayload(_Payload):
    sha: Sha
    parents: tuple[_Parent, ...] = ()
    commit: _GitCommit


class MergeShape(StrEnum):
    """How a merged pull request landed on its base branch."""

    MERGE_COMMIT = "merge-commit"
    SINGLE_COMMIT = "single-commit"


class PullRequestFix(BaseModel):
    """The single commit that landed a pull request, and the base to measure it against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: RepoRef
    number: int = Field(ge=1)
    title: str
    fix_sha: Sha
    base_sha: Sha
    head_sha: Sha
    mainline: int | None = Field(default=None, ge=1)
    shape: MergeShape
    merged_at: AwareDatetime


class GitHubClient:
    """A small, typed client for the few GitHub REST endpoints RepoRewind needs."""

    def __init__(
        self,
        *,
        token: str | None = None,
        api_url: str = DEFAULT_API_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": f"reporewind/{__version__}",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.authenticated = bool(token)
        self._http = httpx.Client(
            base_url=api_url,
            headers=headers,
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
        )

    @classmethod
    def for_repo(
        cls,
        repo: RepoRef,
        *,
        env: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> GitHubClient:
        """A client for ``repo``'s host, authenticated with ``$GITHUB_TOKEN`` if it is set."""
        source = os.environ if env is None else env
        return cls(
            token=source.get(TOKEN_ENV) or None,
            api_url=api_url_for(repo.host),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- transport -------------------------------------------------------------

    def get_json(self, path: str, params: Mapping[str, str | int] | None = None) -> Any:
        """GET ``path`` and return the decoded JSON; raise :class:`GitHubAPIError` on failure."""
        what = f"GET {path}"
        try:
            response = self._http.get(path, params=dict(params or {}))
        except httpx.TimeoutException:
            raise GitHubAPIError(f"GitHub API request timed out: {what}") from None
        except httpx.HTTPError as exc:
            raise GitHubAPIError(f"GitHub API request failed: {what}: {exc}") from None
        if response.status_code >= 400:
            raise self._status_error(response, what)
        try:
            return response.json()
        except ValueError:
            raise GitHubAPIError(
                f"GitHub API returned invalid JSON for {what}", status=response.status_code
            ) from None

    def _status_error(self, response: httpx.Response, what: str) -> GitHubAPIError:
        status = response.status_code
        hint = "" if self.authenticated else f"; set {TOKEN_ENV} for a higher limit"
        exhausted = response.headers.get("x-ratelimit-remaining") == "0"
        if status == 429 or (status == 403 and exhausted):
            reset = response.headers.get("x-ratelimit-reset", "")
            when = (
                datetime.fromtimestamp(int(reset), UTC).strftime(" (resets %H:%M:%S UTC)")
                if reset.isdigit()
                else ""
            )
            return GitHubAPIError(f"GitHub API rate limit exceeded{when}{hint}", status=status)
        if status == 401:
            return GitHubAPIError(
                f"GitHub rejected the credentials (401) for {what}; check {TOKEN_ENV}",
                status=status,
            )
        if status == 404:
            private = "" if self.authenticated else f" (private repositories need {TOKEN_ENV})"
            return GitHubAPIError(f"not found (404): {what}{private}", status=status)
        try:
            message = str(response.json().get("message", ""))
        except (ValueError, AttributeError):
            message = ""
        detail = f": {message}" if message else ""
        return GitHubAPIError(f"GitHub API returned {status} for {what}{detail}", status=status)

    def _get_model[M: _Payload](self, model: type[M], path: str) -> M:
        data = self.get_json(path)
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise GitHubAPIError(f"unexpected GitHub API response for GET {path}: {exc}") from None

    # -- endpoints ---------------------------------------------------------------

    def _pull(self, repo: RepoRef, number: int) -> _PullPayload:
        return self._get_model(_PullPayload, f"/repos/{repo.owner}/{repo.name}/pulls/{number}")

    def _commit(self, repo: RepoRef, sha: str) -> _CommitPayload:
        return self._get_model(_CommitPayload, f"/repos/{repo.owner}/{repo.name}/commits/{sha}")

    def _last_pr_commit(self, repo: RepoRef, pull: _PullPayload) -> _CommitPayload:
        page = math.ceil(pull.commits / PAGE_SIZE)
        path = f"/repos/{repo.owner}/{repo.name}/pulls/{pull.number}/commits"
        data = self.get_json(path, {"per_page": PAGE_SIZE, "page": page})
        try:
            commits = [_CommitPayload.model_validate(item) for item in data]
        except (TypeError, ValidationError) as exc:
            raise GitHubAPIError(f"unexpected GitHub API response for GET {path}: {exc}") from None
        if not commits:
            raise GitHubAPIError(f"GitHub listed no commits for PR #{pull.number} ({path})")
        return commits[-1]

    def resolve_pull_request(self, repo: RepoRef, number: int) -> PullRequestFix:
        """Find the commit that landed PR ``number`` and the base to resolve it against."""
        if number < 1:
            raise ResolveError(f"invalid pull request number {number}")
        pull = self._pull(repo, number)
        if not pull.merged or pull.merged_at is None or pull.merge_commit_sha is None:
            raise ResolveError(
                f"PR #{number} in {repo} is not merged; only a merged pull request has a "
                "fix commit on its base branch"
            )
        landed = self._commit(repo, pull.merge_commit_sha)
        parents = [parent.sha for parent in landed.parents]
        short = landed.sha[:12]
        if len(parents) == 2:
            shape, mainline = MergeShape.MERGE_COMMIT, 1
        elif len(parents) == 1:
            shape, mainline = MergeShape.SINGLE_COMMIT, None
            if pull.commits > 1:
                self._reject_rebase_merge(repo, pull, landed)
        else:
            raise ResolveError(
                f"commit {short} that landed PR #{number} has {len(parents)} parents; "
                "expected a merge commit (2) or a squashed commit (1)"
            )
        return PullRequestFix(
            repo=repo,
            number=number,
            title=pull.title,
            fix_sha=landed.sha,
            base_sha=parents[0],
            head_sha=pull.head.sha,
            mainline=mainline,
            shape=shape,
            merged_at=pull.merged_at,
        )

    def _reject_rebase_merge(
        self, repo: RepoRef, pull: _PullPayload, landed: _CommitPayload
    ) -> None:
        if pull.commits > MAX_LISTED_COMMITS:
            raise ResolveError(
                f"PR #{pull.number} has {pull.commits} commits, more than GitHub lists "
                f"({MAX_LISTED_COMMITS}); pass the fix commit SHA directly"
            )
        last = self._last_pr_commit(repo, pull)
        if (
            last.commit.message == landed.commit.message
            and last.commit.author.date == landed.commit.author.date
        ):
            raise ResolveError(
                f"PR #{pull.number} was rebase-merged as {pull.commits} separate commits, "
                f"so no single commit is the whole fix; pass the SHA of the commit to "
                f"resolve (the last one landed as {landed.sha[:12]})"
            )


__all__ = [
    "API_VERSION",
    "DEFAULT_API_URL",
    "TOKEN_ENV",
    "GitHubClient",
    "MergeShape",
    "PullRequestFix",
    "api_url_for",
]
