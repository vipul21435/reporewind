"""Split a ``git diff`` into one :class:`FilePatch` per file.

The input is what :meth:`reporewind.gitops.Git.diff` produces: ``--binary
--full-index`` output with ``a/`` and ``b/`` prefixes. Each ``diff --git``
section becomes a :class:`FilePatch` whose ``diff`` is exactly that section's
text, so any subset of the patches can be joined again and fed to
``git apply``.

Paths are taken from the least ambiguous source git offers, in order:
``rename``/``copy`` headers, the ``---``/``+++`` lines, and only then the
``diff --git`` line itself (binary and mode-only changes have nothing else).
Quoted paths use git's C-style escapes (``"caf\\303\\251.py"``) and are
decoded to UTF-8. A ``Binary files ... differ`` stub has no patch data and
cannot be re-applied, so it is rejected rather than silently dropped.

A type change (say a regular file replaced by a symlink) is emitted by git as
a deletion followed by an addition of the same path; the two sections are
merged into one ``modified`` patch so every path appears once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import ValidationError

from reporewind.errors import PatchParseError
from reporewind.models import ChangeKind, FilePatch

DIFF_HEADER = "diff --git "
DEV_NULL = "/dev/null"

_SECTION_START = re.compile(r"^diff --git ", re.MULTILINE)
_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}
_OCTAL = frozenset("01234567")
# Extended header lines that carry nothing the splitter needs.
_IGNORED_HEADERS = ("old mode ", "new mode ", "index ", "similarity index ", "dissimilarity index ")


def _decode_path(raw: bytes, *, context: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PatchParseError(f"path in {context} is not valid UTF-8: {raw!r}") from None


def parse_quoted(text: str, start: int = 0) -> tuple[str, int]:
    """Decode the C-style quoted path starting at ``text[start]`` (a ``"``).

    Returns the decoded path and the index just past the closing quote.
    """
    if not text.startswith('"', start):
        raise PatchParseError(f"expected a quoted path at column {start}: {text!r}")
    out = bytearray()
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == '"':
            return _decode_path(bytes(out), context=repr(text)), i + 1
        if ch != "\\":
            out += ch.encode("utf-8", "surrogateescape")
            i += 1
            continue
        esc = text[i + 1 : i + 2]
        if esc in _C_ESCAPES:
            out.append(_C_ESCAPES[esc])
            i += 2
        elif len(digits := text[i + 1 : i + 4]) == 3 and set(digits) <= _OCTAL:
            out.append(int(digits, 8) & 0xFF)
            i += 4
        else:
            raise PatchParseError(f"invalid escape in quoted path: {text!r}")
    raise PatchParseError(f"unterminated quoted path: {text!r}")


def _plain_or_quoted(value: str) -> str:
    """A path as git writes it after ``rename from`` and friends (no prefix)."""
    if value.startswith('"'):
        path, end = parse_quoted(value)
        if end != len(value):
            raise PatchParseError(f"trailing text after quoted path: {value!r}")
        return path
    return _decode_path(value.encode("utf-8", "surrogateescape"), context=repr(value))


def _strip_prefix(name: str, prefix: str, line: str) -> str:
    if not name.startswith(prefix):
        raise PatchParseError(f"expected a {prefix!r} path prefix in {line!r}")
    return name[len(prefix) :]


def _dash_name(line: str, prefix: str) -> str | None:
    """Path from a ``--- a/x`` or ``+++ b/x`` line; ``None`` for ``/dev/null``."""
    rest = line[4:]
    if rest.startswith('"'):
        name, _ = parse_quoted(rest)
    else:
        # git appends a TAB after names that contain a space; anything after it
        # (a timestamp, in other tools' output) is not part of the name.
        name = _plain_or_quoted(rest.split("\t", 1)[0])
    if name == DEV_NULL:
        return None
    return _strip_prefix(name, prefix, line)


def _git_line_names(line: str) -> tuple[str, str]:
    """``(old, new)`` paths from a ``diff --git a/x b/y`` line.

    Unquoted names may contain spaces, so an unquoted line is only accepted in
    the unambiguous same-name form ``a/P b/P`` (every change except renames and
    copies, whose paths come from their own headers).
    """
    rest = line[len(DIFF_HEADER) :]
    if rest.startswith('"'):
        old, end = parse_quoted(rest)
        if rest[end : end + 1] != " ":
            raise PatchParseError(f"malformed diff header: {line!r}")
        new = _plain_or_quoted(rest[end + 1 :])
        return _strip_prefix(old, "a/", line), _strip_prefix(new, "b/", line)
    size, odd = divmod(len(rest) - len("a/ b/"), 2)
    name = rest[2 : 2 + size]
    if odd or size < 1 or rest != f"a/{name} b/{name}":
        raise PatchParseError(f"cannot determine the path from diff header {line!r}")
    path = _plain_or_quoted(name)
    return path, path


@dataclass(slots=True)
class _Header:
    change: ChangeKind = ChangeKind.MODIFIED
    binary: bool = False
    old: str | None = None
    new: str | None = None
    has_dash_lines: bool = False


def _read_header(lines: list[str]) -> _Header:
    header = _Header()
    # The last element is the empty string after the section's final newline.
    for line in lines[1:-1]:
        if line.startswith("@@"):
            break
        if line == "GIT binary patch":
            header.binary = True
            break
        if line.startswith("Binary files ") and line.endswith(" differ"):
            raise PatchParseError(
                f"binary change without patch data ({line!r}); diff with --binary to keep it"
            )
        if line.startswith("--- "):
            header.old = _dash_name(line, "a/")
            header.has_dash_lines = True
        elif line.startswith("+++ "):
            header.new = _dash_name(line, "b/")
        elif line.startswith("new file mode "):
            header.change = ChangeKind.ADDED
        elif line.startswith("deleted file mode "):
            header.change = ChangeKind.DELETED
        elif line.startswith(("rename from ", "copy from ")):
            header.change = ChangeKind.RENAMED if line[0] == "r" else ChangeKind.COPIED
            header.old = _plain_or_quoted(line.split(" ", 2)[2])
        elif line.startswith(("rename to ", "copy to ")):
            header.new = _plain_or_quoted(line.split(" ", 2)[2])
        elif not line.startswith(_IGNORED_HEADERS):
            raise PatchParseError(f"unexpected line in diff header: {line!r}")
    return header


def _parse_section(section: str) -> FilePatch:
    lines = section.split("\n")
    header = _read_header(lines)
    change, old, new = header.change, header.old, header.new
    renamed = change in {ChangeKind.RENAMED, ChangeKind.COPIED}
    if renamed and (old is None or new is None):
        raise PatchParseError(f"{change.value} without both paths: {lines[0]!r}")
    path = old if change is ChangeKind.DELETED else new
    if path is None:
        if header.has_dash_lines:
            raise PatchParseError(f"{change.value} file with a /dev/null side: {lines[0]!r}")
        # Binary, mode-only and empty-file changes have no ---/+++ lines.
        old, new = _git_line_names(lines[0])
        path = old if change is ChangeKind.DELETED else new
    try:
        return FilePatch(
            path=path,
            old_path=old if renamed else None,
            change=change,
            binary=header.binary,
            diff=section,
        )
    except ValidationError as exc:
        detail = "; ".join(str(err["msg"]) for err in exc.errors())
        raise PatchParseError(f"invalid file patch for {lines[0]!r}: {detail}") from None


def _written_paths(patch: FilePatch) -> tuple[str, ...]:
    """Paths a patch creates, changes or removes (a copy only reads its source)."""
    if patch.change is ChangeKind.RENAMED and patch.old_path is not None:
        return (patch.old_path, patch.path)
    return (patch.path,)


def _merge_type_changes(patches: list[FilePatch]) -> tuple[FilePatch, ...]:
    merged: list[FilePatch] = []
    seen: set[str] = set()
    for patch in patches:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.path == patch.path
            and prev.change is ChangeKind.DELETED
            and patch.change is ChangeKind.ADDED
        ):
            merged[-1] = FilePatch(
                path=patch.path,
                change=ChangeKind.MODIFIED,
                binary=prev.binary or patch.binary,
                diff=prev.diff + patch.diff,
            )
            continue
        for path in _written_paths(patch):
            if path in seen:
                raise PatchParseError(f"path {path!r} appears in more than one diff section")
            seen.add(path)
        merged.append(patch)
    return tuple(merged)


def parse_patch(text: str) -> tuple[FilePatch, ...]:
    """Split ``git diff`` output into per-file patches, in diff order.

    Raises :class:`PatchParseError` for anything that is not a complete,
    re-applicable ``git diff`` (leading junk, a missing final newline, an
    unknown header line, an unsafe path or a binary stub without data).
    """
    if not text:
        return ()
    if not text.startswith(DIFF_HEADER):
        raise PatchParseError("patch must start with a 'diff --git' header")
    if not text.endswith("\n"):
        raise PatchParseError("patch does not end with a newline; it looks truncated")
    starts = [match.start() for match in _SECTION_START.finditer(text)]
    bounds = zip(starts, [*starts[1:], len(text)], strict=True)
    return _merge_type_changes([_parse_section(text[a:b]) for a, b in bounds])


__all__ = ["DEV_NULL", "DIFF_HEADER", "parse_patch", "parse_quoted"]
