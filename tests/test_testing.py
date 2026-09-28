from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reporewind.errors import GitError, InvalidPathError
from reporewind.testing import (
    BUGGY_ADD,
    FIXED_ADD,
    FIXTURE_EMAIL,
    FIXTURE_EPOCH,
    FIXTURE_STEP,
    TEST_ADD,
    RepoFactory,
    make_bugfix,
)

# SHA of `create(files={"README.md": "hello\n"})`. Pinned so that a change in
# identity, clock or git invocation that would make fixture SHAs machine- or
# run-dependent fails loudly here (CI and laptops must agree).
PINNED_INITIAL_SHA = "57eaf81e63058a3a5662ae70a40aac69aefd9ae4"


def test_same_calls_give_the_same_shas_everywhere(tmp_path: Path) -> None:
    heads = []
    for root in ("one", "two"):
        repo = RepoFactory(tmp_path / root).create(files={"README.md": "hello\n"})
        assert repo.head == PINNED_INITIAL_SHA
        heads.append(repo.commit("feat: more", {"pkg/a.py": "x = 1\n"}))
    assert heads[0] == heads[1]


def test_commits_use_a_fixed_identity_and_a_strictly_increasing_clock(
    repo_factory: RepoFactory,
) -> None:
    repo = repo_factory.create(files={"a.txt": "1\n"})
    repo.commit("second", {"a.txt": "2\n"})
    first, second = repo.git.commit_info("HEAD~1"), repo.git.commit_info("HEAD")
    assert first.committer_date == first.author_date == FIXTURE_EPOCH
    assert second.committer_date == FIXTURE_EPOCH + FIXTURE_STEP
    email = repo.git.run("log", "-1", "--format=%ae %ce").stdout_text.split()
    assert email == [FIXTURE_EMAIL, FIXTURE_EMAIL]


def test_explicit_dates_keep_their_timezone(repo_factory: RepoFactory) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    repo = repo_factory.create()
    repo.commit("dated", {"a": "1"}, when=datetime(2019, 12, 31, 23, 0, tzinfo=ist))
    date = repo.git.commit_date("HEAD")
    assert date.utcoffset() == timedelta(hours=5, minutes=30)
    assert date.isoformat() == "2019-12-31T23:00:00+05:30"


def test_naive_dates_are_rejected(repo_factory: RepoFactory) -> None:
    repo = repo_factory.create()
    with pytest.raises(ValueError, match="timezone-aware"):
        repo.commit("naive", {"a": "1"}, when=datetime(2020, 1, 1))  # naive on purpose


def test_write_keeps_bytes_exact_and_deletes_with_none(repo_factory: RepoFactory) -> None:
    repo = repo_factory.create(files={"crlf.txt": "a\r\nb", "blob.bin": b"\x00\xff", "gone": "x"})
    assert (repo.path / "crlf.txt").read_bytes() == b"a\r\nb"
    assert repo.git.read_file("HEAD", "blob.bin") == b"\x00\xff"
    repo.commit("chore: delete", {"gone": None})
    assert repo.git.read_file("HEAD", "gone") is None
    assert repo.git.list_files("HEAD") == ("blob.bin", "crlf.txt")


@pytest.mark.parametrize("bad", ["../escape.txt", "/abs.txt", ".git/config"])
def test_write_refuses_paths_outside_the_work_tree(repo_factory: RepoFactory, bad: str) -> None:
    repo = repo_factory.create()
    with pytest.raises(InvalidPathError):
        repo.write({bad: "x"})


def test_empty_commit_is_an_error(repo_factory: RepoFactory) -> None:
    repo = repo_factory.create(files={"a": "1"})
    with pytest.raises(GitError, match="nothing to commit"):
        repo.commit("nothing changed")


def test_create_refuses_to_reuse_a_directory(repo_factory: RepoFactory) -> None:
    repo_factory.create("same")
    with pytest.raises(FileExistsError):
        repo_factory.create("same")


def test_branches_merges_renames_and_tags(repo_factory: RepoFactory) -> None:
    repo = repo_factory.create(files={"src/old.py": "value = 1\n"})
    base = repo.head
    repo.branch("feature")
    repo.switch("feature")
    repo.rename("src/old.py", "src/new/mod.py")
    side = repo.commit("refactor: move module")
    repo.switch("main")
    main_tip = repo.commit("docs: readme", {"README.md": "hi\n"})
    merge = repo.merge("feature", "Merge branch feature")
    repo.tag("v1.0", base)

    assert repo.git.parents(merge) == (main_tip, side)
    assert repo.git.rev_parse("v1.0") == base
    assert repo.git.list_files(merge) == ("README.md", "src/new/mod.py")
    assert repo.git.commit_date(merge) == FIXTURE_EPOCH + 3 * FIXTURE_STEP


def test_bugfix_scenario_is_deterministic_and_well_formed(repo_factory: RepoFactory) -> None:
    bugfix = make_bugfix(repo_factory)
    assert (bugfix.base, bugfix.fix) == (
        "08c803cf23c324844811a367b41c8b50bb016ad6",
        "9adce53ef91b0a3d0856f8cb3bce59cdddc9728a",
    )
    git = bugfix.repo.git
    assert git.parents(bugfix.fix) == (bugfix.base,)
    assert git.read_file(bugfix.base, "src/calc/core.py") == BUGGY_ADD.encode()
    assert git.read_file(bugfix.fix, "src/calc/core.py") == FIXED_ADD.encode()
    assert git.read_file(bugfix.base, "tests/test_core.py") is None
    assert git.read_file(bugfix.fix, "tests/test_core.py") == TEST_ADD.encode()
