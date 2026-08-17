"""Subject initiative authority contracts."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.initiative.authority import InitiativeAuthority
from plugins.life_engine.initiative.contracts import (
    InitiativeActorInactive,
    InitiativeConflict,
    InitiativeOutreachCommand,
    InitiativeSeedCommand,
    InitiativeTransitionError,
)
from plugins.life_engine.initiative.tools import (
    _decision_occurrence,
    _occurred_at,
    _service_actor,
    _source_instance,
    _source_occurrence,
)
from plugins.life_engine.storage.runtime_contracts import (
    RuntimeEventConflict,
    RuntimeEventRecord,
)


class _EventStore:
    def __init__(self) -> None:
        self.events: dict[str, list[RuntimeEventRecord]] = defaultdict(list)
        self.read_calls: list[tuple[str, int]] = []

    async def append_event(self, **kwargs: Any) -> RuntimeEventRecord:
        rows = self.events[kwargs["namespace"]]
        for row in rows:
            if row.occurrence_id != kwargs["occurrence_id"]:
                continue
            if row.payload != kwargs["payload"]:
                raise RuntimeEventConflict("occurrence conflict")
            return row
        record = RuntimeEventRecord(
            position=len(rows) + 1,
            namespace=kwargs["namespace"],
            occurrence_id=kwargs["occurrence_id"],
            event_kind=kwargs["event_kind"],
            payload=kwargs["payload"],
            payload_sha256="0" * 64,
            occurred_at=kwargs["occurred_at"],
            recorded_at=kwargs["occurred_at"],
        )
        rows.append(record)
        return record

    async def read_events(
        self,
        namespace: str,
        *,
        after_position: int = 0,
        limit: int = 100,
    ) -> list[RuntimeEventRecord]:
        self.read_calls.append((namespace, after_position))
        return [
            row
            for row in self.events.get(namespace, ())
            if row.position > after_position
        ][:limit]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _seed(
    *,
    action: str = "hold",
    revision: int = 0,
    occurrence: str = "decision-1",
    statement: str = "我想以后再认真看看这件事。",
    minutes: int = 0,
) -> InitiativeSeedCommand:
    return InitiativeSeedCommand(
        occurrence_id=occurrence,
        seed_id="initiative:seed:one",
        action=action,  # type: ignore[arg-type]
        actor_consciousness_instance_id="chat:active",
        source_instance_id="kook:scene",
        source_occurrence_ids=("message:kook:1",),
        causation_occurrence_id="message:kook:1",
        expected_revision=revision,
        public_statement=statement,
        related_entity_refs=(
            () if action == "reencounter" else ("person:xiaoxi",)
        ),
        occurred_at=_now(),
        reencounter_after_minutes=minutes,
    )


@pytest.mark.asyncio
async def test_seed_is_subject_level_and_rebuildable_without_route_fields() -> None:
    store = _EventStore()
    authority = InitiativeAuthority(
        store,  # type: ignore[arg-type]
        validate_active_actor=lambda _actor: _truthy(),
    )

    commit = await authority.decide_seed(_seed())
    view = await authority.get_seed(commit.seed_id)

    assert view is not None
    assert view.current_statement == "我想以后再认真看看这件事。"
    assert view.content_event_id == "initiative:seed:event:1"
    assert view.content_revision == 1
    payload = store.events["life_initiative.seed_decisions"][0].payload
    forbidden = {
        "platform",
        "stream_id",
        "target_stream_id",
        "target_key",
        "reply",
        "score",
        "priority",
        "repeat",
    }
    assert forbidden.isdisjoint(payload)


async def _truthy() -> bool:
    return True


async def _falsey() -> bool:
    return False


def test_tool_source_instance_does_not_collapse_into_actor() -> None:
    tool = SimpleNamespace(
        trigger_message=SimpleNamespace(
            extra={"source_instance_id": "kook:scene"}
        )
    )
    assert _source_instance(tool, "chat:active") == "kook:scene"  # type: ignore[arg-type]


def test_unknown_surface_cannot_borrow_chat_global_actor(monkeypatch) -> None:
    chat_global = SimpleNamespace(instance_id="chat_global", is_active=True)
    registry = SimpleNamespace(
        get_for_stream=lambda _stream_id: None,
        get=lambda instance_id: chat_global if instance_id == "chat_global" else None,
    )
    service = SimpleNamespace(consciousness_registry=registry)
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: service,
    )
    surface_tool = SimpleNamespace(
        get_current_stream_id=lambda: "stale-voice-stream",
        _runtime_task_name="life_chatter",
    )
    with pytest.raises(PermissionError, match="InitiativeActorIsNotActive"):
        _service_actor(surface_tool)  # type: ignore[arg-type]

    heartbeat_tool = SimpleNamespace(
        get_current_stream_id=lambda: "chat_global",
        _runtime_task_name="core",
    )
    _, actor = _service_actor(heartbeat_tool)  # type: ignore[arg-type]
    assert actor == "chat_global"


def test_tool_decision_identity_is_stable_and_requires_bound_evidence() -> None:
    tool = SimpleNamespace(
        trigger_message=None,
        _tool_call_id="call-7",
        _life_source_occurrence_id="life-event:44",
        _life_source_occurred_at="2026-08-17T08:00:00+00:00",
        get_current_stream_id=lambda: "chat_global",
    )
    assert _source_occurrence(tool) == "life-event:44"  # type: ignore[arg-type]
    first = _decision_occurrence(tool, "same-material")  # type: ignore[arg-type]
    second = _decision_occurrence(tool, "same-material")  # type: ignore[arg-type]
    assert first == second
    assert _occurred_at(tool) == "2026-08-17T08:00:00+00:00"  # type: ignore[arg-type]

    unbound = SimpleNamespace(
        trigger_message=None,
        _tool_call_id="",
        get_current_stream_id=lambda: "chat_global",
    )
    with pytest.raises(RuntimeError, match="InitiativeSourceOccurrenceRequired"):
        _source_occurrence(unbound)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="InitiativeToolCallIdentityRequired"):
        _decision_occurrence(unbound, "material")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="InitiativeSourceTimeRequired"):
        _occurred_at(unbound)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_seed_decisions_require_active_actor_and_exact_revision() -> None:
    store = _EventStore()
    rejected = InitiativeAuthority(
        store,  # type: ignore[arg-type]
        validate_active_actor=lambda _actor: _falsey(),
    )
    with pytest.raises(InitiativeActorInactive):
        await rejected.decide_seed(_seed())
    assert not store.events

    authority = InitiativeAuthority(
        store,  # type: ignore[arg-type]
        validate_active_actor=lambda _actor: _truthy(),
    )
    await authority.decide_seed(_seed())
    with pytest.raises(InitiativeConflict):
        await authority.decide_seed(
            _seed(action="rewrite", revision=9, occurrence="decision-stale")
        )


@pytest.mark.asyncio
async def test_seed_occurrence_replay_is_idempotent_but_payload_reuse_fails() -> None:
    store = _EventStore()
    authority = InitiativeAuthority(
        store,  # type: ignore[arg-type]
        validate_active_actor=lambda _actor: _truthy(),
    )
    command = _seed()
    first = await authority.decide_seed(command)
    second = await authority.decide_seed(command)
    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert len(store.events["life_initiative.seed_decisions"]) == 1

    with pytest.raises(InitiativeConflict):
        await authority.decide_seed(
            _seed(occurrence="decision-1", statement="不同的主体决定")
        )


@pytest.mark.asyncio
async def test_committed_seed_replay_survives_actor_lease_expiry() -> None:
    store = _EventStore()
    actor_state = {"active": True}

    async def active(_actor: str) -> bool:
        return actor_state["active"]

    authority = InitiativeAuthority(
        store,  # type: ignore[arg-type]
        validate_active_actor=active,
    )
    command = _seed()
    await authority.decide_seed(command)
    actor_state["active"] = False

    assert (await authority.decide_seed(command)).idempotent_replay is True
    with pytest.raises(InitiativeActorInactive):
        await authority.decide_seed(
            _seed(occurrence="decision-after-expiry", statement="新的决定")
        )


@pytest.mark.asyncio
async def test_reencounter_is_one_subject_chosen_occurrence_not_recurrence() -> None:
    store = _EventStore()
    authority = InitiativeAuthority(
        store,  # type: ignore[arg-type]
        validate_active_actor=lambda _actor: _truthy(),
    )
    await authority.decide_seed(_seed())
    result = await authority.decide_seed(
        _seed(
            action="reencounter",
            revision=1,
            occurrence="decision-2",
            statement="",
            minutes=45,
        )
    )
    view = await authority.get_seed(result.seed_id)
    assert view is not None
    assert view.reencounter_revision == 2
    payload = store.events["life_initiative.seed_decisions"][1].payload
    assert "repeat" not in payload
    assert "interval_minutes" not in payload
    assert "max_occurrences" not in payload


def test_reencounter_cannot_smuggle_new_statement_or_entity_binding() -> None:
    with pytest.raises(ValueError, match="new subject statement"):
        _seed(
            action="reencounter",
            revision=1,
            statement="这是新的意义",
            minutes=10,
        )
    command = _seed(
        action="reencounter",
        revision=1,
        statement="",
        minutes=10,
    )
    with pytest.raises(ValueError, match="related entity refs"):
        InitiativeSeedCommand(
            occurrence_id=command.occurrence_id,
            seed_id=command.seed_id,
            action=command.action,
            actor_consciousness_instance_id=(
                command.actor_consciousness_instance_id
            ),
            source_instance_id=command.source_instance_id,
            source_occurrence_ids=command.source_occurrence_ids,
            causation_occurrence_id=command.causation_occurrence_id,
            expected_revision=command.expected_revision,
            public_statement="",
            related_entity_refs=("person:new",),
            occurred_at=command.occurred_at,
            reencounter_after_minutes=10,
        )


@pytest.mark.asyncio
async def test_reencounter_delivery_is_content_free_one_shot_evidence() -> None:
    store = _EventStore()
    authority = InitiativeAuthority(
        store,  # type: ignore[arg-type]
        validate_active_actor=lambda _actor: _truthy(),
    )
    await authority.decide_seed(_seed())
    await authority.decide_seed(
        _seed(
            action="reencounter",
            revision=1,
            occurrence="decision-2",
            statement="",
            minutes=1,
        )
    )
    due_at = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
    due = await authority.due_reencounters(now=due_at)
    assert [item.seed_id for item in due] == ["initiative:seed:one"]

    first = await authority.record_reencounter_delivery(
        seed_id="initiative:seed:one",
        seed_revision=2,
        life_event_id="initiative_reencounter_event",
        occurred_at=due_at,
    )
    replay = await authority.record_reencounter_delivery(
        seed_id="initiative:seed:one",
        seed_revision=2,
        life_event_id="initiative_reencounter_event",
        occurred_at=(datetime.now(UTC) + timedelta(minutes=3)).isoformat(),
    )
    assert first.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert await authority.due_reencounters(now=due_at) == ()
    payload = store.events["life_initiative.reencounter_deliveries"][0].payload
    assert "public_statement" not in payload
    assert "related_entity_refs" not in payload
    assert "platform" not in payload
    assert "stream_id" not in payload
    delivery_reads = [
        after
        for namespace, after in store.read_calls
        if namespace == "life_initiative.reencounter_deliveries"
    ]
    # Empty pre-append polls cannot advance an opaque event frontier; once the
    # delivery exists, replay continues strictly after its position.
    assert delivery_reads.count(0) == 2
    assert delivery_reads[-1] == 1


@pytest.mark.asyncio
async def test_due_polling_advances_projection_frontiers_instead_of_replaying_all() -> None:
    store = _EventStore()
    authority = InitiativeAuthority(
        store,  # type: ignore[arg-type]
        validate_active_actor=lambda _actor: _truthy(),
    )
    await authority.decide_seed(_seed())
    await authority.decide_seed(
        _seed(
            action="reencounter",
            revision=1,
            occurrence="decision-2",
            statement="",
            minutes=60,
        )
    )
    future = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    assert await authority.due_reencounters(now=future) == ()
    seed_reads_before = [
        after
        for namespace, after in store.read_calls
        if namespace == "life_initiative.seed_decisions"
    ]
    assert seed_reads_before[-1] == 2

    assert await authority.due_reencounters(now=future) == ()
    seed_reads_after = [
        after
        for namespace, after in store.read_calls
        if namespace == "life_initiative.seed_decisions"
    ]
    assert seed_reads_after[-1] == 2
    assert seed_reads_after.count(0) == 1


@pytest.mark.asyncio
async def test_release_is_terminal_and_list_order_is_event_order() -> None:
    store = _EventStore()
    authority = InitiativeAuthority(
        store,  # type: ignore[arg-type]
        validate_active_actor=lambda _actor: _truthy(),
    )
    await authority.decide_seed(_seed())
    await authority.decide_seed(
        _seed(
            action="release",
            revision=1,
            occurrence="decision-2",
            statement="我决定把它放下。",
        )
    )
    assert await authority.list_seeds() == ()
    assert len(await authority.list_seeds(include_released=True)) == 1
    with pytest.raises(InitiativeTransitionError):
        await authority.decide_seed(
            _seed(action="rewrite", revision=2, occurrence="decision-3")
        )


@pytest.mark.asyncio
async def test_outreach_separates_audience_surface_and_source_instance() -> None:
    store = _EventStore()
    authority = InitiativeAuthority(
        store,  # type: ignore[arg-type]
        validate_active_actor=lambda _actor: _truthy(),
    )
    command = InitiativeOutreachCommand(
        occurrence_id="outreach-1",
        actor_consciousness_instance_id="chat:active",
        source_instance_id="kook:scene",
        source_occurrence_ids=("message:kook:1",),
        causation_occurrence_id="message:kook:1",
        audience_ref="person:xiaoxi",
        surface_ref="surface:qq-private",
        public_intention="我现在想去问候小希。",
        occurred_at=_now(),
    )
    receipt = await authority.begin_outreach(command)
    assert receipt.audience_ref == "person:xiaoxi"
    assert receipt.surface_ref == "surface:qq-private"
    payload = store.events["life_initiative.outreach_decisions"][0].payload
    assert payload["source_instance_id"] == "kook:scene"
    assert "stream_id" not in payload
    assert (await authority.begin_outreach(command)).idempotent_replay is True
    outreach_reads = [
        after
        for namespace, after in store.read_calls
        if namespace == "life_initiative.outreach_decisions"
    ]
    assert outreach_reads.count(0) == 1
    assert outreach_reads[-1] == 1


@pytest.mark.asyncio
async def test_committed_outreach_replay_survives_actor_lease_expiry() -> None:
    store = _EventStore()
    actor_state = {"active": True}

    async def active(_actor: str) -> bool:
        return actor_state["active"]

    authority = InitiativeAuthority(
        store,  # type: ignore[arg-type]
        validate_active_actor=active,
    )
    command = InitiativeOutreachCommand(
        occurrence_id="outreach-replay",
        actor_consciousness_instance_id="chat:active",
        source_instance_id="kook:scene",
        source_occurrence_ids=("message:kook:1",),
        causation_occurrence_id="message:kook:1",
        audience_ref="person:xiaoxi",
        surface_ref="surface:qq-private",
        public_intention="我现在想去问候小希。",
        occurred_at=_now(),
    )
    await authority.begin_outreach(command)
    actor_state["active"] = False

    assert (await authority.begin_outreach(command)).idempotent_replay is True
    with pytest.raises(InitiativeActorInactive):
        await authority.begin_outreach(
            InitiativeOutreachCommand(
                occurrence_id="outreach-after-expiry",
                actor_consciousness_instance_id="chat:active",
                source_instance_id="kook:scene",
                source_occurrence_ids=("message:kook:1",),
                causation_occurrence_id="message:kook:1",
                audience_ref="person:xiaoxi",
                surface_ref="surface:qq-private",
                public_intention="这是新的外联决定。",
                occurred_at=_now(),
            )
        )
