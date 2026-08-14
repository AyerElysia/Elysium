"""Conflict-safe workspace projection and exact external-file observation."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .subject_adapters import normalize_subject_path
from .subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentCommit,
    SubjectDocumentStorePort,
)

_DECLARED_ROOTS = {
    "life_engine_workspace/MEMORY.md",
    "life_engine_workspace/SOUL.md",
    "life_engine_workspace/USER.md",
}
_DECLARED_PREFIXES = (
    "diaries/",
    "life_engine_workspace/diaries/",
    "notes/",
    "life_engine_workspace/notes/",
)


class RootSubjectAuthorityRequired(RuntimeError):
    """Reject generic writes to the unified root subject documents."""

    def __init__(self) -> None:
        super().__init__("RootSubjectAuthorityRequired")


def subject_path_from_workspace_relative(value: str) -> str | None:
    """Map a Life workspace path to the declared subject-document namespace."""

    raw = str(value).strip()
    if not raw or "\\" in raw:
        return None
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    normalized = path.as_posix()
    if normalized.startswith("life_engine_workspace/"):
        normalized = normalized.removeprefix("life_engine_workspace/")
    if normalized in {"SOUL.md", "USER.md", "MEMORY.md"} or normalized.startswith(
        ("diaries/", "notes/")
    ):
        return f"life_engine_workspace/{normalized}"
    return None


def auxiliary_subject_path_from_workspace_relative(value: str) -> str | None:
    """Map an auxiliary subject path while excluding root authority documents.

    ``SOUL.md``, ``USER.md`` and ``MEMORY.md`` may only change through
    ``SubjectAuthorityPort.accept_candidate``. This guard intentionally runs
    before storage-mode branching so disabled/local callers cannot turn a
    rejected root write into an implicit filesystem fallback.
    """

    logical_path = subject_path_from_workspace_relative(value)
    if logical_path in _DECLARED_ROOTS:
        raise RootSubjectAuthorityRequired()
    return logical_path


@dataclass(frozen=True, slots=True)
class SubjectProjectionResult:
    """One bounded workspace projection outcome."""

    status: str
    logical_path: str = ""
    version_id: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SubjectObservationResult:
    """One exact external-file observation outcome."""

    status: str
    logical_path: str
    commit: SubjectDocumentCommit | None = None


def _safe_workspace_path(data_root: Path, logical_path: str) -> Path:
    path = normalize_subject_path(logical_path)
    if path not in _DECLARED_ROOTS and not path.startswith(_DECLARED_PREFIXES):
        raise ValueError("subject path is outside declared workspace roots")
    root = data_root.resolve()
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("subject path escapes data root") from exc
    return candidate


def _encoding_and_newlines(content: bytes) -> tuple[str | None, str | None]:
    encoding: str | None = None
    try:
        if content.startswith(b"\xef\xbb\xbf"):
            content.decode("utf-8-sig")
            encoding = "utf-8-sig"
        else:
            content.decode("utf-8")
            encoding = "utf-8"
    except UnicodeDecodeError:
        pass
    crlf = content.count(b"\r\n")
    lone_lf = content.count(b"\n") - crlf
    lone_cr = content.count(b"\r") - crlf
    styles = [
        name
        for name, count in (("crlf", crlf), ("lf", lone_lf), ("cr", lone_cr))
        if count
    ]
    return encoding, styles[0] if len(styles) == 1 else ("mixed" if styles else None)


class SubjectWorkspaceProjector:
    """Materialize authoritative heads without overwriting unknown bytes."""

    def __init__(
        self,
        store: SubjectDocumentStorePort,
        *,
        data_root: str | Path,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> None:
        self.store = store
        self.data_root = Path(data_root).resolve()
        self.worker_id = str(worker_id).strip()
        self.lease_seconds = int(lease_seconds)
        if not self.worker_id or self.lease_seconds <= 0:
            raise ValueError("projector worker and positive lease are required")

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _replace_exact(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing_mode = None
        if path.exists():
            existing_mode = stat.S_IMODE(path.stat().st_mode)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.elysium-subject-",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if existing_mode is not None:
                os.chmod(temporary, existing_mode)
            os.replace(temporary, path)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except (OSError, PermissionError):
                # Windows does not permit opening directories with os.open.
                # The file itself has already been flushed, fsynced and atomically
                # replaced; directory fsync remains a best-effort POSIX durability
                # enhancement rather than a reason to mark projection failed.
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

    async def project_one(
        self,
        *,
        logical_path: str | None = None,
    ) -> SubjectProjectionResult:
        task = await self.store.claim_projection(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            logical_path=logical_path,
        )
        if task is None:
            return SubjectProjectionResult(status="idle")
        try:
            version = await self.store.get_version(task.version_id)
            if (
                version.content_hash != task.content_hash
                or hashlib.sha256(version.content_bytes).hexdigest()
                != task.content_hash
            ):
                raise RuntimeError("authoritative version bytes/hash mismatch")
            path = _safe_workspace_path(self.data_root, task.logical_path)
            if path.exists() and self._hash(path) == task.content_hash:
                await self.store.confirm_projection(task, worker_id=self.worker_id)
                return SubjectProjectionResult(
                    status="confirmed_existing",
                    logical_path=task.logical_path,
                    version_id=task.version_id,
                )
            if version.parent_version_id:
                parent = await self.store.get_version(version.parent_version_id)
                if not path.exists() or self._hash(path) != parent.content_hash:
                    raise RuntimeError(
                        "workspace bytes diverged from the authoritative parent"
                    )
            elif path.exists():
                raise RuntimeError("new authoritative document would overwrite bytes")
            self._replace_exact(path, version.content_bytes)
            if self._hash(path) != task.content_hash:
                raise RuntimeError("workspace verification failed after projection")
            await self.store.confirm_projection(task, worker_id=self.worker_id)
            return SubjectProjectionResult(
                status="projected",
                logical_path=task.logical_path,
                version_id=task.version_id,
            )
        except Exception as exc:  # noqa: BLE001 - persist bounded worker failure
            await self.store.fail_projection(
                task,
                worker_id=self.worker_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return SubjectProjectionResult(
                status="failed",
                logical_path=task.logical_path,
                version_id=task.version_id,
                detail=f"{type(exc).__name__}: {exc}",
            )


class SubjectWorkspaceObserver:
    """Append changed external bytes as observations without semantic guessing."""

    def __init__(
        self,
        store: SubjectDocumentStorePort,
        *,
        data_root: str | Path,
        recorded_source: str,
    ) -> None:
        self.store = store
        self.data_root = Path(data_root).resolve()
        self.recorded_source = str(recorded_source).strip()
        if not self.recorded_source:
            raise ValueError("observer recorded_source must not be empty")

    async def observe_file(self, logical_path: str) -> SubjectObservationResult:
        normalized = normalize_subject_path(logical_path)
        path = _safe_workspace_path(self.data_root, normalized)
        if not path.is_file() or path.is_symlink():
            return SubjectObservationResult(status="missing", logical_path=normalized)
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
        ):
            return SubjectObservationResult(
                status="changed_during_read",
                logical_path=normalized,
            )
        content_hash = hashlib.sha256(content).hexdigest()
        head = await self.store.get_head(normalized)
        if head is not None:
            current = await self.store.get_version(head.current_version_id)
            if (
                current.content_hash == content_hash
                and current.content_bytes == content
            ):
                return SubjectObservationResult(
                    status="unchanged",
                    logical_path=normalized,
                )
            expected_revision = head.revision
            expected_head = head.current_version_id
            owner = head.declared_owner
        else:
            expected_revision = 0
            expected_head = ""
            owner = "elysia"
        occurrence_material = f"{normalized}\0{expected_head}\0{content_hash}".encode()
        occurrence_id = (
            "observation:subject:" + hashlib.sha256(occurrence_material).hexdigest()
        )
        encoding, newline_style = _encoding_and_newlines(content)
        committed = await self.store.append_version(
            AppendSubjectDocumentVersion(
                logical_path=normalized,
                expected_revision=expected_revision,
                expected_head_version_id=expected_head,
                content_bytes=content,
                occurrence_id=occurrence_id,
                recorded_by="filesystem-observer",
                recorded_source=self.recorded_source,
                declared_owner=owner,
                semantic_actor_id=None,
                semantic_source_id=None,
                occurred_at=None,
                provenance_status="semantic_source_missing",
                byte_fidelity="exact_bytes",
                encoding=encoding,
                newline_style=newline_style,
                change_context={
                    "external_file_observation": True,
                    "source_stat": {
                        "bytes": after.st_size,
                        "device": after.st_dev,
                        "inode": after.st_ino,
                        "mtime_ns": after.st_mtime_ns,
                    },
                },
            )
        )
        return SubjectObservationResult(
            status="appended",
            logical_path=normalized,
            commit=committed,
        )

    def declared_paths(self) -> list[str]:
        paths: set[str] = set()
        workspace = self.data_root / "life_engine_workspace"
        for name in ("MEMORY.md", "SOUL.md", "USER.md"):
            path = workspace / name
            if path.is_file() and not path.is_symlink():
                paths.add(path.relative_to(self.data_root).as_posix())
        for root in (
            self.data_root / "diaries",
            workspace / "diaries",
            workspace / "notes",
        ):
            if not root.is_dir() or root.is_symlink():
                continue
            for path in root.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    paths.add(path.relative_to(self.data_root).as_posix())
        return sorted(paths)

    async def observe_all(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for logical_path in self.declared_paths():
            result = await self.observe_file(logical_path)
            counts[result.status] = counts.get(result.status, 0) + 1
        return counts


__all__ = [
    "RootSubjectAuthorityRequired",
    "SubjectObservationResult",
    "SubjectProjectionResult",
    "SubjectWorkspaceObserver",
    "SubjectWorkspaceProjector",
    "auxiliary_subject_path_from_workspace_relative",
    "subject_path_from_workspace_relative",
]
