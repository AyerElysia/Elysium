"""Shared document eligibility rules for Life Engine memory indexing.

The index must only contain authored memory material. Runtime state, traces, and
internal storage are deliberately excluded before their contents are read.
"""

from __future__ import annotations

import os
import posixpath
import sqlite3
import stat
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SUPPORTED_DOCUMENT_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
MEMORY_CONTENT_DIRECTORIES = frozenset({"archive", "diaries", "dreams", "narrative", "notes"})
ROOT_DOCUMENT_NAMES = frozenset(
    {
        "AyerElysia_preferences.txt",
        "EXISTENCE.md",
        "MEMORY.md",
        "MEMORY_GUIDE.md",
        "SOUL.md",
        "SUBCONSCIOUS.md",
        "TOOL.md",
        "TOOLS.md",
        "USER.md",
        "love_snow.txt",
    }
)
BLOCKED_TOP_LEVEL_DIRECTORIES = frozenset({"runtime", "thoughts"})
TEMPORARY_SUFFIXES = frozenset(
    {".bak", ".backup", ".old", ".orig", ".swp", ".swo", ".temp", ".tmp"}
)
DEFAULT_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class DocumentEligibility:
    """Path-only eligibility result for a memory document."""

    path: str
    eligible: bool
    reason: str = ""


@dataclass(frozen=True)
class WorkspaceDocument:
    """A supported, regular workspace file ready for content ingestion."""

    path: str
    absolute_path: Path
    source_mtime: float
    size_bytes: int


@dataclass(frozen=True)
class WorkspaceDocumentScan:
    """Metadata-only workspace scan shared by health and reconciliation."""

    documents: tuple[WorkspaceDocument, ...]
    rejected: tuple[DocumentEligibility, ...]

    @property
    def rejected_reason_counts(self) -> dict[str, int]:
        counts = Counter(item.reason for item in self.rejected if item.reason)
        return dict(sorted(counts.items()))


def _raw_path(file_path: str | Path) -> str:
    return str(file_path or "").strip().replace("\\", "/")


def normalize_document_path(file_path: str | Path) -> str:
    """Normalize a relative document path without accepting path traversal."""
    raw = _raw_path(file_path)
    if not raw or raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        return ""
    normalized = posixpath.normpath(raw)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return ""
    return normalized


def assess_document_path(file_path: str | Path) -> DocumentEligibility:
    """Decide whether a relative path may enter the memory document index."""
    raw = _raw_path(file_path)
    if not raw:
        return DocumentEligibility("", False, "empty_path")
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        return DocumentEligibility("", False, "absolute_path")

    normalized = normalize_document_path(raw)
    if not normalized:
        return DocumentEligibility("", False, "invalid_path")

    parts = tuple(part for part in normalized.split("/") if part)
    if not parts:
        return DocumentEligibility("", False, "invalid_path")
    if any(part.startswith(".") for part in parts):
        return DocumentEligibility(normalized, False, "hidden_directory")

    filename = parts[-1]
    lowered_filename = filename.casefold()
    suffix = Path(filename).suffix.casefold()
    if lowered_filename.endswith("~") or suffix in TEMPORARY_SUFFIXES:
        return DocumentEligibility(normalized, False, "temporary_name")
    if suffix not in SUPPORTED_DOCUMENT_SUFFIXES:
        return DocumentEligibility(normalized, False, "unsupported_suffix")

    if len(parts) == 1:
        if filename not in ROOT_DOCUMENT_NAMES:
            return DocumentEligibility(normalized, False, "root_not_whitelisted")
        return DocumentEligibility(normalized, True)

    if parts[0] in BLOCKED_TOP_LEVEL_DIRECTORIES:
        return DocumentEligibility(normalized, False, "blocked_directory")
    if parts[0] not in MEMORY_CONTENT_DIRECTORIES:
        return DocumentEligibility(normalized, False, "unsupported_directory")
    return DocumentEligibility(normalized, True)


def is_eligible_document_path(file_path: str | Path) -> bool:
    """Return whether a relative path is allowed in the memory document index."""
    return assess_document_path(file_path).eligible


