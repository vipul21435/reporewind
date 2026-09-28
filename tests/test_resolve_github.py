"""GitHub PR resolution, replayed from recorded API responses (no network).

The JSON under tests/fixtures/github was recorded from the live API by
tests/fixtures/github/record.sh and trimmed to the fields the client reads.
Scenarios GitHub cannot be asked for on demand (a rebase merge, an octopus
merge, error statuses) are built in the tests from those payloads and are
marked as synthetic where they are made.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from reporewind.errors import GitHubAPIError, ResolveError
from reporewind.models import RepoRef
from reporewind.resolve.github import (
    API_VERSION,
    DEFAULT_API_URL,
    GitHubClient,
    MergeShape,
    PullRequestFix,
    api_url_for,
)

FIXTURES = Path(__file__).parent / "fixtures" / "github"
MARKUPSAFE = RepoRef.parse("pallets/markupsafe")
ATTRS = RepoRef.parse("python-attrs/attrs")
TOKEN = "test-token-not-a-real-secret"  # noqa: S105 - a dummy for header tests

Handler = Callable[[httpx.Request], httpx.Response]


def _fixture_file(request: httpx.Request) -> Path:
    page = request.url.params.get("page")
    rel = request.url.path.lstrip("/") + (f".page-{page}" if page else "") + ".json"
    return FIXTURES / rel


def _load(request_path: str) -> Any:
    return json.loads((FIXTURES / request_path.lstrip("/")).read_text())


def replay(
    seen: list[httpx.Request] | None = None,
    overrides: dict[str, Any] | None = None,
) -> httpx.MockTransport:
    """Serve recorded fixtures; ``overrides`` maps a URL path to a synthetic JSON body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if overrides and request.url.path in overrides:
            return httpx.Response(200, json=overrides[request.url.path])
        file = _fixture_file(request)
        if not file.is_file():
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, content=file.read_bytes())

    return httpx.MockTransport(handler)


def client(transport: httpx.BaseTransport, token: str | None = None) -> GitHubClient:
    return GitHubClient(token=token, transport=transport)


# --- recorded pull requests ----------------------------------------------------------


def test_merge_commit_pr_resolves_to_the_merge_with_mainline_1() -> None:
    seen: list[httpx.Request] = []
    with client(replay(seen)) as gh:
        pull = gh.resolve_pull_request(MARKUPSAFE, 477)
    assert isinstance(pull, PullRequestFix)
    assert pull.shape is MergeShape.MERGE_COMMIT
    assert pull.mainline == 1
    assert pull.fix_sha == "e85aff4d878aa458d5c1e879bf475d8483647f71"
    assert pull.base_sha.startswith("9c44ecf45141")
    assert pull.head_sha.startswith("8cb1691ca038")
    assert pull.title == "relax speedups str check"
    assert pull.merged_at.tzinfo is not None
    # A merge commit needs no look at the PR's own commits.
    assert [r.url.path for r in seen] == [
        "/repos/pallets/markupsafe/pulls/477",
        "/repos/pallets/markupsafe/commits/e85aff4d878aa458d5c1e879bf475d8483647f71",
    ]


def test_squashed_multi_commit_pr_resolves_to_the_single_landed_commit() -> None:
    seen: list[httpx.Request] = []
    with client(replay(seen)) as gh:
        pull = gh.resolve_pull_request(ATTRS, 1606)
    assert pull.shape is MergeShape.SINGLE_COMMIT
    assert pull.mainline is None
    assert pull.fix_sha == "f53fc5440d7f86aac4328aec7a563eb48634177f"
    assert pull.base_sha == "f38b8a3f1625060aa4245930822c34c11c252f83"
    # Two PR commits and one parent: the last PR commit is checked for a rebase merge.
    listing = seen[-1]
    assert listing.url.path == "/repos/python-attrs/attrs/pulls/1606/commits"
    assert dict(listing.url.params) == {"per_page": "100", "page": "1"}


