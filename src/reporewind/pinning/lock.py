"""Lock dependencies as they were on the commit date with ``uv pip compile``.

``uv pip compile --exclude-newer <date>`` resolves against the package index
as if every file uploaded after ``<date>`` did not exist, so the lock holds
the versions a developer would have installed on the day of the commit, not
today's. ``--python-version`` and ``--python-platform`` resolve for the
image's interpreter and Linux, whatever machine runs RepoRewind.

Inputs are staged in a scratch directory: a generated ``requirements.in``
(runtime requirements of the project and its extras, plus the recipe's
test dependencies) and the recipe's requirements files copied from the
commit's tree at their repository paths, so relative ``-r``/``-c`` includes
keep working. Lines that install the project itself (``-e .``, ``.``,
``-e ".[tests]"``, ``-e.``) or other local paths are dropped: the project is
installed separately, and resolving it would run its build backend on the
host. :func:`project_lines` reports the extras such lines asked for, so the
caller can lock the requirements of those extras from the commit's metadata.

uv is reached through the :class:`~reporewind.proc.CommandRunner` protocol,
so unit tests use a fake; an ``e2e`` test runs the real resolver.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from pydantic import AwareDatetime, BaseModel, ConfigDict

from reporewind.errors import InvalidPathError, PinError, stderr_tail
from reporewind.models import RepoPath, check_repo_path
from reporewind.proc import CommandRunner
from reporewind.recipes.source import TreeSource

DEFAULT_PLATFORM = "x86_64-unknown-linux-gnu"
"""Target of ``--python-platform``: the architecture of the build images."""

UV_TIMEOUT = 900.0
INPUT_NAME = "requirements.in"
REPO_DIR = "repo"
MAX_INCLUDE_DEPTH = 10

_INCLUDE = re.compile(r"^(-r|-c|--requirement|--constraint)(?:\s*=\s*|\s+|(?=\S))(\S+)$")
_PINNED = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==(\S+)")
# Lines that point at the project itself or at other local directories,
# optionally editable, quoted and with extras: -e ".[tests]", -e., ./pkg[x].
_LOCAL = re.compile(
    r"""^(?:(?:-e|--editable)(?:\s*=\s*|\s*))?(?P<q>["']?)"""
    r"""(?P<path>\.{1,2}(?:/[^\s\[\]"']*)?|/[^\s\[\]"']*|file:[^\s\[\]"']*)"""
    r"""(?:\[(?P<extras>[^\]]*)\])?(?P=q)$"""
)
_ROOT = frozenset({".", "./", "file:.", "file:./"})
# A comment starts at a '#' at the start of a line or after any whitespace.
_COMMENT = re.compile(r"(?:^|\s)#")


def _strip_comment(raw: str) -> str:
    return _COMMENT.split(raw, maxsplit=1)[0].strip()


def format_timestamp(value: datetime) -> str:
    """RFC 3339 UTC timestamp with second precision, as passed to ``--exclude-newer``."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class LockRequest(BaseModel):
    """What to lock and for which interpreter, platform and date."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    python_version: str
    exclude_newer: AwareDatetime
    platform: str = DEFAULT_PLATFORM
    requirements: tuple[str, ...] = ()
    requirements_files: tuple[RepoPath, ...] = ()
    generate_hashes: bool = True
    offline: bool = False


class LockResult(BaseModel):
    """A lock file and how it was produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    sha256: str
    packages: tuple[str, ...]
    argv: tuple[str, ...]
    notes: tuple[str, ...] = ()


class _Walk:
    """State of one pass over requirements files and their includes."""

    def __init__(self, tree: TreeSource, root: Path | None) -> None:
        self.tree = tree
        self.root = root
        self.notes: list[str] = []
        self.seen: set[str] = set()
        self.installs_project = False
        self.extras: list[str] = []


def _stage_file(walk: _Walk, path: str, depth: int) -> None:
    if path in walk.seen:
        return
    walk.seen.add(path)
    notes = walk.notes
    data = walk.tree.read(path)
    if data is None:
        raise PinError(f"requirements file {path} does not exist at this commit")
    kept: list[str] = []
    for raw in data.decode("utf-8-sig", "replace").splitlines():
        line = _strip_comment(raw)
        if local := _LOCAL.fullmatch(line):
            if local["path"] in _ROOT:
                walk.installs_project = True
                for extra in (local["extras"] or "").split(","):
                    if extra.strip() and extra.strip() not in walk.extras:
                        walk.extras.append(extra.strip())
            notes.append(f"{path}: dropped {line!r} (the project is installed separately)")
            continue
        if match := _INCLUDE.fullmatch(line):
            target = str(PurePosixPath(path).parent / match[2])
            normalized = os.path.normpath(target).replace(os.sep, "/")
            try:
                check_repo_path(normalized)
            except InvalidPathError:
                notes.append(f"{path}: dropped {line!r} (outside the repository)")
                continue
            if depth >= MAX_INCLUDE_DEPTH:
                raise PinError(f"{path}: requirements includes nest deeper than {depth}")
            _stage_file(walk, normalized, depth + 1)
        kept.append(raw)
    if walk.root is None:
        return
    target_path = walk.root / REPO_DIR / path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def stage_inputs(tree: TreeSource, request: LockRequest, root: Path) -> tuple[list[str], list[str]]:
    """Write the resolver inputs under ``root``; return (relative input paths, notes)."""
    inputs: list[str] = []
    if request.requirements:
        (root / INPUT_NAME).write_text("\n".join(request.requirements) + "\n", encoding="utf-8")
        inputs.append(INPUT_NAME)
    walk = _Walk(tree, root)
    for path in request.requirements_files:
        _stage_file(walk, path, 0)
        inputs.append(f"{REPO_DIR}/{path}")
    return inputs, walk.notes


def project_lines(tree: TreeSource, paths: Sequence[str]) -> tuple[str, ...] | None:
    """Extras asked for by lines that install the project (``-e .[tests]``) in ``paths``.

    ``None`` when no line installs the project; ``()`` when one does without extras.
    """
    walk = _Walk(tree, None)
    for path in paths:
        _stage_file(walk, path, 0)
    return tuple(walk.extras) if walk.installs_project else None


def uv_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The caller's environment without ``UV_*`` settings (the cache location is kept)."""
    env = dict(os.environ if source is None else source)
    return {k: v for k, v in env.items() if not k.startswith("UV_") or k == "UV_CACHE_DIR"}


def lock_header(request: LockRequest) -> str:
    return (
        f"# Locked by reporewind for Python {request.python_version} on {request.platform}\n"
        f"# from packages published up to {format_timestamp(request.exclude_newer)}"
        " (uv pip compile --exclude-newer).\n"
    )


def pinned_packages(text: str) -> tuple[str, ...]:
    """``name==version`` of every package pinned in a lock file, in file order."""
    return tuple(f"{m[1]}=={m[2]}" for line in text.splitlines() if (m := _PINNED.match(line)))


def compile_lock(
    tree: TreeSource,
    request: LockRequest,
    runner: CommandRunner,
    *,
    uv: str = "uv",
    env: Mapping[str, str] | None = None,
    timeout: float = UV_TIMEOUT,
) -> LockResult:
    """Resolve ``request`` against the index as of ``request.exclude_newer``.

    Raises :class:`PinError` if uv fails (an unsatisfiable constraint, a
    package that did not exist yet on that date, or no cache in offline mode).
    """
    with tempfile.TemporaryDirectory(prefix="reporewind-lock-") as scratch:
        root = Path(scratch)
        inputs, notes = stage_inputs(tree, request, root)
        argv = [
            uv,
            "pip",
            "compile",
            "--no-config",
            "--no-header",
            "--python-version",
            request.python_version,
            "--python-platform",
            request.platform,
            "--exclude-newer",
            format_timestamp(request.exclude_newer),
        ]
        if request.generate_hashes:
            argv.append("--generate-hashes")
        if request.offline:
            argv.append("--offline")
        argv += inputs
        if not inputs:
            notes.append("nothing to lock: no requirements and no requirements files")
            text = lock_header(request)
            return _result(text, argv, notes)
        result = runner.run(argv, cwd=root, env=uv_environment(env), timeout=timeout)
    if not result.ok:
        detail = stderr_tail(result.stderr_text, 12)
        raise PinError(f"uv pip compile failed (exit {result.returncode}):\n{detail}")
    return _result(lock_header(request) + result.stdout_text, argv, notes)


def _result(text: str, argv: Sequence[str], notes: list[str]) -> LockResult:
    digest = hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()
    return LockResult(
        text=text,
        sha256=f"sha256:{digest}",
        packages=pinned_packages(text),
        argv=tuple(argv),
        notes=tuple(notes),
    )


__all__ = [
    "DEFAULT_PLATFORM",
    "INPUT_NAME",
    "LockRequest",
    "LockResult",
    "compile_lock",
    "format_timestamp",
    "lock_header",
    "pinned_packages",
    "project_lines",
    "stage_inputs",
    "uv_environment",
]
