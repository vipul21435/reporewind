"""A cross-process build lock on a file, with a timeout and stale-lock recovery.

The lock is an exclusive ``flock`` on ``<name>.lock``. The kernel drops it
when the holder exits for any reason, so a lock file left behind by a
crashed or killed builder is *stale* and is simply taken over: nothing has
to guess whether a recorded PID is still alive or reused. After acquiring,
the holder writes its PID, host and start time into the file so a waiter
that times out can say who holds the lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from reporewind.errors import BuildError

POLL_INTERVAL: Final = 0.05


class BuildLockTimeoutError(BuildError):
    """Another process held the build lock for longer than the timeout."""


class BuildLock:
    """``with BuildLock(path, timeout=...):`` runs the block holding ``path`` exclusively."""

    def __init__(self, path: Path, *, timeout: float = 600.0) -> None:
        self.path = path
        self.timeout = timeout
        self._fd: int | None = None
        self.recovered_stale = False
        """True if the lock file existed with a holder that was no longer running."""

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            raise BuildError(f"{self.path} is already held by this lock object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BuildLockTimeoutError(
                            f"timed out after {self.timeout:g}s waiting for {self.path}"
                            f" (held by {self._describe_holder(fd)})"
                        ) from None
                    time.sleep(POLL_INTERVAL)
            self.recovered_stale = bool(os.fstat(fd).st_size)
            holder = {"pid": os.getpid(), "host": socket.gethostname(), "since": time.time()}
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps(holder, sort_keys=True).encode("ascii"), 0)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)  # an empty file means "released cleanly"
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @staticmethod
    def _describe_holder(fd: int) -> str:
        try:
            data = json.loads(os.pread(fd, 4096, 0) or b"{}")
            return f"pid {data['pid']} on {data['host']}"
        except (OSError, ValueError, KeyError, TypeError):
            return "an unknown process"

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


__all__ = ["POLL_INTERVAL", "BuildLock", "BuildLockTimeoutError"]
