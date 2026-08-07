"""Fenced SQL adapters for selected technical runtime state and events."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage import canonical_json

from .contracts import StorageBackendRuntime
from .models import BackendKind
from .runtime_contracts import (
    RuntimeEventConflict,
    RuntimeEventRecord,
    RuntimeStateConflict,
    RuntimeStateCorrupt,
    RuntimeStateRecord,
)
from .writer_claims import SingletonWriterClaim

_MAX_NAMESPACE_CHARS = 128
_MAX_STATE_KEY_CHARS = 255
_MAX_EVENT_KIND_CHARS = 128
_MAX_OCCURRENCE_CHARS = 255
_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024


def _identity(value: Any, *, field: str, maximum: int) -> str:
    identity = str(value or "").strip()
    if not identity or len(identity) > maximum:
        raise ValueError(f"{field} must be 1..{maximum} characters")
    return identity


def _payload_json(payload: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise TypeError("runtime payload must be an object")
    encoded = canonical_json(payload)
    if len(encoded.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ValueError("runtime payload exceeds explicit storage limit")
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decode_payload(value: Any, expected_digest: Any, *, identity: str) -> dict[str, Any]:
    raw = value.decode("utf-8") if isinstance(value, bytes) else str(value)
    actual = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    expected = str(expected_digest or "")
    if actual != expected:
        raise RuntimeStateCorrupt(f"RuntimePayloadCorrupt:{identity}:{expected}")
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise RuntimeStateCorrupt(f"RuntimePayloadNotObject:{identity}")
    return decoded


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text_value = str(value or "").strip()
        if text_value.endswith("Z"):
            text_value = text_value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text_value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: Any) -> str:
    return _parse_datetime(value).isoformat()


class SQLRuntimeStateStore:
    """One runtime state/event adapter bound to a coherent storage runtime."""

    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if not runtime.enabled or runtime.engine is None:
            raise RuntimeError("runtime state adapter requires enabled storage")
        self.runtime = runtime
        self.backend = runtime.backend

    @property
    def _for_update(self) -> str:
        return " FOR UPDATE" if self.backend == BackendKind.MYSQL else ""

    def _bind_time(self, value: Any) -> datetime | str:
        parsed = _parse_datetime(value)
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
        return _parse_datetime(value)

    @staticmethod
    def _state_from_row(row: Any) -> RuntimeStateRecord:
        namespace = str(row["namespace"])
        state_key = str(row["state_key"])
        return RuntimeStateRecord(
            namespace=namespace,
            state_key=state_key,
            revision=int(row["revision"]),
            schema_version=int(row["schema_version"]),
            payload=_decode_payload(
                row["payload_json"],
                row["payload_sha256"],
                identity=f"{namespace}:{state_key}",
            ),
            payload_sha256=str(row["payload_sha256"]),
            updated_at=_iso(row["updated_at"]),
        )

    @staticmethod
    def _event_from_row(row: Any) -> RuntimeEventRecord:
        namespace = str(row["namespace"])
        occurrence_id = str(row["occurrence_id"])
        return RuntimeEventRecord(
            position=int(row["position"]),
            namespace=namespace,
            occurrence_id=occurrence_id,
            event_kind=str(row["event_kind"]),
            payload=_decode_payload(
                row["payload_json"],
                row["payload_sha256"],
                identity=f"{namespace}:{occurrence_id}",
            ),
            payload_sha256=str(row["payload_sha256"]),
            occurred_at=_iso(row["occurred_at"]),
            recorded_at=_iso(row["recorded_at"]),
        )

    async def get_state(
        self,
        namespace: str,
        state_key: str,
    ) -> RuntimeStateRecord | None:
        namespace = _identity(
            namespace,
            field="namespace",
            maximum=_MAX_NAMESPACE_CHARS,
        )
        state_key = _identity(
            state_key,
            field="state_key",
            maximum=_MAX_STATE_KEY_CHARS,
        )
        async with self.runtime.unit_of_work() as uow:
            row = (
                await uow.session.execute(
                    text(
                        """SELECT namespace, state_key, revision, schema_version,
                            payload_json, payload_sha256, updated_at
                        FROM runtime_states
                        WHERE namespace = :namespace AND state_key = :state_key"""
                    ),
                    {"namespace": namespace, "state_key": state_key},
                )
            ).mappings().first()
        return self._state_from_row(row) if row is not None else None

    async def put_state(
        self,
        *,
        namespace: str,
        state_key: str,
        expected_revision: int,
        schema_version: int,
        payload: dict[str, Any],
        writer_claim: SingletonWriterClaim | None = None,
    ) -> RuntimeStateRecord:
        namespace = _identity(
            namespace,
            field="namespace",
            maximum=_MAX_NAMESPACE_CHARS,
        )
        state_key = _identity(
            state_key,
            field="state_key",
            maximum=_MAX_STATE_KEY_CHARS,
        )
        expected_revision = int(expected_revision)
        schema_version = int(schema_version)
        if expected_revision < 0:
            raise ValueError("expected_revision must not be negative")
        if schema_version <= 0:
            raise ValueError("schema_version must be positive")
        payload_json, payload_sha256 = _payload_json(payload)

        claim_store = self.runtime._singleton_writer_claims
        if claim_store is None:
            raise RuntimeError("RuntimeSingletonWriterClaimStoreNotAttached")
        async with self.runtime.unit_of_work(writer_claim=writer_claim) as uow:
            session = uow.session
            if writer_claim is not None and (
                writer_claim.namespace != namespace
                or writer_claim.state_key != state_key
            ):
                raise RuntimeStateConflict(
                    "RuntimeStateWriterClaimScopeMismatch:"
                    f"{namespace}:{state_key}"
                )
            await claim_store.prepare_runtime_state_write(
                session,
                namespace=namespace,
                state_key=state_key,
                claim=writer_claim,
            )
            try:
                row = (
                    await session.execute(
                        text(
                            """SELECT namespace, state_key, revision, schema_version,
                                payload_json, payload_sha256, updated_at
                            FROM runtime_states
                            WHERE namespace = :namespace AND state_key = :state_key"""
                            + self._for_update
                        ),
                        {"namespace": namespace, "state_key": state_key},
                    )
                ).mappings().first()
                current_revision = int(row["revision"]) if row is not None else 0
                if current_revision != expected_revision:
                    raise RuntimeStateConflict(
                        f"RuntimeStateRevisionConflict:{namespace}:{state_key}:"
                        f"expected={expected_revision}:actual={current_revision}"
                    )
                now = await self._database_now(session)
                revision = current_revision + 1
                parameters = {
                    "namespace": namespace,
                    "state_key": state_key,
                    "revision": revision,
                    "schema_version": schema_version,
                    "payload_json": payload_json,
                    "payload_sha256": payload_sha256,
                    "updated_at": self._bind_time(now),
                }
                if row is None:
                    await session.execute(
                        text(
                            """INSERT INTO runtime_states (
                                namespace, state_key, revision, schema_version,
                                payload_json, payload_sha256, updated_at
                            ) VALUES (
                                :namespace, :state_key, :revision, :schema_version,
                                :payload_json, :payload_sha256, :updated_at
                            )"""
                        ),
                        parameters,
                    )
                else:
                    result = await session.execute(
                        text(
                            """UPDATE runtime_states SET
                                revision = :revision,
                                schema_version = :schema_version,
                                payload_json = :payload_json,
                                payload_sha256 = :payload_sha256,
                                updated_at = :updated_at
                            WHERE namespace = :namespace AND state_key = :state_key
                                AND revision = :expected_revision"""
                        ),
                        {**parameters, "expected_revision": expected_revision},
                    )
                    if result.rowcount != 1:
                        raise RuntimeStateConflict(
                            f"RuntimeStateRevisionConflict:{namespace}:{state_key}"
                        )
            finally:
                await self.runtime.clear_singleton_writer_write(session)
        return RuntimeStateRecord(
            namespace=namespace,
            state_key=state_key,
            revision=revision,
            schema_version=schema_version,
            payload=dict(payload),
            payload_sha256=payload_sha256,
            updated_at=now.isoformat(),
        )

    async def append_event(
        self,
        *,
        namespace: str,
        occurrence_id: str,
        event_kind: str,
        payload: dict[str, Any],
        occurred_at: str,
    ) -> RuntimeEventRecord:
        namespace = _identity(
            namespace,
            field="namespace",
            maximum=_MAX_NAMESPACE_CHARS,
        )
        occurrence_id = _identity(
            occurrence_id,
            field="occurrence_id",
            maximum=_MAX_OCCURRENCE_CHARS,
        )
        event_kind = _identity(
            event_kind,
            field="event_kind",
            maximum=_MAX_EVENT_KIND_CHARS,
        )
        occurred = _parse_datetime(occurred_at)
        payload_json, payload_sha256 = _payload_json(payload)

        try:
            async with self.runtime.unit_of_work() as uow:
                session = uow.session
                recorded = await self._database_now(session)
                await session.execute(
                    text(
                        """INSERT INTO runtime_events (
                            namespace, occurrence_id, event_kind, payload_json,
                            payload_sha256, occurred_at, recorded_at
                        ) VALUES (
                            :namespace, :occurrence_id, :event_kind, :payload_json,
                            :payload_sha256, :occurred_at, :recorded_at
                        )"""
                    ),
                    {
                        "namespace": namespace,
                        "occurrence_id": occurrence_id,
                        "event_kind": event_kind,
                        "payload_json": payload_json,
                        "payload_sha256": payload_sha256,
                        "occurred_at": self._bind_time(occurred),
                        "recorded_at": self._bind_time(recorded),
                    },
                )
                row = (
                    await session.execute(
                        text(
                            """SELECT position, namespace, occurrence_id,
                                event_kind, payload_json, payload_sha256,
                                occurred_at, recorded_at
                            FROM runtime_events
                            WHERE occurrence_id = :occurrence_id"""
                        ),
                        {"occurrence_id": occurrence_id},
                    )
                ).mappings().one()
        except IntegrityError:
            async with self.runtime.unit_of_work() as uow:
                row = (
                    await uow.session.execute(
                        text(
                            """SELECT position, namespace, occurrence_id,
                                event_kind, payload_json, payload_sha256,
                                occurred_at, recorded_at
                            FROM runtime_events
                            WHERE occurrence_id = :occurrence_id"""
                        ),
                        {"occurrence_id": occurrence_id},
                    )
                ).mappings().first()
            if row is None:
                raise
            existing = self._event_from_row(row)
            if (
                existing.namespace != namespace
                or existing.event_kind != event_kind
                or existing.payload_sha256 != payload_sha256
                or existing.occurred_at != occurred.isoformat()
            ):
                raise RuntimeEventConflict(
                    f"RuntimeEventOccurrenceConflict:{occurrence_id}"
                )
            return existing
        return self._event_from_row(row)

    async def read_events(
        self,
        namespace: str,
        *,
        after_position: int = 0,
        limit: int = 100,
    ) -> list[RuntimeEventRecord]:
        namespace = _identity(
            namespace,
            field="namespace",
            maximum=_MAX_NAMESPACE_CHARS,
        )
        after_position = max(0, int(after_position))
        limit = max(1, min(int(limit), 1000))
        async with self.runtime.unit_of_work() as uow:
            rows = (
                await uow.session.execute(
                    text(
                        """SELECT position, namespace, occurrence_id,
                            event_kind, payload_json, payload_sha256,
                            occurred_at, recorded_at
                        FROM runtime_events
                        WHERE namespace = :namespace AND position > :after_position
                        ORDER BY position LIMIT :limit"""
                    ),
                    {
                        "namespace": namespace,
                        "after_position": after_position,
                        "limit": limit,
                    },
                )
            ).mappings().all()
        return [self._event_from_row(row) for row in rows]

    async def health_snapshot(self) -> dict[str, Any]:
        async with self.runtime.unit_of_work() as uow:
            state_count = int(
                await uow.session.scalar(text("SELECT COUNT(*) FROM runtime_states"))
                or 0
            )
            event_count = int(
                await uow.session.scalar(text("SELECT COUNT(*) FROM runtime_events"))
                or 0
            )
            last_position = int(
                await uow.session.scalar(
                    text("SELECT COALESCE(MAX(position), 0) FROM runtime_events")
                )
                or 0
            )
        return {
            "status": "healthy",
            "backend": self.backend.value,
            "state_count": state_count,
            "event_count": event_count,
            "last_event_position": last_position,
        }


class LocalRuntimeStateStore(SQLRuntimeStateStore):
    """SQLite implementation for explicit local mode and contract tests."""


class MySQLRuntimeStateStore(SQLRuntimeStateStore):
    """MySQL implementation for the selected remote authority."""


__all__ = [
    "LocalRuntimeStateStore",
    "MySQLRuntimeStateStore",
    "SQLRuntimeStateStore",
]
