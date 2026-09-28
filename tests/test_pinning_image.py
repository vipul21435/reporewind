import json

import pytest

from fakes import FakeRunner, failed, ok
from reporewind.errors import CommandTimeoutError, PinError
from reporewind.pinning.image import (
    FALLBACK_DIGESTS,
    BaseImage,
    fallback_image,
    image_tag,
    inspect_digest,
    is_offline,
    resolve_base_image,
)
from reporewind.pinning.python import supported_versions

DIGEST = "sha256:" + "ab" * 32


def manifest(digest: str = DIGEST) -> str:
    return json.dumps({"schemaVersion": 2, "digest": digest, "manifests": []})


def test_fallback_table_covers_every_supported_version() -> None:
    assert tuple(FALLBACK_DIGESTS) == supported_versions()
    for version in supported_versions():
        image = fallback_image(version)
        assert image.tag == f"python:{version}-slim"
        assert image.source == "fallback"


def test_fallback_matches_the_digest_pinned_in_the_cli_image() -> None:
    from pathlib import Path

    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
    assert f"python:3.12-slim@{FALLBACK_DIGESTS['3.12']}" in dockerfile


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("TRUE", True), (" yes ", True), ("on", True), ("0", False), ("", False)],
)
def test_is_offline(value: str, expected: bool) -> None:
    assert is_offline({"REPOREWIND_OFFLINE": value}) is expected
    assert is_offline({}) is False


def test_registry_digest() -> None:
    runner = FakeRunner({"docker buildx": ok(manifest())})
    image, notes = resolve_base_image("3.9", runner)
    assert image == BaseImage(tag="python:3.9-slim", digest=DIGEST, source="registry")
    assert image.reference == f"python:3.9-slim@{DIGEST}"
    assert notes == ()
    assert runner.calls[0].argv == (
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        "python:3.9-slim",
        "--format",
        "{{json .Manifest}}",
    )


def test_offline_uses_the_table_without_running_docker() -> None:
    runner = FakeRunner()
    image, notes = resolve_base_image("3.10", runner, offline=True)
    assert runner.calls == []
    assert image.digest == FALLBACK_DIGESTS["3.10"]
    assert notes == (
        "REPOREWIND_OFFLINE: python:3.10-slim digest from the offline table recorded on 2026-09-29",
    )


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (None, "required tool 'docker' was not found on PATH"),
        (failed("ERROR: failed to authorize\nERROR: no network\n"), "ERROR: no network"),
        (failed(""), "command exited with status 1: docker buildx imagetools inspect"),
        (ok("not json"), "unexpected output from docker buildx imagetools inspect"),
        (ok(manifest("sha256:short")), "python:3.11-slim resolved to an invalid digest"),
        (CommandTimeoutError(["docker"], 120.0), "command timed out after 120s: docker"),
    ],
)
def test_failed_lookup_falls_back_with_a_note(handler: object, reason: str) -> None:
    runner = FakeRunner({} if handler is None else {"docker buildx": handler})
    image, notes = resolve_base_image("3.11", runner)
    assert image.source == "fallback"
    (note,) = notes
    assert note.startswith(f"registry lookup failed ({reason}")
    assert note.endswith("python:3.11-slim digest from the offline table recorded on 2026-09-29")


def test_unknown_version_without_registry_is_an_error() -> None:
    with pytest.raises(PinError, match=r"no recorded digest for python:3\.6-slim"):
        resolve_base_image("3.6", FakeRunner(), offline=True)


def test_inspect_digest_raises_on_failure() -> None:
    with pytest.raises(PinError, match="unexpected output"):
        inspect_digest("python:3.9-slim", FakeRunner({"docker buildx": ok("{}")}))


def test_image_tag() -> None:
    assert image_tag("3.13") == "python:3.13-slim"


@pytest.mark.e2e
def test_real_registry_lookup_returns_a_digest() -> None:
    from reporewind.proc import SubprocessRunner

    image, notes = resolve_base_image("3.12", SubprocessRunner())
    assert notes == ()
    assert image.source == "registry"
    assert image.digest.startswith("sha256:")
