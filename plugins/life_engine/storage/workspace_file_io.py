"""Rooted, byte-exact workspace projection I/O with recoverable filesystem races.

These helpers enforce mechanical byte/path boundaries, not document ownership or
meaning. The caller must hold its authority/binding revision fence. Linux rooted
directory descriptors, O_TMPFILE and renameat2(RENAME_NOREPLACE) are required;
unsupported platforms fail closed. A same-UID/privileged administrator modifying
private quarantine directories is outside this protocol's security boundary.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import secrets
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Self
from weakref import WeakKeyDictionary

DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_IO_BUDGETS: WeakKeyDictionary = WeakKeyDictionary()


class WorkspaceFileError(RuntimeError):
    """Content-free, explicit filesystem failure."""


class WorkspaceFileConflict(WorkspaceFileError):
    """Unknown/current bytes were not authorized for replacement or deletion."""


class WorkspaceFileRecoveryRequired(WorkspaceFileError):
    """An actual moved inode is preserved and must not be automatically deleted."""

    def __init__(self, logical_path: str, recovery_path: Path, *, reason: str) -> None:
        self.logical_path = logical_path
        self.recovery_path = str(recovery_path)
        self.reason = reason
        super().__init__(
            f"recovery_required: {reason}; logical_path={logical_path}; "
            f"recovery_path={recovery_path}"
        )


async def run_workspace_file_io(callback: Any, *args: Any, **kwargs: Any) -> Any:
    """Run at most four owned disk workers per loop; cancellation joins its worker.

    Cancellation cannot roll back an already-published filesystem operation.
    Awaiting the worker before propagating cancellation keeps descriptors alive
    until the operation's own cleanup/recovery protocol has finished.
    """
    loop = asyncio.get_running_loop()
    budget = _IO_BUDGETS.setdefault(loop, asyncio.Semaphore(4))
    async with budget:
        worker = asyncio.create_task(
            asyncio.to_thread(callback, *args, **kwargs),
            name="subject-workspace-owned-io",
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancellation:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001 - arbitrary worker failure cannot bypass join
                    break
            if worker.done() and not worker.cancelled():
                error = worker.exception()
                if error is not None:
                    raise cancellation from error
            raise


def _rename_no_replace(
    source_fd: int, source: str, target_fd: int, target: str
) -> None:
    """Atomically move an entry, including symlinks/directories, without clobber."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise WorkspaceFileError("secure_workspace_io_unavailable: renameat2")
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _normalize_path(value: str) -> str:
    raw = str(value)
    parts = raw.split("/")
    if (
        not raw
        or "\\" in raw
        or "\x00" in raw
        or PurePosixPath(raw).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise WorkspaceFileError("invalid_workspace_logical_path")
    return "/".join(parts)


def _validate_hash(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in value)
    ):
        raise WorkspaceFileError("invalid_expected_sha256")
    try:
        int(value, 16)
    except ValueError as exc:
        raise WorkspaceFileError("invalid_expected_sha256") from exc
    return value.lower()


class _AnchoredTarget:
    """Hold a trusted root and every no-follow descendant directory open."""

    def __init__(self, data_root: Path, logical_path: str, *, create: bool) -> None:
        self.root = Path(os.path.abspath(data_root))
        self.logical_path = _normalize_path(logical_path)
        self.parts = self.logical_path.split("/")
        self.name = self.parts[-1]
        self.path = self.root / self.logical_path
        self.fds: list[int] = []
        self.edges: list[tuple[int, str, int]] = []
        if (
            os.name != "posix"
            or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_TMPFILE")
            or os.open not in os.supports_dir_fd
            or os.link not in os.supports_dir_fd
            or not Path("/proc/self/fd").is_dir()
        ):
            raise WorkspaceFileError("secure_workspace_io_unavailable")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            parent = os.open(self.root, flags)
            self.fds.append(parent)
            for part in self.parts[:-1]:
                try:
                    child = os.open(part, flags, dir_fd=parent)
                except FileNotFoundError:
                    if not create:
                        raise
                    self.assert_attached()
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                    child = os.open(part, flags, dir_fd=parent)
                    os.fsync(parent)
                self.fds.append(child)
                self.edges.append((parent, part, child))
                parent = child
            self.parent_fd = parent
            self.assert_attached()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def assert_attached(self) -> None:
        if _identity(self.root.lstat()) != _identity(os.fstat(self.fds[0])):
            raise WorkspaceFileConflict("workspace_root_changed")
        for parent, name, child in self.edges:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or _identity(info) != _identity(
                os.fstat(child)
            ):
                raise WorkspaceFileConflict("workspace_parent_changed")

    def close(self) -> None:
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()


