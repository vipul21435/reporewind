import inspect

import pytest

from reporewind import errors
from reporewind.errors import (
    CommandError,
    CommandTimeoutError,
    ConfigError,
    GitError,
    InvalidRepoRefError,
    InvalidRevisionError,
    PatchApplyError,
    RepoRewindError,
    RevisionNotFoundError,
    ToolNotFoundError,
    stderr_tail,
)

ALL_ERRORS = [
    obj
    for _, obj in inspect.getmembers(errors, inspect.isclass)
    if issubclass(obj, Exception) and obj.__module__ == errors.__name__
]


def test_every_error_derives_from_the_base_class() -> None:
    assert len(ALL_ERRORS) >= 15
    for cls in ALL_ERRORS:
        assert issubclass(cls, RepoRewindError), cls


def test_direct_subclasses_of_the_base_have_distinct_exit_codes() -> None:
    top_level = [cls for cls in ALL_ERRORS if RepoRewindError in cls.__bases__]
    codes = [cls.exit_code for cls in top_level]
    assert len(codes) == len(set(codes)), codes
    assert all(code > 1 for code in codes)
    assert RepoRewindError.exit_code == 1


def test_subclasses_inherit_their_category_exit_code() -> None:
    assert InvalidRepoRefError.exit_code == ConfigError.exit_code == 2
    assert PatchApplyError.exit_code == GitError.exit_code == CommandError.exit_code
    assert CommandTimeoutError.exit_code == CommandError.exit_code


def test_config_errors_are_value_errors_for_pydantic_validators() -> None:
    err = InvalidRepoRefError("x y", "whitespace")
    assert isinstance(err, ValueError)
    assert err.value == "x y"
    assert err.reason == "whitespace"
    assert str(err) == "invalid repository reference 'x y': whitespace"
    assert isinstance(InvalidRevisionError("-x", "leading dash"), ValueError)


def test_tool_not_found_names_the_tool() -> None:
    err = ToolNotFoundError("docker")
    assert err.tool == "docker"
    assert "'docker'" in str(err)


def test_command_error_keeps_argv_status_and_output() -> None:
    err = CommandError(
        ["git", "fetch", "origin", "my branch"], 128, "out", "noise\n\nfatal: nope\n"
    )
    assert err.argv == ("git", "fetch", "origin", "my branch")
    assert err.returncode == 128
    assert err.stdout == "out"
    assert str(err) == (
        "command exited with status 128: git fetch origin 'my branch'\nnoise\nfatal: nope"
    )


def test_command_error_without_stderr_is_a_single_line() -> None:
    assert str(CommandError(["uv", "lock"], 2)) == "command exited with status 2: uv lock"


def test_timeout_is_a_command_error_with_sentinel_status() -> None:
    err = CommandTimeoutError(["docker", "build", "."], 1.5, stderr="step 3/9")
    assert isinstance(err, CommandError)
    assert err.returncode == -1
    assert err.timeout == 1.5
    assert str(err).startswith("command timed out after 1.5s: docker build .")


def test_revision_not_found_is_a_git_error() -> None:
    err = RevisionNotFoundError(["git", "rev-parse", "nope"], "nope")
    assert isinstance(err, GitError)
    assert err.revision == "nope"
    assert "'nope' does not name a commit" in str(err)


@pytest.mark.parametrize(
    ("stderr", "lines", "expected"),
    [
        ("", 3, ""),
        ("a\n\n  \nb\n", 3, "a\nb"),
        ("1\n2\n3\n4\n5\n", 2, "4\n5"),
    ],
)
def test_stderr_tail_keeps_last_non_blank_lines(stderr: str, lines: int, expected: str) -> None:
    assert stderr_tail(stderr, lines) == expected


def test_command_error_falls_back_to_stdout_when_stderr_is_empty() -> None:
    err = CommandError(["git", "commit"], 1, "On branch main\nnothing to commit\n", "")
    assert str(err).endswith("\nOn branch main\nnothing to commit")
