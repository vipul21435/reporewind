from datetime import timedelta
from pathlib import Path

import pytest

from reporewind.errors import ResolveError
from reporewind.gitops import Git
from reporewind.models import ChangeKind, CommitInfo, RepoRef, ResolvedFix
from reporewind.resolve import (
    DiffSplit,
    SplitRules,
    dump_resolved_fix,
    load_resolved_fix,
    parse_patch,
    prove_split,
    resolve_fix,
    select_base,
)
from reporewind.testing import (
    BUGGY_ADD,
    FIXED_ADD,
    FIXTURE_EMAIL,
    FIXTURE_EPOCH,
    FIXTURE_NAME,
    BugfixRepo,
    FixtureRepo,
    RepoFactory,
)

REPO = RepoRef.parse("example/calc")
A, B, C = "a" * 40, "b" * 40, "c" * 40


def _info(*parents: str) -> CommitInfo:
    return CommitInfo(
        sha="f" * 40, parents=parents, author_date=FIXTURE_EPOCH, committer_date=FIXTURE_EPOCH
    )


# --- base selection -----------------------------------------------------------------


def test_select_base_for_an_ordinary_commit() -> None:
    assert select_base(_info(A)) == A
    assert select_base(_info(A), 1) == A


def test_select_base_rejects_root_commits() -> None:
    with pytest.raises(ResolveError, match="root commit"):
        select_base(_info())


def test_select_base_requires_a_mainline_for_merges() -> None:
    with pytest.raises(ResolveError, match=r"merge commit with 3 parents; pass a mainline \(1-3\)"):
        select_base(_info(A, B, C))
    assert select_base(_info(A, B, C), 1) == A
    assert select_base(_info(A, B, C), 3) == C


@pytest.mark.parametrize(("parents", "mainline"), [((A,), 2), ((A, B), 3), ((A, B), 0)])
def test_select_base_rejects_out_of_range_mainlines(
    parents: tuple[str, ...], mainline: int
) -> None:
    with pytest.raises(ResolveError, match="out of range"):
        select_base(_info(*parents), mainline)


# --- end to end on throwaway repositories ----------------------------------------------


def test_resolve_a_typical_fix(bugfix: BugfixRepo) -> None:
    git = bugfix.repo.git
    resolution = resolve_fix(git, REPO, bugfix.fix)
    fix = resolution.fix

    assert isinstance(fix, ResolvedFix)
    assert fix.repo == REPO
    assert (fix.fix.sha, fix.base.sha) == (bugfix.fix, bugfix.base)
    assert fix.fix.subject == "fix: add really adds"
    assert [p.path for p in fix.source_patches] == ["CHANGELOG.md", "src/calc/core.py"]
    assert [p.path for p in fix.test_patches] == ["tests/test_core.py"]
    assert fix.test_files == ("tests/test_core.py",)
    assert fix.pr_number is None

    proof = resolution.proof
    assert proof.fix_tree == git.tree_id(bugfix.fix)
    assert proof.base_tree == git.tree_id(bugfix.base)
    assert len({proof.base_tree, proof.source_tree, proof.test_tree, proof.fix_tree}) == 4


def test_resolved_patches_really_flip_the_work_tree(bugfix: BugfixRepo, tmp_path: Path) -> None:
    fix = resolve_fix(bugfix.repo.git, REPO, bugfix.fix).fix
    with bugfix.repo.git.worktree(bugfix.base, tmp_path / "wt") as wt:
        wt.apply(fix.test_patch)
        assert (wt.repo_dir / "src/calc/core.py").read_text() == BUGGY_ADD
        assert (wt.repo_dir / "tests/test_core.py").is_file()
        wt.apply(fix.source_patch)
        assert (wt.repo_dir / "src/calc/core.py").read_text() == FIXED_ADD


def test_resolve_accepts_any_revision_and_records_a_pr_number(bugfix: BugfixRepo) -> None:
    fix = resolve_fix(bugfix.repo.git, REPO, "HEAD", pr_number=7).fix
    assert fix.fix.sha == bugfix.fix
    assert fix.pr_number == 7


