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


class _AsyncRegistry:
    """Expose the synchronous registry through awaitable Presence callbacks."""

    def __init__(self, *, fail_touch: bool = False) -> None:
        self._inner = ConsciousnessRegistry()
        self.fail_touch = fail_touch
        self.calls: list[str] = []

    async def register(self, instance: Any) -> None:
        self.calls.append("register")
        self._inner.register(instance)

    async def touch(self, instance_id: str, **kwargs: Any) -> None:
        self.calls.append("touch")
        if self.fail_touch:
            raise RuntimeError("async Presence touch failed")
        self._inner.touch(instance_id, **kwargs)

    async def terminate(self, instance_id: str, **kwargs: Any) -> None:
        self.calls.append("terminate")
        self._inner.terminate(instance_id, **kwargs)

    def get(self, instance_id: str) -> Any:
        return self._inner.get(instance_id)


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


async def test_session_awaits_async_presence_lifecycle_callbacks(
    tmp_path: Path,
) -> None:
    """Async register, touch, terminate, and save complete before API success."""

    registry = _AsyncRegistry()
    saves: list[str] = []

    async def save_registry() -> None:
        saves.append("save")

    bridge = _Bridge()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=MCConfig(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
        consciousness_registry=registry,
        save_consciousness_registry=save_registry,
    )
    session._launcher = _Launcher()

    async def wait_for_bridge(profile: Any) -> _Bridge:
        return bridge

    session._wait_for_bridge = wait_for_bridge

    started = await session.start(body_name="agent")
    assert started["success"] is True
    assert registry.calls == ["register"]
    assert saves == ["save"]

    await session._on_trace("test.trace", {"source": "test"})
    assert registry.calls == ["register", "touch"]
    assert saves == ["save", "save"]

    stopped = await session.stop()
    assert stopped["success"] is True
    assert registry.calls == ["register", "touch", "terminate"]
    assert saves == ["save", "save", "save"]


async def test_session_reports_async_presence_touch_failure(
    tmp_path: Path,
) -> None:
    """A failed durable Presence touch cannot be reported as intent success."""

    registry = _AsyncRegistry(fail_touch=True)
    bridge = _Bridge()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=MCConfig(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
        consciousness_registry=registry,
    )
    session._launcher = _Launcher()

    async def wait_for_bridge(profile: Any) -> _Bridge:
        return bridge

    session._wait_for_bridge = wait_for_bridge
    started = await session.start(body_name="agent")
    assert started["success"] is True
    session._planner = _Planner()

    result = await session.do_intent("walk to x=3")

    assert result["success"] is False
    assert result["error"] == "async Presence touch failed"
    registry.fail_touch = False
    await session.stop()


async def test_session_cleans_up_when_async_presence_save_fails(
    tmp_path: Path,
) -> None:
    """Startup returns failure and releases the bridge after durable save failure."""

    registry = _AsyncRegistry()
    bridge = _Bridge()

    async def save_registry() -> None:
        raise RuntimeError("async Presence save failed")

    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=MCConfig(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
        consciousness_registry=registry,
        save_consciousness_registry=save_registry,
    )
    session._launcher = _Launcher()

    async def wait_for_bridge(profile: Any) -> _Bridge:
        return bridge

    session._wait_for_bridge = wait_for_bridge

    result = await session.start(body_name="agent")

    assert result["success"] is False
    assert "async Presence save failed" in result["error"]
    assert session.is_active is False
    assert bridge.closed is True


async def test_session_awaits_async_perception_prepare_and_commit(
    tmp_path: Path,
) -> None:
    """Async perception preparation is consumed and its cursor is committed."""

    prepared = SimpleNamespace(content="async shared perception")
    calls: list[str] = []

    async def prepare_perception(instance_id: str) -> Any:
        calls.append(f"prepare:{instance_id}")
        return prepared

    async def commit_perception(value: Any) -> None:
        assert value is prepared
        calls.append("commit")

    bridge = _Bridge()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=MCConfig(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
        prepare_perception=prepare_perception,
        commit_perception=commit_perception,
    )
    session._launcher = _Launcher()

    async def wait_for_bridge(profile: Any) -> _Bridge:
        return bridge

    session._wait_for_bridge = wait_for_bridge
    started = await session.start(body_name="agent")
    assert started["success"] is True
    planner = _Planner()
    session._planner = planner

    result = await session.do_intent("walk to x=3")

    assert result["success"] is True
    assert planner.contexts[0]["transient_world_perception"] == prepared.content
    assert calls == [
        f"prepare:{session.state.consciousness_instance_id}",
        "commit",
    ]
    await session.stop()


async def test_session_reports_async_perception_commit_failure(
    tmp_path: Path,
) -> None:
    """A failed async cursor commit cannot be reported as intent success."""

    async def prepare_perception(instance_id: str) -> Any:
        return SimpleNamespace(content=f"prepared:{instance_id}")

    async def commit_perception(value: Any) -> None:
        raise RuntimeError("async perception commit failed")

    bridge = _Bridge()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=MCConfig(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
        prepare_perception=prepare_perception,
        commit_perception=commit_perception,
    )
    session._launcher = _Launcher()

    async def wait_for_bridge(profile: Any) -> _Bridge:
        return bridge

    session._wait_for_bridge = wait_for_bridge
    started = await session.start(body_name="agent")
    assert started["success"] is True
    session._planner = _Planner()

    result = await session.do_intent("walk to x=3")

    assert result["success"] is False
    assert result["error"] == "async perception commit failed"
    await session.stop()
