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


def module_literals(module: ast.Module) -> dict[str, object]:
    names: dict[str, object] = {}
    for node in module.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                value = literal(node.value, names)
                if value is not UNKNOWN:
                    names[target.id] = value
                else:
                    names.pop(target.id, None)
    return names


__all__ = ["UNKNOWN", "callee", "lines", "literal", "module_literals", "strings", "table"]
