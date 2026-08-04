"""Fenced SQL adapters for the shared subjective World projection."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.life_engine.service.event_bus import LifeEvent
from plugins.life_engine.service.world_projection import (
    WORLD_LEGACY_IMPORT_EVENT,
    WORLD_OBSERVATION_EVENT,
    WORLD_PROJECTOR_POLICY,
    WORLD_PROJECTOR_SCHEMA_VERSION,
    WORLD_REBUILD_FAILED,
    WORLD_REBUILD_IDLE,
    WORLD_REBUILDING,
    PerceptionCursorConflict,
    WorldAssertion,
    WorldProjectionChange,
    WorldProjectionConflict,
    WorldProjectionUnavailable,
)
from src.kernel.storage import canonical_json

from .contracts import StorageBackendRuntime
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
    if value is None or value == "":
        return ""
    parsed = _parse_datetime(value)
    if parsed is None:
        raise ValueError(f"invalid persisted World timestamp: {value!r}")
    return parsed.isoformat()


def _json_value(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str):
        return json.loads(value)
    return value


class SQLWorldProjectionStore:
    """Dialect-aware, source-preserving World projection implementation."""

    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if not runtime.enabled or runtime.engine is None:
            raise RuntimeError("World adapter requires an enabled storage runtime")
        self.runtime = runtime
        self.backend = runtime.backend

    @property
    def _for_update(self) -> str:
        return " FOR UPDATE" if self.backend == BackendKind.MYSQL else ""

    def _bind_time(self, value: Any) -> datetime | str | None:
        parsed = _parse_datetime(value)
        if self.backend == BackendKind.MYSQL:
            return parsed.replace(tzinfo=None) if parsed is not None else None
        return parsed.isoformat() if parsed is not None else ""

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
        raise AssertionError("bounded World retry loop exhausted unexpectedly")

    async def _meta(
        self,
        session: AsyncSession,
        key: str,
        *,
        for_update: bool = False,
    ) -> str | None:
        suffix = self._for_update if for_update else ""
        row = (
            (
                await session.execute(
                    text(
                        "SELECT meta_value FROM world_projection_meta "
                        f"WHERE meta_key = :meta_key{suffix}"
                    ),
                    {"meta_key": key},
                )
            )
            .mappings()
            .one_or_none()
        )
        return str(row["meta_value"]) if row is not None else None

    async def _set_meta(
        self,
        session: AsyncSession,
        key: str,
        value: str,
        *,
        database_now: datetime,
    ) -> None:
        updated = await session.execute(
            text(
                """UPDATE world_projection_meta
                SET meta_value = :meta_value, updated_at = :updated_at
                WHERE meta_key = :meta_key"""
            ),
            {
                "meta_key": key,
                "meta_value": value,
                "updated_at": self._bind_time(database_now),
            },
        )
        if updated.rowcount == 0:
            await session.execute(
                text(
                    """INSERT INTO world_projection_meta (
                        meta_key, meta_value, updated_at
                    ) VALUES (:meta_key, :meta_value, :updated_at)"""
                ),
                {
                    "meta_key": key,
                    "meta_value": value,
                    "updated_at": self._bind_time(database_now),
                },
            )

    async def initialize_contract(self) -> None:
        """Initialize immutable projector metadata or reject drift."""

        async def operation(session: AsyncSession) -> None:
            database_now = await self._database_now(session)
            expected = {
                "projector_policy": WORLD_PROJECTOR_POLICY,
                "projector_schema_version": str(WORLD_PROJECTOR_SCHEMA_VERSION),
            }
            for key, value in expected.items():
                actual = await self._meta(session, key, for_update=True)
                if actual is None:
                    await self._set_meta(
                        session,
                        key,
                        value,
                        database_now=database_now,
                    )
                elif actual != value:
                    raise WorldProjectionConflict(
                        f"world projector {key} mismatch: expected {value!r}, "
                        f"actual {actual!r}"
                    )
            for key, value in {
                "as_of_ingest_position": "0",
                "rebuild_state": WORLD_REBUILD_IDLE,
            }.items():
                if await self._meta(session, key, for_update=True) is None:
                    await self._set_meta(
                        session,
                        key,
                        value,
                        database_now=database_now,
                    )

        await self._write(operation)

    @staticmethod
    def _decode_object(content: str) -> dict[str, Any]:
        try:
            value = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return {"content": str(content or "")}
        if isinstance(value, dict):
            return value
        return {"content": value}

    @staticmethod
    def _assertion_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw_many = payload.get("assertions")
        if isinstance(raw_many, list):
            return [dict(item) for item in raw_many if isinstance(item, dict)]
        raw_one = payload.get("assertion")
        if isinstance(raw_one, dict):
            return [dict(raw_one)]
        if "subject" in payload or "predicate" in payload:
            return [dict(payload)]
        return []

    @staticmethod
    def _assertion_id(
        event: LifeEvent,
        assertion: dict[str, Any],
        index: int,
    ) -> str:
        supplied = str(assertion.get("assertion_id") or "").strip()
        if supplied:
            return supplied
        occurrence = event.occurrence_id or event.event_id
        return (
            "assertion_" + hashlib.sha256(f"{occurrence}:{index}".encode()).hexdigest()
        )

    async def _insert_assertion(
        self,
        session: AsyncSession,
        event: LifeEvent,
        assertion: dict[str, Any],
        index: int,
    ) -> None:
        assertion_id = self._assertion_id(event, assertion, index)
        subject = str(assertion.get("subject") or "").strip()
        predicate = str(assertion.get("predicate") or "").strip()
        if not subject or not predicate:
            raise ValueError("world assertions require non-empty subject and predicate")
        observed_at = str(assertion.get("observed_at") or event.timestamp)
        payload_json = canonical_json(assertion)
        occurrence_id = event.occurrence_id or event.event_id
        existing = (
            (
                await session.execute(
                    text(
                        "SELECT payload_json, occurrence_id FROM world_assertions "
                        f"WHERE assertion_id = :assertion_id{self._for_update}"
                    ),
                    {"assertion_id": assertion_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            existing_payload = canonical_json(
                _json_value(existing["payload_json"], default={})
            )
            if (
                existing_payload != payload_json
                or str(existing["occurrence_id"]) != occurrence_id
            ):
                raise WorldProjectionConflict(
                    f"assertion identity reused with different evidence: {assertion_id}"
                )
            return
        await session.execute(
            text(
                """INSERT INTO world_assertions (
                    assertion_id, subject, predicate, value_json, domain, status,
                    source_instance_id, source_event_id, occurrence_id, observed_at,
                    valid_from, valid_to, recorded_at, supersedes_assertion_id,
                    retracted_at, retracted_by_assertion_id, payload_json
                ) VALUES (
                    :assertion_id, :subject, :predicate, :value_json, :domain, :status,
                    :source_instance_id, :source_event_id, :occurrence_id, :observed_at,
                    :valid_from, :valid_to, :recorded_at, :supersedes_assertion_id,
                    :retracted_at, :retracted_by_assertion_id, :payload_json
                )"""
            ),
            {
                "assertion_id": assertion_id,
                "subject": subject,
                "predicate": predicate,
                "value_json": canonical_json(assertion.get("value")),
                "domain": str(assertion.get("domain") or ""),
                "status": str(assertion.get("status") or ""),
                "source_instance_id": str(
                    event.source_instance_id
                    or assertion.get("source_instance_id")
                    or ""
                ),
                "source_event_id": event.event_id,
                "occurrence_id": occurrence_id,
                "observed_at": self._bind_time(observed_at),
                "valid_from": self._bind_time(
                    assertion.get("valid_from") or observed_at
                ),
                "valid_to": self._bind_time(assertion.get("valid_to")),
                "recorded_at": self._bind_time(event.recorded_at),
                "supersedes_assertion_id": str(
                    assertion.get("supersedes_assertion_id") or ""
                ),
                "retracted_at": self._bind_time(None),
                "retracted_by_assertion_id": "",
                "payload_json": payload_json,
            },
        )
        retracts = str(assertion.get("retracts_assertion_id") or "").strip()
        if retracts:
            unretracted = (
                "retracted_at IS NULL"
                if self.backend == BackendKind.MYSQL
                else "retracted_at = ''"
            )
            await session.execute(
                text(
                    f"""UPDATE world_assertions SET
                        retracted_at = :retracted_at,
                        retracted_by_assertion_id = :assertion_id
                    WHERE assertion_id = :retracts
                      AND {unretracted}"""
                ),
                {
                    "retracted_at": self._bind_time(observed_at),
                    "assertion_id": assertion_id,
                    "retracts": retracts,
                },
            )

    @staticmethod
    def _expected_change(
        event: LifeEvent,
        *,
        change_kind: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "occurrence_id": event.occurrence_id or event.event_id,
            "event_type": event.event_type,
            "change_kind": change_kind,
            "source_instance_id": event.source_instance_id,
            "stream_id": event.stream_id,
            "occurred_at": _iso(event.timestamp),
            "recorded_at": _iso(event.recorded_at),
            "payload_json": canonical_json(payload),
        }

    async def _insert_change(
        self,
        session: AsyncSession,
        event: LifeEvent,
        *,
        change_kind: str,
        payload: dict[str, Any],
    ) -> None:
        expected = self._expected_change(
            event,
            change_kind=change_kind,
            payload=payload,
        )
        existing = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM world_projection_changes "
                        f"WHERE ingest_position = :position{self._for_update}"
                    ),
                    {"position": event.sequence},
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            actual = {
                "event_id": str(existing["event_id"]),
                "occurrence_id": str(existing["occurrence_id"]),
                "event_type": str(existing["event_type"]),
                "change_kind": str(existing["change_kind"]),
                "source_instance_id": str(existing["source_instance_id"] or ""),
                "stream_id": str(existing["stream_id"] or ""),
                "occurred_at": _iso(existing["occurred_at"]),
                "recorded_at": _iso(existing["recorded_at"]),
                "payload_json": canonical_json(
                    _json_value(existing["payload_json"], default={})
                ),
            }
            if actual != expected:
                raise WorldProjectionConflict(
                    "world ingest position reused with different evidence: "
                    f"{event.sequence}"
                )
            return
        await session.execute(
            text(
                """INSERT INTO world_projection_changes (
                    ingest_position, event_id, occurrence_id, event_type,
                    change_kind, source_instance_id, stream_id, occurred_at,
                    recorded_at, payload_json
                ) VALUES (
                    :ingest_position, :event_id, :occurrence_id, :event_type,
                    :change_kind, :source_instance_id, :stream_id, :occurred_at,
                    :recorded_at, :payload_json
                )"""
            ),
            {
                **expected,
                "ingest_position": event.sequence,
                "occurred_at": self._bind_time(event.timestamp),
                "recorded_at": self._bind_time(event.recorded_at),
            },
        )

    async def _apply_event(self, session: AsyncSession, event: LifeEvent) -> None:
        payload = self._decode_object(event.content)
        if event.event_type in {WORLD_OBSERVATION_EVENT, WORLD_LEGACY_IMPORT_EVENT}:
            for index, assertion in enumerate(self._assertion_payloads(payload)):
                await self._insert_assertion(session, event, assertion, index)
            await self._insert_change(
                session,
                event,
                change_kind="world_observation",
                payload=payload,
            )
        elif event.event_type.startswith("consciousness.instance_") or (
            event.event_type == "consciousness.chat_global_recovered"
        ):
            await self._insert_change(
                session,
                event,
                change_kind="consciousness_presence",
                payload=payload,
            )

    async def apply_events(self, events: list[LifeEvent]) -> int:
        if not events:
            return int((await self.projector_contract())["as_of_ingest_position"])
        ordered = sorted(events, key=lambda item: item.sequence)

        async def operation(session: AsyncSession) -> int:
            state = await self._meta(session, "rebuild_state", for_update=True)
            if state not in {WORLD_REBUILD_IDLE, WORLD_REBUILDING}:
                raise WorldProjectionUnavailable(
                    f"world projection cannot advance: rebuild_state={state}"
                )
            current = int(
                await self._meta(
                    session,
                    "as_of_ingest_position",
                    for_update=True,
                )
                or 0
            )
            for event in ordered:
                if event.sequence <= 0:
                    raise ValueError(
                        "world projection requires positive ledger positions"
                    )
                if event.sequence > current + 1:
                    raise WorldProjectionConflict(
                        "world projector ledger gap: "
                        f"expected {current + 1}, actual {event.sequence}"
                    )
                await self._apply_event(session, event)
                current = max(current, event.sequence)
            await self._set_meta(
                session,
                "as_of_ingest_position",
                str(current),
                database_now=await self._database_now(session),
            )
            return current

        return await self._write(operation)

    async def begin_rebuild(self) -> None:
        async def operation(session: AsyncSession) -> None:
            database_now = await self._database_now(session)
            await self._meta(session, "as_of_ingest_position", for_update=True)
            await session.execute(text("DELETE FROM world_assertions"))
            await session.execute(text("DELETE FROM world_projection_changes"))
            await self._set_meta(
                session,
                "as_of_ingest_position",
                "0",
                database_now=database_now,
            )
            await self._set_meta(
                session,
                "rebuild_state",
                WORLD_REBUILDING,
                database_now=database_now,
            )

        await self._write(operation)

    async def finish_rebuild(self, *, expected_frontier: int) -> None:
        expected = int(expected_frontier)
        if expected < 0:
            raise ValueError("expected rebuild frontier must not be negative")

        async def operation(session: AsyncSession) -> None:
            state = await self._meta(session, "rebuild_state", for_update=True)
            current = int(
                await self._meta(
                    session,
                    "as_of_ingest_position",
                    for_update=True,
                )
                or 0
            )
            if state != WORLD_REBUILDING or current != expected:
                raise WorldProjectionConflict(
                    "world rebuild completion mismatch: "
                    f"state={state}, expected_frontier={expected}, actual={current}"
                )
            await self._set_meta(
                session,
                "rebuild_state",
                WORLD_REBUILD_IDLE,
                database_now=await self._database_now(session),
            )

        await self._write(operation)

    async def fail_rebuild(self) -> None:
        async def operation(session: AsyncSession) -> None:
            state = await self._meta(session, "rebuild_state", for_update=True)
            if state != WORLD_REBUILDING:
                raise WorldProjectionConflict(
                    f"cannot fail world rebuild from state {state}"
                )
            await self._set_meta(
                session,
                "rebuild_state",
                WORLD_REBUILD_FAILED,
                database_now=await self._database_now(session),
            )

        await self._write(operation)

    async def projector_contract(self) -> dict[str, Any]:
        async with self.runtime.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """SELECT meta_key, meta_value FROM world_projection_meta
                        WHERE meta_key IN (
                            'projector_policy', 'projector_schema_version',
                            'as_of_ingest_position', 'rebuild_state'
                        )"""
                    )
                )
            ).mappings()
            values = {str(row["meta_key"]): str(row["meta_value"]) for row in rows}
        return {
            "policy": values.get("projector_policy", ""),
            "schema_version": int(values.get("projector_schema_version", "0")),
            "as_of_ingest_position": int(values.get("as_of_ingest_position", "0")),
            "rebuild_state": values.get("rebuild_state", ""),
        }

    @staticmethod
    def _assertion_from_row(row: Any) -> WorldAssertion:
        return WorldAssertion(
            assertion_id=str(row["assertion_id"]),
            subject=str(row["subject"]),
            predicate=str(row["predicate"]),
            value=_json_value(row["value_json"], default=None),
            domain=str(row["domain"] or ""),
            status=str(row["status"] or ""),
            source_instance_id=str(row["source_instance_id"] or ""),
            source_event_id=str(row["source_event_id"]),
            occurrence_id=str(row["occurrence_id"]),
            observed_at=_iso(row["observed_at"]),
            valid_from=_iso(row["valid_from"]),
            valid_to=_iso(row["valid_to"]),
            recorded_at=_iso(row["recorded_at"]),
            supersedes_assertion_id=str(row["supersedes_assertion_id"] or ""),
            retracted_at=_iso(row["retracted_at"]),
            retracted_by_assertion_id=str(row["retracted_by_assertion_id"] or ""),
            payload=dict(_json_value(row["payload_json"], default={})),
        )

    async def list_assertions(
        self,
        *,
        include_retracted: bool = True,
    ) -> list[WorldAssertion]:
        sql = "SELECT * FROM world_assertions"
        if not include_retracted:
            sql += (
                " WHERE retracted_at IS NULL"
                if self.backend == BackendKind.MYSQL
                else " WHERE retracted_at = ''"
            )
        sql += " ORDER BY observed_at, assertion_id"
        async with self.runtime.engine.connect() as connection:
            rows = (await connection.execute(text(sql))).mappings()
            return [self._assertion_from_row(row) for row in rows]

    @staticmethod
    def _change_from_row(row: Any) -> WorldProjectionChange:
        return WorldProjectionChange(
            ingest_position=int(row["ingest_position"]),
            event_id=str(row["event_id"]),
            occurrence_id=str(row["occurrence_id"]),
            event_type=str(row["event_type"]),
            change_kind=str(row["change_kind"]),
            source_instance_id=str(row["source_instance_id"] or ""),
            stream_id=str(row["stream_id"] or ""),
            occurred_at=_iso(row["occurred_at"]),
            recorded_at=_iso(row["recorded_at"]),
            payload=dict(_json_value(row["payload_json"], default={})),
        )

    async def changes_since(
        self,
        ingest_position: int,
        *,
        through_position: int | None = None,
    ) -> list[WorldProjectionChange]:
        start = int(ingest_position)
        if start < 0:
            raise ValueError("world change position must not be negative")
        sql = "SELECT * FROM world_projection_changes WHERE ingest_position > :start"
        params: dict[str, Any] = {"start": start}
        if through_position is not None:
            through = int(through_position)
            if through < start:
                raise ValueError("world change window cannot move backwards")
            sql += " AND ingest_position <= :through"
            params["through"] = through
        sql += " ORDER BY ingest_position"
        async with self.runtime.engine.connect() as connection:
            rows = (await connection.execute(text(sql), params)).mappings()
            return [self._change_from_row(row) for row in rows]

    async def perception_cursor(self, instance_id: str) -> tuple[int, int]:
        identity = str(instance_id or "").strip()
        if not identity:
            raise ValueError("perception cursor instance_id must not be empty")
        async with self.runtime.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """SELECT ingest_position, revision
                        FROM world_perception_cursors
                        WHERE instance_id = :instance_id"""
                        ),
                        {"instance_id": identity},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return (
            (0, 0)
            if row is None
            else (
                int(row["ingest_position"]),
                int(row["revision"]),
            )
        )

    async def commit_perception_cursor(
        self,
        instance_id: str,
        *,
        expected_position: int,
        expected_revision: int,
        through_position: int,
    ) -> tuple[int, int]:
        identity = str(instance_id or "").strip()
        expected = int(expected_position)
        revision = int(expected_revision)
        through = int(through_position)
        if not identity:
            raise ValueError("perception cursor instance_id must not be empty")
        if min(expected, revision, through) < 0:
            raise ValueError("perception cursor values must not be negative")
        if through < expected:
            raise ValueError("perception cursor cannot move backwards")

        async def operation(session: AsyncSession) -> tuple[int, int]:
            state = await self._meta(session, "rebuild_state", for_update=True)
            if state != WORLD_REBUILD_IDLE:
                raise WorldProjectionUnavailable(
                    f"world projection is not deliverable: rebuild_state={state}"
                )
            frontier = int(
                await self._meta(
                    session,
                    "as_of_ingest_position",
                    for_update=True,
                )
                or 0
            )
            if through > frontier:
                raise ValueError(
                    "perception cursor cannot advance beyond projection frontier"
                )
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT ingest_position, revision "
                            "FROM world_perception_cursors "
                            f"WHERE instance_id = :instance_id{self._for_update}"
                        ),
                        {"instance_id": identity},
                    )
                )
                .mappings()
                .one_or_none()
            )
            current = int(row["ingest_position"]) if row is not None else 0
            current_revision = int(row["revision"]) if row is not None else 0
            if current != expected or current_revision != revision:
                raise PerceptionCursorConflict(
                    f"stale perception cursor for '{identity}': "
                    f"expected ({expected}, {revision}), "
                    f"actual ({current}, {current_revision})"
                )
            if through == current:
                return current, current_revision
            next_revision = current_revision + 1
            database_now = await self._database_now(session)
            if row is None:
                try:
                    await session.execute(
                        text(
                            """INSERT INTO world_perception_cursors (
                                instance_id, ingest_position, revision, updated_at
                            ) VALUES (
                                :instance_id, :ingest_position, :revision, :updated_at
                            )"""
                        ),
                        {
                            "instance_id": identity,
                            "ingest_position": through,
                            "revision": next_revision,
                            "updated_at": self._bind_time(database_now),
                        },
                    )
                except IntegrityError as exc:
                    raise PerceptionCursorConflict(
                        f"concurrent perception cursor insert for '{identity}'"
                    ) from exc
            else:
                updated = await session.execute(
                    text(
                        """UPDATE world_perception_cursors
                        SET ingest_position = :through,
                            revision = :next_revision,
                            updated_at = :updated_at
                        WHERE instance_id = :instance_id
                          AND ingest_position = :expected
                          AND revision = :revision"""
                    ),
                    {
                        "through": through,
                        "next_revision": next_revision,
                        "updated_at": self._bind_time(database_now),
                        "instance_id": identity,
                        "expected": expected,
                        "revision": revision,
                    },
                )
                if updated.rowcount != 1:
                    raise PerceptionCursorConflict(
                        f"concurrent perception cursor update for '{identity}'"
                    )
            return through, next_revision

        return await self._write(operation)

    async def health_snapshot(self) -> dict[str, Any]:
        contract = await self.projector_contract()
        async with self.runtime.engine.connect() as connection:
            assertion_count = await connection.scalar(
                text("SELECT COUNT(*) FROM world_assertions")
            )
            change_count = await connection.scalar(
                text("SELECT COUNT(*) FROM world_projection_changes")
            )
            cursors = (
                await connection.execute(
                    text(
                        """SELECT instance_id, ingest_position, revision, updated_at
                        FROM world_perception_cursors ORDER BY instance_id"""
                    )
                )
            ).mappings()
            cursor_health = [
                {
                    "instance_id": str(row["instance_id"]),
                    "position": int(row["ingest_position"]),
                    "revision": int(row["revision"]),
                    "lag": max(
                        0,
                        int(contract["as_of_ingest_position"])
                        - int(row["ingest_position"]),
                    ),
                    "updated_at": _iso(row["updated_at"]),
                }
                for row in cursors
            ]
        return {
            "backend": self.backend.value,
            **contract,
            "assertion_count": int(assertion_count or 0),
            "change_count": int(change_count or 0),
            "cursors": cursor_health,
        }


class LocalWorldProjectionStore(SQLWorldProjectionStore):
    """First-class World adapter backed by the selected SQLite runtime."""

    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if runtime.backend != BackendKind.LOCAL:
            raise ValueError("LocalWorldProjectionStore requires the local backend")
        super().__init__(runtime)


class MySQLWorldProjectionStore(SQLWorldProjectionStore):
    """Shared World adapter backed by the selected MySQL runtime."""

    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if runtime.backend != BackendKind.MYSQL:
            raise ValueError("MySQLWorldProjectionStore requires the MySQL backend")
        super().__init__(runtime)


__all__ = [
    "LocalWorldProjectionStore",
    "MySQLWorldProjectionStore",
    "SQLWorldProjectionStore",
]
