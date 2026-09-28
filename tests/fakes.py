"""A scripted CommandRunner for tests that must not run uv or docker."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from reporewind.errors import ToolNotFoundError
from reporewind.proc import CommandResult

Handler = Callable[[tuple[str, ...], Path | None], CommandResult]


@dataclass
class Call:
    argv: tuple[str, ...]
    cwd: Path | None
    env: Mapping[str, str] | None
    inputs: dict[str, str] = field(default_factory=dict)


class FakeRunner:
    """Answers commands by their first two words; records every call.

    A handler may be a CommandResult, a callable or an exception to raise.
    Staged input files under ``cwd`` are captured before the scratch dir goes.
    """

    def __init__(self, handlers: Mapping[str, object] | None = None) -> None:
        self.handlers = dict(handlers or {})
        self.calls: list[Call] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        key = " ".join(argv[:2])
        call = Call(tuple(argv), cwd, env)
        if cwd is not None:
            for path in sorted(cwd.rglob("*")):
                if path.is_file():
                    call.inputs[path.relative_to(cwd).as_posix()] = path.read_text()
        self.calls.append(call)
        handler = self.handlers.get(key)
        if handler is None:
            raise ToolNotFoundError(argv[0])
        if isinstance(handler, BaseException):
            raise handler
        if isinstance(handler, CommandResult):
            return handler
        assert callable(handler)
        result: CommandResult = handler(tuple(argv), cwd)
        return result


def ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult((), 0, stdout.encode(), stderr.encode())


def failed(stderr: str, code: int = 1) -> CommandResult:
    return CommandResult((), code, b"", stderr.encode())
