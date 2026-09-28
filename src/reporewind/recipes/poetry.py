"""Translate Poetry's version constraints and dependency tables to PEP 440/508.

Poetry writes ``^1.2`` and ``~1.2`` where pip expects ``>=1.2,<2`` and
``>=1.2,<1.3``, and its dependency tables use ``{version, extras, python,
markers}`` instead of requirement strings. The rules follow Poetry's own
documentation for caret, tilde, wildcard and inequality constraints.
Alternatives (``a || b``) have no PEP 440 equivalent and are rejected.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from packaging.specifiers import InvalidSpecifier, SpecifierSet

_NUMERIC = re.compile(r"^(\d+(?:\.\d+)*)(.*)$")
_OPERATOR = re.compile(r"^(===|==|!=|<=|>=|<|>|=)\s*(.+)$")
# Poetry accepts both "a,b" and "a b" for "and".
_AND_SPLIT = re.compile(r"\s*,\s*|\s+(?=[<>=!^~])")
_UNSUPPORTED_KEYS = ("git", "path", "url", "file")


def _numbers(version: str) -> list[int]:
    match = _NUMERIC.match(version)
    if match is None:
        raise ValueError(f"cannot read version {version!r}")
    return [int(part) for part in match[1].split(".")]


def _bump(numbers: list[int], index: int) -> str:
    return ".".join(str(n) for n in [*numbers[:index], numbers[index] + 1])


def _caret(version: str) -> list[str]:
    numbers = _numbers(version)
    index = next((i for i, n in enumerate(numbers) if n), len(numbers) - 1)
    return [f">={version}", f"<{_bump(numbers, index)}"]


def _tilde(version: str) -> list[str]:
    numbers = _numbers(version)
    return [f">={version}", f"<{_bump(numbers, 0 if len(numbers) == 1 else 1)}"]


def poetry_constraint(value: str) -> str:
    """Convert a Poetry constraint to a PEP 440 specifier set (``""`` means any)."""
    text = value.strip()
    if "|" in text:
        raise ValueError(f"constraint {value!r} uses '||', which PEP 440 cannot express")
    out: list[str] = []
    for part in (p for p in _AND_SPLIT.split(text) if p):
        if part == "*":
            continue
        if part.startswith("^"):
            out += _caret(part[1:].strip())
        elif part.startswith("~="):
            out.append(f"~={part[2:].strip()}")
        elif part.startswith("~"):
            out += _tilde(part[1:].strip())
        elif match := _OPERATOR.match(part):
            operator = "==" if match[1] == "=" else match[1]
            out.append(f"{operator}{match[2].strip()}")
        else:
            out.append(f"=={part}")
    joined = ",".join(out)
    try:
        return str(SpecifierSet(joined))
    except InvalidSpecifier as exc:
        raise ValueError(f"cannot convert constraint {value!r}: {exc}") from None


def python_markers(constraint: str) -> str:
    """Environment markers equivalent to a Poetry ``python = "..."`` restriction."""
    specifiers = SpecifierSet(poetry_constraint(constraint))
    return " and ".join(
        f'python_version {spec.operator} "{spec.version}"' for spec in sorted(specifiers, key=str)
    )


def poetry_requirement(name: str, spec: object) -> str:
    """A PEP 508 requirement for one entry of a Poetry dependency table.

    Raises ``ValueError`` for entries pip cannot install from an index
    (``git``, ``path``, ``url``, ``file``) and for multiple-constraint lists.
    """
    extras: list[str] = []
    markers: list[str] = []
    if isinstance(spec, str):
        constraint = spec
    elif isinstance(spec, Mapping):
        source = next((key for key in _UNSUPPORTED_KEYS if key in spec), None)
        if source is not None:
            raise ValueError(f"{name} is installed from a {source} source, not an index")
        constraint = str(spec.get("version", "*"))
        raw_extras = spec.get("extras", [])
        if isinstance(raw_extras, list):
            extras = [str(extra) for extra in raw_extras]
        python = spec.get("python")
        if isinstance(python, str) and (restriction := python_markers(python)):
            markers.append(restriction)
        if isinstance(spec.get("markers"), str):
            markers.append(f"({spec['markers']})")
    else:
        raise ValueError(f"{name} has a multiple-constraint entry with no single equivalent")
    requirement = name
    if extras:
        requirement += f"[{','.join(extras)}]"
    requirement += poetry_constraint(constraint)
    if markers:
        requirement += "; " + " and ".join(markers)
    return requirement


__all__ = ["poetry_constraint", "poetry_requirement", "python_markers"]
