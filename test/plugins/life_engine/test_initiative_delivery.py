"""One-shot InitiativeSeed delivery into the durable Life Event path."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.chatter import LifeChatter
from plugins.life_engine.initiative.contracts import (
    InitiativeOutreachCommand,
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


def test_heartbeat_nucleus_pool_exposes_subject_initiative_tools() -> None:
    service = LifeEngineService.__new__(LifeEngineService)
    names = {tool.tool_name for tool in service._get_nucleus_tools()}
    assert {
        "nucleus_manage_initiative_seed",
        "nucleus_reachability",
        "nucleus_begin_outreach",
    } <= names
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

    loop_manager = SimpleNamespace(_wait_states={"qq-stream": object()})
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
    )
    await service._wake_stream_for_initiative(
        stream_id="qq-stream",
        platform="qq",
        command=command,
    )

    assert len(context.unread_messages) == 1
    message = context.unread_messages[0]
    assert message.message_id.startswith("initiative_outreach_")
    assert message.extra["initiative_outreach_occurrence_id"] == (
        command.occurrence_id
    )
    assert message.sender_id == "life_engine_initiative"
    assert not getattr(message, "target_user_id", "")
    assert not getattr(message, "target_user_name", "")
    assert "重新判断" in message.processed_plain_text
    assert "必须发送" not in message.processed_plain_text
    assert loop_manager._wait_states == {}


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
    service._initiative_authority = SimpleNamespace(
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
    service._initiative_authority = SimpleNamespace(
        record_reencounter_delivery=_record
    )

    await service._surface_initiative_reencounter(_view())

    assert len(receipts) == 1
    assert datetime.fromisoformat(str(receipts[0]["occurred_at"])).tzinfo is not None
    assert datetime.now(UTC).tzinfo is not None
