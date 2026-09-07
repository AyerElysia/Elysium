"""Conflict-safe workspace projection and exact external-file observation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .subject_adapters import normalize_subject_path
from .subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentCommit,
    SubjectDocumentStorePort,
    SubjectProjectionTask,
)
from .workspace_file_io import (
    WorkspaceFileConflict,
    WorkspaceFileError,
    project_exact_bytes,
    read_exact_bytes,
    remove_exact_bytes,
    run_workspace_file_io,
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
    outbox_id: int = 0
    head_event_id: str = ""


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
    """Project exact lifecycle tasks inside the store's authority write fence.

    Filesystem projection is recoverable, not atomic with the database commit.
    Confirmation/failure writes happen after leaving the fence; task identity and
    idempotent exact-byte helpers make the commit-to-confirm gap retryable.
    """

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
    def _projection_path(logical_path: str) -> str:
        path = normalize_subject_path(logical_path)
        if not path.startswith(("life_engine_workspace/", "notes/", "diaries/")):
            raise WorkspaceFileError("projection_path_outside_workspace_namespace")
        return path

    @staticmethod
    def _result(
        task: SubjectProjectionTask, status: str, detail: str = ""
    ) -> SubjectProjectionResult:
        return SubjectProjectionResult(
            status=status,
            logical_path=task.logical_path,
            version_id=task.version_id,
            outbox_id=task.outbox_id,
            head_event_id=task.head_event_id,
            detail=detail,
        )

    async def _current_target(self, task: SubjectProjectionTask) -> bool:
        head = await self.store.get_document_head(task.document_id)
        binding = await self.store.get_path_binding(task.logical_path)
        if head is None:
            raise WorkspaceFileError("projection_document_head_missing")
        if binding is None:
            raise WorkspaceFileError("projection_path_binding_missing")
        if task.binding_revision <= 0:
            raise WorkspaceFileError("projection_binding_revision_missing")
        if (
            head.document_id != task.document_id
            or head.logical_path != task.logical_path
            or head.current_version_id != task.version_id
            or head.binding_revision != task.binding_revision
            or binding.revision != task.binding_revision
        ):
            return False
        if task.operation == "delete":
            return head.deleted and binding.document_id is None
        return not head.deleted and binding.document_id == task.document_id

    async def _source_released(self, task: SubjectProjectionTask) -> bool:
        if not task.previous_logical_path or task.previous_binding_revision <= 0:
            raise WorkspaceFileError("projection_source_release_identity_missing")
        binding = await self.store.get_path_binding(task.previous_logical_path)
        if binding is None:
            raise WorkspaceFileError("projection_source_binding_missing")
        return (
            binding.document_id is None
            and binding.revision == task.previous_binding_revision
        )

    async def _previous_hash(self, task: SubjectProjectionTask) -> str:
        if not task.previous_version_id or not task.previous_content_hash:
            raise WorkspaceFileError("projection_previous_version_identity_missing")
        version = await self.store.get_version(task.previous_version_id)
        if (
            version.document_id != task.document_id
            or version.content_hash != task.previous_content_hash
            or hashlib.sha256(version.content_bytes).hexdigest()
            != task.previous_content_hash
        ):
            raise WorkspaceFileError("projection_previous_version_hash_mismatch")
        return task.previous_content_hash

    async def _read_optional(self, logical_path: str) -> bytes | None:
        try:
            return await run_workspace_file_io(
                read_exact_bytes, self.data_root, logical_path
            )
        except FileNotFoundError:
            return None

    async def _project_claimed(
        self, task: SubjectProjectionTask
    ) -> SubjectProjectionResult:
        if task.operation not in {"write", "copy", "rename", "delete"}:
            raise WorkspaceFileError("projection_operation_unsupported")
        if not task.head_event_id or task.outbox_id <= 0:
            raise WorkspaceFileError("projection_exact_task_identity_missing")
        target = self._projection_path(task.logical_path)
        version = await self.store.get_version(task.version_id)
        if (
            version.document_id != task.document_id
            or version.content_hash != task.content_hash
            or hashlib.sha256(version.content_bytes).hexdigest() != task.content_hash
        ):
            raise WorkspaceFileError("authoritative version bytes/hash mismatch")
        if not await self._current_target(task):
            return self._result(task, "superseded")

        if task.operation == "delete":
            if task.previous_logical_path != task.logical_path:
                raise WorkspaceFileError("projection_delete_source_path_mismatch")
            if not await self._source_released(task):
                return self._result(task, "superseded")
            previous_hash = await self._previous_hash(task)
            await run_workspace_file_io(
                remove_exact_bytes,
                self.data_root,
                target,
                expected_hash=previous_hash,
            )
            if await self._read_optional(target) is not None:
                raise WorkspaceFileConflict("projection_delete_path_still_present")
            return self._result(task, "deleted")

        # The configured root is trusted configuration; descendant traversal is
        # always performed by the rooted helper, including the first projection.
        await run_workspace_file_io(self.data_root.mkdir, parents=True, exist_ok=True)
        existing = await self._read_optional(target)
        already_equal = existing == version.content_bytes
        parent_hash: str | None = None
        if task.operation == "write" and version.parent_version_id:
            parent = await self.store.get_version(version.parent_version_id)
            if (
                parent.document_id != task.document_id
                or hashlib.sha256(parent.content_bytes).hexdigest()
                != parent.content_hash
            ):
                raise WorkspaceFileError("projection_parent_version_hash_mismatch")
            parent_hash = parent.content_hash
        # Rename/copy destinations are no-clobber, even when the source version
        # happens to have a parent. They do not authorize replacing target bytes.
        await run_workspace_file_io(
            project_exact_bytes,
            self.data_root,
            target,
            version.content_bytes,
            expected_parent_hash=parent_hash,
        )
        await run_workspace_file_io(
            read_exact_bytes,
            self.data_root,
            target,
            expected_hash=task.content_hash,
        )
        if not await self._current_target(task):
            raise WorkspaceFileConflict("projection_binding_changed_inside_fence")
        if task.operation == "rename":
            source = self._projection_path(task.previous_logical_path)
            if source == target:
                raise WorkspaceFileError("projection_rename_source_equals_target")
            previous_hash = await self._previous_hash(task)
            if not await self._source_released(task):
                return self._result(task, "renamed", "previous_path_rebound_preserved")
            await run_workspace_file_io(
                remove_exact_bytes,
                self.data_root,
                source,
                expected_hash=previous_hash,
            )
            if await self._read_optional(source) is not None:
                raise WorkspaceFileConflict("projection_rename_source_still_present")
            return self._result(task, "renamed")
        return self._result(
            task, "confirmed_existing" if already_equal else "projected"
        )

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
            fence = getattr(self.store, "workspace_projection_fence", None)
            if not callable(fence):
                raise WorkspaceFileError("workspace_projection_fence_unavailable")
            async with fence():
                result = await self._project_claimed(task)
            await self.store.confirm_projection(task, worker_id=self.worker_id)
            return result
        except Exception as exc:  # noqa: BLE001 - persist bounded worker failure
            # Storage/OS exceptions can carry SQL parameters or private bytes.
            # Only our content-free workspace diagnostics are safe to surface.
            detail = (
                f"{type(exc).__name__}: {exc}"
                if isinstance(exc, WorkspaceFileError)
                else type(exc).__name__
            )
            if (
                isinstance(exc, WorkspaceFileConflict)
                and str(exc) == "workspace_predecessor_hash_mismatch"
            ):
                detail += "; workspace bytes diverged from the authoritative parent"
            await self.store.fail_projection(
                task,
                worker_id=self.worker_id,
                error=detail,
            )
            return self._result(task, "failed", detail)


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
