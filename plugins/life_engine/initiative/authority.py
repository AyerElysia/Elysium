"""Event-sourced initiative authority over the service-owned runtime store."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from ..storage.runtime_contracts import RuntimeEventRecord, RuntimeStateStorePort
from .contracts import (
    InitiativeActorInactive,
    InitiativeConflict,
    InitiativeOutreachCommand,
    InitiativeOutreachReceipt,
    InitiativeReencounterReceipt,
    InitiativeSeedCommand,
    InitiativeSeedCommit,
    InitiativeSeedView,
    InitiativeTransitionError,
)

_SEED_EVENTS = "life_initiative.seed_decisions"
_OUTREACH_EVENTS = "life_initiative.outreach_decisions"
_REENCOUNTER_DELIVERIES = "life_initiative.reencounter_deliveries"
_PAGE_LIMIT = 500


def _reencounter_occurrence(seed_id: str, seed_revision: int) -> str:
    digest = hashlib.sha256(
        f"{seed_id}\0{int(seed_revision)}".encode()
    ).hexdigest()
    return f"initiative:reencounter:{digest}"


class InitiativeAuthority:
    """Append-only authority whose current views are always rebuildable."""

    def __init__(
        self,
        runtime_store: RuntimeStateStorePort,
        *,
        validate_active_actor: Callable[[str], Awaitable[bool]],
    ) -> None:
        self._runtime_store = runtime_store
        self._validate_active_actor = validate_active_actor
        self._lock = asyncio.Lock()
        self._seed_views: dict[str, InitiativeSeedView] = {}
        self._seed_occurrences: dict[str, RuntimeEventRecord] = {}
        self._seed_position = 0
        self._delivery_occurrences: dict[str, RuntimeEventRecord] = {}
        self._delivery_position = 0
        self._outreach_occurrences: dict[str, RuntimeEventRecord] = {}
        self._outreach_position = 0

    async def _events(
        self,
        namespace: str,
        *,
        after_position: int = 0,
    ) -> list[RuntimeEventRecord]:
        position = max(0, int(after_position))
        records: list[RuntimeEventRecord] = []
        while True:
            page = await self._runtime_store.read_events(
                namespace,
                after_position=position,
                limit=_PAGE_LIMIT,
            )
            if not page:
                return records
            records.extend(page)
            position = page[-1].position
            if len(page) < _PAGE_LIMIT:
                return records

    @staticmethod
    def _command(payload: dict[str, Any]) -> InitiativeSeedCommand:
        return InitiativeSeedCommand(
            occurrence_id=str(payload["occurrence_id"]),
            seed_id=str(payload["seed_id"]),
            action=str(payload["action"]),  # type: ignore[arg-type]
            actor_consciousness_instance_id=str(payload["actor"]),
            source_instance_id=str(payload["source_instance_id"]),
            source_occurrence_ids=tuple(payload.get("source_occurrence_ids") or ()),
            causation_occurrence_id=str(payload["causation_occurrence_id"]),
            expected_revision=int(payload["expected_revision"]),
            public_statement=str(payload.get("public_statement") or ""),
            related_entity_refs=tuple(payload.get("related_entity_refs") or ()),
            occurred_at=str(payload["occurred_at"]),
            reencounter_after_minutes=int(
                payload.get("reencounter_after_minutes") or 0
            ),
        )

    @staticmethod
    def _payload(command: InitiativeSeedCommand) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "command_sha256": command.canonical_sha256(),
            "occurrence_id": command.occurrence_id,
            "seed_id": command.seed_id,
            "action": command.action,
            "actor": command.actor_consciousness_instance_id,
            "source_instance_id": command.source_instance_id,
            "source_occurrence_ids": list(command.source_occurrence_ids),
            "causation_occurrence_id": command.causation_occurrence_id,
            "expected_revision": command.expected_revision,
            "public_statement": command.public_statement,
            "related_entity_refs": list(command.related_entity_refs),
            "occurred_at": command.occurred_at,
            "reencounter_after_minutes": command.reencounter_after_minutes,
        }

    @classmethod
    def _apply(
        cls,
        current: InitiativeSeedView | None,
        record: RuntimeEventRecord,
    ) -> InitiativeSeedView:
        command = cls._command(record.payload)
        if current is None:
            if command.action != "hold":
                raise InitiativeTransitionError(
                    "initiative history must begin with hold"
                )
            return InitiativeSeedView(
                seed_id=command.seed_id,
                status="open",
                revision=1,
                current_statement=command.public_statement,
                related_entity_refs=command.related_entity_refs,
                opened_at=command.occurred_at,
                last_changed_at=command.occurred_at,
                last_event_position=record.position,
                last_event_id=f"initiative:seed:event:{record.position}",
                last_occurrence_id=command.occurrence_id,
                content_event_id=f"initiative:seed:event:{record.position}",
                content_revision=1,
            )
        if current.status == "released":
            raise InitiativeTransitionError("released initiative is terminal")
        if command.expected_revision != current.revision:
            raise InitiativeConflict(
                "initiative history contains a stale revision",
                seed_id=command.seed_id,
                current_revision=current.revision,
            )
        changes: dict[str, Any] = {}
        if command.action == "rewrite":
            changes.update(
                current_statement=command.public_statement,
                related_entity_refs=command.related_entity_refs,
                content_event_id=f"initiative:seed:event:{record.position}",
                content_revision=command.revision,
            )
        elif command.action == "reencounter":
            changes.update(
                reencounter_at=command.reencounter_at(),
                reencounter_revision=command.revision,
                reencounter_event_id=f"initiative:seed:event:{record.position}",
                reencounter_delivered_at="",
                reencounter_delivery_event_id="",
            )
        elif command.action == "release":
            changes.update(
                status="released",
                current_statement=command.public_statement,
                related_entity_refs=command.related_entity_refs,
                content_event_id=f"initiative:seed:event:{record.position}",
                content_revision=command.revision,
                reencounter_at="",
                reencounter_revision=0,
                reencounter_event_id="",
                reencounter_delivered_at="",
                reencounter_delivery_event_id="",
            )
        else:
            raise InitiativeTransitionError("hold cannot mutate an existing seed")
        return replace(
            current,
            revision=command.revision,
            last_changed_at=command.occurred_at,
            last_event_position=record.position,
            last_event_id=f"initiative:seed:event:{record.position}",
            last_occurrence_id=command.occurrence_id,
            **changes,
        )

    async def _seed_state(
        self,
    ) -> tuple[dict[str, InitiativeSeedView], dict[str, RuntimeEventRecord]]:
        """Incrementally refresh the rebuildable seed projection."""

        for record in await self._events(
            _SEED_EVENTS,
            after_position=self._seed_position,
        ):
            command = self._command(record.payload)
            if record.payload.get("command_sha256") != command.canonical_sha256():
                raise InitiativeConflict(
                    "initiative event digest mismatch",
                    seed_id=command.seed_id,
                )
            if command.occurrence_id in self._seed_occurrences:
                raise InitiativeConflict("duplicate initiative occurrence in ledger")
            self._seed_occurrences[command.occurrence_id] = record
            self._seed_views[command.seed_id] = self._apply(
                self._seed_views.get(command.seed_id),
                record,
            )
            self._seed_position = record.position
        return self._seed_views, self._seed_occurrences

    async def decide_seed(
        self,
        command: InitiativeSeedCommand,
    ) -> InitiativeSeedCommit:
        async with self._lock:
            views, occurrences = await self._seed_state()
            replay = occurrences.get(command.occurrence_id)
            if replay is not None:
                if replay.payload.get("command_sha256") != command.canonical_sha256():
                    raise InitiativeConflict(
                        "initiative occurrence was reused with different content",
                        seed_id=command.seed_id,
                    )
                view = views[command.seed_id]
                return InitiativeSeedCommit(
                    event_id=f"initiative:seed:event:{replay.position}",
                    occurrence_id=command.occurrence_id,
                    seed_id=command.seed_id,
                    revision=command.revision,
                    status="released" if command.action == "release" else view.status,
                    idempotent_replay=True,
                )
            if not await self._validate_active_actor(
                command.actor_consciousness_instance_id
            ):
                raise InitiativeActorInactive("initiative actor is not active")
            current = views.get(command.seed_id)
            if command.action == "hold":
                if current is not None:
                    raise InitiativeConflict(
                        "initiative seed already exists",
                        seed_id=command.seed_id,
                        current_revision=current.revision,
                    )
            elif current is None:
                raise InitiativeConflict(
                    "initiative seed does not exist",
                    seed_id=command.seed_id,
                )
            elif command.expected_revision != current.revision:
                raise InitiativeConflict(
                    "initiative expected_revision is stale",
                    seed_id=command.seed_id,
                    current_revision=current.revision,
                )
            record = await self._runtime_store.append_event(
                namespace=_SEED_EVENTS,
                occurrence_id=command.occurrence_id,
                event_kind=f"seed_{command.action}",
                payload=self._payload(command),
                occurred_at=command.occurred_at,
            )
            view = self._apply(current, record)
            self._seed_occurrences[command.occurrence_id] = record
            self._seed_views[command.seed_id] = view
            self._seed_position = max(self._seed_position, record.position)
            return InitiativeSeedCommit(
                event_id=view.last_event_id,
                occurrence_id=command.occurrence_id,
                seed_id=command.seed_id,
                revision=view.revision,
                status=view.status,
                idempotent_replay=False,
            )

    async def _views_with_delivery(self) -> dict[str, InitiativeSeedView]:
        views, _ = await self._seed_state()
        for record in await self._events(
            _REENCOUNTER_DELIVERIES,
            after_position=self._delivery_position,
        ):
            payload = record.payload
            occurrence_id = str(payload.get("occurrence_id") or "").strip()
            seed_id = str(payload.get("seed_id") or "").strip()
            seed_revision = int(payload.get("seed_revision") or 0)
            life_event_id = str(payload.get("life_event_id") or "").strip()
            occurred_at = str(payload.get("occurred_at") or "").strip()
            expected_occurrence = _reencounter_occurrence(seed_id, seed_revision)
            if (
                not occurrence_id
                or occurrence_id != expected_occurrence
                or not seed_id
                or seed_revision <= 0
                or not life_event_id
                or not occurred_at
            ):
                raise InitiativeConflict("invalid initiative delivery event")
            parsed_at = datetime.fromisoformat(occurred_at)
            if parsed_at.tzinfo is None:
                raise InitiativeConflict("invalid initiative delivery timestamp")
            existing = self._delivery_occurrences.get(occurrence_id)
            if existing is not None:
                if existing.payload != payload:
                    raise InitiativeConflict(
                        "initiative delivery occurrence was reused"
                    )
                self._delivery_position = record.position
                continue
            self._delivery_occurrences[occurrence_id] = record
            view = views.get(seed_id)
            if view is None or view.reencounter_revision != seed_revision:
                self._delivery_position = record.position
                continue
            views[seed_id] = replace(
                view,
                reencounter_delivered_at=occurred_at,
                reencounter_delivery_event_id=(
                    f"initiative:reencounter:delivery:{record.position}"
                ),
            )
            self._delivery_position = record.position
        return views

    async def get_seed(self, seed_id: str) -> InitiativeSeedView | None:
        async with self._lock:
            views = await self._views_with_delivery()
            return views.get(str(seed_id or "").strip())

    async def list_seeds(
        self,
        *,
        include_released: bool = False,
    ) -> tuple[InitiativeSeedView, ...]:
        async with self._lock:
            views = await self._views_with_delivery()
            return self._ordered_views(
                views,
                include_released=include_released,
            )

    @staticmethod
    def _ordered_views(
        views: dict[str, InitiativeSeedView],
        *,
        include_released: bool,
    ) -> tuple[InitiativeSeedView, ...]:
        return tuple(
            sorted(
                (
                    view
                    for view in views.values()
                    if include_released or view.status != "released"
                ),
                key=lambda view: (view.last_event_position, view.seed_id),
            )
        )

    async def due_reencounters(
        self,
        *,
        now: str,
    ) -> tuple[InitiativeSeedView, ...]:
        """Return only due, undelivered one-shot projections in ledger order."""

        parsed_now = datetime.fromisoformat(str(now or "").strip())
        if parsed_now.tzinfo is None:
            raise ValueError("initiative reencounter clock must include a timezone")
        utc_now = parsed_now.astimezone(UTC)
        async with self._lock:
            views = await self._views_with_delivery()
            return tuple(
                view
                for view in self._ordered_views(
                    views,
                    include_released=False,
                )
                if view.reencounter_at
                and not view.reencounter_delivered_at
                and datetime.fromisoformat(view.reencounter_at).astimezone(UTC)
                <= utc_now
            )

    async def record_reencounter_delivery(
        self,
        *,
        seed_id: str,
        seed_revision: int,
        life_event_id: str,
        occurred_at: str,
    ) -> InitiativeReencounterReceipt:
        """Record technical delivery without changing subject-authored state."""

        identity = str(seed_id or "").strip()
        revision = int(seed_revision)
        event_identity = str(life_event_id or "").strip()
        delivered_at = str(occurred_at or "").strip()
        parsed_at = datetime.fromisoformat(delivered_at)
        if parsed_at.tzinfo is None:
            raise ValueError("initiative delivery time must include a timezone")
        delivered_at = parsed_at.astimezone(UTC).isoformat()
        if not identity or revision <= 0 or not event_identity:
            raise ValueError("initiative delivery identity is incomplete")
        occurrence_id = _reencounter_occurrence(identity, revision)
        payload = {
            "schema_version": 1,
            "occurrence_id": occurrence_id,
            "seed_id": identity,
            "seed_revision": revision,
            "life_event_id": event_identity,
            "occurred_at": delivered_at,
        }
        async with self._lock:
            views = await self._views_with_delivery()
            view = views.get(identity)
            if view is None or view.status != "open":
                raise InitiativeTransitionError(
                    "reencounter delivery references a missing or released seed"
                )
            if view.reencounter_revision != revision:
                raise InitiativeConflict(
                    "reencounter revision is stale",
                    seed_id=identity,
                    current_revision=view.revision,
                )
            existing = self._delivery_occurrences.get(occurrence_id)
            if existing is not None:
                if (
                    str(existing.payload.get("seed_id") or "") != identity
                    or int(existing.payload.get("seed_revision") or 0) != revision
                    or str(existing.payload.get("life_event_id") or "")
                    != event_identity
                ):
                    raise InitiativeConflict(
                        "reencounter occurrence was reused with different evidence",
                        seed_id=identity,
                        current_revision=view.revision,
                    )
                return InitiativeReencounterReceipt(
                    event_id=(
                        f"initiative:reencounter:delivery:{existing.position}"
                    ),
                    occurrence_id=occurrence_id,
                    seed_id=identity,
                    seed_revision=revision,
                    life_event_id=event_identity,
                    idempotent_replay=True,
                )
            record = await self._runtime_store.append_event(
                namespace=_REENCOUNTER_DELIVERIES,
                occurrence_id=occurrence_id,
                event_kind="reencounter_delivered",
                payload=payload,
                occurred_at=delivered_at,
            )
            self._delivery_occurrences[occurrence_id] = record
            self._delivery_position = max(self._delivery_position, record.position)
            self._seed_views[identity] = replace(
                view,
                reencounter_delivered_at=delivered_at,
                reencounter_delivery_event_id=(
                    f"initiative:reencounter:delivery:{record.position}"
                ),
            )
            return InitiativeReencounterReceipt(
                event_id=f"initiative:reencounter:delivery:{record.position}",
                occurrence_id=occurrence_id,
                seed_id=identity,
                seed_revision=revision,
                life_event_id=event_identity,
                idempotent_replay=False,
            )

    @staticmethod
    def _outreach_payload(command: InitiativeOutreachCommand) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "command_sha256": command.canonical_sha256(),
            "occurrence_id": command.occurrence_id,
            "actor": command.actor_consciousness_instance_id,
            "source_instance_id": command.source_instance_id,
            "source_occurrence_ids": list(command.source_occurrence_ids),
            "causation_occurrence_id": command.causation_occurrence_id,
            "audience_ref": command.audience_ref,
            "surface_ref": command.surface_ref,
            "public_intention": command.public_intention,
            "occurred_at": command.occurred_at,
            "seed_id": command.seed_id,
            "seed_revision": command.seed_revision,
        }

    @staticmethod
    def _outreach_command(payload: dict[str, Any]) -> InitiativeOutreachCommand:
        return InitiativeOutreachCommand(
            occurrence_id=str(payload["occurrence_id"]),
            actor_consciousness_instance_id=str(payload["actor"]),
            source_instance_id=str(payload["source_instance_id"]),
            source_occurrence_ids=tuple(payload.get("source_occurrence_ids") or ()),
            causation_occurrence_id=str(payload["causation_occurrence_id"]),
            audience_ref=str(payload["audience_ref"]),
            surface_ref=str(payload["surface_ref"]),
            public_intention=str(payload["public_intention"]),
            occurred_at=str(payload["occurred_at"]),
            seed_id=str(payload.get("seed_id") or ""),
            seed_revision=int(payload.get("seed_revision") or 0),
        )

    async def _outreach_state(self) -> dict[str, RuntimeEventRecord]:
        """Incrementally validate and index immutable outreach decisions."""

        for record in await self._events(
            _OUTREACH_EVENTS,
            after_position=self._outreach_position,
        ):
            command = self._outreach_command(record.payload)
            if record.payload.get("command_sha256") != command.canonical_sha256():
                raise InitiativeConflict(
                    "outreach event digest mismatch",
                    seed_id=command.seed_id,
                )
            existing = self._outreach_occurrences.get(command.occurrence_id)
            if existing is not None:
                if existing.payload != record.payload:
                    raise InitiativeConflict(
                        "duplicate outreach occurrence in ledger"
                    )
            else:
                self._outreach_occurrences[command.occurrence_id] = record
            self._outreach_position = record.position
        return self._outreach_occurrences

    async def begin_outreach(
        self,
        command: InitiativeOutreachCommand,
    ) -> InitiativeOutreachReceipt:
        async with self._lock:
            existing = (await self._outreach_state()).get(command.occurrence_id)
            if existing is not None:
                if existing.payload.get("command_sha256") != command.canonical_sha256():
                    raise InitiativeConflict(
                        "outreach occurrence was reused with different content"
                    )
                return InitiativeOutreachReceipt(
                    event_id=f"initiative:outreach:event:{existing.position}",
                    occurrence_id=command.occurrence_id,
                    audience_ref=command.audience_ref,
                    surface_ref=command.surface_ref,
                    idempotent_replay=True,
                )
            if not await self._validate_active_actor(
                command.actor_consciousness_instance_id
            ):
                raise InitiativeActorInactive("outreach actor is not active")
            if command.seed_id:
                views = await self._views_with_delivery()
                seed = views.get(command.seed_id)
                if seed is None or seed.status != "open":
                    raise InitiativeTransitionError(
                        "outreach references a missing or released seed"
                    )
                if seed.revision != command.seed_revision:
                    raise InitiativeConflict(
                        "outreach seed_revision is stale",
                        seed_id=seed.seed_id,
                        current_revision=seed.revision,
                    )
            record = await self._runtime_store.append_event(
                namespace=_OUTREACH_EVENTS,
                occurrence_id=command.occurrence_id,
                event_kind="outreach_begun",
                payload=self._outreach_payload(command),
                occurred_at=command.occurred_at,
            )
            self._outreach_occurrences[command.occurrence_id] = record
            self._outreach_position = max(self._outreach_position, record.position)
            return InitiativeOutreachReceipt(
                event_id=f"initiative:outreach:event:{record.position}",
                occurrence_id=command.occurrence_id,
                audience_ref=command.audience_ref,
                surface_ref=command.surface_ref,
                idempotent_replay=False,
            )


__all__ = ["InitiativeAuthority"]
