from pathlib import Path

import pytest

from reporewind.errors import PatchParseError, ResolveError
from reporewind.models import ChangeKind, FilePatch, join_patches
from reporewind.resolve.patches import parse_patch, parse_quoted
from reporewind.testing import FixtureRepo, RepoFactory

ZERO = "0" * 40
BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


@pytest.fixture
def kitchen_sink(repo_factory: RepoFactory) -> tuple[FixtureRepo, str, str]:
    """A base and a fix commit that exercise every kind of change git can emit."""
    repo = repo_factory.create(
        files={
            "src/pkg/core.py": "def f():\n    return 1\n",
            "src/pkg/gone.py": "x = 1\n",
            "src/pkg/old_name.py": "".join(f"line {i}\n" for i in range(20)),
            "tests/test_moved.py": "def test_a():\n    pass\n",
            "scripts/run.sh": "echo hi\n",
            "assets/logo.bin": b"\x89PNG\x00\x01\x02",
            "assets/old blob.bin": b"\x00\xff\x00",
            "link": "plain file\n",
            "caf\u00e9.py": "v = 1\n",
        }
    )
    base = repo.head
    repo.write(
        {
            "src/pkg/core.py": "def f():\n    return 2\n",
            "src/pkg/gone.py": None,
            "src/pkg/new_file.py": "y = 2\n",
            "src/pkg/empty.py": "",
            "assets/logo.bin": b"\x89PNG\x00\x01\x03\x04",
            "assets/new blob.bin": b"\x00\x02\xfe",
            "assets/old blob.bin": None,
            "caf\u00e9.py": "v = 2\n",
            "link": None,
        }
    )
    old = (repo.path / "src/pkg/old_name.py").read_text()
    repo.write({"src/pkg/old_name.py": None, "src/pkg/new_name.py": old + "line 20\n"})
    repo.rename("tests/test_moved.py", "tests/unit/test_moved.py")
    (repo.path / "scripts/run.sh").chmod(0o755)
    (repo.path / "link").symlink_to("src/pkg/core.py")
    fix = repo.commit("fix: everything at once")
    return repo, base, fix


def test_real_diff_splits_into_one_patch_per_path(
    kitchen_sink: tuple[FixtureRepo, str, str],
) -> None:
    repo, base, fix = kitchen_sink
    diff = repo.git.diff(base, fix)
    patches = parse_patch(diff)
    by_path = {p.path: p for p in patches}

    assert by_path["src/pkg/core.py"].change is ChangeKind.MODIFIED
    assert by_path["src/pkg/gone.py"].change is ChangeKind.DELETED
    assert by_path["src/pkg/new_file.py"].change is ChangeKind.ADDED
    assert by_path["src/pkg/empty.py"].change is ChangeKind.ADDED
    renamed = by_path["src/pkg/new_name.py"]
    assert (renamed.change, renamed.old_path) == (ChangeKind.RENAMED, "src/pkg/old_name.py")
    moved = by_path["tests/unit/test_moved.py"]
    assert (moved.change, moved.old_path) == (ChangeKind.RENAMED, "tests/test_moved.py")
    assert by_path["scripts/run.sh"].change is ChangeKind.MODIFIED  # mode-only
    assert by_path["assets/logo.bin"].binary
    assert by_path["assets/new blob.bin"].change is ChangeKind.ADDED
    assert by_path["assets/new blob.bin"].binary
    assert by_path["assets/old blob.bin"].change is ChangeKind.DELETED
    assert by_path["caf\u00e9.py"].change is ChangeKind.MODIFIED
    # A file replaced by a symlink is one "modified" patch, not a delete + add.
    link = by_path["link"]
    assert link.change is ChangeKind.MODIFIED
    assert link.diff.count("diff --git ") == 2
    assert len(by_path) == len(patches) == 12


def test_split_is_lossless_and_each_patch_applies_on_its_own(
    kitchen_sink: tuple[FixtureRepo, str, str], tmp_path: Path
) -> None:
    repo, base, fix = kitchen_sink
    diff = repo.git.diff(base, fix)
    patches = parse_patch(diff)
    assert join_patches(patches) == diff
    with repo.git.worktree(base, tmp_path / "wt") as wt:
        for patch in patches:
            wt.apply_check(patch.diff)
        wt.apply(join_patches(patches))
        assert set(wt.changed_paths()) >= {p.path for p in patches}


def test_non_utf8_content_survives_the_split(repo_factory: RepoFactory) -> None:
    repo = repo_factory.create(files={"legacy.py": b"x = 1  # caf\xe9\n"})
    base = repo.head
    fix = repo.commit("fix: latin-1 file", {"legacy.py": b"x = 2  # caf\xe9\n"})
    diff = repo.git.diff(base, fix)
    (patch,) = parse_patch(diff)
    assert patch.path == "legacy.py"
    assert "\udce9" in patch.diff  # the raw byte, kept via surrogateescape
    assert patch.diff.encode("utf-8", "surrogateescape").count(b"caf\xe9") == 2


def test_empty_diff_has_no_patches() -> None:
    assert parse_patch("") == ()


def _section(header: str, *extra: str) -> str:
    return "\n".join([header, *extra]) + "\n"


