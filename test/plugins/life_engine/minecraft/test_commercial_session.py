"""Lifecycle tests for the evidence-driven Minecraft session."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from plugins.life_engine.minecraft.embodiment_contracts import (
    ActionCommand,
    ActionReceipt,
    EmbodiedIntent,
    IntentConclusion,
    PlannerTurn,
    WorldObservation,
    utc_now,
)
from plugins.life_engine.minecraft.launcher import LaunchResult, MCConfig
from plugins.life_engine.minecraft.session import MinecraftSession
from plugins.life_engine.service.consciousness import ConsciousnessRegistry


class _Launcher:
    """Pretend an exact configured installation was launched."""

    async def check_installation(self) -> dict[str, Any]:
        """Return complete positive installation evidence."""

        return {"exists": True, "bat_exists": True}

    async def launch(self) -> LaunchResult:
        """Return a successful launch dispatch receipt."""

        return LaunchResult(success=True)


class _Bridge:
    """In-memory authenticated body endpoint used by session tests."""

    def __init__(self) -> None:
        """Create a disconnected body with an empty observation sequence."""

        self.connected = False
        self.instance_id = "minecraft-test"
        self.capabilities = ("modded.operation",)
        self.sequence = 0
        self.interruptions: list[tuple[str, str]] = []
        self.closed = False

    async def open(self) -> None:
        """Mark the endpoint connected."""

        self.connected = True

    async def close(self) -> None:
        """Mark the endpoint closed."""

        self.connected = False
        self.closed = True

    async def observe(self, after_sequence: int | None = None) -> WorldObservation:
        """Emit the next factual observation."""

        self.sequence += 1
        return WorldObservation(
            instance_id=self.instance_id,
            sequence=self.sequence,
            observed_at=utc_now(),
            source="test-bridge",
            facts={"position": {"x": self.sequence, "y": 64, "z": 0}},
        )

    async def act(self, command: ActionCommand) -> ActionReceipt:
        """Return a terminal dispatch receipt for one command."""

        return ActionReceipt(
            command_id=command.command_id,
            intent_id=command.intent_id,
            accepted=True,
            completed=True,
            interrupted=False,
            facts={"operation_dispatched": command.operation},
            observation_sequence=self.sequence,
        )

    async def interrupt(self, intent_id: str, reason: str) -> None:
        """Record control release for the active intention."""

        self.interruptions.append((intent_id, reason))


class _Planner:
    """Issue one operation and then make an evidence-backed conclusion."""

    def __init__(self) -> None:
        self.contexts: list[dict[str, Any]] = []

    async def decide(
        self,
        intent: EmbodiedIntent,
        observations: tuple[WorldObservation, ...],
        receipts: tuple[ActionReceipt, ...],
    ) -> PlannerTurn:
        """Act once, then cite both receipt and post-action observation."""

        self.contexts.append(dict(intent.context))

        if not receipts:
            return PlannerTurn(
                command=ActionCommand(
                    intent_id=intent.intent_id,
                    intent_revision=intent.revision,
                    operation="modded.operation",
                    parameters={"goal": intent.text},
                    based_on_observation=observations[-1].observation_id,
                )
            )
        return PlannerTurn(
            conclusion=IntentConclusion(
                statement="The operation was dispatched and a later position was observed.",
                evidence_ids=(
                    receipts[-1].receipt_id,
                    observations[-1].observation_id,
                ),
            )
        )


async def _started_session(
    tmp_path: Path,
) -> tuple[MinecraftSession, _Bridge, ConsciousnessRegistry, list[dict[str, Any]]]:
    """Create and start a fully integrated in-memory session."""

    registry = ConsciousnessRegistry()
    observations: list[dict[str, Any]] = []

    async def report_world_observation(
        report: str,
        **kwargs: Any,
    ) -> dict[str, str]:
        observations.append({"report": report, **kwargs})
        return {"assertion_id": f"assertion-{len(observations)}"}

    def prepare_perception(instance_id: str) -> Any:
        return SimpleNamespace(
            instance_id=instance_id,
            content="shared-presence",
        )

    def commit_perception(prepared: Any) -> None:
        observations.append({"perception_commit": prepared})

    bridge = _Bridge()
    config = MCConfig(mc_home=tmp_path, bridge_ready_timeout_seconds=1)
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=config,
        consciousness_registry=registry,
        prepare_perception=prepare_perception,
        commit_perception=commit_perception,
        report_world_observation=report_world_observation,
    )
    session._launcher = _Launcher()

    async def wait_for_bridge(profile: Any) -> _Bridge:
        """Return the exact selected test bridge."""

        return bridge

    session._wait_for_bridge = wait_for_bridge
    result = await session.start(goal="walk together", body_name="agent")
    assert result["success"] is True
    return session, bridge, registry, observations


async def test_session_registers_and_terminates_independent_consciousness(
    tmp_path: Path,
) -> None:
    """Game start and stop own a distinct registry and WorldState lifecycle."""

    session, bridge, registry, observations = await _started_session(tmp_path)
    instance_id = session.state.consciousness_instance_id
    stream_id = session.state.stream_id

    instance = registry.get(instance_id)
    assert instance is not None and instance.is_active
    assert instance.stream_ids == [stream_id]
    assert observations[-1]["subject"] == stream_id
    assert observations[-1]["source_instance_id"] == instance_id

    result = await session.stop()

    assert result["success"] is True
    assert registry.get(instance_id).status == "terminated"
    assert observations[-1]["report"] == "session ended"
    assert bridge.closed is True


async def test_session_returns_full_intention_evidence(tmp_path: Path) -> None:
    """The public session API returns receipts and observations, not guessed success."""

    session, _, _, _ = await _started_session(tmp_path)
    planner = _Planner()
    session._planner = planner

    result = await session.do_intent("walk to x=3")

    assert result["success"] is True
    assert result["conclusion"]["evidence_ids"]
    assert len(result["receipts"]) == 1
    assert len(result["observations"]) == 2
    assert session.state.conclusions == [result["conclusion"]]
    assert planner.contexts[0]["transient_world_perception"] == "shared-presence"