def _read_regular(
    parent_fd: int, name: str, *, max_bytes: int, collect: bool = False
) -> tuple[str, bytes, os.stat_result]:
    if max_bytes < 0:
        raise WorkspaceFileError("invalid_workspace_byte_limit")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise WorkspaceFileConflict("workspace_target_not_regular_file")
        if before.st_size > max_bytes:
            raise WorkspaceFileError("workspace_file_byte_limit_exceeded")
        digest = hashlib.sha256()
        output = bytearray()
        total = 0
        while chunk := os.read(fd, _CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                raise WorkspaceFileError("workspace_file_byte_limit_exceeded")
            digest.update(chunk)
            if collect:
                output.extend(chunk)
        after = os.fstat(fd)
        marker = lambda info: (
            _identity(info),
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        if marker(before) != marker(after):
            raise WorkspaceFileConflict("workspace_file_changed_while_reading")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(current) != _identity(after):
            raise WorkspaceFileConflict("workspace_target_changed_while_reading")
        return digest.hexdigest(), bytes(output), after
    finally:
        os.close(fd)


class _Quarantine:
    """Preserve the actual atomically moved entry until checked or safely restored."""

    def __init__(self, target: _AnchoredTarget) -> None:
        self.target = target
        self.name = f".elysium-quarantine-{secrets.token_hex(16)}"
        self.entry = "original"
        self.moved = False
        target.assert_attached()
        os.mkdir(self.name, mode=0o700, dir_fd=target.parent_fd)
        descriptor: int | None = None
        created = os.stat(self.name, dir_fd=target.parent_fd, follow_symlinks=False)
        try:
            descriptor = os.open(
                self.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=target.parent_fd,
            )
            self.fd = descriptor
            os.fsync(target.parent_fd)
        except BaseException:
            try:
                current = os.stat(
                    self.name, dir_fd=target.parent_fd, follow_symlinks=False
                )
                if _identity(current) != _identity(created):
                    raise WorkspaceFileConflict(
                        "quarantine_initialization_path_changed"
                    )
                os.rmdir(self.name, dir_fd=target.parent_fd)
            except (OSError, WorkspaceFileError) as cleanup_error:
                raise WorkspaceFileRecoveryRequired(
                    target.logical_path,
                    target.path.parent / self.name,
                    reason="quarantine_initialization_incomplete",
                ) from cleanup_error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            raise

    @property
    def recovery_path(self) -> Path:
        # A renamed ancestor may have moved. Resolve this owned fd for diagnosis
        # only; never use the resulting path for mutation.
        try:
            location = Path(os.readlink(f"/proc/self/fd/{self.fd}"))
        except OSError:
            location = self.target.path.parent / self.name
        return location / self.entry

    def take(self) -> None:
        self.target.assert_attached()
        _rename_no_replace(self.target.parent_fd, self.target.name, self.fd, self.entry)
        self.moved = True
        os.fsync(self.fd)
        os.fsync(self.target.parent_fd)

    def restore(self, reason: str) -> None:
        """Restore even an unknown symlink/directory without following or replacing it."""
        if not self.moved:
            return
        try:
            self.target.assert_attached()
            _rename_no_replace(
                self.fd, self.entry, self.target.parent_fd, self.target.name
            )
        except (OSError, WorkspaceFileError) as exc:
            raise WorkspaceFileRecoveryRequired(
                self.target.logical_path, self.recovery_path, reason=reason
            ) from exc
        self.moved = False
        try:
            os.fsync(self.target.parent_fd)
        except OSError as exc:
            # The inode is already back at the original path. Do not issue a
            # recovery reference to a quarantine entry that no longer exists.
            raise WorkspaceFileError(
                f"workspace_restored_durability_unconfirmed: {self.target.logical_path}"
            ) from exc

    def discard_verified(self) -> None:
        """Only called for an exact verified inode inside our private directory."""
        os.unlink(self.entry, dir_fd=self.fd)
        self.moved = False
        try:
            os.fsync(self.fd)
        except OSError as exc:
            raise WorkspaceFileError(
                "workspace_verified_removal_durability_unconfirmed"
            ) from exc

    def close(self) -> None:
        try:
            if not self.moved:
                current = os.stat(
                    self.name, dir_fd=self.target.parent_fd, follow_symlinks=False
                )
                if _identity(current) == _identity(os.fstat(self.fd)):
                    os.rmdir(self.name, dir_fd=self.target.parent_fd)
                    os.fsync(self.target.parent_fd)
        finally:
            os.close(self.fd)


def _new_inode(target: _AnchoredTarget, content: bytes) -> int:
    target.assert_attached()
    fd = os.open(".", os.O_TMPFILE | os.O_RDWR, 0o600, dir_fd=target.parent_fd)
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view[:_CHUNK_BYTES])
            if written <= 0:
                raise WorkspaceFileError("workspace_write_made_no_progress")
            view = view[written:]
        os.fsync(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _publish(target: _AnchoredTarget, fd: int) -> None:
    target.assert_attached()
    os.link(
        f"/proc/self/fd/{fd}",
        target.name,
        dst_dir_fd=target.parent_fd,
        follow_symlinks=True,
    )
    try:
        os.fsync(target.parent_fd)
    except OSError as exc:
        raise WorkspaceFileError(
            f"workspace_published_durability_unconfirmed: {target.logical_path}"
        ) from exc


def read_exact_bytes(
    data_root: Path,
    logical_path: str,
    *,
    expected_hash: str | None = None,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> bytes:
    """Read complete, stable regular-file bytes; a limit violation never truncates."""
    expected_hash = _validate_hash(expected_hash) if expected_hash is not None else None
    with _AnchoredTarget(data_root, logical_path, create=False) as target:
        digest, content, _ = _read_regular(
            target.parent_fd, target.name, max_bytes=max_bytes, collect=True
        )
        target.assert_attached()
        if expected_hash is not None and digest != expected_hash:
            raise WorkspaceFileConflict("workspace_content_hash_mismatch")
        return content


def project_exact_bytes(
    data_root: Path,
    logical_path: str,
    content: bytes,
    *,
    expected_parent_hash: str | None = None,
    allow_equal_existing: bool = True,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> None:
    """Create without overwrite; replace only the exact expected predecessor bytes.

    Re-projecting already-equal bytes is an idempotent no-op by default. Caches
    must disable that shortcut when equality cannot prove inode ownership.
    Unknown bytes are
    preserved. If a race prevents no-overwrite restoration, recovery_required
    names the retained quarantine instead of deleting or hiding those bytes.
    """
    if not isinstance(content, bytes) or len(content) > max_bytes:
        raise WorkspaceFileError("workspace_content_type_or_byte_limit_invalid")
    expected = (
        _validate_hash(expected_parent_hash)
        if expected_parent_hash is not None
        else None
    )
    desired = hashlib.sha256(content).hexdigest()
    with _AnchoredTarget(data_root, logical_path, create=True) as target:
        try:
            current, _, _ = _read_regular(
                target.parent_fd, target.name, max_bytes=max_bytes
            )
        except FileNotFoundError:
            current = None
        if allow_equal_existing and current == desired:
            target.assert_attached()
            return
        if current is not None and (expected is None or current != expected):
            raise WorkspaceFileConflict("workspace_predecessor_hash_mismatch")
        new_fd = _new_inode(target, content)
        quarantine: _Quarantine | None = None
        try:
            if current is not None:
                quarantine = _Quarantine(target)
                quarantine.take()
                moved_hash, _, info = _read_regular(
                    quarantine.fd, quarantine.entry, max_bytes=max_bytes
                )
                if moved_hash != expected:
                    raise WorkspaceFileConflict(
                        "workspace_moved_predecessor_hash_mismatch"
                    )
                os.fchmod(new_fd, stat.S_IMODE(info.st_mode) & 0o777)
                os.fsync(new_fd)
            _publish(target, new_fd)
            if quarantine is not None:
                quarantine.discard_verified()
        except BaseException as exc:
            if quarantine is not None and quarantine.moved:
                quarantine.restore(type(exc).__name__)
            raise
        finally:
            os.close(new_fd)
            if quarantine is not None:
                quarantine.close()


def remove_exact_bytes(
    data_root: Path,
    logical_path: str,
    *,
    expected_hash: str,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> None:
    """Remove only exact expected projection bytes; absence is idempotent success."""
    expected = _validate_hash(expected_hash)
    try:
        target = _AnchoredTarget(data_root, logical_path, create=False)
    except FileNotFoundError:
        return
    with target:
        try:
            current, _, _ = _read_regular(
                target.parent_fd, target.name, max_bytes=max_bytes
            )
        except FileNotFoundError:
            return
        if current != expected:
            raise WorkspaceFileConflict("workspace_delete_hash_mismatch")
        quarantine = _Quarantine(target)
        try:
            quarantine.take()
            moved_hash, _, _ = _read_regular(
                quarantine.fd, quarantine.entry, max_bytes=max_bytes
            )
            if moved_hash != expected:
                raise WorkspaceFileConflict("workspace_moved_delete_hash_mismatch")
            target.assert_attached()
            try:
                os.stat(target.name, dir_fd=target.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise WorkspaceFileConflict("workspace_delete_target_recreated")
            quarantine.discard_verified()
        except BaseException as exc:
            if quarantine.moved:
                quarantine.restore(type(exc).__name__)
            raise
        finally:
            quarantine.close()
