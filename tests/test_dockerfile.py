"""Static checks on the Dockerfile: pinned bases, non-root user, project label."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="ascii")


def _args() -> dict[str, str]:
    return dict(re.findall(r"^ARG (\w+)=(\S+)$", DOCKERFILE, flags=re.MULTILINE))


def test_every_base_image_is_pinned_by_digest() -> None:
    args = _args()
    bases = re.findall(r"^FROM (\S+)", DOCKERFILE, flags=re.MULTILINE)
    assert bases, "no FROM lines"
    for base in bases:
        image = args.get(base.removeprefix("${").removesuffix("}"), base)
        if image in {"uv", "builder"}:
            continue
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), f"{base} is not pinned by digest"


def test_runtime_stage_is_non_root_labelled_and_runs_the_cli() -> None:
    runtime = DOCKERFILE.rsplit("\nFROM ", 1)[1]
    users = re.findall(r"^USER (\S+)$", runtime, flags=re.MULTILINE)
    assert users
    assert users[-1] not in {"root", "0"}
    assert "LABEL project=reporewind" in runtime
    assert 'ENTRYPOINT ["reporewind"]' in runtime


def test_build_context_includes_everything_the_image_copies() -> None:
    allowed = {
        line.removeprefix("!").rstrip("/")
        for line in (ROOT / ".dockerignore").read_text(encoding="ascii").splitlines()
        if line.startswith("!")
    }
    copied: set[str] = set()
    for line in re.findall(r"^COPY (?!--from)(.+)$", DOCKERFILE, flags=re.MULTILINE):
        copied.update(part.rstrip("/") for part in line.split()[:-1])
    assert copied
    assert copied <= allowed
    for name in copied:
        assert (ROOT / name).exists()
