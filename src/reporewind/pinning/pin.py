"""Pin one commit: interpreter, dependency lock and base image, recorded as a PinResult.

:func:`pin_commit` ties the three pinning steps together for the commit that
gets built (the fix's base):

1. :func:`~reporewind.pinning.python.choose_python` picks ``X.Y`` from the
   commit date, the recipe's Python constraint and the trove classifiers;
2. :func:`~reporewind.pinning.image.resolve_base_image` pins
   ``python:X.Y-slim`` to a digest;
3. :func:`~reporewind.pinning.lock.compile_lock` locks the project's runtime
   requirements (and those of the recipe's extras), the recipe's test
   dependencies and its requirements files as of the commit date.

:func:`write_pin` stores the lock file next to ``pin.json``, the
:class:`PinResult` that later stages (the Dockerfile renderer, bundles) read.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from reporewind.errors import PinError
from reporewind.models import CommitInfo, RepoRef, Sha
from reporewind.pinning.image import BaseImage, resolve_base_image
from reporewind.pinning.lock import DEFAULT_PLATFORM, LockRequest, compile_lock
from reporewind.pinning.metadata import read_metadata
from reporewind.pinning.python import PythonChoice, choose_python
from reporewind.proc import CommandRunner
from reporewind.recipes.model import InstallMode, Recipe, recipe_hash
from reporewind.recipes.source import TreeSource

PIN_FILE = "pin.json"
LOCK_FILE = "requirements.lock"


class PinResult(BaseModel):
    """Everything pinned for one commit; stored as ``pin.json`` beside the lock file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    repo: RepoRef
    commit: Sha
    commit_date: AwareDatetime
    recipe_hash: str
    python: PythonChoice
    base_image: BaseImage
    exclude_newer: AwareDatetime
    platform: str
    lock_file: str = LOCK_FILE
    lock_sha256: str
    packages: tuple[str, ...]
    requirements: tuple[str, ...]
    requirements_files: tuple[str, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PinOptions:
    """Choices a caller can make instead of taking the inferred defaults."""

    python: str | None = None
    exclude_newer: datetime | None = None
    platform: str = DEFAULT_PLATFORM
    generate_hashes: bool = True
    offline: bool = False


def pin_commit(
    tree: TreeSource,
    repo: RepoRef,
    commit: CommitInfo,
    recipe: Recipe,
    runner: CommandRunner,
    options: PinOptions | None = None,
    *,
    notes: tuple[str, ...] = (),
) -> tuple[PinResult, str]:
    """Pin ``commit`` (whose snapshot is ``tree``); return the result and the lock text."""
    options = options or PinOptions()
    metadata = read_metadata(tree)
    choice = choose_python(
        commit.committer_date,
        constraint=recipe.python,
        classifiers=metadata.classifiers,
        override=options.python,
    )
    image, image_notes = resolve_base_image(choice.version, runner, offline=options.offline)
    installs_project = recipe.install in {InstallMode.EDITABLE, InstallMode.PACKAGE}
    project = metadata.requirements(recipe.extras) if installs_project else ()
    # Test dependencies may name the project's own extras too (dependency groups).
    requirements = metadata.expand((*project, *recipe.test_dependencies))
    cutoff = options.exclude_newer or commit.committer_date
    lock = compile_lock(
        tree,
        LockRequest(
            python_version=choice.version,
            exclude_newer=cutoff,
            platform=options.platform,
            requirements=requirements,
            requirements_files=recipe.requirements_files,
            generate_hashes=options.generate_hashes,
            offline=options.offline,
        ),
        runner,
    )
    result = PinResult(
        repo=repo,
        commit=commit.sha,
        commit_date=commit.committer_date,
        recipe_hash=recipe_hash(recipe),
        python=choice,
        base_image=image,
        exclude_newer=cutoff,
        platform=options.platform,
        lock_sha256=lock.sha256,
        packages=lock.packages,
        requirements=requirements,
        requirements_files=recipe.requirements_files,
        notes=(*notes, *metadata.notes, *choice.notes, *image_notes, *lock.notes),
    )
    return result, lock.text


def dump_pin_result(result: PinResult) -> str:
    """``pin.json`` text: indented ASCII JSON with a trailing newline."""
    return json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=True) + "\n"


def load_pin_result(path: Path) -> PinResult:
    """Read and validate a ``pin.json``; raise :class:`PinError` if it is not one."""
    try:
        return PinResult.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise PinError(f"{path} is not a valid pin result: {exc}") from None


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        Path(tmp).chmod(0o644)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_pin(result: PinResult, lock_text: str, out_dir: Path) -> tuple[Path, Path]:
    """Write the lock file, then ``pin.json``; return both paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    lock_path = out_dir / result.lock_file
    pin_path = out_dir / PIN_FILE
    _write_atomic(lock_path, lock_text)
    _write_atomic(pin_path, dump_pin_result(result))
    return lock_path, pin_path


__all__ = [
    "LOCK_FILE",
    "PIN_FILE",
    "PinOptions",
    "PinResult",
    "dump_pin_result",
    "load_pin_result",
    "pin_commit",
    "write_pin",
]
