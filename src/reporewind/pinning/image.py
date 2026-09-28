"""Pin ``python:X.Y-slim`` to an immutable ``@sha256:`` digest.

A tag moves every time the image is rebuilt; a digest never does. The
digest of the multi-platform image index is read with ``docker buildx
imagetools inspect`` (registry metadata only, nothing is pulled). When that
is not possible (no Docker, no network) or ``REPOREWIND_OFFLINE`` is set,
the digest comes from :data:`FALLBACK_DIGESTS`, a table recorded with the
same command, and the result says so.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reporewind.errors import CommandError, PinError, ToolNotFoundError, stderr_tail
from reporewind.proc import CommandRunner

OFFLINE_ENV = "REPOREWIND_OFFLINE"
INSPECT_TIMEOUT = 120.0

DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_DIGEST_RE = re.compile(DIGEST_PATTERN)
_TRUE = frozenset({"1", "true", "yes", "on"})

FALLBACK_RECORDED = date(2026, 9, 29)
"""When :data:`FALLBACK_DIGESTS` was recorded."""

FALLBACK_DIGESTS: Mapping[str, str] = {
    "3.7": "sha256:b53f496ca43e5af6994f8e316cf03af31050bf7944e0e4a308ad86c001cf028b",
    "3.8": "sha256:1d52838af602b4b5a831beb13a0e4d073280665ea7be7f69ce2382f29c5a613f",
    "3.9": "sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b",
    "3.10": "sha256:31dd4d9529d02d7436659061cb7564cd4733fc90e5e152709a942d53382ec8d0",
    "3.11": "sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e",
    "3.12": "sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f",
    "3.13": "sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b",
    "3.14": "sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d",
}
"""Index digests of ``python:X.Y-slim`` from ``docker buildx imagetools inspect``."""


def image_tag(python_version: str) -> str:
    """The Docker Official Image tag for a Python minor version."""
    return f"python:{python_version}-slim"


def is_offline(env: Mapping[str, str] | None = None) -> bool:
    """True if ``$REPOREWIND_OFFLINE`` is set to 1, true, yes or on."""
    source = os.environ if env is None else env
    return source.get(OFFLINE_ENV, "").strip().lower() in _TRUE


class BaseImage(BaseModel):
    """A base image tag and the digest it resolved to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str
    digest: str = Field(pattern=DIGEST_PATTERN)
    source: Literal["registry", "fallback"]

    @property
    def reference(self) -> str:
        """``tag@digest``, the form a Dockerfile ``FROM`` line pins."""
        return f"{self.tag}@{self.digest}"


def fallback_image(python_version: str) -> BaseImage:
    """The recorded digest for ``python_version``; :class:`PinError` if there is none."""
    digest = FALLBACK_DIGESTS.get(python_version)
    if digest is None:
        raise PinError(f"no recorded digest for {image_tag(python_version)} in the offline table")
    return BaseImage(tag=image_tag(python_version), digest=digest, source="fallback")


def inspect_digest(tag: str, runner: CommandRunner, *, docker: str = "docker") -> str:
    """The index digest of ``tag`` from the registry; raises on any failure."""
    argv = [docker, "buildx", "imagetools", "inspect", tag, "--format", "{{json .Manifest}}"]
    result = runner.run(argv, timeout=INSPECT_TIMEOUT)
    if not result.ok:
        raise CommandError(argv, result.returncode, result.stdout_text, result.stderr_text)
    try:
        digest = json.loads(result.stdout_text)["digest"]
    except (ValueError, KeyError, TypeError):
        raise PinError(f"unexpected output from {' '.join(argv[:4])} for {tag}") from None
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise PinError(f"{tag} resolved to an invalid digest {digest!r}")
    return digest


def resolve_base_image(
    python_version: str,
    runner: CommandRunner,
    *,
    offline: bool = False,
    docker: str = "docker",
) -> tuple[BaseImage, tuple[str, ...]]:
    """Resolve ``python:X.Y-slim`` to a digest; return the image and notes.

    Offline, the recorded table is used directly. Online, a registry lookup
    that fails falls back to the table with a note, so a missing Docker
    daemon does not stop pinning; a version absent from the table then
    raises :class:`PinError`.
    """
    tag = image_tag(python_version)
    recorded = f"the offline table recorded on {FALLBACK_RECORDED}"
    if offline:
        return fallback_image(python_version), (f"{OFFLINE_ENV}: {tag} digest from {recorded}",)
    try:
        digest = inspect_digest(tag, runner, docker=docker)
    except (CommandError, ToolNotFoundError, PinError) as exc:
        reason = str(exc).splitlines()[0]
        if isinstance(exc, CommandError) and (tail := stderr_tail(exc.stderr, 1)):
            reason = tail
        image = fallback_image(python_version)
        return image, (f"registry lookup failed ({reason}); {tag} digest from {recorded}",)
    return BaseImage(tag=tag, digest=digest, source="registry"), ()


__all__ = [
    "DIGEST_PATTERN",
    "FALLBACK_DIGESTS",
    "FALLBACK_RECORDED",
    "OFFLINE_ENV",
    "BaseImage",
    "fallback_image",
    "image_tag",
    "inspect_digest",
    "is_offline",
    "resolve_base_image",
]
