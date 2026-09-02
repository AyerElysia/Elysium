"""Fenced SQL adapters for operational consciousness Presence."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage import canonical_json

from ..service.presence_store import (
    PresenceRevisionConflict,
    StreamOwnershipConflict,
)
from .contracts import StorageBackendRuntime
from ._write_base import run_write_attempts
from .domain_contracts import (
    PresenceCommitResult,
    PresenceLeaseConflict,
    PresenceTakeoverResult,
)
from .models import BackendKind

_T = TypeVar("_T")


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
        raise ValueError(f"invalid persisted Presence timestamp: {value!r}")
    return parsed.isoformat()


def _json_value(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str):
        return json.loads(value)
    return value


class SQLPresenceStore:
    """One dialect-aware implementation bound to a coherent storage runtime."""

    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if not runtime.enabled or runtime.engine is None:
            raise RuntimeError("Presence adapter requires an enabled storage runtime")
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

    async def _write(
        self,
        operation: Callable[[AsyncSession], Awaitable[_T]],
    ) -> _T:
        async def _attempt() -> _T:
            async with self.runtime.unit_of_work() as uow:
                return await operation(uow.session)

        return await run_write_attempts(
            _attempt,
            exhaustion_message="bounded Presence retry loop exhausted unexpectedly",
        )

    @staticmethod
    def _normalize_instance(instance: dict[str, Any]) -> dict[str, Any]:
        snapshot = dict(instance)
        identity = str(snapshot.get("instance_id") or "").strip()
        if not identity:
            raise ValueError("consciousness instance_id must not be empty")
        status = str(snapshot.get("status") or "").strip()
        if not status:
            raise ValueError("consciousness presence status must not be empty")
        streams = list(
            dict.fromkeys(
                str(value).strip()
                for value in (snapshot.get("stream_ids") or [])
                if str(value).strip()
            )
        )
        lease_duration = snapshot.get("lease_duration_seconds")
        if lease_duration is not None and int(lease_duration) <= 0:
            raise ValueError("lease_duration_seconds must be positive")
        snapshot.update(
            {
                "instance_id": identity,
                "kind": str(snapshot.get("kind") or ""),
                "display_name": str(snapshot.get("display_name") or ""),
                "status": status,
                "created_at": _iso(snapshot.get("created_at")),
                "last_active_at": _iso(snapshot.get("last_active_at")),
                "suspended_at": _iso(snapshot.get("suspended_at")),
                "stream_ids": streams,
                "perception_filter": dict(snapshot.get("perception_filter") or {}),
                "metadata": dict(snapshot.get("metadata") or {}),
                "session_id": str(snapshot.get("session_id") or ""),
                "process_epoch": str(snapshot.get("process_epoch") or ""),
                "lease_expires_at": _iso(snapshot.get("lease_expires_at")),
                "lease_duration_seconds": (
                    int(lease_duration) if lease_duration is not None else None
                ),
            }
        )
        return snapshot

    @staticmethod
    def _decode_row(row: Any) -> dict[str, Any]:
        return {
            "instance_id": str(row["instance_id"]),
            "kind": str(row["kind"]),
            "display_name": str(row["display_name"] or ""),
            "status": str(row["status"]),
            "created_at": _iso(row["created_at"]),
            "last_active_at": _iso(row["last_active_at"]),
            "suspended_at": _iso(row["suspended_at"]),
            "stream_ids": list(_json_value(row["stream_ids_json"], default=[])),
            "perception_filter": dict(
                _json_value(row["perception_filter_json"], default={})
            ),
            "metadata": dict(_json_value(row["metadata_json"], default={})),
            "session_id": str(row["session_id"] or ""),
            "process_epoch": str(row["process_epoch"] or ""),
            "lease_expires_at": _iso(row["lease_expires_at"]),
            "lease_duration_seconds": (
                int(row["lease_duration_seconds"])
                if row["lease_duration_seconds"] is not None
                else None
            ),
            "revision": int(row["revision"]),
        }

    async def list_instances(self) -> list[dict[str, Any]]:
        async with self.runtime.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text("SELECT * FROM consciousness_presence ORDER BY instance_id")
                )
            ).mappings()
            return [self._decode_row(row) for row in rows]

    async def _locked_instance(
        self,
        session: AsyncSession,
        instance_id: str,
    ) -> Any:
        return (
            (
                await session.execute(
                    text(
                        "SELECT * FROM consciousness_presence WHERE instance_id = "
                        f":instance_id{self._for_update}"
                    ),
                    {"instance_id": instance_id},
                )
            )
            .mappings()
            .one_or_none()
        )

    async def _locked_owner(
        self,
        session: AsyncSession,
        stream_id: str,
    ) -> Any:
        return (
            (
                await session.execute(
                    text(
                        "SELECT stream_id, instance_id FROM consciousness_stream_owners "
                        f"WHERE stream_id = :stream_id{self._for_update}"
                    ),
                    {"stream_id": stream_id},
                )
            )
            .mappings()
            .one_or_none()
        )

    async def _persist_snapshot(
        self,
        session: AsyncSession,
        instance: dict[str, Any],
        *,
        expected_revision: int | None,
        event_type: str,
        event_payload: dict[str, Any] | None,
        database_now: datetime,
        refresh_lease: bool,
    ) -> PresenceCommitResult:
        snapshot = self._normalize_instance(instance)
        identity = snapshot["instance_id"]
        existing = await self._locked_instance(session, identity)
        if expected_revision is None:
            if existing is not None and str(existing["status"]) != "terminated":
                raise PresenceRevisionConflict(
                    f"consciousness instance '{identity}' already exists with "
                    f"status {existing['status']}"
                )
            previous_revision = int(existing["revision"]) if existing else 0
        else:
            if existing is None or int(existing["revision"]) != int(expected_revision):
                actual = int(existing["revision"]) if existing else None
                raise PresenceRevisionConflict(
                    f"presence revision conflict for '{identity}': "
                    f"expected {expected_revision}, actual {actual}"
                )
            previous_revision = int(existing["revision"])
        if refresh_lease:
            duration = snapshot["lease_duration_seconds"]
            if snapshot["status"] != "active" or duration is None:
                raise PresenceLeaseConflict(
                    "lease refresh requires an active instance and lease duration"
                )
            snapshot["lease_expires_at"] = (
                database_now + timedelta(seconds=duration)
            ).isoformat()
        elif snapshot["status"] == "active" and (
            snapshot["lease_duration_seconds"] is not None
            or snapshot["lease_expires_at"]
        ):
            raise PresenceLeaseConflict(
                "active lease timestamps must be generated from database time"
            )
        elif snapshot["status"] != "active":
            snapshot["lease_expires_at"] = ""

        streams = list(snapshot["stream_ids"])
        if snapshot["status"] == "active":
            for stream_id in sorted(streams):
                owner = await self._locked_owner(session, stream_id)
                if owner is not None and str(owner["instance_id"]) != identity:
                    raise StreamOwnershipConflict(
                        stream_id,
                        str(owner["instance_id"]),
                        identity,
                    )

        revision = previous_revision + 1
        snapshot["revision"] = revision
        params = {
            "instance_id": identity,
            "kind": snapshot["kind"],
            "display_name": snapshot["display_name"],
            "status": snapshot["status"],
            "created_at": self._bind_time(snapshot["created_at"]),
            "last_active_at": self._bind_time(snapshot["last_active_at"]),
            "suspended_at": self._bind_time(snapshot["suspended_at"]),
            "stream_ids_json": canonical_json(streams),
            "perception_filter_json": canonical_json(snapshot["perception_filter"]),
            "metadata_json": canonical_json(snapshot["metadata"]),
            "session_id": snapshot["session_id"],
            "process_epoch": snapshot["process_epoch"],
            "lease_expires_at": self._bind_time(snapshot["lease_expires_at"]),
            "lease_duration_seconds": snapshot["lease_duration_seconds"],
            "revision": revision,
            "updated_at": self._bind_time(database_now),
        }
        if existing is None:
            try:
                await session.execute(
                    text(
                        """INSERT INTO consciousness_presence (
                            instance_id, kind, display_name, status, created_at,
                            last_active_at, suspended_at, stream_ids_json,
                            perception_filter_json, metadata_json, session_id,
                            process_epoch, lease_expires_at, lease_duration_seconds,
                            revision, updated_at
                        ) VALUES (
                            :instance_id, :kind, :display_name, :status, :created_at,
                            :last_active_at, :suspended_at, :stream_ids_json,
                            :perception_filter_json, :metadata_json, :session_id,
                            :process_epoch, :lease_expires_at, :lease_duration_seconds,
                            :revision, :updated_at
                        )"""
                    ),
                    params,
                )
            except IntegrityError as exc:
                raise PresenceRevisionConflict(
                    f"concurrent presence insert for '{identity}'"
                ) from exc
        else:
            updated = await session.execute(
                text(
                    """UPDATE consciousness_presence SET
                        kind = :kind, display_name = :display_name, status = :status,
                        created_at = :created_at, last_active_at = :last_active_at,
                        suspended_at = :suspended_at,
                        stream_ids_json = :stream_ids_json,
                        perception_filter_json = :perception_filter_json,
                        metadata_json = :metadata_json, session_id = :session_id,
                        process_epoch = :process_epoch,
                        lease_expires_at = :lease_expires_at,
                        lease_duration_seconds = :lease_duration_seconds,
                        revision = :revision, updated_at = :updated_at
                    WHERE instance_id = :instance_id
                      AND revision = :previous_revision"""
                ),
                {**params, "previous_revision": previous_revision},
            )
            if updated.rowcount != 1:
                raise PresenceRevisionConflict(
                    f"concurrent presence update for '{identity}'"
                )

        await session.execute(
            text(
                "DELETE FROM consciousness_stream_owners "
                "WHERE instance_id = :instance_id"
            ),
            {"instance_id": identity},
        )
        if snapshot["status"] == "active":
            for stream_id in streams:
                try:
                    await session.execute(
                        text(
                            """INSERT INTO consciousness_stream_owners (
                                stream_id, instance_id, claimed_at
                            ) VALUES (:stream_id, :instance_id, :claimed_at)"""
                        ),
                        {
                            "stream_id": stream_id,
                            "instance_id": identity,
                            "claimed_at": self._bind_time(database_now),
                        },
                    )
                except IntegrityError as exc:
                    raise StreamOwnershipConflict(
                        stream_id,
                        "<concurrent>",
                        identity,
                    ) from exc

        if event_type:
            payload = dict(event_payload or {})
            payload.update(
                {
                    "instance": dict(snapshot),
                    "previous_revision": previous_revision,
                    "revision": revision,
                }
            )
            occurred_at = (
                _parse_datetime(payload.get("occurred_at"))
                or _parse_datetime(snapshot["last_active_at"])
                or database_now
            )
            await session.execute(
                text(
                    """INSERT INTO consciousness_presence_outbox (
                        occurrence_id, event_type, instance_id, stream_id,
                        occurred_at, payload_json
                    ) VALUES (
                        :occurrence_id, :event_type, :instance_id, :stream_id,
                        :occurred_at, :payload_json
                    )"""
                ),
                {
                    "occurrence_id": "presence_" + uuid4().hex,
                    "event_type": event_type,
                    "instance_id": identity,
                    "stream_id": streams[0] if streams else "",
                    "occurred_at": self._bind_time(occurred_at),
                    "payload_json": canonical_json(payload),
                },
            )
        return PresenceCommitResult(
            instance=snapshot,
            previous_revision=previous_revision,
            revision=revision,
            database_now=database_now.isoformat(),
        )

    async def commit(
        self,
        instance: dict[str, Any],
        *,
        expected_revision: int | None,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
        refresh_lease: bool = False,
    ) -> PresenceCommitResult:
        async def operation(session: AsyncSession) -> PresenceCommitResult:
            database_now = await self._database_now(session)
            return await self._persist_snapshot(
                session,
                instance,
                expected_revision=expected_revision,
                event_type=event_type,
                event_payload=event_payload,
                database_now=database_now,
                refresh_lease=refresh_lease,
            )

        return await self._write(operation)

    async def renew_lease(
        self,
        instance_id: str,
        *,
        expected_revision: int,
        process_epoch: str,
        lease_seconds: int,
        event_payload: dict[str, Any] | None = None,
    ) -> PresenceCommitResult:
        identity = str(instance_id or "").strip()
        epoch = str(process_epoch or "").strip()
        if not identity or not epoch:
            raise ValueError("lease renewal requires instance_id and process_epoch")
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")

        async def operation(session: AsyncSession) -> PresenceCommitResult:
            row = await self._locked_instance(session, identity)
            if row is None or int(row["revision"]) != int(expected_revision):
                actual = int(row["revision"]) if row else None
                raise PresenceRevisionConflict(
                    f"presence revision conflict for '{identity}': "
                    f"expected {expected_revision}, actual {actual}"
                )
            snapshot = self._decode_row(row)
            if snapshot["status"] != "active":
                raise PresenceLeaseConflict("only active Presence can renew a lease")
            if snapshot["process_epoch"] != epoch:
                raise PresenceLeaseConflict(
                    f"process epoch does not own Presence '{identity}'"
                )
            database_now = await self._database_now(session)
            snapshot["last_active_at"] = database_now.isoformat()
            snapshot["lease_duration_seconds"] = int(lease_seconds)
            payload = dict(event_payload or {})
            payload.setdefault("occurred_at", database_now.isoformat())
            payload.setdefault("reason", "lease_renewed")
            return await self._persist_snapshot(
                session,
                snapshot,
                expected_revision=expected_revision,
                event_type="consciousness.instance_seen",
                event_payload=payload,
                database_now=database_now,
                refresh_lease=True,
            )

        return await self._write(operation)

    async def takeover_expired(
        self,
        instance: dict[str, Any],
        *,
        expected_revision: int | None,
        process_epoch: str,
        lease_seconds: int,
        event_payload: dict[str, Any] | None = None,
    ) -> PresenceTakeoverResult:
        epoch = str(process_epoch or "").strip()
        if not epoch:
            raise ValueError("Presence takeover process_epoch must not be empty")
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        claimant = self._normalize_instance(instance)
        claimant["status"] = "active"
        claimant["process_epoch"] = epoch
        claimant["lease_duration_seconds"] = int(lease_seconds)

        async def operation(session: AsyncSession) -> PresenceTakeoverResult:
            database_now = await self._database_now(session)
            owner_ids: set[str] = set()
            for stream_id in sorted(claimant["stream_ids"]):
                owner = await self._locked_owner(session, stream_id)
                if (
                    owner is not None
                    and str(owner["instance_id"]) != claimant["instance_id"]
                ):
                    owner_ids.add(str(owner["instance_id"]))
            owner_rows: dict[str, Any] = {}
            for owner_id in sorted(owner_ids):
                owner_row = await self._locked_instance(session, owner_id)
                if owner_row is None:
                    raise PresenceLeaseConflict(
                        f"stream owner '{owner_id}' has no Presence snapshot"
                    )
                owner_rows[owner_id] = owner_row

            displaced: list[PresenceCommitResult] = []
            for owner_id in sorted(owner_rows):
                owner_snapshot = self._decode_row(owner_rows[owner_id])
                expiry = _parse_datetime(owner_snapshot["lease_expires_at"])
                if owner_snapshot["status"] != "active" or expiry is None:
                    raise PresenceLeaseConflict(
                        f"stream owner '{owner_id}' is not an expirable active lease"
                    )
                if expiry >= database_now:
                    stream = next(
                        value
                        for value in claimant["stream_ids"]
                        if value in owner_snapshot["stream_ids"]
                    )
                    raise StreamOwnershipConflict(
                        stream,
                        owner_id,
                        claimant["instance_id"],
                    )
                owner_snapshot["status"] = "suspended"
                owner_snapshot["suspended_at"] = database_now.isoformat()
                owner_snapshot["lease_expires_at"] = ""
                displaced.append(
                    await self._persist_snapshot(
                        session,
                        owner_snapshot,
                        expected_revision=owner_snapshot["revision"],
                        event_type="consciousness.instance_lease_expired",
                        event_payload={
                            "occurred_at": database_now.isoformat(),
                            "reason": "lease_expired_takeover",
                            "claimant_instance_id": claimant["instance_id"],
                        },
                        database_now=database_now,
                        refresh_lease=False,
                    )
                )

            claimant["last_active_at"] = database_now.isoformat()
            payload = dict(event_payload or {})
            payload.setdefault("occurred_at", database_now.isoformat())
            payload["displaced_instance_ids"] = sorted(owner_ids)
            committed = await self._persist_snapshot(
                session,
                claimant,
                expected_revision=expected_revision,
                event_type="consciousness.instance_taken_over",
                event_payload=payload,
                database_now=database_now,
                refresh_lease=True,
            )
            return PresenceTakeoverResult(
                claimant=committed,
                displaced=tuple(displaced),
            )

        return await self._write(operation)

    async def expire_leases(
        self,
        *,
        limit: int = 200,
    ) -> tuple[PresenceCommitResult, ...]:
        """Suspend expired active leases using one backend-time transaction."""

        bounded_limit = max(1, int(limit))

        async def operation(
            session: AsyncSession,
        ) -> tuple[PresenceCommitResult, ...]:
            database_now = await self._database_now(session)
            rows = (
                (
                    await session.execute(
                        text(
                            """SELECT * FROM consciousness_presence
                            WHERE status = 'active'
                              AND lease_duration_seconds IS NOT NULL
                              AND lease_expires_at IS NOT NULL
                            ORDER BY lease_expires_at, instance_id
                            LIMIT :limit"""
                            + self._for_update
                        ),
                        {"limit": bounded_limit},
                    )
                )
                .mappings()
                .all()
            )
            expired: list[PresenceCommitResult] = []
            for row in rows:
                snapshot = self._decode_row(row)
                expiry = _parse_datetime(snapshot["lease_expires_at"])
                if expiry is None or expiry >= database_now:
                    break
                snapshot["status"] = "suspended"
                snapshot["suspended_at"] = database_now.isoformat()
                snapshot["lease_expires_at"] = ""
                expired.append(
                    await self._persist_snapshot(
                        session,
                        snapshot,
                        expected_revision=int(row["revision"]),
                        event_type="consciousness.instance_lease_expired",
                        event_payload={
                            "occurred_at": database_now.isoformat(),
                            "reason": "lease_expired_reconcile",
                        },
                        database_now=database_now,
                        refresh_lease=False,
                    )
                )
            return tuple(expired)

        return await self._write(operation)

    async def pending_events(self, limit: int = 200) -> list[dict[str, Any]]:
        async with self.runtime.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """SELECT * FROM consciousness_presence_outbox
                        WHERE published_at IS NULL
                        ORDER BY outbox_id LIMIT :limit"""
                    ),
                    {"limit": max(1, int(limit))},
                )
            ).mappings()
            return [
                {
                    "outbox_id": int(row["outbox_id"]),
                    "occurrence_id": str(row["occurrence_id"]),
                    "event_type": str(row["event_type"]),
                    "instance_id": str(row["instance_id"]),
                    "stream_id": str(row["stream_id"] or ""),
                    "occurred_at": _iso(row["occurred_at"]),
                    "payload": dict(_json_value(row["payload_json"], default={})),
                }
                for row in rows
            ]

    async def acknowledge_events(self, outbox_ids: list[int]) -> None:
        identities = sorted({int(value) for value in outbox_ids})
        if not identities:
            return

        async def operation(session: AsyncSession) -> None:
            database_now = await self._database_now(session)
            for outbox_id in identities:
                await session.execute(
                    text(
                        """UPDATE consciousness_presence_outbox
                        SET published_at = :published_at
                        WHERE outbox_id = :outbox_id AND published_at IS NULL"""
                    ),
                    {
                        "outbox_id": outbox_id,
                        "published_at": self._bind_time(database_now),
                    },
                )

        await self._write(operation)

    async def health_snapshot(self) -> dict[str, Any]:
        async with self.runtime.engine.connect() as connection:
            counts = (
                (
                    await connection.execute(
                        text(
                            """SELECT COUNT(*) AS total,
                        SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active
                        FROM consciousness_presence"""
                        )
                    )
                )
                .mappings()
                .one()
            )
            owner_count = await connection.scalar(
                text("SELECT COUNT(*) FROM consciousness_stream_owners")
            )
            pending_count = await connection.scalar(
                text(
                    """SELECT COUNT(*) FROM consciousness_presence_outbox
                    WHERE published_at IS NULL"""
                )
            )
        return {
            "backend": self.backend.value,
            "instance_count": int(counts["total"] or 0),
            "active_count": int(counts["active"] or 0),
            "owned_stream_count": int(owner_count or 0),
            "pending_event_count": int(pending_count or 0),
        }


class LocalPresenceStore(SQLPresenceStore):
    """First-class local adapter backed by the selected SQLite runtime."""

    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if runtime.backend != BackendKind.LOCAL:
            raise ValueError("LocalPresenceStore requires the local backend")
        super().__init__(runtime)


class MySQLPresenceStore(SQLPresenceStore):
    """Multi-process adapter backed by the selected MySQL runtime."""

    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if runtime.backend != BackendKind.MYSQL:
            raise ValueError("MySQLPresenceStore requires the MySQL backend")
        super().__init__(runtime)


__all__ = ["LocalPresenceStore", "MySQLPresenceStore", "SQLPresenceStore"]
