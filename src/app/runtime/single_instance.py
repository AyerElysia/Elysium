"""Process-level single-instance guard for the Elysium runtime."""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO


class AlreadyRunningError(RuntimeError):
    """Raised when another Elysium process already owns the runtime lock."""


class SingleInstanceLock:
    """Hold an operating-system file lock for one Elysium process lifetime.

    The lock is released by the kernel when the process exits, including
    crashes.  The PID written into the file is diagnostic only; ownership is
    decided exclusively by the OS lock, so a stale file never blocks startup.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        """Acquire the lock without waiting.

        Raises:
            AlreadyRunningError: If another process already owns the lock.
        """
        if self._handle is not None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        handle.seek(0)
        owner = handle.read().strip()
        try:
            self._lock_handle(handle)
        except OSError as exc:
            handle.close()
            owner_hint = f" (PID {owner})" if owner.isdigit() else ""
            raise AlreadyRunningError(
                f"Elysium 已在运行{owner_hint}；请先退出已有实例后再启动"
            ) from exc

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        """Release the lock if this object owns it."""
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            self._unlock_handle(handle)
        finally:
            handle.close()

    @staticmethod
    def _lock_handle(handle: IO[str]) -> None:
        """Acquire the platform-specific non-blocking file lock."""
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_handle(handle: IO[str]) -> None:
        """Release the platform-specific file lock."""
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def __enter__(self) -> "SingleInstanceLock":
        """Acquire and return this lock."""
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        """Release this lock when leaving the process scope."""
        self.release()


__all__ = ["AlreadyRunningError", "SingleInstanceLock"]
