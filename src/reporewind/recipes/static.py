"""Static readers shared by recipe detection and pinning.

Everything here reads values without running repository code: TOML tables,
ini values and the literal parts of a ``setup.py`` syntax tree.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Any, Final

UNKNOWN: Final = object()
"""Returned by :func:`literal` for values that only exist when setup.py runs."""


def table(data: Mapping[str, Any] | None, *keys: str) -> dict[str, Any]:
    """``data[k1][k2]...`` if every level is a table, else ``{}``."""
    current: Any = data or {}
    for key in keys:
        current = current.get(key) if isinstance(current, Mapping) else None
    return dict(current) if isinstance(current, Mapping) else {}


def strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def lines(value: str) -> list[str]:
    """Non-empty, non-comment lines of a multi-line ini value."""
    out = []
    for raw in value.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def callee(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def literal(node: ast.expr, names: Mapping[str, object]) -> object:
    """Evaluate literals, names bound to literals and ``+`` of lists or strings.

    Returns ``UNKNOWN`` for anything else (calls, attributes, comprehensions):
    those values only exist when setup.py runs, and it never runs here.
    """
    if isinstance(node, ast.Name):
        return names.get(node.id, UNKNOWN)
    if isinstance(node, ast.List | ast.Tuple):
        items = [literal(item, names) for item in node.elts]
        return UNKNOWN if UNKNOWN in items else items
    if isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):
            return UNKNOWN  # {**other}
        keys = [literal(key, names) for key in node.keys if key is not None]
        values = [literal(value, names) for value in node.values]
        if UNKNOWN in keys or UNKNOWN in values:
            return UNKNOWN
        try:
            return dict(zip(keys, values, strict=True))
        except TypeError:
            return UNKNOWN  # unhashable key
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = literal(node.left, names), literal(node.right, names)
        if isinstance(left, list) and isinstance(right, list):
            return left + right
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        return UNKNOWN
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return UNKNOWN


_MUTATING_METHODS: Final = frozenset(
    {"append", "extend", "insert", "update", "setdefault", "pop", "remove", "clear", "add"}
)


def _mutated_names(module: ast.Module) -> set[str]:
    """Names changed in place anywhere: ``+=``, ``x[k] = v``, ``x.append(...)``, ``del x[k]``."""
    mutated: set[str] = set()
    for node in ast.walk(module):
        targets: list[ast.expr] = []
        if isinstance(node, ast.AugAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign | ast.Delete):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATING_METHODS
        ):
            targets = [node.func.value]
        for target in targets:
            if isinstance(node, ast.AugAssign | ast.Call) and isinstance(target, ast.Name):
                mutated.add(target.id)
            while isinstance(target, ast.Subscript | ast.Attribute):
                target = target.value
                if isinstance(target, ast.Name):
                    mutated.add(target.id)
    return mutated


def module_literals(module: ast.Module, *, before: int | None = None) -> dict[str, object]:
    """Module-level names bound to literals, left out when the value is not certain.

    A name is left out (so :func:`literal` returns ``UNKNOWN`` and callers
    note it) when it is changed in place anywhere (``+=``, item assignment,
    ``append``/``extend``/``update``...), bound anywhere other than a plain
    top-level assignment (loops, ``with``, functions, conditionals) or, with
    ``before`` (a line number such as the ``setup()`` call's), rebound at or
    after that line.
    """
    mutated = _mutated_names(module)
    top_level = {id(node) for node in module.body if isinstance(node, ast.Assign)}
    unsure = set(mutated)
    for node in ast.walk(module):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            parent_ok = any(
                id(stmt) in top_level
                and any(target is node for target in stmt.targets)
                and (before is None or stmt.lineno < before)
                for stmt in module.body
                if isinstance(stmt, ast.Assign)
            )
            if not parent_ok:
                unsure.add(node.id)
    names: dict[str, object] = {}
    for stmt in module.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if isinstance(target, ast.Name) and (before is None or stmt.lineno < before):
                value = literal(stmt.value, names)
                if value is not UNKNOWN:
                    names[target.id] = value
                else:
                    names.pop(target.id, None)
    for name in unsure:
        names.pop(name, None)
    return names


__all__ = ["UNKNOWN", "callee", "lines", "literal", "module_literals", "strings", "table"]
