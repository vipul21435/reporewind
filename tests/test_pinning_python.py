from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from packaging.specifiers import SpecifierSet

from reporewind.errors import ConfigError, PinError
from reporewind.pinning.python import (
    CPYTHON_RELEASES,
    OLDEST_SUPPORTED,
    PythonChoice,
    choose_python,
    classifier_versions,
    released_by,
    satisfies,
    supported_versions,
    version_key,
)


def at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, tzinfo=UTC)


def test_release_table_is_ordered_by_version_and_date() -> None:
    versions = supported_versions()
    assert versions[0] == OLDEST_SUPPORTED
    assert versions == tuple(sorted(versions, key=version_key))
    dates = [CPYTHON_RELEASES[v] for v in versions]
    assert dates == sorted(dates)
    assert CPYTHON_RELEASES["3.12"] == date(2023, 10, 2)


def test_released_by_includes_the_release_day() -> None:
    assert released_by(date(2021, 10, 3))[-1] == "3.9"
    assert released_by(date(2021, 10, 4))[-1] == "3.10"
    assert released_by(date(2017, 1, 1)) == ()


@pytest.mark.parametrize(
    ("minor", "spec", "expected"),
    [
        ("3.8", ">=3.8.1", True),
        ("3.10", "<3.10.2", True),
        ("3.9", ">=3.10", False),
        ("3.11", "==3.11.*", True),
        ("3.12", "!=3.12.*", False),
        ("3.7", "", True),
    ],
)
def test_satisfies_checks_any_patch_release(minor: str, spec: str, expected: bool) -> None:
    assert satisfies(minor, SpecifierSet(spec)) is expected


def test_classifier_versions_ignore_other_classifiers() -> None:
    classifiers = [
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.10",
        " Programming Language :: Python :: 3.8 ",
        "Programming Language :: Python :: Implementation :: CPython",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.9",
    ]
    assert classifier_versions(classifiers) == ("3.8", "3.9", "3.10")


def test_newest_release_at_the_commit_date_wins() -> None:
    choice = choose_python(at(2021, 6, 1), constraint=">=3.6")
    assert choice.version == "3.9"
    assert choice.candidates == ("3.7", "3.8", "3.9")
    assert choice.constraint == ">=3.6"
    assert choice.reason == "newest release out by 2021-06-01 that satisfies >=3.6"
    assert choice.notes == ()


def test_no_constraint_takes_the_newest_release() -> None:
    choice = choose_python(at(2024, 1, 1))
    assert choice.version == "3.12"
    assert choice.constraint is None
    assert "has no constraint" in choice.reason


def test_upper_bound_is_respected() -> None:
    assert choose_python(at(2024, 1, 1), constraint=">=3.7,<3.11").version == "3.10"


def test_commit_date_is_compared_in_utc() -> None:
    # 2021-10-04 01:00 in UTC+05:30 is still 2021-10-03 in UTC: 3.10 was not out yet.
    ist = timezone(timedelta(hours=5, minutes=30))
    choice = choose_python(datetime(2021, 10, 4, 1, 0, tzinfo=ist))
    assert choice.version == "3.9"


def test_classifiers_narrow_the_choice() -> None:
    classifiers = [
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.12",
    ]
    choice = choose_python(at(2023, 1, 1), constraint=">=3.8", classifiers=classifiers)
    assert choice.version == "3.9"
    assert choice.classifiers == ("3.8", "3.9", "3.12")
    assert choice.reason.startswith("newest classifier version released by 2023-01-01")


def test_classifiers_outside_the_candidates_are_ignored_with_a_note() -> None:
    choice = choose_python(
        at(2020, 1, 1),
        constraint=">=3.7",
        classifiers=["Programming Language :: Python :: 3.6"],
    )
    assert choice.version == "3.8"
    assert choice.notes == (
        "classifiers list 3.6 but none was released by 2020-01-01 and satisfies >=3.7;"
        " they were ignored",
    )


def test_commit_before_the_oldest_supported_release_falls_back() -> None:
    choice = choose_python(at(2017, 5, 1), constraint=">=3.5")
    assert choice.version == OLDEST_SUPPORTED
    assert choice.candidates == ()
    assert choice.notes == ("the commit predates Python 3.7, the oldest supported",)


def test_constraint_that_nothing_released_satisfies_is_an_error() -> None:
    with pytest.raises(PinError, match=r"no Python release out by 2021-01-01 satisfies >=3\.10"):
        choose_python(at(2021, 1, 1), constraint=">=3.10")


def test_constraint_that_no_supported_version_satisfies_is_an_error() -> None:
    with pytest.raises(PinError, match=r"no supported Python version satisfies <3\.6"):
        choose_python(at(2024, 1, 1), constraint="<3.6")


def test_invalid_constraint_is_a_pin_error() -> None:
    with pytest.raises(PinError, match="invalid Python constraint"):
        choose_python(at(2024, 1, 1), constraint=">=three")


def test_override_wins_and_reports_conflicts() -> None:
    choice = choose_python(at(2021, 1, 1), constraint="<3.9", override="3.11")
    assert choice == PythonChoice(
        version="3.11",
        reason="given explicitly",
        constraint="<3.9",
        classifiers=(),
        candidates=("3.7", "3.8"),
        notes=(
            "Python 3.11 does not satisfy the constraint <3.9",
            "Python 3.11 was released after the commit date 2021-01-01",
        ),
    )


@pytest.mark.parametrize("override", ["3", "3.x", "three"])
def test_malformed_override_is_a_config_error(override: str) -> None:
    with pytest.raises(ConfigError, match=r"expected MAJOR\.MINOR"):
        choose_python(at(2024, 1, 1), override=override)


def test_unsupported_override_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match=r"Python 3\.6 is not supported"):
        choose_python(at(2024, 1, 1), override="3.6")
