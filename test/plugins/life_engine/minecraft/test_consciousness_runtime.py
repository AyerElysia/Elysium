"""Production contracts for the dedicated Minecraft scene consciousness."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as PILImage

import plugins.life_engine.minecraft.consciousness as consciousness_module
from plugins.life_engine.minecraft.consciousness import (
    ElysiumMinecraftDecisionSource,
    MinecraftConsciousnessDecision,
    MinecraftConsciousnessOutputError,
    MinecraftConsciousnessPerception,
    MinecraftConsciousnessRuntime,
    MinecraftConsciousnessTurnContext,
    MinecraftSubjectContextBinding,
    MinecraftSubjectContextError,
    MinecraftTaskDirective,
    build_observation_projection,
)
from plugins.life_engine.minecraft.embodiment_contracts import (
    WorldObservation,
    utc_now,
)
from plugins.life_engine.service.subconscious_context import (
    RecentSubconsciousContext,
)
from src.kernel.llm import ROLE, Image


def _subject_snapshot(text: str = "subject projection") -> dict[str, Any]:
    encoded = text.encode("utf-8")
    return {
        "text": text,
        "source_digest": "1" * 64,
        "projection_sha256": hashlib.sha256(encoded).hexdigest(),
        "projection_version": 4,
        "projection_algorithm": "llm_semantic_subject_continuity",
        "projection_profile": "minecraft",
        "authority": "derived_non_authoritative",
        "sources": [
            {"path": "SOUL.md", "sha256": "2" * 64},
            {"path": "USER.md", "sha256": "3" * 64},
            {"path": "MEMORY.md", "sha256": "4" * 64},
        ],
        "budget": {
            "max_bytes": 16384,
            "delivered_bytes": len(encoded),
        },
    }


def _subject() -> MinecraftSubjectContextBinding:
    return MinecraftSubjectContextBinding.from_snapshot(
        _subject_snapshot(),
        expected_max_bytes=16384,
    )


def _observation(
    sequence: int = 1, facts: Mapping[str, Any] | None = None
) -> WorldObservation:
    return WorldObservation(
        observation_id=f"observation-{sequence}",
        instance_id="game-test",
        sequence=sequence,
        observed_at=utc_now(),
        facts=dict(facts or {"world_loaded": True, "player": {"health": 20}}),
        source="test-body",
    )


def _perception(sequence: int = 1) -> MinecraftConsciousnessPerception:
    return MinecraftConsciousnessPerception(
        observation=_observation(sequence),
        frame_bytes=None,
        recent_subconscious=RecentSubconsciousContext.empty(),
    )


def _context(*, turn_index: int = 1) -> MinecraftConsciousnessTurnContext:
    return MinecraftConsciousnessTurnContext(
        session_id="session-1",
        stream_id="game.minecraft.session-1",
        instance_id="minecraft-session-1",
        body_name="agent",
        session_goal="一起探索",
        turn_index=turn_index,
        wake_reasons=("session_started",),
        subject=_subject(),
        perception=_perception(turn_index),
        recent_outcomes=(),
    )


class _ScriptedDecisionSource:
    def __init__(self, decisions: list[MinecraftConsciousnessDecision]) -> None:
        self.decisions = list(decisions)
        self.calls = 0

    async def decide(
        self,
        context: MinecraftConsciousnessTurnContext,
    ) -> MinecraftConsciousnessDecision:
        self.calls += 1
        if self.decisions:
            decision = self.decisions.pop(0)
            return MinecraftConsciousnessDecision(
                decision_id=decision.decision_id,
                kind=decision.kind,
                turn_index=context.turn_index,
                authored_at=decision.authored_at,
                intention=decision.intention,
                speech=decision.speech,
                task=decision.task,
                reason=decision.reason,
                reconsider_after_seconds=decision.reconsider_after_seconds,
            )
        return MinecraftConsciousnessDecision(
            decision_id=f"wait-{context.turn_index}",
            kind="wait",
            turn_index=context.turn_index,
            authored_at=utc_now(),
            reason="先看看变化",
            reconsider_after_seconds=30.0,
        )


def _pursue(decision_id: str = "decision-1") -> MinecraftConsciousnessDecision:
    return MinecraftConsciousnessDecision(
        decision_id=decision_id,
        kind="pursue",
        turn_index=1,
        authored_at=utc_now(),
        intention="去河边看看并和同伴说说我看到了什么",
        reason="我想探索",
    )


def _wait(decision_id: str = "wait-1") -> MinecraftConsciousnessDecision:
    return MinecraftConsciousnessDecision(
        decision_id=decision_id,
        kind="wait",
        turn_index=1,
        authored_at=utc_now(),
        reason="我想先看看世界会怎么变化",
        reconsider_after_seconds=0.01,
    )


def _end(decision_id: str = "end-1") -> MinecraftConsciousnessDecision:
    return MinecraftConsciousnessDecision(
        decision_id=decision_id,
        kind="end_session",
        turn_index=2,
        authored_at=utc_now(),
        reason="今天先玩到这里",
    )


def test_subject_binding_fails_closed_on_wrong_identity_profile() -> None:
    snapshot = _subject_snapshot()
    snapshot["projection_profile"] = "voice_live"

    with pytest.raises(MinecraftSubjectContextError, match="not minecraft"):
        MinecraftSubjectContextBinding.from_snapshot(
            snapshot,
            expected_max_bytes=16384,
        )


def test_subject_binding_preserves_exact_utf8_snapshot_bytes() -> None:
    text = "爱莉的 Minecraft 主体投影\n"
    snapshot = _subject_snapshot(text)

    binding = MinecraftSubjectContextBinding.from_snapshot(
        snapshot,
        expected_max_bytes=16384,
    )

    assert binding.text == text
    assert binding.delivered_bytes == len(text.encode("utf-8"))
    assert binding.projection_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_large_observation_projection_is_explicit_and_bounded() -> None:
    observation = _observation(7, {"world_loaded": True, "large": "世界" * 500_000})

    rendered = build_observation_projection(observation, max_bytes=8192)
    payload = json.loads(rendered)

    assert len(rendered.encode("utf-8")) <= 8192
    assert payload["truncated"] is True
    assert payload["observation_id"] == observation.observation_id
    assert payload["source_bytes"] > 1_000_000
    assert len(payload["source_sha256"]) == 64


def test_large_observation_keeps_core_body_chat_and_task_facts_visible() -> None:
    """Large optional sensors cannot push current companion facts out of view."""

    observation = _observation(
        8,
        {
            "entities": [
                {
                    "id": index,
                    "name": "minecraft:zombie",
                    "description": "实体" * 200,
                }
                for index in range(200)
            ],
            "inventory": [{"slot": index, "item": "minecraft:stone"} for index in range(46)],
            "world_loaded": True,
            "world": {"mode": "multiplayer", "server_address": "host:25565"},
            "player": {"name": "Elysia", "x": 1, "y": 64, "z": 2},
            "chat": [{"username": "AyerElysia", "message": "爱莉，跟我来"}],
            "bot_tasks": {"high_level": {"active": None}},
        },
    )

    rendered = build_observation_projection(observation, max_bytes=4096)
    payload = json.loads(rendered)
    prefix = payload["utf8_prefix"]

    assert payload["truncated"] is True
    assert payload["prefix_order"] == "minecraft-core-facts-v1"
    assert '"name":"Elysia"' in prefix
    assert "爱莉，跟我来" in prefix
    assert '"bot_tasks"' in prefix
    assert len(rendered.encode("utf-8")) <= 4096


def test_turn_reference_never_contains_prompt_projection_text() -> None:
    context = _context()

    rendered = json.dumps(context.reference(), ensure_ascii=False)

    assert "subject projection" not in rendered
    assert "recent_subconscious" in rendered
    assert "facts" not in rendered
    assert "projection_sha256" in rendered


async def test_runtime_acts_without_any_chat_and_records_before_body_action() -> None:
    order: list[str] = []
    acted = asyncio.Event()
    sequence = 0

    async def perceive() -> MinecraftConsciousnessPerception:
        nonlocal sequence
        sequence += 1
        order.append("perceive")
        return _perception(sequence)

    async def record(
        decision: MinecraftConsciousnessDecision,
        context: MinecraftConsciousnessTurnContext,
    ) -> None:
        del context
        order.append(f"record:{decision.decision_id}")

    async def execute(intention: str) -> Mapping[str, Any]:
        order.append(f"act:{intention}")
        acted.set()
        return {"success": True, "conclusion": {"statement": "到了河边"}}

    async def end(_reason: str) -> None:
        raise AssertionError("session should not end")

    async def refresh(_reason: str) -> None:
        return None

    runtime = MinecraftConsciousnessRuntime(
        session_id="session-1",
        stream_id="game.minecraft.session-1",
        instance_id="minecraft-session-1",
        body_name="agent",
        session_goal="一起玩",
        subject=_subject(),
        decision_source=_ScriptedDecisionSource([_pursue()]),
        perception_source=perceive,
        execute_intent=execute,
        record_decision=record,
        request_end_session=end,
        refresh_presence=refresh,
        retry_base_seconds=0.01,
        retry_max_seconds=0.02,
    )
    runtime.start()
    await asyncio.wait_for(acted.wait(), timeout=1.0)
    await runtime.close()

    assert order[:3] == [
        "perceive",
        "record:decision-1",
        "act:去河边看看并和同伴说说我看到了什么",
    ]
    assert runtime.status()["turn_count"] >= 1


async def test_record_failure_retries_same_decision_without_duplicate_action() -> None:
    source = _ScriptedDecisionSource([_pursue("stable-decision")])
    recorded_ids: list[str] = []
    action_count = 0
    acted = asyncio.Event()

    async def record(
        decision: MinecraftConsciousnessDecision,
        _context: MinecraftConsciousnessTurnContext,
    ) -> None:
        recorded_ids.append(decision.decision_id)
        if len(recorded_ids) == 1:
            raise OSError("checkpoint temporarily unavailable")

    async def execute(_intention: str) -> Mapping[str, Any]:
        nonlocal action_count
        action_count += 1
        acted.set()
        return {"success": True}

    async def ignore(_reason: str) -> None:
        return None

    runtime = MinecraftConsciousnessRuntime(
        session_id="session-1",
        stream_id="game.minecraft.session-1",
        instance_id="minecraft-session-1",
        body_name="agent",
        session_goal="",
        subject=_subject(),
        decision_source=source,
        perception_source=lambda: asyncio.sleep(0, result=_perception()),
        execute_intent=execute,
        record_decision=record,
        request_end_session=ignore,
        refresh_presence=ignore,
        retry_base_seconds=0.01,
        retry_max_seconds=0.02,
    )
    runtime.start()
    await asyncio.wait_for(acted.wait(), timeout=1.0)
    await runtime.close()

    assert recorded_ids[:2] == ["stable-decision", "stable-decision"]
    assert source.calls >= 1
    assert action_count == 1


async def test_high_level_task_dispatch_yields_until_a_body_event_wakes_scene() -> None:
    dispatched = asyncio.Event()
    ended = asyncio.Event()
    perceptions = 0
    scene_decisions: list[MinecraftConsciousnessDecision] = []
    source = _ScriptedDecisionSource(
        [
            MinecraftConsciousnessDecision(
                decision_id="task-decision-1",
                kind="pursue",
                turn_index=1,
                authored_at=utc_now(),
                intention="跟着同伴一起走",
                speech="我来啦，我们一起走吧♪",
                task=MinecraftTaskDirective(
                    kind="follow_player",
                    arguments={"player": "Traveler", "distance": 3},
                ),
                reason="我想陪在同伴身边",
                reconsider_after_seconds=30.0,
            ),
            _end("task-end-2"),
        ]
    )

    async def perceive() -> MinecraftConsciousnessPerception:
        nonlocal perceptions
        perceptions += 1
        return _perception(perceptions)

    async def execute_scene(
        decision: MinecraftConsciousnessDecision,
    ) -> Mapping[str, Any]:
        scene_decisions.append(decision)
        dispatched.set()
        return {
            "success": True,
            "task_id": f"{decision.decision_id}:task",
            "receipts": [{"receipt_id": "task-start-receipt"}],
        }

    async def legacy_execute(_intention: str) -> Mapping[str, Any]:
        raise AssertionError("advertised high-level task must bypass legacy planner")

    async def ignore_record(
        _decision: MinecraftConsciousnessDecision,
        _context: MinecraftConsciousnessTurnContext,
    ) -> None:
        return None

    async def end(_reason: str) -> None:
        ended.set()

    async def refresh(_reason: str) -> None:
        return None

    runtime = MinecraftConsciousnessRuntime(
        session_id="session-1",
        stream_id="game.minecraft.session-1",
        instance_id="minecraft-session-1",
        body_name="bot",
        session_goal="一起玩",
        subject=_subject(),
        decision_source=source,
        perception_source=perceive,
        execute_intent=legacy_execute,
        execute_scene_decision=execute_scene,
        record_decision=ignore_record,
        request_end_session=end,
        refresh_presence=refresh,
        task_kinds=("follow_player",),
        retry_base_seconds=0.01,
        retry_max_seconds=0.02,
    )
    runtime.start()
    await asyncio.wait_for(dispatched.wait(), timeout=1.0)
    await asyncio.sleep(0.02)

    assert runtime.status()["phase"] == "waiting_for_body_event"
    assert source.calls == 1
    runtime.wake("minecraft.chat.received:event-1")
    await asyncio.wait_for(ended.wait(), timeout=1.0)
    await runtime.close()

    assert source.calls == 2
    assert perceptions == 2
    assert scene_decisions[0].speech == "我来啦，我们一起走吧♪"


async def test_runtime_honors_authored_wait_then_requests_session_end() -> None:
    source = _ScriptedDecisionSource([_wait(), _end()])
    recorded: list[str] = []
    refreshed: list[str] = []
    perceptions = 0
    ended = asyncio.Event()

    async def perceive() -> MinecraftConsciousnessPerception:
        nonlocal perceptions
        perceptions += 1
        return _perception(perceptions)

    async def record(
        decision: MinecraftConsciousnessDecision,
        _context: MinecraftConsciousnessTurnContext,
    ) -> None:
        recorded.append(decision.kind)

    async def execute(_intention: str) -> Mapping[str, Any]:
        raise AssertionError("wait/end decisions must not touch the body")

    async def end(reason: str) -> None:
        assert reason == "今天先玩到这里"
        ended.set()

    async def refresh(reason: str) -> None:
        refreshed.append(reason)

    runtime = MinecraftConsciousnessRuntime(
        session_id="session-1",
        stream_id="game.minecraft.session-1",
        instance_id="minecraft-session-1",
        body_name="agent",
        session_goal="",
        subject=_subject(),
        decision_source=source,
        perception_source=perceive,
        execute_intent=execute,
        record_decision=record,
        request_end_session=end,
        refresh_presence=refresh,
        retry_base_seconds=0.01,
        retry_max_seconds=0.02,
    )
    runtime.start()
    await asyncio.wait_for(ended.wait(), timeout=1.0)
    await runtime.close()

    assert recorded == ["wait", "end_session"]
    assert refreshed == ["minecraft_consciousness_wait"]
    assert perceptions == 2
    assert source.calls == 2


def test_model_parser_keeps_open_intention_but_rejects_unknown_control_fields() -> None:
    source = ElysiumMinecraftDecisionSource(
        "agent",
        observation_max_bytes=8192,
        min_wait_seconds=2,
        max_wait_seconds=45,
    )
    context = _context()

    parsed = source._parse_decision(
        json.dumps(
            {
                "decision": {
                    "kind": "pursue",
                    "intention": "随便转转，也许搭一个只属于今晚的小亭子",
                    "reason": "这是我现在想做的",
                }
            },
            ensure_ascii=False,
        ),
        context,
    )

    assert parsed.intention == "随便转转，也许搭一个只属于今晚的小亭子"
    with pytest.raises(MinecraftConsciousnessOutputError, match="unknown fields"):
        source._parse_decision(
            '{"decision":{"kind":"wait","reason":"看看","mood":"happy",'
            '"reconsider_after_seconds":5}}',
            context,
        )


def test_model_parser_requires_an_advertised_typed_task_for_pursue() -> None:
    source = ElysiumMinecraftDecisionSource(
        "agent",
        observation_max_bytes=8192,
        min_wait_seconds=2,
        max_wait_seconds=45,
    )
    base = _context()
    context = MinecraftConsciousnessTurnContext(
        session_id=base.session_id,
        stream_id=base.stream_id,
        instance_id=base.instance_id,
        body_name="bot",
        session_goal=base.session_goal,
        turn_index=base.turn_index,
        wake_reasons=base.wake_reasons,
        subject=base.subject,
        perception=base.perception,
        recent_outcomes=(),
        task_kinds=("follow_player",),
    )

    parsed = source._parse_decision(
        json.dumps(
            {
                "decision": {
                    "kind": "pursue",
                    "intention": "跟着同伴继续探索",
                    "speech": "等等我呀♪",
                    "task": {
                        "kind": "follow_player",
                        "arguments": {"player": "Traveler", "distance": 3},
                        "replace_current": False,
                    },
                    "reason": "我想和同伴一起走",
                    "reconsider_after_seconds": 6,
                }
            },
            ensure_ascii=False,
        ),
        context,
    )

    assert parsed.task is not None
    assert parsed.task.kind == "follow_player"
    assert parsed.speech == "等等我呀♪"
    with pytest.raises(MinecraftConsciousnessOutputError, match="requires one"):
        source._parse_decision(
            '{"decision":{"kind":"pursue","intention":"跟着同伴",'
            '"reason":"一起走"}}',
            context,
        )


async def test_model_turn_delivers_identity_observation_and_native_pixels_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The formal model path proves its Text parts and carries raw image media."""

    class _Response:
        message = json.dumps(
            {
                "decision": {
                    "kind": "wait",
                    "reason": "我想先看清楚周围",
                    "reconsider_after_seconds": 5,
                }
            },
            ensure_ascii=False,
        )
        reasoning_content = "我先观察环境，再决定是否行动"
        request_record_id = "minecraft-request-1"

        def __init__(self, deliveries: dict[str, str]) -> None:
            self._deliveries = deliveries

        def __await__(self):
            async def collect() -> _Response:
                return self

            return collect().__await__()

        def effective_context_receipt(self, delivery_id: str) -> Any:
            text = self._deliveries[delivery_id]
            encoded = text.encode("utf-8")
            return SimpleNamespace(
                exact_present=True,
                effective_utf8_bytes=len(encoded),
                effective_sha256=hashlib.sha256(encoded).hexdigest(),
            )

    class _Request:
        def __init__(self) -> None:
            self.payloads: list[Any] = []
            self.deliveries: dict[str, str] = {}

        def add_payload(self, payload: Any) -> None:
            self.payloads.append(payload)

        def register_context_delivery(
            self,
            delivery_id: str,
            text: str,
            *,
            marker: str,
        ) -> None:
            assert marker in text
            self.deliveries[delivery_id] = text

        async def send(self, *, stream: bool) -> _Response:
            assert stream is False
            return _Response(self.deliveries)

    request = _Request()
    monkeypatch.setattr(
        consciousness_module,
        "get_model_set_by_task",
        lambda _task: [{"model_identifier": "test"}],
    )
    monkeypatch.setattr(
        consciousness_module,
        "create_llm_request",
        lambda **_kwargs: request,
    )
    frame = io.BytesIO()
    PILImage.new("RGB", (16, 16), color=(20, 80, 150)).save(frame, format="JPEG")
    context = _context()
    context = MinecraftConsciousnessTurnContext(
        session_id=context.session_id,
        stream_id=context.stream_id,
        instance_id=context.instance_id,
        body_name=context.body_name,
        session_goal=context.session_goal,
        turn_index=context.turn_index,
        wake_reasons=context.wake_reasons,
        subject=context.subject,
        perception=MinecraftConsciousnessPerception(
            observation=context.perception.observation,
            frame_bytes=frame.getvalue(),
            recent_subconscious=RecentSubconsciousContext.empty(),
        ),
        recent_outcomes=(),
    )
    source = ElysiumMinecraftDecisionSource(
        "agent",
        observation_max_bytes=8192,
        min_wait_seconds=2,
        max_wait_seconds=45,
    )

    decision = await source.decide(context)

    assert decision.kind == "wait"
    record = decision.to_record()
    assert record["transport_request_id"] == "minecraft-request-1"
    assert record["provider_reasoning_content"] == (
        "我先观察环境，再决定是否行动"
    )
    assert record["assistant_message"] == _Response.message
    assert len(request.deliveries) == 2
    user_payload = next(item for item in request.payloads if item.role == ROLE.USER)
    assert any(isinstance(part, Image) for part in user_payload.content)


