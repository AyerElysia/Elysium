"""Tests for strict open-vocabulary Minecraft model planning."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.minecraft import model_planner as model_planner_module
from plugins.life_engine.minecraft.embodiment_contracts import (
    EmbodiedIntent,
    PerceptionReference,
    WorldObservation,
    utc_now,
)
from plugins.life_engine.minecraft.model_planner import (
    ElysiumModelDecisionSource,
    JsonIntentPlanner,
    PlannerOutputError,
)
from plugins.life_engine.minecraft.session import MinecraftSession, SessionState
from src.kernel.llm import EffectiveContextReceipt, Text


def _observation() -> WorldObservation:
    """Create one factual planner observation."""

    return WorldObservation(
        instance_id="world-test",
        sequence=1,
        observed_at=utc_now(),
        source="test",
        facts={"crosshair": {"kind": "block", "block": "minecraft:oak_log"}},
    )


class _ModelResponse:
    """Minimal awaitable response with one effective delivery receipt."""

    def __init__(
        self,
        receipt: EffectiveContextReceipt | None,
    ) -> None:
        self.message = '{"conclusion":{"statement":"observed","evidence_ids":[]}}'
        self.request_record_id = 47
        self.reasoning_content = "根据观察与动作回执选择下一步"
        self._receipt = receipt

    def __await__(self):
        async def _complete() -> _ModelResponse:
            return self

        return _complete().__await__()

    def effective_context_receipt(
        self,
        delivery_id: str,
    ) -> EffectiveContextReceipt | None:
        """Return the fake receipt only for its registered identity."""

        if self._receipt is None or delivery_id != self._receipt.delivery_id:
            return None
        return self._receipt


class _ModelRequest:
    """Capture exact planner payload parts without contacting a provider."""

    def __init__(self, *, exact: bool) -> None:
        self.payloads: list[Any] = []
        self.expectation: tuple[str, str, str | None] | None = None
        self._exact = exact

    def add_payload(self, payload: Any) -> None:
        """Capture one request payload."""

        self.payloads.append(payload)

    def register_context_delivery(
        self,
        delivery_id: str,
        expected_text: str,
        *,
        marker: str | None = None,
    ) -> None:
        """Capture the delivery expectation registered by Minecraft."""

        self.expectation = (delivery_id, expected_text, marker)

    async def send(self, *, stream: bool) -> _ModelResponse:
        """Return an exact or deliberately failed effective receipt."""

        assert stream is False
        if self.expectation is None:
            return _ModelResponse(None)
        delivery_id, expected_text, _marker = self.expectation
        encoded = expected_text.encode("utf-8")
        digest = sha256(encoded).hexdigest()
        return _ModelResponse(
            EffectiveContextReceipt(
                delivery_id=delivery_id,
                exact_present=self._exact,
                expected_utf8_bytes=len(encoded),
                expected_sha256=digest,
                effective_utf8_bytes=(
                    len(encoded) if self._exact else len(encoded) - 1
                ),
                effective_sha256=(
                    digest if self._exact else sha256(encoded[:-1]).hexdigest()
                ),
            )
        )


def _perception_intent(content: str) -> EmbodiedIntent:
    """Build one intent with PreparedPerception-v2 provenance."""

    encoded = content.encode("utf-8")
    prepared = SimpleNamespace(
        instance_id="minecraft:test",
        projection_kind="world_perception",
        from_position=3,
        through_position=5,
        source_frontier=7,
        cursor_revision=2,
        content=content,
        assertion_ids=("assertion-1",),
        change_positions=(4, 5),
        delivery_id="delivery-minecraft-test",
        projection_sha256=sha256(encoded).hexdigest(),
        algorithm_version="world-perception-v2",
        delivered_bytes=len(encoded),
    )
    reference = PerceptionReference.from_prepared(prepared)
    return EmbodiedIntent(
        text="inspect the world",
        body_name="agent",
        durable_context={"session_id": "session-test"},
        transient_prompt_context={"world_perception": content},
        perception_reference=reference,
    )


def _decision_document(intent: EmbodiedIntent) -> dict[str, Any]:
    """Build the transport document consumed by the configured model source."""

    return {
        "planner_guidance": "test",
        "advertised_operations": ["observation.wait"],
        "intent": intent.to_prompt(),
        "observations": [],
        "receipts": [],
    }


async def test_planner_preserves_open_operation_and_parameters() -> None:
    """A live advertised operation passes through without keyword translation."""

    captured: dict[str, Any] = {}

    async def source(document: dict[str, Any]) -> str:
        """Capture full input and return one command."""

        captured.update(document)
        return json.dumps(
            {
                "command": {
                    "operation": "modded.executor.operation",
                    "parameters": {"arbitrary_mod_item": "example:crystal"},
                    "based_on_observation": document["observations"][-1][
                        "observation_id"
                    ],
                    "timeout_seconds": 8,
                }
            }
        )

    planner = JsonIntentPlanner(
        source,
        lambda: ("modded.executor.operation",),
        "runtime supplied contract",
    )
    intent = EmbodiedIntent(text="interact with the crystal", body_name="agent")

    turn = await planner.decide(intent, (_observation(),), ())

    assert turn.command is not None
    assert turn.command.operation == "modded.executor.operation"
    assert turn.command.parameters == {"arbitrary_mod_item": "example:crystal"}
    assert captured["intent"]["text"] == intent.text


async def test_planner_rejects_unadvertised_operation() -> None:
    """A model cannot smuggle an operation unsupported by the selected body."""

    async def source(document: dict[str, Any]) -> str:
        """Return an operation absent from live capabilities."""

        return '{"command":{"operation":"unknown","parameters":{}}}'

    planner = JsonIntentPlanner(source, lambda: ("known",), "contract")

    with pytest.raises(PlannerOutputError):
        await planner.decide(
            EmbodiedIntent(text="act", body_name="agent"),
            (_observation(),),
            (),
        )


async def test_planner_rejects_markdown_wrapped_json() -> None:
    """Malformed output is surfaced rather than converted to a guessed action."""

    async def source(document: dict[str, Any]) -> str:
        """Return a format forbidden by the planner contract."""

        return '```json\n{"command":{"operation":"known","parameters":{}}}\n```'

    planner = JsonIntentPlanner(source, lambda: ("known",), "contract")

    with pytest.raises(PlannerOutputError):
        await planner.decide(
            EmbodiedIntent(text="act", body_name="agent"),
            (_observation(),),
            (),
        )


async def test_model_source_proves_exact_transient_perception_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Projection text is a standalone tracked Text and never enters durable JSON."""

    request = _ModelRequest(exact=True)
    monkeypatch.setattr(
        model_planner_module,
        "get_model_set_by_task",
        lambda _name: [{"model_identifier": "test"}],
    )
    monkeypatch.setattr(
        model_planner_module,
        "create_llm_request",
        lambda **_kwargs: request,
    )
    projection = "WORLD-PROJECTION-UNIQUE\n" + ("observed stone\n" * 2048)
    intent = _perception_intent(projection)
    source = ElysiumModelDecisionSource("minecraft")

    await source(_decision_document(intent))

    assert request.expectation is not None
    delivery_id, expected_text, marker = request.expectation
    assert delivery_id == intent.perception_reference.delivery_id
    assert expected_text == projection
    assert marker and projection.startswith(marker)
    user_texts = [
        part.text for part in request.payloads[-1].content if isinstance(part, Text)
    ]
    assert user_texts.count(projection) == 1
    durable_document = json.loads(user_texts[0])
    assert "transient_prompt_context" not in durable_document["intent"]
    assert projection not in user_texts[0]
    proof = source.consume_context_delivery(delivery_id)
    assert proof is not None
    assert proof.projection_sha256 == sha256(projection.encode("utf-8")).hexdigest()
    assert proof.delivered_bytes == len(projection.encode("utf-8"))
    assert proof.transport_request_id == "47"


