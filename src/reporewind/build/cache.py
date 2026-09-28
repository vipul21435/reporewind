"""Content-addressed image tags, the JSON build-cache index and the locked build.

An environment is identified by a hash of everything that goes into its
image: the recipe hash, the lock file's sha256, the base image digest, the
repository and commit, and the renderer version. The image tag is derived
from that hash, and ``$REPOREWIND_HOME/build/index.json`` maps each hash to
the tag and image id that were built for it.

:func:`ensure_image` builds an environment at most once: a cache hit whose
image still exists returns at once; otherwise the builder takes the
per-environment :class:`~reporewind.build.lock.BuildLock`, looks at the
index again (another process may have finished the build while it waited)
and only then builds and records the result.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from reporewind.build.dockerfile import RENDERER_VERSION, lock_sha256
from reporewind.build.lock import BuildLock
from reporewind.errors import BuildError, CommandError
from reporewind.gitops import Git
from reporewind.models import RepoRef, Sha
from reporewind.pinning.pin import PinResult
from reporewind.proc import CommandRunner

INDEX_FILE: Final = "index.json"
DEFAULT_LOCK_TIMEOUT: Final = 1800.0
BUILD_TIMEOUT: Final = 3600.0


def environment_key(pin: PinResult, lock_text: str) -> str:
    """``sha256:`` of the canonical JSON of every input that changes the image."""
    payload = {
        "renderer": RENDERER_VERSION,
        "repo": str(pin.repo),
        "commit": pin.commit,
        "recipe": pin.recipe_hash,
        "lock": lock_sha256(lock_text),
        "base": pin.base_image.digest,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(text.encode("ascii")).hexdigest()


def image_tag(repo: RepoRef, key: str) -> str:
    """``reporewind/<owner>__<repo>:env-<16 hex>``; the same environment, the same tag."""
    return f"reporewind/{repo.recipe_stem}:env-{key.removeprefix('sha256:')[:16]}"


class CacheEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str
    image_id: str
    repo: RepoRef
    commit: Sha
    built_at: AwareDatetime


class CacheIndex(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    images: dict[str, CacheEntry] = Field(default_factory=dict)


class BuildCache:
    """``<root>/index.json`` plus one lock file per environment under ``<root>/locks``."""

    def __init__(self, root: Path, *, lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        self.root = root
        self.lock_timeout = lock_timeout

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_FILE

    def lock_for(self, key: str) -> BuildLock:
        name = key.removeprefix("sha256:")
        return BuildLock(self.root / "locks" / f"{name}.lock", timeout=self.lock_timeout)

    def read(self) -> CacheIndex:
        try:
            return CacheIndex.model_validate_json(self.index_path.read_bytes())
        except FileNotFoundError:
            return CacheIndex()
        except (OSError, ValidationError) as exc:
            raise BuildError(f"{self.index_path} is not a valid build-cache index: {exc}") from None

    def get(self, key: str) -> CacheEntry | None:
        return self.read().images.get(key)

    def record(self, key: str, entry: CacheEntry) -> None:
        """Add ``entry`` under ``key``; read-modify-write under the index lock."""
        self.root.mkdir(parents=True, exist_ok=True)
        with BuildLock(self.root / "index.lock", timeout=self.lock_timeout):
            images = {**self.read().images, key: entry}
            text = CacheIndex(images=dict(sorted(images.items()))).model_dump_json(indent=2)
            fd, tmp = tempfile.mkstemp(prefix=".index.", dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(text + "\n")
                Path(tmp).replace(self.index_path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise


class ImageBuilder(Protocol):
    """What :func:`ensure_image` needs from a container engine."""

    def image_id(self, tag: str) -> str | None:
        """The id of the local image ``tag``, or ``None`` if there is none."""
        ...

    def build(self, context: Path, tag: str) -> str:
        """Build ``context`` (which holds a ``Dockerfile``) as ``tag``; return the image id."""
        ...


@dataclass(frozen=True, slots=True)
class DockerBuilder:
    """:class:`ImageBuilder` backed by the ``docker`` CLI."""

    runner: CommandRunner
    docker: str = "docker"

    def image_id(self, tag: str) -> str | None:
        argv = [self.docker, "image", "inspect", "--format", "{{.Id}}", tag]
        result = self.runner.run(argv, timeout=60)
        return result.stdout_text.strip() or None if result.ok else None

    def build(self, context: Path, tag: str) -> str:
        argv = [self.docker, "build", "--label", "project=reporewind", "--tag", tag, str(context)]
        result = self.runner.run(argv, timeout=BUILD_TIMEOUT)
        if not result.ok:
            raise CommandError(argv, result.returncode, result.stdout_text, result.stderr_text)
        image = self.image_id(tag)
        if image is None:
            raise BuildError(f"docker build succeeded but {tag} is not in the local image store")
        return image


def export_source(git: Git, sha: str, dest: Path) -> None:
    """Write the tree of ``sha`` to ``dest`` with ``git archive`` (no ``.git`` directory)."""
    archive = git.run("archive", "--format=tar", "--end-of-options", git.rev_parse(sha)).stdout
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        tar.extractall(dest, filter="data")


def prepare_context(
    context: Path, dockerfile: str, lock_text: str, write_source: Callable[[Path], None]
) -> None:
    """Lay out a build context: ``Dockerfile``, ``requirements.lock`` and ``src/``."""
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(dockerfile, encoding="utf-8", newline="\n")
    (context / "requirements.lock").write_text(lock_text, encoding="utf-8", newline="\n")
    write_source(context / "src")


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    key: str
    tag: str
    image_id: str
    status: Literal["built", "cached"]


def ensure_image(
    pin: PinResult,
    dockerfile: str,
    lock_text: str,
    *,
    builder: ImageBuilder,
    cache: BuildCache,
    write_source: Callable[[Path], None],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BuildOutcome:
    """Return the image for this environment, building it only if no process has."""
    key = environment_key(pin, lock_text)
    tag = image_tag(pin.repo, key)

    def cached() -> BuildOutcome | None:
        entry = cache.get(key)
        if entry is not None and builder.image_id(entry.tag) == entry.image_id:
            return BuildOutcome(key, entry.tag, entry.image_id, "cached")
        return None

    if hit := cached():
        return hit
    with cache.lock_for(key):
        if hit := cached():
            return hit  # another process built it while this one waited
        cache.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="context-", dir=cache.root) as tmp:
            prepare_context(Path(tmp), dockerfile, lock_text, write_source)
            image = builder.build(Path(tmp), tag)
        cache.record(
            key,
            CacheEntry(tag=tag, image_id=image, repo=pin.repo, commit=pin.commit, built_at=now()),
        )
    return BuildOutcome(key, tag, image, "built")


__all__ = [
    "BuildCache",
    "BuildOutcome",
    "CacheEntry",
    "CacheIndex",
    "DockerBuilder",
    "ImageBuilder",
    "ensure_image",
    "environment_key",
    "export_source",
    "image_tag",
    "prepare_context",
]