def assess_indexed_document_path(file_path: str | Path | None) -> DocumentEligibility:
    """Validate a stored path without silently normalizing historical rows.

    External API inputs may be normalized before writing a document.  Persisted
    index rows are different: a transformed path can point at a different
    physical file or identity, so only an exact canonical spelling is trusted.
    """
    raw = "" if file_path is None else str(file_path)
    decision = assess_document_path(raw)
    if not decision.eligible:
        return decision
    if raw != decision.path:
        return DocumentEligibility(decision.path, False, "noncanonical_path")
    return decision


def is_eligible_indexed_document_path(file_path: str | Path | None) -> bool:
    """Return whether a stored index path is already canonical and eligible."""
    return assess_indexed_document_path(file_path).eligible


def _open_workspace_document_windows(
    root: Path,
    path: str,
) -> tuple[int, os.stat_result]:
    """Open a checked workspace file on Windows without opening directories."""

    candidate = root.joinpath(*Path(path).parts)
    current = root
    before_path: os.stat_result | None = None
    for part in Path(path).parts:
        current = current / part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError("workspace document path contains a symlink")
        before_path = metadata
    if before_path is None or not stat.S_ISREG(before_path.st_mode):
        raise OSError("workspace document is not a regular file")
    candidate.resolve().relative_to(root)

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    file_fd = os.open(candidate, flags)
    try:
        opened = os.fstat(file_fd)
        after_path = candidate.lstat()
        if stat.S_ISLNK(after_path.st_mode) or not stat.S_ISREG(opened.st_mode):
            raise OSError("workspace document is not a regular file")
        if (
            before_path.st_dev != opened.st_dev
            or before_path.st_ino != opened.st_ino
            or after_path.st_dev != opened.st_dev
            or after_path.st_ino != opened.st_ino
        ):
            raise OSError("workspace document changed while opening")
        candidate.resolve().relative_to(root)
        return file_fd, opened
    except BaseException:
        os.close(file_fd)
        raise


def _open_workspace_document_fd(root: Path, path: str) -> tuple[int, os.stat_result]:
    """Open a canonical relative document without following any symlink."""
    if os.name == "nt":
        return _open_workspace_document_windows(root, path)

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    root_fd = os.open(root, directory_flags | cloexec)
    current_fd = root_fd
    file_fd = -1
    try:
        parts = Path(path).parts
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags | nofollow | cloexec, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | nofollow | cloexec, dir_fd=current_fd)
        metadata = os.fstat(file_fd)
    except BaseException:
        if file_fd >= 0:
            os.close(file_fd)
        if current_fd >= 0:
            os.close(current_fd)
        raise
    os.close(current_fd)
    return file_fd, metadata


