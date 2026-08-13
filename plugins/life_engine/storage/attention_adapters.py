"""Fenced SQL authority for subject-level persistent attention threads."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage import canonical_json

from ..attention_threads.contracts import (
    AttentionThreadActorInactive,
    AttentionThreadCommand,
    AttentionThreadCommit,
    AttentionThreadConflict,
    AttentionThreadEvent,
    AttentionThreadEventPage,
    AttentionThreadPage,
    AttentionThreadPageQuery,
    AttentionThreadProjectionConflict,
    AttentionThreadValueChunk,
    AttentionThreadView,
    InstanceFocus,
)
from ..attention_threads.models import (
    apply_attention_thread_event,
)
from ..attention_threads.projection import (
    build_attention_thread_projection,
)
from .contracts import StorageBackendRuntime
from .models import BackendKind

_T = TypeVar("_T")
_MAX_WRITE_ATTEMPTS = 3
_MAX_CHUNK_BYTES = 256 * 1024


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
        raise ValueError(f"invalid attention timestamp: {value!r}")
    return parsed.isoformat()


def _json_string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RuntimeError("stored attention source occurrences are corrupt")
    return tuple(value)


class SQLAttentionThreadStore:
    """Authority and ephemeral focus store bound to one coherent runtime."""

    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if not runtime.enabled or runtime.engine is None:
            raise RuntimeError("attention adapter requires an enabled storage runtime")
        self.runtime = runtime
        self.backend = runtime.backend

    @property
    def _for_update(self) -> str:
        return " FOR UPDATE" if self.backend == BackendKind.MYSQL else ""

    def _bind_time(self, value: Any) -> datetime | str:
        parsed = _parse_datetime(value)
        if parsed is None:
            raise ValueError(f"invalid attention timestamp: {value!r}")
        if self.backend == BackendKind.MYSQL:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed.astimezone(UTC).isoformat()

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
        raise AssertionError("bounded attention retry loop exhausted unexpectedly")

    async def _assert_active_actor(
        self,
        session: AsyncSession,
        actor_id: str,
        *,
        database_now: datetime,
    ) -> None:
        row = (
            (
                await session.execute(
                    text(
                        """SELECT status, lease_expires_at
                        FROM consciousness_presence
                        WHERE instance_id = :instance_id"""
                        + self._for_update
                    ),
                    {"instance_id": actor_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or str(row["status"]) != "active":
            raise AttentionThreadActorInactive(actor_id)
        lease_expires_at = _parse_datetime(row["lease_expires_at"])
        if lease_expires_at is not None and lease_expires_at <= database_now:
            raise AttentionThreadActorInactive(actor_id)

    @staticmethod
    def _decode_event(row: Any) -> AttentionThreadEvent:
        return AttentionThreadEvent(
            position=int(row["position"]),
            event_id=str(row["event_id"]),
            occurrence_id=str(row["occurrence_id"]),
            thread_id=str(row["thread_id"]),
            action=str(row["action"]),  # type: ignore[arg-type]
            actor_consciousness_instance_id=str(
                row["actor_consciousness_instance_id"]
            ),
            source_instance_id=str(row["source_instance_id"]),
            source_occurrence_ids=_json_string_tuple(
                row["source_occurrence_ids_json"]
            ),
            causation_occurrence_id=str(row["causation_occurrence_id"]),
            expected_revision=int(row["expected_revision"]),
            revision=int(row["revision"]),
            public_statement=str(row["public_statement"]),
            occurred_at=_iso(row["occurred_at"]),
            recorded_at=_iso(row["recorded_at"]),
            event_sha256=str(row["event_sha256"]),
        )

    @staticmethod
    def _decode_view(row: Any) -> AttentionThreadView:
        return AttentionThreadView(
            thread_id=str(row["thread_id"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            revision=int(row["revision"]),
            opened_at=_iso(row["opened_at"]),
            last_changed_at=_iso(row["last_changed_at"]),
            current_statement=str(row["current_statement"]),
            statement_event_id=str(row["statement_event_id"]),
            statement_sha256=str(row["statement_sha256"]),
            statement_bytes=int(row["statement_bytes"]),
            last_event_id=str(row["last_event_id"]),
            last_occurrence_id=str(row["last_occurrence_id"]),
            last_event_position=int(row["last_event_position"]),
        )

    @staticmethod
    def _decode_focus(row: Any) -> InstanceFocus:
        return InstanceFocus(
            instance_id=str(row["instance_id"]),
            focus_occurrence_id=str(row["focus_occurrence_id"]),
            source_occurrence_id=str(row["source_occurrence_id"]),
            entered_at=_iso(row["entered_at"]),
            expires_at=_iso(row["expires_at"]),
            revision=int(row["revision"]),
            thread_id=str(row["thread_id"]),
        )

    @staticmethod
    def _event_columns() -> str:
        return """position, event_id, occurrence_id, thread_id, action,
            actor_consciousness_instance_id, source_instance_id,
            source_occurrence_ids_json, causation_occurrence_id,
            expected_revision, revision, public_statement, occurred_at,
            recorded_at, event_sha256"""

    @staticmethod
    def _view_columns() -> str:
        return """thread_id, status, revision, opened_at, last_changed_at,
            current_statement, statement_event_id, statement_sha256,
            statement_bytes, last_event_id, last_occurrence_id,
            last_event_position"""

    async def _event_by_occurrence_in_session(
        self,
        session: AsyncSession,
        occurrence_id: str,
    ) -> AttentionThreadEvent | None:
        row = (
            (
                await session.execute(
                    text(
                        f"""SELECT {self._event_columns()}
                        FROM attention_thread_events
                        WHERE occurrence_id = :occurrence_id"""
                        + self._for_update
                    ),
                    {"occurrence_id": occurrence_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._decode_event(row) if row is not None else None

    async def _view_in_session(
        self,
        session: AsyncSession,
        thread_id: str,
        *,
        for_update: bool,
    ) -> AttentionThreadView | None:
        row = (
            (
                await session.execute(
                    text(
                        f"""SELECT {self._view_columns()}
                        FROM attention_thread_heads
                        WHERE thread_id = :thread_id"""
                        + (self._for_update if for_update else "")
                    ),
                    {"thread_id": thread_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._decode_view(row) if row is not None else None

    async def _insert_event(
        self,
        session: AsyncSession,
        command: AttentionThreadCommand,
        *,
        recorded_at: datetime,
    ) -> AttentionThreadEvent:
        digest = command.canonical_sha256()
        event_id = f"attention:event:{digest[:32]}"
        insert_prefix = (
            "INSERT IGNORE" if self.backend == BackendKind.MYSQL else "INSERT OR IGNORE"
        )
        await session.execute(
            text(
                f"""{insert_prefix} INTO attention_thread_events (
                    event_id, occurrence_id, thread_id, action,
                    actor_consciousness_instance_id, source_instance_id,
                    source_occurrence_ids_json, causation_occurrence_id,
                    expected_revision, revision, public_statement,
                    occurred_at, recorded_at, event_sha256
                ) VALUES (
                    :event_id, :occurrence_id, :thread_id, :action,
                    :actor, :source_instance_id, :source_occurrences,
                    :causation, :expected_revision, :revision, :statement,
                    :occurred_at, :recorded_at, :event_sha256
                )"""
            ),
            {
                "event_id": event_id,
                "occurrence_id": command.occurrence_id,
                "thread_id": command.thread_id,
                "action": command.action,
                "actor": command.actor_consciousness_instance_id,
                "source_instance_id": command.source_instance_id,
                "source_occurrences": canonical_json(
                    list(command.source_occurrence_ids)
                ),
                "causation": command.causation_occurrence_id,
                "expected_revision": command.expected_revision,
                "revision": command.expected_revision + 1,
                "statement": command.public_statement,
                "occurred_at": self._bind_time(command.occurred_at),
                "recorded_at": self._bind_time(recorded_at),
                "event_sha256": digest,
            },
        )
        persisted = await self._event_by_occurrence_in_session(
            session,
            command.occurrence_id,
        )
        if persisted is None:
            raise RuntimeError(f"AttentionThreadEventInsertLost:{command.occurrence_id}")
        if persisted.event_sha256 != digest:
            raise AttentionThreadConflict(command.occurrence_id)
        return persisted

    async def _write_head(
        self,
        session: AsyncSession,
        previous: AttentionThreadView | None,
        current: AttentionThreadView,
        *,
        database_now: datetime,
    ) -> None:
        parameters = {
            "thread_id": current.thread_id,
            "status": current.status,
            "revision": current.revision,
            "opened_at": self._bind_time(current.opened_at),
            "last_changed_at": self._bind_time(current.last_changed_at),
            "current_statement": current.current_statement,
            "statement_event_id": current.statement_event_id,
            "statement_sha256": current.statement_sha256,
            "statement_bytes": current.statement_bytes,
            "last_event_id": current.last_event_id,
            "last_occurrence_id": current.last_occurrence_id,
            "last_event_position": current.last_event_position,
            "updated_at": self._bind_time(database_now),
        }
        if previous is None:
            await session.execute(
                text(
                    """INSERT INTO attention_thread_heads (
                        thread_id, status, revision, opened_at, last_changed_at,
                        current_statement, statement_event_id, statement_sha256,
                        statement_bytes, last_event_id, last_occurrence_id,
                        last_event_position, updated_at
                    ) VALUES (
                        :thread_id, :status, :revision, :opened_at,
                        :last_changed_at, :current_statement, :statement_event_id,
                        :statement_sha256, :statement_bytes, :last_event_id,
                        :last_occurrence_id, :last_event_position, :updated_at
                    )"""
                ),
                parameters,
            )
            return
        parameters["expected_revision"] = previous.revision
        result = await session.execute(
            text(
                """UPDATE attention_thread_heads SET
                    status = :status, revision = :revision,
                    last_changed_at = :last_changed_at,
                    current_statement = :current_statement,
                    statement_event_id = :statement_event_id,
                    statement_sha256 = :statement_sha256,
                    statement_bytes = :statement_bytes,
                    last_event_id = :last_event_id,
                    last_occurrence_id = :last_occurrence_id,
                    last_event_position = :last_event_position,
                    updated_at = :updated_at
                WHERE thread_id = :thread_id
                  AND revision = :expected_revision"""
            ),
            parameters,
        )
        if result.rowcount != 1:
            raise AttentionThreadConflict(
                current.thread_id,
                thread_id=current.thread_id,
                current_revision=(
                    previous.revision if previous is not None else 0
                ),
                thread_exists=previous is not None,
            )

    async def decide(
        self,
        command: AttentionThreadCommand,
    ) -> AttentionThreadCommit:
        """Atomically gate the actor, append the event, and CAS the view."""

        async def operation(session: AsyncSession) -> AttentionThreadCommit:
            replay = await self._event_by_occurrence_in_session(
                session,
                command.occurrence_id,
            )
            digest = command.canonical_sha256()
            if replay is not None:
                if replay.event_sha256 != digest:
                    raise AttentionThreadConflict(command.occurrence_id)
                status = {
                    "pause": "paused",
                    "close": "closed",
                }.get(replay.action, "open")
                return AttentionThreadCommit(
                    event_id=replay.event_id,
                    occurrence_id=replay.occurrence_id,
                    thread_id=replay.thread_id,
                    revision=replay.revision,
                    status=status,  # type: ignore[arg-type]
                    idempotent_replay=True,
                )

            database_now = await self._database_now(session)
            await self._assert_active_actor(
                session,
                command.actor_consciousness_instance_id,
                database_now=database_now,
            )
            previous = await self._view_in_session(
                session,
                command.thread_id,
                for_update=True,
            )
            if (previous is None and command.expected_revision != 0) or (
                previous is not None
                and previous.revision != command.expected_revision
            ):
                raise AttentionThreadConflict(
                    command.thread_id,
                    thread_id=command.thread_id,
                    current_revision=(
                        previous.revision if previous is not None else 0
                    ),
                    thread_exists=previous is not None,
                )
            event = await self._insert_event(
                session,
                command,
                recorded_at=database_now,
            )
            current = apply_attention_thread_event(previous, event)
            await self._write_head(
                session,
                previous,
                current,
                database_now=database_now,
            )
            return AttentionThreadCommit(
                event_id=event.event_id,
                occurrence_id=event.occurrence_id,
                thread_id=event.thread_id,
                revision=event.revision,
                status=current.status,
                idempotent_replay=False,
            )

        return await self._write(operation)

    async def get(self, thread_id: str) -> AttentionThreadView | None:
        identity = str(thread_id or "").strip()
        if not identity:
            raise ValueError("thread_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            return await self._view_in_session(
                uow.session,
                identity,
                for_update=False,
            )

    @staticmethod
    def _query_fingerprint(query: AttentionThreadPageQuery) -> str:
        material = {
            "statuses": list(query.statuses),
            "limit": query.limit,
            "max_bytes": query.max_bytes,
            "projection_kind": query.projection_kind,
            "focus_instance_id": query.focus_instance_id,
        }
        return hashlib.sha256(canonical_json(material).encode()).hexdigest()

    @classmethod
    def _encode_continuation(
        cls,
        *,
        source_frontier: int,
        last_position: int,
        last_thread_id: str,
        query: AttentionThreadPageQuery,
    ) -> str:
        payload = {
            "v": 1,
            "source_frontier": source_frontier,
            "last_position": last_position,
            "last_thread_id": last_thread_id,
            "query_sha256": cls._query_fingerprint(query),
        }
        encoded = canonical_json(payload)
        envelope = {
            "payload": payload,
            "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        }
        return base64.urlsafe_b64encode(canonical_json(envelope).encode()).decode()

    @classmethod
    def _decode_continuation(
        cls,
        value: str,
        *,
        query: AttentionThreadPageQuery,
    ) -> tuple[int, int, str]:
        try:
            decoded = base64.urlsafe_b64decode(value.encode()).decode()
            envelope = json.loads(decoded)
            payload = envelope["payload"]
            expected = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
            if envelope["sha256"] != expected:
                raise ValueError("continuation checksum changed")
            if payload["v"] != 1:
                raise ValueError("unsupported continuation version")
            if payload["query_sha256"] != cls._query_fingerprint(query):
                raise ValueError("continuation query changed")
            return (
                int(payload["source_frontier"]),
                int(payload["last_position"]),
                str(payload["last_thread_id"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AttentionThreadProjectionConflict(
                "attention continuation is invalid"
            ) from exc

    async def page(self, query: AttentionThreadPageQuery) -> AttentionThreadPage:
        """Return a stable, content-bounded current-view page."""

        async with self.runtime.unit_of_work() as uow:
            session = uow.session
            source_frontier = int(
                await session.scalar(
                    text("SELECT COALESCE(MAX(position), 0) FROM attention_thread_events")
                )
                or 0
            )
            last_position = 0
            last_thread_id = ""
            if query.continuation:
                expected_frontier, last_position, last_thread_id = (
                    self._decode_continuation(query.continuation, query=query)
                )
                if expected_frontier != source_frontier:
                    raise AttentionThreadProjectionConflict(
                        "attention continuation source frontier changed"
                    )

            parameters: dict[str, Any] = {"limit": query.limit + 1}
            conditions: list[str] = []
            if query.statuses:
                conditions.append("status IN :statuses")
                parameters["statuses"] = query.statuses
            if query.continuation:
                conditions.append(
                    "(last_event_position < :last_position OR "
                    "(last_event_position = :last_position "
                    "AND thread_id > :last_thread_id))"
                )
                parameters.update(
                    {
                        "last_position": last_position,
                        "last_thread_id": last_thread_id,
                    }
                )
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            statement = text(
                f"""SELECT {self._view_columns()}
                FROM attention_thread_heads{where}
                ORDER BY last_event_position DESC, thread_id ASC
                LIMIT :limit"""
            )
            if query.statuses:
                statement = statement.bindparams(bindparam("statuses", expanding=True))
            rows = (await session.execute(statement, parameters)).mappings().all()
            views = [self._decode_view(row) for row in rows[: query.limit]]
            count_statement = text(
                f"""SELECT COUNT(*) AS item_count,
                    COALESCE(SUM(statement_bytes), 0) AS statement_bytes
                FROM attention_thread_heads{where}"""
            )
            if query.statuses:
                count_statement = count_statement.bindparams(
                    bindparam("statuses", expanding=True)
                )
            count_parameters = dict(parameters)
            count_parameters.pop("limit", None)
            totals = (
                (await session.execute(count_statement, count_parameters))
                .mappings()
                .one()
            )
            focus = None
            if query.focus_instance_id:
                database_now = await self._database_now(session)
                focus_row = (
                    (
                        await session.execute(
                            text(
                                """SELECT instance_id, focus_occurrence_id,
                                source_occurrence_id, entered_at, expires_at,
                                revision, thread_id
                                FROM attention_instance_focus
                                WHERE instance_id = :instance_id"""
                            ),
                            {"instance_id": query.focus_instance_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                candidate_focus = (
                    self._decode_focus(focus_row)
                    if focus_row is not None
                    else None
                )
                if candidate_focus is not None and (
                    datetime.fromisoformat(candidate_focus.expires_at) > database_now
                ):
                    focus = candidate_focus

        provisional = build_attention_thread_projection(
            views,
            source_frontier=source_frontier,
            projection_revision=source_frontier,
            max_bytes=query.max_bytes,
            projection_kind=query.projection_kind,
            focus=focus,
        )
        delivered_count = len(provisional.items)
        total_count = int(totals["item_count"])
        next_token = ""
        if delivered_count < total_count:
            if delivered_count == 0:
                raise RuntimeError("attention projection budget cannot deliver one ref")
            last = provisional.items[-1]
            next_token = self._encode_continuation(
                source_frontier=source_frontier,
                last_position=last.last_event_position,
                last_thread_id=last.thread_id,
                query=query,
            )
        page = build_attention_thread_projection(
            views,
            source_frontier=source_frontier,
            projection_revision=source_frontier,
            max_bytes=query.max_bytes,
            continuation=next_token,
            projection_kind=query.projection_kind,
            focus=focus,
        )
        return replace(
            page,
            original_bytes=max(page.original_bytes, int(totals["statement_bytes"])),
            omitted_count=max(0, total_count - len(page.items)),
        )

    async def event_page(
        self,
        thread_id: str,
        *,
        after_position: int = 0,
        limit: int = 100,
    ) -> AttentionThreadEventPage:
        identity = str(thread_id or "").strip()
        if not identity:
            raise ValueError("thread_id must not be empty")
        if after_position < 0 or not 1 <= int(limit) <= 1000:
            raise ValueError("attention event page bounds are invalid")
        async with self.runtime.unit_of_work() as uow:
            source_frontier = int(
                await uow.session.scalar(
                    text("SELECT COALESCE(MAX(position), 0) FROM attention_thread_events")
                )
                or 0
            )
            rows = (
                (
                    await uow.session.execute(
                        text(
                            f"""SELECT {self._event_columns()}
                            FROM attention_thread_events
                            WHERE thread_id = :thread_id
                              AND position > :after_position
                            ORDER BY position LIMIT :limit"""
                        ),
                        {
                            "thread_id": identity,
                            "after_position": int(after_position),
                            "limit": int(limit) + 1,
                        },
                    )
                )
                .mappings()
                .all()
            )
        events = tuple(self._decode_event(row) for row in rows[: int(limit)])
        return AttentionThreadEventPage(
            items=events,
            source_frontier=source_frontier,
            next_position=events[-1].position if events else int(after_position),
            has_more=len(rows) > int(limit),
        )

    async def read_statement_chunk(
        self,
        event_id: str,
        *,
        offset_bytes: int = 0,
        max_bytes: int = 16 * 1024,
    ) -> AttentionThreadValueChunk:
        identity = str(event_id or "").strip()
        if not identity:
            raise ValueError("event_id must not be empty")
        if offset_bytes < 0 or not 1 <= int(max_bytes) <= _MAX_CHUNK_BYTES:
            raise ValueError("attention statement chunk bounds are invalid")
        async with self.runtime.unit_of_work() as uow:
            row = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT event_id, public_statement, event_sha256
                            FROM attention_thread_events
                            WHERE event_id = :event_id"""
                        ),
                        {"event_id": identity},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise KeyError(identity)
        content = str(row["public_statement"])
        encoded = content.encode("utf-8")
        if offset_bytes > len(encoded):
            raise ValueError("attention statement offset exceeds value length")
        try:
            encoded[:offset_bytes].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("attention statement offset splits UTF-8") from exc
        end = min(len(encoded), offset_bytes + int(max_bytes))
        while end > offset_bytes:
            try:
                chunk = encoded[offset_bytes:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            chunk = ""
        return AttentionThreadValueChunk(
            event_id=identity,
            offset_bytes=offset_bytes,
            next_offset_bytes=end,
            total_bytes=len(encoded),
            statement_sha256=hashlib.sha256(encoded).hexdigest(),
            content=chunk,
            complete=end == len(encoded),
        )

    async def set_focus(self, focus: InstanceFocus) -> InstanceFocus:
        """CAS-set instance focus without changing a subject thread."""

        async def operation(session: AsyncSession) -> InstanceFocus:
            database_now = await self._database_now(session)
            await self._assert_active_actor(
                session,
                focus.instance_id,
                database_now=database_now,
            )
            row = (
                (
                    await session.execute(
                        text(
                            """SELECT instance_id, focus_occurrence_id,
                            source_occurrence_id, entered_at, expires_at,
                            revision, thread_id FROM attention_instance_focus
                            WHERE instance_id = :instance_id"""
                            + self._for_update
                        ),
                        {"instance_id": focus.instance_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            current = self._decode_focus(row) if row is not None else None
            if current == focus:
                return current
            expected_revision = 1 if current is None else current.revision + 1
            if focus.revision != expected_revision:
                raise AttentionThreadConflict(focus.instance_id)
            parameters = {
                "instance_id": focus.instance_id,
                "focus_occurrence_id": focus.focus_occurrence_id,
                "source_occurrence_id": focus.source_occurrence_id,
                "entered_at": self._bind_time(focus.entered_at),
                "expires_at": self._bind_time(focus.expires_at),
                "revision": focus.revision,
                "thread_id": focus.thread_id,
                "updated_at": self._bind_time(database_now),
            }
            if current is None:
                await session.execute(
                    text(
                        """INSERT INTO attention_instance_focus (
                            instance_id, focus_occurrence_id,
                            source_occurrence_id, entered_at, expires_at,
                            revision, thread_id, updated_at
                        ) VALUES (
                            :instance_id, :focus_occurrence_id,
                            :source_occurrence_id, :entered_at, :expires_at,
                            :revision, :thread_id, :updated_at
                        )"""
                    ),
                    parameters,
                )
            else:
                parameters["expected_revision"] = current.revision
                result = await session.execute(
                    text(
                        """UPDATE attention_instance_focus SET
                            focus_occurrence_id = :focus_occurrence_id,
                            source_occurrence_id = :source_occurrence_id,
                            entered_at = :entered_at, expires_at = :expires_at,
                            revision = :revision, thread_id = :thread_id,
                            updated_at = :updated_at
                        WHERE instance_id = :instance_id
                          AND revision = :expected_revision"""
                    ),
                    parameters,
                )
                if result.rowcount != 1:
                    raise AttentionThreadConflict(focus.instance_id)
            return focus

        return await self._write(operation)

    async def get_focus(self, instance_id: str) -> InstanceFocus | None:
        identity = str(instance_id or "").strip()
        if not identity:
            raise ValueError("instance_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            database_now = await self._database_now(uow.session)
            row = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT instance_id, focus_occurrence_id,
                            source_occurrence_id, entered_at, expires_at,
                            revision, thread_id FROM attention_instance_focus
                            WHERE instance_id = :instance_id"""
                        ),
                        {"instance_id": identity},
                    )
                )
                .mappings()
                .one_or_none()
            )
        focus = self._decode_focus(row) if row is not None else None
        if focus is not None and datetime.fromisoformat(focus.expires_at) <= database_now:
            return None
        return focus

    async def clear_focus(
        self,
        instance_id: str,
        *,
        expected_revision: int,
    ) -> None:
        identity = str(instance_id or "").strip()
        if not identity or expected_revision <= 0:
            raise ValueError("focus clear identity/revision is invalid")

        async def operation(session: AsyncSession) -> None:
            result = await session.execute(
                text(
                    """DELETE FROM attention_instance_focus
                    WHERE instance_id = :instance_id
                      AND revision = :expected_revision"""
                ),
                {
                    "instance_id": identity,
                    "expected_revision": int(expected_revision),
                },
            )
            if result.rowcount != 1:
                raise AttentionThreadConflict(identity)

        await self._write(operation)

    async def health_snapshot(self) -> dict[str, Any]:
        """Return content-free authority, projection, and focus diagnostics."""

        async with self.runtime.unit_of_work() as uow:
            row = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT
                                (SELECT COUNT(*) FROM attention_thread_events)
                                    AS event_count,
                                (SELECT COALESCE(MAX(position), 0)
                                    FROM attention_thread_events) AS frontier,
                                (SELECT COUNT(*) FROM attention_thread_heads
                                    WHERE status = 'open') AS open_count,
                                (SELECT COUNT(*) FROM attention_thread_heads
                                    WHERE status = 'paused') AS paused_count,
                                (SELECT COUNT(*) FROM attention_thread_heads
                                    WHERE status = 'closed') AS closed_count,
                                (SELECT COUNT(*) FROM attention_instance_focus)
                                    AS focus_count"""
                        )
                    )
                )
                .mappings()
                .one()
            )
        return {
            "status": "healthy",
            "event_count": int(row["event_count"]),
            "source_frontier": int(row["frontier"]),
            "threads": {
                "open": int(row["open_count"]),
                "paused": int(row["paused_count"]),
                "closed": int(row["closed_count"]),
            },
            "instance_focus_count": int(row["focus_count"]),
            "schema_version": 1,
        }


LocalAttentionThreadStore = SQLAttentionThreadStore
MySQLAttentionThreadStore = SQLAttentionThreadStore

__all__ = [
    "LocalAttentionThreadStore",
    "MySQLAttentionThreadStore",
    "SQLAttentionThreadStore",
]
