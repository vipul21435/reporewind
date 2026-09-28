"""Choose the CPython version a historical commit was built with.

The rule: the newest CPython minor version that had been released on the
commit date, that satisfies the project's ``requires-python`` constraint and,
when the project lists ``Programming Language :: Python :: X.Y`` trove
classifiers, that is one of them. A project dated 2021-06 with ``>=3.6``
gets 3.9 (3.10 did not exist yet), not today's newest Python, which may
reject syntax or C APIs the code relied on.

Release dates are the ``X.Y.0`` final releases from python.org. Only minor
versions from :data:`OLDEST_SUPPORTED` on are chosen: older ones have no
maintained ``python:X.Y-slim`` image and no support in the resolver.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import BaseModel, ConfigDict

from reporewind.errors import ConfigError, PinError

CPYTHON_RELEASES: Mapping[str, date] = {
    "2.7": date(2010, 7, 3),
    "3.0": date(2008, 12, 3),
    "3.1": date(2009, 6, 27),
    "3.2": date(2011, 2, 20),
    "3.3": date(2012, 9, 29),
    "3.4": date(2014, 3, 16),
    "3.5": date(2015, 9, 13),
    "3.6": date(2016, 12, 23),
    "3.7": date(2018, 6, 27),
    "3.8": date(2019, 10, 14),
    "3.9": date(2020, 10, 5),
    "3.10": date(2021, 10, 4),
    "3.11": date(2022, 10, 24),
    "3.12": date(2023, 10, 2),
    "3.13": date(2024, 10, 7),
    "3.14": date(2025, 10, 7),
}
"""Release date of each CPython ``X.Y.0`` final release."""

OLDEST_SUPPORTED = "3.7"
"""Oldest minor version RepoRewind builds (slim images and resolver support)."""

_MINOR_RE = re.compile(r"^(\d+)\.(\d+)$")
_CLASSIFIER_RE = re.compile(r"^Programming Language :: Python :: (\d+\.\d+)$")
# A minor version satisfies a constraint if its first or a late patch release
# does: ">=3.8.1" admits 3.8 and "<3.10.2" admits 3.10.
_PATCH_PROBES = ("0", "99")


def version_key(minor: str) -> tuple[int, int]:
    """Sort key for an ``X.Y`` string."""
    match = _MINOR_RE.fullmatch(minor)
    if match is None:
        raise ConfigError(f"invalid Python version {minor!r}: expected MAJOR.MINOR such as 3.11")
    return int(match[1]), int(match[2])


def supported_versions() -> tuple[str, ...]:
    """Every minor version RepoRewind can build, oldest first."""
    floor = version_key(OLDEST_SUPPORTED)
    return tuple(sorted((v for v in CPYTHON_RELEASES if version_key(v) >= floor), key=version_key))


def released_by(day: date) -> tuple[str, ...]:
    """Supported minor versions whose ``X.Y.0`` was out on ``day``, oldest first."""
    return tuple(v for v in supported_versions() if CPYTHON_RELEASES[v] <= day)


def satisfies(minor: str, constraint: SpecifierSet) -> bool:
    """True if some release of ``minor`` satisfies ``constraint``."""
    return any(constraint.contains(f"{minor}.{patch}") for patch in _PATCH_PROBES)


def classifier_versions(classifiers: Iterable[str]) -> tuple[str, ...]:
    """``X.Y`` versions named by ``Programming Language :: Python :: X.Y``, oldest first."""
    found = {m[1] for c in classifiers if (m := _CLASSIFIER_RE.fullmatch(c.strip()))}
    return tuple(sorted(found, key=version_key))


class PythonChoice(BaseModel):
    """The interpreter chosen for one commit, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    reason: str
    constraint: str | None = None
    classifiers: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _parse_constraint(constraint: str | None) -> SpecifierSet:
    try:
        return SpecifierSet(constraint or "")
    except InvalidSpecifier as exc:
        raise PinError(f"invalid Python constraint {constraint!r}: {exc}") from None


def choose_python(
    commit_date: datetime,
    *,
    constraint: str | None = None,
    classifiers: Sequence[str] = (),
    override: str | None = None,
) -> PythonChoice:
    """Pick the CPython minor version for a commit made at ``commit_date``.

    ``override`` (``X.Y``) wins over inference, but a version that is not
    supported, or that contradicts the constraint, is still reported in the
    notes. Raises :class:`PinError` when no supported release satisfies the
    constraint.
    """
    spec = _parse_constraint(constraint)
    day = commit_date.astimezone(UTC).date()
    listed = classifier_versions(classifiers)
    released = released_by(day)
    candidates = tuple(v for v in released if satisfies(v, spec))
    notes: list[str] = []

    def choice(version: str, reason: str) -> PythonChoice:
        return PythonChoice(
            version=version,
            reason=reason,
            constraint=str(spec) or None,
            classifiers=listed,
            candidates=candidates,
            notes=tuple(notes),
        )

    if override is not None:
        version_key(override)
        if override not in supported_versions():
            raise ConfigError(
                f"Python {override} is not supported; choose one of"
                f" {', '.join(supported_versions())}"
            )
        if not satisfies(override, spec):
            notes.append(f"Python {override} does not satisfy the constraint {spec}")
        if CPYTHON_RELEASES[override] > day:
            notes.append(f"Python {override} was released after the commit date {day}")
        return choice(override, "given explicitly")

    constraint_text = f"satisfies {spec}" if str(spec) else "has no constraint"
    if not candidates:
        later = [v for v in supported_versions() if satisfies(v, spec)]
        if not later:
            raise PinError(f"no supported Python version satisfies {spec}")
        if released:
            raise PinError(
                f"no Python release out by {day} satisfies {spec}; pass an explicit version"
            )
        # The commit predates the oldest supported release: use that one.
        notes.append(f"the commit predates Python {OLDEST_SUPPORTED}, the oldest supported")
        return choice(later[0], f"oldest supported version that {constraint_text}")

    in_classifiers = [v for v in candidates if v in listed]
    if in_classifiers:
        return choice(
            in_classifiers[-1],
            f"newest classifier version released by {day} that {constraint_text}",
        )
    if listed:
        notes.append(
            f"classifiers list {', '.join(listed)} but none was released by {day} and"
            f" {constraint_text}; they were ignored"
        )
    return choice(candidates[-1], f"newest release out by {day} that {constraint_text}")


__all__ = [
    "CPYTHON_RELEASES",
    "OLDEST_SUPPORTED",
    "PythonChoice",
    "choose_python",
    "classifier_versions",
    "released_by",
    "satisfies",
    "supported_versions",
    "version_key",
]
