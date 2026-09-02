"""Fenced SQL adapters for append-only learning evidence and projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage import canonical_json

from .contracts import StorageBackendRuntime
from ._write_base import run_write_attempts
from .learning_contracts import (
    LearningCommitResult,
    LearningEventDraft,
    LearningEventRecord,
    LearningOccurrenceConflict,
    LearningProjection,
    LearningProjectionConflict,
    LearningProjectionWrite,
)
from .models import BackendKind
from .writer_claims import SingletonWriterClaim, SingletonWriterClaimLost

def _translate_learning_claim_lost(exc: DBAPIError) -> Exception | None:
    if "LearningSingletonWriterClaimRequired" in str(exc.orig):
        return SingletonWriterClaimLost("LearningSingletonWriterClaimRequired")
    return None


_T = TypeVar("_T")
_PROJECTION_STATES = frozenset({"ready", "rebuilding", "failed"})
_MAX_OCCURRENCE_ID_CHARS = 255
_MAX_EVENT_KIND_CHARS = 128
_MAX_SOURCE_CHARS = 255
_MAX_ACTOR_CHARS = 255
_MAX_PROJECTION_NAME_CHARS = 128
_MAX_PROJECTOR_VERSION_CHARS = 128
_MAX_EVENT_JSON_BYTES = 8 * 1024 * 1024
_MAX_PROJECTION_JSON_BYTES = 64 * 1024 * 1024
_MAX_EVENT_KIND_FILTERS = 128


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


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise TypeError("stored learning JSON must be an object")
    return dict(value)


class SQLLearningStore:
    """One event/projection store bound to a coherent runtime."""

    def __init__(
        self,
        runtime: StorageBackendRuntime,
        *,
        writer_claim: SingletonWriterClaim | None = None,
    ) -> None:
        if not runtime.enabled or runtime.engine is None:
            raise RuntimeError("learning adapter requires an enabled storage runtime")
        self.runtime = runtime
        self.backend = runtime.backend
        self.writer_claim = writer_claim

    @property
    def _for_update(self) -> str:
        return " FOR UPDATE" if self.backend == BackendKind.MYSQL else ""

    def _bind_time(self, value: Any) -> datetime | str:
        parsed = _parse_datetime(value)
        if parsed is None:
            raise ValueError(f"invalid learning timestamp: {value!r}")
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
            async with self.runtime.unit_of_work(
                writer_claim=self.writer_claim
            ) as uow:
                if self.writer_claim is not None:
                    await self.runtime.bind_singleton_writer_write(
                        uow.session,
                        self.writer_claim,
                    )
                result = await operation(uow.session)
                if self.writer_claim is not None:
                    await self.runtime.clear_singleton_writer_write(uow.session)
                return result

        return await run_write_attempts(
            _attempt,
            exhaustion_message="bounded learning retry loop exhausted unexpectedly",
            translate=_translate_learning_claim_lost,
        )

    @staticmethod
    def _validate_draft(draft: LearningEventDraft) -> None:
        occurrence_id = str(draft.occurrence_id).strip()
        event_kind = str(draft.event_kind).strip()
        source = str(draft.source).strip()
        actor = str(draft.actor_consciousness_instance_id)
        if not occurrence_id:
            raise ValueError("learning occurrence_id must not be empty")
        if len(occurrence_id) > _MAX_OCCURRENCE_ID_CHARS:
            raise ValueError("learning occurrence_id exceeds portable storage limit")
        if not event_kind:
            raise ValueError("learning event_kind must not be empty")
        if len(event_kind) > _MAX_EVENT_KIND_CHARS:
            raise ValueError("learning event_kind exceeds portable storage limit")
        if not source:
            raise ValueError("learning source must not be empty")
        if len(source) > _MAX_SOURCE_CHARS:
            raise ValueError("learning source exceeds portable storage limit")
        if len(actor) > _MAX_ACTOR_CHARS:
            raise ValueError("learning actor exceeds portable storage limit")
        revision = str(draft.subject_revision).strip().lower()
        if revision and (
            len(revision) != 64
            or any(char not in "0123456789abcdef" for char in revision)
        ):
            raise ValueError("subject_revision must be empty or a 64-hex digest")
        if not isinstance(draft.provenance, dict) or not isinstance(
            draft.payload, dict
        ):
            raise TypeError("learning provenance and payload must be objects")
        if len(canonical_json(draft.provenance).encode("utf-8")) > (
            _MAX_EVENT_JSON_BYTES
        ) or len(canonical_json(draft.payload).encode("utf-8")) > (
            _MAX_EVENT_JSON_BYTES
        ):
            raise ValueError("learning event JSON exceeds explicit storage limit")
        if _parse_datetime(draft.occurred_at) is None:
            raise ValueError("learning occurred_at must be an ISO timestamp")

    @classmethod
    def _event_material(cls, draft: LearningEventDraft) -> dict[str, Any]:
        cls._validate_draft(draft)
        return {
            "occurrence_id": str(draft.occurrence_id),
            "event_kind": str(draft.event_kind),
            "occurred_at": _iso(draft.occurred_at),
            "source": str(draft.source),
            "actor_consciousness_instance_id": str(
                draft.actor_consciousness_instance_id
            ),
            "subject_revision": str(draft.subject_revision).lower(),
            "provenance": dict(draft.provenance),
            "payload": dict(draft.payload),
        }

    @classmethod
    def _encoded_event(
        cls,
        draft: LearningEventDraft,
    ) -> tuple[dict[str, Any], str, str, str]:
        material = cls._event_material(draft)
        provenance_json = canonical_json(material["provenance"])
        payload_json = canonical_json(material["payload"])
        event_sha256 = hashlib.sha256(canonical_json(material).encode()).hexdigest()
        return material, provenance_json, payload_json, event_sha256

    @classmethod
    def _decode_event(cls, row: Any) -> LearningEventRecord:
        provenance = _json_object(row["provenance_json"])
        payload = _json_object(row["payload_json"])
        draft = LearningEventDraft(
            occurrence_id=str(row["occurrence_id"]),
            event_kind=str(row["event_kind"]),
            occurred_at=_iso(row["occurred_at"]),
            source=str(row["source"]),
            actor_consciousness_instance_id=str(
                row["actor_consciousness_instance_id"] or ""
            ),
            subject_revision=str(row["subject_revision"] or ""),
            provenance=provenance,
            payload=payload,
        )
        _, _, _, calculated = cls._encoded_event(draft)
        persisted = str(row["event_sha256"])
        if calculated != persisted:
            raise RuntimeError(
                f"LearningEventCorrupt:{draft.occurrence_id}:{persisted}"
            )
        return LearningEventRecord(
            position=int(row["position"]),
            occurrence_id=draft.occurrence_id,
            event_kind=draft.event_kind,
            occurred_at=draft.occurred_at,
            recorded_at=_iso(row["recorded_at"]),
            source=draft.source,
            actor_consciousness_instance_id=(draft.actor_consciousness_instance_id),
            subject_revision=draft.subject_revision,
            provenance=provenance,
            payload=payload,
            event_sha256=persisted,
        )

    async def _append_one(
        self,
        session: AsyncSession,
        draft: LearningEventDraft,
        *,
        database_now: datetime,
    ) -> LearningEventRecord:
        material, provenance_json, payload_json, event_sha256 = self._encoded_event(
            draft
        )
        insert_sql = (
            "INSERT IGNORE INTO learning_events"
            if self.backend == BackendKind.MYSQL
            else "INSERT OR IGNORE INTO learning_events"
        )
        await session.execute(
            text(
                f"""{insert_sql} (
                    occurrence_id, event_kind, occurred_at, recorded_at,
                    source, actor_consciousness_instance_id, subject_revision,
                    provenance_json, payload_json, event_sha256
                ) VALUES (
                    :occurrence_id, :event_kind, :occurred_at, :recorded_at,
                    :source, :actor, :subject_revision,
                    :provenance_json, :payload_json, :event_sha256
                )"""
            ),
            {
                "occurrence_id": material["occurrence_id"],
                "event_kind": material["event_kind"],
                "occurred_at": self._bind_time(material["occurred_at"]),
                "recorded_at": self._bind_time(database_now),
                "source": material["source"],
                "actor": material["actor_consciousness_instance_id"],
                "subject_revision": material["subject_revision"],
                "provenance_json": provenance_json,
                "payload_json": payload_json,
                "event_sha256": event_sha256,
            },
        )
        row = (
            (
                await session.execute(
                    text(
                        """SELECT position, occurrence_id, event_kind,
                        occurred_at, recorded_at, source,
                        actor_consciousness_instance_id, subject_revision,
                        provenance_json, payload_json, event_sha256
                        FROM learning_events
                        WHERE occurrence_id = :occurrence_id"""
                    ),
                    {"occurrence_id": material["occurrence_id"]},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RuntimeError(f"LearningEventInsertLost:{draft.occurrence_id}")
        if str(row["event_sha256"]) != event_sha256:
            raise LearningOccurrenceConflict(draft.occurrence_id)
        return self._decode_event(row)

    @staticmethod
    def _validate_projection(write: LearningProjectionWrite) -> None:
        projection_name = str(write.projection_name).strip()
        projector_version = str(write.projector_version).strip()
        if not projection_name:
            raise ValueError("projection_name must not be empty")
        if len(projection_name) > _MAX_PROJECTION_NAME_CHARS:
            raise ValueError("projection_name exceeds portable storage limit")
        if write.expected_revision < 0 or write.expected_source_frontier < 0:
            raise ValueError("projection expectations must be non-negative")
        if write.schema_version <= 0:
            raise ValueError("projection schema_version must be positive")
        if not projector_version:
            raise ValueError("projector_version must not be empty")
        if len(projector_version) > _MAX_PROJECTOR_VERSION_CHARS:
            raise ValueError("projector_version exceeds portable storage limit")
        if write.rebuild_state not in _PROJECTION_STATES:
            raise ValueError(f"invalid rebuild_state: {write.rebuild_state}")
        if not isinstance(write.payload, dict):
            raise TypeError("projection payload must be an object")
        if len(canonical_json(write.payload).encode("utf-8")) > (
            _MAX_PROJECTION_JSON_BYTES
        ):
            raise ValueError("projection payload exceeds explicit storage limit")

    @staticmethod
    def _decode_projection(row: Any) -> LearningProjection:
        payload = _json_object(row["payload_json"])
        calculated = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        persisted = str(row["projection_sha256"])
        if calculated != persisted:
            raise RuntimeError(
                f"LearningProjectionCorrupt:{row['projection_name']}:{persisted}"
            )
        return LearningProjection(
            projection_name=str(row["projection_name"]),
            revision=int(row["revision"]),
            source_frontier=int(row["source_frontier"]),
            schema_version=int(row["schema_version"]),
            projector_version=str(row["projector_version"]),
            rebuild_state=str(row["rebuild_state"]),
            payload=payload,
            projection_sha256=persisted,
            updated_at=_iso(row["updated_at"]),
        )

    async def _commit_projection(
        self,
        session: AsyncSession,
        write: LearningProjectionWrite,
        *,
        event_frontier: int,
        database_now: datetime,
    ) -> LearningProjection:
        self._validate_projection(write)
        payload_json = canonical_json(write.payload)
        projection_sha256 = hashlib.sha256(payload_json.encode()).hexdigest()
        row = (
            (
                await session.execute(
                    text(
                        """SELECT projection_name, revision, source_frontier,
                        schema_version, projector_version, rebuild_state,
                        payload_json, projection_sha256, updated_at
                        FROM learning_projections
                        WHERE projection_name = :projection_name"""
                        + self._for_update
                    ),
                    {"projection_name": write.projection_name},
                )
            )
            .mappings()
            .one_or_none()
        )
        current = self._decode_projection(row) if row is not None else None
        target_frontier = max(write.expected_source_frontier, event_frontier)

        def same_content(projection: LearningProjection) -> bool:
            return all(
                (
                    projection.source_frontier == target_frontier,
                    projection.schema_version == write.schema_version,
                    projection.projector_version == write.projector_version,
                    projection.rebuild_state == write.rebuild_state,
                    projection.projection_sha256 == projection_sha256,
                )
            )

        if current is None:
            if write.expected_revision != 0 or write.expected_source_frontier != 0:
                raise LearningProjectionConflict(
                    projection_name=write.projection_name,
                    expected_revision=write.expected_revision,
                    expected_source_frontier=write.expected_source_frontier,
                    actual_revision=0,
                    actual_source_frontier=0,
                )
            revision = 1
            await session.execute(
                text(
                    """INSERT INTO learning_projections (
                        projection_name, revision, source_frontier,
                        schema_version, projector_version, rebuild_state,
                        payload_json, projection_sha256, updated_at
                    ) VALUES (
                        :projection_name, :revision, :source_frontier,
                        :schema_version, :projector_version, :rebuild_state,
                        :payload_json, :projection_sha256, :updated_at
                    )"""
                ),
                {
                    "projection_name": write.projection_name,
                    "revision": revision,
                    "source_frontier": target_frontier,
                    "schema_version": write.schema_version,
                    "projector_version": write.projector_version,
                    "rebuild_state": write.rebuild_state,
                    "payload_json": payload_json,
                    "projection_sha256": projection_sha256,
                    "updated_at": self._bind_time(database_now),
                },
            )
        else:
            if current.revision == write.expected_revision + 1 and same_content(
                current
            ):
                return current
            if (
                current.revision != write.expected_revision
                or current.source_frontier != write.expected_source_frontier
            ):
                raise LearningProjectionConflict(
                    projection_name=write.projection_name,
                    expected_revision=write.expected_revision,
                    expected_source_frontier=write.expected_source_frontier,
                    actual_revision=current.revision,
                    actual_source_frontier=current.source_frontier,
                    actual_projection_sha256=current.projection_sha256,
                )
            if same_content(current):
                return current
            revision = current.revision + 1
            result = await session.execute(
                text(
                    """UPDATE learning_projections SET
                        revision = :new_revision,
                        source_frontier = :source_frontier,
                        schema_version = :schema_version,
                        projector_version = :projector_version,
                        rebuild_state = :rebuild_state,
                        payload_json = :payload_json,
                        projection_sha256 = :projection_sha256,
                        updated_at = :updated_at
                    WHERE projection_name = :projection_name
                      AND revision = :expected_revision
                      AND source_frontier = :expected_source_frontier"""
                ),
                {
                    "new_revision": revision,
                    "source_frontier": target_frontier,
                    "schema_version": write.schema_version,
                    "projector_version": write.projector_version,
                    "rebuild_state": write.rebuild_state,
                    "payload_json": payload_json,
                    "projection_sha256": projection_sha256,
                    "updated_at": self._bind_time(database_now),
                    "projection_name": write.projection_name,
                    "expected_revision": write.expected_revision,
                    "expected_source_frontier": write.expected_source_frontier,
                },
            )
            if result.rowcount != 1:
                raise LearningProjectionConflict(
                    projection_name=write.projection_name,
                    expected_revision=write.expected_revision,
                    expected_source_frontier=write.expected_source_frontier,
                    actual_revision=current.revision,
                    actual_source_frontier=current.source_frontier,
                    actual_projection_sha256=current.projection_sha256,
                )

        persisted = (
            (
                await session.execute(
                    text(
                        """SELECT projection_name, revision, source_frontier,
                        schema_version, projector_version, rebuild_state,
                        payload_json, projection_sha256, updated_at
                        FROM learning_projections
                        WHERE projection_name = :projection_name"""
                    ),
                    {"projection_name": write.projection_name},
                )
            )
            .mappings()
            .one()
        )
        return self._decode_projection(persisted)

    async def commit(
        self,
        *,
        events: list[LearningEventDraft],
        projections: list[LearningProjectionWrite],
    ) -> LearningCommitResult:
        """Commit occurrences and projection CAS updates in one transaction."""

        occurrence_ids = [event.occurrence_id for event in events]
        projection_names = [write.projection_name for write in projections]
        if len(occurrence_ids) != len(set(occurrence_ids)):
            raise ValueError("one learning commit cannot repeat an occurrence_id")
        if len(projection_names) != len(set(projection_names)):
            raise ValueError("one learning commit cannot repeat a projection_name")
        if not events and not projections:
            return LearningCommitResult(events=(), projections=())

        async def operation(session: AsyncSession) -> LearningCommitResult:
            database_now = await self._database_now(session)
            records = tuple(
                [
                    await self._append_one(
                        session,
                        event,
                        database_now=database_now,
                    )
                    for event in events
                ]
            )
            event_frontier = max(
                (record.position for record in records),
                default=0,
            )
            committed_projections = tuple(
                [
                    await self._commit_projection(
                        session,
                        write,
                        event_frontier=event_frontier,
                        database_now=database_now,
                    )
                    for write in projections
                ]
            )
            return LearningCommitResult(
                events=records,
                projections=committed_projections,
            )

        return await self._write(operation)

    async def read_events(
        self,
        after_position: int,
        *,
        limit: int = 100,
        event_kinds: tuple[str, ...] = (),
    ) -> list[LearningEventRecord]:
        """Read one bounded stable page in ascending position order."""

        if int(after_position) < 0:
            raise ValueError("after_position must be non-negative")
        bounded_limit = int(limit)
        if bounded_limit <= 0 or bounded_limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        parameters: dict[str, Any] = {
            "after_position": int(after_position),
            "limit": bounded_limit,
        }
        statement = """SELECT position, occurrence_id, event_kind,
            occurred_at, recorded_at, source,
            actor_consciousness_instance_id, subject_revision,
            provenance_json, payload_json, event_sha256
            FROM learning_events WHERE position > :after_position"""
        sql = text(statement + " ORDER BY position LIMIT :limit")
        if event_kinds:
            normalized = tuple(str(kind).strip() for kind in event_kinds)
            if any(not kind for kind in normalized):
                raise ValueError("event_kinds must not contain empty values")
            if len(normalized) > _MAX_EVENT_KIND_FILTERS or any(
                len(kind) > _MAX_EVENT_KIND_CHARS for kind in normalized
            ):
                raise ValueError("event_kinds exceed portable query limits")
            sql = text(
                statement
                + " AND event_kind IN :event_kinds"
                + " ORDER BY position LIMIT :limit"
            ).bindparams(bindparam("event_kinds", expanding=True))
            parameters["event_kinds"] = normalized
        async with self.runtime.unit_of_work() as uow:
            rows = (await uow.session.execute(sql, parameters)).mappings().all()
        return [self._decode_event(row) for row in rows]

    async def event_by_occurrence(
        self,
        occurrence_id: str,
    ) -> LearningEventRecord | None:
        """Resolve one immutable occurrence."""

        identity = str(occurrence_id).strip()
        if not identity:
            raise ValueError("occurrence_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            row = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT position, occurrence_id, event_kind,
                            occurred_at, recorded_at, source,
                            actor_consciousness_instance_id, subject_revision,
                            provenance_json, payload_json, event_sha256
                            FROM learning_events
                            WHERE occurrence_id = :occurrence_id"""
                        ),
                        {"occurrence_id": identity},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._decode_event(row) if row is not None else None

    async def get_projection(
        self,
        projection_name: str,
    ) -> LearningProjection | None:
        """Read one current projection with payload integrity verification."""

        name = str(projection_name).strip()
        if not name:
            raise ValueError("projection_name must not be empty")
        async with self.runtime.unit_of_work() as uow:
            row = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT projection_name, revision,
                            source_frontier, schema_version, projector_version,
                            rebuild_state, payload_json, projection_sha256,
                            updated_at FROM learning_projections
                            WHERE projection_name = :projection_name"""
                        ),
                        {"projection_name": name},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._decode_projection(row) if row is not None else None

    async def list_projections(self) -> list[LearningProjection]:
        """List current projections by stable identity."""

        async with self.runtime.unit_of_work() as uow:
            rows = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT projection_name, revision,
                            source_frontier, schema_version, projector_version,
                            rebuild_state, payload_json, projection_sha256,
                            updated_at FROM learning_projections
                            ORDER BY projection_name"""
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [self._decode_projection(row) for row in rows]

    async def health_snapshot(self) -> dict[str, Any]:
        """Return content-free counts and rebuild/frontier diagnostics."""

        async with self.runtime.unit_of_work() as uow:
            event_row = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT COUNT(*) AS total,
                            COALESCE(MAX(position), 0) AS frontier,
                            MAX(recorded_at) AS last_recorded_at
                            FROM learning_events"""
                        )
                    )
                )
                .mappings()
                .one()
            )
            projection_rows = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT projection_name, revision,
                            source_frontier, rebuild_state
                            FROM learning_projections ORDER BY projection_name"""
                        )
                    )
                )
                .mappings()
                .all()
            )
        states = {state: 0 for state in sorted(_PROJECTION_STATES)}
        projections: dict[str, dict[str, Any]] = {}
        for row in projection_rows:
            state = str(row["rebuild_state"])
            states[state] = states.get(state, 0) + 1
            projections[str(row["projection_name"])] = {
                "revision": int(row["revision"]),
                "source_frontier": int(row["source_frontier"]),
                "rebuild_state": state,
            }
        return {
            "status": (
                "failed"
                if states.get("failed", 0)
                else "degraded"
                if states.get("rebuilding", 0)
                else "healthy"
            ),
            "event_count": int(event_row["total"]),
            "event_frontier": int(event_row["frontier"]),
            "last_recorded_at": _iso(event_row["last_recorded_at"]),
            "projection_count": len(projection_rows),
            "projection_states": states,
            "projections": projections,
            "singleton_writer": (
                {
                    "status": "claimed",
                    "generation_id": self.writer_claim.generation_id,
                    "namespace": self.writer_claim.namespace,
                    "state_key": self.writer_claim.state_key,
                    "owner_instance_id": self.writer_claim.owner_instance_id,
                    "lease_epoch": self.writer_claim.lease_epoch,
                }
                if self.writer_claim is not None
                else {"status": "unclaimed"}
            ),
        }


class LocalLearningStore(SQLLearningStore):
    """SQLite implementation of the learning contract."""


class MySQLLearningStore(SQLLearningStore):
    """MySQL implementation of the learning contract."""


__all__ = ["LocalLearningStore", "MySQLLearningStore", "SQLLearningStore"]
