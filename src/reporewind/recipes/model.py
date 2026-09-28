"""The build recipe: how to install and test one repository at one commit.

A :class:`Recipe` holds only the values that change the built environment,
normalized so that equivalent recipes compare (and hash) equal: the Python
constraint is a canonical PEP 440 specifier set, extras and system packages
are sorted sets, requirement strings are PEP 508 normalized, and the test
command falls back to the framework's default when none is given.

Recipes are assembled from two layers: values detected from the repository
and human overrides. :func:`merge_patch` combines them with JSON Merge Patch
semantics (RFC 7396): mappings merge key by key, lists and scalars replace,
and ``null`` removes a detected value so the field falls back to its default.

:func:`recipe_hash` is the sha256 of the canonical JSON of a recipe, so the
hash is independent of YAML formatting, key order and which layer a value
came from; later stages use it as part of the environment's content hash.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Self

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reporewind.models import RepoPath

RECIPE_SCHEMA_VERSION = 1

_EXTRA_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Debian package names (policy 5.6.1), optionally with an =version pin.
_APT_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+(?:=[A-Za-z0-9.+:~-]+)?$")
_LINE_BREAK_OR_NUL = re.compile(r"[\r\n\x00]")


class InstallMode(StrEnum):
    """How the repository itself is installed into the environment."""

    EDITABLE = "editable"
    """``pip install -e .[extras]``: tests import the checked-out sources."""
    PACKAGE = "package"
    """``pip install .[extras]``: for backends without editable-install support."""
    REQUIREMENTS = "requirements"
    """No packaging metadata: install the requirements files only."""
    NONE = "none"
    """Nothing to install beyond the test dependencies."""


class Framework(StrEnum):
    """The test runner the verification stage drives."""

    PYTEST = "pytest"
    UNITTEST = "unittest"


DEFAULT_TEST_COMMANDS: Mapping[Framework, tuple[str, ...]] = {
    Framework.PYTEST: ("python", "-m", "pytest", "-rA"),
    Framework.UNITTEST: ("python", "-m", "unittest", "discover", "-v"),
}


def _unique(values: tuple[str, ...], what: str) -> tuple[str, ...]:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {what} {value!r}")
        seen.add(value)
    return values


def _single_line(value: str, what: str) -> str:
    if not value.strip():
        raise ValueError(f"{what} must not be blank")
    if _LINE_BREAK_OR_NUL.search(value):
        raise ValueError(f"{what} {value!r} must be a single line without NUL")
    return value


def normalize_specifier(value: str) -> str:
    """Canonical form of a PEP 440 specifier set (sorted, no spaces)."""
    try:
        return str(SpecifierSet(value))
    except InvalidSpecifier as exc:
        raise ValueError(f"invalid Python version specifier {value!r}: {exc}") from None


def normalize_requirement(value: str) -> str:
    """Canonical form of a PEP 508 requirement string."""
    try:
        return str(Requirement(value))
    except InvalidRequirement as exc:
        raise ValueError(f"invalid requirement {value!r}: {exc}") from None


def normalize_extra(value: str) -> str:
    """PEP 685 normalized extra name."""
    if not _EXTRA_RE.fullmatch(value):
        raise ValueError(f"invalid extra name {value!r}")
    return canonicalize_name(value)


class Recipe(BaseModel):
    """Everything needed to install and test a repository at one commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    python: str | None = Field(
        default=None, description="PEP 440 constraint on the Python version, e.g. '>=3.8'."
    )
    install: InstallMode = InstallMode.EDITABLE
    extras: tuple[str, ...] = Field(
        default=(), description="Extras installed with the project (normalized, sorted)."
    )
    requirements_files: tuple[RepoPath, ...] = Field(
        default=(), description="Requirements files installed in this order."
    )
    test_dependencies: tuple[str, ...] = Field(
        default=(), description="PEP 508 requirements needed to run the tests."
    )
    system_packages: tuple[str, ...] = Field(
        default=(), description="Debian packages installed in the image (sorted)."
    )
    pre_install: tuple[str, ...] = Field(
        default=(), description="Shell steps run in the image before the install, in order."
    )
    test_framework: Framework = Framework.PYTEST
    test_command: tuple[str, ...] = Field(
        default=(), description="Test command argv; empty means the framework's default."
    )
    env: dict[str, str] = Field(default_factory=dict, description="Environment for the tests.")

    @model_validator(mode="before")
    @classmethod
    def _default_test_command(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and not data.get("test_command"):
            framework = data.get("test_framework", Framework.PYTEST)
            try:
                default = DEFAULT_TEST_COMMANDS[Framework(framework)]
            except (ValueError, TypeError):
                return data  # the field validator reports the bad framework
            return {**data, "test_command": default}
        return data

    @field_validator("python")
    @classmethod
    def _check_python(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return normalize_specifier(value)

    @field_validator("extras")
    @classmethod
    def _check_extras(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({normalize_extra(value) for value in values}))

    @field_validator("requirements_files")
    @classmethod
    def _check_requirements_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(values, "requirements file")

    @field_validator("test_dependencies")
    @classmethod
    def _check_test_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(tuple(normalize_requirement(v) for v in values), "test dependency")

    @field_validator("system_packages")
    @classmethod
    def _check_system_packages(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if not _APT_RE.fullmatch(value):
                raise ValueError(f"invalid Debian package name {value!r}")
        return tuple(sorted(set(values)))

    @field_validator("pre_install")
    @classmethod
    def _check_pre_install(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_single_line(value, "pre-install step") for value in values)

    @field_validator("test_command")
    @classmethod
    def _check_test_command(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if not value or "\x00" in value:
                raise ValueError("test command arguments must be non-empty and contain no NUL")
        return values

    @field_validator("env")
    @classmethod
    def _check_env(cls, values: dict[str, str]) -> dict[str, str]:
        for key, value in values.items():
            if not _ENV_KEY_RE.fullmatch(key):
                raise ValueError(f"invalid environment variable name {key!r}")
            if "\x00" in value:
                raise ValueError(f"environment variable {key} contains NUL")
        return dict(sorted(values.items()))

    @model_validator(mode="after")
    def _check_install(self) -> Self:
        if self.install is InstallMode.REQUIREMENTS and not self.requirements_files:
            raise ValueError("install mode 'requirements' needs at least one requirements file")
        if self.extras and self.install not in {InstallMode.EDITABLE, InstallMode.PACKAGE}:
            raise ValueError(f"extras need an installed project, not install mode {self.install}")
        return self


def merge_patch(target: Any, patch: Any) -> Any:
    """Apply ``patch`` to ``target`` with JSON Merge Patch semantics (RFC 7396).

    Mappings merge recursively, ``None`` deletes a key and any other value
    (lists included) replaces the target outright. Neither input is mutated.
    """
    if not isinstance(patch, Mapping):
        return copy.deepcopy(patch)
    result = dict(target) if isinstance(target, Mapping) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def canonical_json(recipe: Recipe) -> str:
    """The hashed form of a recipe: sorted keys, no whitespace, ASCII only."""
    payload = {"schema_version": RECIPE_SCHEMA_VERSION, "recipe": recipe.model_dump(mode="json")}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def recipe_hash(recipe: Recipe) -> str:
    """``sha256:<hex>`` of :func:`canonical_json`; stable across machines and runs."""
    digest = hashlib.sha256(canonical_json(recipe).encode("ascii")).hexdigest()
    return f"sha256:{digest}"


__all__ = [
    "DEFAULT_TEST_COMMANDS",
    "RECIPE_SCHEMA_VERSION",
    "Framework",
    "InstallMode",
    "Recipe",
    "canonical_json",
    "merge_patch",
    "normalize_extra",
    "normalize_requirement",
    "normalize_specifier",
    "recipe_hash",
]
