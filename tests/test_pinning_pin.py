import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakes import FakeRunner, ok
from reporewind.errors import PinError
from reporewind.models import CommitInfo, RepoRef
from reporewind.pinning.image import FALLBACK_DIGESTS
from reporewind.pinning.pin import (
    PIN_FILE,
    PinOptions,
    PinResult,
    dump_pin_result,
    load_pin_result,
    pin_commit,
    write_pin,
)
from reporewind.recipes.model import Recipe, recipe_hash
from reporewind.recipes.source import MemoryTreeSource

DIGEST = "sha256:" + "cd" * 32
SHA = "a" * 40
LOCK = "attrs==21.2.0\n    # via -r requirements.in\nclick==8.0.1\npytest==6.2.4\n"
PYPROJECT = """
[project]
name = "demo"
requires-python = ">=3.7"
classifiers = [
    "Programming Language :: Python :: 3.7",
    "Programming Language :: Python :: 3.8",
]
dependencies = ["click>=7"]

[project.optional-dependencies]
yaml = ["pyyaml"]
"""


def commit(when: datetime = datetime(2021, 6, 1, 12, tzinfo=UTC)) -> CommitInfo:
    return CommitInfo(sha=SHA, author_date=when, committer_date=when, subject="base")


def runner() -> FakeRunner:
    return FakeRunner(
        {
            "docker buildx": ok(json.dumps({"digest": DIGEST})),
            "uv pip": ok(LOCK),
        }
    )


def test_pin_commit_combines_python_image_and_lock() -> None:
    tree = MemoryTreeSource({"pyproject.toml": PYPROJECT, "req.txt": "attrs\n"})
    recipe = Recipe(
        python=">=3.7",
        extras=("yaml",),
        requirements_files=("req.txt",),
        test_dependencies=("pytest", "click>=7"),
    )
    fake = runner()
    result, lock_text = pin_commit(
        tree, RepoRef.parse("example/demo"), commit(), recipe, fake, notes=("given",)
    )
    assert result.python.version == "3.8"
    assert result.python.reason.startswith("newest classifier version")
    assert result.base_image.reference == f"python:3.8-slim@{DIGEST}"
    assert result.requirements == ("click>=7", "pyyaml", "pytest")
    assert result.requirements_files == ("req.txt",)
    assert result.packages == ("attrs==21.2.0", "click==8.0.1", "pytest==6.2.4")
    assert result.exclude_newer == commit().committer_date
    assert result.recipe_hash == recipe_hash(recipe)
    assert result.notes == ("given",)
    assert lock_text.endswith(LOCK)
    uv_call = fake.calls[1]
    assert uv_call.argv[uv_call.argv.index("--python-version") + 1] == "3.8"
    assert uv_call.argv[uv_call.argv.index("--exclude-newer") + 1] == "2021-06-01T12:00:00Z"


def test_options_override_the_inferred_values() -> None:
    fake = runner()
    cutoff = datetime(2020, 1, 1, tzinfo=UTC)
    result, _ = pin_commit(
        MemoryTreeSource({"pyproject.toml": PYPROJECT}),
        RepoRef.parse("example/demo"),
        commit(),
        Recipe(install="none", test_dependencies=("pytest",)),
        fake,
        PinOptions(
            python="3.7",
            exclude_newer=cutoff,
            platform="linux",
            generate_hashes=False,
            offline=True,
        ),
    )
    assert result.python.version == "3.7"
    assert result.base_image.digest == FALLBACK_DIGESTS["3.7"]
    assert result.base_image.source == "fallback"
    assert result.requirements == ("pytest",)  # install: none skips the project's own deps
    assert result.exclude_newer == cutoff
    assert result.platform == "linux"
    (uv_call,) = fake.calls
    assert "--offline" in uv_call.argv
    assert "--generate-hashes" not in uv_call.argv
    assert result.notes[0].startswith("REPOREWIND_OFFLINE: python:3.7-slim digest")


def test_write_and_load_round_trip(tmp_path: Path) -> None:
    result, lock_text = pin_commit(
        MemoryTreeSource({"pyproject.toml": PYPROJECT}),
        RepoRef.parse("example/demo"),
        commit(),
        Recipe(),
        runner(),
    )
    lock_path, pin_path = write_pin(result, lock_text, tmp_path / "out")
    assert lock_path.read_text() == lock_text
    assert pin_path.name == PIN_FILE
    assert oct(pin_path.stat().st_mode & 0o777) == "0o644"
    assert pin_path.read_text() == dump_pin_result(result)
    assert pin_path.read_text().isascii()
    assert load_pin_result(pin_path) == result
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [PIN_FILE, "requirements.lock"]


def test_load_rejects_invalid_files(tmp_path: Path) -> None:
    bad = tmp_path / PIN_FILE
    bad.write_text('{"schema_version": 1}')
    with pytest.raises(PinError, match="is not a valid pin result"):
        load_pin_result(bad)
    with pytest.raises(PinError, match="is not a valid pin result"):
        load_pin_result(tmp_path / "missing.json")


def test_pin_result_rejects_unknown_fields() -> None:
    result, _ = pin_commit(
        MemoryTreeSource({}), RepoRef.parse("example/demo"), commit(), Recipe(), runner()
    )
    data = json.loads(dump_pin_result(result))
    data["extra"] = 1
    with pytest.raises(ValueError, match="extra"):
        PinResult.model_validate(data)