async def test_invalid_model_turn_is_recorded_before_the_parser_rejects_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real but invalid scene generation remains part of conscious history."""

    class _Response:
        message = "not-json"
        reasoning_content = "我还没有整理成协议要求的决定"
        request_record_id = "minecraft-invalid-request"

        def __init__(self, deliveries: dict[str, str]) -> None:
            self._deliveries = deliveries

        def __await__(self):
            async def collect() -> _Response:
                return self

            return collect().__await__()

        def effective_context_receipt(self, delivery_id: str) -> Any:
            text = self._deliveries[delivery_id]
            encoded = text.encode("utf-8")
            return SimpleNamespace(
                exact_present=True,
                effective_utf8_bytes=len(encoded),
                effective_sha256=hashlib.sha256(encoded).hexdigest(),
            )

    class _Request:
        def __init__(self) -> None:
            self.deliveries: dict[str, str] = {}

        def add_payload(self, _payload: Any) -> None:
            return

        def register_context_delivery(
            self,
            delivery_id: str,
            text: str,
            *,
            marker: str,
        ) -> None:
            assert marker in text
            self.deliveries[delivery_id] = text

        async def send(self, *, stream: bool) -> _Response:
            assert stream is False
            return _Response(self.deliveries)

    request = _Request()
    recorded: list[tuple[Any, MinecraftConsciousnessTurnContext]] = []

    async def record_failed(
        response: Any,
        context: MinecraftConsciousnessTurnContext,
    ) -> None:
        recorded.append((response, context))

    monkeypatch.setattr(
        consciousness_module,
        "get_model_set_by_task",
        lambda _task: [{"model_identifier": "test"}],
    )
    monkeypatch.setattr(
        consciousness_module,
        "create_llm_request",
        lambda **_kwargs: request,
    )
    context = _context()
    source = ElysiumMinecraftDecisionSource(
        "agent",
        observation_max_bytes=8192,
        min_wait_seconds=2,
        max_wait_seconds=45,
        failed_turn_recorder=record_failed,
    )

    with pytest.raises(
        MinecraftConsciousnessOutputError,
        match="not strict JSON",
    ):
        await source.decide(context)

    assert len(recorded) == 1
    response, recorded_context = recorded[0]
    assert recorded_context is context
    assert response.request_record_id == "minecraft-invalid-request"
