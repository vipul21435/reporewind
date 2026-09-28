import shutil
from pathlib import Path

import pytest

from reporewind.errors import ConfigError, PatchApplyError
from reporewind.gitops import Git
from reporewind.testing import FixtureRepo, RepoFactory

OLD_SRC = "def add(a, b):\n    return a - b\n"
NEW_SRC = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def fix_history(repo_factory: RepoFactory) -> tuple[FixtureRepo, str, str]:
    """A parent commit and a fix commit that edits source, edits a test and adds a test."""
    repo = repo_factory.create(
        files={
            ".gitignore": "*.pyc\n",
            "src/calc.py": OLD_SRC,
            "tests/test_calc.py": "def test_placeholder():\n    pass\n",
        }
    )
    base = repo.head
    fix = repo.commit(
        "fix: add really adds",
        {
            "src/calc.py": NEW_SRC,
            "tests/test_calc.py": "def test_add():\n    assert 2 + 2 == 4\n",
            "tests/test_new.py": "def test_new():\n    pass\n",
        },
    )
    return repo, base, fix


def test_checkout_detached_materialises_the_commit(
    fix_history: tuple[FixtureRepo, str, str], tmp_path: Path
) -> None:
    repo, base, fix = fix_history
    wt = repo.git.checkout_detached(base, tmp_path / "work" / "base")
    assert wt.repo_dir == (tmp_path / "work" / "base").resolve()
    assert wt.rev_parse("HEAD") == base
    assert (wt.repo_dir / "src" / "calc.py").read_text() == OLD_SRC
    assert not (wt.repo_dir / "tests" / "test_new.py").exists()
    assert wt.run("symbolic-ref", "--quiet", "HEAD", check=False).returncode == 1
    assert wt.changed_paths() == ()
    assert repo.git.rev_parse("HEAD") == fix
    assert repo.git.changed_paths() == ()


def test_checkout_detached_needs_a_missing_or_empty_dir(
    fix_history: tuple[FixtureRepo, str, str], tmp_path: Path
) -> None:
    repo, base, _ = fix_history
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("mine")
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    for target in (busy, a_file):
        with pytest.raises(ConfigError, match="missing or an empty directory"):
            repo.git.checkout_detached(base, target)
    assert (busy / "keep.txt").read_text() == "mine"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert repo.git.checkout_detached(base, empty).rev_parse("HEAD") == base


def test_fix_patch_round_trips_on_the_parent(
    fix_history: tuple[FixtureRepo, str, str], tmp_path: Path
) -> None:
    repo, base, fix = fix_history
    patch = repo.git.diff(base, fix)
    with repo.git.worktree(base, tmp_path / "wt") as wt:
        wt.apply_check(patch)
        assert wt.changed_paths() == ()
        wt.apply(patch)
        assert wt.changed_paths() == ("src/calc.py", "tests/test_calc.py", "tests/test_new.py")
        for path in repo.git.list_files(fix):
            assert (wt.repo_dir / path).read_bytes() == repo.git.read_file(fix, path)
        assert not wt.can_apply(patch)
        assert wt.can_apply(patch, reverse=True)
        wt.apply(patch, reverse=True)
        assert wt.changed_paths() == ()
    assert not (tmp_path / "wt").exists()
    worktrees = repo.git.run("worktree", "list", "--porcelain").stdout_text
    assert worktrees.count("worktree ") == 1


def test_apply_is_all_or_nothing(fix_history: tuple[FixtureRepo, str, str], tmp_path: Path) -> None:
    repo, base, fix = fix_history
    patch = repo.git.diff(base, fix)
    with repo.git.worktree(base, tmp_path / "wt") as wt:
        (wt.repo_dir / "src" / "calc.py").write_text("def add(a, b):\n    return b - a\n")
        with pytest.raises(PatchApplyError, match=r"src/calc\.py") as exc:
            wt.apply(patch)
        assert exc.value.argv[-1] == "-"
        assert not (wt.repo_dir / "tests" / "test_new.py").exists()
        assert "placeholder" in (wt.repo_dir / "tests" / "test_calc.py").read_text()


@pytest.mark.parametrize("patch", ["", "this is not a patch\n"])
def test_empty_or_garbage_patches_are_rejected(
    fix_history: tuple[FixtureRepo, str, str], tmp_path: Path, patch: str
) -> None:
    repo, base, _ = fix_history
    with repo.git.worktree(base, tmp_path / "wt") as wt, pytest.raises(PatchApplyError):
        wt.apply(patch)


