import os
import sys
from pathlib import Path

import pytest

from reporewind.errors import CommandTimeoutError, ToolNotFoundError
from reporewind.proc import CommandResult, SubprocessRunner

runner = SubprocessRunner()
PY = sys.executable


def test_captures_status_and_both_streams() -> None:
    code = "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(3)"
    result = runner.run([PY, "-c", code])
    assert result.returncode == 3
    assert not result.ok
    assert result.stdout == b"out"
    assert result.stderr_text == "err"
    assert result.argv == (PY, "-c", code)


def test_stdin_is_forwarded_and_defaults_to_empty() -> None:
    echo = [PY, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
    assert runner.run(echo, stdin=b"patch bytes").stdout == b"patch bytes"
    assert runner.run(echo).stdout == b""


def test_cwd_and_env_are_applied(tmp_path: Path) -> None:
    code = "import os; print(os.getcwd()); print(os.environ['RR_PROBE'])"
    env = {"RR_PROBE": "42", "PATH": os.environ["PATH"]}
    cwd_line, probe = runner.run([PY, "-c", code], cwd=tmp_path, env=env).stdout_text.split()
    assert Path(cwd_line).resolve() == tmp_path.resolve()
    assert probe == "42"


def test_shell_metacharacters_are_passed_through_literally(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    payload = f"x; touch {marker} $(touch {marker})"
    result = runner.run([PY, "-c", "import sys; print(sys.argv[1])", payload])
    assert result.stdout_text.strip() == payload
    assert not marker.exists()


def test_missing_tool_raises_typed_error() -> None:
    with pytest.raises(ToolNotFoundError) as exc:
        runner.run(["reporewind-no-such-tool", "--help"])
    assert exc.value.tool == "reporewind-no-such-tool"


def test_tool_lookup_uses_the_path_of_the_given_env(tmp_path: Path) -> None:
    with pytest.raises(ToolNotFoundError):
        runner.run(["git", "--version"], env={"PATH": str(tmp_path)})


def test_timeout_raises_with_partial_output() -> None:
    code = "import time; print('started', flush=True); time.sleep(30)"
    with pytest.raises(CommandTimeoutError) as exc:
        runner.run([PY, "-c", code], timeout=1.0)
    assert exc.value.timeout == 1.0
    assert "started" in exc.value.stdout


def test_empty_argv_is_rejected() -> None:
    with pytest.raises(ValueError, match="argv"):
        runner.run([])


def test_stdout_text_round_trips_undecodable_bytes() -> None:
    result = CommandResult(("x",), 0, b"caf\xe9\n", b"bad \xff")
    assert result.stdout_text.encode("utf-8", "surrogateescape") == b"caf\xe9\n"
    assert result.stderr_text == "bad \ufffd"
    assert result.ok
