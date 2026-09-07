"""Bounded, metadata-only current view over authority bindings and disk.

Disk is scanned without following symlinks, in an owned offloaded worker.
Every registered path (including a released binding) shadows its stale disk
entry. Only exact current-version descriptors are added from authority; this
module never imports disk bytes, reads blobs, or mutates either surface.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ..storage.workspace_file_io import run_workspace_file_io

if TYPE_CHECKING:
    from .managed_files import ManagedFileSession

_PREFIX = "life_engine_workspace/"
_MAX_INVENTORY_ENTRIES = 10_000
_PAGE_SIZE = 500
_IGNORE_DIRS = {".memory", "__pycache__", ".git", ".svn", "node_modules"}


class ManagedFileInventoryError(RuntimeError):
    """An incomplete or unsafe current view cannot be returned as a full list."""


class _RootNotDirectory(ManagedFileInventoryError):
    """A physical file can be superseded only by actual bound descendants."""


def _visible(parts: tuple[str, ...], include_hidden: bool) -> bool:
    return include_hidden or not any(
        part.startswith(".") or part in _IGNORE_DIRS for part in parts
    )


def _size_human(size: int) -> str:
    amount = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024:
            return f"{size}B" if unit == "B" else f"{amount:.1f}{unit}"
        amount /= 1024
    return f"{amount:.1f}TB"


def _item(
    path: str, *, directory: bool, size: int, mtime: float, authority: str
) -> dict[str, Any]:
    return {
        "path": path,
        "name": PurePosixPath(path).name,
        "type": "directory" if directory else "file",
        "size": None if directory else size,
        "size_human": None if directory else _size_human(size),
        "modified_at": datetime.fromtimestamp(mtime, tz=UTC).astimezone().isoformat(),
        "_mtime": mtime,
        "source_authority": authority,
    }


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


@contextmanager
def _directory_anchor(
    workspace: Path, parts: tuple[str, ...]
) -> Iterator[tuple[int, Any]]:
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
    ):
        raise ManagedFileInventoryError("secure_workspace_inventory_unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    edges: list[tuple[int, str, int]] = []
    try:
        if stat.S_ISLNK(workspace.lstat().st_mode):
            raise ManagedFileInventoryError("workspace_inventory_symlink_root_rejected")
        root_fd = os.open(workspace, flags)
        descriptors.append(root_fd)
        current = root_fd
        for part in parts:
            info = os.stat(part, dir_fd=current, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise ManagedFileInventoryError(
                    "workspace_inventory_symlink_root_rejected"
                )
            if not stat.S_ISDIR(info.st_mode):
                raise NotADirectoryError("workspace_inventory_root_not_directory")
            child = os.open(part, flags, dir_fd=current)
            descriptors.append(child)
            edges.append((current, part, child))
            current = child

        def attached() -> None:
            if _identity(workspace.lstat()) != _identity(os.fstat(root_fd)):
                raise ManagedFileInventoryError("workspace_inventory_root_changed")
            for parent, name, child in edges:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode) or _identity(info) != _identity(
                    os.fstat(child)
                ):
                    raise ManagedFileInventoryError(
                        "workspace_inventory_directory_changed"
                    )

        attached()
        yield current, attached
        attached()
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _scan_disk(
    workspace: Path,
    root_parts: tuple[str, ...],
    max_depth: int,
    include_hidden: bool,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    visited = 0
    pending = [root_parts]
    while pending:
        parts = pending.pop()
        anchored = False
        try:
            with _directory_anchor(workspace, parts) as (descriptor, attached):
                anchored = True
                before = os.fstat(descriptor)
                if not _visible(parts, include_hidden):
                    continue
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > limit:
                            raise ManagedFileInventoryError(
                                "workspace_inventory_budget_exceeded"
                            )
                        entry_parts = (*parts, entry.name)
                        if not _visible(entry_parts, include_hidden):
                            continue
                        attached()
                        info = entry.stat(follow_symlinks=False)
                        directory = stat.S_ISDIR(info.st_mode)
                        if not directory and not stat.S_ISREG(info.st_mode):
                            continue
                        rows.append(
                            _item(
                                "/".join(entry_parts),
                                directory=directory,
                                size=info.st_size,
                                mtime=info.st_mtime,
                                authority="unregistered_workspace_file",
                            )
                        )
                        depth = len(entry_parts) - len(root_parts)
                        if directory and (max_depth == 0 or depth < max_depth):
                            pending.append(entry_parts)
                after = os.fstat(descriptor)
                if (before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ManagedFileInventoryError(
                        "workspace_inventory_directory_changed"
                    )
        except FileNotFoundError:
            if not anchored and parts == root_parts and not rows:
                # A missing projection directory may exist solely in authority.
                continue
            raise ManagedFileInventoryError(
                "workspace_inventory_directory_changed"
            ) from None
        except NotADirectoryError:
            if not anchored and parts == root_parts:
                raise _RootNotDirectory(
                    "workspace_inventory_root_not_directory"
                ) from None
            raise ManagedFileInventoryError(
                "workspace_inventory_directory_changed"
            ) from None
        except OSError as exc:
            raise ManagedFileInventoryError(
                f"workspace_inventory_io_rejected:{type(exc).__name__}"
            ) from None
    return rows, visited


def _binding_path(row: dict[str, Any], prefix: str) -> tuple[str, tuple[str, ...]]:
    required = {
        "logical_path",
        "document_id",
        "binding_revision",
        "current_version_id",
        "document_revision",
        "byte_length",
        "content_hash",
        "recorded_at",
        "encoding",
    }
    if not required <= set(row):
        raise ManagedFileInventoryError(
            "workspace_inventory_incomplete_binding_descriptor"
        )
    logical = row.get("logical_path")
    if (
        not isinstance(logical, str)
        or len(logical) > 512
        or not logical.startswith(prefix)
        or not logical.startswith(_PREFIX)
    ):
        raise ManagedFileInventoryError("workspace_inventory_binding_outside_root")
    relative = logical[len(_PREFIX) :]
    parts = tuple(relative.split("/"))
    if (
        not relative
        or "\\" in relative
        or "\x00" in relative
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ManagedFileInventoryError("workspace_inventory_invalid_binding_path")
    return relative, parts


def _managed_item(row: dict[str, Any], relative: str) -> dict[str, Any]:
    document_id, version_id = row.get("document_id"), row.get("current_version_id")
    size, digest = row.get("byte_length"), row.get("content_hash")
    if (
        not isinstance(document_id, str)
        or not document_id
        or not isinstance(version_id, str)
        or not version_id
        or type(size) is not int
        or size < 0
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
        or type(row.get("binding_revision")) is not int
        or row["binding_revision"] < 1
        or type(row.get("document_revision")) is not int
        or row["document_revision"] < 1
    ):
        raise ManagedFileInventoryError(
            "workspace_inventory_invalid_version_descriptor"
        )
    try:
        recorded = datetime.fromisoformat(row["recorded_at"])
        mtime = recorded.replace(tzinfo=recorded.tzinfo or UTC).timestamp()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ManagedFileInventoryError(
            "workspace_inventory_invalid_recorded_time"
        ) from None
    item = _item(
        relative,
        directory=False,
        size=size,
        mtime=mtime,
        authority="subject_document_store",
    )
    item.update(
        {
            "document_id": document_id,
            "subject_version_id": version_id,
            "file_ref": f"subject-file:{document_id}@{version_id}",
            "content_hash": digest,
            "binding_revision": row["binding_revision"],
            "document_revision": row["document_revision"],
            "encoding": row.get("encoding"),
        }
    )
    return item


def _overlay(
    disk: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    *,
    prefix: str,
    root_parts: tuple[str, ...],
    max_depth: int,
    include_hidden: bool,
) -> list[dict[str, Any]]:
    registered: set[str] = set()
    managed: dict[str, dict[str, Any]] = {}
    for row in bindings:
        relative, parts = _binding_path(row, prefix)
        registered.add(relative)
        if type(row.get("binding_revision")) is not int or row["binding_revision"] < 1:
            raise ManagedFileInventoryError(
                "workspace_inventory_invalid_binding_revision"
            )
        if row.get("document_id") is not None:
            item = _managed_item(row, relative)
            if _visible(parts, include_hidden):
                managed[relative] = item
        elif row.get("document_revision") != 0 or any(
            row[name] is not None
            for name in (
                "current_version_id",
                "byte_length",
                "content_hash",
                "recorded_at",
                "encoding",
            )
        ):
            raise ManagedFileInventoryError(
                "workspace_inventory_invalid_released_binding"
            )

    def ancestors(path: str) -> Iterator[str]:
        parts = path.split("/")
        for count in range(1, len(parts)):
            yield "/".join(parts[:count])

    for path in managed:
        if any(parent in managed for parent in ancestors(path)):
            raise ManagedFileInventoryError(
                "workspace_inventory_file_directory_conflict"
            )
    result = {
        item["path"]: item
        for item in disk
        if item["path"] not in registered
        and not any(parent in registered for parent in ancestors(item["path"]))
    }
    for path, item in managed.items():
        parts = tuple(path.split("/"))
        depth = len(parts) - len(root_parts)
        if max_depth == 0 or depth <= max_depth:
            result[path] = item
        for length in range(len(root_parts) + 1, len(parts)):
            if max_depth and length - len(root_parts) > max_depth:
                break
            parent = "/".join(parts[:length])
            existing = result.get(parent)
            if existing is not None and existing["type"] != "directory":
                # A stale ordinary file cannot hide an authority-only directory.
                result.pop(parent)
                existing = None
            if existing is None or existing["_mtime"] < item["_mtime"]:
                result[parent] = _item(
                    parent,
                    directory=True,
                    size=0,
                    mtime=item["_mtime"],
                    authority="derived_subject_directory",
                )
        if len(result) > _MAX_INVENTORY_ENTRIES:
            raise ManagedFileInventoryError("workspace_inventory_budget_exceeded")
    return sorted(
        result.values(), key=lambda item: (item["name"].casefold(), item["path"])
    )


async def load_managed_inventory(
    session: ManagedFileSession,
    *,
    root: Path,
    max_depth: int = 0,
    include_hidden: bool = True,
) -> list[dict[str, Any]]:
    """Return flat exact-version metadata, never a silently truncated inventory.

    ``max_depth=1`` includes immediate children; zero traverses all depths. Disk
    entries and authority bindings each have a 10,000-record scan budget; the
    final unique view (including derived directories) is also capped at 10,000.
    Thus a managed path's physical projection is not counted twice against the
    result budget, while neither source can hide unbounded work behind overlap.
    Hidden names and the existing glob ignore directories are excluded when
    ``include_hidden`` is false.
    Each authority item pins its descriptor's immutable version for later reads.
    """
    if type(max_depth) is not int or max_depth < 0 or type(include_hidden) is not bool:
        raise ValueError(
            "workspace inventory requires nonnegative depth and boolean visibility"
        )
    workspace = Path(os.path.abspath(session.workspace))
    requested = Path(root)
    requested = requested if requested.is_absolute() else workspace / requested
    requested = Path(os.path.abspath(requested))
    try:
        root_parts = requested.relative_to(workspace).parts
    except ValueError:
        raise ManagedFileInventoryError(
            "workspace_inventory_root_outside_workspace"
        ) from None
    if (
        root_parts
        and await session.store.get_head(_PREFIX + "/".join(root_parts)) is not None
    ):
        raise ManagedFileInventoryError("workspace_inventory_root_not_directory")
    root_is_file = False
    try:
        disk, _disk_visited = await run_workspace_file_io(
            _scan_disk,
            workspace,
            root_parts,
            max_depth,
            include_hidden,
            _MAX_INVENTORY_ENTRIES,
        )
    except _RootNotDirectory:
        disk, root_is_file = [], True
    if not _visible(root_parts, include_hidden) and not root_is_file:
        return []
    prefix = _PREFIX + ("/".join(root_parts) + "/" if root_parts else "")
    bindings: list[dict[str, Any]] = []
    cursor = ""
    binding_count = 0
    while True:
        batch = await session.store.list_file_bindings(
            logical_path_prefix=prefix,
            after_logical_path=cursor,
            limit=_PAGE_SIZE,
        )
        if not isinstance(batch, list) or len(batch) > _PAGE_SIZE:
            raise ManagedFileInventoryError("workspace_inventory_invalid_binding_page")
        if not batch:
            break
        for row in batch:
            if not isinstance(row, dict):
                raise ManagedFileInventoryError(
                    "workspace_inventory_invalid_binding_row"
                )
            _binding_path(row, prefix)
            logical = row["logical_path"]
            if logical <= cursor:
                raise ManagedFileInventoryError(
                    "workspace_inventory_nonmonotonic_binding_page"
                )
            cursor = logical
            binding_count += 1
            if binding_count > _MAX_INVENTORY_ENTRIES:
                raise ManagedFileInventoryError("workspace_inventory_budget_exceeded")
            bindings.append(row)
    if root_is_file and not any(row["document_id"] is not None for row in bindings):
        raise ManagedFileInventoryError("workspace_inventory_root_not_directory")
    return await run_workspace_file_io(
        _overlay,
        disk,
        bindings,
        prefix=prefix,
        root_parts=root_parts,
        max_depth=max_depth,
        include_hidden=include_hidden,
    )


__all__ = ["ManagedFileInventoryError", "load_managed_inventory"]