def read_workspace_document(
    workspace: str | Path,
    file_path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
) -> tuple[str, float, int]:
    """Read one canonical workspace document from a checked regular-file fd.

    The returned bytes come from the same descriptor that was checked for
    symlinks, type, and size.  A concurrent replacement causes a failure rather
    than silently indexing a different object.
    """
    decision = assess_indexed_document_path(file_path)
    if not decision.eligible:
        raise ValueError(f"不是可读取的记忆文档: {decision.reason}")
    root = Path(workspace).expanduser().resolve()
    root_metadata = root.stat()
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise OSError("workspace is not a directory")

    file_fd, before = _open_workspace_document_fd(root, decision.path)
    try:
        if not stat.S_ISREG(before.st_mode):
            raise OSError("workspace document is not a regular file")
        maximum = max(0, int(max_bytes))
        if before.st_size > maximum:
            raise OSError("workspace document exceeds maximum size")
        chunks: list[bytes] = []
        remaining = int(before.st_size)
        while remaining > 0:
            chunk = os.read(file_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(file_fd)
        unchanged = (
            before.st_dev == after.st_dev
            and before.st_ino == after.st_ino
            and before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
            and stat.S_ISREG(after.st_mode)
        )
        if not unchanged or remaining != 0:
            raise OSError("workspace document changed during read")
        return b"".join(chunks).decode("utf-8", errors="replace"), float(before.st_mtime), int(before.st_size)
    finally:
        os.close(file_fd)


def assess_workspace_document(
    workspace: str | Path,
    file_path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
) -> DocumentEligibility:
    """Check a document path and its local file metadata without reading content."""
    decision = assess_document_path(file_path)
    if not decision.eligible:
        return decision

    root = Path(workspace).expanduser().resolve()
    candidate = root / decision.path
    try:
        current = root
        for part in Path(decision.path).parts:
            current = current / part
            if current.is_symlink():
                return DocumentEligibility(decision.path, False, "symlink")
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return DocumentEligibility(decision.path, False, "outside_workspace")

    try:
        metadata = candidate.stat()
    except OSError:
        return DocumentEligibility(decision.path, False, "stat_error")
    if not stat.S_ISREG(metadata.st_mode):
        return DocumentEligibility(decision.path, False, "not_regular_file")
    if metadata.st_size > max(0, int(max_bytes)):
        return DocumentEligibility(decision.path, False, "too_large")
    return decision


def scan_workspace_documents(
    workspace: str | Path,
    *,
    limit: int | None = None,
    max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
) -> WorkspaceDocumentScan:
    """Scan only memory-authoring domains and return eligible regular files.

    The scanner intentionally does not recurse through runtime, hidden, or
    otherwise unsupported top-level directories.  Those trees may contain
    large traces and transient state; recording one aggregate rejection for
    their root preserves health diagnostics without making their contents part
    of the indexing workload.
    """
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir() or limit == 0:
        return WorkspaceDocumentScan((), ())

    documents: list[WorkspaceDocument] = []
    rejected: list[DocumentEligibility] = []

    def _reject(path: str, reason: str) -> None:
        rejected.append(DocumentEligibility(path, False, reason))

    def _limit_reached() -> bool:
        return limit is not None and len(documents) >= max(0, int(limit))

    def _inspect_file(candidate: Path, relative: str) -> None:
        path_decision = assess_document_path(relative)
        if not path_decision.eligible:
            rejected.append(path_decision)
            return
        if path_decision.path != relative:
            _reject(relative, "noncanonical_path")
            return
        try:
            decision = assess_workspace_document(root, relative, max_bytes=max_bytes)
            if not decision.eligible:
                rejected.append(decision)
                return
            metadata = candidate.stat()
        except OSError:
            _reject(relative, "stat_error")
            return
        documents.append(
            WorkspaceDocument(
                path=relative,
                absolute_path=candidate,
                source_mtime=float(metadata.st_mtime),
                size_bytes=int(metadata.st_size),
            )
        )

    def _scan_memory_directory(directory: Path) -> bool:
        """Scan one allowed tree; return whether the document limit was met."""
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            try:
                relative = directory.relative_to(root).as_posix()
            except ValueError:
                return False
            _reject(relative, "stat_error")
            return False

        for candidate in entries:
            try:
                relative = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            try:
                if candidate.is_symlink():
                    decision = assess_workspace_document(root, relative, max_bytes=max_bytes)
                    rejected.append(decision)
                    continue
                if candidate.is_dir():
                    if candidate.name.startswith("."):
                        _reject(relative, "hidden_directory")
                        continue
                    if _scan_memory_directory(candidate):
                        return True
                    continue
            except OSError:
                _reject(relative, "stat_error")
                continue

            _inspect_file(candidate, relative)
            if _limit_reached():
                return True
        return False

    try:
        root_entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return WorkspaceDocumentScan((), ())

    for candidate in root_entries:
        relative = candidate.name
        # The SQLite/Chroma store is implementation state, not a rejected
        # authoring domain.  Skip it silently so its own artifacts cannot skew
        # workspace eligibility diagnostics.
        if relative == ".memory":
            continue
        if relative in ROOT_DOCUMENT_NAMES:
            _inspect_file(candidate, relative)
        elif relative in MEMORY_CONTENT_DIRECTORIES:
            try:
                if candidate.is_symlink():
                    _reject(relative, "symlink")
                elif candidate.is_dir():
                    if _scan_memory_directory(candidate):
                        break
                else:
                    _reject(relative, "not_regular_file")
            except OSError:
                _reject(relative, "stat_error")
        else:
            try:
                if candidate.is_dir() and not candidate.is_symlink():
                    if candidate.name.startswith("."):
                        _reject(relative, "hidden_directory")
                    elif candidate.name in BLOCKED_TOP_LEVEL_DIRECTORIES:
                        _reject(relative, "blocked_directory")
                    else:
                        _reject(relative, "unsupported_directory")
                else:
                    _inspect_file(candidate, relative)
            except OSError:
                _reject(relative, "stat_error")
        if _limit_reached():
            break

    return WorkspaceDocumentScan(tuple(documents), tuple(rejected))


SQLITE_INDEXED_PATH_FUNCTION = "life_memory_indexed_path_ok"
_SQL_FUNCTION_LOCK = threading.RLock()


def _indexed_path_sql_value(value: object) -> int:
    """Return SQLite's integer representation of the strict stored-path rule."""
    return int(is_eligible_indexed_document_path(value))


def _has_indexed_path_sql_function(db: sqlite3.Connection) -> bool:
    """Return whether this handle already owns the strict-path UDF."""
    try:
        return (
            db.execute(
                "SELECT 1 FROM pragma_function_list WHERE name = ? AND narg = ? LIMIT 1",
                (SQLITE_INDEXED_PATH_FUNCTION, 1),
            ).fetchone()
            is not None
        )
    except sqlite3.DatabaseError:
        # ``pragma_function_list`` is unavailable on older SQLite builds. A
        # direct invocation remains connection-local and, under the caller's
        # lock, avoids falling back to repeated ``create_function`` calls.
        try:
            db.execute(f"SELECT {SQLITE_INDEXED_PATH_FUNCTION}(NULL)").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such function" in str(exc).lower():
                return False
            raise
        return True


def register_indexed_path_sql_function(db: sqlite3.Connection) -> None:
    """Install the authoritative stored-path predicate at most once per handle."""
    with _SQL_FUNCTION_LOCK:
        if _has_indexed_path_sql_function(db):
            return
        db.create_function(
            SQLITE_INDEXED_PATH_FUNCTION,
            1,
            _indexed_path_sql_value,
            deterministic=True,
        )


def eligible_document_path_sql(column: str) -> tuple[str, list[str]]:
    """Return a strict SQLite predicate for canonical eligible index rows.

    The predicate delegates to the same Python rule used for final filtering,
    so SQL prefiltering cannot widen authorization through case folding or
    pathname normalization.
    """
    return f"{SQLITE_INDEXED_PATH_FUNCTION}({column}) = 1", []


def summarize_rejections(decisions: Iterable[DocumentEligibility]) -> dict[str, int]:
    """Return deterministic reason counts for health and CLI reports."""
    counts = Counter(item.reason for item in decisions if item.reason)
    return dict(sorted(counts.items()))


__all__ = [
    "BLOCKED_TOP_LEVEL_DIRECTORIES",
    "DEFAULT_MAX_DOCUMENT_BYTES",
    "DocumentEligibility",
    "MEMORY_CONTENT_DIRECTORIES",
    "ROOT_DOCUMENT_NAMES",
    "SQLITE_INDEXED_PATH_FUNCTION",
    "SUPPORTED_DOCUMENT_SUFFIXES",
    "TEMPORARY_SUFFIXES",
    "WorkspaceDocument",
    "WorkspaceDocumentScan",
    "assess_document_path",
    "assess_indexed_document_path",
    "assess_workspace_document",
    "eligible_document_path_sql",
    "is_eligible_document_path",
    "is_eligible_indexed_document_path",
    "normalize_document_path",
    "read_workspace_document",
    "register_indexed_path_sql_function",
    "scan_workspace_documents",
    "summarize_rejections",
]
