"""The bundled demo: its sample data is reproducible and ``demo/run.sh`` passes offline."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

DEMO = Path(__file__).resolve().parent.parent / "demo"


def _load_make_sample() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_sample", DEMO / "make_sample.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_stream_matches_generator(tmp_path: Path) -> None:
    make_sample = _load_make_sample()
    regenerated = make_sample.export_stream(make_sample.build(tmp_path))
    assert regenerated == (DEMO / "slugkit.fi").read_bytes(), (
        "demo/slugkit.fi is stale: run `uv run python demo/make_sample.py`"
    )
    assert regenerated.isascii()


def test_sample_history_has_pinned_shas(tmp_path: Path) -> None:
    make_sample = _load_make_sample()
    git = make_sample.Git(make_sample.build(tmp_path))
    assert git.rev_parse("fix-separators").startswith("cd84329fdd59")
    assert git.rev_parse("merge-unicode").startswith("1cd73e1f716d")
    assert git.rev_parse("refactor-only").startswith("61a502bf56cf")


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_demo_script_runs_end_to_end(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "REPOREWIND": f"{sys.executable} -m reporewind",
        "PYTHON": sys.executable,
        "REPOREWIND_DEMO_WORK": str(tmp_path / "work"),
    }
    result = subprocess.run(
        ["sh", str(DEMO / "run.sh")],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "fix cd84329fdd59 (fix: collapse runs of separators in slugify)" in output
    assert "base    1c2c60f7c97c (parent 1 of 2)" in output
    assert "exit code 10 (resolve error), as expected" in output
    assert "base + test.patch:             FAILED (failures=1)" in output
    assert "base + fix.patch + test.patch: OK" in output
    assert (tmp_path / "work" / "fix-separators.json").is_file()
    assert (tmp_path / "work" / "merge-unicode.json").is_file()
