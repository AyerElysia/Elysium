"""One-shot InitiativeSeed delivery into the durable Life Event path."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.core.chatter import LifeChatter
from plugins.life_engine.initiative.contracts import (
    InitiativeOutreachCommand,
    InitiativeOutreachDeliveryReceipt,
    InitiativeOutreachReceipt,
    InitiativePendingExpression,
    InitiativeSeedView,
)
from plugins.life_engine.initiative.projection import (
    INITIATIVE_CONTENT_PROJECTION_MAX_BYTES,
    initiative_seed_content,
    initiative_seed_summary,
    project_initiative_seed_content,
)
from plugins.life_engine.service.consciousness import (
    ConsciousnessInstance,
    ConsciousnessRegistry,
)
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.event_builder import (
    EventType,
    LifeEngineEvent,
    LifeEngineState,
)
from plugins.life_engine.service.event_bus import life_event_from_legacy
from plugins.life_engine.service.state_manager import event_from_dict, event_to_dict
from src.core.models.stream import StreamContext


def _view() -> InitiativeSeedView:
    return InitiativeSeedView(
        seed_id="initiative:seed:story",
        status="open",
        revision=3,
        current_statement="我想在合适的时候，再看看自己写到哪里的故事。",
        related_entity_refs=("person:xiaoxi",),
        opened_at="2026-08-17T00:00:00+00:00",
        last_changed_at="2026-08-17T01:00:00+00:00",
        last_event_position=3,
        last_event_id="initiative:seed:event:3",
        last_occurrence_id="decision-3",
        reencounter_at="2026-08-17T02:00:00+00:00",
        reencounter_revision=2,
        reencounter_event_id="initiative:seed:event:2",
        content_event_id="initiative:seed:event:1",
        content_revision=1,
    )


def test_heartbeat_nucleus_pool_exposes_only_unified_proactive_tools() -> None:
    service = LifeEngineService.__new__(LifeEngineService)
    names = {tool.tool_name for tool in service._get_nucleus_tools()}
    assert {
        "nucleus_proactive_query",
        "nucleus_proactive_command",
    } <= names
    assert {
        "nucleus_manage_thought_stream",
        "nucleus_manage_attention_thread",
        "nucleus_manage_initiative_seed",
        "nucleus_reachability",
        "nucleus_begin_outreach",
    }.isdisjoint(names)
    assert "nucleus_schedule_autonomy_intent" not in names


@pytest.mark.asyncio
async def test_initiative_actor_gate_reconciles_expired_lease() -> None:
    registry = ConsciousnessRegistry(bootstrap=False)
    registry.register(
        ConsciousnessInstance(
            instance_id="voice:expired",
            kind="voice_live",
            stream_ids=["voice-stream"],
            created_at="2020-01-01T00:00:00+00:00",
            last_active_at="2020-01-01T00:00:00+00:00",
            lease_duration_seconds=1,
        )
    )
    service = LifeEngineService.__new__(LifeEngineService)
    service._consciousness_registry = registry
    service._selectable_storage_enabled = False

    assert (
        await service._validate_initiative_decision_actor("voice:expired")
        is False
    )
    assert registry.get("voice:expired").status == "suspended"


def test_legacy_event_bridge_preserves_explicit_occurrence_identity() -> None:
    event = LifeEngineEvent(
        event_id="initiative-reencounter-event",
        event_type=EventType.MESSAGE,
        timestamp="2026-08-17T02:00:00+00:00",
        sequence=19,
        source="life_engine",
        source_detail="initiative",
        content="subject-authored seed",
        occurrence_id="initiative-reencounter-occurrence",
    )
    restored = event_from_dict(event_to_dict(event))
    raw = life_event_from_legacy(restored)
    assert restored.occurrence_id == "initiative-reencounter-occurrence"
    assert raw.occurrence_id == "initiative-reencounter-occurrence"

    restored.content_type = "initiative_reencounter"
    raw = life_event_from_legacy(restored)
    assert raw.channel == "life"
    assert raw.priority == 50


@pytest.mark.asyncio
async def test_outreach_wake_enters_expression_without_forcing_a_reply() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    outreach = SimpleNamespace(
        is_proactive_opportunity_trigger=False,
        is_proactive_followup_trigger=False,
        is_initiative_outreach_trigger=True,
        is_autonomy_intent_trigger=False,
        sender_role="other",
    )
    decision = await chatter._should_respond(
        "initiative outreach",
        [outreach],  # type: ignore[list-item]
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert decision["should_respond"] is True
    assert decision["force_reply"] is False
    assert LifeChatter._should_force_reply_for_unread_batch([outreach]) is False  # type: ignore[list-item]

    retired = SimpleNamespace(
        is_proactive_opportunity_trigger=False,
        is_proactive_followup_trigger=False,
        is_initiative_outreach_trigger=False,
        is_autonomy_intent_trigger=True,
    )
    assert LifeChatter._is_proactive_trigger_message(retired) is False  # type: ignore[arg-type]
    assert LifeChatter._autonomy_occurrence_scope(  # type: ignore[arg-type]
        [retired],
        "legacy-stream",
    ) == []


@pytest.mark.asyncio
async def test_retired_followup_trigger_does_not_enter_expression() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    retired = SimpleNamespace(
        is_proactive_opportunity_trigger=False,
        is_proactive_followup_trigger=True,
        is_initiative_outreach_trigger=False,
        sender_role="other",
    )
    decision = await chatter._should_respond(
        "retired followup",
        [retired],  # type: ignore[list-item]
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert decision == {
        "reason": "旧延迟续话触发已退役，不进入表达决策",
        "should_respond": False,
        "force_reply": False,
    }
    assert LifeChatter._is_proactive_trigger_message(retired) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_legacy_followup_service_cannot_schedule_or_wake() -> None:
    service = LifeEngineService.__new__(LifeEngineService)
    ok, reason = await service.schedule_followup_for_stream(
        SimpleNamespace(stream_id="legacy-stream"),
        delay_seconds=30,
        thought="old thought",
        topic="old topic",
        followup_type="add_detail",
        source="legacy",
    )
    assert ok is False
    assert "LegacyFollowupReadOnly" in reason


@pytest.mark.asyncio
async def test_outreach_wake_uses_stable_message_identity_and_dedupes(
    monkeypatch,
) -> None:
    context = StreamContext(stream_id="qq-stream")
    chat_stream = SimpleNamespace(
        stream_id="qq-stream",
        platform="qq",
        context=context,
    )

    class _Streams:
        async def get_or_create_stream(self, *, stream_id: str):
            assert stream_id == "qq-stream"
            return chat_stream

    loop_manager = SimpleNamespace(
        _wait_states={"qq-stream": object()},
        start_stream_loop=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.core.managers.get_stream_manager",
        lambda: _Streams(),
    )
    monkeypatch.setattr(
        "src.core.transport.distribution.stream_loop_manager.get_stream_loop_manager",
        lambda: loop_manager,
    )
    service = LifeEngineService.__new__(LifeEngineService)
    command = InitiativeOutreachCommand(
        occurrence_id="initiative:outreach:one-stable-occurrence",
        actor_consciousness_instance_id="chat:active",
        source_instance_id="kook:scene",
        source_occurrence_ids=("message:kook:7",),
        causation_occurrence_id="message:kook:7",
        audience_ref="person:xiaoxi",
        surface_ref="surface:qq",
        public_intention="我现在想去问候小希。",
        occurred_at="2026-08-17T08:00:00+00:00",
    )

    await service._wake_stream_for_initiative(
        stream_id="qq-stream",
        platform="qq",
        command=command,
        turn_id="initiative-turn-stable",
    )
    await service._wake_stream_for_initiative(
        stream_id="qq-stream",
        platform="qq",
        command=command,
        turn_id="initiative-turn-stable",
    )

    assert len(context.unread_messages) == 1
    message = context.unread_messages[0]
    assert message.message_id.startswith("initiative_outreach_")
    assert message.extra["initiative_outreach_occurrence_id"] == (
        command.occurrence_id
    )
    assert message.extra["initiative_outreach_turn_id"] == "initiative-turn-stable"
    assert message.sender_id == "life_engine_initiative"
    assert not getattr(message, "target_user_id", "")
    assert not getattr(message, "target_user_name", "")
    assert "重新判断" in message.processed_plain_text
    assert "必须发送" not in message.processed_plain_text
    assert message.extra["bypass_message_buffer"] is True
    assert loop_manager._wait_states == {}
    assert loop_manager.start_stream_loop.await_count == 2
    loop_manager.start_stream_loop.assert_awaited_with("qq-stream")


def test_outreach_occurrence_scope_reads_only_exact_trigger_metadata() -> None:
    valid = SimpleNamespace(
        is_initiative_outreach_trigger=True,
        initiative_outreach_occurrence_id="outreach:one",
        extra={},
    )
    fallback = SimpleNamespace(
        is_initiative_outreach_trigger=False,
        extra={
            "is_initiative_outreach_trigger": True,
            "initiative_outreach_occurrence_id": "outreach:two",
        },
    )
    ordinary = SimpleNamespace(
        is_initiative_outreach_trigger=False,
        initiative_outreach_occurrence_id="outreach:forged",
        extra={},
    )

    assert LifeChatter._initiative_outreach_occurrence_scope(  # type: ignore[arg-type]
        [valid, fallback, valid, ordinary]
    ) == ["outreach:one", "outreach:two"]


@pytest.mark.asyncio
async def test_service_claim_replay_preserves_live_lease_without_reexecuting() -> None:
    resolutions: list[dict[str, object]] = []

    class _Authority:
        async def claim_outreach_expression(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                claim_epoch=4,
                execute_allowed=False,
            )

        async def resolve_outreach_expression(self, **kwargs: object) -> object:
            resolutions.append(kwargs)
            return SimpleNamespace()

    service = LifeEngineService.__new__(LifeEngineService)
    service._proactive_authority = _Authority()
    service._active_initiative_expression_claims = set()
    service._pending_initiative_expression_resolutions = {}
    service._proactive_claim_owner = "boot:test:claim-replay"
    service._cfg = lambda: SimpleNamespace(  # type: ignore[method-assign]
        proactive=SimpleNamespace(expression_claim_lease_seconds=300)
    )

    result = await service.claim_initiative_outreach_expressions(
        ["outreach:one"],
        action_id="action:one",
    )

    assert result["execute_allowed"] is False
    assert result["reason"] == "claim_replayed"
    assert resolutions == []
    assert service._active_initiative_expression_claims == set()


@pytest.mark.asyncio
async def test_recovery_scanner_never_settles_a_live_expression_claim() -> None:
    command = InitiativeOutreachCommand(
        occurrence_id="initiative:outreach:live-claim",
        actor_consciousness_instance_id="chat:active",
        source_instance_id="chat:active",
        source_occurrence_ids=("life:event:live-claim",),
        causation_occurrence_id="life:event:live-claim",
        audience_ref="person:xiaoxi",
        surface_ref="surface:qq",
        public_intention="我选择表达。",
        occurred_at="2026-08-23T12:00:00+00:00",
    )
    pending = InitiativePendingExpression(
        authority_event_id="initiative:outreach:event:1",
        event_position=1,
        command=command,
        delivery_event_id="initiative:outreach:inbox:2",
        delivery_position=2,
        stream_id="qq-stream",
        platform="qq",
        trigger_message_id="initiative_outreach_live_claim",
        turn_id="initiative-turn-live-claim",
        delivered_at="2026-08-23T12:00:01+00:00",
        status="processing",
        claimed_action_id="action:live",
        claim_epoch=1,
        claim_owner="boot:other-process",
        claim_lease_until="2099-01-01T00:00:00+00:00",
        claim_expired=False,
    )
    service = LifeEngineService.__new__(LifeEngineService)
    service._stop_event = asyncio.Event()

    class _Authority:
        async def pending_outreach(self, **_kwargs: object) -> tuple[()]:
            return ()

        async def pending_expression_outreach(
            self, **_kwargs: object
        ) -> tuple[InitiativePendingExpression, ...]:
            service._stop_event.set()
            return (pending,)

        async def due_reencounters(self, **_kwargs: object) -> tuple[()]:
            return ()

    service._proactive_authority = _Authority()
    service._active_initiative_expression_claims = set()
    service._pending_initiative_expression_resolutions = {}
    service.resolve_initiative_outreach_expressions = AsyncMock()  # type: ignore[method-assign]
    service._wake_stream_for_initiative = AsyncMock()  # type: ignore[method-assign]

    await service._initiative_reencounter_loop()

    service.resolve_initiative_outreach_expressions.assert_not_awaited()
    service._wake_stream_for_initiative.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_scanner_marks_only_expired_claim_delivery_unknown() -> None:
    command = InitiativeOutreachCommand(
        occurrence_id="initiative:outreach:expired-claim",
        actor_consciousness_instance_id="chat:active",
        source_instance_id="chat:active",
        source_occurrence_ids=("life:event:expired-claim",),
        causation_occurrence_id="life:event:expired-claim",
        audience_ref="person:xiaoxi",
        surface_ref="surface:qq",
        public_intention="我选择表达。",
        occurred_at="2026-08-23T12:00:00+00:00",
    )
    pending = InitiativePendingExpression(
        authority_event_id="initiative:outreach:event:1",
        event_position=1,
        command=command,
        delivery_event_id="initiative:outreach:inbox:2",
        delivery_position=2,
        stream_id="qq-stream",
        platform="qq",
        trigger_message_id="initiative_outreach_expired_claim",
        turn_id="initiative-turn-expired-claim",
        delivered_at="2026-08-23T12:00:01+00:00",
        status="processing",
        claimed_action_id="action:expired",
        claim_epoch=1,
        claim_owner="boot:stopped-process",
        claim_lease_until="2020-01-01T00:00:00+00:00",
        claim_expired=True,
    )
    service = LifeEngineService.__new__(LifeEngineService)
    service._stop_event = asyncio.Event()

    class _Authority:
        async def pending_outreach(self, **_kwargs: object) -> tuple[()]:
            return ()

        async def pending_expression_outreach(
            self, **_kwargs: object
        ) -> tuple[InitiativePendingExpression, ...]:
            service._stop_event.set()
            return (pending,)

        async def due_reencounters(self, **_kwargs: object) -> tuple[()]:
            return ()

    service._proactive_authority = _Authority()
    service._active_initiative_expression_claims = set()
    service._pending_initiative_expression_resolutions = {}
    service.resolve_initiative_outreach_expressions = AsyncMock()  # type: ignore[method-assign]
    service._wake_stream_for_initiative = AsyncMock()  # type: ignore[method-assign]

    await service._initiative_reencounter_loop()

    service.resolve_initiative_outreach_expressions.assert_awaited_once_with(
        [command.occurrence_id],
        outcome="delivery_unknown",
        action_id="action:expired",
    )
    service._wake_stream_for_initiative.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_retries_terminal_outreach_receipt_without_rewake() -> None:
    attempts = 0

    class _Authority:
        async def resolve_outreach_expression(self, **_kwargs: object) -> object:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary database outage")
            return SimpleNamespace()

    service = LifeEngineService.__new__(LifeEngineService)
    service._proactive_authority = _Authority()
    service._active_initiative_expression_claims = {"outreach:one"}
    service._pending_initiative_expression_resolutions = {}

    first = await service.resolve_initiative_outreach_expressions(
        ["outreach:one"],
        outcome="spoke",
        action_id="action:one",
        delivery_receipt_sha256="d" * 64,
        delivery_message_id="message:one",
    )
    assert first["resolved_count"] == 0
    assert first["pending_count"] == 1
    assert "outreach:one" in service._pending_initiative_expression_resolutions
    assert service._active_initiative_expression_claims == set()

    await service._flush_pending_initiative_expression_resolutions()

    assert attempts == 2
    assert service._pending_initiative_expression_resolutions == {}


@pytest.mark.asyncio
async def test_outreach_wake_failure_preserves_durable_inbox_before_volatile_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    command = InitiativeOutreachCommand(
        occurrence_id="initiative:outreach:durable-before-wake",
        actor_consciousness_instance_id="chat:active",
        source_instance_id="chat:active",
        source_occurrence_ids=("life:event:outreach",),
        causation_occurrence_id="life:event:outreach",
        audience_ref="person:xiaoxi",
        surface_ref="surface:qq",
        public_intention="我明确选择去问候她。",
        occurred_at="2026-08-17T08:00:00+00:00",
    )

    class _Authority:
        async def begin_outreach(self, _command: object) -> InitiativeOutreachReceipt:
            order.append("authority")
            return InitiativeOutreachReceipt(
                event_id="initiative:outreach:event:9",
                occurrence_id=command.occurrence_id,
                audience_ref=command.audience_ref,
                surface_ref=command.surface_ref,
                idempotent_replay=False,
            )

        async def record_outreach_delivery(
            self,
            **kwargs: object,
        ) -> InitiativeOutreachDeliveryReceipt:
            order.append("inbox")
            assert kwargs["platform"] == "qq"
            return InitiativeOutreachDeliveryReceipt(
                event_id="initiative:outreach:inbox:10",
                occurrence_id="initiative:outreach:inbox:stable",
                outreach_occurrence_id=command.occurrence_id,
                stream_id="qq-stream",
                trigger_message_id=(
                    service._initiative_outreach_trigger_message_id(
                        command.occurrence_id
                    )
                ),
                turn_id="initiative-outreach-turn-10",
                inbox_payload_sha256="a" * 64,
                idempotent_replay=False,
            )

    async def resolve(**_kwargs: object) -> object:
        return SimpleNamespace(stream_id="qq-stream", platform="qq")

    async def fail_wake(**_kwargs: object) -> str:
        order.append("wake")
        raise RuntimeError("expression inbox unavailable")

    monkeypatch.setattr(
        "plugins.life_engine.initiative.reachability.resolve_reachable_surface",
        resolve,
    )
    service = LifeEngineService.__new__(LifeEngineService)
    service._proactive_authority = _Authority()
    service._wake_stream_for_initiative = fail_wake  # type: ignore[method-assign]

    result = await service.begin_initiative_outreach(command)

    assert result["authority_committed"] is True
    assert result["inbox_committed"] is True
    assert result["expression_wake_enqueued"] is False
    assert result["delivery_pending"] is True
    assert result["message_sent"] is False
    assert result["event_id"] == "initiative:outreach:event:9"
    assert result["occurrence_id"] == command.occurrence_id
    assert result["delivery_event_id"] == "initiative:outreach:inbox:10"
    assert result["delivery_error_type"] == "RuntimeError"
    assert order == ["authority", "inbox", "wake"]


@pytest.mark.asyncio
async def test_surface_reencounter_queues_bounded_subject_projection_without_action_rule() -> None:
    service = LifeEngineService.__new__(LifeEngineService)
    service._state = LifeEngineState()
    queued = []
    receipts: list[dict[str, object]] = []

    async def _missing(_event_id: str) -> bool:
        return False

    async def _queue(event: object) -> None:
        queued.append(event)

    async def _record(**kwargs: object) -> None:
        receipts.append(kwargs)

    service._initiative_life_event_exists = _missing  # type: ignore[method-assign]
    service._queue_pending_event = _queue  # type: ignore[method-assign]
    service._proactive_authority = SimpleNamespace(
        record_reencounter_delivery=_record
    )

    await service._surface_initiative_reencounter(_view())

    assert len(queued) == 1
    event = queued[0]
    assert event.event_id.startswith("initiative_reencounter_")
    assert event.content_type == "initiative_reencounter"
    payload = ast.literal_eval(event.content)
    assert payload["authority"] == "subject_initiative"
    assert payload["action_required"] is False
    assert "自己写到哪里的故事" in payload["content"]
    assert len(event.content.encode("utf-8")) <= (
        INITIATIVE_CONTENT_PROJECTION_MAX_BYTES
    )
    assert "QQ" not in event.content
    assert "Kook" not in event.content
    assert "stream_id" not in event.content
    assert receipts[0]["seed_revision"] == 2
    assert receipts[0]["life_event_id"] == event.event_id


def test_large_seed_content_is_exactly_resumable_and_utf8_bounded() -> None:
    seed = _view()
    seed = replace(
        seed,
        current_statement="星光与故事♪" * 20_000,
        related_entity_refs=tuple(
            f"person:explicit-{index}" for index in range(64)
        ),
    )
    summary = initiative_seed_summary(seed)
    assert "current_statement" not in summary
    assert summary["content_bytes"] == len(
        initiative_seed_content(seed).encode("utf-8")
    )
    chunks: list[str] = []
    continuation = ""
    starts: list[int] = []
    while True:
        page = project_initiative_seed_content(
            seed,
            continuation=continuation,
        )
        assert len(str(page).encode("utf-8")) <= (
            INITIATIVE_CONTENT_PROJECTION_MAX_BYTES
        )
        starts.append(int(page["page_start_byte"]))
        chunks.append(str(page["content"]))
        continuation = str(page["continuation"])
        if not continuation:
            break

    assert "".join(chunks) == initiative_seed_content(seed)
    assert starts == sorted(set(starts))


@pytest.mark.asyncio
async def test_existing_durable_reencounter_repairs_receipt_without_requeue() -> None:
    service = LifeEngineService.__new__(LifeEngineService)
    service._state = LifeEngineState()
    receipts: list[dict[str, object]] = []

    async def _exists(_event_id: str) -> bool:
        return True

    async def _forbidden_queue(_event: object) -> None:
        raise AssertionError("an existing immutable event must not be queued again")

    async def _record(**kwargs: object) -> None:
        receipts.append(kwargs)

    service._initiative_life_event_exists = _exists  # type: ignore[method-assign]
    service._queue_pending_event = _forbidden_queue  # type: ignore[method-assign]
    service._proactive_authority = SimpleNamespace(
        record_reencounter_delivery=_record
    )

    await service._surface_initiative_reencounter(_view())

    assert len(receipts) == 1
    assert datetime.fromisoformat(str(receipts[0]["occurred_at"])).tzinfo is not None
    assert datetime.now(UTC).tzinfo is not None
