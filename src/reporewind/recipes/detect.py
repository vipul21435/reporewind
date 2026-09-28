"""Detect a build recipe from a repository's packaging metadata at one commit.

Detectors read, in this order of precedence:

* ``pyproject.toml``: PEP 621 ``[project]``, the ``[build-system]`` backend
  (setuptools, poetry, hatch, flit, pdm, ...), PEP 735
  ``[dependency-groups]``, ``[tool.poetry]`` (constraints translated to
  PEP 440), legacy ``[tool.flit.metadata]``, hatch environments and
  ``[tool.pytest.ini_options]``;
* ``setup.cfg``: ``[options]``, ``[options.extras_require]``,
  ``[tool:pytest]``;
* ``setup.py``: parsed with :mod:`ast` and **never executed**; only literal
  keyword arguments of the ``setup()`` call (or module-level names bound to
  literals and never changed in place or rebound) are read, and anything
  computed at run time is reported in the notes instead of guessed;
* ``requirements*.txt`` and ``requirements/*.txt`` (runtime and test files;
  docs, lint and typing files are skipped, and when ``tox.ini`` names
  requirements files, tox is the authority: files it uses only under a
  factor such as ``min:`` are never installed);
* ``tox.ini``: ``[testenv]`` deps, extras, commands and setenv.

The result is a :class:`Detection`: the sparse recipe fields that had
evidence, the files that were read and human-readable notes about what was
skipped. Fields without evidence are left out so they take the schema
default, and a human override can change the framework without also having
to restate the command.
"""

from __future__ import annotations

import ast
import configparser
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field

from reporewind.errors import InvalidPathError
from reporewind.gitops import Git
from reporewind.models import Sha, check_repo_path
from reporewind.recipes.model import (
    Framework,
    InstallMode,
    Recipe,
    normalize_extra,
    normalize_requirement,
    normalize_specifier,
)
from reporewind.recipes.poetry import poetry_constraint, poetry_requirement
from reporewind.recipes.source import GitTreeSource, TreeSource
from reporewind.recipes.static import UNKNOWN as _UNKNOWN
from reporewind.recipes.static import callee as _callee
from reporewind.recipes.static import lines as _lines
from reporewind.recipes.static import literal as _literal
from reporewind.recipes.static import module_literals as _module_literals
from reporewind.recipes.static import strings as _strings
from reporewind.recipes.static import table as _table

TEST_NAMES = frozenset({"test", "tests", "testing"})
"""Extras, dependency groups and hatch environments that hold test dependencies."""

DEV_NAME = "dev"
"""Fallback group when a project has no test-specific one."""

NATIVE_BUILD_PACKAGES = ("build-essential",)
"""Debian packages added when the repository compiles C extensions."""

MAX_TEST_FILES_SCANNED = 25

BACKENDS: Mapping[str, str] = {
    "setuptools.build_meta": "setuptools",
    "setuptools.build_meta:__legacy__": "setuptools",
    "poetry.core.masonry.api": "poetry",
    "poetry.masonry.api": "poetry",
    "hatchling.build": "hatch",
    "flit_core.buildapi": "flit",
    "flit.buildapi": "flit",
    "pdm.backend": "pdm",
    "pdm.pep517.api": "pdm",
    "maturin": "maturin",
}
# Backends that predate PEP 660, so `pip install -e .` fails with them.
_NO_EDITABLE = frozenset({"poetry.masonry.api", "flit.buildapi"})
_SKIPPED_REQUIREMENT_WORDS = ("doc", "lint", "typing", "mypy", "style", "release", "bench")
_TOX_FACTOR = re.compile(r"^[\w.!,{}-]+:\s")
_TEST_FILE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$")
_IMPORTS_PYTEST = re.compile(r"^\s*(?:import pytest\b|from pytest\b)", re.MULTILINE)
_TEST_FUNCTION = re.compile(r"^def test_", re.MULTILINE)
_TEST_CASE = re.compile(r"\bTestCase\b")
_NATIVE_SUFFIXES = (".pyx", ".c", ".cpp", ".cc")


