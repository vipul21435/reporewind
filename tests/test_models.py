from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from reporewind.errors import InvalidRepoRefError
from reporewind.models import (
    ChangeKind,
    CommitInfo,
    FilePatch,
    RepoRef,
    ResolvedFix,
    is_full_sha,
    join_patches,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
WHEN = datetime(2021, 3, 4, 5, 6, 7, tzinfo=UTC)


def _diff(path: str, body: str = "@@ -1 +1 @@\n-old\n+new\n") -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{body}"


def _patch(path: str, change: ChangeKind = ChangeKind.MODIFIED, **kw: object) -> FilePatch:
    return FilePatch.model_validate({"path": path, "change": change, "diff": _diff(path), **kw})


def _commit(sha: str, *parents: str) -> CommitInfo:
    return CommitInfo(
        sha=sha, parents=parents, author_date=WHEN, committer_date=WHEN, subject="fix: x"
    )


# --- RepoRef -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("pallets/click", ("github.com", "pallets", "click")),
        ("  pallets/click.git/ ", ("github.com", "pallets", "click")),
        ("github.com/psf/requests", ("github.com", "psf", "requests")),
        ("https://github.com/psf/requests", ("github.com", "psf", "requests")),
        ("https://GitHub.com/psf/requests.git", ("github.com", "psf", "requests")),
        ("http://gitlab.example.org/team/tool/", ("gitlab.example.org", "team", "tool")),
        ("ssh://git@github.com/owner/repo.git", ("github.com", "owner", "repo")),
        ("ssh://git@github.com:2222/owner/repo", ("github.com", "owner", "repo")),
        ("git://example.org/owner/re_po", ("example.org", "owner", "re_po")),
        ("git@github.com:owner/my.repo.git", ("github.com", "owner", "my.repo")),
        ("deploy@Code.Example.org:o-w-n/r", ("code.example.org", "o-w-n", "r")),
    ],
)
def test_repo_ref_parses_accepted_forms(value: str, expected: tuple[str, str, str]) -> None:
    ref = RepoRef.parse(value)
    assert (ref.host, ref.owner, ref.name) == expected


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "empty"),
        ("owner/ repo", "whitespace"),
        ("owner", "two path segments"),
        ("a/b/c", "two path segments"),
        ("https://github.com/owner/repo/tree/main", "two path segments"),
        ("https://github.com/owner/repo?tab=readme", "query"),
        ("https://user:pw@github.com/owner/repo", "credentials"),
        ("https://token@github.com/owner/repo", "credentials"),
        ("ftp://example.org/owner/repo", "scheme"),
        ("file:///srv/owner/repo", "scheme"),
        ("https:///owner/repo", "no host"),
        ("-owner/repo", "invalid owner"),
        ("owner/..", "invalid repository name"),
        ("owner/.git", "two path segments"),
        ("owner/re$po", "invalid repository name"),
        ("git@bad_host!:owner/repo", "invalid owner"),
        ("git@-bad.example:owner/repo", "invalid host"),
    ],
)
def test_repo_ref_rejects_ambiguous_or_unsafe_forms(value: str, reason: str) -> None:
    with pytest.raises(InvalidRepoRefError, match=reason):
        RepoRef.parse(value)


def test_repo_ref_derived_names() -> None:
    ref = RepoRef.parse("git@github.com:Pallets/Click.git")
    assert ref.slug == "Pallets/Click"
    assert ref.clone_url == "https://github.com/Pallets/Click.git"
    assert ref.web_url == "https://github.com/Pallets/Click"
    assert ref.recipe_stem == "pallets__click"
    assert str(ref) == "Pallets/Click"
    assert str(RepoRef.parse("https://gitlab.com/a/b")) == "gitlab.com/a/b"


def test_repo_ref_validates_from_a_plain_string_and_round_trips() -> None:
    ref = RepoRef.model_validate("https://github.com/psf/requests.git")
    assert ref == RepoRef(owner="psf", name="requests")
    assert RepoRef.model_validate_json(ref.model_dump_json()) == ref


