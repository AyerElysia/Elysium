"""Async operational Presence runtime backed by the selected storage Port.

The runtime keeps an in-process read snapshot for routing and rendering, but
every lifecycle mutation becomes visible only after the authoritative Port
commits its revision. It never writes the legacy Presence SQLite database.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from .consciousness import (
    CHAT_GLOBAL_INSTANCE_ID,
    ConsciousnessInstance,
    ConsciousnessRegistry,
)
from .event_bus import LifeEvent, LifeEventChannel, LifeEventPriority
from .presence_store import (
    PresenceRevisionConflict,
    StreamOwnershipConflict,
)
from .world_state import PerceptionFilter


class AsyncPresenceStore(Protocol):
    """Structural subset of the backend-neutral Presence Store Port."""

    async def list_instances(self) -> list[dict[str, Any]]: ...

    async def commit(
        self,
        instance: dict[str, Any],
        *,
        expected_revision: int | None,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
        refresh_lease: bool = False,
    ) -> Any: ...

    async def renew_lease(
        self,
        instance_id: str,
        *,
        expected_revision: int,
        process_epoch: str,
        lease_seconds: int,
        event_payload: dict[str, Any] | None = None,
    ) -> Any: ...

    async def takeover_expired(
        self,
        instance: dict[str, Any],
        *,
        expected_revision: int | None,
        process_epoch: str,
        lease_seconds: int,
        event_payload: dict[str, Any] | None = None,
    ) -> Any: ...

    async def expire_leases(self, *, limit: int = 200) -> Sequence[Any]: ...

    async def pending_events(self, limit: int = 200) -> list[dict[str, Any]]: ...

    async def acknowledge_events(self, outbox_ids: list[int]) -> None: ...

    async def health_snapshot(self) -> dict[str, Any]: ...


class AsyncLifeEventStore(Protocol):
    """Structural append subset of the authoritative Life Event Port."""

    async def append_many(self, events: list[LifeEvent]) -> list[LifeEvent]: ...


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def presence_outbox_event(item: dict[str, Any]) -> LifeEvent:
    """Convert one durable Presence outbox row into immutable Life evidence."""

    payload = dict(item["payload"])
    instance = payload.get("instance")
    session_id = (
        str(instance.get("session_id") or "")
        if isinstance(instance, dict)
        else ""
    )
    return LifeEvent(
        event_id=str(item["occurrence_id"]),
        sequence=0,
        timestamp=str(item["occurred_at"]),
        source="life_engine.presence",
        channel=LifeEventChannel.SYSTEM.value,
        event_type=str(item["event_type"]),
        content=json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        stream_id=str(item["stream_id"]),
        priority=int(LifeEventPriority.NORMAL),
        salience=0.8,
        metadata={
            "source_instance_id": str(item["instance_id"]),
            "presence_revision": int(payload.get("revision") or 0),
            "session_id": session_id,
        },
        occurrence_id=str(item["occurrence_id"]),
        source_instance_id=str(item["instance_id"]),
        correlation_id=session_id,
    )


async def flush_presence_lifecycle_events(
    presence: AsyncPresenceStore,
    ledger: AsyncLifeEventStore,
    *,
    batch_size: int = 200,
    max_events: int = 2000,
) -> int:
    """Publish and acknowledge a bounded Presence outbox prefix.

    Appends are idempotent by occurrence identity. A failed append leaves every
    row pending; a failed acknowledgement is safe to retry because the ledger
    accepts only byte-equivalent replay for the same occurrence.
    """

    batch_size = max(1, int(batch_size))
    max_events = max(batch_size, int(max_events))
    published = 0
    while published < max_events:
        limit = min(batch_size, max_events - published)
        pending = await presence.pending_events(limit=limit)
        if not pending:
            break
        events = [presence_outbox_event(item) for item in pending]
        persisted = await ledger.append_many(events)
        if len(persisted) != len(events):
            raise RuntimeError(
                "PresenceLifecycleAppendIncomplete: ledger returned a partial batch"
            )
        expected_occurrences = [item.occurrence_id for item in events]
        persisted_occurrences = [item.occurrence_id for item in persisted]
        if persisted_occurrences != expected_occurrences:
            raise RuntimeError(
                "PresenceLifecycleAppendMismatch: ledger returned a different "
                "occurrence sequence"
            )
        await presence.acknowledge_events(
            [int(item["outbox_id"]) for item in pending]
        )
        published += len(pending)
        if len(pending) < limit:
            break
    remaining = await presence.pending_events(limit=1)
    if remaining:
        raise RuntimeError(
            "PresenceLifecycleFlushLimit: pending lifecycle evidence exceeded "
            f"the bounded flush window of {max_events} events"
        )
    return published


class AsyncConsciousnessRegistry:
    """Authoritative async lifecycle operations plus synchronous read snapshots."""

    def __init__(
        self,
        store: AsyncPresenceStore,
        *,
        process_epoch: str = "",
    ) -> None:
        self._store = store
        self._process_epoch = process_epoch or ("proc_" + uuid4().hex)
        self._instances: dict[str, ConsciousnessInstance] = {}
        self._lock = asyncio.Lock()

    @classmethod
    async def load(
        cls,
        store: AsyncPresenceStore,
        *,
        process_epoch: str = "",
    ) -> AsyncConsciousnessRegistry:
        """Load one selected backend and establish the global chat invariant."""

        registry = cls(store, process_epoch=process_epoch)
        await registry.refresh()
        await registry.reconcile_expired()
        await registry._ensure_chat_global()
        return registry

    @property
    def process_epoch(self) -> str:
        return self._process_epoch

    @property
    def database_path(self) -> None:
        """Selected storage is never represented as a legacy SQLite path."""

        return None

    async def refresh(self) -> None:
        """Refresh the read snapshot without inventing lifecycle transitions."""

        rows = await self._store.list_instances()
        restored = {
            str(row["instance_id"]): ConsciousnessInstance.from_dict(row)
            for row in rows
        }
        async with self._lock:
            self._instances = restored

    def get(self, instance_id: str) -> ConsciousnessInstance | None:
        return self._instances.get(instance_id)

    def get_active(self) -> list[ConsciousnessInstance]:
        return [item for item in self._instances.values() if item.is_active]

    def get_for_stream(self, stream_id: str) -> ConsciousnessInstance | None:
        identity = str(stream_id or "").strip()
        if not identity:
            return None
        owners = [
            item
            for item in self._instances.values()
            if item.is_active and identity in item.stream_ids
        ]
        if len(owners) > 1:
            raise RuntimeError(
                f"PresenceSnapshotCorrupt: stream has multiple owners: {identity}"
            )
        return owners[0] if owners else None

    @property
    def active_count(self) -> int:
        return sum(item.is_active for item in self._instances.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "instances": {
                key: value.to_dict()
                for key, value in sorted(self._instances.items())
            },
        }

    @staticmethod
    def _apply_result(
        target: ConsciousnessInstance | None,
        result: Any,
    ) -> ConsciousnessInstance:
        committed = ConsciousnessInstance.from_dict(dict(result.instance))
        if target is not None:
            ConsciousnessRegistry._apply_candidate(target, committed)
            return target
        return committed

    async def _ensure_chat_global(self) -> ConsciousnessInstance:
        existing = self.get(CHAT_GLOBAL_INSTANCE_ID)
        if existing is not None and existing.is_active:
            return existing
        if existing is not None:
            await self.resume(
                CHAT_GLOBAL_INSTANCE_ID,
                reason="chat_global_invariant",
                event_type="consciousness.chat_global_recovered",
            )
            recovered = self.get(CHAT_GLOBAL_INSTANCE_ID)
            assert recovered is not None
            return recovered
        now = _now_iso()
        candidate = ConsciousnessInstance(
            instance_id=CHAT_GLOBAL_INSTANCE_ID,
            kind="chat",
            display_name="全局聊天意识",
            status="active",
            created_at=now,
            last_active_at=now,
            perception_filter=PerceptionFilter.full(),
        )
        try:
            return await self.register(candidate)
        except PresenceRevisionConflict:
            await self.refresh()
            existing = self.get(CHAT_GLOBAL_INSTANCE_ID)
            if existing is None:
                raise
            if not existing.is_active:
                await self.resume(
                    CHAT_GLOBAL_INSTANCE_ID,
                    reason="chat_global_invariant",
                    event_type="consciousness.chat_global_recovered",
                )
            recovered = self.get(CHAT_GLOBAL_INSTANCE_ID)
            assert recovered is not None
            return recovered

    async def register(
        self,
        instance: ConsciousnessInstance,
        *,
        event_type: str = "consciousness.instance_registered",
    ) -> ConsciousnessInstance:
        """Commit registration and active stream ownership before exposure."""

        async with self._lock:
            candidate = ConsciousnessInstance.from_dict(instance.to_dict())
            ConsciousnessRegistry._validate_instance(candidate)
            existing = self._instances.get(candidate.instance_id)
            if existing is not None and existing.status != "terminated":
                raise ValueError(
                    f"consciousness instance '{candidate.instance_id}' already exists "
                    f"with status {existing.status}"
                )
            if existing is not None:
                candidate.revision = existing.revision
            if not candidate.created_at:
                candidate.created_at = _now_iso()
            if not candidate.last_active_at:
                candidate.last_active_at = candidate.created_at
            if not candidate.session_id:
                candidate.session_id = str(
                    candidate.metadata.get("session_id")
                    or candidate.metadata.get("episode_id")
                    or ""
                )
            refresh_lease = bool(
                candidate.is_active and candidate.lease_duration_seconds is not None
            )
            if refresh_lease:
                candidate.process_epoch = self._process_epoch
                candidate.lease_expires_at = ""
            try:
                result = await self._store.commit(
                    candidate.to_dict(),
                    expected_revision=None,
                    event_type=event_type,
                    event_payload={"occurred_at": candidate.last_active_at},
                    refresh_lease=refresh_lease,
                )
                displaced: Sequence[Any] = ()
            except StreamOwnershipConflict:
                if not refresh_lease:
                    raise
                takeover = await self._store.takeover_expired(
                    candidate.to_dict(),
                    expected_revision=None,
                    process_epoch=self._process_epoch,
                    lease_seconds=int(candidate.lease_duration_seconds or 0),
                    event_payload={"occurred_at": candidate.last_active_at},
                )
                result = takeover.claimant
                displaced = takeover.displaced
            for item in displaced:
                identity = str(item.instance["instance_id"])
                current = self._instances.get(identity)
                self._instances[identity] = self._apply_result(current, item)
            committed = self._apply_result(existing, result)
            self._instances[committed.instance_id] = committed
            ConsciousnessRegistry._apply_candidate(instance, committed)
            return instance

    async def _transition_status(
        self,
        instance_id: str,
        *,
        status: str,
        timestamp: str,
        event_type: str,
        reason: str,
    ) -> bool:
        async with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None:
                return False
            candidate = ConsciousnessInstance.from_dict(instance.to_dict())
            previous_status = candidate.status
            candidate.status = status
            refresh_lease = False
            if status == "active":
                candidate.suspended_at = ""
                candidate.last_active_at = timestamp or _now_iso()
                refresh_lease = candidate.lease_duration_seconds is not None
                if refresh_lease:
                    candidate.process_epoch = self._process_epoch
                    candidate.lease_expires_at = ""
            else:
                candidate.suspended_at = timestamp or _now_iso()
                candidate.lease_expires_at = ""
            payload = {
                "occurred_at": timestamp or _now_iso(),
                "previous_status": previous_status,
                "status": status,
                "reason": reason,
            }
            try:
                result = await self._store.commit(
                    candidate.to_dict(),
                    expected_revision=instance.revision,
                    event_type=event_type,
                    event_payload=payload,
                    refresh_lease=refresh_lease,
                )
                displaced: Sequence[Any] = ()
            except StreamOwnershipConflict:
                if not refresh_lease:
                    raise
                takeover = await self._store.takeover_expired(
                    candidate.to_dict(),
                    expected_revision=instance.revision,
                    process_epoch=self._process_epoch,
                    lease_seconds=int(candidate.lease_duration_seconds or 0),
                    event_payload=payload,
                )
                result = takeover.claimant
                displaced = takeover.displaced
            for item in displaced:
                displaced_id = str(item.instance["instance_id"])
                current = self._instances.get(displaced_id)
                self._instances[displaced_id] = self._apply_result(current, item)
            self._instances[instance_id] = self._apply_result(instance, result)
            return True

    async def suspend(
        self,
        instance_id: str,
        *,
        timestamp: str = "",
        reason: str = "requested",
    ) -> bool:
        instance = self.get(instance_id)
        if instance is None or not instance.is_active:
            return False
        return await self._transition_status(
            instance_id,
            status="suspended",
            timestamp=timestamp or _now_iso(),
            event_type="consciousness.instance_suspended",
            reason=reason,
        )

    async def resume(
        self,
        instance_id: str,
        *,
        timestamp: str = "",
        reason: str = "requested",
        event_type: str = "consciousness.instance_resumed",
    ) -> bool:
        instance = self.get(instance_id)
        if instance is None or not instance.is_suspended:
            return False
        return await self._transition_status(
            instance_id,
            status="active",
            timestamp=timestamp or _now_iso(),
            event_type=event_type,
            reason=reason,
        )

    async def terminate(
        self,
        instance_id: str,
        *,
        timestamp: str = "",
        reason: str = "requested",
    ) -> bool:
        if instance_id == CHAT_GLOBAL_INSTANCE_ID:
            return False
        instance = self.get(instance_id)
        if instance is None or instance.status == "terminated":
            return False
        return await self._transition_status(
            instance_id,
            status="terminated",
            timestamp=timestamp or _now_iso(),
            event_type="consciousness.instance_terminated",
            reason=reason,
        )

    async def touch(
        self,
        instance_id: str,
        *,
        timestamp: str = "",
        reason: str = "activity",
    ) -> None:
        async with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None or not instance.is_active:
                return
            if instance.lease_duration_seconds is not None:
                result = await self._store.renew_lease(
                    instance_id,
                    expected_revision=instance.revision,
                    process_epoch=self._process_epoch,
                    lease_seconds=instance.lease_duration_seconds,
                    event_payload={
                        "occurred_at": timestamp or _now_iso(),
                        "reason": reason,
                    },
                )
            else:
                candidate = ConsciousnessInstance.from_dict(instance.to_dict())
                candidate.last_active_at = timestamp or _now_iso()
                result = await self._store.commit(
                    candidate.to_dict(),
                    expected_revision=instance.revision,
                    event_type="consciousness.instance_seen",
                    event_payload={
                        "occurred_at": candidate.last_active_at,
                        "reason": reason,
                    },
                )
            self._instances[instance_id] = self._apply_result(instance, result)

    async def reconcile_expired(self, *, limit: int = 200) -> list[str]:
        """Suspend a bounded database-time-expired lease prefix."""

        async with self._lock:
            expired = await self._store.expire_leases(limit=max(1, int(limit)))
            identities: list[str] = []
            for result in expired:
                identity = str(result.instance["instance_id"])
                current = self._instances.get(identity)
                self._instances[identity] = self._apply_result(current, result)
                identities.append(identity)
            return identities

    async def health_snapshot(self) -> dict[str, Any]:
        return await self._store.health_snapshot()


__all__ = [
    "AsyncConsciousnessRegistry",
    "flush_presence_lifecycle_events",
    "presence_outbox_event",
]
