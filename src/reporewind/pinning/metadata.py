"""Static project metadata that pinning needs: classifiers and runtime dependencies.

The recipe records how to install and test a project; pinning also needs
what the project itself depends on (to lock it) and which Python versions
it advertised (to choose an interpreter). Both are read from the same files
as recipe detection, at the same commit, and just as statically:
``pyproject.toml`` (PEP 621 ``[project]`` and ``[tool.poetry]``),
``setup.cfg`` and ``setup.py`` (parsed with :mod:`ast`, never executed).

Values that only exist at build time (``dynamic`` fields, ``file:``
directives, computed ``setup()`` arguments) are reported as notes instead
of guessed.
"""

from __future__ import annotations

import ast
import configparser
import tomllib
from collections.abc import Iterable, Mapping
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field

from reporewind.recipes.poetry import poetry_requirement
from reporewind.recipes.source import TreeSource
from reporewind.recipes.static import UNKNOWN, callee, lines, literal, module_literals, strings
from reporewind.recipes.static import table as toml_table


class ProjectMetadata(BaseModel):
    """Classifiers and runtime requirements of a project at one commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classifiers: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    extras: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    sources: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def requirements(self, extras: Iterable[str] = ()) -> tuple[str, ...]:
        """Runtime requirements plus those of the given (normalized) extras, deduplicated."""
        out = list(self.dependencies)
        for extra in extras:
            out += self.extras.get(canonicalize_name(extra), ())
        return tuple(dict.fromkeys(out))


class _Reader:
    def __init__(self, tree: TreeSource) -> None:
        self.tree = tree
        self.index = frozenset(tree.files())
        self.classifiers: list[str] = []
        self.dependencies: list[str] = []
        self.extras: dict[str, list[str]] = {}
        self.sources: list[str] = []
        self.notes: list[str] = []

    def text(self, path: str) -> str | None:
        if path not in self.index:
            return None
        data = self.tree.read(path)
        if data is None:
            return None
        self.sources.append(path)
        return data.decode("utf-8-sig", "replace")

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def add_requirements(self, target: list[str], values: Iterable[str], origin: str) -> None:
        for value in values:
            try:
                normalized = str(Requirement(value))
            except InvalidRequirement:
                self.note(f"{origin}: skipped {value!r} (not a PEP 508 requirement)")
                continue
            if normalized not in target:
                target.append(normalized)

    def add_extras(self, table: Mapping[str, object], origin: str) -> None:
        for name, values in table.items():
            if ":" in name:
                self.note(f"{origin}: skipped conditional extra {name!r}")
                continue
            bucket = self.extras.setdefault(canonicalize_name(name), [])
            self.add_requirements(bucket, strings(values), f"{origin} {name}")

    def add_classifiers(self, values: object) -> None:
        if not self.classifiers:
            self.classifiers = [value.strip() for value in strings(values) if value.strip()]


def _pyproject(reader: _Reader) -> None:
    text = reader.text("pyproject.toml")
    if text is None:
        return
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        reader.note("pyproject.toml is not valid TOML; it was skipped")
        return
    project = toml_table(data, "project")
    dynamic = set(strings(project.get("dynamic")))
    reader.add_classifiers(project.get("classifiers"))
    if "dependencies" in dynamic:
        reader.note("pyproject.toml: dependencies are dynamic (computed at build time)")
    reader.add_requirements(
        reader.dependencies, strings(project.get("dependencies")), "pyproject.toml dependencies"
    )
    if "optional-dependencies" in dynamic:
        reader.note("pyproject.toml: optional-dependencies are dynamic (computed at build time)")
    reader.add_extras(toml_table(project, "optional-dependencies"), "pyproject.toml extra")

    poetry = toml_table(data, "tool", "poetry")
    if not poetry:
        return
    reader.add_classifiers(poetry.get("classifiers"))
    for name, spec in toml_table(poetry, "dependencies").items():
        if canonicalize_name(name) == "python":
            continue
        try:
            requirement = poetry_requirement(name, spec)
        except (ValueError, TypeError) as exc:
            reader.note(f"pyproject.toml: poetry dependency skipped: {exc}")
            continue
        reader.add_requirements(reader.dependencies, [requirement], "pyproject.toml poetry")
    for name, members in toml_table(poetry, "extras").items():
        # Poetry extras name optional dependencies declared in [tool.poetry.dependencies].
        declared = toml_table(poetry, "dependencies")
        by_name = {canonicalize_name(key): key for key in declared}
        resolved = []
        for member in strings(members):
            key = by_name.get(canonicalize_name(member))
            if key is None:
                reader.note(f"pyproject.toml: poetry extra {name} names undeclared {member!r}")
                continue
            try:
                resolved.append(poetry_requirement(key, declared[key]))
            except (ValueError, TypeError):
                continue
        reader.add_extras({name: resolved}, "pyproject.toml poetry extra")


def _setup_cfg(reader: _Reader) -> None:
    text = reader.text("setup.cfg")
    if text is None:
        return
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text, source="setup.cfg")
    except configparser.Error:
        reader.note("setup.cfg could not be parsed; it was skipped")
        return
    reader.add_classifiers(lines(parser.get("metadata", "classifiers", fallback="")))
    install = parser.get("options", "install_requires", fallback="")
    if install.strip().startswith("file:"):
        reader.note("setup.cfg: install_requires reads a file at build time; not read")
    else:
        reader.add_requirements(reader.dependencies, lines(install), "setup.cfg install_requires")
    if parser.has_section("options.extras_require"):
        extras = {name: lines(value) for name, value in parser.items("options.extras_require")}
        reader.add_extras(extras, "setup.cfg extras_require")


def _setup_py(reader: _Reader) -> None:
    text = reader.text("setup.py")
    if text is None:
        return
    try:
        module = ast.parse(text, filename="setup.py")
    except (SyntaxError, ValueError):
        reader.note("setup.py is not valid Python 3 syntax; its arguments were not read")
        return
    calls = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call) and callee(node.func) == "setup"
    ]
    if not calls:
        return
    names = module_literals(module)
    for keyword in calls[0].keywords:
        if keyword.arg not in {"classifiers", "install_requires", "extras_require"}:
            continue
        value: Any = literal(keyword.value, names)
        if value is UNKNOWN:
            reader.note(f"setup.py: {keyword.arg} is computed when setup.py runs; not read")
        elif keyword.arg == "classifiers":
            reader.add_classifiers(value)
        elif keyword.arg == "install_requires":
            origin = "setup.py install_requires"
            reader.add_requirements(reader.dependencies, strings(value), origin)
        elif isinstance(value, dict):
            plain = {k: v for k, v in value.items() if isinstance(k, str)}
            reader.add_extras(plain, "setup.py extras_require")


def read_metadata(tree: TreeSource) -> ProjectMetadata:
    """Read classifiers and runtime requirements of ``tree`` without running any of its code.

    Classifiers come from the first file that lists them (``pyproject.toml``,
    then ``setup.cfg``, then ``setup.py``); dependencies and extras from all
    of them, deduplicated in that order.
    """
    reader = _Reader(tree)
    _pyproject(reader)
    _setup_cfg(reader)
    _setup_py(reader)
    return ProjectMetadata(
        classifiers=tuple(reader.classifiers),
        dependencies=tuple(reader.dependencies),
        extras={name: tuple(values) for name, values in sorted(reader.extras.items())},
        sources=tuple(reader.sources),
        notes=tuple(reader.notes),
    )


__all__ = ["ProjectMetadata", "read_metadata"]