def test_requests_carry_api_headers_and_only_an_explicit_token() -> None:
    seen: list[httpx.Request] = []
    with client(replay(seen)) as gh:
        gh.resolve_pull_request(MARKUPSAFE, 477)
    with client(replay(seen), token=TOKEN) as gh:
        gh.resolve_pull_request(MARKUPSAFE, 477)
    anonymous, authenticated = seen[0], seen[-1]
    assert anonymous.headers["Accept"] == "application/vnd.github+json"
    assert anonymous.headers["X-GitHub-Api-Version"] == API_VERSION
    assert anonymous.headers["User-Agent"].startswith("reporewind/")
    assert "Authorization" not in anonymous.headers
    assert authenticated.headers["Authorization"] == f"Bearer {TOKEN}"
    assert anonymous.url.host == "api.github.com"


def test_for_repo_reads_the_token_and_picks_the_api_host() -> None:
    seen: list[httpx.Request] = []
    enterprise = RepoRef.parse("https://git.example.com/pallets/markupsafe")
    gh = GitHubClient.for_repo(enterprise, env={"GITHUB_TOKEN": TOKEN}, transport=replay(seen))
    with gh, pytest.raises(GitHubAPIError):
        gh.resolve_pull_request(enterprise, 477)
    assert str(seen[0].url).startswith("https://git.example.com/api/v3/repos/pallets/")
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert not GitHubClient.for_repo(MARKUPSAFE, env={"GITHUB_TOKEN": ""}).authenticated
    assert api_url_for("github.com") == DEFAULT_API_URL


# --- synthetic shapes built from the recorded payloads -----------------------------------


def _attrs_overrides(**changes: Any) -> dict[str, Any]:
    pull = _load("repos/python-attrs/attrs/pulls/1606.json")
    landed_path = "/repos/python-attrs/attrs/commits/" + pull["merge_commit_sha"]
    landed = _load(landed_path + ".json")
    listing = _load("repos/python-attrs/attrs/pulls/1606/commits.page-1.json")
    for key, value in changes.items():
        target, field = key.split("__", 1)
        {"pull": pull, "landed": landed}[target][field] = value
    return {
        "/repos/python-attrs/attrs/pulls/1606": pull,
        landed_path: landed,
        "/repos/python-attrs/attrs/pulls/1606/commits": listing,
    }


def test_rebase_merged_multi_commit_pr_is_rejected() -> None:
    # Synthetic: the landed commit repeats the last PR commit, as a rebase merge does.
    overrides = _attrs_overrides()
    last = overrides["/repos/python-attrs/attrs/pulls/1606/commits"][-1]
    landed = overrides["/repos/python-attrs/attrs/commits/f53fc5440d7f86aac4328aec7a563eb48634177f"]
    landed["commit"] = last["commit"]
    with client(replay(overrides=overrides)) as gh, pytest.raises(ResolveError) as exc:
        gh.resolve_pull_request(ATTRS, 1606)
    assert "rebase-merged as 2 separate commits" in str(exc.value)


def test_single_commit_pr_skips_the_rebase_check() -> None:
    seen: list[httpx.Request] = []
    overrides = _attrs_overrides(pull__commits=1)
    with client(replay(seen, overrides)) as gh:
        pull = gh.resolve_pull_request(ATTRS, 1606)
    assert pull.shape is MergeShape.SINGLE_COMMIT
    assert len(seen) == 2


def test_the_last_page_of_pr_commits_is_requested() -> None:
    seen: list[httpx.Request] = []
    overrides = _attrs_overrides(pull__commits=201)
    with client(replay(seen, overrides)) as gh:
        gh.resolve_pull_request(ATTRS, 1606)
    assert seen[-1].url.params["page"] == "3"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"pull__merged": False, "pull__merged_at": None}, "is not merged"),
        ({"pull__merge_commit_sha": None}, "is not merged"),
        ({"pull__commits": 251}, "more than GitHub lists"),
        (
            {"landed__parents": [{"sha": "a" * 40}, {"sha": "b" * 40}, {"sha": "c" * 40}]},
            "3 parents",
        ),
        ({"landed__parents": []}, "0 parents"),
    ],
)
def test_unusable_pull_requests_are_rejected(changes: dict[str, Any], message: str) -> None:
    with (
        client(replay(overrides=_attrs_overrides(**changes))) as gh,
        pytest.raises(ResolveError) as exc,
    ):
        gh.resolve_pull_request(ATTRS, 1606)
    assert message in str(exc.value)


