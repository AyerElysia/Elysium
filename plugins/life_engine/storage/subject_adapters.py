"""Fenced local/MySQL adapters for exact-byte subject document history."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage import canonical_json

from ._write_base import run_write_attempts
from .authority import MySQLAuthorityRegistry
from .contracts import StorageBackendRuntime
from .models import BackendKind
from .subject_contracts import (
    SUBJECT_AUTHORITY_PATHS,
    AcceptSubjectCandidate,
    AppendSubjectDocumentVersion,
    SubjectAuthorityActorInactive,
    SubjectAuthorityCommit,
    SubjectAuthorityConflict,
    SubjectAuthorityEvidenceError,
    SubjectAuthoritySnapshot,
    SubjectDocumentCommit,
    SubjectDocumentConflict,
    SubjectDocumentHead,
    SubjectDocumentMutation,
    SubjectDocumentMutationCommit,
    SubjectDocumentNotFound,
    SubjectDocumentOperation,
    SubjectDocumentPathBinding,
    SubjectDocumentVersion,
    SubjectProjectionTask,
    subject_authority_logical_path,
    subject_revision_from_contents,
)

_T = TypeVar("_T")
_MAX_SUBJECT_CANDIDATE_BYTES = 4 * 1024 * 1024
_LOCAL_WRITE_GATE_TIMEOUT_SECONDS = 30.0


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


def _require_hex_digest(value: Any, *, field: str) -> str:
    digest = str(value).strip().lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(f"{field} must be a 64-hex digest")
    return digest


def _required_identity(value: Any, *, field: str, maximum: int = 255) -> str:
    identity = str(value).strip()
    if not identity or len(identity) > maximum:
        raise ValueError(f"{field} must be 1..{maximum} characters")
    return identity


def _decode_base64(value: Any, *, field: str) -> bytes:
    try:
        return base64.b64decode(str(value), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SubjectAuthorityEvidenceError(f"invalid {field} base64") from exc


def _subject_text_format(content: bytes) -> tuple[str, str | None]:
    encoding = "utf-8-sig" if content.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        content.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ValueError("accepted subject content must be valid UTF-8") from exc
    crlf = content.count(b"\r\n")
    lone_lf = content.count(b"\n") - crlf
    lone_cr = content.count(b"\r") - crlf
    styles = [
        name
        for name, count in (("crlf", crlf), ("lf", lone_lf), ("cr", lone_cr))
        if count
    ]
    return encoding, styles[0] if len(styles) == 1 else ("mixed" if styles else None)


def _subject_head_change_marker(
    heads: dict[str, tuple[str, int]],
) -> str:
    marker = hashlib.sha256()
    for path in SUBJECT_AUTHORITY_PATHS:
        head = heads.get(path)
        if head is None:
            raise SubjectAuthorityEvidenceError(
                f"subject authority head is missing: {path}"
            )
        version_id, revision = head
        if not version_id or revision <= 0:
            raise SubjectAuthorityEvidenceError(
                f"subject authority head is invalid: {path}"
            )
        marker.update(path.encode("utf-8"))
        marker.update(b"\0")
        marker.update(version_id.encode("ascii"))
        marker.update(b"\0")
        marker.update(revision.to_bytes(8, "big"))
    return marker.hexdigest()


class SQLSubjectDocumentStore:
    """One subject ledger bound to a coherent storage runtime."""

    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if not runtime.enabled or runtime.engine is None:
            raise RuntimeError("subject document adapter requires enabled storage")
        self.runtime = runtime
        self.backend = runtime.backend
        self._local_write_gate = asyncio.Lock()
        self._projection_fence_owner: asyncio.Task[Any] | None = None

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

    async def _write(
        self,
        operation: Callable[[AsyncSession], Awaitable[_T]],
    ) -> _T:
        async def _attempt() -> _T:
            async with self.runtime.unit_of_work() as uow:
                await self._reserve_document_writer(uow.session)
                return await operation(uow.session)

        async with self._local_write_scope():
            return await run_write_attempts(
                _attempt,
                exhaustion_message="bounded subject document retry loop exhausted",
            )

    async def _reserve_document_writer(self, session: AsyncSession) -> None:
        """Serialize path writes without relying on READ COMMITTED gap locks.

        MySQL uses an existing immutable schema-v5 control row as a transaction
        mutex. This serializes this subject domain only. Older writers that do
        not acquire it must be stopped before enabling v5 file lifecycle tools.
        """

        if self.backend == BackendKind.LOCAL:
            await session.execute(text("BEGIN IMMEDIATE"))
            return
        version = await session.scalar(text(
            """SELECT version FROM subject_document_schema_migrations
            WHERE version = 5 FOR UPDATE"""
        ))
        if version != 5:
            raise SubjectDocumentConflict("subject schema v5 writer guard is missing")

    @asynccontextmanager
    async def _local_write_scope(self) -> AsyncIterator[None]:
        if self._projection_fence_owner is asyncio.current_task():
            raise RuntimeError(
                "subject writes and nested fences are forbidden inside "
                "a workspace namespace/projection fence; confirm/fail after leaving it"
            )
        try:
            async with asyncio.timeout(_LOCAL_WRITE_GATE_TIMEOUT_SECONDS):
                await self._local_write_gate.acquire()
        except TimeoutError as exc:
            raise SubjectDocumentConflict(
                "subject write gate deadline exceeded"
            ) from exc
        try:
            yield
        finally:
            self._local_write_gate.release()

    @asynccontextmanager
    async def workspace_projection_fence(self) -> AsyncIterator[None]:
        """Hold the LOCAL database writer reservation across exact disk changes.

        SQLite's configured busy timeout bounds acquisition across adapters;
        the coroutine-side write gate bounds and serializes local contenders.
        No filesystem action is retried here. Exceptions/cancellation unwind
        the existing fenced UoW and release both reservation and write gate.
        """

        if self.backend != BackendKind.LOCAL:
            raise RuntimeError("workspace projection fence is LOCAL-only")
        async with self.workspace_namespace_fence():
            yield

    @asynccontextmanager
    async def workspace_namespace_fence(self) -> AsyncIterator[None]:
        """Fence path publication on either backend; perform no filesystem I/O."""

        async with self._local_write_scope(), self.runtime.unit_of_work() as uow:
            await self._reserve_document_writer(uow.session)
            # UoW validates again at commit, but external publication also
            # needs authority checked and held before the caller touches disk.
            if self.runtime._write_fence is not None:
                await self.runtime._write_fence(uow.session)
            elif self.backend == BackendKind.MYSQL:
                registry, token = self.runtime.authority_registry, self.runtime.authority_token
                if not isinstance(registry, MySQLAuthorityRegistry) or token is None:
                    raise RuntimeError("MySQL namespace publication lacks writer authority")
                await registry.validate_in_transaction(
                    await uow.session.connection(), token,
                )
            self._projection_fence_owner = asyncio.current_task()
            try:
                yield
            finally:
                self._projection_fence_owner = None

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
            binding_revision=int(row.get("binding_revision", 0)),
            deleted=bool(row.get("is_deleted", False)),
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

    @staticmethod
    def _authority_occurrence_id(decision_occurrence_id: str) -> str:
        digest = hashlib.sha256(decision_occurrence_id.encode("utf-8")).hexdigest()
        return f"subject_authority:{digest}"

    @staticmethod
    def _authority_command_material(
        command: AcceptSubjectCandidate,
    ) -> dict[str, Any]:
        candidate_id = _required_identity(command.candidate_id, field="candidate_id")
        candidate_revision = int(command.candidate_revision)
        if candidate_revision <= 0:
            raise ValueError("candidate_revision must be positive")
        candidate_occurrence = _required_identity(
            command.candidate_occurrence_id,
            field="candidate_occurrence_id",
        )
        decision_occurrence = _required_identity(
            command.decision_occurrence_id,
            field="decision_occurrence_id",
        )
        if candidate_occurrence == decision_occurrence:
            raise ValueError("candidate and decision occurrences must differ")
        actor = _required_identity(
            command.actor_consciousness_instance_id,
            field="actor_consciousness_instance_id",
        )
        target_path = str(command.target_path).strip()
        if target_path not in SUBJECT_AUTHORITY_PATHS:
            raise ValueError("target_path must be SOUL.md, USER.md, or MEMORY.md")
        accepted_content = bytes(command.accepted_content_bytes)
        if len(accepted_content) > _MAX_SUBJECT_CANDIDATE_BYTES:
            raise ValueError("accepted content exceeds the explicit storage limit")
        accepted_hash = _require_hex_digest(
            command.accepted_content_sha256,
            field="accepted_content_sha256",
        )
        if hashlib.sha256(accepted_content).hexdigest() != accepted_hash:
            raise ValueError("accepted content hash mismatch")
        encoding, _ = _subject_text_format(accepted_content)
        decoded = accepted_content.decode(encoding)
        if target_path == "SOUL.md" and not decoded.strip():
            raise ValueError("SOUL.md cannot become empty")
        occurred_at = _iso(command.occurred_at)
        if not occurred_at:
            raise ValueError("occurred_at must be an ISO timestamp")
        return {
            "candidate_id": candidate_id,
            "candidate_revision": candidate_revision,
            "candidate_sha256": _require_hex_digest(
                command.candidate_sha256,
                field="candidate_sha256",
            ),
            "candidate_occurrence_id": candidate_occurrence,
            "decision_occurrence_id": decision_occurrence,
            "actor_consciousness_instance_id": actor,
            "expected_subject_revision": _require_hex_digest(
                command.expected_subject_revision,
                field="expected_subject_revision",
            ),
            "target_path": target_path,
            "accepted_content_sha256": accepted_hash,
            "occurred_at": occurred_at,
        }

    @staticmethod
    def _authority_command_sha256(material: dict[str, Any]) -> str:
        return hashlib.sha256(canonical_json(material).encode()).hexdigest()

    @staticmethod
    def _authority_commit_from_row(
        row: Any,
        *,
        idempotent_replay: bool,
    ) -> SubjectAuthorityCommit:
        return SubjectAuthorityCommit(
            authority_occurrence_id=str(row["authority_occurrence_id"]),
            candidate_id=str(row["candidate_id"]),
            decision_occurrence_id=str(row["decision_occurrence_id"]),
            actor_consciousness_instance_id=str(row["actor_consciousness_instance_id"]),
            previous_subject_revision=str(row["previous_subject_revision"]),
            new_subject_revision=str(row["new_subject_revision"]),
            document_version_id=str(row["document_version_id"]),
            document_revision=int(row["document_revision"]),
            accepted_content_sha256=str(row["accepted_content_sha256"]),
            idempotent_replay=idempotent_replay,
        )

    async def _subject_authority_state(
        self,
        session: AsyncSession,
        *,
        lock: bool,
    ) -> tuple[dict[str, SubjectDocumentCommit], str]:
        logical_paths = {
            path: subject_authority_logical_path(path)
            for path in SUBJECT_AUTHORITY_PATHS
        }
        rows = (
            (
                await session.execute(
                    text(
                        f"""SELECT
                        d.document_id AS head_document_id,
                        d.logical_path AS head_logical_path,
                        d.declared_owner AS head_declared_owner,
                        d.current_version_id AS head_current_version_id,
                        d.revision AS head_revision,
                        d.binding_revision AS head_binding_revision,
                        {self._version_columns("v")}
                        FROM subject_documents AS d
                        JOIN subject_document_path_bindings AS b
                          ON b.document_id = d.document_id
                          AND b.logical_path = d.logical_path
                          AND b.revision = d.binding_revision
                        JOIN subject_document_versions AS v
                          ON v.version_id = d.current_version_id
                        WHERE d.logical_path IN (:soul, :user, :memory)
                          AND d.is_deleted = 0
                        ORDER BY d.logical_path"""
                        + (self._for_update if lock else "")
                    ),
                    {
                        "soul": logical_paths["SOUL.md"],
                        "user": logical_paths["USER.md"],
                        "memory": logical_paths["MEMORY.md"],
                    },
                )
            )
            .mappings()
            .all()
        )
        by_logical = {str(row["head_logical_path"]): row for row in rows}
        state: dict[str, SubjectDocumentCommit] = {}
        contents: dict[Any, bytes] = {}
        for path in SUBJECT_AUTHORITY_PATHS:
            logical_path = logical_paths[path]
            row = by_logical.get(logical_path)
            if row is None:
                raise SubjectAuthorityEvidenceError(
                    f"subject authority head is missing: {path}"
                )
            head = SubjectDocumentHead(
                document_id=str(row["head_document_id"]),
                logical_path=logical_path,
                declared_owner=_optional(row["head_declared_owner"]),
                current_version_id=str(row["head_current_version_id"]),
                revision=int(row["head_revision"]),
                binding_revision=int(row["head_binding_revision"]),
            )
            version = self._decode_version(row)
            if head.declared_owner != "elysia":
                raise SubjectAuthorityEvidenceError(
                    f"subject authority owner mismatch: {path}"
                )
            if (
                version.document_id != head.document_id
                or version.logical_path != logical_path
                or version.version_id != head.current_version_id
                or hashlib.sha256(version.content_bytes).hexdigest()
                != version.content_hash
            ):
                raise SubjectAuthorityEvidenceError(
                    f"subject authority head/version mismatch: {path}"
                )
            state[path] = SubjectDocumentCommit(version=version, head=head)
            contents[path] = version.content_bytes
        return state, subject_revision_from_contents(contents)

    async def current_subject_revision(self) -> str:
        """Return one coherent exact-byte revision for all three authorities."""

        async with self.runtime.unit_of_work() as uow:
            _, revision = await self._subject_authority_state(
                uow.session,
                lock=False,
            )
        return revision

    async def current_subject_change_marker(self) -> str:
        """Return a lightweight marker for the current three authority heads.

        This marker detects changes without transferring or decoding subject
        content. ``read_subject_authority()`` remains the only path that mints
        and validates the canonical exact-byte revision.
        """

        logical_paths = {
            path: subject_authority_logical_path(path)
            for path in SUBJECT_AUTHORITY_PATHS
        }
        async with self.runtime.unit_of_work() as uow:
            rows = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT logical_path, current_version_id, revision
                            FROM subject_documents
                            WHERE logical_path IN (:soul, :user, :memory)
                            ORDER BY logical_path"""
                        ),
                        {
                            "soul": logical_paths["SOUL.md"],
                            "user": logical_paths["USER.md"],
                            "memory": logical_paths["MEMORY.md"],
                        },
                    )
                )
                .mappings()
                .all()
            )
        by_logical = {str(row["logical_path"]): row for row in rows}
        heads: dict[str, tuple[str, int]] = {}
        for path in SUBJECT_AUTHORITY_PATHS:
            logical_path = logical_paths[path]
            row = by_logical.get(logical_path)
            if row is None:
                raise SubjectAuthorityEvidenceError(
                    f"subject authority head is missing: {path}"
                )
            heads[path] = (
                str(row["current_version_id"] or ""),
                int(row["revision"]),
            )
        return _subject_head_change_marker(heads)

    async def read_subject_authority(self) -> SubjectAuthoritySnapshot:
        """Read all three authority head/version pairs in one consistent snapshot."""

        async with self.runtime.unit_of_work() as uow:
            state, revision = await self._subject_authority_state(
                uow.session,
                lock=False,
            )
        heads = {
            path: (
                state[path].head.current_version_id,
                state[path].head.revision,
            )
            for path in SUBJECT_AUTHORITY_PATHS
        }
        return SubjectAuthoritySnapshot(
            commits={path: state[path] for path in SUBJECT_AUTHORITY_PATHS},
            revision=revision,
            change_marker=_subject_head_change_marker(heads),
        )

    @staticmethod
    def _validate_learning_event_integrity(row: Any) -> dict[str, Any]:
        provenance = _json_object(row["provenance_json"])
        payload = _json_object(row["payload_json"])
        material = {
            "occurrence_id": str(row["occurrence_id"]),
            "event_kind": str(row["event_kind"]),
            "occurred_at": _iso(row["occurred_at"]),
            "source": str(row["source"]),
            "actor_consciousness_instance_id": str(
                row["actor_consciousness_instance_id"] or ""
            ),
            "subject_revision": str(row["subject_revision"] or "").lower(),
            "provenance": provenance,
            "payload": payload,
        }
        calculated = hashlib.sha256(canonical_json(material).encode()).hexdigest()
        if calculated != str(row["event_sha256"]):
            raise SubjectAuthorityEvidenceError(
                f"learning occurrence hash mismatch: {row['occurrence_id']}"
            )
        return material

    async def _validate_active_actor(
        self,
        session: AsyncSession,
        *,
        actor: str,
        database_now: datetime,
    ) -> None:
        row = (
            (
                await session.execute(
                    text(
                        """SELECT instance_id, status, lease_expires_at,
                        lease_duration_seconds FROM consciousness_presence
                        WHERE instance_id = :instance_id"""
                        + self._for_update
                    ),
                    {"instance_id": actor},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or str(row["status"]) != "active":
            raise SubjectAuthorityActorInactive(actor)
        if row["lease_duration_seconds"] is not None:
            expiry = _parse_datetime(row["lease_expires_at"])
            if expiry is None or expiry <= database_now:
                raise SubjectAuthorityActorInactive(f"{actor}: active lease is expired")

    async def _validate_learning_evidence(
        self,
        session: AsyncSession,
        *,
        material: dict[str, Any],
        accepted_content: bytes,
    ) -> None:
        rows = (
            (
                await session.execute(
                    text(
                        """SELECT occurrence_id, event_kind, occurred_at,
                        source, actor_consciousness_instance_id,
                        subject_revision, provenance_json, payload_json,
                        event_sha256 FROM learning_events
                        WHERE occurrence_id IN (:candidate_occurrence,
                                                :decision_occurrence)
                        ORDER BY occurrence_id"""
                        + self._for_update
                    ),
                    {
                        "candidate_occurrence": material["candidate_occurrence_id"],
                        "decision_occurrence": material["decision_occurrence_id"],
                    },
                )
            )
            .mappings()
            .all()
        )
        events = {
            str(row["occurrence_id"]): self._validate_learning_event_integrity(row)
            for row in rows
        }
        candidate = events.get(str(material["candidate_occurrence_id"]))
        decision = events.get(str(material["decision_occurrence_id"]))
        if candidate is None or candidate["event_kind"] != "candidate.proposed":
            raise SubjectAuthorityEvidenceError(
                "immutable candidate occurrence is missing"
            )
        if decision is None or decision["event_kind"] != "candidate.accept_requested":
            raise SubjectAuthorityEvidenceError(
                "immutable accept decision occurrence is missing"
            )
        candidate_payload = dict(candidate["payload"])
        decision_payload = dict(decision["payload"])
        candidate_bytes = _decode_base64(
            candidate_payload.get("candidate_content_base64", ""),
            field="candidate content",
        )
        if not all(
            (
                candidate["subject_revision"] == material["expected_subject_revision"],
                str(candidate_payload.get("candidate_id", ""))
                == material["candidate_id"],
                int(candidate_payload.get("candidate_revision", 0))
                == material["candidate_revision"],
                str(candidate_payload.get("candidate_sha256", ""))
                == material["candidate_sha256"],
                str(candidate_payload.get("target_path", ""))
                == material["target_path"],
                hashlib.sha256(candidate_bytes).hexdigest()
                == material["candidate_sha256"],
            )
        ):
            raise SubjectAuthorityEvidenceError(
                "candidate occurrence does not match the authority command"
            )
        decision_bytes = _decode_base64(
            decision_payload.get("accepted_content_base64", ""),
            field="accepted content",
        )
        if not all(
            (
                decision["source"] == "learning.subject_decision",
                decision["actor_consciousness_instance_id"]
                == material["actor_consciousness_instance_id"],
                decision["subject_revision"] == material["expected_subject_revision"],
                decision["occurred_at"] == material["occurred_at"],
                str(decision_payload.get("decision_kind", "")) == "accept_requested",
                str(decision_payload.get("candidate_id", ""))
                == material["candidate_id"],
                int(decision_payload.get("candidate_revision", 0))
                == material["candidate_revision"],
                str(decision_payload.get("candidate_sha256", ""))
                == material["candidate_sha256"],
                str(decision_payload.get("candidate_occurrence_id", ""))
                == material["candidate_occurrence_id"],
                str(decision_payload.get("target_path", "")) == material["target_path"],
                str(decision_payload.get("accepted_content_sha256", ""))
                == material["accepted_content_sha256"],
                decision_bytes == accepted_content,
                hashlib.sha256(decision_bytes).hexdigest()
                == material["accepted_content_sha256"],
            )
        ):
            raise SubjectAuthorityEvidenceError(
                "decision occurrence does not match the authority command"
            )

    @staticmethod
    def _authority_decision_columns() -> str:
        return """decision_occurrence_id, authority_occurrence_id,
        candidate_id, candidate_revision, candidate_sha256,
        candidate_occurrence_id, actor_consciousness_instance_id,
        expected_subject_revision, target_path, accepted_content_sha256,
        occurred_at, previous_subject_revision, new_subject_revision,
        document_version_id, document_revision, command_sha256, committed_at"""

    async def _append_authority_version(
        self,
        session: AsyncSession,
        *,
        current: SubjectDocumentCommit,
        material: dict[str, Any],
        content: bytes,
        previous_subject_revision: str,
        database_now: datetime,
    ) -> SubjectDocumentCommit:
        authority_occurrence = self._authority_occurrence_id(
            str(material["decision_occurrence_id"])
        )
        encoding, newline_style = _subject_text_format(content)
        command = AppendSubjectDocumentVersion(
            logical_path=current.head.logical_path,
            expected_revision=current.head.revision,
            expected_head_version_id=current.head.current_version_id,
            content_bytes=content,
            occurrence_id=authority_occurrence,
            recorded_by=str(material["actor_consciousness_instance_id"]),
            recorded_source="learning.subject_authority",
            declared_owner="elysia",
            semantic_actor_id=str(material["actor_consciousness_instance_id"]),
            semantic_source_id=str(material["decision_occurrence_id"]),
            occurred_at=str(material["occurred_at"]),
            provenance_status="complete",
            byte_fidelity="exact_bytes",
            encoding=encoding,
            newline_style=newline_style,
            change_context={
                "operation": "accept_learning_subject_candidate",
                "candidate_id": material["candidate_id"],
                "candidate_revision": material["candidate_revision"],
                "candidate_sha256": material["candidate_sha256"],
                "candidate_occurrence_id": material["candidate_occurrence_id"],
                "decision_occurrence_id": material["decision_occurrence_id"],
                "previous_subject_revision": previous_subject_revision,
            },
        )
        content_hash = hashlib.sha256(content).hexdigest()
        version_id = self._version_id(
            document_id=current.head.document_id,
            parent_version_id=current.head.current_version_id,
            occurrence_id=authority_occurrence,
            content_hash=content_hash,
            command=command,
        )
        head_event_id = self._head_event_id(
            current.head.document_id,
            authority_occurrence,
        )
        context_json = canonical_json(command.change_context or {})
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
                "document_id": current.head.document_id,
                "logical_path": current.head.logical_path,
                "parent_version_id": current.head.current_version_id,
                "occurrence_id": authority_occurrence,
                "semantic_actor_id": command.semantic_actor_id,
                "semantic_source_id": command.semantic_source_id,
                "occurred_at": self._bind_time(command.occurred_at),
                "recorded_by": command.recorded_by,
                "recorded_source": command.recorded_source,
                "recorded_at": self._bind_time(database_now),
                "provenance_status": command.provenance_status,
                "content_bytes": content,
                "content_hash": content_hash,
                "byte_length": len(content),
                "byte_fidelity": command.byte_fidelity,
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
                "document_id": current.head.document_id,
                "previous_version_id": current.head.current_version_id,
                "next_version_id": version_id,
                "occurrence_id": authority_occurrence,
                "actor_id": command.recorded_by,
                "source_id": command.recorded_source,
                "occurred_at": self._bind_time(database_now),
                "authority_epoch": authority_epoch,
                "change_context_json": context_json,
            },
        )
        next_revision = current.head.revision + 1
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
                "next_revision": next_revision,
                "document_id": current.head.document_id,
                "expected_head": current.head.current_version_id,
                "expected_revision": current.head.revision,
            },
        )
        if updated.rowcount != 1:
            raise SubjectAuthorityConflict(
                f"subject document CAS failed: {current.head.logical_path}"
            )
        await session.execute(
            text(
                """INSERT INTO subject_projection_outbox (
                    head_event_id, document_id, logical_path, version_id,
                    content_hash, state, attempt_count, created_at,
                    confirmed_at, last_error
                ) VALUES (
                    :head_event_id, :document_id, :logical_path, :version_id,
                    :content_hash, :projection_state, 0, :created_at,
                    :confirmed_at, ''
                )"""
            ),
            {
                "head_event_id": head_event_id,
                "document_id": current.head.document_id,
                "logical_path": current.head.logical_path,
                "version_id": version_id,
                "content_hash": content_hash,
                "projection_state": (
                    "confirmed" if self.backend == BackendKind.MYSQL else "pending"
                ),
                "created_at": self._bind_time(database_now),
                "confirmed_at": (
                    self._bind_time(database_now)
                    if self.backend == BackendKind.MYSQL
                    else ""
                ),
            },
        )
        version = SubjectDocumentVersion(
            version_id=version_id,
            document_id=current.head.document_id,
            logical_path=current.head.logical_path,
            parent_version_id=current.head.current_version_id,
            occurrence_id=authority_occurrence,
            semantic_actor_id=command.semantic_actor_id,
            semantic_source_id=command.semantic_source_id,
            occurred_at=_iso(command.occurred_at) or None,
            recorded_by=command.recorded_by,
            recorded_source=command.recorded_source,
            recorded_at=database_now.isoformat(),
            provenance_status=command.provenance_status,
            content_bytes=content,
            content_hash=content_hash,
            byte_length=len(content),
            byte_fidelity=command.byte_fidelity,
            encoding=command.encoding,
            newline_style=command.newline_style,
            change_context=dict(command.change_context or {}),
        )
        head = SubjectDocumentHead(
            document_id=current.head.document_id,
            logical_path=current.head.logical_path,
            declared_owner=current.head.declared_owner,
            current_version_id=version_id,
            revision=next_revision,
        )
        return SubjectDocumentCommit(version=version, head=head)

    async def accept_candidate(
        self,
        command: AcceptSubjectCandidate,
    ) -> SubjectAuthorityCommit:
        """Commit an explicit active-instance decision under one fenced UoW."""

        material = self._authority_command_material(command)
        command_sha256 = self._authority_command_sha256(material)
        accepted_content = bytes(command.accepted_content_bytes)

        async def operation(session: AsyncSession) -> SubjectAuthorityCommit:
            existing = (
                (
                    await session.execute(
                        text(
                            f"SELECT {self._authority_decision_columns()} "
                            "FROM subject_authority_decisions "
                            "WHERE decision_occurrence_id = :occurrence_id"
                            + self._for_update
                        ),
                        {"occurrence_id": material["decision_occurrence_id"]},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if str(existing["command_sha256"]) != command_sha256:
                    raise SubjectAuthorityConflict(
                        "decision occurrence identity conflict"
                    )
                return self._authority_commit_from_row(
                    existing,
                    idempotent_replay=True,
                )

            database_now = await self._database_now(session)
            await self._validate_active_actor(
                session,
                actor=str(material["actor_consciousness_instance_id"]),
                database_now=database_now,
            )
            await self._validate_learning_evidence(
                session,
                material=material,
                accepted_content=accepted_content,
            )
            state, previous_revision = await self._subject_authority_state(
                session,
                lock=True,
            )
            if previous_revision != material["expected_subject_revision"]:
                raise SubjectAuthorityConflict(
                    "unified subject revision CAS failed: expected "
                    f"{material['expected_subject_revision']}, actual "
                    f"{previous_revision}"
                )
            target = state[str(material["target_path"])]
            committed = await self._append_authority_version(
                session,
                current=target,
                material=material,
                content=accepted_content,
                previous_subject_revision=previous_revision,
                database_now=database_now,
            )
            next_contents = {
                path: (
                    accepted_content
                    if path == material["target_path"]
                    else state[path].version.content_bytes
                )
                for path in SUBJECT_AUTHORITY_PATHS
            }
            new_revision = subject_revision_from_contents(next_contents)
            authority_occurrence = self._authority_occurrence_id(
                str(material["decision_occurrence_id"])
            )
            await session.execute(
                text(
                    """INSERT INTO subject_authority_decisions (
                        decision_occurrence_id, authority_occurrence_id,
                        candidate_id, candidate_revision, candidate_sha256,
                        candidate_occurrence_id, actor_consciousness_instance_id,
                        expected_subject_revision, target_path,
                        accepted_content_sha256, occurred_at,
                        previous_subject_revision, new_subject_revision,
                        document_version_id, document_revision,
                        command_sha256, committed_at
                    ) VALUES (
                        :decision_occurrence_id, :authority_occurrence_id,
                        :candidate_id, :candidate_revision, :candidate_sha256,
                        :candidate_occurrence_id, :actor,
                        :expected_subject_revision, :target_path,
                        :accepted_content_sha256, :occurred_at,
                        :previous_subject_revision, :new_subject_revision,
                        :document_version_id, :document_revision,
                        :command_sha256, :committed_at
                    )"""
                ),
                {
                    **material,
                    "authority_occurrence_id": authority_occurrence,
                    "actor": material["actor_consciousness_instance_id"],
                    "previous_subject_revision": previous_revision,
                    "new_subject_revision": new_revision,
                    "document_version_id": committed.version.version_id,
                    "document_revision": committed.head.revision,
                    "command_sha256": command_sha256,
                    "occurred_at": self._bind_time(material["occurred_at"]),
                    "committed_at": self._bind_time(database_now),
                },
            )
            return SubjectAuthorityCommit(
                authority_occurrence_id=authority_occurrence,
                candidate_id=str(material["candidate_id"]),
                decision_occurrence_id=str(material["decision_occurrence_id"]),
                actor_consciousness_instance_id=str(
                    material["actor_consciousness_instance_id"]
                ),
                previous_subject_revision=previous_revision,
                new_subject_revision=new_revision,
                document_version_id=committed.version.version_id,
                document_revision=committed.head.revision,
                accepted_content_sha256=str(material["accepted_content_sha256"]),
                idempotent_replay=False,
            )

        try:
            return await self._write(operation)
        except IntegrityError as exc:
            raise SubjectAuthorityConflict(
                "subject authority acceptance conflicted during commit"
            ) from exc

    async def _read_head(
        self, session: AsyncSession, *, logical_path: str | None = None,
        document_id: str | None = None, lock: bool = False,
    ) -> SubjectDocumentHead | None:
        statement = """SELECT d.document_id, d.logical_path, d.declared_owner,
            d.current_version_id, d.revision, d.binding_revision, d.is_deleted
            FROM subject_documents AS d"""
        if logical_path is not None:
            statement += """ JOIN subject_document_path_bindings AS b
                ON b.document_id = d.document_id
                AND b.logical_path = d.logical_path
                AND b.revision = d.binding_revision
                WHERE b.logical_path = :identity AND d.is_deleted = 0"""
            identity = logical_path
        else:
            statement += " WHERE d.document_id = :identity"
            identity = document_id
        row = (
            await session.execute(
                text(statement + (self._for_update if lock else "")),
                {"identity": identity},
            )
        ).mappings().one_or_none()
        return self._decode_head(row)

    async def _read_binding(
        self, session: AsyncSession, path: str, *, lock: bool = False,
    ) -> SubjectDocumentPathBinding | None:
        row = (
            await session.execute(
                text("SELECT logical_path, document_id, revision "
                     "FROM subject_document_path_bindings "
                     "WHERE logical_path = :path"
                     + (self._for_update if lock else "")),
                {"path": path},
            )
        ).mappings().one_or_none()
        return None if row is None else SubjectDocumentPathBinding(
            logical_path=str(row["logical_path"]),
            document_id=_optional(row["document_id"]),
            revision=int(row["revision"]),
        )

    async def _assert_file_path_shape(
        self, session: AsyncSession, path: str,
    ) -> None:
        """Reject bound ancestor/descendant files while the writer mutex is held."""

        parts = path.split("/")
        parents = ["/".join(parts[:index]) for index in range(1, len(parts))]
        if parents:
            parameters = {f"parent_{index}": parent for index, parent in enumerate(parents)}
            placeholders = ", ".join(f":{name}" for name in parameters)
            ancestor = await session.scalar(text(
                "SELECT logical_path FROM subject_document_path_bindings "
                "WHERE document_id IS NOT NULL "
                f"AND logical_path IN ({placeholders}) LIMIT 1"
            ), parameters)
            if ancestor is not None:
                raise SubjectDocumentConflict("a managed file already occupies an ancestor path")
        # Paths use SQLite BINARY / MySQL utf8mb4_bin. '/' immediately
        # precedes '0', so this half-open range is the exact descendant prefix.
        descendant = await session.scalar(text(
            """SELECT logical_path FROM subject_document_path_bindings
            WHERE document_id IS NOT NULL AND logical_path >= :lower
              AND logical_path < :upper ORDER BY logical_path LIMIT 1"""
        ), {"lower": path + "/", "upper": path + "0"})
        if descendant is not None:
            raise SubjectDocumentConflict("managed descendant files already occupy this path")

    async def _set_binding(
        self, session: AsyncSession, *, path: str,
        previous: SubjectDocumentPathBinding | None, document_id: str | None,
        occurrence_id: str, recorded_at: datetime,
    ) -> int:
        revision = previous.revision if previous else 0
        parameters = {
            "path": path, "document_id": document_id,
            "previous_document_id": previous.document_id if previous else None,
            "previous_revision": revision, "revision": revision + 1,
            "occurrence_id": occurrence_id,
            "recorded_at": self._bind_time(recorded_at),
            "event_id": "path_" + hashlib.sha256(canonical_json({
                "path": path, "revision": revision + 1,
                "occurrence_id": occurrence_id,
            }).encode()).hexdigest(),
        }
        if previous is None:
            try:
                await session.execute(text(
                    """INSERT INTO subject_document_path_bindings
                    (logical_path, document_id, revision)
                    VALUES (:path, :document_id, :revision)"""
                ), parameters)
            except IntegrityError as exc:
                raise SubjectDocumentConflict("concurrent path creation") from exc
        else:
            changed = await session.execute(text(
                """UPDATE subject_document_path_bindings
                SET document_id = :document_id, revision = :revision
                WHERE logical_path = :path AND revision = :previous_revision"""
            ), parameters)
            if changed.rowcount != 1:
                raise SubjectDocumentConflict("path binding CAS failed")
        await session.execute(text(
            """INSERT INTO subject_document_path_events
            (event_id, logical_path, previous_document_id, document_id,
             previous_revision, revision, occurrence_id, recorded_at)
            VALUES (:event_id, :path, :previous_document_id, :document_id,
             :previous_revision, :revision, :occurrence_id, :recorded_at)"""
        ), parameters)
        return revision + 1

    @staticmethod
    def _operation_digest(command: Any) -> str:
        material = asdict(command)
        if material.get("content_bytes") is not None:
            content = bytes(material.pop("content_bytes"))
            material["content_hash"] = hashlib.sha256(content).hexdigest()
            material["byte_length"] = len(content)
        return hashlib.sha256(canonical_json(material).encode()).hexdigest()

    @staticmethod
    def _decode_operation(row: Any) -> SubjectDocumentOperation | None:
        return None if row is None else SubjectDocumentOperation(
            occurrence_id=str(row["occurrence_id"]),
            operation=str(row["operation"]),
            document_id=str(row["document_id"]),
            command_digest=str(row["command_digest"]),
            result=_json_object(row["result_json"]),
            change_context=_json_object(row["change_context_json"]),
            recorded_at=_iso(row["recorded_at"]),
        )

    async def _read_operation(
        self, session: AsyncSession, occurrence_id: str,
    ) -> SubjectDocumentOperation | None:
        row = (
            await session.execute(
                text("SELECT * FROM subject_document_operations "
                     "WHERE occurrence_id = :occurrence_id" + self._for_update),
                {"occurrence_id": occurrence_id},
            )
        ).mappings().one_or_none()
        return self._decode_operation(row)

    async def _record_operation(
        self, session: AsyncSession, *, occurrence_id: str, operation: str,
        digest: str, head: SubjectDocumentHead, version_id: str,
        context: dict[str, Any], recorded_at: datetime,
    ) -> None:
        await session.execute(text(
            """INSERT INTO subject_document_operations
            (occurrence_id, operation, document_id, command_digest,
             result_json, change_context_json, recorded_at)
            VALUES (:occurrence_id, :operation, :document_id, :digest,
             :result_json, :context_json, :recorded_at)"""
        ), {
            "occurrence_id": occurrence_id, "operation": operation,
            "document_id": head.document_id, "digest": digest,
            "result_json": canonical_json({
                "head": asdict(head), "version_id": version_id,
                "logical_path": head.logical_path, "revision": head.revision,
                "binding_revision": head.binding_revision,
                "deleted": head.deleted,
            }),
            "context_json": canonical_json(context),
            "recorded_at": self._bind_time(recorded_at),
        })

    async def _replay_operation(
        self, session: AsyncSession, receipt: SubjectDocumentOperation,
        digest: str,
    ) -> SubjectDocumentCommit:
        if receipt.command_digest != digest:
            raise SubjectDocumentConflict(
                f"subject operation identity conflict: {receipt.occurrence_id}"
            )
        row = (
            await session.execute(text(
                f"SELECT {self._version_columns()} FROM subject_document_versions "
                "WHERE version_id = :version_id"
            ), {"version_id": receipt.result["version_id"]})
        ).mappings().one_or_none()
        if row is None:
            raise SubjectDocumentNotFound(str(receipt.result["version_id"]))
        return SubjectDocumentCommit(
            version=self._decode_version(row),
            head=SubjectDocumentHead(**receipt.result["head"]),
        )

    async def get_path_binding(
        self, logical_path: str,
    ) -> SubjectDocumentPathBinding | None:
        async with self.runtime.unit_of_work() as uow:
            return await self._read_binding(
                uow.session, normalize_subject_path(logical_path),
            )

    async def list_file_bindings(
        self, *, logical_path_prefix: str = "", after_logical_path: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        bounded = min(500, max(0, int(limit)))
        if bounded == 0:
            return []
        prefix = str(logical_path_prefix)
        if len(prefix) > 512:
            raise ValueError("logical_path_prefix exceeds 512 characters")
        cursor = (
            normalize_subject_path(after_logical_path) if after_logical_path else ""
        )
        async with self.runtime.unit_of_work() as uow:
            rows = (
                await uow.session.execute(text(
                    """SELECT b.logical_path, b.document_id,
                    b.revision AS binding_revision, d.current_version_id,
                    d.revision AS document_revision, v.byte_length, v.content_hash,
                    v.recorded_at, v.encoding
                    FROM subject_document_path_bindings AS b
                    LEFT JOIN subject_documents AS d
                      ON d.document_id = b.document_id AND d.logical_path = b.logical_path
                      AND d.binding_revision = b.revision AND d.is_deleted = 0
                    LEFT JOIN subject_document_versions AS v
                      ON v.version_id = d.current_version_id
                      AND v.document_id = d.document_id
                    WHERE b.logical_path > :cursor
                      AND SUBSTR(b.logical_path, 1, :prefix_length) = :prefix
                    ORDER BY b.logical_path LIMIT :limit"""
                ), {
                    "cursor": cursor, "prefix": prefix, "prefix_length": len(prefix),
                    "limit": bounded,
                })
            ).mappings().all()
        result = [dict(row) for row in rows]
        for row in result:
            if row["document_id"] is not None and (
                not row["current_version_id"] or row["content_hash"] is None
            ):
                raise SubjectDocumentConflict("file binding/head/version evidence mismatch")
            row["binding_revision"] = int(row["binding_revision"])
            row["document_revision"] = int(row["document_revision"] or 0)
            row["byte_length"] = (
                int(row["byte_length"]) if row["byte_length"] is not None else None
            )
            row["recorded_at"] = _iso(row["recorded_at"]) or None
        return result

    async def get_head(self, logical_path: str) -> SubjectDocumentHead | None:
        async with self.runtime.unit_of_work() as uow:
            return await self._read_head(
                uow.session, logical_path=normalize_subject_path(logical_path),
            )

    async def get_document_head(
        self, document_id: str,
    ) -> SubjectDocumentHead | None:
        async with self.runtime.unit_of_work() as uow:
            return await self._read_head(uow.session, document_id=document_id)

    async def get_document_projection_frontier(
        self, *, logical_path_prefix: str = "",
    ) -> int:
        prefix = str(logical_path_prefix)
        if len(prefix) > 512:
            raise ValueError("logical_path_prefix exceeds 512 characters")
        async with self.runtime.unit_of_work() as uow:
            return int((await uow.session.execute(text(
                "SELECT COALESCE(MAX(outbox_id), 0) "
                "FROM subject_projection_outbox "
                "WHERE SUBSTR(logical_path, 1, :prefix_length) = :prefix"
            ), {"prefix": prefix, "prefix_length": len(prefix)})).scalar_one())

    async def list_document_projection_changes(
        self, *, after_outbox_id: int, through_outbox_id: int,
        logical_path_prefix: str = "", limit: int = 500,
    ) -> list[dict[str, Any]]:
        if (
            type(after_outbox_id) is not int or type(through_outbox_id) is not int
            or after_outbox_id < 0 or through_outbox_id < after_outbox_id
            or type(limit) is not int or not 1 <= limit <= 500
            or len(str(logical_path_prefix)) > 512
        ):
            raise ValueError("invalid document projection frontier page")
        prefix = str(logical_path_prefix)
        async with self.runtime.unit_of_work() as uow:
            rows = (await uow.session.execute(text(
                "SELECT outbox_id, document_id FROM subject_projection_outbox "
                "WHERE outbox_id > :after_id AND outbox_id <= :through_id "
                "AND SUBSTR(logical_path, 1, :prefix_length) = :prefix "
                "ORDER BY outbox_id LIMIT :limit"
            ), {
                "after_id": after_outbox_id, "through_id": through_outbox_id,
                "prefix": prefix, "prefix_length": len(prefix), "limit": limit,
            })).mappings().all()
        return [
            {"outbox_id": int(row["outbox_id"]), "document_id": str(row["document_id"])}
            for row in rows
        ]

    async def get_document_operation(
        self, occurrence_id: str,
    ) -> SubjectDocumentOperation | None:
        async with self.runtime.unit_of_work() as uow:
            return await self._read_operation(uow.session, occurrence_id)

    async def list_document_operations(
        self, document_id: str, *, after_recorded_at: str = "",
        after_occurrence_id: str = "", limit: int = 100,
    ) -> list[SubjectDocumentOperation]:
        parameters: dict[str, Any] = {
            "document_id": document_id, "limit": min(500, max(0, int(limit))),
        }
        statement = ("SELECT * FROM subject_document_operations "
                     "WHERE document_id = :document_id")
        if after_recorded_at:
            parsed = _parse_datetime(after_recorded_at)
            if parsed is None or not after_occurrence_id:
                raise ValueError("operation cursor requires valid time and identity")
            statement += """ AND (recorded_at > :recorded_at OR
                (recorded_at = :recorded_at AND occurrence_id > :occurrence_id))"""
            parameters.update(
                recorded_at=self._bind_time(parsed),
                occurrence_id=after_occurrence_id,
            )
        statement += " ORDER BY recorded_at, occurrence_id LIMIT :limit"
        async with self.runtime.unit_of_work() as uow:
            rows = (
                await uow.session.execute(text(statement), parameters)
            ).mappings().all()
        return [self._decode_operation(row) for row in rows]

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

    async def get_version_descriptor(self, version_id: str) -> dict[str, Any]:
        identity = str(version_id).strip()
        if not identity:
            raise ValueError("version_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            row = (
                await uow.session.execute(text(
                    """SELECT version_id, document_id, logical_path,
                    parent_version_id, occurrence_id, content_hash, byte_length,
                    recorded_at, semantic_actor_id, semantic_source_id, occurred_at,
                    provenance_status, encoding, newline_style, byte_fidelity,
                    recorded_by, recorded_source FROM subject_document_versions
                    WHERE version_id = :version_id"""
                ), {"version_id": identity})
            ).mappings().one_or_none()
        if row is None:
            raise SubjectDocumentNotFound(identity)
        descriptor = dict(row)
        descriptor["recorded_at"] = _iso(descriptor["recorded_at"])
        descriptor["occurred_at"] = _iso(descriptor["occurred_at"]) or None
        descriptor["byte_length"] = int(descriptor["byte_length"])
        return descriptor

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
                            """SELECT d.document_id, d.logical_path, d.declared_owner,
                            d.current_version_id, d.revision, d.binding_revision,
                            d.is_deleted FROM subject_documents AS d
                            JOIN subject_document_path_bindings AS b
                              ON b.document_id = d.document_id
                              AND b.logical_path = d.logical_path
                              AND b.revision = d.binding_revision
                            WHERE d.logical_path > :after_logical_path
                              AND d.is_deleted = 0
                            ORDER BY d.logical_path LIMIT :limit"""
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
                            d.binding_revision AS head_binding_revision,
                            {self._version_columns("v")}
                            FROM subject_documents AS d
                            JOIN subject_document_path_bindings AS b
                              ON b.document_id = d.document_id
                              AND b.logical_path = d.logical_path
                              AND b.revision = d.binding_revision
                            JOIN subject_document_versions AS v
                              ON v.version_id = d.current_version_id
                            WHERE d.logical_path > :after_logical_path
                              AND d.is_deleted = 0
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
                binding_revision=int(row["head_binding_revision"]),
            )
            version = self._decode_version(row)
            if (
                version.document_id != head.document_id
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
        head = await self.get_head(logical_path)
        if head is None:
            return []
        return await self.list_document_history(
            head.document_id, after_recorded_at=after_recorded_at,
            after_version_id=after_version_id, limit=limit,
        )

    async def list_document_history(
        self, document_id: str, *, after_recorded_at: str = "",
        after_version_id: str = "", limit: int = 100,
    ) -> list[SubjectDocumentVersion]:
        bounded = min(500, max(0, int(limit)))
        if bounded == 0:
            return []
        statement = (
            f"SELECT {self._version_columns()} FROM subject_document_versions "
            "WHERE document_id = :document_id"
        )
        parameters: dict[str, Any] = {"document_id": document_id, "limit": bounded}
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

    async def list_document_version_descriptors(
        self, document_id: str, *, after_recorded_at: str = "",
        after_version_id: str = "", limit: int = 100,
    ) -> list[dict[str, Any]]:
        bounded = min(500, max(0, int(limit)))
        if bounded == 0:
            return []
        statement = """SELECT version_id, document_id, logical_path,
            parent_version_id, occurrence_id, content_hash, byte_length,
            recorded_at, semantic_actor_id, semantic_source_id, occurred_at,
            provenance_status FROM subject_document_versions
            WHERE document_id = :document_id"""
        parameters: dict[str, Any] = {
            "document_id": document_id, "limit": bounded,
        }
        if after_recorded_at:
            parsed = _parse_datetime(after_recorded_at)
            if parsed is None or not after_version_id:
                raise ValueError("history cursor requires valid time and version id")
            statement += """ AND (recorded_at > :recorded_at OR
                (recorded_at = :recorded_at AND version_id > :version_id))"""
            parameters.update(
                recorded_at=self._bind_time(parsed), version_id=after_version_id,
            )
        statement += " ORDER BY recorded_at, version_id LIMIT :limit"
        async with self.runtime.unit_of_work() as uow:
            rows = (
                await uow.session.execute(text(statement), parameters)
            ).mappings().all()
        descriptors = [dict(row) for row in rows]
        for descriptor in descriptors:
            descriptor["recorded_at"] = _iso(descriptor["recorded_at"])
            descriptor["occurred_at"] = _iso(descriptor["occurred_at"]) or None
            descriptor["byte_length"] = int(descriptor["byte_length"])
        return descriptors

    async def append_version(
        self,
        command: AppendSubjectDocumentVersion,
    ) -> SubjectDocumentCommit:
        return await self._append_version(command)

    async def _append_version(
        self, command: AppendSubjectDocumentVersion, *,
        session: AsyncSession | None = None, operation_name: str = "write",
        operation_digest: str | None = None, record_operation: bool = True,
        create_projection: bool = True,
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
        context_json = canonical_json(command.change_context or {})
        command_digest = operation_digest or self._operation_digest(command)

        async def operation(session: AsyncSession) -> SubjectDocumentCommit:
            receipt = await self._read_operation(session, occurrence_id)
            if receipt is not None:
                return await self._replay_operation(session, receipt, command_digest)
            binding = await self._read_binding(session, path, lock=True)
            current = await self._read_head(
                session, logical_path=path, lock=True,
            )
            # Legacy versions have no S2 receipt. Resolve their immutable
            # occurrence before path reuse, never derive their identity anew.
            existing_version = (
                (
                    await session.execute(
                        text(
                            f"SELECT {self._version_columns()} "
                            "FROM subject_document_versions "
                            "WHERE logical_path = :logical_path "
                            "AND occurrence_id = :occurrence_id" + self._for_update
                        ),
                        {
                            "logical_path": path,
                            "occurrence_id": occurrence_id,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing_version is not None:
                decoded = self._decode_version(existing_version)
                legacy_version_id = self._version_id(
                    document_id=decoded.document_id,
                    parent_version_id=expected_head, occurrence_id=occurrence_id,
                    content_hash=content_hash, command=command,
                )
                if decoded.version_id != legacy_version_id:
                    raise SubjectDocumentConflict(
                        f"subject occurrence identity conflict: {occurrence_id}"
                    )
                head = await self._read_head(
                    session, document_id=decoded.document_id, lock=True,
                )
                if head is None:
                    raise SubjectDocumentConflict(
                        "version exists without document head"
                    )
                return SubjectDocumentCommit(version=decoded, head=head)

            actual_binding_revision = binding.revision if binding else 0
            if (
                command.expected_binding_revision is not None
                and int(command.expected_binding_revision) != actual_binding_revision
            ):
                raise SubjectDocumentConflict("path binding revision CAS failed")
            if command.expected_document_id is not None:
                actual_document_id = current.document_id if current else ""
                if command.expected_document_id != actual_document_id:
                    raise SubjectDocumentConflict("document identity CAS failed")
            if binding is not None and binding.document_id and current is None:
                raise SubjectDocumentConflict("path binding/head evidence mismatch")
            document_id = current.document_id if current else (
                "doc_" + hashlib.sha256(canonical_json({
                    "create_occurrence_id": occurrence_id, "logical_path": path,
                }).encode()).hexdigest()
            )
            version_id = self._version_id(
                document_id=document_id, parent_version_id=expected_head,
                occurrence_id=occurrence_id, content_hash=content_hash,
                command=command,
            )
            head_event_id = self._head_event_id(document_id, occurrence_id)
            database_now = await self._database_now(session)
            if current is None:
                if expected_revision != 0 or expected_head:
                    raise SubjectDocumentConflict("new document requires empty head")
                await self._assert_file_path_shape(session, path)
                try:
                    await session.execute(
                        text(
                            """INSERT INTO subject_documents (
                                document_id, logical_path, declared_owner,
                                current_version_id, revision, binding_revision
                            ) VALUES (
                                :document_id, :logical_path, :declared_owner, '', 0,
                                :binding_revision
                            )"""
                        ),
                        {
                            "document_id": document_id,
                            "logical_path": path,
                            "declared_owner": command.declared_owner,
                            "binding_revision": actual_binding_revision + 1,
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
                    binding_revision=await self._set_binding(
                        session, path=path, previous=binding,
                        document_id=document_id, occurrence_id=occurrence_id,
                        recorded_at=database_now,
                    ),
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
                      AND revision = :expected_revision
                      AND logical_path = :logical_path
                      AND binding_revision = :binding_revision
                      AND is_deleted = 0"""
                ),
                {
                    "version_id": version_id,
                    "next_revision": expected_revision + 1,
                    "document_id": document_id,
                    "expected_head": expected_head,
                    "expected_revision": expected_revision,
                    "logical_path": path,
                    "binding_revision": current.binding_revision,
                },
            )
            if updated.rowcount != 1:
                raise SubjectDocumentConflict(f"concurrent subject head update: {path}")
            if create_projection:
                await self._enqueue_projection(
                    session, head_event_id=head_event_id, document_id=document_id,
                    logical_path=path, version_id=version_id,
                    content_hash=content_hash, database_now=database_now,
                    operation=operation_name,
                    binding_revision=current.binding_revision,
                    previous_version_id=expected_head,
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
                document_id=document_id, logical_path=path,
                declared_owner=current.declared_owner,
                current_version_id=version_id, revision=expected_revision + 1,
                binding_revision=current.binding_revision,
            )
            if record_operation:
                await self._record_operation(
                    session, occurrence_id=occurrence_id,
                    operation=operation_name, digest=command_digest, head=head,
                    version_id=version_id, context=dict(command.change_context or {}),
                    recorded_at=database_now,
                )
            return SubjectDocumentCommit(version=version, head=head)

        if session is not None:
            return await operation(session)
        return await self._write(operation)

    async def _enqueue_projection(
        self, session: AsyncSession, *, head_event_id: str, document_id: str,
        logical_path: str, version_id: str, content_hash: str,
        database_now: datetime, operation: str = "write",
        binding_revision: int = 0, previous_logical_path: str = "",
        previous_binding_revision: int = 0, previous_version_id: str = "",
    ) -> None:
        previous_hash = ""
        if previous_version_id:
            previous_hash = str(await session.scalar(text(
                "SELECT content_hash FROM subject_document_versions "
                "WHERE version_id = :version_id"
            ), {"version_id": previous_version_id}) or "")
            if not previous_hash:
                raise SubjectDocumentNotFound(previous_version_id)
        await session.execute(
                text(
                    """INSERT INTO subject_projection_outbox (
                        head_event_id, document_id, logical_path, version_id,
                        content_hash, state, attempt_count, created_at,
                        confirmed_at, last_error, operation,
                        binding_revision, previous_logical_path,
                        previous_binding_revision, previous_version_id,
                        previous_content_hash
                    ) VALUES (
                        :head_event_id, :document_id, :logical_path, :version_id,
                        :content_hash, :projection_state, 0, :created_at,
                        :confirmed_at, '', :operation, :binding_revision,
                        :previous_logical_path, :previous_binding_revision,
                        :previous_version_id, :previous_content_hash
                    )"""
                ),
                {
                    "head_event_id": head_event_id,
                    "document_id": document_id,
                    "logical_path": logical_path,
                    "version_id": version_id,
                    "content_hash": content_hash,
                    "operation": operation,
                    "binding_revision": binding_revision,
                    "previous_logical_path": previous_logical_path,
                    "previous_binding_revision": previous_binding_revision,
                    "previous_version_id": previous_version_id,
                    "previous_content_hash": previous_hash,
                    "projection_state": (
                        "confirmed" if self.backend == BackendKind.MYSQL else "pending"
                    ),
                    "created_at": self._bind_time(database_now),
                    "confirmed_at": (
                        self._bind_time(database_now)
                        if self.backend == BackendKind.MYSQL
                        else ""
                    ),
                },
            )

    async def mutate_document(
        self, command: SubjectDocumentMutation,
    ) -> SubjectDocumentMutationCommit:
        return await self._mutate_document(command)

    async def _mutate_document(
        self, command: SubjectDocumentMutation, *,
        session: AsyncSession | None = None,
    ) -> SubjectDocumentMutationCommit:
        path = normalize_subject_path(command.logical_path)
        operation_name = str(command.operation)
        if operation_name not in {"rename", "delete", "copy"}:
            raise ValueError("unsupported document lifecycle operation")
        target = (
            normalize_subject_path(command.target_logical_path)
            if command.target_logical_path else ""
        )
        if operation_name == "delete":
            if target or command.content_bytes is not None:
                raise ValueError("delete accepts neither target nor replacement bytes")
        elif not target or target == path:
            raise ValueError("rename/copy requires a distinct target path")
        fixed = set(SUBJECT_AUTHORITY_PATHS) | {
            subject_authority_logical_path(item) for item in SUBJECT_AUTHORITY_PATHS
        }
        if path in fixed or target in fixed:
            raise SubjectDocumentConflict(
                "fixed subject authority slots cannot be renamed, deleted, or copied"
            )
        occurrence_id = _required_identity(
            command.occurrence_id, field="occurrence_id",
        )
        _required_identity(command.recorded_by, field="recorded_by", maximum=128)
        _required_identity(command.recorded_source, field="recorded_source")
        if (
            not command.expected_document_id or not command.expected_head_version_id
            or command.expected_revision <= 0 or command.expected_binding_revision <= 0
            or command.expected_target_binding_revision < 0
        ):
            raise ValueError("lifecycle command requires exact document/head/binding CAS")
        digest = self._operation_digest(command)

        async def operation(session: AsyncSession) -> SubjectDocumentMutationCommit:
            receipt = await self._read_operation(session, occurrence_id)
            if receipt is not None:
                replay = await self._replay_operation(session, receipt, digest)
                return SubjectDocumentMutationCommit(
                    operation=receipt.operation, occurrence_id=occurrence_id,
                    document_id=replay.head.document_id, head=replay.head,
                    version=replay.version, idempotent_replay=True,
                )
            # Deterministic path-lock order prevents opposite-direction moves
            # from forming a lock cycle on the shared backend.
            bindings = {
                item: await self._read_binding(session, item, lock=True)
                for item in sorted({path, target} - {""})
            }
            source_binding = bindings[path]
            head = await self._read_head(session, logical_path=path, lock=True)
            if (
                head is None or source_binding is None
                or head.document_id != command.expected_document_id
                or head.revision != command.expected_revision
                or head.current_version_id != command.expected_head_version_id
                or head.binding_revision != command.expected_binding_revision
                or source_binding.revision != command.expected_binding_revision
                or source_binding.document_id != head.document_id
            ):
                raise SubjectDocumentConflict("lifecycle source identity/head/path CAS failed")
            target_binding = bindings.get(target)
            if target and (
                (target_binding is not None and target_binding.document_id is not None)
                or (target_binding.revision if target_binding else 0)
                != command.expected_target_binding_revision
            ):
                raise SubjectDocumentConflict("lifecycle target path CAS failed")
            if target:
                await self._assert_file_path_shape(session, target)
            row = (
                await session.execute(text(
                    f"SELECT {self._version_columns()} FROM subject_document_versions "
                    "WHERE version_id = :version_id"
                ), {"version_id": head.current_version_id})
            ).mappings().one_or_none()
            if row is None:
                raise SubjectDocumentNotFound(head.current_version_id)
            original = self._decode_version(row)
            if original.document_id != head.document_id:
                raise SubjectDocumentConflict("source head/version identity mismatch")
            database_now = await self._database_now(session)
            context = dict(command.change_context or {})
            context.update({
                "operation_actor_id": command.semantic_actor_id,
                "operation_source_id": command.semantic_source_id,
                "operation_occurred_at": _iso(command.occurred_at) or None,
                "source_document_id": head.document_id,
                "source_version_id": original.version_id,
                "previous_logical_path": path,
                "target_logical_path": target,
            })
            if operation_name == "copy":
                context.update({
                    "copied_from_document_id": head.document_id,
                    "copied_from_version_id": original.version_id,
                    "copy_actor_id": command.semantic_actor_id,
                    "copy_source_id": command.semantic_source_id,
                    "copy_occurred_at": _iso(command.occurred_at) or None,
                })
                unchanged = command.content_bytes is None
                copied = await self._append_version(
                    AppendSubjectDocumentVersion(
                        logical_path=target, expected_revision=0,
                        expected_head_version_id="", expected_document_id="",
                        expected_binding_revision=command.expected_target_binding_revision,
                        content_bytes=(
                            original.content_bytes if unchanged
                            else bytes(command.content_bytes)
                        ),
                        occurrence_id=occurrence_id,
                        recorded_by=command.recorded_by,
                        recorded_source=command.recorded_source,
                        declared_owner=head.declared_owner,
                        semantic_actor_id=(
                            original.semantic_actor_id if unchanged
                            else command.semantic_actor_id
                        ),
                        semantic_source_id=(
                            original.semantic_source_id if unchanged
                            else command.semantic_source_id
                        ),
                        occurred_at=original.occurred_at if unchanged else command.occurred_at,
                        provenance_status=original.provenance_status if unchanged else "complete",
                        byte_fidelity=original.byte_fidelity if unchanged else "exact_bytes",
                        encoding=original.encoding if unchanged else command.encoding,
                        newline_style=original.newline_style if unchanged else command.newline_style,
                        change_context=context,
                    ),
                    session=session, operation_name="copy", operation_digest=digest,
                )
                return SubjectDocumentMutationCommit(
                    operation="copy", occurrence_id=occurrence_id,
                    document_id=copied.head.document_id,
                    head=copied.head, version=copied.version,
                )
            version = original
            original_head = head
            if command.content_bytes is not None:
                changed = await self._append_version(
                    AppendSubjectDocumentVersion(
                        logical_path=path, expected_revision=head.revision,
                        expected_head_version_id=head.current_version_id,
                        expected_document_id=head.document_id,
                        expected_binding_revision=head.binding_revision,
                        content_bytes=bytes(command.content_bytes),
                        occurrence_id=occurrence_id,
                        recorded_by=command.recorded_by,
                        recorded_source=command.recorded_source,
                        declared_owner=head.declared_owner,
                        semantic_actor_id=command.semantic_actor_id,
                        semantic_source_id=command.semantic_source_id,
                        occurred_at=command.occurred_at,
                        encoding=command.encoding,
                        newline_style=command.newline_style,
                        change_context=context,
                    ),
                    session=session, operation_name=operation_name,
                    operation_digest=digest, record_operation=False,
                    create_projection=False,
                )
                head, version = changed.head, changed.version
            release_revision = await self._set_binding(
                session, path=path, previous=source_binding, document_id=None,
                occurrence_id=occurrence_id, recorded_at=database_now,
            )
            next_binding_revision = release_revision
            if target:
                next_binding_revision = await self._set_binding(
                    session, path=target, previous=target_binding,
                    document_id=head.document_id, occurrence_id=occurrence_id,
                    recorded_at=database_now,
                )
            next_head = SubjectDocumentHead(
                document_id=head.document_id, logical_path=target or path,
                declared_owner=head.declared_owner, current_version_id=version.version_id,
                revision=original_head.revision + 1,
                binding_revision=next_binding_revision,
                deleted=operation_name == "delete",
            )
            updated = await session.execute(text(
                """UPDATE subject_documents SET logical_path = :next_path,
                binding_revision = :next_binding_revision,
                revision = :next_revision, is_deleted = :deleted
                WHERE document_id = :document_id AND logical_path = :path
                  AND current_version_id = :expected_head
                  AND revision = :expected_revision
                  AND binding_revision = :expected_binding_revision
                  AND is_deleted = 0"""
            ), {
                "next_path": next_head.logical_path,
                "next_binding_revision": next_binding_revision,
                "next_revision": next_head.revision,
                "deleted": int(next_head.deleted), "document_id": head.document_id,
                "path": path, "expected_head": head.current_version_id,
                "expected_revision": head.revision,
                "expected_binding_revision": head.binding_revision,
            })
            if updated.rowcount != 1:
                raise SubjectDocumentConflict("lifecycle head CAS failed")
            head_event_id = self._head_event_id(head.document_id, occurrence_id)
            if command.content_bytes is None:
                await session.execute(text(
                    """INSERT INTO subject_document_head_events
                    (head_event_id, document_id, previous_version_id, next_version_id,
                     occurrence_id, actor_id, source_id, occurred_at,
                     authority_epoch, change_context_json)
                    VALUES (:head_event_id, :document_id, :version_id, :version_id,
                     :occurrence_id, :actor_id, :source_id, :occurred_at,
                     :authority_epoch, :context_json)"""
                ), {
                    "head_event_id": head_event_id, "document_id": head.document_id,
                    "version_id": version.version_id, "occurrence_id": occurrence_id,
                    "actor_id": command.recorded_by, "source_id": command.recorded_source,
                    "occurred_at": self._bind_time(database_now),
                    "authority_epoch": (
                        self.runtime.authority_token.authority_epoch
                        if self.runtime.authority_token is not None
                        else int(self.runtime.writer_epoch)
                    ),
                    "context_json": canonical_json(context),
                })
            await self._enqueue_projection(
                session, head_event_id=head_event_id, document_id=head.document_id,
                logical_path=next_head.logical_path, version_id=version.version_id,
                content_hash=version.content_hash, database_now=database_now,
                operation=operation_name, binding_revision=next_binding_revision,
                previous_logical_path=path, previous_binding_revision=release_revision,
                previous_version_id=original.version_id,
            )
            await self._record_operation(
                session, occurrence_id=occurrence_id, operation=operation_name,
                digest=digest, head=next_head, version_id=version.version_id,
                context=context, recorded_at=database_now,
            )
            return SubjectDocumentMutationCommit(
                operation=operation_name, occurrence_id=occurrence_id,
                document_id=next_head.document_id, head=next_head, version=version,
            )

        if session is not None:
            return await operation(session)
        return await self._write(operation)

    async def apply_document_batch(
        self, commands: list[AppendSubjectDocumentVersion | SubjectDocumentMutation],
    ) -> list[SubjectDocumentCommit | SubjectDocumentMutationCommit]:
        paths: set[str] = set()
        occurrences: set[str] = set()
        for command in commands:
            if not isinstance(command, (AppendSubjectDocumentVersion, SubjectDocumentMutation)):
                raise TypeError("batch accepts only explicit document commands")
            selected = {normalize_subject_path(command.logical_path)}
            if isinstance(command, SubjectDocumentMutation) and command.target_logical_path:
                selected.add(normalize_subject_path(command.target_logical_path))
            if (
                paths & selected or command.occurrence_id in occurrences
                or any(
                    item.startswith(previous + "/") or previous.startswith(item + "/")
                    for item in selected for previous in paths
                )
                or (
                    len(selected) > 1 and any(
                        item != other and item.startswith(other + "/")
                        for item in selected for other in selected
                    )
                )
            ):
                raise ValueError(
                    "document batch requires disjoint non-hierarchical paths and occurrences"
                )
            paths.update(selected)
            occurrences.add(command.occurrence_id)

        async def operation(
            session: AsyncSession,
        ) -> list[SubjectDocumentCommit | SubjectDocumentMutationCommit]:
            commits: list[SubjectDocumentCommit | SubjectDocumentMutationCommit] = []
            for command in commands:
                if isinstance(command, AppendSubjectDocumentVersion):
                    commits.append(await self._append_version(command, session=session))
                else:
                    commits.append(await self._mutate_document(command, session=session))
            return commits

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
            operation=str(row["operation"]),
            previous_logical_path=str(row["previous_logical_path"] or ""),
            binding_revision=int(row["binding_revision"]),
            previous_binding_revision=int(row["previous_binding_revision"]),
            previous_version_id=str(row["previous_version_id"] or ""),
            previous_content_hash=str(row["previous_content_hash"] or ""),
        )

    @staticmethod
    def _projection_columns() -> str:
        return """outbox_id, head_event_id, document_id, logical_path,
        version_id, content_hash, state, attempt_count,
        lease_owner, lease_until, revision, operation, previous_logical_path,
        binding_revision, previous_binding_revision, previous_version_id,
        previous_content_hash"""

    async def get_projection_task(
        self,
        logical_path: str,
        version_id: str,
        *, occurrence_id: str | None = None,
    ) -> SubjectProjectionTask | None:
        path = normalize_subject_path(logical_path)
        identity = str(version_id).strip()
        if not identity:
            raise ValueError("projection version_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            row = (
                (
                    await uow.session.execute(
                        text(
                            f"SELECT {self._projection_columns()} "
                            "FROM subject_projection_outbox "
                            "WHERE logical_path = :logical_path "
                            "AND version_id = :version_id"
                            + (
                                " AND head_event_id IN (SELECT head_event_id "
                                "FROM subject_document_head_events "
                                "WHERE occurrence_id = :occurrence_id)"
                                if occurrence_id is not None else ""
                            )
                            + " ORDER BY outbox_id DESC LIMIT 1"
                        ),
                        {"logical_path": path, "version_id": identity,
                         **({"occurrence_id": occurrence_id}
                            if occurrence_id is not None else {})},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._decode_projection(row) if row is not None else None

    async def claim_projection(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        logical_path: str | None = None,
    ) -> SubjectProjectionTask | None:
        worker = str(worker_id).strip()
        if not worker or len(worker) > 255:
            raise ValueError("projection worker_id must be 1..255 characters")
        if int(lease_seconds) <= 0:
            raise ValueError("projection lease_seconds must be positive")
        path = normalize_subject_path(logical_path) if logical_path else ""

        async def operation(session: AsyncSession) -> SubjectProjectionTask | None:
            database_now = await self._database_now(session)
            lease_available = (
                "lease_until IS NULL"
                if self.backend == BackendKind.MYSQL
                else "(lease_until IS NULL OR lease_until = '')"
            )
            path_filter = " AND logical_path = :logical_path" if path else ""
            row = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {self._projection_columns()}
                            FROM subject_projection_outbox
                            WHERE state = 'pending'
                              AND ({lease_available}
                                   OR lease_until <= :database_now)
                              {path_filter}
                            ORDER BY outbox_id LIMIT 1{self._for_update}"""
                        ),
                        {
                            "database_now": self._bind_time(database_now),
                            **({"logical_path": path} if path else {}),
                        },
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

    async def retry_projection(
        self,
        task: SubjectProjectionTask,
        *,
        worker_id: str,
    ) -> SubjectProjectionTask:
        """Requeue an exact failed task after the caller resolves file conflict."""

        worker = str(worker_id).strip()
        if not worker or len(worker) > 255:
            raise ValueError("projection worker_id must be 1..255 characters")

        async def operation(session: AsyncSession) -> SubjectProjectionTask:
            updated = await session.execute(
                text(
                    """UPDATE subject_projection_outbox SET
                    state = 'pending', lease_owner = :empty_owner,
                    lease_until = :empty_lease, last_error = '',
                    revision = revision + 1
                    WHERE outbox_id = :outbox_id AND state = 'failed'
                      AND revision = :revision AND version_id = :version_id
                      AND content_hash = :content_hash"""
                ),
                {
                    "empty_owner": "",
                    "empty_lease": (None if self.backend == BackendKind.MYSQL else ""),
                    "outbox_id": task.outbox_id,
                    "revision": task.revision,
                    "version_id": task.version_id,
                    "content_hash": task.content_hash,
                },
            )
            if updated.rowcount != 1:
                raise SubjectDocumentConflict("projection retry CAS failed")
            row = (
                (
                    await session.execute(
                        text(
                            f"SELECT {self._projection_columns()} "
                            "FROM subject_projection_outbox "
                            "WHERE outbox_id = :outbox_id"
                        ),
                        {"outbox_id": task.outbox_id},
                    )
                )
                .mappings()
                .one()
            )
            return self._decode_projection(row)

        return await self._write(operation)

    async def heal_projection(
        self,
        task: SubjectProjectionTask,
        *,
        worker_id: str,
    ) -> None:
        """Mark a legacy pending/failed projection as confirmed.

        MySQL backend never runs a workspace projector: ``append_version`` writes
        outbox rows directly as ``confirmed``. Rows left in ``pending``/``failed``
        are historical migration residue or abnormal data; the authoritative
        version bytes already live in ``subject_document_versions``. This healing
        rebuilds the (reconstructible) projection outbox state without touching
        history. LOCAL backend must keep ``failed`` as a legitimate terminal
        state, so this method is intentionally only used under MySQL.
        """
        worker = str(worker_id).strip()
        if not worker or len(worker) > 255:
            raise ValueError("projection worker_id must be 1..255 characters")

        async def operation(session: AsyncSession) -> None:
            database_now = await self._database_now(session)
            updated = await session.execute(
                text(
                    """UPDATE subject_projection_outbox SET
                    state = 'confirmed', confirmed_at = :confirmed_at,
                    lease_owner = '', lease_until = :empty_lease,
                    last_error = '', revision = revision + 1
                    WHERE outbox_id = :outbox_id
                      AND state IN ('pending', 'failed')
                      AND version_id = :version_id
                      AND content_hash = :content_hash"""
                ),
                {
                    "confirmed_at": self._bind_time(database_now),
                    "empty_lease": (None if self.backend == BackendKind.MYSQL else ""),
                    "outbox_id": task.outbox_id,
                    "version_id": task.version_id,
                    "content_hash": task.content_hash,
                },
            )
            if updated.rowcount != 1:
                raise SubjectDocumentConflict(
                    "projection self-heal CAS failed"
                )

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
        outbox = {str(row["state"]): int(row["total"]) for row in outbox_rows}
        failed = int(outbox.get("failed", 0))
        pending = int(outbox.get("pending", 0))
        return {
            "status": "failed" if failed else ("degraded" if pending else "healthy"),
            "backend": self.backend.value,
            "backend_identity": self.runtime.backend_identity,
            "documents": documents,
            "versions": versions,
            "projection_outbox": outbox,
            "reason": (
                "subject workspace projection failures require operator review"
                if failed
                else (
                    "subject workspace projection backlog is pending"
                    if pending
                    else "subject workspace projection is current"
                )
            ),
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
