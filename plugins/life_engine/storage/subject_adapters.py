"""Fenced local/MySQL adapters for exact-byte subject document history."""

from __future__ import annotations

import asyncio
import base64
import binascii
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
    SUBJECT_AUTHORITY_PATHS,
    AcceptSubjectCandidate,
    AppendSubjectDocumentVersion,
    SubjectAuthorityActorInactive,
    SubjectAuthorityCommit,
    SubjectAuthorityConflict,
    SubjectAuthorityEvidenceError,
    SubjectDocumentCommit,
    SubjectDocumentConflict,
    SubjectDocumentHead,
    SubjectDocumentNotFound,
    SubjectDocumentVersion,
    SubjectProjectionTask,
    subject_authority_logical_path,
    subject_revision_from_contents,
)

_T = TypeVar("_T")
_MAX_WRITE_ATTEMPTS = 3
_MAX_SUBJECT_CANDIDATE_BYTES = 4 * 1024 * 1024


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
                        {self._version_columns("v")}
                        FROM subject_documents AS d
                        JOIN subject_document_versions AS v
                          ON v.version_id = d.current_version_id
                        WHERE d.logical_path IN (:soul, :user, :memory)
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
                    :content_hash, 'pending', 0, :created_at,
                    :confirmed_at, ''
                )"""
            ),
            {
                "head_event_id": head_event_id,
                "document_id": current.head.document_id,
                "logical_path": current.head.logical_path,
                "version_id": version_id,
                "content_hash": content_hash,
                "created_at": self._bind_time(database_now),
                "confirmed_at": (None if self.backend == BackendKind.MYSQL else ""),
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

    async def get_projection_task(
        self,
        logical_path: str,
        version_id: str,
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
                        ),
                        {"logical_path": path, "version_id": identity},
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