def test_resolve_a_fix_with_renames_binaries_and_deletions(repo_factory: RepoFactory) -> None:
    repo = repo_factory.create(
        files={
            "pkg/old.py": "".join(f"v{i} = {i}\n" for i in range(30)),
            "pkg/data.bin": b"\x00\x01\x02",
            "pkg/dead.py": "x = 1\n",
            "tests/test_old.py": "".join(f"# {i}\n" for i in range(30)),
            "tests/fixtures/blob.bin": b"\x00\xff",
        }
    )
    body = (repo.path / "pkg/old.py").read_text()
    repo.write(
        {
            "pkg/old.py": None,
            "pkg/new.py": body + "v30 = 30\n",
            "pkg/data.bin": b"\x00\x01\x03",
            "pkg/dead.py": None,
            "tests/fixtures/blob.bin": b"\x00\xfe\xfd",
        }
    )
    repo.rename("tests/test_old.py", "tests/unit/test_new.py")
    fix_sha = repo.commit("fix: reorganise")
    fix = resolve_fix(repo.git, REPO, fix_sha).fix

    changes = {p.path: p.change for p in (*fix.source_patches, *fix.test_patches)}
    assert changes == {
        "pkg/data.bin": ChangeKind.MODIFIED,
        "pkg/dead.py": ChangeKind.DELETED,
        "pkg/new.py": ChangeKind.RENAMED,
        "tests/fixtures/blob.bin": ChangeKind.MODIFIED,
        "tests/unit/test_new.py": ChangeKind.RENAMED,
    }
    tests = {p.path for p in fix.test_patches}
    assert tests == {"tests/fixtures/blob.bin", "tests/unit/test_new.py"}


def test_resolve_a_merge_commit_needs_a_mainline(repo_factory: RepoFactory) -> None:
    repo = repo_factory.create(files={"pkg/core.py": BUGGY_ADD})
    repo.branch("topic")
    repo.commit("docs: readme on main", {"README.md": "hi\n"})
    main_tip = repo.head
    repo.switch("topic")
    repo.commit("fix: add", {"pkg/core.py": FIXED_ADD})
    repo.commit("test: add", {"tests/test_core.py": "def test():\n    pass\n"})
    topic_tip = repo.head
    repo.switch("main")
    merge = repo.merge("topic", "Merge branch topic")

    with pytest.raises(ResolveError, match="merge commit with 2 parents"):
        resolve_fix(repo.git, REPO, merge)

    into_main = resolve_fix(repo.git, REPO, merge, mainline=1).fix
    assert into_main.base.sha == main_tip
    assert [p.path for p in into_main.source_patches] == ["pkg/core.py"]
    assert [p.path for p in into_main.test_patches] == ["tests/test_core.py"]

    # Against the topic side, the "fix" is main's README change: no tests.
    with pytest.raises(ResolveError, match="changes no test files"):
        resolve_fix(repo.git, REPO, merge, mainline=2)
    assert repo.git.parents(merge) == (main_tip, topic_tip)


def test_resolve_rejects_root_and_unsplittable_commits(repo_factory: RepoFactory) -> None:
    repo = repo_factory.create(files={"pkg/core.py": BUGGY_ADD, "tests/test_core.py": "# t\n"})
    root = repo.head
    with pytest.raises(ResolveError, match="root commit"):
        resolve_fix(repo.git, REPO, root)

    source_only = repo.commit("fix: no tests", {"pkg/core.py": FIXED_ADD})
    with pytest.raises(ResolveError, match="changes no test files"):
        resolve_fix(repo.git, REPO, source_only)

    tests_only = repo.commit("test: only", {"tests/test_core.py": "# t2\n"})
    with pytest.raises(ResolveError, match="only changes test files"):
        resolve_fix(repo.git, REPO, tests_only)

    stamp = (FIXTURE_EPOCH + timedelta(days=1)).isoformat()
    identity = {
        f"GIT_{who}_{what}": value
        for who in ("AUTHOR", "COMMITTER")
        for what, value in (("NAME", FIXTURE_NAME), ("EMAIL", FIXTURE_EMAIL), ("DATE", stamp))
    }
    repo.git.run("commit", "--quiet", "--allow-empty", "-m", "chore: empty", env=identity)
    with pytest.raises(ResolveError, match="does not change any file"):
        resolve_fix(repo.git, REPO, "HEAD")


