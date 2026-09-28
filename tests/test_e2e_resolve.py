"""End-to-end resolution against the live GitHub API and git remotes.

Excluded from the default suite; run with `make e2e` (network needed,
GITHUB_TOKEN optional).
"""

from pathlib import Path

import pytest

from reporewind.models import RepoRef
from reporewind.resolve import GitHubClient, MergeShape, RepoCache, resolve_fix

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize(
    ("slug", "number", "shape", "source", "tests"),
    [
        (
            "pallets/markupsafe",
            477,
            MergeShape.MERGE_COMMIT,
            ["CHANGES.rst", "pyproject.toml", "src/markupsafe/_speedups.c"],
            ["tests/test_escape.py"],
        ),
        (
            "python-attrs/attrs",
            1606,
            MergeShape.SINGLE_COMMIT,
            ["changelog.d/1606.change.md", "src/attr/_make.py"],
            ["tests/test_functional.py"],
        ),
    ],
)
def test_resolve_real_pull_requests(
    tmp_path: Path,
    slug: str,
    number: int,
    shape: MergeShape,
    source: list[str],
    tests: list[str],
) -> None:
    repo = RepoRef.parse(slug)
    with GitHubClient.for_repo(repo) as client:
        pull = client.resolve_pull_request(repo, number)
    assert pull.shape is shape
    git = RepoCache(tmp_path).ensure_commit(repo, pull.fix_sha)
    resolution = resolve_fix(git, repo, pull.fix_sha, mainline=pull.mainline, pr_number=number)
    fix = resolution.fix
    assert fix.base.sha == pull.base_sha
    assert [p.path for p in fix.source_patches] == source
    assert [p.path for p in fix.test_patches] == tests
    assert resolution.proof.fix_tree == git.tree_id(pull.fix_sha)
