"""Shared inputs for the build-stage tests (importable by spawned processes)."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from reporewind.models import RepoRef
from reporewind.pinning.image import BaseImage
from reporewind.pinning.pin import PinResult
from reporewind.pinning.python import PythonChoice
from reporewind.recipes.model import Recipe, recipe_hash

LOCK = (
    "# reporewind: python 3.8, x86_64-unknown-linux-gnu, exclude-newer 2021-05-11T19:02:03Z\n"
    "iniconfig==1.1.1 \\\n"
    "    --hash=sha256:011e24c64b7f47f6ebd835bb12a743f2fbe9a26d4cecaa7f53bc4f35ee9da8b3\n"
    "pytest==6.2.4 \\\n"
    "    --hash=sha256:50bcad0a0b9c5a72c8e4e7c9855a3ad496ca6a881a3641b4260605450772c54b\n"
)
DIGEST = "sha256:" + "ab" * 32
COMMIT = "c0ffee" + "0" * 34


def pin_for(recipe: Recipe, lock: str = LOCK, *, digest: str = DIGEST) -> PinResult:
    when = datetime(2021, 5, 11, 19, 2, 3, tzinfo=UTC)
    return PinResult(
        repo=RepoRef.parse("example/calc"),
        commit=COMMIT,
        commit_date=when,
        recipe_hash=recipe_hash(recipe),
        python=PythonChoice(version="3.8", reason="test"),
        base_image=BaseImage(tag="python:3.8-slim", digest=digest, source="fallback"),
        exclude_newer=when,
        platform="x86_64-unknown-linux-gnu",
        lock_sha256=hashlib.sha256(lock.encode()).hexdigest(),
        packages=tuple(line.split(" ")[0] for line in lock.splitlines() if "==" in line),
        requirements=("pytest",),
        requirements_files=recipe.requirements_files,
    )


class FileRegistryBuilder:
    """An ImageBuilder whose "images" are files, so separate processes share them.

    Every build appends one line to ``builds.log`` and sleeps first, which
    widens the window in which a missing lock would let two builds overlap.
    """

    def __init__(self, root: Path, *, delay: float = 0.3) -> None:
        self.root = root
        self.delay = delay

    def _image(self, tag: str) -> Path:
        return self.root / "images" / hashlib.sha256(tag.encode()).hexdigest()

    def image_id(self, tag: str) -> str | None:
        path = self._image(tag)
        return path.read_text() if path.is_file() else None

    def build(self, context: Path, tag: str) -> str:
        files = sorted(p.relative_to(context).as_posix() for p in context.rglob("*") if p.is_file())
        time.sleep(self.delay)
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "builds.log").open("a") as log:
            log.write(json.dumps({"tag": tag, "files": files}) + "\n")
        image = "sha256:" + hashlib.sha256((context / "Dockerfile").read_bytes()).hexdigest()
        self._image(tag).parent.mkdir(parents=True, exist_ok=True)
        self._image(tag).write_text(image)
        return image

    def builds(self) -> list[dict[str, object]]:
        log = self.root / "builds.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