def test_custom_rules_change_the_split(bugfix: BugfixRepo) -> None:
    rules = SplitRules(include=("CHANGELOG.md",))
    fix = resolve_fix(bugfix.repo.git, REPO, bugfix.fix, rules=rules).fix
    assert [p.path for p in fix.test_patches] == ["CHANGELOG.md", "tests/test_core.py"]
    spec_only = SplitRules(test_dirs=("spec",), test_files=("*_spec.py",))
    with pytest.raises(ResolveError, match="changes no test files"):
        resolve_fix(bugfix.repo.git, REPO, bugfix.fix, rules=spec_only)


def test_resolve_explains_a_missing_base_in_a_shallow_clone(
    bugfix: BugfixRepo, tmp_path: Path
) -> None:
    shallow = Git(tmp_path / "shallow").clone_from(bugfix.repo.path.as_uri(), depth=1)
    with pytest.raises(ResolveError, match="not in the local repository"):
        resolve_fix(shallow, REPO, "HEAD")


# --- the proof itself -----------------------------------------------------------------


def _split(git: Git, base: str, fix: str) -> DiffSplit:
    patches = parse_patch(git.diff(base, fix))
    rules = SplitRules()
    return DiffSplit(
        source=tuple(p for p in patches if not rules.is_test_patch(p)),
        tests=tuple(p for p in patches if rules.is_test_patch(p)),
    )


def test_prove_split_detects_a_lossy_split(bugfix: BugfixRepo) -> None:
    git = bugfix.repo.git
    split = _split(git, bugfix.base, bugfix.fix)
    lossy = DiffSplit(source=split.source[1:], tests=split.tests)
    with pytest.raises(ResolveError, match="lost or changed content"):
        prove_split(git, bugfix.base, bugfix.fix, lossy)


@pytest.mark.parametrize("half", ["source", "test"])
def test_prove_split_names_the_half_that_does_not_apply(bugfix: BugfixRepo, half: str) -> None:
    git = bugfix.repo.git
    good = _split(git, bugfix.base, bugfix.fix)
    stale = _split(git, bugfix.fix, bugfix.base)  # reversed: cannot apply to base
    if half == "source":
        split = DiffSplit(source=stale.source, tests=good.tests)
    else:
        split = DiffSplit(source=good.source, tests=stale.tests)
    with pytest.raises(ResolveError, match=f"the {half} patch does not apply cleanly"):
        prove_split(git, bugfix.base, bugfix.fix, split)


def test_prove_split_detects_halves_that_conflict_with_each_other(
    repo_factory: RepoFactory,
) -> None:
    repo = repo_factory.create(files={"pkg/a.py": "a\n", "tests/test_a.py": "t\n"})
    base = repo.head
    fix = repo.commit("fix", {"pkg/a.py": "b\n", "tests/test_a.py": "u\n"})
    split = _split(repo.git, base, fix)
    # Both halves edit pkg/a.py: each applies alone, not together.
    clash = DiffSplit(source=split.source, tests=split.source)
    with pytest.raises(ResolveError, match="do not apply together"):
        prove_split(repo.git, base, fix, clash)


# --- JSON round trip --------------------------------------------------------------------


def test_json_round_trip_is_byte_exact_for_non_utf8_files(
    repo_factory: RepoFactory, tmp_path: Path
) -> None:
    repo: FixtureRepo = repo_factory.create(
        files={"pkg/legacy.py": b"x = 1  # caf\xe9\n", "tests/test_legacy.py": b"# \xff\n"}
    )
    base = repo.head
    fix_sha = repo.commit(
        "fix: latin-1",
        {"pkg/legacy.py": b"x = 2  # caf\xe9\n", "tests/test_legacy.py": b"# \xfe\n"},
    )
    fix = resolve_fix(repo.git, REPO, fix_sha).fix
    text = dump_resolved_fix(fix)
    assert text.isascii()
    assert "\\udce9" in text
    loaded = load_resolved_fix(text)
    assert loaded == fix
    with repo.git.worktree(base, tmp_path / "wt") as wt:
        wt.apply(loaded.source_patch + loaded.test_patch)
        assert (wt.repo_dir / "pkg/legacy.py").read_bytes() == b"x = 2  # caf\xe9\n"
        assert (wt.repo_dir / "tests/test_legacy.py").read_bytes() == b"# \xfe\n"


@pytest.mark.parametrize("text", ["not json", "{}", '{"repo": "example/calc"}'])
def test_load_resolved_fix_rejects_invalid_input(text: str) -> None:
    with pytest.raises(ResolveError, match="invalid ResolvedFix JSON"):
        load_resolved_fix(text)
