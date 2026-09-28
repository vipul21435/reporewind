"""Run external commands (git, uv, docker) without a shell.

Stages talk to external tools through the :class:`CommandRunner` protocol so
unit tests can substitute a fake. The real :class:`SubprocessRunner` always
passes an argument list (never ``shell=True``), never lets a child read the
terminal, and turns the two failure modes that are not exit statuses (missing
executable, timeout) into typed errors. Non-zero exits are returned, not
raised, so each adapter decides which statuses are errors.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from reporewind.errors import CommandTimeoutError, ToolNotFoundError


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Exit status and raw captured output of one finished command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def stdout_text(self) -> str:
        """UTF-8 stdout; undecodable bytes survive a round trip via ``surrogateescape``."""
        return self.stdout.decode("utf-8", "surrogateescape")

    @property
    def stderr_text(self) -> str:
        """UTF-8 stderr for humans; undecodable bytes are replaced."""
        return self.stderr.decode("utf-8", "replace")


class CommandRunner(Protocol):
    """Anything that can run an argument vector and report its result."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run ``argv`` to completion; raise only for a missing tool or a timeout."""
        ...


class SubprocessRunner:
    """:class:`CommandRunner` backed by :func:`subprocess.run`."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        if not argv:
            raise ValueError("argv must name a command")
        search_path = env.get("PATH") if env is not None else None
        executable = shutil.which(argv[0], path=search_path)
        if executable is None:
            raise ToolNotFoundError(argv[0])
        command = [executable, *argv[1:]]
        try:
            # An empty stdin (never the inherited terminal) means a child that
            # unexpectedly prompts sees EOF instead of hanging the pipeline.
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=None if env is None else dict(env),
                input=b"" if stdin is None else stdin,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeoutError(
                argv,
                exc.timeout,
                _decode(exc.stdout),
                _decode(exc.stderr),
            ) from exc
        return CommandResult(tuple(argv), completed.returncode, completed.stdout, completed.stderr)


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", "replace")


__all__ = ["CommandResult", "CommandRunner", "SubprocessRunner"]