async def test_model_source_records_full_generation_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """具身 planner 的原始生成必须先进入统一活动链。"""

    request = _ModelRequest(exact=True)
    monkeypatch.setattr(
        model_planner_module,
        "get_model_set_by_task",
        lambda _name: [{"model_identifier": "test"}],
    )
    monkeypatch.setattr(
        model_planner_module,
        "create_llm_request",
        lambda **_kwargs: request,
    )
    recorded: list[tuple[Any, dict[str, Any]]] = []

    async def record_activity(
        response: Any,
        document: dict[str, Any],
    ) -> None:
        recorded.append((response, document))

    intent = _perception_intent("bounded perception")
    document = _decision_document(intent)
    source = ElysiumModelDecisionSource(
        "minecraft",
        activity_recorder=record_activity,
    )

    await source(document)

    assert len(recorded) == 1
    assert recorded[0][0].reasoning_content == (
        "根据观察与动作回执选择下一步"
    )
    assert recorded[0][0].message == (
        '{"conclusion":{"statement":"observed","evidence_ids":[]}}'
    )
    assert recorded[0][1] is document


async def test_session_attributes_embodiment_planner_turn_to_active_instance(
    tmp_path: Path,
) -> None:
    """具身规划生成必须保留 Minecraft instance/stream 与稳定 intent 身份。"""

    turns: list[dict[str, Any]] = []

    async def record_turn(**kwargs: Any) -> dict[str, str]:
        turns.append(dict(kwargs))
        return {}

    session = MinecraftSession(
        tmp_path,
        record_conscious_model_turn=record_turn,
    )
    session._state = SessionState(
        active=True,
        session_id="session-1",
        stream_id="game.minecraft.session-1",
        consciousness_instance_id="minecraft_session-1",
    )
    intent = _perception_intent("bounded perception")

    await session._record_minecraft_planner_activity(
        _ModelResponse(None),
        _decision_document(intent),
    )

    assert len(turns) == 1
    assert turns[0]["stream_id"] == "game.minecraft.session-1"
    assert turns[0]["source_instance_id"] == "minecraft_session-1"
    assert turns[0]["transport_request_id"] == "47"
    assert turns[0]["provider_reasoning_content"] == (
        "根据观察与动作回执选择下一步"
    )
    assert turns[0]["assistant_message"] == (
        '{"conclusion":{"statement":"observed","evidence_ids":[]}}'
    )
    assert turns[0]["surface"] == "minecraft_embodiment_planner"
    assert intent.intent_id in turns[0]["turn_occurrence_id"]