def test_apply_refuses_paths_outside_the_work_tree(
    fix_history: tuple[FixtureRepo, str, str], tmp_path: Path
) -> None:
    repo, base, _ = fix_history
    evil = (
        "diff --git a/../evil.txt b/../evil.txt\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/../evil.txt\n@@ -0,0 +1 @@\n+owned\n"
    )
    with repo.git.worktree(base, tmp_path / "wt") as wt:
        with pytest.raises(PatchApplyError):
            wt.apply(evil)
        assert not (wt.repo_dir.parent / "evil.txt").exists()


def test_patches_are_refused_in_a_bare_repository(
    fix_history: tuple[FixtureRepo, str, str], tmp_path: Path
) -> None:
    repo, base, fix = fix_history
    bare = Git(tmp_path / "bare.git").clone_from(str(repo.path), bare=True)
    patch = repo.git.diff(base, fix)
    with pytest.raises(ConfigError, match="no work tree"):
        bare.apply(patch)
    with pytest.raises(ConfigError, match="no work tree"):
        bare.reset_work_tree()
    assert not (bare.repo_dir / "src").exists()
    with bare.worktree(base, tmp_path / "from-bare") as wt:
        wt.apply(patch)
        assert (wt.repo_dir / "src" / "calc.py").read_text() == NEW_SRC


def test_binary_and_non_utf8_patches_apply_byte_for_byte(
    repo_factory: RepoFactory, tmp_path: Path
) -> None:
    repo = repo_factory.create(files={"img.bin": b"\x89PNG\x00\x01", "latin1.txt": b"caf\xe9\n"})
    base = repo.head
    fix = repo.commit("fix: data", {"img.bin": b"\x89PNG\x00\x02\xff", "latin1.txt": b"caf\xe8\n"})
    patch = repo.git.diff(base, fix)
    with repo.git.worktree(base, tmp_path / "wt") as wt:
        wt.apply(patch)
        assert (wt.repo_dir / "img.bin").read_bytes() == b"\x89PNG\x00\x02\xff"
        assert (wt.repo_dir / "latin1.txt").read_bytes() == b"caf\xe8\n"


def test_reset_work_tree_discards_edits_untracked_and_ignored_files(
    fix_history: tuple[FixtureRepo, str, str], tmp_path: Path
) -> None:
    repo, base, fix = fix_history
    with repo.git.worktree(base, tmp_path / "wt") as wt:
        wt.apply(repo.git.diff(base, fix))
        (wt.repo_dir / "build" / "cache").mkdir(parents=True)
        (wt.repo_dir / "build" / "cache" / "calc.pyc").write_bytes(b"\x00")
        (wt.repo_dir / "notes.txt").write_text("scratch")
        assert "notes.txt" in wt.changed_paths()
        wt.reset_work_tree()
        assert wt.changed_paths() == ()
        assert not (wt.repo_dir / "build").exists()
        assert (wt.repo_dir / "src" / "calc.py").read_text() == OLD_SRC


def test_changed_paths_reports_both_sides_of_a_staged_rename(
    fix_history: tuple[FixtureRepo, str, str], tmp_path: Path
) -> None:
    repo, base, _ = fix_history
    with repo.git.worktree(base, tmp_path / "wt") as wt:
        wt.run("mv", "src/calc.py", "src/arith.py")
        assert wt.changed_paths() == ("src/arith.py", "src/calc.py")


def test_remove_worktree_tolerates_a_deleted_directory(
    fix_history: tuple[FixtureRepo, str, str], tmp_path: Path
) -> None:
    repo, base, _ = fix_history
    target = tmp_path / "gone"
    repo.git.checkout_detached(base, target)
    shutil.rmtree(target)
    repo.git.remove_worktree(target)
    listing = repo.git.run("worktree", "list", "--porcelain").stdout_text
    assert listing.count("worktree ") == 1


def test_worktree_context_cleans_up_when_the_body_fails(
    fix_history: tuple[FixtureRepo, str, str], tmp_path: Path
) -> None:
    repo, base, _ = fix_history
    target = tmp_path / "wt"
    with pytest.raises(RuntimeError, match="boom"), repo.git.worktree(base, target):
        raise RuntimeError("boom")
    assert not target.exists()


def test_worktree_wrapper_inherits_settings(
    fix_history: tuple[FixtureRepo, str, str], tmp_path: Path
) -> None:
    repo, base, _ = fix_history
    git = Git(repo.path, timeout=42.0, env={"GIT_AUTHOR_NAME": "inherited"})
    with git.worktree(base, tmp_path / "wt") as wt:
        assert wt.timeout == 42.0
        assert wt.runner is git.runner
        assert wt.run("var", "GIT_AUTHOR_IDENT", check=False).stdout_text.startswith("inherited")
