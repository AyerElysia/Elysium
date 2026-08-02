"""Consciousness instance model and transactional runtime presence registry.

The registry represents operational presence, not subjective world knowledge.
Durable instances and stream ownership live in SQLite; a JSON file remains only
as a compatibility export during migration.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .presence_store import (
    PRESENCE_DB_FILE,
    SQLitePresenceStore,
    StreamOwnershipConflict,
)
from .world_state import PerceptionFilter

logger = logging.getLogger(__name__)

REGISTRY_SCHEMA_VERSION = 2
CHAT_GLOBAL_INSTANCE_ID = "chat_global"


class PresenceMigrationError(RuntimeError):
    """Raised when legacy presence cannot be imported without data loss."""


def _now_iso() -> str:
    """Return a timezone-aware timestamp for presence changes."""

    return datetime.now(UTC).astimezone().isoformat()


def _parse_datetime(value: str) -> datetime | None:
    """Parse a persisted ISO timestamp without inventing one on failure."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


@dataclass(slots=True)
class ConsciousnessInstance:
    """One local runtime window of the same continuous subject."""

    instance_id: str
    kind: str = "chat"
    display_name: str = ""
    stream_ids: list[str] = field(default_factory=list)
    status: str = "active"
    created_at: str = ""
    last_active_at: str = ""
    suspended_at: str = ""
    perception_filter: PerceptionFilter = field(default_factory=PerceptionFilter.full)
    metadata: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    process_epoch: str = ""
    lease_expires_at: str = ""
    lease_duration_seconds: int | None = None
    revision: int = 0

    @property
    def is_active(self) -> bool:
        """Return whether the runtime currently owns its declared streams."""

        return self.status == "active"

    @property
    def is_suspended(self) -> bool:
        """Return whether the runtime can be resumed with the same identity."""

        return self.status == "suspended"

    def to_dict(self) -> dict[str, Any]:
        """Serialize one instance for SQLite or compatibility export."""

        return {
            "instance_id": self.instance_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "stream_ids": list(self.stream_ids),
            "status": self.status,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "suspended_at": self.suspended_at,
            "perception_filter": self.perception_filter.to_dict(),
            "metadata": dict(self.metadata),
            "session_id": self.session_id,
            "process_epoch": self.process_epoch,
            "lease_expires_at": self.lease_expires_at,
            "lease_duration_seconds": self.lease_duration_seconds,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConsciousnessInstance:
        """Restore one instance from a durable transfer record."""

        pf_data = data.get("perception_filter")
        perception_filter = (
            PerceptionFilter.from_dict(pf_data)
            if isinstance(pf_data, dict)
            else PerceptionFilter.full()
        )
        raw_lease = data.get("lease_duration_seconds")
        lease_duration = int(raw_lease) if raw_lease is not None else None
        return cls(
            instance_id=str(data.get("instance_id") or ""),
            kind=str(data.get("kind") or "chat"),
            display_name=str(data.get("display_name") or ""),
            stream_ids=[str(value) for value in (data.get("stream_ids") or []) if value],
            status=str(data.get("status") or "active"),
            created_at=str(data.get("created_at") or ""),
            last_active_at=str(data.get("last_active_at") or ""),
            suspended_at=str(data.get("suspended_at") or ""),
            perception_filter=perception_filter,
            metadata=dict(data.get("metadata") or {}),
            session_id=str(data.get("session_id") or ""),
            process_epoch=str(data.get("process_epoch") or ""),
            lease_expires_at=str(data.get("lease_expires_at") or ""),
            lease_duration_seconds=lease_duration,
            revision=int(data.get("revision") or 0),
        )


class ConsciousnessRegistry:
    """Manage durable instance presence and exclusive active stream ownership."""

    def __init__(
        self,
        store: SQLitePresenceStore | None = None,
        *,
        process_epoch: str = "",
        bootstrap: bool = True,
    ) -> None:
        """Load the store snapshot and establish a process epoch."""

        self._store = store
        self._process_epoch = process_epoch or ("proc_" + uuid4().hex)
        self._lock = threading.RLock()
        self._memory_outbox: list[dict[str, Any]] = []
        self._instances: dict[str, ConsciousnessInstance] = {}
        if store is not None:
            self._instances = {
                raw["instance_id"]: ConsciousnessInstance.from_dict(raw)
                for raw in store.list_instances()
            }
        if bootstrap:
            self._ensure_chat_global()

    @property
    def process_epoch(self) -> str:
        """Return the identity of this registry-owning process."""

        return self._process_epoch

    @property
    def database_path(self) -> Path | None:
        """Return the authoritative database path when persistence is enabled."""

        return self._store.path if self._store is not None else None

    @staticmethod
    def _validate_instance(instance: ConsciousnessInstance) -> None:
        """Validate structural presence fields without judging cognition."""

        instance.instance_id = instance.instance_id.strip()
        instance.kind = instance.kind.strip()
        instance.stream_ids = list(
            dict.fromkeys(
                value.strip()
                for value in instance.stream_ids
                if isinstance(value, str) and value.strip()
            )
        )
        if not instance.instance_id:
            raise ValueError("consciousness instance_id must not be empty")
        if not instance.kind:
            raise ValueError("consciousness instance kind must not be empty")
        if not instance.status.strip():
            raise ValueError("consciousness presence status must not be empty")
        if (
            instance.lease_duration_seconds is not None
            and instance.lease_duration_seconds <= 0
        ):
            raise ValueError("lease_duration_seconds must be positive")

    def _assert_streams_available(
        self,
        instance: ConsciousnessInstance,
    ) -> None:
        """Reject an active stream claim already held in this snapshot."""

        if not instance.is_active:
            return
        for stream_id in instance.stream_ids:
            for other in self._instances.values():
                if (
                    other.instance_id != instance.instance_id
                    and other.is_active
                    and stream_id in other.stream_ids
                ):
                    raise StreamOwnershipConflict(
                        stream_id,
                        other.instance_id,
                        instance.instance_id,
                    )

    def _renew_lease(
        self,
        instance: ConsciousnessInstance,
        *,
        timestamp: str,
    ) -> None:
        """Renew an explicitly configured technical liveness lease."""

        if instance.lease_duration_seconds is None:
            return
        base = _parse_datetime(timestamp) or datetime.now(UTC)
        instance.process_epoch = self._process_epoch
        instance.lease_expires_at = (
            base + timedelta(seconds=instance.lease_duration_seconds)
        ).isoformat()

    def _enqueue_memory_event(
        self,
        *,
        event_type: str,
        instance: ConsciousnessInstance,
        previous_revision: int,
        event_payload: dict[str, Any],
    ) -> None:
        """Retain lifecycle events for registries used without SQLite."""

        payload = dict(event_payload)
        payload["instance"] = instance.to_dict()
        payload["previous_revision"] = previous_revision
        payload["revision"] = instance.revision
        self._memory_outbox.append(
            {
                "outbox_id": len(self._memory_outbox) + 1,
                "occurrence_id": "presence_" + uuid4().hex,
                "event_type": event_type,
                "instance_id": instance.instance_id,
                "stream_id": instance.stream_ids[0] if instance.stream_ids else "",
                "occurred_at": str(
                    payload.get("occurred_at")
                    or instance.last_active_at
                    or _now_iso()
                ),
                "payload": payload,
            }
        )

    def _persist_transition(
        self,
        instance: ConsciousnessInstance,
        *,
        expected_revision: int | None,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist one revision and its lifecycle evidence before exposure."""

        payload = dict(event_payload or {})
        previous_revision = (
            instance.revision if expected_revision is None else expected_revision
        )
        if self._store is not None:
            instance.revision = self._store.commit(
                instance.to_dict(),
                expected_revision=expected_revision,
                event_type=event_type,
                event_payload=payload,
            )
        else:
            instance.revision = previous_revision + 1
            self._enqueue_memory_event(
                event_type=event_type,
                instance=instance,
                previous_revision=previous_revision,
                event_payload=payload,
            )

    @staticmethod
    def _apply_candidate(
        target: ConsciousnessInstance,
        candidate: ConsciousnessInstance,
    ) -> None:
        """Commit a persisted candidate without invalidating held references."""

        for model_field in dataclass_fields(ConsciousnessInstance):
            setattr(target, model_field.name, getattr(candidate, model_field.name))

    def _ensure_chat_global(self) -> None:
        """Ensure the default chat presence exists and cannot remain inactive."""

        with self._lock:
            existing = self._instances.get(CHAT_GLOBAL_INSTANCE_ID)
            now = _now_iso()
            if existing is None:
                self.register(
                    ConsciousnessInstance(
                        instance_id=CHAT_GLOBAL_INSTANCE_ID,
                        kind="chat",
                        display_name="全局聊天意识",
                        status="active",
                        created_at=now,
                        last_active_at=now,
                        perception_filter=PerceptionFilter.full(),
                    ),
                    event_type="consciousness.instance_registered",
                )
            elif not existing.is_active:
                self._transition_status(
                    existing,
                    status="active",
                    timestamp=now,
                    event_type="consciousness.chat_global_recovered",
                    reason="chat_global_invariant",
                )

    def register(
        self,
        instance: ConsciousnessInstance,
        *,
        event_type: str = "consciousness.instance_registered",
    ) -> ConsciousnessInstance:
        """Register an instance and atomically claim all of its active streams."""

        with self._lock:
            self._validate_instance(instance)
            existing = self._instances.get(instance.instance_id)
            if existing is not None and existing.status != "terminated":
                raise ValueError(
                    f"consciousness instance '{instance.instance_id}' already exists "
                    f"with status {existing.status}"
                )
            if existing is not None:
                instance.revision = existing.revision
            if not instance.created_at:
                instance.created_at = _now_iso()
            if not instance.last_active_at:
                instance.last_active_at = instance.created_at
            if not instance.session_id:
                instance.session_id = str(
                    instance.metadata.get("session_id")
                    or instance.metadata.get("episode_id")
                    or ""
                )
            if instance.is_active:
                self._renew_lease(instance, timestamp=instance.last_active_at)
            self._assert_streams_available(instance)
            self._persist_transition(
                instance,
                expected_revision=None,
                event_type=event_type,
                event_payload={"occurred_at": instance.last_active_at},
            )
            self._instances[instance.instance_id] = instance
            logger.info(
                "registered consciousness instance: %s (kind=%s, streams=%s)",
                instance.instance_id,
                instance.kind,
                instance.stream_ids,
            )
            return instance

    def _transition_status(
        self,
        instance: ConsciousnessInstance,
        *,
        status: str,
        timestamp: str,
        event_type: str,
        reason: str,
    ) -> bool:
        """Commit one technical lifecycle transition with its reason."""

        previous_status = instance.status
        expected_revision = instance.revision
        candidate = ConsciousnessInstance.from_dict(instance.to_dict())
        candidate.status = status
        if status == "active":
            candidate.suspended_at = ""
            candidate.last_active_at = timestamp or _now_iso()
            self._renew_lease(candidate, timestamp=candidate.last_active_at)
            self._assert_streams_available(candidate)
        else:
            candidate.suspended_at = timestamp or _now_iso()
            candidate.lease_expires_at = ""
        self._persist_transition(
            candidate,
            expected_revision=expected_revision,
            event_type=event_type,
            event_payload={
                "occurred_at": timestamp or _now_iso(),
                "previous_status": previous_status,
                "status": status,
                "reason": reason,
            },
        )
        self._apply_candidate(instance, candidate)
        self._instances[instance.instance_id] = instance
        return True

    def suspend(
        self,
        instance_id: str,
        *,
        timestamp: str = "",
        reason: str = "requested",
    ) -> bool:
        """Suspend an active instance and release all of its stream claims."""

        with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None or not instance.is_active:
                return False
            return self._transition_status(
                instance,
                status="suspended",
                timestamp=timestamp or _now_iso(),
                event_type="consciousness.instance_suspended",
                reason=reason,
            )

    def resume(
        self,
        instance_id: str,
        *,
        timestamp: str = "",
        reason: str = "requested",
    ) -> bool:
        """Resume a suspended instance after reclaiming its streams."""

        with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None or not instance.is_suspended:
                return False
            return self._transition_status(
                instance,
                status="active",
                timestamp=timestamp or _now_iso(),
                event_type="consciousness.instance_resumed",
                reason=reason,
            )

    def terminate(
        self,
        instance_id: str,
        *,
        timestamp: str = "",
        reason: str = "requested",
    ) -> bool:
        """Terminate an instance and release all of its stream claims."""

        if instance_id == CHAT_GLOBAL_INSTANCE_ID:
            logger.warning("the global chat consciousness cannot be terminated")
            return False
        with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None or instance.status == "terminated":
                return False
            return self._transition_status(
                instance,
                status="terminated",
                timestamp=timestamp or _now_iso(),
                event_type="consciousness.instance_terminated",
                reason=reason,
            )

    def touch(
        self,
        instance_id: str,
        *,
        timestamp: str = "",
        reason: str = "activity",
    ) -> None:
        """Renew activity and lease while persisting a causally visible event."""

        with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None or not instance.is_active:
                return
            candidate = ConsciousnessInstance.from_dict(instance.to_dict())
            candidate.last_active_at = timestamp or _now_iso()
            self._renew_lease(candidate, timestamp=candidate.last_active_at)
            self._persist_transition(
                candidate,
                expected_revision=instance.revision,
                event_type="consciousness.instance_seen",
                event_payload={
                    "occurred_at": candidate.last_active_at,
                    "reason": reason,
                },
            )
            self._apply_candidate(instance, candidate)
            self._instances[instance_id] = instance

    def reconcile_expired(self, *, timestamp: str = "") -> list[str]:
        """Suspend expired leased instances so crashes do not leave ghosts."""

        now = _parse_datetime(timestamp) or datetime.now(UTC)
        expired: list[str] = []
        with self._lock:
            for instance in list(self._instances.values()):
                lease_expiry = _parse_datetime(instance.lease_expires_at)
                if (
                    instance.is_active
                    and lease_expiry is not None
                    and lease_expiry <= now
                    and self._transition_status(
                        instance,
                        status="suspended",
                        timestamp=now.isoformat(),
                        event_type="consciousness.instance_lease_expired",
                        reason="lease_expired",
                    )
                ):
                    expired.append(instance.instance_id)
        return expired

    def get(self, instance_id: str) -> ConsciousnessInstance | None:
        """Return an instance by its stable runtime identity."""

        with self._lock:
            return self._instances.get(instance_id)

    def get_active(self) -> list[ConsciousnessInstance]:
        """Return the current active presence snapshot."""

        with self._lock:
            return [instance for instance in self._instances.values() if instance.is_active]

    def get_all(self) -> list[ConsciousnessInstance]:
        """Return all current and historical registry entries."""

        with self._lock:
            return list(self._instances.values())

    def get_for_stream(self, stream_id: str) -> ConsciousnessInstance | None:
        """Find the unique active owner, retaining chat fallback for migration."""

        with self._lock:
            owners = [
                instance
                for instance in self._instances.values()
                if instance.is_active and stream_id in instance.stream_ids
            ]
            if len(owners) > 1:
                raise RuntimeError(f"multiple active owners found for stream '{stream_id}'")
            if owners:
                return owners[0]
            return self._instances.get(CHAT_GLOBAL_INSTANCE_ID)

    def get_by_kind(self, kind: str) -> list[ConsciousnessInstance]:
        """Return active instances declaring the requested open kind string."""

        with self._lock:
            return [
                instance
                for instance in self._instances.values()
                if instance.is_active and instance.kind == kind
            ]

    @property
    def active_count(self) -> int:
        """Return the number of currently active instances."""

        return len(self.get_active())

    def to_dict(self) -> dict[str, Any]:
        """Build the compatibility export shape."""

        with self._lock:
            return {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "storage": "sqlite_presence_v1",
                "process_epoch": self._process_epoch,
                "instances": {
                    instance_id: instance.to_dict()
                    for instance_id, instance in self._instances.items()
                },
            }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConsciousnessRegistry:
        """Restore an in-memory registry from a compatibility snapshot."""

        registry = cls(bootstrap=False)
        raw_instances = data.get("instances")
        if isinstance(raw_instances, dict):
            for key, raw in raw_instances.items():
                if not isinstance(raw, dict):
                    continue
                instance = ConsciousnessInstance.from_dict(raw)
                if not instance.instance_id:
                    instance.instance_id = str(key)
                if instance.instance_id != str(key):
                    raise ValueError(
                        "registry key does not match embedded consciousness instance_id: "
                        f"{key!r} != {instance.instance_id!r}"
                    )
                registry._validate_instance(instance)
                registry._assert_streams_available(instance)
                registry._instances[instance.instance_id] = instance
        registry._ensure_chat_global()
        registry._memory_outbox.clear()
        return registry

    def to_json(self) -> str:
        """Serialize the compatibility export without changing authority."""

        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> ConsciousnessRegistry:
        """Parse one compatibility JSON registry export."""

        raw = json.loads(value)
        if not isinstance(raw, dict):
            raise TypeError("registry JSON must be an object")
        return cls.from_dict(raw)

    @classmethod
    def load(cls, path: Path) -> ConsciousnessRegistry:
        """Load SQLite presence and import the legacy JSON once when necessary."""

        store = SQLitePresenceStore(path.with_name(PRESENCE_DB_FILE))
        durable_rows = store.list_instances()
        if durable_rows:
            registry = cls(store=store)
            registry.reconcile_expired()
            return registry

        registry = cls(store=store, bootstrap=False)
        legacy_instances: list[ConsciousnessInstance] = []
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise TypeError("registry JSON must be an object")
                raw_instances = raw.get("instances")
                if isinstance(raw_instances, dict):
                    for key, value in raw_instances.items():
                        if not isinstance(value, dict):
                            continue
                        instance = ConsciousnessInstance.from_dict(value)
                        if not instance.instance_id:
                            instance.instance_id = str(key)
                        if instance.instance_id != str(key):
                            raise ValueError(
                                "registry key does not match embedded instance_id: "
                                f"{key!r} != {instance.instance_id!r}"
                            )
                        cls._validate_instance(instance)
                        legacy_instances.append(instance)
            except (
                OSError,
                TypeError,
                UnicodeError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                raise PresenceMigrationError(
                    "legacy consciousness registry could not be imported; "
                    "the source file was preserved"
                ) from exc
        for instance in legacy_instances:
            try:
                registry.register(
                    instance,
                    event_type="consciousness.instance_imported",
                )
            except StreamOwnershipConflict as exc:
                instance.status = "suspended"
                instance.suspended_at = _now_iso()
                instance.metadata["legacy_import_conflict"] = str(exc)
                registry.register(
                    instance,
                    event_type="consciousness.instance_imported_suspended",
                )
        registry._ensure_chat_global()
        registry.reconcile_expired()
        return registry

    def save(self, path: Path) -> None:
        """Write a compatibility JSON export; SQLite is already authoritative."""

        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
            tmp.write_text(self.to_json(), encoding="utf-8")
            os.replace(tmp, path)

    def flush_lifecycle_events(
        self,
        append_sync: Callable[[Any], Any],
    ) -> int:
        """Publish pending outbox events idempotently to the raw event ledger."""

        from .event_bus import (
            LifeEvent,
            LifeEventChannel,
            LifeEventPriority,
        )

        published = 0
        with self._lock:
            pending = (
                self._store.pending_events()
                if self._store is not None
                else list(self._memory_outbox)
            )
            for item in pending:
                payload = dict(item["payload"])
                instance = payload.get("instance")
                session_id = (
                    str(instance.get("session_id") or "")
                    if isinstance(instance, dict)
                    else ""
                )
                event = LifeEvent(
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
                append_sync(event)
                if self._store is not None:
                    self._store.acknowledge_events([int(item["outbox_id"])])
                else:
                    self._memory_outbox.remove(item)
                published += 1
        return published

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._store is not None:
                health = self._store.health_snapshot()
            else:
                health = {
                    "database_path": None,
                    "instance_count": len(self._instances),
                    "active_count": self.active_count,
                    "owned_stream_count": len(
                        {
                            stream_id
                            for instance in self._instances.values()
                            if instance.is_active
                            for stream_id in instance.stream_ids
                        }
                    ),
                    "pending_event_count": len(self._memory_outbox),
                }
            health["process_epoch"] = self._process_epoch
            return health
