"""Byte-exact JSON for :class:`ResolvedFix`.

Diffs of files that are not valid UTF-8 (old Latin-1 sources, for example)
carry their raw bytes as lone surrogates (``surrogateescape``), which
pydantic's own JSON encoder rejects. :func:`dump_resolved_fix` writes them
as ``\\udcXX`` escapes in pure-ASCII JSON and :func:`load_resolved_fix` turns
them back, so a patch read from the JSON re-applies byte for byte.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from reporewind.errors import ResolveError
from reporewind.models import ResolvedFix


def dump_resolved_fix(fix: ResolvedFix, *, indent: int | None = 2) -> str:
    """Serialize ``fix`` to ASCII JSON (with a trailing newline)."""
    return json.dumps(fix.model_dump(mode="json"), indent=indent, ensure_ascii=True) + "\n"


def load_resolved_fix(text: str) -> ResolvedFix:
    """Parse JSON written by :func:`dump_resolved_fix`; raise :class:`ResolveError` if invalid."""
    try:
        return ResolvedFix.model_validate(json.loads(text))
    except (ValueError, ValidationError) as exc:
        raise ResolveError(f"invalid ResolvedFix JSON: {exc}") from exc


__all__ = ["dump_resolved_fix", "load_resolved_fix"]