def test_invalid_pr_number_is_rejected_without_a_request() -> None:
    seen: list[httpx.Request] = []
    with client(replay(seen)) as gh, pytest.raises(ResolveError, match="invalid pull request"):
        gh.resolve_pull_request(ATTRS, 0)
    assert seen == []


# --- transport and status errors ------------------------------------------------------------


def _responding(response: httpx.Response | Exception) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    return httpx.MockTransport(handler)


RESET = int(datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC).timestamp())


@pytest.mark.parametrize(
    ("response", "token", "status", "message"),
    [
        (httpx.Response(404, json={"message": "Not Found"}), None, 404, "private repositories"),
        (
            httpx.Response(401, json={"message": "Bad credentials"}),
            TOKEN,
            401,
            "check GITHUB_TOKEN",
        ),
        (
            httpx.Response(
                403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(RESET)}
            ),
            None,
            403,
            "rate limit exceeded (resets 12:00:00 UTC); set GITHUB_TOKEN",
        ),
        (httpx.Response(429), TOKEN, 429, "rate limit exceeded"),
        (
            httpx.Response(403, json={"message": "Resource protected"}),
            None,
            403,
            "Resource protected",
        ),
        (
            httpx.Response(502, content=b"<html>bad gateway</html>"),
            None,
            502,
            "returned 502 for GET",
        ),
        (httpx.Response(200, content=b"{not json"), None, 200, "invalid JSON"),
    ],
)
def test_http_failures_become_github_api_errors(
    response: httpx.Response, token: str | None, status: int, message: str
) -> None:
    with client(_responding(response), token) as gh, pytest.raises(GitHubAPIError) as exc:
        gh.resolve_pull_request(ATTRS, 1606)
    assert exc.value.status == status
    assert message in str(exc.value)
    assert TOKEN not in str(exc.value)
    assert exc.value.exit_code == ResolveError.exit_code


def test_authenticated_errors_do_not_suggest_a_token() -> None:
    not_found = httpx.Response(404, json={"message": "Not Found"})
    limited = httpx.Response(403, headers={"x-ratelimit-remaining": "0"})
    for response in (not_found, limited):
        with client(_responding(response), TOKEN) as gh, pytest.raises(GitHubAPIError) as exc:
            gh.resolve_pull_request(ATTRS, 1606)
        assert "GITHUB_TOKEN" not in str(exc.value)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (httpx.ReadTimeout("slow"), "timed out"),
        (httpx.ConnectError("no route to host"), "request failed: GET /repos/python-attrs"),
    ],
)
def test_transport_failures_become_github_api_errors(error: Exception, message: str) -> None:
    with client(_responding(error)) as gh, pytest.raises(GitHubAPIError) as exc:
        gh.resolve_pull_request(ATTRS, 1606)
    assert message in str(exc.value)
    assert exc.value.status is None


@pytest.mark.parametrize(
    ("path", "body", "message"),
    [
        (
            "/repos/python-attrs/attrs/pulls/1606",
            {"number": 1606},
            "unexpected GitHub API response",
        ),
        ("/repos/python-attrs/attrs/pulls/1606/commits", {"not": "a list"}, "unexpected"),
        ("/repos/python-attrs/attrs/pulls/1606/commits", [{"sha": "zz"}], "unexpected"),
        ("/repos/python-attrs/attrs/pulls/1606/commits", [], "listed no commits"),
    ],
)
def test_unexpected_payloads_are_reported(path: str, body: Any, message: str) -> None:
    overrides = {**_attrs_overrides(), path: body}
    with client(replay(overrides=overrides)) as gh, pytest.raises(GitHubAPIError) as exc:
        gh.resolve_pull_request(ATTRS, 1606)
    assert message in str(exc.value)
