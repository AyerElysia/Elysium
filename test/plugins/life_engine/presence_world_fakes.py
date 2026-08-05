"""In-memory reference adapters for shared Presence/World contract tests."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from plugins.life_engine.service.event_bus import LifeEvent
from plugins.life_engine.service.presence_store import (
    PresenceRevisionConflict,
    StreamOwnershipConflict,
)
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
    WorldAssertionReference,
    WorldAssertionReferencePage,
    WorldChangeReference,
    WorldChangeReferencePage,
    WorldProjectionChange,
    WorldProjectionConflict,
    WorldProjectionStore,
    WorldProjectionUnavailable,
    WorldValueChunk,
)
from plugins.life_engine.storage.domain_contracts import (
    PresenceCommitResult,
    PresenceLeaseConflict,
    PresenceTakeoverResult,
    PresenceWorldStores,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _parsed(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = (
        value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    )
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


class FakePresenceStore:
    """Transactional in-memory reference for the Presence Port."""

    def __init__(self) -> None:
        self._instances: dict[str, dict[str, Any]] = {}
        self._owners: dict[str, str] = {}
        self._outbox: list[dict[str, Any]] = []
        self._next_outbox_id = 1
        self._lock = asyncio.Lock()

    @staticmethod
    def _normalize(instance: dict[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(instance)
        identity = str(value.get("instance_id") or "").strip()
        if not identity:
            raise ValueError("consciousness instance_id must not be empty")
        value["instance_id"] = identity
        value["status"] = str(value.get("status") or "").strip()
        value["stream_ids"] = list(
            dict.fromkeys(
                str(item).strip()
                for item in (value.get("stream_ids") or [])
                if str(item).strip()
            )
        )
        value["perception_filter"] = dict(value.get("perception_filter") or {})
        value["metadata"] = dict(value.get("metadata") or {})
        value["lease_duration_seconds"] = (
            int(value["lease_duration_seconds"])
            if value.get("lease_duration_seconds") is not None
            else None
        )
        return value

    def _commit_state(
        self,
        instances: dict[str, dict[str, Any]],
        owners: dict[str, str],
        outbox: list[dict[str, Any]],
        instance: dict[str, Any],
        *,
        expected_revision: int | None,
        event_type: str,
        event_payload: dict[str, Any] | None,
        database_now: datetime,
        refresh_lease: bool,
    ) -> PresenceCommitResult:
        snapshot = self._normalize(instance)
        identity = snapshot["instance_id"]
        existing = instances.get(identity)
        if expected_revision is None:
            if existing is not None and existing["status"] != "terminated":
                raise PresenceRevisionConflict(
                    f"consciousness instance '{identity}' already exists"
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
            duration = snapshot.get("lease_duration_seconds")
            if snapshot["status"] != "active" or duration is None:
                raise PresenceLeaseConflict("active lease duration is required")
            snapshot["lease_expires_at"] = (
                database_now + timedelta(seconds=duration)
            ).isoformat()
        elif snapshot["status"] == "active" and (
            snapshot.get("lease_duration_seconds") is not None
            or snapshot.get("lease_expires_at")
        ):
            raise PresenceLeaseConflict(
                "active lease timestamps must be generated from database time"
            )
        elif snapshot["status"] != "active":
            snapshot["lease_expires_at"] = ""
        if snapshot["status"] == "active":
            for stream_id in snapshot["stream_ids"]:
                owner = owners.get(stream_id)
                if owner is not None and owner != identity:
                    raise StreamOwnershipConflict(stream_id, owner, identity)
        for stream_id, owner in list(owners.items()):
            if owner == identity:
                del owners[stream_id]
        if snapshot["status"] == "active":
            for stream_id in snapshot["stream_ids"]:
                owners[stream_id] = identity
        snapshot["revision"] = previous_revision + 1
        instances[identity] = copy.deepcopy(snapshot)
        if event_type:
            payload = copy.deepcopy(event_payload or {})
            payload.update(
                {
                    "instance": copy.deepcopy(snapshot),
                    "previous_revision": previous_revision,
                    "revision": snapshot["revision"],
                }
            )
            outbox.append(
                {
                    "outbox_id": self._next_outbox_id + len(outbox) - len(self._outbox),
                    "occurrence_id": "presence_" + uuid4().hex,
                    "event_type": event_type,
                    "instance_id": identity,
                    "stream_id": snapshot["stream_ids"][0]
                    if snapshot["stream_ids"]
                    else "",
                    "occurred_at": str(
                        payload.get("occurred_at") or database_now.isoformat()
                    ),
                    "payload": payload,
                    "published": False,
                }
            )
        return PresenceCommitResult(
            instance=copy.deepcopy(snapshot),
            previous_revision=previous_revision,
            revision=int(snapshot["revision"]),
            database_now=database_now.isoformat(),
        )

    async def list_instances(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                copy.deepcopy(self._instances[key]) for key in sorted(self._instances)
            ]

    async def commit(
        self,
        instance: dict[str, Any],
        *,
        expected_revision: int | None,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
        refresh_lease: bool = False,
    ) -> PresenceCommitResult:
        async with self._lock:
            instances = copy.deepcopy(self._instances)
            owners = dict(self._owners)
            outbox = copy.deepcopy(self._outbox)
            result = self._commit_state(
                instances,
                owners,
                outbox,
                instance,
                expected_revision=expected_revision,
                event_type=event_type,
                event_payload=event_payload,
                database_now=_now(),
                refresh_lease=refresh_lease,
            )
            self._instances, self._owners, self._outbox = instances, owners, outbox
            self._next_outbox_id = len(outbox) + 1
            return result

    async def renew_lease(
        self,
        instance_id: str,
        *,
        expected_revision: int,
        process_epoch: str,
        lease_seconds: int,
        event_payload: dict[str, Any] | None = None,
    ) -> PresenceCommitResult:
        async with self._lock:
            instances = copy.deepcopy(self._instances)
            owners = dict(self._owners)
            outbox = copy.deepcopy(self._outbox)
            snapshot = instances.get(instance_id)
            if snapshot is None or int(snapshot["revision"]) != int(expected_revision):
                raise PresenceRevisionConflict("presence revision conflict")
            if (
                snapshot["status"] != "active"
                or snapshot["process_epoch"] != process_epoch
            ):
                raise PresenceLeaseConflict(
                    "process epoch does not own active Presence"
                )
            database_now = _now()
            snapshot["last_active_at"] = database_now.isoformat()
            snapshot["lease_duration_seconds"] = int(lease_seconds)
            result = self._commit_state(
                instances,
                owners,
                outbox,
                snapshot,
                expected_revision=expected_revision,
                event_type="consciousness.instance_seen",
                event_payload=event_payload,
                database_now=database_now,
                refresh_lease=True,
            )
            self._instances, self._owners, self._outbox = instances, owners, outbox
            self._next_outbox_id = len(outbox) + 1
            return result

    async def takeover_expired(
        self,
        instance: dict[str, Any],
        *,
        expected_revision: int | None,
        process_epoch: str,
        lease_seconds: int,
        event_payload: dict[str, Any] | None = None,
    ) -> PresenceTakeoverResult:
        async with self._lock:
            instances = copy.deepcopy(self._instances)
            owners = dict(self._owners)
            outbox = copy.deepcopy(self._outbox)
            claimant = self._normalize(instance)
            claimant.update(
                {
                    "status": "active",
                    "process_epoch": process_epoch,
                    "lease_duration_seconds": int(lease_seconds),
                }
            )
            database_now = _now()
            displaced: list[PresenceCommitResult] = []
            owner_ids = sorted(
                {
                    owners[stream_id]
                    for stream_id in claimant["stream_ids"]
                    if stream_id in owners
                    and owners[stream_id] != claimant["instance_id"]
                }
            )
            for owner_id in owner_ids:
                owner = instances[owner_id]
                expiry = _parsed(owner.get("lease_expires_at"))
                if owner["status"] != "active" or expiry is None:
                    raise PresenceLeaseConflict(
                        "owner is not an expirable active lease"
                    )
                if expiry >= database_now:
                    stream = next(
                        item
                        for item in claimant["stream_ids"]
                        if owners.get(item) == owner_id
                    )
                    raise StreamOwnershipConflict(
                        stream, owner_id, claimant["instance_id"]
                    )
                owner.update(
                    {
                        "status": "suspended",
                        "suspended_at": database_now.isoformat(),
                        "lease_expires_at": "",
                    }
                )
                displaced.append(
                    self._commit_state(
                        instances,
                        owners,
                        outbox,
                        owner,
                        expected_revision=int(owner["revision"]),
                        event_type="consciousness.instance_lease_expired",
                        event_payload={"occurred_at": database_now.isoformat()},
                        database_now=database_now,
                        refresh_lease=False,
                    )
                )
            claimant["last_active_at"] = database_now.isoformat()
            committed = self._commit_state(
                instances,
                owners,
                outbox,
                claimant,
                expected_revision=expected_revision,
                event_type="consciousness.instance_taken_over",
                event_payload=event_payload,
                database_now=database_now,
                refresh_lease=True,
            )
            self._instances, self._owners, self._outbox = instances, owners, outbox
            self._next_outbox_id = len(outbox) + 1
            return PresenceTakeoverResult(committed, tuple(displaced))

    async def expire_leases(
        self,
        *,
        limit: int = 200,
    ) -> tuple[PresenceCommitResult, ...]:
        async with self._lock:
            instances = copy.deepcopy(self._instances)
            owners = dict(self._owners)
            outbox = copy.deepcopy(self._outbox)
            database_now = _now()
            candidates = sorted(
                (
                    item
                    for item in instances.values()
                    if item["status"] == "active"
                    and item.get("lease_duration_seconds") is not None
                    and _parsed(item.get("lease_expires_at")) is not None
                ),
                key=lambda item: (
                    _parsed(item.get("lease_expires_at")) or database_now,
                    str(item["instance_id"]),
                ),
            )[: max(1, int(limit))]
            expired: list[PresenceCommitResult] = []
            for snapshot in candidates:
                expiry = _parsed(snapshot.get("lease_expires_at"))
                if expiry is None or expiry >= database_now:
                    break
                snapshot["status"] = "suspended"
                snapshot["suspended_at"] = database_now.isoformat()
                snapshot["lease_expires_at"] = ""
                expired.append(
                    self._commit_state(
                        instances,
                        owners,
                        outbox,
                        snapshot,
                        expected_revision=int(snapshot["revision"]),
                        event_type="consciousness.instance_lease_expired",
                        event_payload={
                            "occurred_at": database_now.isoformat(),
                            "reason": "lease_expired_reconcile",
                        },
                        database_now=database_now,
                        refresh_lease=False,
                    )
                )
            self._instances, self._owners, self._outbox = (
                instances,
                owners,
                outbox,
            )
            self._next_outbox_id = len(outbox) + 1
            return tuple(expired)

    async def pending_events(self, limit: int = 200) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                {
                    key: copy.deepcopy(value)
                    for key, value in item.items()
                    if key != "published"
                }
                for item in self._outbox
                if not item["published"]
            ][: max(1, int(limit))]

    async def acknowledge_events(self, outbox_ids: list[int]) -> None:
        identities = {int(value) for value in outbox_ids}
        async with self._lock:
            for item in self._outbox:
                if item["outbox_id"] in identities:
                    item["published"] = True

    async def health_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "backend": "fake",
                "instance_count": len(self._instances),
                "active_count": sum(
                    item["status"] == "active" for item in self._instances.values()
                ),
                "owned_stream_count": len(self._owners),
                "pending_event_count": sum(
                    not item["published"] for item in self._outbox
                ),
            }


class FakeWorldProjectionStore:
    """In-memory source-preserving reference for the World Port."""

    def __init__(self) -> None:
        self._assertions: dict[str, WorldAssertion] = {}
        self._changes: dict[int, WorldProjectionChange] = {}
        self._cursors: dict[str, tuple[int, int]] = {}
        self._frontier = 0
        self._state = WORLD_REBUILD_IDLE
        self._lock = asyncio.Lock()

    @staticmethod
    def _payload(event: LifeEvent) -> dict[str, Any]:
        value = json.loads(event.content)
        return value if isinstance(value, dict) else {"content": value}

    def _apply(self, event: LifeEvent) -> None:
        payload = self._payload(event)
        if event.event_type in {WORLD_OBSERVATION_EVENT, WORLD_LEGACY_IMPORT_EVENT}:
            raw = payload.get("assertions")
            assertions = (
                raw if isinstance(raw, list) else [payload.get("assertion", payload)]
            )
            for index, item in enumerate(assertions):
                if not isinstance(item, dict):
                    continue
                assertion_id = str(item.get("assertion_id") or "") or (
                    "assertion_"
                    + hashlib.sha256(
                        f"{event.occurrence_id or event.event_id}:{index}".encode()
                    ).hexdigest()
                )
                observed = str(item.get("observed_at") or event.timestamp)
                assertion = WorldAssertion(
                    assertion_id=assertion_id,
                    subject=str(item.get("subject") or ""),
                    predicate=str(item.get("predicate") or ""),
                    value=copy.deepcopy(item.get("value")),
                    domain=str(item.get("domain") or ""),
                    status=str(item.get("status") or ""),
                    source_instance_id=event.source_instance_id,
                    source_event_id=event.event_id,
                    occurrence_id=event.occurrence_id or event.event_id,
                    observed_at=observed,
                    valid_from=str(item.get("valid_from") or observed),
                    valid_to=str(item.get("valid_to") or ""),
                    recorded_at=event.recorded_at,
                    supersedes_assertion_id=str(
                        item.get("supersedes_assertion_id") or ""
                    ),
                    retracted_at="",
                    retracted_by_assertion_id="",
                    payload=copy.deepcopy(item),
                )
                existing = self._assertions.get(assertion_id)
                if existing is not None and (
                    existing.payload != assertion.payload
                    or existing.occurrence_id != assertion.occurrence_id
                ):
                    raise WorldProjectionConflict(
                        f"assertion identity reused with different evidence: {assertion_id}"
                    )
                self._assertions.setdefault(assertion_id, assertion)
                retracts = str(item.get("retracts_assertion_id") or "")
                if retracts in self._assertions:
                    self._assertions[retracts] = replace(
                        self._assertions[retracts],
                        retracted_at=observed,
                        retracted_by_assertion_id=assertion_id,
                    )
            change_kind = "world_observation"
        elif event.event_type.startswith("consciousness.instance_") or (
            event.event_type == "consciousness.chat_global_recovered"
        ):
            change_kind = "consciousness_presence"
        else:
            return
        change = WorldProjectionChange(
            ingest_position=event.sequence,
            event_id=event.event_id,
            occurrence_id=event.occurrence_id or event.event_id,
            event_type=event.event_type,
            change_kind=change_kind,
            source_instance_id=event.source_instance_id,
            stream_id=event.stream_id,
            occurred_at=event.timestamp,
            recorded_at=event.recorded_at,
            payload=copy.deepcopy(payload),
        )
        existing_change = self._changes.get(event.sequence)
        if existing_change is not None and existing_change != change:
            raise WorldProjectionConflict(
                f"world ingest position reused with different evidence: {event.sequence}"
            )
        self._changes.setdefault(event.sequence, change)

    async def apply_events(self, events: list[LifeEvent]) -> int:
        async with self._lock:
            if self._state not in {WORLD_REBUILD_IDLE, WORLD_REBUILDING}:
                raise WorldProjectionUnavailable("world projection is unavailable")
            assertions = copy.deepcopy(self._assertions)
            changes = copy.deepcopy(self._changes)
            frontier = self._frontier
            try:
                for event in sorted(events, key=lambda item: item.sequence):
                    self._apply(event)
                    self._frontier = max(self._frontier, event.sequence)
            except BaseException:
                self._assertions, self._changes, self._frontier = (
                    assertions,
                    changes,
                    frontier,
                )
                raise
            return self._frontier

    async def begin_rebuild(self) -> None:
        async with self._lock:
            self._assertions.clear()
            self._changes.clear()
            self._frontier = 0
            self._state = WORLD_REBUILDING

    async def finish_rebuild(self, *, expected_frontier: int) -> None:
        async with self._lock:
            if self._state != WORLD_REBUILDING or self._frontier != expected_frontier:
                raise WorldProjectionConflict("world rebuild completion mismatch")
            self._state = WORLD_REBUILD_IDLE

    async def fail_rebuild(self) -> None:
        async with self._lock:
            self._state = WORLD_REBUILD_FAILED

    async def projector_contract(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "policy": WORLD_PROJECTOR_POLICY,
                "schema_version": WORLD_PROJECTOR_SCHEMA_VERSION,
                "as_of_ingest_position": self._frontier,
                "rebuild_state": self._state,
            }

    async def list_assertions(
        self,
        *,
        include_retracted: bool = True,
    ) -> list[WorldAssertion]:
        async with self._lock:
            values = self._assertions.values()
            if not include_retracted:
                values = [item for item in values if not item.retracted_at]
            return sorted(
                values, key=lambda item: (item.observed_at, item.assertion_id)
            )

    async def list_assertion_references_page(
        self,
        *,
        include_retracted: bool = False,
        after_observed_at: str = "",
        after_assertion_id: str = "",
        limit: int = 128,
        inline_max_bytes: int = 1024,
    ) -> WorldAssertionReferencePage:
        values = await self.list_assertions(include_retracted=include_retracted)
        filtered = [
            item
            for item in values
            if (item.observed_at, item.assertion_id)
            > (str(after_observed_at or ""), str(after_assertion_id or ""))
        ]
        page_limit = max(1, min(int(limit), 1000))
        selected = filtered[:page_limit]
        items: list[WorldAssertionReference] = []
        total_bytes = 0
        for item in filtered:
            raw = json.dumps(
                item.value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            total_bytes += len(raw.encode("utf-8"))
        for item in selected:
            raw = json.dumps(
                item.value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            value_bytes = len(raw.encode("utf-8"))
            inlined = value_bytes <= int(inline_max_bytes)
            value = item.value if inlined else None
            trace = item.value if isinstance(item.value, dict) else {}
            context = (
                trace.get("payload", {}).get("context", {})
                if isinstance(trace.get("payload"), dict)
                else {}
            )
            transport_echo = bool(
                item.domain == "minecraft"
                and item.predicate == "embodied_trace"
                and trace.get("trace_kind") == "intent.issued"
                and isinstance(context, dict)
                and "transient_world_perception" in context
            )
            items.append(
                WorldAssertionReference(
                    assertion_id=item.assertion_id,
                    subject=item.subject,
                    predicate=item.predicate,
                    domain=item.domain,
                    status=item.status,
                    source_instance_id=item.source_instance_id,
                    source_event_id=item.source_event_id,
                    occurrence_id=item.occurrence_id,
                    observed_at=item.observed_at,
                    valid_from=item.valid_from,
                    valid_to=item.valid_to,
                    recorded_at=item.recorded_at,
                    supersedes_assertion_id=item.supersedes_assertion_id,
                    value_bytes=value_bytes,
                    value_inlined=inlined,
                    value=copy.deepcopy(value),
                    transport_echo=transport_echo,
                )
            )
        has_more = len(filtered) > page_limit
        last = selected[-1] if selected and has_more else None
        return WorldAssertionReferencePage(
            items=tuple(items),
            total_items=len(filtered),
            total_value_bytes=total_bytes,
            next_after_observed_at=last.observed_at if last else "",
            next_after_assertion_id=last.assertion_id if last else "",
        )

    async def changes_since(
        self,
        ingest_position: int,
        *,
        through_position: int | None = None,
    ) -> list[WorldProjectionChange]:
        through = self._frontier if through_position is None else int(through_position)
        async with self._lock:
            return [
                self._changes[position]
                for position in sorted(self._changes)
                if int(ingest_position) < position <= through
            ]

    async def change_references_page(
        self,
        ingest_position: int,
        *,
        through_position: int,
        limit: int = 128,
        inline_max_bytes: int = 1024,
    ) -> WorldChangeReferencePage:
        values = await self.changes_since(
            ingest_position,
            through_position=through_position,
        )
        page_limit = max(1, min(int(limit), 1000))
        total_bytes = 0
        items: list[WorldChangeReference] = []
        for item in values:
            raw = json.dumps(
                item.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            total_bytes += len(raw.encode("utf-8"))
        for item in values[:page_limit]:
            raw = json.dumps(
                item.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            payload_bytes = len(raw.encode("utf-8"))
            inlined = payload_bytes <= int(inline_max_bytes)
            assertion = item.payload.get("assertion")
            assertion_value = (
                assertion.get("value") if isinstance(assertion, dict) else None
            )
            trace_payload = (
                assertion_value.get("payload")
                if isinstance(assertion_value, dict)
                else None
            )
            context = (
                trace_payload.get("context")
                if isinstance(trace_payload, dict)
                else None
            )
            transport_echo = bool(
                isinstance(assertion, dict)
                and assertion.get("domain") == "minecraft"
                and assertion.get("predicate") == "embodied_trace"
                and isinstance(assertion_value, dict)
                and assertion_value.get("trace_kind") == "intent.issued"
                and isinstance(context, dict)
                and "transient_world_perception" in context
            )
            items.append(
                WorldChangeReference(
                    ingest_position=item.ingest_position,
                    event_id=item.event_id,
                    occurrence_id=item.occurrence_id,
                    event_type=item.event_type,
                    change_kind=item.change_kind,
                    source_instance_id=item.source_instance_id,
                    stream_id=item.stream_id,
                    occurred_at=item.occurred_at,
                    recorded_at=item.recorded_at,
                    payload_bytes=payload_bytes,
                    payload_inlined=inlined,
                    payload=copy.deepcopy(item.payload) if inlined else {},
                    transport_echo=transport_echo,
                )
            )
        return WorldChangeReferencePage(
            items=tuple(items),
            total_items=len(values),
            total_payload_bytes=total_bytes,
            has_more=len(values) > page_limit,
        )

    async def read_assertion_value_chunk(
        self,
        assertion_id: str,
        *,
        offset_bytes: int = 0,
        max_bytes: int = 16 * 1024,
    ) -> WorldValueChunk:
        async with self._lock:
            item = self._assertions.get(str(assertion_id or ""))
            if item is None:
                raise KeyError(str(assertion_id or ""))
            raw = json.dumps(
                item.value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return WorldProjectionStore._value_chunk(
            raw,
            reference_kind="assertion_value",
            reference_id=item.assertion_id,
            offset_bytes=offset_bytes,
            max_bytes=max_bytes,
        )

    async def read_change_payload_chunk(
        self,
        ingest_position: int,
        *,
        offset_bytes: int = 0,
        max_bytes: int = 16 * 1024,
    ) -> WorldValueChunk:
        position = int(ingest_position)
        async with self._lock:
            item = self._changes.get(position)
            if item is None:
                raise KeyError(str(position))
            raw = json.dumps(
                item.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return WorldProjectionStore._value_chunk(
            raw,
            reference_kind="change_payload",
            reference_id=str(position),
            offset_bytes=offset_bytes,
            max_bytes=max_bytes,
        )

    async def perception_cursor(self, instance_id: str) -> tuple[int, int]:
        async with self._lock:
            return self._cursors.get(instance_id, (0, 0))

    async def commit_perception_cursor(
        self,
        instance_id: str,
        *,
        expected_position: int,
        expected_revision: int,
        through_position: int,
    ) -> tuple[int, int]:
        async with self._lock:
            if self._state != WORLD_REBUILD_IDLE:
                raise WorldProjectionUnavailable("world projection is unavailable")
            current = self._cursors.get(instance_id, (0, 0))
            if current != (expected_position, expected_revision):
                raise PerceptionCursorConflict("stale perception cursor")
            if through_position < expected_position:
                raise ValueError("perception cursor cannot move backwards")
            if through_position > self._frontier:
                raise ValueError("perception cursor cannot advance beyond frontier")
            if through_position == expected_position:
                return current
            committed = (through_position, expected_revision + 1)
            self._cursors[instance_id] = committed
            return committed

    async def health_snapshot(self) -> dict[str, Any]:
        contract = await self.projector_contract()
        async with self._lock:
            return {
                "backend": "fake",
                **contract,
                "assertion_count": len(self._assertions),
                "change_count": len(self._changes),
                "cursors": [
                    {
                        "instance_id": instance_id,
                        "position": position,
                        "revision": revision,
                        "lag": max(0, self._frontier - position),
                        "updated_at": "",
                    }
                    for instance_id, (position, revision) in sorted(
                        self._cursors.items()
                    )
                ],
            }


def build_fake_stores() -> PresenceWorldStores:
    """Return independent in-memory implementations of both domain Ports."""

    return PresenceWorldStores(
        presence=FakePresenceStore(),
        world=FakeWorldProjectionStore(),
    )
