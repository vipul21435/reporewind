import runpy
import sys

import pytest
from typer.testing import CliRunner

from reporewind import __version__
from reporewind.cli import app

runner = CliRunner()


def test_version_flag_prints_package_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"reporewind {__version__}"


def test_version_command_prints_bare_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_version_matches_pyproject() -> None:
    assert __version__ == "0.1.0"


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output


def test_module_entry_point_runs_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["reporewind", "--version"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("reporewind", run_name="__main__")
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"reporewind {__version__}"