class Detection(BaseModel):
    """What detection found at one commit, before human overrides."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commit: Sha | None = None
    backend: str | None = None
    sources: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    fields: dict[str, Any] = Field(default_factory=dict)

    def recipe(self) -> Recipe:
        """The detected fields validated as a :class:`Recipe` (defaults fill the rest)."""
        return Recipe.model_validate(self.fields)


@dataclass
class _Facts:
    python: list[tuple[str, str]] = field(default_factory=list)
    backend: str | None = None
    backend_entry: str | None = None
    has_metadata: bool = False
    extras_available: dict[str, list[str]] = field(default_factory=dict)
    extras_wanted: list[str] = field(default_factory=list)
    test_requirements: list[str] = field(default_factory=list)
    requirements_files: list[str] = field(default_factory=list)
    tox_requirements: bool = False
    factor_requirements: set[str] = field(default_factory=set)
    declared_names: set[str] = field(default_factory=set)
    pytest_evidence: list[str] = field(default_factory=list)
    unittest_evidence: list[str] = field(default_factory=list)
    native: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


class _Scan:
    """Reads files from the snapshot, remembering sources and notes."""

    def __init__(self, tree: TreeSource) -> None:
        self.tree = tree
        self.files = tree.files()
        self.index = frozenset(self.files)
        self.sources: list[str] = []
        self.notes: list[str] = []

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def text(self, path: str, *, record: bool = True) -> str | None:
        if path not in self.index:
            return None
        data = self.tree.read(path)
        if data is None:
            return None
        if record and path not in self.sources:
            self.sources.append(path)
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            self.note(f"{path} is not valid UTF-8; undecodable bytes were replaced")
            return data.decode("utf-8", "replace")

    def toml(self, path: str) -> dict[str, Any] | None:
        text = self.text(path)
        if text is None:
            return None
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            self.note(f"{path} is not valid TOML ({exc}); it was skipped")
            return None

    def ini(self, path: str) -> configparser.ConfigParser | None:
        text = self.text(path)
        if text is None:
            return None
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(text, source=path)
        except configparser.Error as exc:
            self.note(f"{path} could not be parsed ({exc.__class__.__name__}); it was skipped")
            return None
        return parser


# -- small helpers ------------------------------------------------------------


def _requirement_name(text: str) -> str | None:
    try:
        return canonicalize_name(Requirement(text).name)
    except InvalidRequirement:
        return None


def _add_requirements(scan: _Scan, facts: _Facts, values: Iterable[str], origin: str) -> None:
    for value in values:
        if "{" in value:
            scan.note(f"{origin}: skipped {value!r} (uses tool-specific substitution)")
            continue
        try:
            facts.test_requirements.append(normalize_requirement(value))
        except ValueError:
            scan.note(f"{origin}: skipped {value!r} (not a PEP 508 requirement)")


def _extra(scan: _Scan, value: str, origin: str) -> str | None:
    try:
        return normalize_extra(value.strip())
    except ValueError:
        scan.note(f"{origin}: skipped invalid extra name {value!r}")
        return None


def _add_extras(scan: _Scan, facts: _Facts, table: Mapping[str, object], origin: str) -> None:
    for name, requirements in table.items():
        if extra := _extra(scan, name, origin):
            facts.extras_available[extra] = _strings(requirements)


def _pick_names(available: Iterable[str]) -> list[str]:
    """The test-ish names among ``available``, or ``dev`` if there are none."""
    names = list(available)
    chosen = [name for name in names if canonicalize_name(name) in TEST_NAMES]
    if not chosen:
        chosen = [name for name in names if canonicalize_name(name) == DEV_NAME]
    return chosen


# -- pyproject.toml -----------------------------------------------------------


def _dependency_group(
    scan: _Scan, groups: Mapping[str, Any], name: str, seen: frozenset[str]
) -> list[str]:
    by_normal = {canonicalize_name(key): key for key in groups}
    key = by_normal.get(canonicalize_name(name))
    items = groups.get(key, []) if key is not None else []
    out: list[str] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("include-group"), str):
            included = item["include-group"]
            if canonicalize_name(included) in seen:
                scan.note(f"pyproject.toml: dependency group cycle through {included!r}")
                continue
            out += _dependency_group(scan, groups, included, seen | {canonicalize_name(included)})
    return out


def _pyproject_poetry(scan: _Scan, facts: _Facts, poetry: Mapping[str, Any]) -> None:
    facts.has_metadata = True
    facts.backend = facts.backend or "poetry"
    for name, spec in _table(poetry, "dependencies").items():
        if name == "python":
            try:
                if not isinstance(spec, str):
                    raise TypeError(spec)
                facts.python.append(("[tool.poetry.dependencies] python", poetry_constraint(spec)))
            except (ValueError, TypeError):
                scan.note(f"pyproject.toml: python constraint {spec!r} was not translated")
            continue
        facts.declared_names.add(canonicalize_name(name))
    for name in _table(poetry, "extras"):
        if extra := _extra(scan, name, "pyproject.toml [tool.poetry.extras]"):
            facts.extras_available.setdefault(extra, [])
    groups = {name: _table(body, "dependencies") for name, body in _table(poetry, "group").items()}
    if not groups and _table(poetry, "dev-dependencies"):
        groups = {"dev": _table(poetry, "dev-dependencies")}
    for group in _pick_names(groups):
        for name, spec in groups[group].items():
            try:
                requirement = poetry_requirement(name, spec)
            except (ValueError, TypeError) as exc:
                scan.note(f"pyproject.toml: poetry {group} dependency skipped: {exc}")
                continue
            _add_requirements(scan, facts, [requirement], f"pyproject.toml poetry group {group}")


def _pyproject(scan: _Scan, facts: _Facts) -> None:
    data = scan.toml("pyproject.toml")
    if data is None:
        return
    build = _table(data, "build-system")
    backend = build.get("build-backend")
    if build:
        facts.has_metadata = True
        entry = backend if isinstance(backend, str) else "setuptools.build_meta:__legacy__"
        facts.backend_entry = entry
        facts.backend = BACKENDS.get(entry, entry)
        requires = " ".join(_strings(build.get("requires"))).lower()
        if "setuptools-rust" in requires or facts.backend == "maturin":
            scan.note("the build needs a Rust toolchain; add it with a pre_install override")
        if "cython" in requires:
            facts.native.append("pyproject.toml build requires Cython")

    project = data.get("project")
    if isinstance(project, Mapping):
        facts.has_metadata = True
        if not build:
            # PEP 517: without [build-system], pip falls back to setuptools.
            facts.backend = facts.backend or "setuptools"
        requires_python = project.get("requires-python")
        if isinstance(requires_python, str):
            facts.python.append(("[project] requires-python", requires_python))
        for requirement in _strings(project.get("dependencies")):
            if name := _requirement_name(requirement):
                facts.declared_names.add(name)
        optional = _table(project, "optional-dependencies")
        _add_extras(scan, facts, optional, "pyproject.toml optional-dependencies")

    groups = _table(data, "dependency-groups")
    for group in _pick_names(groups):
        members = _dependency_group(scan, groups, group, frozenset({canonicalize_name(group)}))
        _add_requirements(scan, facts, members, f"pyproject.toml dependency group {group}")

    tool = _table(data, "tool")
    if poetry := _table(tool, "poetry"):
        _pyproject_poetry(scan, facts, poetry)
    if flit := _table(tool, "flit", "metadata"):
        facts.has_metadata = True
        facts.backend = facts.backend or "flit"
        if isinstance(flit.get("requires-python"), str):
            facts.python.append(("[tool.flit.metadata] requires-python", flit["requires-python"]))
        _add_extras(scan, facts, _table(flit, "requires-extra"), "pyproject.toml flit metadata")
    envs = _table(tool, "hatch", "envs")
    for env in [name for name in envs if name in TEST_NAMES | {"default", "hatch-test"}]:
        body = envs[env]
        origin = f"pyproject.toml hatch env {env}"
        _add_requirements(scan, facts, _strings(_table(envs, env).get("dependencies")), origin)
        _add_requirements(
            scan, facts, _strings(_table(envs, env).get("extra-dependencies")), origin
        )
        facts.extras_wanted += _strings(body.get("features") if isinstance(body, Mapping) else None)
        for script in _table(envs, env, "scripts").values():
            if "pytest" in " ".join(_strings(script)):
                facts.pytest_evidence.append(f"{origin} runs pytest")
    if "pytest" in tool:
        facts.pytest_evidence.append("pyproject.toml [tool.pytest]")


# -- setup.cfg ----------------------------------------------------------------


def _setup_cfg(scan: _Scan, facts: _Facts) -> None:
    cfg = scan.ini("setup.cfg")
    if cfg is None:
        return
    if cfg.has_section("metadata") or cfg.has_section("options"):
        facts.has_metadata = True
        facts.backend = facts.backend or "setuptools"
    if cfg.has_option("options", "python_requires"):
        facts.python.append(("setup.cfg python_requires", cfg.get("options", "python_requires")))
    for name in _lines(cfg.get("options", "install_requires", fallback="")):
        if parsed := _requirement_name(name):
            facts.declared_names.add(parsed)
    if cfg.has_section("options.extras_require"):
        extras = {name: _lines(value) for name, value in cfg.items("options.extras_require")}
        _add_extras(scan, facts, extras, "setup.cfg extras_require")
    tests_require = _lines(cfg.get("options", "tests_require", fallback=""))
    _add_requirements(scan, facts, tests_require, "setup.cfg tests_require")
    if cfg.has_section("tool:pytest"):
        facts.pytest_evidence.append("setup.cfg [tool:pytest]")


# -- setup.py (ast only, never executed) --------------------------------------


def _setup_py(scan: _Scan, facts: _Facts) -> None:
    text = scan.text("setup.py")
    if text is None:
        return
    facts.has_metadata = True
    facts.backend = facts.backend or "setuptools"
    try:
        module = ast.parse(text, filename="setup.py")
    except (SyntaxError, ValueError):
        scan.note("setup.py is not valid Python 3 syntax; its arguments were not read")
        return
    calls = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call) and _callee(node.func) in {"setup", "Extension"}
    ]
    if any(_callee(call.func) == "Extension" for call in calls):
        facts.native.append("setup.py builds an Extension")
    setup_calls = [call for call in calls if _callee(call.func) == "setup"]
    if not setup_calls:
        scan.note("setup.py has no setup() call that could be read")
        return
    names = _module_literals(module, before=setup_calls[0].lineno)
    for keyword in setup_calls[0].keywords:
        if keyword.arg is None:
            scan.note("setup.py passes **kwargs to setup(); those values were not read")
            continue
        if keyword.arg == "ext_modules":
            facts.native.append("setup.py sets ext_modules")
            continue
        if keyword.arg not in {
            "python_requires",
            "install_requires",
            "extras_require",
            "tests_require",
            "test_suite",
        }:
            continue
        value = _literal(keyword.value, names)
        if value is _UNKNOWN:
            scan.note(f"setup.py: {keyword.arg} is computed when setup.py runs; not read")
        elif keyword.arg == "python_requires" and isinstance(value, str):
            facts.python.append(("setup.py python_requires", value))
        elif keyword.arg == "install_requires":
            for requirement in _strings(value):
                if name := _requirement_name(requirement):
                    facts.declared_names.add(name)
        elif keyword.arg == "extras_require" and isinstance(value, dict):
            plain = {k: v for k, v in value.items() if isinstance(k, str) and ":" not in k}
            _add_extras(scan, facts, plain, "setup.py extras_require")
        elif keyword.arg == "tests_require":
            _add_requirements(scan, facts, _strings(value), "setup.py tests_require")
        elif keyword.arg == "test_suite":
            facts.unittest_evidence.append("setup.py sets test_suite")


# -- requirements files -------------------------------------------------------


def _requirements_candidates(files: Iterable[str]) -> list[str]:
    runtime: list[str] = []
    tests: list[str] = []
    dev: list[str] = []
    for path in files:
        pure = PurePosixPath(path)
        in_dir = len(pure.parts) == 2 and pure.parts[0] == "requirements"
        at_root = len(pure.parts) == 1 and "requirements" in pure.name.lower()
        if pure.suffix != ".txt" or not (in_dir or at_root):
            continue
        lowered = path.lower()
        if any(word in lowered for word in _SKIPPED_REQUIREMENT_WORDS):
            continue
        if "test" in lowered:
            tests.append(path)
        elif "dev" in lowered or re.search(r"\bci\b", pure.stem.lower()):
            dev.append(path)
        elif path == "requirements.txt":
            runtime.append(path)
    return runtime + sorted(tests or dev)


def _requirements_files(scan: _Scan, facts: _Facts) -> None:
    for path in _requirements_candidates(scan.files):
        if path in facts.requirements_files:
            continue
        if path in facts.factor_requirements:
            continue  # tox installs it only under a factor; already noted
        if facts.tox_requirements and path != "requirements.txt":
            scan.note(f"{path} not installed: tox.ini's [testenv] does not use it")
            continue
        facts.requirements_files.append(path)
    for path in facts.requirements_files:
        for line in _lines(scan.text(path) or ""):
            if not line.startswith("-") and (name := _requirement_name(line)):
                facts.declared_names.add(name)


# -- tox.ini ------------------------------------------------------------------


def _tox_dep(scan: _Scan, facts: _Facts, line: str) -> None:
    if _TOX_FACTOR.match(line):
        scan.note(f"tox.ini: skipped factor-conditional dependency {line!r}")
        rest = line.split(":", 1)[1].strip()
        if rest.startswith("-r"):
            facts.factor_requirements.add(rest[2:].strip())
        return
    if "{" in line:
        scan.note(f"tox.ini: skipped {line!r} (uses tox substitution)")
        return
    if line.startswith("-r"):
        path = line[2:].strip()
        try:
            check_repo_path(path)
        except InvalidPathError:
            scan.note(f"tox.ini: skipped requirements file {path!r} (outside the repository)")
            return
        facts.tox_requirements = True
        if path not in scan.index:
            scan.note(f"tox.ini: requirements file {path} does not exist at this commit")
        elif path not in facts.requirements_files:
            facts.requirements_files.append(path)
        return
    if line.startswith(("-c", "--")):
        scan.note(f"tox.ini: skipped option line {line!r}")
        return
    target = line.removeprefix("-e").strip()
    if target.startswith(".["):
        facts.extras_wanted += [extra.strip() for extra in target[2:].rstrip("]").split(",")]
        return
    if target in {".", "-e ."}:
        return
    _add_requirements(scan, facts, [line], "tox.ini deps")


def _tox_ini(scan: _Scan, facts: _Facts) -> None:
    cfg = scan.ini("tox.ini")
    if cfg is None:
        return
    if cfg.has_section("pytest"):
        facts.pytest_evidence.append("tox.ini [pytest]")
    if not cfg.has_section("testenv"):
        return
    for line in _lines(cfg.get("testenv", "deps", fallback="")):
        _tox_dep(scan, facts, line)
    for line in _lines(cfg.get("testenv", "extras", fallback="")):
        facts.extras_wanted += [extra.strip() for extra in line.split(",") if extra.strip()]
    commands = cfg.get("testenv", "commands", fallback="")
    if "pytest" in commands or "py.test" in commands:
        facts.pytest_evidence.append("tox.ini commands run pytest")
    elif "unittest" in commands:
        facts.unittest_evidence.append("tox.ini commands run unittest")
    for line in _lines(cfg.get("testenv", "setenv", fallback="")):
        key, sep, value = line.partition("=")
        if not sep or "{" in line or not re.fullmatch(r"[A-Za-z_]\w*", key.strip()):
            scan.note(f"tox.ini: skipped setenv line {line!r}")
            continue
        facts.env[key.strip()] = value.strip()


# -- the whole tree -----------------------------------------------------------


def _scan_tree(scan: _Scan, facts: _Facts) -> None:
    if "pytest.ini" in scan.index:
        facts.pytest_evidence.append("pytest.ini")
    if any(PurePosixPath(path).name == "conftest.py" for path in scan.files):
        facts.pytest_evidence.append("conftest.py")
    for path in scan.files:
        if path.endswith(_NATIVE_SUFFIXES) and not _TEST_FILE.search(path):
            facts.native.append(f"C/Cython sources such as {path}")
            break
    test_files = [path for path in scan.files if _TEST_FILE.search(path)]
    for path in test_files[:MAX_TEST_FILES_SCANNED]:
        text = scan.text(path, record=False) or ""
        if _IMPORTS_PYTEST.search(text):
            facts.pytest_evidence.append("test files import pytest")
        elif _TEST_FUNCTION.search(text):
            facts.pytest_evidence.append("test files define plain test functions")
        elif _TEST_CASE.search(text):
            facts.unittest_evidence.append("test files use unittest.TestCase")


def _unittest_command(scan: _Scan) -> list[str] | None:
    for directory in ("tests", "test"):
        prefix = f"{directory}/"
        if any(path.startswith(prefix) and _TEST_FILE.search(path) for path in scan.files):
            command = ["python", "-m", "unittest", "discover", "-v", "-s", directory]
            if f"{prefix}__init__.py" in scan.index:
                command += ["-t", "."]
            return command
    return None


def _python(scan: _Scan, facts: _Facts) -> str | None:
    chosen: tuple[str, str] | None = None
    for origin, value in facts.python:
        try:
            normalized = normalize_specifier(value)
        except ValueError:
            scan.note(f"{origin} {value!r} is not a valid specifier; ignored")
            continue
        if chosen is None:
            chosen = (origin, normalized)
        elif normalized != chosen[1]:
            scan.note(f"{origin} {normalized!r} differs from {chosen[0]} {chosen[1]!r}; ignored")
    return chosen[1] if chosen else None


def detect_recipe(tree: TreeSource) -> Detection:
    """Detect the recipe of the snapshot ``tree``; never runs repository code."""
    scan = _Scan(tree)
    facts = _Facts()
    _pyproject(scan, facts)
    _setup_cfg(scan, facts)
    _setup_py(scan, facts)
    _tox_ini(scan, facts)
    _requirements_files(scan, facts)
    _scan_tree(scan, facts)

    fields: dict[str, Any] = {}
    if python := _python(scan, facts):
        fields["python"] = python

    if facts.has_metadata:
        install = InstallMode.EDITABLE
        if facts.backend_entry in _NO_EDITABLE:
            install = InstallMode.PACKAGE
            scan.note(f"backend {facts.backend_entry} has no editable installs; using 'package'")
    elif facts.requirements_files:
        install = InstallMode.REQUIREMENTS
    else:
        install = InstallMode.NONE
    fields["install"] = install.value

    wanted = {
        extra
        for name in facts.extras_wanted
        if name.strip() and (extra := _extra(scan, name, "requested extras"))
    }
    wanted |= {extra for extra in _pick_names(facts.extras_available) if extra != DEV_NAME}
    if wanted and install in {InstallMode.EDITABLE, InstallMode.PACKAGE}:
        fields["extras"] = sorted(wanted)
        for extra in wanted:
            for requirement in facts.extras_available.get(extra, []):
                if name := _requirement_name(requirement):
                    facts.declared_names.add(name)
    elif wanted:
        scan.note(f"extras {sorted(wanted)} ignored: there is no installable project")

    if facts.requirements_files:
        fields["requirements_files"] = list(facts.requirements_files)

    framework = Framework.PYTEST
    if facts.pytest_evidence:
        scan.note(f"pytest: {facts.pytest_evidence[0]}")
    elif facts.unittest_evidence:
        framework = Framework.UNITTEST
        scan.note(f"unittest: {facts.unittest_evidence[0]}")
    else:
        scan.note("no test runner evidence; defaulting to pytest")
    fields["test_framework"] = framework.value
    if framework is Framework.UNITTEST and (command := _unittest_command(scan)):
        fields["test_command"] = command

    for requirement in facts.test_requirements:
        if name := _requirement_name(requirement):
            facts.declared_names.add(name)
    tests = list(dict.fromkeys(facts.test_requirements))
    if framework is Framework.PYTEST and "pytest" not in facts.declared_names:
        tests.append("pytest")
        scan.note("pytest is not declared anywhere; added unpinned (pinning dates it)")
    if tests:
        fields["test_dependencies"] = tests

    if facts.native:
        fields["system_packages"] = list(NATIVE_BUILD_PACKAGES)
        scan.note(f"compiler toolchain added: {facts.native[0]}")
    if facts.env:
        fields["env"] = dict(sorted(facts.env.items()))

    if not facts.has_metadata and not facts.requirements_files:
        scan.note("no packaging metadata or requirements files found")
    return Detection(
        commit=tree.commit,
        backend=facts.backend,
        sources=tuple(scan.sources),
        notes=tuple(scan.notes),
        fields=fields,
    )


def detect_at(git: Git, rev: str) -> Detection:
    """Detect the recipe at commit ``rev`` of ``git`` (no checkout needed)."""
    return detect_recipe(GitTreeSource(git, rev))


__all__ = [
    "BACKENDS",
    "DEV_NAME",
    "NATIVE_BUILD_PACKAGES",
    "TEST_NAMES",
    "Detection",
    "detect_at",
    "detect_recipe",
]
