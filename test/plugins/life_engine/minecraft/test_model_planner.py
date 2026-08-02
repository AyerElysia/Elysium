"""Tests for strict open-vocabulary Minecraft model planning."""

from __future__ import annotations

import json
from typing import Any

import pytest

from plugins.life_engine.minecraft.embodiment_contracts import (
    EmbodiedIntent,
    WorldObservation,
    utc_now,
)
from plugins.life_engine.minecraft.model_planner import (
    JsonIntentPlanner,
    PlannerOutputError,
)


def _observation() -> WorldObservation:
    """Create one factual planner observation."""

    return WorldObservation(
        instance_id="world-test",
        sequence=1,
        observed_at=utc_now(),
        source="test",
        facts={"crosshair": {"kind": "block", "block": "minecraft:oak_log"}},
    )


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
