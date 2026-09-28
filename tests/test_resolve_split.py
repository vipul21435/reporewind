import pytest
from pydantic import ValidationError

from reporewind.models import ChangeKind, FilePatch
from reporewind.resolve.split import DiffSplit, SplitRules, glob_match, split_patches


def _patch(path: str, old_path: str | None = None) -> FilePatch:
    change = ChangeKind.RENAMED if old_path else ChangeKind.MODIFIED
    return FilePatch(
        path=path,
        old_path=old_path,
        change=change,
        diff=f"diff --git a/{old_path or path} b/{path}\n",
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_core.py", True),
        ("tests/data/input.json", True),
        ("src/pkg/tests/helpers.py", True),
        ("test/regression.py", True),
        ("pkg/test_util.py", True),
        ("pkg/util_test.py", True),
        ("app/tests.py", True),
        ("conftest.py", True),
        ("src/conftest.py", True),
        ("src/pkg/core.py", False),
        ("src/pkg/testing/helpers.py", False),
        ("src/pkg/attest.py", False),
        ("contest.py", False),
        ("Tests/test.txt", False),
        ("docs/testing.md", False),
        ("tests", False),
    ],
)
def test_default_rules(path: str, expected: bool) -> None:
    assert SplitRules().is_test_path(path) is expected


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/x/a.py", False),
        ("src/**/*.py", "src/a.py", True),
        ("src/**/*.py", "src/x/y/a.py", True),
        ("**/conftest.py", "conftest.py", True),
        ("**/conftest.py", "a/b/conftest.py", True),
        ("docs/", "docs/a/b.md", True),
        ("docs/", "docsx/a.md", False),
        ("fixtures/**", "fixtures/a/b", True),
        ("a?.py", "ab.py", True),
        ("a?.py", "a/.py", False),
        ("a+b.py", "a+b.py", True),
        ("a+b.py", "aab.py", False),
        ("**", "anything/at/all", True),
    ],
)
def test_glob_match(pattern: str, path: str, expected: bool) -> None:
    assert glob_match(pattern, path) is expected


def test_include_adds_tests_and_exclude_wins() -> None:
    rules = SplitRules(
        include=("examples/**/check_*.py",),
        exclude=("tests/fixtures/generated/", "examples/skip/**"),
    )
    assert rules.is_test_path("examples/a/check_x.py")
    assert not rules.is_test_path("examples/skip/check_x.py")
    assert not rules.is_test_path("tests/fixtures/generated/big.py")
    assert rules.is_test_path("tests/fixtures/handmade.py")


def test_rules_are_fully_configurable() -> None:
    rules = SplitRules(test_dirs=("spec",), test_files=("*_spec.py",))
    assert rules.is_test_path("spec/a.py")
    assert rules.is_test_path("pkg/a_spec.py")
    assert not rules.is_test_path("tests/test_a.py")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"test_dirs": ("a/b",)},
        {"test_dirs": ("",)},
        {"test_dirs": ("..",)},
        {"test_files": ("tests/test_*.py",)},
        {"test_files": ("",)},
        {"include": ("/abs/**",)},
        {"exclude": ("",)},
        {"unknown": ()},
    ],
)
def test_invalid_rules_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SplitRules.model_validate(kwargs)


def test_a_rename_counts_as_a_test_change_only_if_both_sides_are_tests() -> None:
    rules = SplitRules()
    assert rules.is_test_patch(_patch("tests/unit/test_a.py", "tests/test_a.py"))
    assert not rules.is_test_patch(_patch("src/pkg/helpers.py", "tests/helpers.py"))
    assert not rules.is_test_patch(_patch("tests/helpers.py", "src/pkg/helpers.py"))


def test_split_patches_partitions_in_diff_order() -> None:
    patches = [
        _patch("src/a.py"),
        _patch("tests/test_a.py"),
        _patch("README.md"),
        _patch("tests/conftest.py"),
    ]
    split = split_patches(patches)
    assert isinstance(split, DiffSplit)
    assert [p.path for p in split.source] == ["src/a.py", "README.md"]
    assert [p.path for p in split.tests] == ["tests/test_a.py", "tests/conftest.py"]
    custom = split_patches(patches, SplitRules(exclude=("tests/conftest.py",)))
    assert [p.path for p in custom.source] == ["src/a.py", "README.md", "tests/conftest.py"]