async def test_model_source_sends_recent_subconscious_as_transient_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recent continuity reaches the current request but not durable JSON."""

    request = _ModelRequest(exact=True)
    monkeypatch.setattr(
        model_planner_module,
        "get_model_set_by_task",
        lambda _name: [{"model_identifier": "test"}],
    )
    monkeypatch.setattr(
        model_planner_module,
        "create_llm_request",
        lambda **_kwargs: request,
    )
    recent = "RECENT-SUBCONSCIOUS-UNIQUE\nheartbeat considered shelter"
    intent = EmbodiedIntent(
        text="continue playing",
        body_name="agent",
        durable_context={
            "session_id": "session-test",
            "recent_subconscious_reference": {
                "schema": "minecraft.recent_subconscious_reference.v1",
                "projection_sha256": sha256(recent.encode()).hexdigest(),
                "delivered_bytes": len(recent.encode()),
            },
        },
        transient_prompt_context={"recent_subconscious_context": recent},
    )
    source = ElysiumModelDecisionSource("minecraft")

    await source(_decision_document(intent))

    assert request.expectation is None
    user_texts = [
        part.text for part in request.payloads[-1].content if isinstance(part, Text)
    ]
    assert user_texts.count(recent) == 1
    durable_document = json.loads(user_texts[0])
    durable_intent = durable_document["intent"]
    assert "transient_prompt_context" not in durable_intent
    assert recent not in user_texts[0]
    assert durable_intent["durable_context"]["recent_subconscious_reference"][
        "delivered_bytes"
    ] == len(recent.encode())
    assert any("past context" in text for text in user_texts)


async def test_model_source_rejects_trimmed_transient_perception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-exact effective request cannot leave a commit-eligible proof."""

    request = _ModelRequest(exact=False)
    monkeypatch.setattr(
        model_planner_module,
        "get_model_set_by_task",
        lambda _name: [{"model_identifier": "test"}],
    )
    monkeypatch.setattr(
        model_planner_module,
        "create_llm_request",
        lambda **_kwargs: request,
    )
    intent = _perception_intent("WORLD-PROJECTION-TRIMMED\nobserved grass")
    source = ElysiumModelDecisionSource("minecraft")

    with pytest.raises(RuntimeError, match="absent, duplicated, or trimmed"):
        await source(_decision_document(intent))

    assert (
        source.consume_context_delivery(intent.perception_reference.delivery_id) is None
    )
