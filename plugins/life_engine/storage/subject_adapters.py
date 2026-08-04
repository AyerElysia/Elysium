"""Fenced local/MySQL adapters for exact-byte subject document history."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage import canonical_json

from .contracts import StorageBackendRuntime
from .models import BackendKind
from .subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentCommit,
    SubjectDocumentConflict,
    SubjectDocumentHead,
    SubjectDocumentNotFound,
    SubjectDocumentVersion,
    SubjectProjectionTask,
)

_T = TypeVar("_T")
_MAX_WRITE_ATTEMPTS = 3


def normalize_subject_path(value: str) -> str:
    """Return one portable relative logical path without filesystem guessing."""

    raw = str(value).strip()
    if not raw or "\\" in raw:
        raise ValueError("subject logical_path must be a nonempty POSIX path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("subject logical_path must stay inside its logical root")
    normalized = path.as_posix()
    if len(normalized) > 512:
        raise ValueError("subject logical_path exceeds 512 characters")
    return normalized


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: Any) -> str:
    parsed = _parse_datetime(value)
    return parsed.isoformat() if parsed is not None else ""


def _optional(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def _json_object(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise TypeError("subject change context must be an object")
    return decoded


class SQLSubjectDocumentStore:
    """One subject ledger bound to a coherent storage runtime."""

    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if not runtime.enabled or runtime.engine is None:
            raise RuntimeError("subject document adapter requires enabled storage")
        self.runtime = runtime
        self.backend = runtime.backend

    @property
    def _for_update(self) -> str:
        return " FOR UPDATE" if self.backend == BackendKind.MYSQL else ""

    def _bind_time(self, value: Any) -> datetime | str | None:
        parsed = _parse_datetime(value)
        if parsed is None:
            return None
        if self.backend == BackendKind.MYSQL:
            return parsed.replace(tzinfo=None)
        return parsed.isoformat()

    async def _database_now(self, session: AsyncSession) -> datetime:
        if self.backend == BackendKind.MYSQL:
            value = await session.scalar(text("SELECT CURRENT_TIMESTAMP(6)"))
        else:
            value = await session.scalar(
                text("SELECT STRFTIME('%Y-%m-%dT%H:%M:%f+00:00', 'now')")
            )
        parsed = _parse_datetime(value)
        if parsed is None:
            raise RuntimeError("storage backend returned invalid database time")
        return parsed

    @staticmethod
    def _retryable(exc: DBAPIError) -> bool:
        message = str(exc.orig).lower()
        codes = {str(value) for value in getattr(exc.orig, "args", ())}
        return bool(
            {"1205", "1213"} & codes
            or "deadlock" in message
            or "database is locked" in message
            or "lock wait timeout" in message
        )

    async def _write(self, operation: Callable[[AsyncSession], Awaitable[_T]]) -> _T:
        for attempt in range(_MAX_WRITE_ATTEMPTS):
            try:
                async with self.runtime.unit_of_work() as uow:
                    return await operation(uow.session)
            except DBAPIError as exc:
                if attempt + 1 >= _MAX_WRITE_ATTEMPTS or not self._retryable(exc):
                    raise
                await asyncio.sleep(0.02 * (attempt + 1))
        raise AssertionError("bounded subject document retry loop exhausted")

    @staticmethod
    def _document_id(logical_path: str) -> str:
        return "doc_" + hashlib.sha256(logical_path.encode()).hexdigest()

    @staticmethod
    def _version_id(
        *,
        document_id: str,
        parent_version_id: str,
        occurrence_id: str,
        content_hash: str,
        command: AppendSubjectDocumentVersion,
    ) -> str:
        material = canonical_json(
            {
                "document_id": document_id,
                "parent_version_id": parent_version_id,
                "occurrence_id": occurrence_id,
                "content_hash": content_hash,
                "semantic_actor_id": command.semantic_actor_id,
                "semantic_source_id": command.semantic_source_id,
                "occurred_at": _iso(command.occurred_at),
                "recorded_by": command.recorded_by,
                "recorded_source": command.recorded_source,
                "provenance_status": command.provenance_status,
                "byte_fidelity": command.byte_fidelity,
                "encoding": command.encoding,
                "newline_style": command.newline_style,
                "change_context": command.change_context or {},
            }
        )
        return "ver_" + hashlib.sha256(material.encode()).hexdigest()

    @staticmethod
    def _head_event_id(document_id: str, occurrence_id: str) -> str:
        material = canonical_json(
            {"document_id": document_id, "occurrence_id": occurrence_id}
        )
        return "head_" + hashlib.sha256(material.encode()).hexdigest()

    @staticmethod
    def _decode_head(row: Any) -> SubjectDocumentHead | None:
        if row is None:
            return None
        return SubjectDocumentHead(
            document_id=str(row["document_id"]),
            logical_path=str(row["logical_path"]),
            declared_owner=_optional(row["declared_owner"]),
            current_version_id=str(row["current_version_id"] or ""),
            revision=int(row["revision"]),
        )

    @staticmethod
    def _decode_version(row: Any) -> SubjectDocumentVersion:
        content = row["content_bytes"]
        if isinstance(content, memoryview):
            content = content.tobytes()
        return SubjectDocumentVersion(
            version_id=str(row["version_id"]),
            document_id=str(row["document_id"]),
            logical_path=str(row["logical_path"]),
            parent_version_id=str(row["parent_version_id"] or ""),
            occurrence_id=str(row["occurrence_id"]),
            semantic_actor_id=_optional(row["semantic_actor_id"]),
            semantic_source_id=_optional(row["semantic_source_id"]),
            occurred_at=_iso(row["occurred_at"]) or None,
            recorded_by=str(row["recorded_by"]),
            recorded_source=str(row["recorded_source"]),
            recorded_at=_iso(row["recorded_at"]),
            provenance_status=str(row["provenance_status"]),
            content_bytes=bytes(content),
            content_hash=str(row["content_hash"]),
            byte_length=int(row["byte_length"]),
            byte_fidelity=str(row["byte_fidelity"]),
            encoding=_optional(row["encoding"]),
            newline_style=_optional(row["newline_style"]),
            change_context=_json_object(row["change_context_json"]),
        )

    @staticmethod
    def _version_columns(prefix: str = "") -> str:
        qualifier = f"{prefix}." if prefix else ""
        columns = (
            "version_id",
            "document_id",
            "logical_path",
            "parent_version_id",
            "occurrence_id",
            "semantic_actor_id",
            "semantic_source_id",
            "occurred_at",
            "recorded_by",
            "recorded_source",
            "recorded_at",
            "provenance_status",
            "content_bytes",
            "content_hash",
            "byte_length",
            "byte_fidelity",
            "encoding",
            "newline_style",
            "change_context_json",
        )
        return ", ".join(f"{qualifier}{column} AS {column}" for column in columns)

    async def get_head(self, logical_path: str) -> SubjectDocumentHead | None:
        path = normalize_subject_path(logical_path)
        async with self.runtime.unit_of_work() as uow:
            row = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT document_id, logical_path, declared_owner,
                            current_version_id, revision FROM subject_documents
                            WHERE logical_path = :logical_path"""
                        ),
                        {"logical_path": path},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._decode_head(row)

    async def get_version(self, version_id: str) -> SubjectDocumentVersion:
        identity = str(version_id).strip()
        if not identity:
            raise ValueError("version_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            row = (
                (
                    await uow.session.execute(
                        text(
                            f"SELECT {self._version_columns()} "
                            "FROM subject_document_versions "
                            "WHERE version_id = :version_id"
                        ),
                        {"version_id": identity},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SubjectDocumentNotFound(identity)
        return self._decode_version(row)

    async def list_heads(
        self,
        *,
        after_logical_path: str = "",
        limit: int = 100,
    ) -> list[SubjectDocumentHead]:
        cursor = (
            normalize_subject_path(after_logical_path) if after_logical_path else ""
        )
        bounded = min(500, max(0, int(limit)))
        if bounded == 0:
            return []
        async with self.runtime.unit_of_work() as uow:
            rows = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT document_id, logical_path, declared_owner,
                            current_version_id, revision FROM subject_documents
                            WHERE logical_path > :after_logical_path
                            ORDER BY logical_path LIMIT :limit"""
                        ),
                        {"after_logical_path": cursor, "limit": bounded},
                    )
                )
                .mappings()
                .all()
            )
        heads: list[SubjectDocumentHead] = []
        for row in rows:
            head = self._decode_head(row)
            if head is None:  # pragma: no cover - mappings rows are never None
                raise RuntimeError("subject head query returned an empty row")
            heads.append(head)
        return heads

    async def list_current_versions(
        self,
        *,
        after_logical_path: str = "",
        limit: int = 100,
    ) -> list[SubjectDocumentCommit]:
        cursor = (
            normalize_subject_path(after_logical_path) if after_logical_path else ""
        )
        bounded = min(500, max(0, int(limit)))
        if bounded == 0:
            return []
        async with self.runtime.unit_of_work() as uow:
            rows = (
                (
                    await uow.session.execute(
                        text(
                            f"""SELECT
                            d.document_id AS head_document_id,
                            d.logical_path AS head_logical_path,
                            d.declared_owner AS head_declared_owner,
                            d.current_version_id AS head_current_version_id,
                            d.revision AS head_revision,
                            {self._version_columns("v")}
                            FROM subject_documents AS d
                            JOIN subject_document_versions AS v
                              ON v.version_id = d.current_version_id
                            WHERE d.logical_path > :after_logical_path
                            ORDER BY d.logical_path LIMIT :limit"""
                        ),
                        {"after_logical_path": cursor, "limit": bounded},
                    )
                )
                .mappings()
                .all()
            )
        commits: list[SubjectDocumentCommit] = []
        for row in rows:
            head = SubjectDocumentHead(
                document_id=str(row["head_document_id"]),
                logical_path=str(row["head_logical_path"]),
                declared_owner=_optional(row["head_declared_owner"]),
                current_version_id=str(row["head_current_version_id"]),
                revision=int(row["head_revision"]),
            )
            version = self._decode_version(row)
            if (
                version.document_id != head.document_id
                or version.logical_path != head.logical_path
                or version.version_id != head.current_version_id
            ):
                raise SubjectDocumentConflict(
                    f"subject head/current version mismatch: {head.logical_path}"
                )
            commits.append(SubjectDocumentCommit(version=version, head=head))
        return commits

    async def list_history(
        self,
        logical_path: str,
        *,
        after_recorded_at: str = "",
        after_version_id: str = "",
        limit: int = 100,
    ) -> list[SubjectDocumentVersion]:
        path = normalize_subject_path(logical_path)
        bounded = min(500, max(0, int(limit)))
        if bounded == 0:
            return []
        statement = (
            f"SELECT {self._version_columns()} FROM subject_document_versions "
            "WHERE logical_path = :logical_path"
        )
        parameters: dict[str, Any] = {"logical_path": path, "limit": bounded}
        if after_recorded_at:
            parsed = _parse_datetime(after_recorded_at)
            if parsed is None or not after_version_id:
                raise ValueError("history cursor requires valid time and version id")
            statement += " AND (recorded_at > :after_recorded_at OR "
            statement += "(recorded_at = :after_recorded_at "
            statement += "AND version_id > :after_version_id))"
            parameters["after_recorded_at"] = self._bind_time(parsed)
            parameters["after_version_id"] = str(after_version_id)
        statement += " ORDER BY recorded_at, version_id LIMIT :limit"
        async with self.runtime.unit_of_work() as uow:
            rows = (
                (await uow.session.execute(text(statement), parameters))
                .mappings()
                .all()
            )
        return [self._decode_version(row) for row in rows]

    async def append_version(
        self,
        command: AppendSubjectDocumentVersion,
    ) -> SubjectDocumentCommit:
        path = normalize_subject_path(command.logical_path)
        occurrence_id = str(command.occurrence_id).strip()
        recorded_by = str(command.recorded_by).strip()
        recorded_source = str(command.recorded_source).strip()
        provenance = str(command.provenance_status).strip()
        fidelity = str(command.byte_fidelity).strip()
        if not occurrence_id or len(occurrence_id) > 255:
            raise ValueError("subject occurrence_id must be 1..255 characters")
        if not recorded_by or not recorded_source or not provenance or not fidelity:
            raise ValueError("subject provenance and recording identity are required")
        expected_revision = int(command.expected_revision)
        if expected_revision < 0:
            raise ValueError("expected_revision must not be negative")
        expected_head = str(command.expected_head_version_id or "")
        content = bytes(command.content_bytes)
        content_hash = hashlib.sha256(content).hexdigest()
        document_id = self._document_id(path)
        version_id = self._version_id(
            document_id=document_id,
            parent_version_id=expected_head,
            occurrence_id=occurrence_id,
            content_hash=content_hash,
            command=command,
        )
        head_event_id = self._head_event_id(document_id, occurrence_id)
        context_json = canonical_json(command.change_context or {})

        async def operation(session: AsyncSession) -> SubjectDocumentCommit:
            document_row = (
                (
                    await session.execute(
                        text(
                            """SELECT document_id, logical_path, declared_owner,
                            current_version_id, revision FROM subject_documents
                            WHERE logical_path = :logical_path"""
                            + self._for_update
                        ),
                        {"logical_path": path},
                    )
                )
                .mappings()
                .one_or_none()
            )
            existing_version = (
                (
                    await session.execute(
                        text(
                            f"SELECT {self._version_columns()} "
                            "FROM subject_document_versions "
                            "WHERE document_id = :document_id "
                            "AND occurrence_id = :occurrence_id" + self._for_update
                        ),
                        {
                            "document_id": document_id,
                            "occurrence_id": occurrence_id,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing_version is not None:
                decoded = self._decode_version(existing_version)
                if decoded.version_id != version_id:
                    raise SubjectDocumentConflict(
                        f"subject occurrence identity conflict: {occurrence_id}"
                    )
                head = self._decode_head(document_row)
                if head is None:
                    raise SubjectDocumentConflict(
                        "version exists without document head"
                    )
                return SubjectDocumentCommit(version=decoded, head=head)

            current = self._decode_head(document_row)
            if current is None:
                if expected_revision != 0 or expected_head:
                    raise SubjectDocumentConflict("new document requires empty head")
                try:
                    await session.execute(
                        text(
                            """INSERT INTO subject_documents (
                                document_id, logical_path, declared_owner,
                                current_version_id, revision
                            ) VALUES (
                                :document_id, :logical_path, :declared_owner, '', 0
                            )"""
                        ),
                        {
                            "document_id": document_id,
                            "logical_path": path,
                            "declared_owner": command.declared_owner,
                        },
                    )
                except IntegrityError as exc:
                    raise SubjectDocumentConflict(
                        f"concurrent subject document creation: {path}"
                    ) from exc
                current = SubjectDocumentHead(
                    document_id=document_id,
                    logical_path=path,
                    declared_owner=command.declared_owner,
                    current_version_id="",
                    revision=0,
                )
            if (
                current.revision != expected_revision
                or current.current_version_id != expected_head
            ):
                raise SubjectDocumentConflict(
                    f"subject head CAS failed for {path}: expected "
                    f"({expected_revision}, {expected_head!r}), actual "
                    f"({current.revision}, {current.current_version_id!r})"
                )
            if (
                command.declared_owner is not None
                and current.declared_owner != command.declared_owner
            ):
                raise SubjectDocumentConflict("declared subject owner is immutable")

            database_now = await self._database_now(session)
            await session.execute(
                text(
                    """INSERT INTO subject_document_versions (
                        version_id, document_id, logical_path, parent_version_id,
                        occurrence_id, semantic_actor_id, semantic_source_id,
                        occurred_at, recorded_by, recorded_source, recorded_at,
                        provenance_status, content_bytes, content_hash, byte_length,
                        byte_fidelity, encoding, newline_style, change_context_json
                    ) VALUES (
                        :version_id, :document_id, :logical_path, :parent_version_id,
                        :occurrence_id, :semantic_actor_id, :semantic_source_id,
                        :occurred_at, :recorded_by, :recorded_source, :recorded_at,
                        :provenance_status, :content_bytes, :content_hash, :byte_length,
                        :byte_fidelity, :encoding, :newline_style, :change_context_json
                    )"""
                ),
                {
                    "version_id": version_id,
                    "document_id": document_id,
                    "logical_path": path,
                    "parent_version_id": expected_head,
                    "occurrence_id": occurrence_id,
                    "semantic_actor_id": command.semantic_actor_id,
                    "semantic_source_id": command.semantic_source_id,
                    "occurred_at": self._bind_time(command.occurred_at),
                    "recorded_by": recorded_by,
                    "recorded_source": recorded_source,
                    "recorded_at": self._bind_time(database_now),
                    "provenance_status": provenance,
                    "content_bytes": content,
                    "content_hash": content_hash,
                    "byte_length": len(content),
                    "byte_fidelity": fidelity,
                    "encoding": command.encoding,
                    "newline_style": command.newline_style,
                    "change_context_json": context_json,
                },
            )
            authority_epoch = (
                self.runtime.authority_token.authority_epoch
                if self.runtime.authority_token is not None
                else int(self.runtime.writer_epoch)
            )
            await session.execute(
                text(
                    """INSERT INTO subject_document_head_events (
                        head_event_id, document_id, previous_version_id,
                        next_version_id, occurrence_id, actor_id, source_id,
                        occurred_at, authority_epoch, change_context_json
                    ) VALUES (
                        :head_event_id, :document_id, :previous_version_id,
                        :next_version_id, :occurrence_id, :actor_id, :source_id,
                        :occurred_at, :authority_epoch, :change_context_json
                    )"""
                ),
                {
                    "head_event_id": head_event_id,
                    "document_id": document_id,
                    "previous_version_id": expected_head,
                    "next_version_id": version_id,
                    "occurrence_id": occurrence_id,
                    "actor_id": recorded_by,
                    "source_id": recorded_source,
                    "occurred_at": self._bind_time(database_now),
                    "authority_epoch": authority_epoch,
                    "change_context_json": context_json,
                },
            )
            updated = await session.execute(
                text(
                    """UPDATE subject_documents SET
                        current_version_id = :version_id,
                        revision = :next_revision
                    WHERE document_id = :document_id
                      AND current_version_id = :expected_head
                      AND revision = :expected_revision"""
                ),
                {
                    "version_id": version_id,
                    "next_revision": expected_revision + 1,
                    "document_id": document_id,
                    "expected_head": expected_head,
                    "expected_revision": expected_revision,
                },
            )
            if updated.rowcount != 1:
                raise SubjectDocumentConflict(f"concurrent subject head update: {path}")
            await session.execute(
                text(
                    """INSERT INTO subject_projection_outbox (
                        head_event_id, document_id, logical_path, version_id,
                        content_hash, state, attempt_count, created_at,
                        confirmed_at, last_error
                    ) VALUES (
                        :head_event_id, :document_id, :logical_path, :version_id,
                        :content_hash, 'pending', 0, :created_at,
                        :confirmed_at, ''
                    )"""
                ),
                {
                    "head_event_id": head_event_id,
                    "document_id": document_id,
                    "logical_path": path,
                    "version_id": version_id,
                    "content_hash": content_hash,
                    "created_at": self._bind_time(database_now),
                    "confirmed_at": (None if self.backend == BackendKind.MYSQL else ""),
                },
            )
            version = SubjectDocumentVersion(
                version_id=version_id,
                document_id=document_id,
                logical_path=path,
                parent_version_id=expected_head,
                occurrence_id=occurrence_id,
                semantic_actor_id=command.semantic_actor_id,
                semantic_source_id=command.semantic_source_id,
                occurred_at=_iso(command.occurred_at) or None,
                recorded_by=recorded_by,
                recorded_source=recorded_source,
                recorded_at=database_now.isoformat(),
                provenance_status=provenance,
                content_bytes=content,
                content_hash=content_hash,
                byte_length=len(content),
                byte_fidelity=fidelity,
                encoding=command.encoding,
                newline_style=command.newline_style,
                change_context=dict(command.change_context or {}),
            )
            head = SubjectDocumentHead(
                document_id=document_id,
                logical_path=path,
                declared_owner=current.declared_owner,
                current_version_id=version_id,
                revision=expected_revision + 1,
            )
            return SubjectDocumentCommit(version=version, head=head)

        return await self._write(operation)

    @staticmethod
    def _decode_projection(row: Any) -> SubjectProjectionTask:
        return SubjectProjectionTask(
            outbox_id=int(row["outbox_id"]),
            head_event_id=str(row["head_event_id"]),
            document_id=str(row["document_id"]),
            logical_path=str(row["logical_path"]),
            version_id=str(row["version_id"]),
            content_hash=str(row["content_hash"]),
            state=str(row["state"]),
            attempt_count=int(row["attempt_count"]),
            lease_owner=str(row["lease_owner"] or ""),
            lease_until=_iso(row["lease_until"]),
            revision=int(row["revision"]),
        )

    @staticmethod
    def _projection_columns() -> str:
        return """outbox_id, head_event_id, document_id, logical_path,
        version_id, content_hash, state, attempt_count,
        lease_owner, lease_until, revision"""

    async def claim_projection(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> SubjectProjectionTask | None:
        worker = str(worker_id).strip()
        if not worker or len(worker) > 255:
            raise ValueError("projection worker_id must be 1..255 characters")
        if int(lease_seconds) <= 0:
            raise ValueError("projection lease_seconds must be positive")

        async def operation(session: AsyncSession) -> SubjectProjectionTask | None:
            database_now = await self._database_now(session)
            lease_available = (
                "lease_until IS NULL"
                if self.backend == BackendKind.MYSQL
                else "(lease_until IS NULL OR lease_until = '')"
            )
            row = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {self._projection_columns()}
                            FROM subject_projection_outbox
                            WHERE state = 'pending'
                              AND ({lease_available}
                                   OR lease_until <= :database_now)
                            ORDER BY outbox_id LIMIT 1{self._for_update}"""
                        ),
                        {"database_now": self._bind_time(database_now)},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            current = self._decode_projection(row)
            lease_until = database_now + timedelta(seconds=int(lease_seconds))
            updated = await session.execute(
                text(
                    """UPDATE subject_projection_outbox SET
                    lease_owner = :worker_id, lease_until = :lease_until,
                    attempt_count = attempt_count + 1, revision = revision + 1
                    WHERE outbox_id = :outbox_id AND state = 'pending'
                      AND revision = :revision"""
                ),
                {
                    "worker_id": worker,
                    "lease_until": self._bind_time(lease_until),
                    "outbox_id": current.outbox_id,
                    "revision": current.revision,
                },
            )
            if updated.rowcount != 1:
                raise SubjectDocumentConflict("projection claim CAS failed")
            claimed = (
                (
                    await session.execute(
                        text(
                            f"SELECT {self._projection_columns()} "
                            "FROM subject_projection_outbox "
                            "WHERE outbox_id = :outbox_id"
                        ),
                        {"outbox_id": current.outbox_id},
                    )
                )
                .mappings()
                .one()
            )
            return self._decode_projection(claimed)

        return await self._write(operation)

    async def confirm_projection(
        self,
        task: SubjectProjectionTask,
        *,
        worker_id: str,
    ) -> None:
        worker = str(worker_id).strip()

        async def operation(session: AsyncSession) -> None:
            database_now = await self._database_now(session)
            updated = await session.execute(
                text(
                    """UPDATE subject_projection_outbox SET
                    state = 'confirmed', confirmed_at = :confirmed_at,
                    lease_owner = '', lease_until = :empty_lease,
                    last_error = '', revision = revision + 1
                    WHERE outbox_id = :outbox_id AND state = 'pending'
                      AND lease_owner = :worker_id AND revision = :revision
                      AND version_id = :version_id
                      AND content_hash = :content_hash"""
                ),
                {
                    "confirmed_at": self._bind_time(database_now),
                    "empty_lease": (None if self.backend == BackendKind.MYSQL else ""),
                    "outbox_id": task.outbox_id,
                    "worker_id": worker,
                    "revision": task.revision,
                    "version_id": task.version_id,
                    "content_hash": task.content_hash,
                },
            )
            if updated.rowcount != 1:
                raise SubjectDocumentConflict("projection confirmation CAS failed")

        await self._write(operation)

    async def fail_projection(
        self,
        task: SubjectProjectionTask,
        *,
        worker_id: str,
        error: str,
    ) -> None:
        worker = str(worker_id).strip()
        detail = str(error).strip()[:4096]
        if not detail:
            raise ValueError("projection failure detail must not be empty")

        async def operation(session: AsyncSession) -> None:
            updated = await session.execute(
                text(
                    """UPDATE subject_projection_outbox SET
                    state = 'failed', lease_owner = '', lease_until = :empty_lease,
                    last_error = :last_error, revision = revision + 1
                    WHERE outbox_id = :outbox_id AND state = 'pending'
                      AND lease_owner = :worker_id AND revision = :revision
                      AND version_id = :version_id"""
                ),
                {
                    "empty_lease": (None if self.backend == BackendKind.MYSQL else ""),
                    "last_error": detail,
                    "outbox_id": task.outbox_id,
                    "worker_id": worker,
                    "revision": task.revision,
                    "version_id": task.version_id,
                },
            )
            if updated.rowcount != 1:
                raise SubjectDocumentConflict("projection failure CAS failed")

        await self._write(operation)

    async def health_snapshot(self) -> dict[str, Any]:
        async with self.runtime.unit_of_work() as uow:
            documents = int(
                await uow.session.scalar(text("SELECT COUNT(*) FROM subject_documents"))
                or 0
            )
            versions = int(
                await uow.session.scalar(
                    text("SELECT COUNT(*) FROM subject_document_versions")
                )
                or 0
            )
            outbox_rows = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT state, COUNT(*) AS total
                            FROM subject_projection_outbox GROUP BY state"""
                        )
                    )
                )
                .mappings()
                .all()
            )
        return {
            "status": "healthy",
            "backend": self.backend.value,
            "backend_identity": self.runtime.backend_identity,
            "documents": documents,
            "versions": versions,
            "projection_outbox": {
                str(row["state"]): int(row["total"]) for row in outbox_rows
            },
        }


class LocalSubjectDocumentStore(SQLSubjectDocumentStore):
    """SQLite-backed subject document history."""


class MySQLSubjectDocumentStore(SQLSubjectDocumentStore):
    """MySQL-backed subject document history."""


__all__ = [
    "LocalSubjectDocumentStore",
    "MySQLSubjectDocumentStore",
    "SQLSubjectDocumentStore",
    "normalize_subject_path",
]
