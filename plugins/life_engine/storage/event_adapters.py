"""Fenced SQL adapters for the authoritative Life Event ledger."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage import canonical_json

from ..service.event_bus import (
    LifeEvent,
    RawEventGapError,
    life_event_from_dict,
    life_event_to_dict,
)
from .contracts import StorageBackendRuntime, StorageWriterRole
from .event_contracts import (
    LifeEventConsumerConflict,
    LifeEventConsumerCursor,
    LifeEventDigest,
    LifeEventOccurrenceConflict,
    LifeEventSnapshotCursor,
    LifeEventSnapshotRecord,
)
from .models import BackendKind

_T = TypeVar("_T")
_MAX_WRITE_ATTEMPTS = 3


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
    if parsed is None:
        return ""
    return parsed.isoformat()


def _json_value(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str):
        return json.loads(value)
    return value


class SQLLifeEventStore:
    """One source-preserving event ledger bound to a coherent runtime."""

    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if not runtime.enabled or runtime.engine is None:
            raise RuntimeError("Life Event adapter requires an enabled storage runtime")
        self.runtime = runtime
        self.backend = runtime.backend

    @property
    def _for_update(self) -> str:
        return " FOR UPDATE" if self.backend == BackendKind.MYSQL else ""

    def _bind_time(self, value: Any) -> datetime | str:
        parsed = _parse_datetime(value)
        if parsed is None:
            raise ValueError(f"invalid Life Event timestamp: {value!r}")
        if self.backend == BackendKind.MYSQL:
            return parsed.replace(tzinfo=None)
        return parsed.isoformat()

    def _require_candidate_copy(self) -> None:
        if (
            self.runtime.writer_role != StorageWriterRole.CANDIDATE_COPY
            or self.runtime.authority_registry is not None
            or self.runtime.authority_token is not None
            or self.runtime._write_fence is None
        ):
            raise RuntimeError(
                "exact Life Event snapshot import requires candidate-copy authority"
            )

    async def _database_now(self, session: AsyncSession) -> datetime:
        if self.backend == BackendKind.MYSQL:
            value = await session.scalar(text("SELECT CURRENT_TIMESTAMP(6)"))
        else:
            value = await session.scalar(
                text("SELECT STRFTIME('%Y-%m-%dT%H:%M:%f+00:00', 'now')")
            )
        parsed = _parse_datetime(value)
        if parsed is None:
            raise RuntimeError("storage backend did not return a valid database time")
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

    async def _write(
        self,
        operation: Callable[[AsyncSession], Awaitable[_T]],
    ) -> _T:
        for attempt in range(_MAX_WRITE_ATTEMPTS):
            try:
                async with self.runtime.unit_of_work() as uow:
                    return await operation(uow.session)
            except DBAPIError as exc:
                if attempt + 1 >= _MAX_WRITE_ATTEMPTS or not self._retryable(exc):
                    raise
                await asyncio.sleep(0.02 * (attempt + 1))
        raise AssertionError("bounded Life Event retry loop exhausted unexpectedly")

    @staticmethod
    def _source_sequence(event: LifeEvent) -> int:
        return int(event.source_sequence or event.sequence or 0)

    @classmethod
    def _canonical_payload(
        cls,
        event: LifeEvent,
        *,
        occurrence_id: str,
    ) -> dict[str, Any]:
        source_sequence = cls._source_sequence(event)
        payload = life_event_to_dict(
            replace(
                event,
                occurrence_id=occurrence_id,
                sequence=source_sequence,
                source_sequence=source_sequence,
                recorded_at="",
            )
        )
        payload["occurrence_id"] = occurrence_id
        return payload

    @classmethod
    def _occurrence_id(cls, event: LifeEvent) -> str:
        explicit = str(event.occurrence_id or "").strip()
        if explicit:
            return explicit
        payload = cls._canonical_payload(event, occurrence_id="")
        payload.pop("occurrence_id", None)
        material = canonical_json(payload)
        return "occ_" + hashlib.sha256(material.encode()).hexdigest()

    @classmethod
    def _encoded_event(cls, event: LifeEvent) -> tuple[str, str, str]:
        occurrence_id = cls._occurrence_id(event)
        payload_json = canonical_json(
            cls._canonical_payload(event, occurrence_id=occurrence_id)
        )
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        return occurrence_id, payload_json, payload_hash

    @staticmethod
    def _decode_event(row: Any) -> LifeEvent:
        payload = _json_value(row["payload_json"], default={})
        event = life_event_from_dict(dict(payload))
        return replace(
            event,
            sequence=int(row["ingest_position"]),
            source_sequence=int(row["source_sequence"]),
            occurrence_id=str(row["occurrence_id"]),
            recorded_at=_iso(row["recorded_at"]),
        )

    async def _append_one(
        self,
        session: AsyncSession,
        event: LifeEvent,
        *,
        database_now: datetime,
    ) -> LifeEvent:
        occurrence_id, payload_json, payload_hash = self._encoded_event(event)
        occurred_at = self._bind_time(event.timestamp)
        recorded_at = self._bind_time(event.recorded_at or database_now)
        insert_sql = (
            "INSERT IGNORE INTO raw_life_events"
            if self.backend == BackendKind.MYSQL
            else "INSERT OR IGNORE INTO raw_life_events"
        )
        await session.execute(
            text(
                f"""{insert_sql} (
                    occurrence_id, source_event_id, source_sequence, occurred_at,
                    recorded_at, payload_json, payload_hash
                ) VALUES (
                    :occurrence_id, :source_event_id, :source_sequence, :occurred_at,
                    :recorded_at, :payload_json, :payload_hash
                )"""
            ),
            {
                "occurrence_id": occurrence_id,
                "source_event_id": str(event.event_id),
                "source_sequence": self._source_sequence(event),
                "occurred_at": occurred_at,
                "recorded_at": recorded_at,
                "payload_json": payload_json,
                "payload_hash": payload_hash,
            },
        )
        row = (
            (
                await session.execute(
                    text(
                        """SELECT ingest_position, occurrence_id, source_sequence,
                        recorded_at, payload_json, payload_hash
                        FROM raw_life_events WHERE occurrence_id = :occurrence_id"""
                    ),
                    {"occurrence_id": occurrence_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RuntimeError(f"LifeEventInsertLost:{occurrence_id}")
        if str(row["payload_hash"]) != payload_hash:
            raise LifeEventOccurrenceConflict(occurrence_id)

        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        if metadata.get("sync_export") is True:
            visibility = str(metadata.get("visibility") or "private").lower()
            state = "pending" if visibility in {"shared", "public"} else "held"
            outbox_insert = (
                "INSERT IGNORE INTO raw_event_export_outbox"
                if self.backend == BackendKind.MYSQL
                else "INSERT OR IGNORE INTO raw_event_export_outbox"
            )
            await session.execute(
                text(
                    f"""{outbox_insert} (
                        occurrence_id, payload_hash, payload_json, state,
                        remote_position, created_at, confirmed_at
                    ) VALUES (
                        :occurrence_id, :payload_hash, :payload_json, :state,
                        0, :created_at, :confirmed_at
                    )"""
                ),
                {
                    "occurrence_id": occurrence_id,
                    "payload_hash": payload_hash,
                    "payload_json": payload_json,
                    "state": state,
                    "created_at": self._bind_time(database_now),
                    "confirmed_at": (None if self.backend == BackendKind.MYSQL else ""),
                },
            )
            outbox_hash = await session.scalar(
                text(
                    "SELECT payload_hash FROM raw_event_export_outbox "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            )
            if str(outbox_hash or "") != payload_hash:
                raise LifeEventOccurrenceConflict(f"export-outbox:{occurrence_id}")
        return self._decode_event(row)

    async def append(self, event: LifeEvent) -> LifeEvent:
        """Append one event with occurrence idempotency and fencing."""

        return (await self.append_many([event]))[0]

    async def append_many(self, events: list[LifeEvent]) -> list[LifeEvent]:
        """Append an ordered batch in one transaction."""

        if not events:
            return []

        async def operation(session: AsyncSession) -> list[LifeEvent]:
            database_now = await self._database_now(session)
            return [
                await self._append_one(
                    session,
                    event,
                    database_now=database_now,
                )
                for event in events
            ]

        return await self._write(operation)

    async def import_snapshot_records(
        self,
        records: list[LifeEventSnapshotRecord],
    ) -> list[LifeEventDigest]:
        """Import byte-exact immutable rows under candidate-copy authority."""

        self._require_candidate_copy()
        if not records:
            return []
        positions = [int(record.ingest_position) for record in records]
        identities = [str(record.occurrence_id) for record in records]
        if any(position <= 0 for position in positions):
            raise ValueError("snapshot ingest positions must be positive")
        if positions != sorted(positions) or len(set(positions)) != len(positions):
            raise ValueError("snapshot ingest positions must be unique and ordered")
        if any(not identity for identity in identities) or len(set(identities)) != len(
            identities
        ):
            raise ValueError(
                "snapshot occurrence identities must be unique and nonempty"
            )

        parameters: list[dict[str, Any]] = []
        for record in records:
            calculated = hashlib.sha256(record.payload_json.encode()).hexdigest()
            if calculated != record.payload_hash:
                raise ValueError(
                    f"snapshot payload hash mismatch: {record.occurrence_id}"
                )
            payload = json.loads(record.payload_json)
            if not isinstance(payload, dict):
                raise TypeError("snapshot Life Event payload must be an object")
            life_event_from_dict(payload)
            parameters.append(
                {
                    "ingest_position": int(record.ingest_position),
                    "occurrence_id": str(record.occurrence_id),
                    "source_event_id": str(record.source_event_id),
                    "source_sequence": int(record.source_sequence),
                    "occurred_at": self._bind_time(record.occurred_at),
                    "recorded_at": self._bind_time(record.recorded_at),
                    "payload_json": str(record.payload_json),
                    "payload_hash": str(record.payload_hash),
                }
            )

        async def operation(session: AsyncSession) -> list[LifeEventDigest]:
            insert_sql = (
                "INSERT IGNORE INTO raw_life_events"
                if self.backend == BackendKind.MYSQL
                else "INSERT OR IGNORE INTO raw_life_events"
            )
            await session.execute(
                text(
                    f"""{insert_sql} (
                        ingest_position, occurrence_id, source_event_id,
                        source_sequence, occurred_at, recorded_at,
                        payload_json, payload_hash
                    ) VALUES (
                        :ingest_position, :occurrence_id, :source_event_id,
                        :source_sequence, :occurred_at, :recorded_at,
                        :payload_json, :payload_hash
                    )"""
                ),
                parameters,
            )
            statement = text(
                """SELECT ingest_position, occurrence_id, source_event_id,
                source_sequence, occurred_at, recorded_at,
                payload_json, payload_hash FROM raw_life_events
                WHERE occurrence_id IN :occurrence_ids
                   OR ingest_position IN :ingest_positions"""
            ).bindparams(
                bindparam("occurrence_ids", expanding=True),
                bindparam("ingest_positions", expanding=True),
            )
            rows = (
                (
                    await session.execute(
                        statement,
                        {
                            "occurrence_ids": identities,
                            "ingest_positions": positions,
                        },
                    )
                )
                .mappings()
                .all()
            )
            by_occurrence = {str(row["occurrence_id"]): row for row in rows}
            by_position = {int(row["ingest_position"]): row for row in rows}
            digests: list[LifeEventDigest] = []
            for record in records:
                row = by_occurrence.get(record.occurrence_id)
                position_row = by_position.get(record.ingest_position)
                same_time = row is not None and all(
                    (
                        _parse_datetime(row["occurred_at"])
                        == _parse_datetime(record.occurred_at),
                        _parse_datetime(row["recorded_at"])
                        == _parse_datetime(record.recorded_at),
                    )
                )
                if (
                    row is None
                    or position_row is None
                    or str(position_row["occurrence_id"]) != record.occurrence_id
                    or int(row["ingest_position"]) != record.ingest_position
                    or str(row["source_event_id"]) != record.source_event_id
                    or int(row["source_sequence"]) != record.source_sequence
                    or str(row["payload_json"]) != record.payload_json
                    or str(row["payload_hash"]) != record.payload_hash
                    or not same_time
                ):
                    raise LifeEventOccurrenceConflict(record.occurrence_id)
                digests.append(
                    LifeEventDigest(
                        occurrence_id=record.occurrence_id,
                        position=record.ingest_position,
                        payload_hash=record.payload_hash,
                    )
                )
            return digests

        return await self._write(operation)

    async def import_snapshot_cursors(
        self,
        cursors: list[LifeEventSnapshotCursor],
        *,
        source_frontier: int,
    ) -> None:
        """Import exact cursor evidence without running active consumer logic."""

        self._require_candidate_copy()
        if not cursors:
            return
        identities = [str(cursor.consumer_id) for cursor in cursors]
        if any(not identity for identity in identities) or len(set(identities)) != len(
            identities
        ):
            raise ValueError("snapshot cursor identities must be unique and nonempty")
        parameters: list[dict[str, Any]] = []
        decoded_metadata: dict[str, dict[str, Any]] = {}
        for cursor in cursors:
            if cursor.ingest_position < 0 or cursor.ingest_position > source_frontier:
                raise LifeEventConsumerConflict(
                    f"snapshot cursor exceeds source frontier: {cursor.consumer_id}"
                )
            if cursor.revision <= 0:
                raise ValueError("persisted snapshot cursor revision must be positive")
            metadata = json.loads(cursor.metadata_json)
            if not isinstance(metadata, dict):
                raise TypeError("snapshot cursor metadata must be an object")
            decoded_metadata[cursor.consumer_id] = metadata
            parameters.append(
                {
                    "consumer_id": cursor.consumer_id,
                    "ingest_position": int(cursor.ingest_position),
                    "revision": int(cursor.revision),
                    "updated_at": self._bind_time(cursor.updated_at),
                    "metadata_json": cursor.metadata_json,
                }
            )

        async def operation(session: AsyncSession) -> None:
            insert_sql = (
                "INSERT IGNORE INTO raw_event_consumer_offsets"
                if self.backend == BackendKind.MYSQL
                else "INSERT OR IGNORE INTO raw_event_consumer_offsets"
            )
            await session.execute(
                text(
                    f"""{insert_sql} (
                        consumer_id, ingest_position, revision,
                        updated_at, metadata_json
                    ) VALUES (
                        :consumer_id, :ingest_position, :revision,
                        :updated_at, :metadata_json
                    )"""
                ),
                parameters,
            )
            statement = text(
                """SELECT consumer_id, ingest_position, revision,
                updated_at, metadata_json FROM raw_event_consumer_offsets
                WHERE consumer_id IN :consumer_ids"""
            ).bindparams(bindparam("consumer_ids", expanding=True))
            rows = (
                (
                    await session.execute(
                        statement,
                        {"consumer_ids": identities},
                    )
                )
                .mappings()
                .all()
            )
            by_identity = {str(row["consumer_id"]): row for row in rows}
            for cursor in cursors:
                row = by_identity.get(cursor.consumer_id)
                if (
                    row is None
                    or int(row["ingest_position"]) != cursor.ingest_position
                    or int(row["revision"]) != cursor.revision
                    or _parse_datetime(row["updated_at"])
                    != _parse_datetime(cursor.updated_at)
                    or dict(_json_value(row["metadata_json"], default={}))
                    != decoded_metadata[cursor.consumer_id]
                ):
                    raise LifeEventConsumerConflict(
                        f"snapshot cursor conflict: {cursor.consumer_id}"
                    )

        await self._write(operation)

    async def _history_floor(self, session: AsyncSession) -> int:
        value = await session.scalar(
            text(
                "SELECT meta_value FROM raw_event_ledger_meta "
                "WHERE meta_key = 'history_floor_position'"
            )
        )
        return int(value or 0)

    async def read_since(
        self,
        position: int,
        *,
        limit: int | None = None,
    ) -> list[LifeEvent]:
        """Read by ordered token; numeric gaps alone never imply data loss."""

        requested = max(0, int(position))
        if limit is not None and int(limit) <= 0:
            return []
        async with self.runtime.unit_of_work() as uow:
            floor = await self._history_floor(uow.session)
            if requested < floor:
                raise RawEventGapError(requested, floor + 1)
            statement = """SELECT ingest_position, occurrence_id, source_sequence,
                recorded_at, payload_json FROM raw_life_events
                WHERE ingest_position > :position ORDER BY ingest_position"""
            parameters: dict[str, Any] = {"position": requested}
            if limit is not None:
                statement += " LIMIT :limit"
                parameters["limit"] = int(limit)
            rows = (
                (await uow.session.execute(text(statement), parameters))
                .mappings()
                .all()
            )
        return [self._decode_event(row) for row in rows]

    async def read_tail(self, limit: int = 100) -> list[LifeEvent]:
        """Read a bounded tail and return it in ascending position order."""

        if int(limit) <= 0:
            return []
        async with self.runtime.unit_of_work() as uow:
            rows = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT ingest_position, occurrence_id,
                            source_sequence, recorded_at, payload_json
                            FROM raw_life_events
                            ORDER BY ingest_position DESC LIMIT :limit"""
                        ),
                        {"limit": int(limit)},
                    )
                )
                .mappings()
                .all()
            )
        return [self._decode_event(row) for row in reversed(rows)]

    async def occurrence_digest(self, occurrence_id: str) -> LifeEventDigest | None:
        """Read one occurrence's immutable migration identity and digest."""

        identity = str(occurrence_id).strip()
        if not identity:
            raise ValueError("occurrence_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            row = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT ingest_position, occurrence_id, payload_hash
                            FROM raw_life_events
                            WHERE occurrence_id = :occurrence_id"""
                        ),
                        {"occurrence_id": identity},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return LifeEventDigest(
            occurrence_id=str(row["occurrence_id"]),
            position=int(row["ingest_position"]),
            payload_hash=str(row["payload_hash"]),
        )

    async def occurrence_digests(
        self,
        occurrence_ids: list[str],
    ) -> list[LifeEventDigest]:
        """Batch-read immutable digests without per-row database round trips."""

        identities = list(
            dict.fromkeys(
                str(value).strip() for value in occurrence_ids if str(value).strip()
            )
        )
        if not identities:
            return []
        statement = text(
            """SELECT ingest_position, occurrence_id, payload_hash
            FROM raw_life_events WHERE occurrence_id IN :occurrence_ids"""
        ).bindparams(bindparam("occurrence_ids", expanding=True))
        async with self.runtime.unit_of_work() as uow:
            rows = (
                (
                    await uow.session.execute(
                        statement,
                        {"occurrence_ids": identities},
                    )
                )
                .mappings()
                .all()
            )
        by_identity = {
            str(row["occurrence_id"]): LifeEventDigest(
                occurrence_id=str(row["occurrence_id"]),
                position=int(row["ingest_position"]),
                payload_hash=str(row["payload_hash"]),
            )
            for row in rows
        }
        return [by_identity[value] for value in identities if value in by_identity]

    async def snapshot_records_after(
        self,
        position: int,
        *,
        limit: int,
    ) -> list[LifeEventSnapshotRecord]:
        """Read exact ledger rows for a bounded audited export."""

        requested = max(0, int(position))
        bounded = int(limit)
        if bounded <= 0:
            return []
        async with self.runtime.unit_of_work() as uow:
            rows = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT ingest_position, occurrence_id,
                            source_event_id, source_sequence, occurred_at,
                            recorded_at, payload_json, payload_hash
                            FROM raw_life_events
                            WHERE ingest_position > :position
                            ORDER BY ingest_position LIMIT :limit"""
                        ),
                        {"position": requested, "limit": bounded},
                    )
                )
                .mappings()
                .all()
            )
        return [
            LifeEventSnapshotRecord(
                ingest_position=int(row["ingest_position"]),
                occurrence_id=str(row["occurrence_id"]),
                source_event_id=str(row["source_event_id"]),
                source_sequence=int(row["source_sequence"]),
                occurred_at=_iso(row["occurred_at"]),
                recorded_at=_iso(row["recorded_at"]),
                payload_json=str(row["payload_json"]),
                payload_hash=str(row["payload_hash"]),
            )
            for row in rows
        ]

    async def snapshot_cursors(self) -> list[LifeEventSnapshotCursor]:
        """Read exact durable cursor evidence for audited export."""

        async with self.runtime.unit_of_work() as uow:
            rows = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT consumer_id, ingest_position, revision,
                            updated_at, metadata_json
                            FROM raw_event_consumer_offsets
                            ORDER BY consumer_id"""
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [
            LifeEventSnapshotCursor(
                consumer_id=str(row["consumer_id"]),
                ingest_position=int(row["ingest_position"]),
                revision=int(row["revision"]),
                updated_at=_iso(row["updated_at"]),
                metadata_json=canonical_json(
                    dict(_json_value(row["metadata_json"], default={}))
                ),
            )
            for row in rows
        ]

    @staticmethod
    def _decode_cursor(row: Any, consumer_id: str) -> LifeEventConsumerCursor:
        if row is None:
            return LifeEventConsumerCursor(
                consumer_id=consumer_id,
                position=0,
                revision=0,
                updated_at="",
                metadata={},
            )
        return LifeEventConsumerCursor(
            consumer_id=consumer_id,
            position=int(row["ingest_position"]),
            revision=int(row["revision"]),
            updated_at=_iso(row["updated_at"]),
            metadata=dict(_json_value(row["metadata_json"], default={})),
        )

    async def consumer_cursor(self, consumer_id: str) -> LifeEventConsumerCursor:
        """Read one cursor without changing durable state."""

        identity = str(consumer_id).strip()
        if not identity:
            raise ValueError("consumer_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            row = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT ingest_position, revision, updated_at,
                            metadata_json FROM raw_event_consumer_offsets
                            WHERE consumer_id = :consumer_id"""
                        ),
                        {"consumer_id": identity},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._decode_cursor(row, identity)

    async def get_consumer_offset(self, consumer_id: str) -> int:
        """Compatibility read for existing at-least-once ledger consumers."""

        return (await self.consumer_cursor(consumer_id)).position

    async def commit_consumer_cursor(
        self,
        consumer_id: str,
        *,
        expected_position: int,
        expected_revision: int,
        through_position: int,
        metadata: dict[str, Any] | None = None,
    ) -> LifeEventConsumerCursor:
        """Advance a cursor with position+revision CAS and ledger bounds."""

        identity = str(consumer_id).strip()
        if not identity:
            raise ValueError("consumer_id must not be empty")
        expected = max(0, int(expected_position))
        expected_rev = max(0, int(expected_revision))
        through = max(0, int(through_position))
        if through < expected:
            raise LifeEventConsumerConflict(
                f"cursor regression: expected={expected}, through={through}"
            )

        async def operation(session: AsyncSession) -> LifeEventConsumerCursor:
            row = (
                (
                    await session.execute(
                        text(
                            """SELECT ingest_position, revision, updated_at,
                            metadata_json FROM raw_event_consumer_offsets
                            WHERE consumer_id = :consumer_id"""
                            + self._for_update
                        ),
                        {"consumer_id": identity},
                    )
                )
                .mappings()
                .one_or_none()
            )
            current = self._decode_cursor(row, identity)
            if current.position != expected or current.revision != expected_rev:
                raise LifeEventConsumerConflict(
                    f"cursor CAS failed for {identity}: expected "
                    f"({expected}, {expected_rev}), actual "
                    f"({current.position}, {current.revision})"
                )
            latest = int(
                await session.scalar(
                    text(
                        "SELECT COALESCE(MAX(ingest_position), 0) FROM raw_life_events"
                    )
                )
                or 0
            )
            if through > latest:
                raise LifeEventConsumerConflict(
                    f"cursor {through} exceeds ledger frontier {latest}"
                )
            if through == current.position:
                return current
            database_now = await self._database_now(session)
            next_revision = current.revision + 1
            encoded_metadata = canonical_json(metadata or {})
            if row is None:
                try:
                    await session.execute(
                        text(
                            """INSERT INTO raw_event_consumer_offsets (
                                consumer_id, ingest_position, revision,
                                updated_at, metadata_json
                            ) VALUES (
                                :consumer_id, :ingest_position, :revision,
                                :updated_at, :metadata_json
                            )"""
                        ),
                        {
                            "consumer_id": identity,
                            "ingest_position": through,
                            "revision": next_revision,
                            "updated_at": self._bind_time(database_now),
                            "metadata_json": encoded_metadata,
                        },
                    )
                except IntegrityError as exc:
                    raise LifeEventConsumerConflict(
                        f"concurrent cursor creation for {identity}"
                    ) from exc
            else:
                updated = await session.execute(
                    text(
                        """UPDATE raw_event_consumer_offsets
                        SET ingest_position = :through_position,
                            revision = :next_revision,
                            updated_at = :updated_at,
                            metadata_json = :metadata_json
                        WHERE consumer_id = :consumer_id
                          AND ingest_position = :expected_position
                          AND revision = :expected_revision"""
                    ),
                    {
                        "through_position": through,
                        "next_revision": next_revision,
                        "updated_at": self._bind_time(database_now),
                        "metadata_json": encoded_metadata,
                        "consumer_id": identity,
                        "expected_position": expected,
                        "expected_revision": expected_rev,
                    },
                )
                if updated.rowcount != 1:
                    raise LifeEventConsumerConflict(
                        f"concurrent cursor update for {identity}"
                    )
            return LifeEventConsumerCursor(
                consumer_id=identity,
                position=through,
                revision=next_revision,
                updated_at=database_now.isoformat(),
                metadata=dict(metadata or {}),
            )

        return await self._write(operation)

    async def commit_consumer_offset(
        self,
        consumer_id: str,
        ingest_position: int,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Compatibility monotonic advance implemented through revision CAS."""

        requested = max(0, int(ingest_position))
        for _ in range(_MAX_WRITE_ATTEMPTS):
            cursor = await self.consumer_cursor(consumer_id)
            if requested <= cursor.position:
                return cursor.position
            try:
                committed = await self.commit_consumer_cursor(
                    consumer_id,
                    expected_position=cursor.position,
                    expected_revision=cursor.revision,
                    through_position=requested,
                    metadata=metadata,
                )
            except LifeEventConsumerConflict:
                continue
            return committed.position
        raise LifeEventConsumerConflict(
            f"consumer cursor remained contended after {_MAX_WRITE_ATTEMPTS} attempts"
        )

    async def health_snapshot(self) -> dict[str, Any]:
        """Return bounded diagnostics without event payload contents."""

        async with self.runtime.unit_of_work() as uow:
            bounds = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT COUNT(*) AS total,
                            MIN(ingest_position) AS earliest,
                            MAX(ingest_position) AS latest FROM raw_life_events"""
                        )
                    )
                )
                .mappings()
                .one()
            )
            consumers = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT consumer_id, ingest_position, revision,
                            updated_at FROM raw_event_consumer_offsets
                            ORDER BY consumer_id LIMIT 200"""
                        )
                    )
                )
                .mappings()
                .all()
            )
            outbox = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT state, COUNT(*) AS total
                            FROM raw_event_export_outbox GROUP BY state"""
                        )
                    )
                )
                .mappings()
                .all()
            )
            floor = await self._history_floor(uow.session)
        latest = int(bounds["latest"] or 0)
        return {
            "status": "healthy",
            "backend": self.backend.value,
            "backend_identity": self.runtime.backend_identity,
            "total": int(bounds["total"] or 0),
            "earliest_position": int(bounds["earliest"] or 0),
            "latest_position": latest,
            "history_floor_position": floor,
            "consumers": [
                {
                    "consumer_id": str(row["consumer_id"]),
                    "position": int(row["ingest_position"]),
                    "revision": int(row["revision"]),
                    "lag": max(0, latest - int(row["ingest_position"])),
                    "updated_at": _iso(row["updated_at"]),
                }
                for row in consumers
            ],
            "export_outbox": {str(row["state"]): int(row["total"]) for row in outbox},
        }

    async def health(self) -> dict[str, Any]:
        """Compatibility alias for the existing async ledger health API."""

        return await self.health_snapshot()


class LocalLifeEventStore(SQLLifeEventStore):
    """SQLite-backed Life Event adapter."""


class MySQLLifeEventStore(SQLLifeEventStore):
    """MySQL-backed Life Event adapter."""


__all__ = [
    "LocalLifeEventStore",
    "MySQLLifeEventStore",
    "SQLLifeEventStore",
]
