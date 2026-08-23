"""End-to-end contracts for the one local proactive authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from plugins.life_engine.attention_threads import (
    AttentionThreadCommand,
    AttentionThreadConflict,
)
from plugins.life_engine.initiative import (
    InitiativeConflict,
    InitiativeOutreachCommand,
    InitiativeSeedCommand,
    InitiativeTransitionError,
)
from plugins.life_engine.initiative.reducer import seed_command_payload
from plugins.life_engine.proactive.actor_gate import ProactiveActorDecisionGate
from plugins.life_engine.proactive.backend_binding import (
    ProactiveBackendBindingConflict,
    ensure_proactive_backend_binding,
)
from plugins.life_engine.proactive.runtime import open_local_proactive_runtime
from plugins.life_engine.storage.attention_adapters import SQLAttentionThreadStore
from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.initiative_adapters import SQLInitiativeRecordStore
from plugins.life_engine.storage.models import BackendKind
from plugins.life_engine.storage.proactive_decision_guard import (
    ProactiveDecisionGuardConflict,
)


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        local_database_path="runtime/proactive/proactive.sqlite3",
        local_authority_state_path="runtime/proactive/authority.json",
        authority_lease_seconds=30,
        authority_renew_interval_seconds=5,
    )


async def _active_actor(instance_id: str) -> bool:
    return instance_id == "chat_global"


async def _inactive_actor(_instance_id: str) -> bool:
    return False


def _attention() -> AttentionThreadCommand:
    return AttentionThreadCommand(
        occurrence_id="proactive:test:attention:open",
        thread_id="attention:thread:proactive-runtime",
        action="open",
        actor_consciousness_instance_id="chat_global",
        source_instance_id="chat_global",
        source_occurrence_ids=("life:event:proactive-runtime",),
        causation_occurrence_id="life:event:proactive-runtime",
        expected_revision=0,
        public_statement="我选择把这条关注留给未来的自己。",
        occurred_at="2026-08-23T10:00:00+00:00",
    )


def _initiative() -> InitiativeSeedCommand:
    return InitiativeSeedCommand(
        occurrence_id="proactive:test:initiative:hold",
        seed_id="initiative:seed:proactive-runtime",
        action="hold",
        actor_consciousness_instance_id="chat_global",
        source_instance_id="chat_global",
        source_occurrence_ids=("life:event:proactive-runtime",),
        causation_occurrence_id="life:event:proactive-runtime",
        expected_revision=0,
        public_statement="我也许会在以后主动回来看看这件事。",
        related_entity_refs=(),
        occurred_at="2026-08-23T10:00:01+00:00",
        reencounter_after_minutes=0,
    )


def _initiative_change(
    *,
    action: str,
    expected_revision: int,
    occurrence_id: str,
    statement: str = "",
    reencounter_after_minutes: int = 0,
) -> InitiativeSeedCommand:
    return InitiativeSeedCommand(
        occurrence_id=occurrence_id,
        seed_id="initiative:seed:proactive-runtime",
        action=action,  # type: ignore[arg-type]
        actor_consciousness_instance_id="chat_global",
        source_instance_id="chat_global",
        source_occurrence_ids=("life:event:proactive-runtime",),
        causation_occurrence_id="life:event:proactive-runtime",
        expected_revision=expected_revision,
        public_statement=statement,
        related_entity_refs=(),
        occurred_at="2026-08-23T10:01:00+00:00",
        reencounter_after_minutes=reencounter_after_minutes,
    )


@pytest.mark.asyncio
async def test_local_proactive_authority_restarts_without_legacy_migration(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "thoughts" / "streams.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        '{"schema_version":2,"global_revision":7,"streams":[]}',
        encoding="utf-8",
    )
    legacy_digest = hashlib.sha256(legacy_path.read_bytes()).hexdigest()

    first = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    attention_commit = await first.authority.decide_attention(_attention())
    initiative_commit = await first.authority.decide_initiative(_initiative())
    health = await first.health_snapshot()

    assert attention_commit.revision == 1
    assert initiative_commit.revision == 1
    assert health["status"] == "healthy"
    assert health["authority"]["authority_count"] == 1
    assert tuple(health["authority"]["record_families"]) == (
        "attention",
        "initiative",
    )
    assert health["authority"]["initiative"]["open_count"] == 1

    with pytest.raises(RuntimeError, match="AlreadyOwned"):
        await open_local_proactive_runtime(
            workspace_path=tmp_path,
            config=_config(),
            validate_active_actor=_active_actor,
        )

    await first.close()
    await first.close()

    restarted = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_inactive_actor,
    )
    try:
        attention = await restarted.authority.get_attention(
            "attention:thread:proactive-runtime"
        )
        initiative = await restarted.authority.get_initiative(
            "initiative:seed:proactive-runtime"
        )
        assert attention is not None and attention.revision == 1
        assert initiative is not None and initiative.revision == 1
        assert (
            await restarted.authority.decide_attention(_attention())
        ).idempotent_replay
        assert (
            await restarted.authority.decide_initiative(_initiative())
        ).idempotent_replay
    finally:
        await restarted.close()

    assert hashlib.sha256(legacy_path.read_bytes()).hexdigest() == legacy_digest
    assert (tmp_path / "runtime" / "proactive" / "proactive.sqlite3").exists()


@pytest.mark.asyncio
async def test_local_proactive_authority_rejects_inactive_new_decisions(
    tmp_path: Path,
) -> None:
    runtime = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_inactive_actor,
    )
    try:
        with pytest.raises(RuntimeError, match="chat_global"):
            await runtime.authority.decide_attention(_attention())
        with pytest.raises(RuntimeError, match="not active"):
            await runtime.authority.decide_initiative(_initiative())
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("record_family", ("attention", "initiative", "outreach"))
async def test_local_actor_deactivation_linearizes_with_proactive_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_family: str,
) -> None:
    gate = ProactiveActorDecisionGate()
    actor = {"active": True}

    async def validate(_instance_id: str) -> bool:
        return actor["active"]

    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=validate,
        actor_decision_guard=gate.hold,
    )
    entered_write = asyncio.Event()
    release_write = asyncio.Event()

    if record_family == "attention":
        store: object = SQLAttentionThreadStore(
            owned.runtime,
            validate_active_actor=validate,
            actor_decision_guard=gate.hold,
        )
        method_name = "_insert_event"
        decision = store.decide(  # type: ignore[attr-defined]
            replace(
                _attention(),
                occurrence_id="proactive:test:gate:attention",
                thread_id="attention:thread:gate",
            )
        )
    else:
        store = SQLInitiativeRecordStore(
            owned.runtime,
            validate_active_actor=validate,
            actor_decision_guard=gate.hold,
        )
        method_name = "_append_event"
        if record_family == "initiative":
            decision = store.decide_seed(  # type: ignore[attr-defined]
                replace(
                    _initiative(),
                    occurrence_id="proactive:test:gate:initiative",
                    seed_id="initiative:seed:gate",
                )
            )
        else:
            decision = store.begin_outreach(  # type: ignore[attr-defined]
                InitiativeOutreachCommand(
                    occurrence_id="proactive:test:gate:outreach",
                    actor_consciousness_instance_id="chat_global",
                    source_instance_id="chat_global",
                    source_occurrence_ids=("life:event:gate",),
                    causation_occurrence_id="life:event:gate",
                    audience_ref="person:test",
                    surface_ref="surface:test",
                    public_intention="我明确选择发起这次测试外联。",
                    occurred_at="2026-08-23T10:05:00+00:00",
                )
            )

    original_write = getattr(store, method_name)

    async def blocked_write(*args: object, **kwargs: object) -> object:
        entered_write.set()
        await release_write.wait()
        return await original_write(*args, **kwargs)

    monkeypatch.setattr(store, method_name, blocked_write)
    decision_task = asyncio.create_task(decision)
    await asyncio.wait_for(entered_write.wait(), timeout=1.0)

    async def deactivate() -> None:
        async with gate.hold("chat_global"):
            actor["active"] = False

    deactivate_task = asyncio.create_task(deactivate())
    await asyncio.sleep(0)
    assert deactivate_task.done() is False

    release_write.set()
    await asyncio.wait_for(decision_task, timeout=1.0)
    await asyncio.wait_for(deactivate_task, timeout=1.0)
    assert actor["active"] is False
    await owned.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("record_family", ("attention", "initiative", "outreach"))
async def test_local_actor_deactivation_wins_before_new_proactive_commit(
    tmp_path: Path,
    record_family: str,
) -> None:
    """A completed suspension boundary leaves no event, guard, or head behind."""

    gate = ProactiveActorDecisionGate()
    actor = {"active": True}

    async def validate(_instance_id: str) -> bool:
        return actor["active"]

    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=validate,
        actor_decision_guard=gate.hold,
    )

    async def proactive_counts() -> tuple[int, int, int]:
        async with owned.runtime.unit_of_work() as uow:
            return (
                int(
                    await uow.session.scalar(
                        text("SELECT COUNT(*) FROM attention_thread_events")
                    )
                    or 0
                ),
                int(
                    await uow.session.scalar(
                        text(
                            """SELECT COUNT(*) FROM runtime_events
                            WHERE namespace LIKE 'life_initiative.%'
                               OR namespace = 'life_proactive.decision_guards'"""
                        )
                    )
                    or 0
                ),
                int(
                    await uow.session.scalar(
                        text(
                            """SELECT COUNT(*) FROM runtime_states
                            WHERE namespace LIKE 'life_initiative.%'"""
                        )
                    )
                    or 0
                ),
            )

    baseline_counts = await proactive_counts()
    deactivation_entered = asyncio.Event()
    release_deactivation = asyncio.Event()

    async def deactivate() -> None:
        async with gate.hold("chat_global"):
            actor["active"] = False
            deactivation_entered.set()
            await release_deactivation.wait()

    deactivation = asyncio.create_task(deactivate())
    await asyncio.wait_for(deactivation_entered.wait(), timeout=1.0)

    if record_family == "attention":
        decision = owned.authority.decide_attention(_attention())
    elif record_family == "initiative":
        decision = owned.authority.decide_initiative(_initiative())
    else:
        decision = owned.authority.begin_outreach(
            InitiativeOutreachCommand(
                occurrence_id="proactive:test:gate:outreach:inactive",
                actor_consciousness_instance_id="chat_global",
                source_instance_id="chat_global",
                source_occurrence_ids=("life:event:gate",),
                causation_occurrence_id="life:event:gate",
                audience_ref="person:test",
                surface_ref="surface:test",
                public_intention="我明确选择发起这次测试外联。",
                occurred_at="2026-08-23T10:05:00+00:00",
            )
        )
    decision_task = asyncio.create_task(decision)
    await asyncio.sleep(0)
    assert decision_task.done() is False

    release_deactivation.set()
    await asyncio.wait_for(deactivation, timeout=1.0)
    with pytest.raises(RuntimeError, match="active|chat_global"):
        await asyncio.wait_for(decision_task, timeout=1.0)

    assert await proactive_counts() == baseline_counts
    await owned.close()


@pytest.mark.asyncio
async def test_cancelled_decision_waiting_for_actor_gate_never_writes(
    tmp_path: Path,
) -> None:
    gate = ProactiveActorDecisionGate()
    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
        actor_decision_guard=gate.hold,
    )

    async with gate.hold("chat_global"):
        decision = asyncio.create_task(
            owned.authority.decide_attention(_attention())
        )
        await asyncio.sleep(0)
        assert decision.done() is False
        decision.cancel()
        with pytest.raises(asyncio.CancelledError):
            await decision

    async with owned.runtime.unit_of_work() as uow:
        assert int(
            await uow.session.scalar(text("SELECT COUNT(*) FROM attention_thread_events"))
            or 0
        ) == 0
        assert int(
            await uow.session.scalar(
                text(
                    """SELECT COUNT(*) FROM runtime_events
                    WHERE namespace = 'life_proactive.decision_guards'"""
                )
            )
            or 0
        ) == 0
    await owned.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field_name",
    (
        "local_database_path",
        "local_authority_state_path",
        "backend_binding_path",
    ),
)
async def test_local_proactive_paths_must_stay_inside_workspace(
    tmp_path: Path,
    field_name: str,
) -> None:
    config = _config()
    setattr(config, field_name, str(tmp_path.parent / "escaped-proactive-state"))

    with pytest.raises(ValueError, match="must stay inside"):
        await open_local_proactive_runtime(
            workspace_path=tmp_path,
            config=config,
            validate_active_actor=_active_actor,
        )


@pytest.mark.asyncio
async def test_initiative_revision_cas_is_atomic_across_authority_instances(
    tmp_path: Path,
) -> None:
    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    await owned.authority.decide_initiative(_initiative())
    first = SQLInitiativeRecordStore(
        owned.runtime,
        validate_active_actor=_active_actor,
        actor_decision_guard=owned.actor_decision_guard,
    )
    second = SQLInitiativeRecordStore(
        owned.runtime,
        validate_active_actor=_active_actor,
        actor_decision_guard=owned.actor_decision_guard,
    )

    def rewrite(occurrence: str, statement: str) -> InitiativeSeedCommand:
        return InitiativeSeedCommand(
            occurrence_id=occurrence,
            seed_id="initiative:seed:proactive-runtime",
            action="rewrite",
            actor_consciousness_instance_id="chat_global",
            source_instance_id="chat_global",
            source_occurrence_ids=("life:event:proactive-race",),
            causation_occurrence_id="life:event:proactive-race",
            expected_revision=1,
            public_statement=statement,
            related_entity_refs=(),
            occurred_at="2026-08-23T10:01:00+00:00",
        )

    results = await asyncio.gather(
        first.decide_seed(rewrite("proactive:race:a", "我选择版本 A。")),
        second.decide_seed(rewrite("proactive:race:b", "我选择版本 B。")),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    assert sum(isinstance(item, InitiativeConflict) for item in results) == 1
    await first.reconcile()
    view = await first.get_seed("initiative:seed:proactive-runtime")
    assert view is not None and view.revision == 2
    await owned.close()

    restarted = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_inactive_actor,
    )
    try:
        restored = await restarted.authority.get_initiative(
            "initiative:seed:proactive-runtime"
        )
        assert restored is not None and restored.revision == 2
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_unified_authority_owns_full_initiative_lifecycle(
    tmp_path: Path,
) -> None:
    payload = seed_command_payload(_initiative())
    assert {
        "platform",
        "stream_id",
        "target_stream_id",
        "target_key",
        "reply",
        "score",
        "priority",
        "repeat",
    }.isdisjoint(payload)

    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    try:
        held = await owned.authority.decide_initiative(_initiative())
        assert held.revision == 1
        assert (
            await owned.authority.decide_initiative(_initiative())
        ).idempotent_replay
        with pytest.raises(InitiativeConflict):
            await owned.authority.decide_initiative(
                replace(
                    _initiative(),
                    public_statement="相同 occurrence 不得偷换含义。",
                )
            )

        scheduled = await owned.authority.decide_initiative(
            _initiative_change(
                action="reencounter",
                expected_revision=1,
                occurrence_id="proactive:test:initiative:reencounter",
                reencounter_after_minutes=10,
            )
        )
        assert scheduled.revision == 2
        assert await owned.authority.due_reencounters(
            now="2026-08-23T10:10:59+00:00"
        ) == ()
        due = await owned.authority.due_reencounters(
            now="2026-08-23T10:11:01+00:00"
        )
        assert [item.seed_id for item in due] == [
            "initiative:seed:proactive-runtime"
        ]

        delivery = await owned.authority.record_reencounter_delivery(
            seed_id="initiative:seed:proactive-runtime",
            seed_revision=2,
            life_event_id="life:event:initiative:delivery",
            occurred_at="2026-08-23T10:12:00+00:00",
        )
        assert delivery.idempotent_replay is False
        assert (
            await owned.authority.record_reencounter_delivery(
                seed_id="initiative:seed:proactive-runtime",
                seed_revision=2,
                life_event_id="life:event:initiative:delivery",
                occurred_at="2026-08-23T10:13:00+00:00",
            )
        ).idempotent_replay
        assert await owned.authority.due_reencounters(
            now="2026-08-23T10:20:00+00:00"
        ) == ()

        released = await owned.authority.decide_initiative(
            _initiative_change(
                action="release",
                expected_revision=2,
                occurrence_id="proactive:test:initiative:release",
                statement="我明确决定把这件事放下。",
            )
        )
        assert released.revision == 3
        assert await owned.authority.list_initiatives() == ()
        historical = await owned.authority.list_initiatives(
            include_released=True
        )
        assert len(historical) == 1 and historical[0].status == "released"
        with pytest.raises(InitiativeTransitionError):
            await owned.authority.decide_initiative(
                _initiative_change(
                    action="rewrite",
                    expected_revision=3,
                    occurrence_id="proactive:test:initiative:after-release",
                    statement="终态之后不得继续改写。",
                )
            )
    finally:
        await owned.close()


@pytest.mark.asyncio
async def test_startup_backfills_pre_unification_occurrences_across_families(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def legacy_no_guard(*_args: object, **_kwargs: object) -> None:
        return None

    with monkeypatch.context() as legacy:
        legacy.setattr(
            "plugins.life_engine.storage.attention_adapters.claim_proactive_decision",
            legacy_no_guard,
        )
        owned = await open_local_proactive_runtime(
            workspace_path=tmp_path,
            config=_config(),
            validate_active_actor=_active_actor,
        )
        await owned.authority.decide_attention(_attention())
        await owned.close()

    restarted = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    try:
        with pytest.raises(InitiativeConflict):
            await restarted.authority.decide_initiative(
                replace(
                    _initiative(),
                    occurrence_id=_attention().occurrence_id,
                )
            )
        assert (
            await restarted.authority.decide_attention(_attention())
        ).idempotent_replay
    finally:
        await restarted.close()

    initiative_workspace = tmp_path / "initiative-legacy"
    initiative_workspace.mkdir()
    with monkeypatch.context() as legacy:
        legacy.setattr(
            "plugins.life_engine.storage.initiative_adapters.claim_proactive_decision",
            legacy_no_guard,
        )
        owned = await open_local_proactive_runtime(
            workspace_path=initiative_workspace,
            config=_config(),
            validate_active_actor=_active_actor,
        )
        legacy_initiative = replace(
            _initiative(),
            occurrence_id="proactive:test:legacy-initiative-occurrence",
        )
        await owned.authority.decide_initiative(legacy_initiative)
        await owned.close()

    restarted = await open_local_proactive_runtime(
        workspace_path=initiative_workspace,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    try:
        with pytest.raises(AttentionThreadConflict):
            await restarted.authority.decide_attention(
                replace(
                    _attention(),
                    occurrence_id=legacy_initiative.occurrence_id,
                )
            )
        assert (
            await restarted.authority.decide_initiative(legacy_initiative)
        ).idempotent_replay
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_startup_fails_closed_on_legacy_cross_family_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def legacy_no_guard(*_args: object, **_kwargs: object) -> None:
        return None

    with monkeypatch.context() as legacy:
        legacy.setattr(
            "plugins.life_engine.storage.attention_adapters.claim_proactive_decision",
            legacy_no_guard,
        )
        legacy.setattr(
            "plugins.life_engine.storage.initiative_adapters.claim_proactive_decision",
            legacy_no_guard,
        )
        owned = await open_local_proactive_runtime(
            workspace_path=tmp_path,
            config=_config(),
            validate_active_actor=_active_actor,
        )
        await owned.authority.decide_attention(_attention())
        await owned.authority.decide_initiative(
            replace(
                _initiative(),
                occurrence_id=_attention().occurrence_id,
            )
        )
        await owned.close()

    with pytest.raises(
        ProactiveDecisionGuardConflict,
        match="ProactiveLegacyDecisionOccurrenceConflict",
    ):
        await open_local_proactive_runtime(
            workspace_path=tmp_path,
            config=_config(),
            validate_active_actor=_active_actor,
        )

    # Failed startup must release the process lock and writer lease so the
    # failure is diagnosable rather than turning into a stale ownership error.
    with pytest.raises(ProactiveDecisionGuardConflict):
        await open_local_proactive_runtime(
            workspace_path=tmp_path,
            config=_config(),
            validate_active_actor=_active_actor,
        )


@pytest.mark.asyncio
async def test_local_activation_is_revoked_when_backend_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.proactive import runtime as runtime_module

    original_open = runtime_module.open_storage_backend

    async def fail_after_activation(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected backend open failure")

    monkeypatch.setattr(runtime_module, "open_storage_backend", fail_after_activation)
    with pytest.raises(RuntimeError, match="injected backend"):
        await open_local_proactive_runtime(
            workspace_path=tmp_path,
            config=_config(),
            validate_active_actor=_active_actor,
        )

    registry = FileAuthorityRegistry(
        tmp_path / "runtime" / "proactive" / "authority.json",
        registry_id="life-proactive-local",
    )
    health = await registry.health()
    assert health["active_generation"] == ""
    assert not (
        tmp_path / "runtime" / "proactive" / "backend-binding.json"
    ).exists()

    monkeypatch.setattr(runtime_module, "open_storage_backend", original_open)
    recovered = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    await recovered.close()


@pytest.mark.asyncio
async def test_partial_schema_start_failure_revokes_writer_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.proactive import runtime as runtime_module

    original_open = runtime_module.open_attention_thread_stores

    async def fail_after_binding(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected proactive schema failure")

    monkeypatch.setattr(
        runtime_module,
        "open_attention_thread_stores",
        fail_after_binding,
    )
    with pytest.raises(RuntimeError, match="schema failure"):
        await open_local_proactive_runtime(
            workspace_path=tmp_path,
            config=_config(),
            validate_active_actor=_active_actor,
        )

    registry = FileAuthorityRegistry(
        tmp_path / "runtime" / "proactive" / "authority.json",
        registry_id="life-proactive-local",
    )
    assert (await registry.health())["active_generation"] == ""
    # Schema acquisition failed before a durable database anchor existed; the
    # recoverable workspace cache must not pretend that binding completed.
    assert not (
        tmp_path / "runtime" / "proactive" / "backend-binding.json"
    ).exists()

    monkeypatch.setattr(
        runtime_module,
        "open_attention_thread_stores",
        original_open,
    )
    recovered = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    await recovered.close()


@pytest.mark.asyncio
async def test_local_renewal_failure_invalidates_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    original_task = owned._renew_task
    assert original_task is not None
    original_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await original_task
    owned._renew_task = None
    owned.renew_interval_seconds = 0.01  # type: ignore[assignment]

    async def fail_renewal(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected renewal failure")

    monkeypatch.setattr(type(owned.runtime), "renew_authority", fail_renewal)
    owned.start_renewal()
    renewal_task = owned._renew_task
    assert renewal_task is not None
    await asyncio.wait_for(asyncio.shield(renewal_task), timeout=1.0)

    assert owned.runtime.authority_token is None
    assert owned.renewal_health_snapshot() == {
        "status": "failed",
        "last_success_at": "",
        "error_type": "RuntimeError",
        "consecutive_failures": 1,
    }
    assert (await owned.health_snapshot())["status"] == "failed"
    await owned.close()


@pytest.mark.asyncio
async def test_backend_binding_rejects_unverified_mode_switch(
    tmp_path: Path,
) -> None:
    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    try:
        generation = owned.runtime.generation
        assert generation is not None
        switched = replace(
            owned.runtime,
            backend=BackendKind.MYSQL,
            backend_identity="mysql://different-authority",
            generation=replace(
                generation,
                generation_id="proactive-mysql-candidate",
                backend=BackendKind.MYSQL,
            ),
        )
        with pytest.raises(
            ProactiveBackendBindingConflict,
            match="RequiresVerifiedMigration",
        ):
            await ensure_proactive_backend_binding(
                workspace_path=tmp_path,
                binding_path="runtime/proactive/backend-binding.json",
                runtime=switched,
            )
    finally:
        await owned.close()


@pytest.mark.asyncio
async def test_bound_old_backend_cannot_override_current_workspace_marker(
    tmp_path: Path,
) -> None:
    old_workspace = tmp_path / "old-backend"
    current_workspace = tmp_path / "current-backend"
    old = await open_local_proactive_runtime(
        workspace_path=old_workspace,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    await old.authority.decide_attention(_attention())
    await old.close()
    current = await open_local_proactive_runtime(
        workspace_path=current_workspace,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    await current.close()

    relative = Path("runtime/proactive/backend-binding.json")
    (old_workspace / relative).write_bytes(
        (current_workspace / relative).read_bytes()
    )
    with pytest.raises(
        ProactiveBackendBindingConflict,
        match="RequiresVerifiedMigration",
    ):
        await open_local_proactive_runtime(
            workspace_path=old_workspace,
            config=_config(),
            validate_active_actor=_inactive_actor,
        )


@pytest.mark.asyncio
async def test_backend_binding_cache_is_rebuilt_from_database_anchor(
    tmp_path: Path,
) -> None:
    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    await owned.authority.decide_attention(_attention())
    binding_path = tmp_path / "runtime" / "proactive" / "backend-binding.json"
    expected = json.loads(binding_path.read_text(encoding="utf-8"))
    assert expected["schema_version"] == 3
    assert len(expected["identity"]["generation_manifest_sha256"]) == 64
    assert expected["identity"]["authority_provider"] == "file"
    assert expected["identity"]["authority_registry_id"] == "life-proactive-local"
    await owned.close()

    binding_path.unlink()
    restarted = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_inactive_actor,
    )
    try:
        assert json.loads(binding_path.read_text(encoding="utf-8")) == expected
        health = await restarted.health_snapshot()
        assert health["backend_binding"]["status"] == "healthy"
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_backend_binding_corrupt_database_head_fails_closed(
    tmp_path: Path,
) -> None:
    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    async with owned.runtime.unit_of_work() as uow:
        await uow.session.execute(
            text(
                """UPDATE runtime_states SET payload_sha256 = :digest
                WHERE namespace = 'life_proactive.backend_binding'
                  AND state_key = 'active'"""
            ),
            {"digest": "0" * 64},
        )
    assert (await owned.health_snapshot())["status"] == "failed"
    await owned.close()

    with pytest.raises(
        ProactiveBackendBindingConflict,
        match="RecordCorrupt",
    ):
        await open_local_proactive_runtime(
            workspace_path=tmp_path,
            config=_config(),
            validate_active_actor=_inactive_actor,
        )


def _remove_database_binding_anchor(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER runtime_events_immutable_delete_v1")
        connection.execute(
            "DELETE FROM runtime_events WHERE namespace = ?",
            ("life_proactive.backend_binding",),
        )
        connection.execute(
            "DELETE FROM runtime_states WHERE namespace = ? AND state_key = ?",
            ("life_proactive.backend_binding", "active"),
        )
        connection.commit()


@pytest.mark.asyncio
async def test_existing_history_without_either_binding_anchor_fails_closed(
    tmp_path: Path,
) -> None:
    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    await owned.authority.decide_attention(_attention())
    await owned.close()
    database_path = tmp_path / "runtime" / "proactive" / "proactive.sqlite3"
    binding_path = tmp_path / "runtime" / "proactive" / "backend-binding.json"
    _remove_database_binding_anchor(database_path)
    binding_path.unlink()

    with pytest.raises(
        ProactiveBackendBindingConflict,
        match="MissingForExistingHistory",
    ):
        await open_local_proactive_runtime(
            workspace_path=tmp_path,
            config=_config(),
            validate_active_actor=_inactive_actor,
        )


@pytest.mark.asyncio
async def test_orphan_projection_without_binding_anchor_fails_closed(
    tmp_path: Path,
) -> None:
    """A head/inbox-only corruption cannot be laundered as a fresh authority."""

    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    await owned.close()
    database_path = tmp_path / "runtime" / "proactive" / "proactive.sqlite3"
    binding_path = tmp_path / "runtime" / "proactive" / "backend-binding.json"
    _remove_database_binding_anchor(database_path)
    binding_path.unlink()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO runtime_states (
                namespace, state_key, revision, schema_version,
                payload_json, payload_sha256, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "life_initiative.seed_heads",
                "orphan-head",
                1,
                1,
                "{}",
                hashlib.sha256(b"{}").hexdigest(),
                "2026-08-23T00:00:00+00:00",
            ),
        )
        connection.commit()

    with pytest.raises(
        ProactiveBackendBindingConflict,
        match="MissingForExistingHistory",
    ):
        await open_local_proactive_runtime(
            workspace_path=tmp_path,
            config=_config(),
            validate_active_actor=_inactive_actor,
        )


@pytest.mark.asyncio
async def test_legacy_workspace_binding_cannot_rebind_existing_history(
    tmp_path: Path,
) -> None:
    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    await owned.authority.decide_attention(_attention())
    binding_path = tmp_path / "runtime" / "proactive" / "backend-binding.json"
    current = json.loads(binding_path.read_text(encoding="utf-8"))
    await owned.close()

    database_path = tmp_path / "runtime" / "proactive" / "proactive.sqlite3"
    _remove_database_binding_anchor(database_path)
    legacy = {"schema_version": 1, **current["identity"]}
    binding_path.write_text(json.dumps(legacy), encoding="utf-8")

    with pytest.raises(
        ProactiveBackendBindingConflict,
        match="LegacyBindingRequiresVerifiedMigration",
    ):
        await open_local_proactive_runtime(
            workspace_path=tmp_path,
            config=_config(),
            validate_active_actor=_inactive_actor,
        )


@pytest.mark.asyncio
async def test_outreach_expression_inbox_claim_and_terminal_survive_restarts(
    tmp_path: Path,
) -> None:
    command = InitiativeOutreachCommand(
        occurrence_id="proactive:test:outreach:pending",
        actor_consciousness_instance_id="chat_global",
        source_instance_id="chat_global",
        source_occurrence_ids=("life:event:outreach",),
        causation_occurrence_id="life:event:outreach",
        audience_ref="person:xiaoxi",
        surface_ref="surface:qq",
        public_intention="我明确选择发起一次问候。",
        occurred_at="2026-08-23T10:02:00+00:00",
    )
    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    receipt = await owned.authority.begin_outreach(command)
    assert receipt.idempotent_replay is False
    pending = await owned.authority.pending_outreach()
    assert [item.command.occurrence_id for item in pending] == [
        command.occurrence_id
    ]
    await owned.close()

    restarted = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_inactive_actor,
    )
    try:
        replayed = await restarted.authority.pending_outreach()
        assert [item.command.occurrence_id for item in replayed] == [
            command.occurrence_id
        ]
        delivery = await restarted.authority.record_outreach_delivery(
            outreach_occurrence_id=command.occurrence_id,
            stream_id="qq-stream",
            trigger_message_id="initiative_outreach_stable",
            occurred_at="2026-08-23T10:03:00+00:00",
            platform="qq",
        )
        assert delivery.idempotent_replay is False
        assert delivery.turn_id
        assert len(delivery.inbox_payload_sha256) == 64
        assert await restarted.authority.pending_outreach() == ()
        expression = await restarted.authority.pending_expression_outreach()
        assert len(expression) == 1
        assert expression[0].status == "pending"
        assert expression[0].platform == "qq"
        assert expression[0].turn_id == delivery.turn_id
    finally:
        await restarted.close()

    final = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_inactive_actor,
    )
    try:
        assert await final.authority.pending_outreach() == ()
        expression = await final.authority.pending_expression_outreach()
        assert len(expression) == 1
        assert expression[0].status == "pending"

        claim = await final.authority.claim_outreach_expression(
            outreach_occurrence_id=command.occurrence_id,
            action_id="tool-call-visible-1",
            claim_owner="boot:test:first",
            lease_seconds=300,
            occurred_at="2026-08-23T10:04:00+00:00",
        )
        assert claim.execute_allowed is True
        assert claim.claim_epoch == 1
        replay = await final.authority.claim_outreach_expression(
            outreach_occurrence_id=command.occurrence_id,
            action_id="tool-call-visible-1",
            claim_owner="boot:test:first",
            lease_seconds=300,
            occurred_at="2026-08-23T10:04:00+00:00",
        )
        assert replay.execute_allowed is False
        assert replay.idempotent_replay is True
        expression = await final.authority.pending_expression_outreach()
        assert expression[0].status == "processing"
        assert expression[0].claimed_action_id == "tool-call-visible-1"
        assert expression[0].claim_epoch == 1
        health = await final.authority.health_snapshot()
        assert health["initiative"]["pending_expression_count"] == 1
        assert health["initiative"]["processing_expression_count"] == 1
    finally:
        await final.close()

    recovered = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_inactive_actor,
    )
    try:
        interrupted = await recovered.authority.pending_expression_outreach()
        assert len(interrupted) == 1
        assert interrupted[0].status == "processing"
        resolution = await recovered.authority.resolve_outreach_expression(
            outreach_occurrence_id=command.occurrence_id,
            outcome="delivery_unknown",
            action_id="tool-call-visible-1",
            occurred_at="2026-08-23T10:05:00+00:00",
        )
        assert resolution.idempotent_replay is False
        assert resolution.claim_epoch == 1
        assert await recovered.authority.pending_expression_outreach() == ()
        delivery_replay = await recovered.authority.record_outreach_delivery(
            outreach_occurrence_id=command.occurrence_id,
            stream_id="qq-stream",
            trigger_message_id="initiative_outreach_stable",
            occurred_at="2026-08-23T10:03:00+00:00",
            platform="qq",
        )
        assert delivery_replay.idempotent_replay is True
        assert delivery_replay.expression_resolved is True
        assert delivery_replay.expression_outcome == "delivery_unknown"

        async with recovered.runtime.unit_of_work() as uow:
            turn = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT status, result_ref, result_digest
                            FROM stream_turns WHERE turn_id = :turn_id"""
                        ),
                        {"turn_id": resolution.turn_id},
                    )
                )
                .mappings()
                .one()
            )
        assert turn["status"] == "completed"
        assert str(turn["result_ref"]).endswith("/resolution")
        assert len(str(turn["result_digest"])) == 64
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_visible_outreach_outcome_requires_exact_action_claim(
    tmp_path: Path,
) -> None:
    command = InitiativeOutreachCommand(
        occurrence_id="proactive:test:outreach:claim-required",
        actor_consciousness_instance_id="chat_global",
        source_instance_id="chat_global",
        source_occurrence_ids=("life:event:outreach",),
        causation_occurrence_id="life:event:outreach",
        audience_ref="person:xiaoxi",
        surface_ref="surface:qq",
        public_intention="我选择现在表达。",
        occurred_at="2026-08-23T11:00:00+00:00",
    )
    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    try:
        await owned.authority.begin_outreach(command)
        await owned.authority.record_outreach_delivery(
            outreach_occurrence_id=command.occurrence_id,
            stream_id="qq-stream",
            trigger_message_id="initiative_outreach_claim_required",
            occurred_at="2026-08-23T11:01:00+00:00",
            platform="qq",
        )
        with pytest.raises(InitiativeTransitionError):
            await owned.authority.resolve_outreach_expression(
                outreach_occurrence_id=command.occurrence_id,
                outcome="spoke",
                action_id="unclaimed-action",
                delivery_receipt_sha256="a" * 64,
                delivery_message_id="message:unclaimed",
                occurred_at="2026-08-23T11:02:00+00:00",
            )
        claim = await owned.authority.claim_outreach_expression(
            outreach_occurrence_id=command.occurrence_id,
            action_id="claimed-action",
            claim_owner="boot:test:claim-required",
            lease_seconds=300,
            occurred_at="2026-08-23T11:03:00+00:00",
        )
        with pytest.raises(InitiativeConflict):
            await owned.authority.resolve_outreach_expression(
                outreach_occurrence_id=command.occurrence_id,
                outcome="spoke",
                action_id="different-action",
                delivery_receipt_sha256="b" * 64,
                delivery_message_id="message:different-action",
                occurred_at="2026-08-23T11:04:00+00:00",
            )
        with pytest.raises(InitiativeTransitionError):
            await owned.authority.resolve_outreach_expression(
                outreach_occurrence_id=command.occurrence_id,
                outcome="spoke",
                action_id=claim.action_id,
                delivery_receipt_sha256="c" * 64,
                delivery_message_id="message:claimed-action",
                occurred_at="2026-08-23T11:04:30+00:00",
            )
        delivery_receipt = {
            "schema_version": 1,
            "receipt_kind": "adapter_ack",
            "message_id": "message:claimed-action",
            "platform": "qq",
            "adapter_signature": "napcat_adapter:adapter:napcat_adapter",
            "provider_receipt": {"status": "ok", "retcode": 0},
        }
        receipt_sha256 = hashlib.sha256(
            json.dumps(
                delivery_receipt,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        proof = await owned.authority.record_outreach_delivery_proof(
            outreach_occurrence_id=command.occurrence_id,
            action_id=claim.action_id,
            delivery_receipt=delivery_receipt,
            occurred_at="2026-08-23T11:04:45+00:00",
        )
        assert proof.delivery_receipt_sha256 == receipt_sha256
        assert proof.delivery_message_id == "message:claimed-action"
        resolution = await owned.authority.resolve_outreach_expression(
            outreach_occurrence_id=command.occurrence_id,
            outcome="spoke",
            action_id=claim.action_id,
            delivery_receipt_sha256=receipt_sha256,
            delivery_message_id="message:claimed-action",
            occurred_at="2026-08-23T11:05:00+00:00",
        )
        assert resolution.outcome == "spoke"
        assert resolution.action_id == claim.action_id
        assert resolution.delivery_receipt_sha256 == receipt_sha256
        assert resolution.delivery_message_id == "message:claimed-action"
        assert (await owned.authority.health_snapshot())["status"] == "healthy"
        async with owned.runtime.unit_of_work() as uow:
            # Simulate out-of-band disk corruption in this disposable test DB.
            # Production writes cannot do this: the immutable delete trigger is
            # itself separately covered by the storage contract suite.
            await uow.session.execute(
                text("DROP TRIGGER runtime_events_immutable_delete_v1")
            )
            await uow.session.execute(
                text(
                    """DELETE FROM runtime_events
                    WHERE namespace = 'life_initiative.outreach_delivery_proofs'"""
                )
            )
        proof_health = await owned.authority.health_snapshot()
        assert proof_health["status"] == "failed"
        assert (
            "spoke_without_delivery_proof"
            in proof_health["initiative"]["consistency_error_types"]
        )
        with pytest.raises(InitiativeConflict):
            await owned.authority.resolve_outreach_expression(
                outreach_occurrence_id=command.occurrence_id,
                outcome="passed",
                action_id="",
                occurred_at="2026-08-23T11:06:00+00:00",
            )
    finally:
        await owned.close()


@pytest.mark.asyncio
async def test_health_replays_events_instead_of_trusting_projection_heads(
    tmp_path: Path,
) -> None:
    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    try:
        await owned.authority.decide_attention(_attention())
        await owned.authority.decide_initiative(_initiative())
        assert (await owned.authority.health_snapshot())["status"] == "healthy"

        async with owned.runtime.unit_of_work() as uow:
            await uow.session.execute(
                text("DELETE FROM attention_thread_heads")
            )
            await uow.session.execute(
                text(
                    """DELETE FROM runtime_states
                    WHERE namespace = 'life_initiative.seed_heads'"""
                )
            )

        health = await owned.authority.health_snapshot()
        assert health["status"] == "failed"
        assert (
            "attention_head_missing"
            in health["attention"]["consistency_error_types"]
        )
        assert (
            "seed_head_missing"
            in health["initiative"]["consistency_error_types"]
        )
    finally:
        await owned.close()


@pytest.mark.asyncio
async def test_expired_expression_claim_degrades_health_and_becomes_recoverable(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC).isoformat()
    command = InitiativeOutreachCommand(
        occurrence_id="proactive:test:outreach:expired-lease",
        actor_consciousness_instance_id="chat_global",
        source_instance_id="chat_global",
        source_occurrence_ids=("life:event:expired-lease",),
        causation_occurrence_id="life:event:expired-lease",
        audience_ref="person:xiaoxi",
        surface_ref="surface:qq",
        public_intention="我选择现在表达。",
        occurred_at=now,
    )
    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    try:
        await owned.authority.begin_outreach(command)
        await owned.authority.record_outreach_delivery(
            outreach_occurrence_id=command.occurrence_id,
            stream_id="qq-stream",
            trigger_message_id="initiative_outreach_expired_lease",
            occurred_at=now,
            platform="qq",
        )
        await owned.authority.claim_outreach_expression(
            outreach_occurrence_id=command.occurrence_id,
            action_id="claimed-action",
            claim_owner="boot:test:expired-lease",
            lease_seconds=15,
            occurred_at=now,
        )
        store = owned.authority._initiative
        database_now = store._database_now

        async def after_lease(session):
            return (await database_now(session)) + timedelta(seconds=16)

        store._database_now = after_lease  # type: ignore[method-assign]

        pending = await owned.authority.pending_expression_outreach()
        assert pending[0].claim_expired is True
        health = await owned.authority.health_snapshot()
        assert health["status"] == "degraded"
        assert health["initiative"]["expired_processing_expression_count"] == 1
        assert "expired_expression_claim" in health["initiative"]["degraded_reasons"]
    finally:
        await owned.close()


@pytest.mark.asyncio
async def test_stale_outreach_backlog_is_observable_without_auto_decision(
    tmp_path: Path,
) -> None:
    command = InitiativeOutreachCommand(
        occurrence_id="proactive:test:outreach:stale-backlog",
        actor_consciousness_instance_id="chat_global",
        source_instance_id="chat_global",
        source_occurrence_ids=("life:event:stale-backlog",),
        causation_occurrence_id="life:event:stale-backlog",
        audience_ref="person:xiaoxi",
        surface_ref="surface:qq",
        public_intention="我选择稍后从已选表面表达。",
        occurred_at="2020-01-01T00:00:00+00:00",
    )
    owned = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    try:
        await owned.authority.begin_outreach(command)
        health = await owned.authority.health_snapshot()
        assert health["status"] == "degraded"
        assert health["initiative"]["pending_outreach_count"] == 1
        assert (
            "outreach_delivery_backlog_stale"
            in health["initiative"]["degraded_reasons"]
        )
        assert (await owned.authority.pending_outreach())[0].command == command
    finally:
        await owned.close()