def test_quoted_paths_are_decoded() -> None:
    header = 'diff --git "a/caf\\303\\251 \\"q\\".py" "b/caf\\303\\251 \\"q\\".py"'
    text = _section(
        header,
        f"index {ZERO}..{BLOB} 100644",
        '--- "a/caf\\303\\251 \\"q\\".py"',
        '+++ "b/caf\\303\\251 \\"q\\".py"',
        "@@ -1 +1 @@",
        "-a",
        "+b",
    )
    (patch,) = parse_patch(text)
    assert patch.path == 'caf\u00e9 "q".py'


def test_binary_patch_path_comes_from_the_same_name_header() -> None:
    text = _section(
        "diff --git a/dir with space/x.bin b/dir with space/x.bin",
        "new file mode 100644",
        f"index {ZERO}..{BLOB}",
        "GIT binary patch",
        "literal 2",
        "JcmZQz1ONa700IC2",
        "",
        "literal 0",
        "HcmV?d00001",
        "",
    )
    (patch,) = parse_patch(text)
    assert (patch.path, patch.change, patch.binary) == (
        "dir with space/x.bin",
        ChangeKind.ADDED,
        True,
    )


def test_quoted_binary_header_and_empty_file_deletion() -> None:
    text = _section(
        'diff --git "a/\\303\\251.bin" "b/\\303\\251.bin"',
        "deleted file mode 100644",
        f"index {BLOB}..{ZERO}",
    )
    (patch,) = parse_patch(text)
    assert (patch.path, patch.change) == ("\u00e9.bin", ChangeKind.DELETED)


def test_copy_headers_are_understood() -> None:
    text = _section(
        "diff --git a/src/a.py b/src/b.py",
        "similarity index 100%",
        "copy from src/a.py",
        "copy to src/b.py",
    ) + _section(
        "diff --git a/src/a.py b/src/a.py",
        f"index {BLOB}..{ZERO} 100644",
        "--- a/src/a.py",
        "+++ b/src/a.py",
        "@@ -1 +1 @@",
        "-a",
        "+b",
    )
    copy, edit = parse_patch(text)
    assert (copy.change, copy.old_path, copy.path) == (ChangeKind.COPIED, "src/a.py", "src/b.py")
    assert edit.path == "src/a.py"


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("junk\ndiff --git a/x b/x\n", "must start with"),
        ("diff --git a/x b/x\nold mode 100644\nnew mode 100755", "truncated"),
        (
            "diff --git a/x.bin b/x.bin\nindex 1..2 100644\n"
            "Binary files a/x.bin and b/x.bin differ\n",
            "without patch data",
        ),
        ("diff --git a/x b/x\nwhat is this\n", "unexpected line"),
        ("diff --git a/x b/y\nold mode 100644\nnew mode 100755\n", "cannot determine the path"),
        ("diff --git a/ b/\nold mode 100644\nnew mode 100755\n", "cannot determine the path"),
        ("diff --git a/x b/y\nrename from x\n", "without both paths"),
        (
            "diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ /dev/null\n@@ -1 +1 @@\n-a\n+b\n",
            "/dev/null side",
        ),
        ("diff --git a/x b/x\nindex 1..2 100644\n--- x\n+++ b/x\n", "path prefix"),
        ("diff --git a/../x b/../x\nold mode 100644\nnew mode 100755\n", "'..' segments"),
        (
            "diff --git a/.git/config b/.git/config\nold mode 100644\nnew mode 100755\n",
            "inside a .git directory",
        ),
        ('diff --git "x" "b/x"\nold mode 100644\nnew mode 100755\n', "prefix"),
        ('diff --git "a/x""b/x"\nold mode 100644\nnew mode 100755\n', "malformed diff header"),
        ('diff --git "a/\\377" "b/\\377"\nold mode 100644\nnew mode 100755\n', "not valid UTF-8"),
        ('diff --git "a/\\q" "b/\\q"\nold mode 100644\nnew mode 100755\n', "invalid escape"),
        ('diff --git "a/x\nold mode 100644\n', "unterminated"),
        ('diff --git a/x b/y\nrename from "x"y\nrename to y\n', "trailing text"),
        (
            "diff --git a/x b/x\nold mode 100644\nnew mode 100755\n"
            "diff --git a/x b/x\nold mode 100755\nnew mode 100644\n",
            "more than one diff section",
        ),
        (
            "diff --git a/x b/x\nnew file mode 100644\nindex 1..2\n--- /dev/null\n+++ b/x\n"
            "@@ -0,0 +1 @@\n+a\n"
            "diff --git a/x b/x\nnew file mode 100644\nindex 1..2\n--- /dev/null\n+++ b/x\n"
            "@@ -0,0 +1 @@\n+a\n",
            "more than one diff section",
        ),
    ],
)
def test_malformed_or_unsafe_patches_are_rejected(text: str, match: str) -> None:
    with pytest.raises(PatchParseError, match=match):
        parse_patch(text)


def test_patch_parse_error_is_a_resolve_error() -> None:
    with pytest.raises(ResolveError):
        parse_patch("nope\n")


def test_parse_quoted_reports_the_end_index() -> None:
    assert parse_quoted('x "a\\tb\\\\c" y', 2) == ("a\tb\\c", 11)
    with pytest.raises(PatchParseError, match="expected a quoted path"):
        parse_quoted("abc")


def test_parsed_patches_are_file_patch_models() -> None:
    text = _section("diff --git a/x b/x", "old mode 100644", "new mode 100755")
    (patch,) = parse_patch(text)
    assert isinstance(patch, FilePatch)
    assert patch.diff == text
