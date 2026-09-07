"""Existing file-tool adapter for exact selected document transactions.

This module does not infer the meaning of a file. The workspace is the caller's
authorized file surface; imported pre-existing bytes retain unknown semantic
provenance. The selected document store owns identity/history, not the disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage.models import BackendKind
from ..storage.subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentConflict,
    SubjectDocumentNotFound,
)
from ..storage.workspace_file_io import read_exact_bytes, run_workspace_file_io
from ._utils import _get_workspace

_ROOT_NAMES = frozenset({"SOUL.md", "USER.md", "MEMORY.md"})
_PREFIX = "life_engine_workspace/"
_MAX_FILE_BYTES = 16 * 1024 * 1024


class SelectedSubjectStorageNotStarted(RuntimeError):
    """Content-free diagnostic for a selected authority that is unavailable."""

    def __init__(self) -> None:
        super().__init__("SelectedSubjectStorageNotStarted")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """A read pin; unknown filesystem bytes are never a subject assertion."""

    path: str
    logical_path: str
    content: bytes | None
    head: Any = None
    version: Any = None
    binding_revision: int = 0
    legacy: bool = False
    disk_signature: tuple[int, int, int, int] | None = None

    @property
    def expected_version(self) -> str:
        material = {
            "path": self.logical_path,
            "document_id": self.head.document_id if self.head else "",
            "version_id": self.version.version_id if self.version else "",
            "revision": self.head.revision if self.head else 0,
            "binding_revision": self.binding_revision,
            "legacy_sha256": hashlib.sha256(self.content).hexdigest()
            if self.legacy and self.content is not None
            else "",
            "exists": self.content is not None,
        }
        return "file-revision:" + _digest(material)

    @property
    def reference(self) -> str:
        if self.version is None:
            return ""
        return f"subject-file:{self.version.document_id}@{self.version.version_id}"


def split_file_continuation(continuation: str, version_id: str) -> tuple[str, str]:
    """Unwrap a version pin; the existing bounded cursor still verifies pages."""
    if not continuation.startswith("mfc1."):
        return continuation, version_id
    pieces = continuation.split(".", 2)
    if len(pieces) != 3 or not pieces[1].startswith("ver_") or not pieces[2]:
        raise ValueError("ManagedFileContinuationInvalid")
    if version_id and version_id != pieces[1]:
        raise ValueError("ManagedFileContinuationVersionConflict")
    return pieces[2], pieces[1]


def pin_file_continuation(continuation: str, version_id: str) -> str:
    return f"mfc1.{version_id}.{continuation}" if continuation else ""


class ManagedFileSession:
    """One authorized existing tool call backed by the selected history store."""

    def __init__(self, tool: Any, service: Any) -> None:
        self.tool = tool
        self.service = service
        self.store = getattr(service, "_subject_document_store", None)
        if self.store is None:
            raise SelectedSubjectStorageNotStarted()
        self.workspace = _get_workspace(tool.plugin).resolve()

    def relative(self, target: Path) -> str:
        relative = target.resolve().relative_to(self.workspace).as_posix()
        if relative in {"", "."}:
            raise ValueError("ManagedFilePathMustNameAFile")
        return relative

    async def read(
        self,
        target: Path,
        *,
        version_id: str = "",
        allow_missing: bool = False,
        max_bytes: int = _MAX_FILE_BYTES,
    ) -> FileSnapshot:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("ManagedFileReadByteBudgetInvalid")
        relative = self.relative(target)
        logical = _PREFIX + relative
        binding = await self.store.get_path_binding(logical)
        binding_revision = binding.revision if binding else 0
        head = await self.store.get_head(logical)
        if version_id:
            version = await self.store.get_version(version_id)
            if not version.logical_path.startswith(_PREFIX):
                raise PermissionError("ManagedFileVersionOutsideWorkspace")
            document_head = await self.store.get_document_head(version.document_id)
            if document_head is None or not document_head.logical_path.startswith(
                _PREFIX
            ):
                raise PermissionError("ManagedFileDocumentOutsideWorkspace")
            if version.content_hash != hashlib.sha256(
                version.content_bytes
            ).hexdigest() or version.byte_length != len(version.content_bytes):
                raise RuntimeError("SelectedSubjectVersionIntegrityError")
            return FileSnapshot(
                relative,
                logical,
                bytes(version.content_bytes),
                document_head,
                version,
                binding_revision,
            )
        if head is not None:
            version = await self.store.get_version(head.current_version_id)
            content = bytes(version.content_bytes)
            if (
                version.document_id != head.document_id
                or version.content_hash != hashlib.sha256(content).hexdigest()
                or version.byte_length != len(content)
            ):
                raise RuntimeError("SelectedSubjectVersionIntegrityError")
            return FileSnapshot(
                relative, logical, content, head, version, binding_revision
            )
        if binding is not None:
            if not allow_missing:
                raise SubjectDocumentNotFound("ManagedFileHasNoCurrentBinding")
            return FileSnapshot(
                relative, logical, None, binding_revision=binding_revision
            )
        if relative in _ROOT_NAMES and target.exists():
            raise SubjectDocumentNotFound("SelectedSubjectHeadMissing")
        if not target.exists():
            if not allow_missing:
                name = (
                    "SelectedSubjectHeadMissing"
                    if relative in _ROOT_NAMES
                    else "ManagedFileNotFound"
                )
                raise SubjectDocumentNotFound(name)
            return FileSnapshot(relative, logical, None)
        if not target.is_file() or target.is_symlink():
            raise ValueError("ManagedFileLegacySourceNotRegular")
        content = await run_workspace_file_io(
            read_exact_bytes,
            self.workspace,
            relative,
            max_bytes=min(max_bytes, _MAX_FILE_BYTES),
        )
        return FileSnapshot(
            relative,
            logical,
            content,
            legacy=True,
        )

    async def guard_file_ancestors(self, logical_path: str) -> None:
        """A registered file cannot become a directory because its cache is absent."""
        parts = logical_path.split("/")
        if len(parts) > 64:
            raise ValueError("ManagedFilePathDepthExceedsBudget")
        for length in range(2, len(parts)):
            ancestor = "/".join(parts[:length])
            if await self.store.get_head(ancestor) is not None:
                raise SubjectDocumentConflict("ManagedFileAncestorIsRegisteredFile")

    def unbound_disk_blocks_creation(self, snapshot: FileSnapshot) -> bool:
        """Remote authority never treats a previously registered disk cache as current."""
        if (
            getattr(self.store, "backend", None) == BackendKind.MYSQL
            and snapshot.binding_revision > 0
        ):
            return False
        return (self.workspace / snapshot.path).exists()

    async def origin(self) -> tuple[str, str, str | None]:
        scope = (
            getattr(getattr(self.tool, "trigger_message", None), "extra", {}) or {}
        ).get("life_turn_scope", {})
        scope = scope if isinstance(scope, dict) else {}
        activities = scope.get("conscious_activity_ids", {})
        activities = activities if isinstance(activities, dict) else {}
        actor = str(
            getattr(self.tool, "_life_source_instance_id", "")
            or scope.get("consciousness_instance_id", "")
        ).strip()
        source = str(
            getattr(self.tool, "_life_source_occurrence_id", "")
            or activities.get(str(getattr(self.tool, "_tool_call_id", "") or ""), "")
        ).strip()
        if not actor or not source:
            raise PermissionError("SubjectFileWriteOriginRequired")
        if not await self.service._validate_learning_decision_actor(actor):
            raise PermissionError("SubjectFileWriteActorIsNotActive")
        occurred = str(getattr(self.tool, "_life_source_occurred_at", "") or "") or None
        return actor, source, occurred

    def occurrence(self, actor: str, source: str, ordinal: str = "") -> str:
        call_id = str(getattr(self.tool, "_tool_call_id", "") or "")
        if not call_id:
            raise PermissionError("ManagedFileToolCallIdentityRequired")
        return "file-tool:" + _digest([actor, source, call_id, ordinal])

    async def replay(
        self, occurrence: str, request_digest: str
    ) -> dict[str, Any] | None:
        existing = await self.store.get_document_operation(occurrence)
        if existing is None:
            return None
        context = existing.change_context
        if context.get("file_tool_request_sha256") != request_digest:
            raise SubjectDocumentConflict("ManagedFileOperationIdentityConflict")
        result = existing.result
        return {
            "occurrence_id": occurrence,
            "operation": existing.operation,
            "document_id": existing.document_id,
            "version_id": str(result["version_id"]),
            "logical_path": str(result["logical_path"]),
            "revision": int(result["revision"]),
            "idempotent_replay": True,
        }

    @staticmethod
    def request_digest(request: dict[str, Any]) -> str:
        return _digest(request)

    @staticmethod
    def require_pin(snapshot: FileSnapshot, expected_version: str) -> None:
        if snapshot.content is not None and not expected_version:
            raise SubjectDocumentConflict(
                "ManagedFileExpectedVersionRequired: read the file first"
            )
        if expected_version and expected_version != snapshot.expected_version:
            if (
                snapshot.version is not None
                and snapshot.version.recorded_source == "file_tool_prechange_import"
                and snapshot.version.change_context.get("legacy_expected_version")
                == expected_version
            ):
                return
            raise SubjectDocumentConflict(
                "ManagedFileReadVersionConflict: read the current file again"
            )

    async def adopt_legacy(self, snapshot: FileSnapshot) -> FileSnapshot:
        """Record exact old bytes with unknown author before an authorized edit."""
        if not snapshot.legacy:
            return snapshot
        assert snapshot.content is not None
        target = self.workspace / snapshot.path
        current = await self.read(target)
        if current.expected_version != snapshot.expected_version:
            raise SubjectDocumentConflict("ManagedFileLegacySourceChanged")
        occurrence = "file-observation:" + _digest(
            [snapshot.logical_path, snapshot.expected_version]
        )
        commit = await self.store.append_version(
            AppendSubjectDocumentVersion(
                logical_path=snapshot.logical_path,
                expected_revision=0,
                expected_head_version_id="",
                expected_document_id="",
                expected_binding_revision=0,
                content_bytes=snapshot.content,
                occurrence_id=occurrence,
                recorded_by="filesystem-observer",
                recorded_source="file_tool_prechange_import",
                declared_owner="elysia",
                semantic_actor_id=None,
                semantic_source_id=None,
                occurred_at=None,
                provenance_status="semantic_source_missing",
                byte_fidelity="exact_bytes",
                change_context={
                    "operation": "exact_legacy_capture",
                    "source_sha256": hashlib.sha256(snapshot.content).hexdigest(),
                    "legacy_expected_version": snapshot.expected_version,
                },
            )
        )
        return FileSnapshot(
            snapshot.path,
            snapshot.logical_path,
            snapshot.content,
            commit.head,
            commit.version,
            commit.head.binding_revision,
        )

    async def prepare_write(
        self,
        snapshot: FileSnapshot,
        content: bytes,
        *,
        expected_version: str,
        occurrence: str,
        request_digest: str,
        origin: tuple[str, str, str | None],
        encoding: str,
        reason: str,
    ) -> AppendSubjectDocumentVersion:
        self.require_pin(snapshot, expected_version)
        await self.guard_file_ancestors(snapshot.logical_path)
        if len(content) > _MAX_FILE_BYTES:
            raise ValueError("ManagedFileWriteExceedsByteBudget")
        if snapshot.content is None and self.unbound_disk_blocks_creation(snapshot):
            raise SubjectDocumentConflict(
                "ManagedFileUnboundProjectionRequiresRecovery"
            )
        snapshot = await self.adopt_legacy(snapshot)
        actor, source, occurred = origin
        return AppendSubjectDocumentVersion(
            logical_path=snapshot.logical_path,
            expected_revision=snapshot.head.revision if snapshot.head else 0,
            expected_head_version_id=snapshot.version.version_id
            if snapshot.version
            else "",
            expected_document_id=snapshot.head.document_id if snapshot.head else "",
            expected_binding_revision=snapshot.binding_revision,
            content_bytes=bytes(content),
            occurrence_id=occurrence,
            recorded_by="life_engine",
            recorded_source="nucleus_file_tool",
            declared_owner=snapshot.head.declared_owner if snapshot.head else "elysia",
            semantic_actor_id=actor,
            semantic_source_id=source,
            occurred_at=occurred,
            provenance_status="complete",
            encoding=encoding,
            change_context={
                "operation": "file_tool_write",
                "reason": reason,
                "file_tool_request_sha256": request_digest,
                "tool_name": self.tool.tool_name,
            },
        )

    @staticmethod
    def commit_result(commit: Any, occurrence: str, operation: str) -> dict[str, Any]:
        return {
            "occurrence_id": occurrence,
            "operation": operation,
            "document_id": commit.head.document_id,
            "version_id": commit.version.version_id,
            "logical_path": commit.head.logical_path,
            "revision": commit.head.revision,
            "idempotent_replay": bool(getattr(commit, "idempotent_replay", False)),
        }

    async def write(
        self, snapshot: FileSnapshot, content: bytes, **kwargs: Any
    ) -> dict[str, Any]:
        command = await self.prepare_write(snapshot, content, **kwargs)
        commit = await self.store.append_version(command)
        return self.commit_result(commit, command.occurrence_id, "write")

    async def prepare_mutation(
        self,
        snapshot: FileSnapshot,
        *,
        operation: str,
        target: FileSnapshot | None,
        expected_version: str,
        occurrence: str,
        request_digest: str,
        origin: tuple[str, str, str | None],
        reason: str,
        content_bytes: bytes | None = None,
        encoding: str | None = None,
    ) -> Any:
        from ..storage.subject_contracts import SubjectDocumentMutation

        self.require_pin(snapshot, expected_version)
        if snapshot.path in _ROOT_NAMES or (target and target.path in _ROOT_NAMES):
            raise PermissionError("StandingPromptPathProtected")
        if snapshot.content is None:
            raise SubjectDocumentNotFound("ManagedFileNotFound")
        if target is not None and (
            target.content is not None or self.unbound_disk_blocks_creation(target)
        ):
            raise SubjectDocumentConflict("ManagedFileTargetAlreadyExists")
        if target is not None:
            await self.guard_file_ancestors(target.logical_path)
        snapshot = await self.adopt_legacy(snapshot)
        actor, source, occurred = origin
        return SubjectDocumentMutation(
            operation=operation,
            logical_path=snapshot.logical_path,
            expected_document_id=snapshot.head.document_id,
            expected_revision=snapshot.head.revision,
            expected_head_version_id=snapshot.version.version_id,
            expected_binding_revision=snapshot.binding_revision,
            target_logical_path=target.logical_path if target else "",
            expected_target_binding_revision=target.binding_revision if target else 0,
            occurrence_id=occurrence,
            recorded_by="life_engine",
            recorded_source="nucleus_file_tool",
            semantic_actor_id=actor,
            semantic_source_id=source,
            occurred_at=occurred,
            content_bytes=content_bytes,
            encoding=encoding,
            change_context={
                "reason": reason,
                "file_tool_request_sha256": request_digest,
                "tool_name": self.tool.tool_name,
            },
        )

    async def mutate(self, snapshot: FileSnapshot, **kwargs: Any) -> dict[str, Any]:
        command = await self.prepare_mutation(snapshot, **kwargs)
        commit = await self.store.mutate_document(command)
        return self.commit_result(commit, command.occurrence_id, command.operation)

    async def finish(self, result: dict[str, Any]) -> dict[str, Any]:
        """Report committed authority independently of a fallible projection."""
        projection: dict[str, Any]
        try:
            projection = await self.service._project_subject_version(
                logical_path=result["logical_path"],
                version_id=result["version_id"],
                max_tasks=result["revision"] + 2,
                occurrence_id=result["occurrence_id"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - committed authority needs an explicit projection failure receipt
            projection = {
                "status": "pending_recovery",
                "error_type": type(exc).__name__,
            }
        relative = str(result["logical_path"]).removeprefix(_PREFIX)
        notification: dict[str, Any] = {"status": "notified"}
        try:
            self.service.notify_subject_context_source_changed(relative)
        except Exception as exc:  # noqa: BLE001 - context refresh cannot turn a committed write into rejection
            notification = {
                "status": "pending_refresh",
                "error_type": type(exc).__name__,
            }
        memory = getattr(self.service, "_memory_service", None)
        index_projection: dict[str, Any] = {"status": "disabled"}
        if memory is not None:
            try:
                indexed = await memory.project_managed_document(result["document_id"])
                index_projection = {
                    "status": "updated" if indexed.indexed else "not_indexed",
                    "node_id": indexed.node_id,
                    "document_id": indexed.document_id,
                    "version_id": indexed.version_id,
                    "document_revision": indexed.document_revision,
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - index is fallible and independently recoverable
                index_projection = {
                    "status": "pending_rebuild",
                    "error_type": type(exc).__name__,
                }
        return {
            "schema": "elysium.file_commit.v1",
            "commit_status": "committed",
            **result,
            "file_ref": f"subject-file:{result['document_id']}@{result['version_id']}",
            "projection": projection,
            "context_notification": notification,
            "index_projection": index_projection,
            "retry_guidance": "read the committed version; never repeat as a new write",
        }

    async def recover_projection(self, occurrence_id: str) -> dict[str, Any]:
        """Recover only a committed operation's exact projection; append nothing."""
        await self.origin()
        operation = await self.store.get_document_operation(occurrence_id)
        if operation is None:
            raise SubjectDocumentNotFound("ManagedFileCommittedOperationNotFound")
        head = await self.store.get_document_head(operation.document_id)
        if head is None or not head.logical_path.startswith(_PREFIX):
            raise PermissionError("ManagedFileDocumentOutsideWorkspace")
        result = operation.result
        task = await self.store.get_projection_task(
            str(result["logical_path"]),
            str(result["version_id"]),
            occurrence_id=occurrence_id,
        )
        if (
            task is None
            or not task.logical_path.startswith(_PREFIX)
            or (
                task.previous_logical_path
                and not task.previous_logical_path.startswith(_PREFIX)
            )
        ):
            raise PermissionError("ManagedFileProjectionOutsideWorkspace")
        return await self.finish(
            {
                "occurrence_id": occurrence_id,
                "operation": operation.operation,
                "document_id": operation.document_id,
                "version_id": str(result["version_id"]),
                "logical_path": str(result["logical_path"]),
                "revision": int(result["revision"]),
                "idempotent_replay": True,
            }
        )


def selected_file_session(tool: Any, service: Any) -> ManagedFileSession | None:
    if service is None or not bool(
        getattr(service, "_selectable_storage_enabled", False)
    ):
        return None
    return ManagedFileSession(tool, service)
