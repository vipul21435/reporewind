from pathlib import Path

import pytest

from reporewind.errors import ConfigError, InvalidRevisionError, PatchApplyError
from reporewind.gitops import Git, hermetic_env
from reporewind.testing import BugfixRepo, RepoFactory


def test_tree_id_matches_git(bugfix: BugfixRepo) -> None:
    git = bugfix.repo.git
    expected = git.run("rev-parse", f"{bugfix.fix}^{{tree}}").stdout_text.strip()
    assert git.tree_id(bugfix.fix) == expected
    assert git.tree_id(bugfix.fix) != git.tree_id(bugfix.base)


def test_apply_to_tree_reproduces_the_fix_tree_without_side_effects(bugfix: BugfixRepo) -> None:
    git = bugfix.repo.git
    index = (bugfix.repo.path / ".git" / "index").read_bytes()
    patch = git.diff(bugfix.base, bugfix.fix)

    assert git.apply_to_tree(bugfix.base, patch) == git.tree_id(bugfix.fix)
    assert git.apply_to_tree(bugfix.base) == git.tree_id(bugfix.base)
    assert git.apply_to_tree(bugfix.base, "", patch, "") == git.tree_id(bugfix.fix)

    # HEAD, the real index, the work tree and the worktree list are untouched.
    assert git.rev_parse("HEAD") == bugfix.fix
    assert (bugfix.repo.path / ".git" / "index").read_bytes() == index
    assert git.changed_paths() == ()
    assert git.run("worktree", "list", "--porcelain").stdout_text.count("worktree ") == 1


def test_apply_to_tree_applies_patches_in_order(bugfix: BugfixRepo) -> None:
    git = bugfix.repo.git
    root = git.parents(bugfix.base)[0]
    first, second = git.diff(root, bugfix.base), git.diff(bugfix.base, bugfix.fix)
    assert git.apply_to_tree(root, first, second) == git.tree_id(bugfix.fix)
    with pytest.raises(PatchApplyError):
        git.apply_to_tree(root, second, second)


def test_apply_to_tree_works_in_a_bare_repository(bugfix: BugfixRepo, tmp_path: Path) -> None:
    bare = Git(tmp_path / "bare.git").clone_from(str(bugfix.repo.path), bare=True)
    patch = bare.diff(bugfix.base, bugfix.fix)
    assert bare.apply_to_tree(bugfix.base, patch) == bare.tree_id(bugfix.fix)


def test_apply_to_tree_rejects_a_patch_that_does_not_apply(bugfix: BugfixRepo) -> None:
    git = bugfix.repo.git
    reverse = git.diff(bugfix.fix, bugfix.base)
    with pytest.raises(PatchApplyError) as exc:
        git.apply_to_tree(bugfix.base, reverse)
    assert "apply" in exc.value.argv


def test_update_ref_pins_a_commit(bugfix: BugfixRepo) -> None:
    git = bugfix.repo.git
    git.update_ref("refs/reporewind/commits/x", bugfix.base)
    assert git.rev_parse("refs/reporewind/commits/x") == bugfix.base
    with pytest.raises(ConfigError, match="must start with 'refs/'"):
        git.update_ref("heads/main", bugfix.base)
    with pytest.raises(InvalidRevisionError):
        git.update_ref("refs/has space", bugfix.base)


def test_shallow_boundary_keeps_its_recorded_parents(bugfix: BugfixRepo, tmp_path: Path) -> None:
    full = bugfix.repo.git
    cache = Git(tmp_path / "cache.git").init(bare=True)
    assert cache.shallow_commits() == frozenset()

    cache.fetch_commit(str(bugfix.repo.path), bugfix.fix, depth=2)
    assert cache.shallow_commits() == {bugfix.base}
    # `%P` would report no parents for the boundary; the raw object still has them.
    assert cache.commit_info(bugfix.base).parents == full.commit_info(bugfix.base).parents
    assert cache.commit_info(bugfix.base).parents != ()
    assert not cache.has_commit(cache.commit_info(bugfix.base).parents[0])


def test_merge_parents_are_listed_in_order(repo_factory: RepoFactory) -> None:
    repo = repo_factory.create(files={"a.txt": "a\n"})
    repo.branch("topic")
    main_tip = repo.commit("feat: main", {"m.txt": "m\n"})
    repo.switch("topic")
    topic_tip = repo.commit("feat: topic", {"t.txt": "t\n"})
    repo.switch("main")
    merge = repo.merge("topic", "merge topic")
    assert repo.git.parents(merge) == (main_tip, topic_tip)


def test_hermetic_env_ignores_replace_objects() -> None:
    assert hermetic_env({})["GIT_NO_REPLACE_OBJECTS"] == "1"