@pytest.mark.parametrize(
    "fields",
    [
        {"owner": "ok", "name": "bad name"},
        {"owner": "ok", "name": ".."},
        {"owner": "ok", "name": "repo.git"},
        {"owner": "ok", "name": "r", "host": "UPPER.example"},
        {"owner": "ok", "name": "r", "extra": 1},
    ],
)
def test_repo_ref_direct_construction_is_validated(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RepoRef.model_validate(fields)


def test_repo_ref_string_errors_surface_as_validation_errors() -> None:
    with pytest.raises(ValidationError, match="two path segments"):
        RepoRef.model_validate("just-a-name")


def test_repo_ref_is_frozen_and_hashable() -> None:
    ref = RepoRef.parse("a/b")
    with pytest.raises(ValidationError):
        ref.name = "c"  # type: ignore[misc]
    assert len({ref, RepoRef.parse("https://github.com/a/b")}) == 1


# --- CommitInfo --------------------------------------------------------------


def test_sha_is_normalized_and_length_checked() -> None:
    commit = _commit(" " + "A" * 40 + " ", SHA_B)
    assert commit.sha == SHA_A
    assert is_full_sha(SHA_A)
    assert is_full_sha("0" * 64)
    assert not is_full_sha("abc123")
    assert not is_full_sha("A" * 40)
    with pytest.raises(ValidationError):
        _commit("abc1234")


def test_commit_root_and_merge_flags() -> None:
    assert _commit(SHA_A).is_root
    assert not _commit(SHA_A).is_merge
    merge = _commit(SHA_A, SHA_B, SHA_C)
    assert merge.is_merge
    assert not merge.is_root


@pytest.mark.parametrize("parents", [(SHA_A,), (SHA_B, SHA_B)])
def test_commit_rejects_impossible_parent_lists(parents: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        _commit(SHA_A, *parents)


def test_commit_dates_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        CommitInfo(
            sha=SHA_A,
            author_date=datetime(2021, 1, 1),  # naive on purpose
            committer_date=WHEN,
        )
    ist = timezone(timedelta(hours=5, minutes=30))
    commit = CommitInfo(sha=SHA_A, author_date=WHEN.astimezone(ist), committer_date=WHEN)
    assert commit.author_date == commit.committer_date


# --- FilePatch ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["", "/etc/passwd", "../outside", "a/../b", "a//b", "a/./b", "src/", ".git/config",
     "vendor/.GIT/hooks/pre-commit", "nul\x00byte"],
)  # fmt: skip
def test_file_patch_rejects_paths_that_escape_the_repo(path: str) -> None:
    with pytest.raises(ValidationError):
        FilePatch(path=path, change=ChangeKind.MODIFIED, diff=_diff("x"))


def test_file_patch_rename_requires_distinct_old_path() -> None:
    with pytest.raises(ValidationError, match="needs old_path"):
        _patch("new.py", ChangeKind.RENAMED)
    with pytest.raises(ValidationError, match="must differ"):
        _patch("same.py", ChangeKind.COPIED, old_path="same.py")
    rename = _patch("pkg/new.py", ChangeKind.RENAMED, old_path="pkg/old.py")
    assert rename.touched_paths == ("pkg/old.py", "pkg/new.py")


def test_file_patch_old_path_only_for_renames_and_copies() -> None:
    with pytest.raises(ValidationError, match="only valid for renames"):
        _patch("a.py", ChangeKind.MODIFIED, old_path="b.py")
    assert _patch("a.py", ChangeKind.ADDED).touched_paths == ("a.py",)


@pytest.mark.parametrize(
    ("diff", "reason"),
    [("--- a/x\n+++ b/x\n", "diff --git"), ("diff --git a/x b/x", "end with a newline")],
)
def test_file_patch_diff_shape_is_checked(diff: str, reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        FilePatch(path="x", change=ChangeKind.MODIFIED, diff=diff)


def test_join_patches_concatenates_in_order() -> None:
    a, b = _patch("a.py"), _patch("b.py")
    assert join_patches([a, b]) == a.diff + b.diff
    assert join_patches(()) == ""


# --- ResolvedFix -------------------------------------------------------------


def _resolved(**overrides: object) -> ResolvedFix:
    data: dict[str, object] = {
        "repo": "owner/project",
        "fix": _commit(SHA_A, SHA_B),
        "base": _commit(SHA_B, SHA_C),
        "source_patches": [_patch("src/pkg/core.py")],
        "test_patches": [
            _patch("tests/test_core.py"),
            _patch("tests/test_gone.py", ChangeKind.DELETED),
        ],
    }
    data.update(overrides)
    return ResolvedFix.model_validate(data)


def test_resolved_fix_exposes_joined_patches_and_test_files() -> None:
    fix = _resolved(pr_number=42)
    assert fix.repo == RepoRef(owner="owner", name="project")
    assert fix.source_patch == fix.source_patches[0].diff
    assert fix.test_patch.count("diff --git") == 2
    assert fix.test_files == ("tests/test_core.py",)
    assert fix.pr_number == 42


def test_resolved_fix_json_round_trip_is_lossless() -> None:
    fix = _resolved()
    assert ResolvedFix.model_validate_json(fix.model_dump_json()) == fix


def test_resolved_fix_base_must_be_a_parent_of_the_fix() -> None:
    with pytest.raises(ValidationError, match="is not a parent"):
        _resolved(base=_commit(SHA_C))


def test_resolved_fix_accepts_any_parent_of_a_merge() -> None:
    fix = _resolved(fix=_commit(SHA_A, SHA_C, SHA_B))
    assert fix.base.sha == SHA_B


@pytest.mark.parametrize(
    ("field", "reason"), [("source_patches", "source"), ("test_patches", "test file")]
)
def test_resolved_fix_needs_both_halves(field: str, reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        _resolved(**{field: []})


def test_resolved_fix_rejects_a_path_in_two_patches() -> None:
    renamed = _patch("tests/test_core.py", ChangeKind.RENAMED, old_path="src/pkg/core.py")
    with pytest.raises(ValidationError, match="more than one file patch"):
        _resolved(test_patches=[renamed])


def test_resolved_fix_pr_number_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _resolved(pr_number=0)
