"""Lifecycle tests for the evidence-driven Minecraft session."""

from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import plugins.life_engine.minecraft.session as session_module
from plugins.life_engine.minecraft.consciousness import (
    MinecraftConsciousnessDecision,
    MinecraftConsciousnessTurnContext,
)
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
from plugins.life_engine.minecraft.trace_projection import (
    WORLD_TRACE_RECEIPT_MAX_BYTES,
    world_trace_receipt_size,
)
from plugins.life_engine.service.consciousness import ConsciousnessRegistry
from plugins.life_engine.service.subconscious_context import RecentSubconsciousContext

_DURABLE_SCENE_LOOP_TIMEOUT_SECONDS = 10.0


def _body_only_config(**kwargs: Any) -> MCConfig:
    """Keep legacy body tests separate from the new scene-runtime contract."""

    return MCConfig(consciousness_enabled=False, **kwargs)


def _recent_subconscious(content: str) -> RecentSubconsciousContext:
    """Create one bounded recent-subconscious projection for session tests."""

    encoded = content.encode("utf-8")
    digest = sha256(encoded).hexdigest()
    return RecentSubconsciousContext(
        content=content,
        event_ids=("heartbeat-4", "tool-call-5", "tool-result-6"),
        from_sequence=4,
        through_sequence=6,
        group_count=2,
        source_group_count=3,
        omitted_group_count=1,
        delivered_bytes=len(encoded),
        projection_sha256=digest,
        truncated=True,
    )


def _minecraft_subject_snapshot() -> dict[str, Any]:
    """Return one valid immutable subject projection for integration tests."""

    text = "# Subject Context Projection\n\n爱莉作为同一主体继续进入 Minecraft。\n"
    encoded = text.encode("utf-8")
    return {
        "text": text,
        "source_digest": "1" * 64,
        "projection_sha256": sha256(encoded).hexdigest(),
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


class _Launcher:
    """Pretend an exact configured installation was launched."""

    async def check_installation(self) -> dict[str, Any]:
        """Return complete positive installation evidence."""

        return {
            "exists": True,
            "has_version": True,
            "bat_exists": True,
            "world_exists": True,
            "quick_play_configured": True,
            "bridge_mod_ready": True,
            "baritone_mod_ready": True,
        }

    async def launch(self) -> LaunchResult:
        """Return a successful launch dispatch receipt."""

        return LaunchResult(success=True)

    async def find_window(self) -> dict[str, Any] | None:
        """Report that no exact client window existed before launch."""

        return None


class _Bridge:
    """In-memory authenticated body endpoint used by session tests."""

    def __init__(self) -> None:
        """Create a disconnected body with an empty observation sequence."""

        self.connected = False
        self.instance_id = "minecraft-test"
        self.capabilities = (
            "chat.send",
            "control.release_all",
            "modded.operation",
            "movement.input",
            "navigation.goto",
            "navigation.stop",
            "player.respawn",
            "world.mine",
        )
        self.hello_metadata = {"bridge_version": "0.2.1"}
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
            facts={
                "world_loaded": True,
                "world": {"mode": "singleplayer", "singleplayer_name": "Elysian Realm"},
                "player": {
                    "uuid": "00000000-0000-0000-0000-000000000001",
                    "x": self.sequence,
                    "y": 64,
                    "z": 0,
                },
            },
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


class _TitleScreenBridge(_Bridge):
    """Authenticate successfully while never loading a playable world."""

    async def observe(self, after_sequence: int | None = None) -> WorldObservation:
        self.sequence += 1
        return WorldObservation(
            instance_id=self.instance_id,
            sequence=self.sequence,
            observed_at=utc_now(),
            source="test-bridge",
            facts={"world_loaded": False, "screen": {"class": "TitleScreen"}},
        )


class _WrongWorldBridge(_Bridge):
    """Expose a real world whose identity does not match the managed save."""

    async def observe(self, after_sequence: int | None = None) -> WorldObservation:
        observation = await super().observe(after_sequence)
        observation.facts["world"]["singleplayer_name"] = "Disposable Test World"
        return observation


class _PausedWorldBridge(_Bridge):
    """Expose the right world while its single-player simulation is paused."""

    async def observe(self, after_sequence: int | None = None) -> WorldObservation:
        observation = await super().observe(after_sequence)
        observation.facts["client_paused"] = True
        observation.facts["screen"] = {"class": "PauseScreen"}
        return observation


class _FlakyCloseBridge(_Bridge):
    """Fail the first body release so shutdown retry behavior is observable."""

    def __init__(self) -> None:
        super().__init__()
        self.close_attempts = 0

    async def close(self) -> None:
        self.close_attempts += 1
        if self.close_attempts == 1:
            raise RuntimeError("simulated release failure")
        await super().close()


class _MissingQuickPlayLauncher(_Launcher):
    """Report a valid install that would stop at the title screen."""

    async def check_installation(self) -> dict[str, Any]:
        result = await super().check_installation()
        result["quick_play_configured"] = False
        return result


class _BrokenWindowLauncher(_Launcher):
    """Expose a failed WSL-to-Windows inspection path."""

    async def find_window(self) -> dict[str, Any] | None:
        raise RuntimeError("Windows interoperability missing")


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


class _AutonomousDecisionSource:
    """Choose one intention without waiting for an external chat turn."""

    def __init__(self) -> None:
        self.calls = 0

    async def decide(
        self,
        context: MinecraftConsciousnessTurnContext,
    ) -> MinecraftConsciousnessDecision:
        self.calls += 1
        if self.calls == 1:
            return MinecraftConsciousnessDecision(
                decision_id="minecraft_decision_" + "a" * 64,
                kind="pursue",
                turn_index=context.turn_index,
                authored_at=utc_now(),
                intention="走到附近高处看看，然后确认同伴还在身边",
                reason="我想先熟悉我们周围的地方",
            )
        return MinecraftConsciousnessDecision(
            decision_id=f"minecraft_wait_{context.turn_index}",
            kind="wait",
            turn_index=context.turn_index,
            authored_at=utc_now(),
            reason="刚完成一件事，先看看世界接下来有什么变化",
            reconsider_after_seconds=30.0,
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

    async def resume(self, instance_id: str, **kwargs: Any) -> bool:
        self.calls.append("resume")
        return self._inner.resume(instance_id, **kwargs)

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

    async def get_recent_subconscious_context() -> RecentSubconsciousContext:
        return _recent_subconscious("shared-subconscious")

    bridge = _Bridge()
    config = _body_only_config(mc_home=tmp_path, bridge_ready_timeout_seconds=1)
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=config,
        consciousness_registry=registry,
        get_recent_subconscious_context=get_recent_subconscious_context,
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


async def test_dedicated_consciousness_runs_observe_decide_act_without_chat(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """The production session closes a full autonomous scene loop on its own."""

    order: list[str] = []
    decisions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    world_receipts: list[dict[str, Any]] = []
    acted = asyncio.Event()

    class _RecordingBridge(_Bridge):
        async def act(self, command: ActionCommand) -> ActionReceipt:
            order.append(f"act:{command.operation}")
            acted.set()
            return await super().act(command)

    async def subject_projection(**kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"projection_kind": "minecraft", "max_bytes": 16384}
        return _minecraft_subject_snapshot()

    async def recent_subconscious(**kwargs: Any) -> RecentSubconsciousContext:
        assert kwargs == {"group_limit": 5, "max_bytes": 8192}
        return _recent_subconscious("刚才还在聊天，现在我来到 Minecraft。")

    async def record_decision(
        decision: dict[str, Any],
        context_reference: dict[str, Any],
    ) -> None:
        order.append(f"record:{decision['decision_id']}")
        decisions.append((decision, context_reference))

    async def report_world(report: str, **kwargs: Any) -> dict[str, str]:
        world_receipts.append({"report": report, **kwargs})
        return {"assertion_id": f"assertion-{len(world_receipts)}"}

    bridge = _RecordingBridge()
    planner = _Planner()
    decision_source = _AutonomousDecisionSource()
    monkeypatch.setattr(session_module, "JsonIntentPlanner", lambda *_args: planner)
    config = MCConfig(
        mc_home=tmp_path,
        bridge_ready_timeout_seconds=1,
        consciousness_retry_base_seconds=0.01,
        consciousness_retry_max_seconds=0.02,
    )
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=config,
        consciousness_registry=ConsciousnessRegistry(),
        get_recent_subconscious_context=recent_subconscious,
        get_subject_context_projection_snapshot=subject_projection,
        record_minecraft_consciousness_decision=record_decision,
        report_world_observation=report_world,
        consciousness_decision_source=decision_source,
    )
    session._launcher = _Launcher()

    async def wait_for_bridge(_profile: Any) -> _RecordingBridge:
        return bridge

    session._wait_for_bridge = wait_for_bridge
    started = await session.start(goal="一起随便探索", body_name="agent")
    assert started["success"] is True, started
    try:
        # This path intentionally crosses several append+fsync boundaries before
        # dispatch.  Keep the assertion event-driven while allowing parallel
        # full-suite disk contention; the deadline still detects a stuck loop.
        await asyncio.wait_for(
            acted.wait(),
            timeout=_DURABLE_SCENE_LOOP_TIMEOUT_SECONDS,
        )
    except TimeoutError as exception:
        raise AssertionError(
            {"status": await session.get_status(), "order": order}
        ) from exception
    for _ in range(100):
        status = await session.get_status()
        if status["consciousness"]["recent_outcome_count"] >= 1:
            break
        await asyncio.sleep(0.01)
    stopped = await session.stop()

    assert stopped["success"] is True
    assert decisions
    assert order[0].startswith("record:minecraft_decision_")
    assert order[1] == "act:modded.operation"
    assert decisions[0][0]["intention"] == ("走到附近高处看看，然后确认同伴还在身边")
    context_json = json.dumps(decisions[0][1], ensure_ascii=False)
    assert "Subject Context Projection" not in context_json
    assert "刚才还在聊天" not in context_json
    assert decisions[0][1]["perception"]["observation"]["observation_id"]
    records = await session._trace.verify()
    kinds = [record.kind for record in records]
    assert "observation" in kinds
    assert "intent.issued" in kinds
    assert "command.receipt" in kinds
    assert "intent.conclusion" in kinds
    trace_receipts = [
        item["value"]
        for item in world_receipts
        if item.get("predicate") == "embodied_trace"
    ]
    assert trace_receipts
    assert all(
        len(json.dumps(item, ensure_ascii=False).encode("utf-8")) <= 8192
        for item in trace_receipts
    )


async def test_subject_binding_failure_happens_before_body_launch(
    tmp_path: Path,
) -> None:
    """A scene without the unified subject authority must not acquire a body."""

    class _CountingLauncher(_Launcher):
        launch_calls = 0

        async def launch(self) -> LaunchResult:
            self.launch_calls += 1
            return await super().launch()

    launcher = _CountingLauncher()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=MCConfig(mc_home=tmp_path),
        consciousness_registry=ConsciousnessRegistry(),
    )
    session._launcher = launcher

    result = await session.start(goal="一起玩", body_name="agent")

    assert result["success"] is False
    assert "requires the subject projection service" in result["error"]
    assert launcher.launch_calls == 0
    assert session.state.active is False


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
    assert planner.contexts[0]["recent_subconscious_context"] == "shared-subconscious"
    reference = planner.contexts[0]["recent_subconscious_reference"]
    assert reference["through_sequence"] == 6
    assert reference["delivered_bytes"] == len(b"shared-subconscious")


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
        mc_config=_body_only_config(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
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

    assert session._trace is not None
    record = await session._trace.append("body.selected", {"body_name": "agent"})
    await session._on_trace(record)
    assert registry.calls == ["register", "touch"]
    assert saves == ["save", "save"]

    stopped = await session.stop()
    assert stopped["success"] is True
    assert registry.calls == ["register", "touch", "terminate"]
    assert saves == ["save", "save", "save"]


async def test_session_resumes_expired_presence_on_real_body_activity(
    tmp_path: Path,
) -> None:
    """A live body action reclaims a lease-expired Presence before success."""

    registry = _AsyncRegistry()
    saves: list[str] = []

    async def save_registry() -> None:
        saves.append("save")

    bridge = _Bridge()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
        consciousness_registry=registry,
        save_consciousness_registry=save_registry,
    )
    session._launcher = _Launcher()

    async def wait_for_bridge(profile: Any) -> _Bridge:
        return bridge

    session._wait_for_bridge = wait_for_bridge
    started = await session.start(body_name="agent")
    assert started["success"] is True
    instance_id = session.state.consciousness_instance_id
    assert registry._inner.suspend(instance_id, reason="lease_expired") is True

    session._planner = _Planner()
    result = await session.do_intent("walk to x=3")

    assert result["success"] is True
    assert registry.get(instance_id).is_active
    assert registry.calls[0:2] == ["register", "resume"]
    assert saves[0:2] == ["save", "save"]
    await session.stop()


async def test_session_reports_async_presence_touch_failure(
    tmp_path: Path,
) -> None:
    """A failed durable Presence touch cannot be reported as intent success."""

    registry = _AsyncRegistry(fail_touch=True)
    bridge = _Bridge()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
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
        mc_config=_body_only_config(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
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


async def test_session_awaits_async_recent_subconscious_context(
    tmp_path: Path,
) -> None:
    """Each intent consumes the bounded async subconscious projection once."""

    calls: list[str] = []

    async def get_recent_subconscious_context() -> RecentSubconsciousContext:
        calls.append("recent")
        return _recent_subconscious("async recent activity")

    bridge = _Bridge()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
        get_recent_subconscious_context=get_recent_subconscious_context,
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
    assert planner.contexts[0]["recent_subconscious_context"] == "async recent activity"
    assert calls == ["recent"]
    reference = planner.contexts[0]["recent_subconscious_reference"]
    assert reference["schema"] == "minecraft.recent_subconscious_reference.v1"
    assert reference["delivered_bytes"] == len(b"async recent activity")
    await session.stop()


async def test_session_reports_recent_subconscious_failure(
    tmp_path: Path,
) -> None:
    """A failed continuity read cannot be reported as intent success."""

    async def get_recent_subconscious_context() -> RecentSubconsciousContext:
        raise RuntimeError("recent subconscious unavailable")

    bridge = _Bridge()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
        get_recent_subconscious_context=get_recent_subconscious_context,
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

    assert result["success"] is False
    assert result["error"] == "recent subconscious unavailable"
    await session.stop()


async def test_session_rejects_invalid_recent_subconscious_contract(
    tmp_path: Path,
) -> None:
    """The session fails closed when a producer violates the typed contract."""

    async def get_recent_subconscious_context() -> Any:
        return SimpleNamespace(content="untyped context")

    bridge = _Bridge()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
        get_recent_subconscious_context=get_recent_subconscious_context,
    )
    session._launcher = _Launcher()

    async def wait_for_bridge(profile: Any) -> _Bridge:
        return bridge

    session._wait_for_bridge = wait_for_bridge
    assert (await session.start(body_name="agent"))["success"] is True
    planner = _Planner()
    session._planner = planner

    result = await session.do_intent("inspect with a typed continuity snapshot")

    assert result["success"] is False
    assert "RecentSubconsciousContext" in result["error"]
    assert planner.contexts == []
    await session.stop()


async def test_preflight_rejects_launcher_without_exact_quick_play(
    tmp_path: Path,
) -> None:
    """A launch script that stops at the menu is never described as ready."""

    token_file = tmp_path / "agent-token.json"
    token_file.write_text('{"authentication_token":"secret"}', encoding="utf-8")
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(
            mc_home=tmp_path,
            agent_token_file=token_file,
            shared_world_enabled=False,
        ),
    )
    session._launcher = _MissingQuickPlayLauncher()

    result = await session.preflight(body_name="agent")

    assert result["success"] is False
    assert result["ready_to_start"] is False
    assert any("--quickPlaySingleplayer" in item for item in result["blockers"])


async def test_agent_preflight_allows_first_launch_to_create_token(
    tmp_path: Path,
) -> None:
    """The NeoForge bridge may atomically create its token on first launch."""

    token_file = tmp_path / "not-created-yet.json"
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(mc_home=tmp_path, agent_token_file=token_file),
    )
    session._launcher = _Launcher()

    result = await session.preflight(body_name="agent")

    assert result["success"] is True
    assert result["ready_to_start"] is True
    assert result["token_bootstraps_on_launch"] is True


async def test_preflight_reports_windows_bridge_failure(tmp_path: Path) -> None:
    """A failed process/window probe is not silently converted to no window."""

    token_file = tmp_path / "agent-token.json"
    token_file.write_text('{"authentication_token":"secret"}', encoding="utf-8")
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(mc_home=tmp_path, agent_token_file=token_file),
    )
    session._launcher = _BrokenWindowLauncher()

    result = await session.preflight(body_name="agent")

    assert result["success"] is False
    assert "Windows interoperability missing" in result["windows_bridge_error"]


async def test_session_rejects_authenticated_title_screen(tmp_path: Path) -> None:
    """Authenticated transport alone cannot satisfy production readiness."""

    bridge = _TitleScreenBridge()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(
            mc_home=tmp_path,
            bridge_ready_timeout_seconds=1,
            world_ready_timeout_seconds=0.01,
            shared_world_enabled=False,
        ),
    )
    session._launcher = _Launcher()

    async def wait_for_bridge(profile: Any) -> _Bridge:
        return bridge

    session._wait_for_bridge = wait_for_bridge

    result = await session.start(body_name="agent")

    assert result["success"] is False
    assert "no playable world is loaded" in result["error"]
    assert session.is_active is False
    assert bridge.closed is True


async def test_session_rejects_wrong_singleplayer_world(tmp_path: Path) -> None:
    """A different loaded save must not be mistaken for the managed world."""

    bridge = _WrongWorldBridge()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(
            mc_home=tmp_path,
            bridge_ready_timeout_seconds=1,
            world_ready_timeout_seconds=0.01,
            shared_world_enabled=False,
        ),
    )
    session._launcher = _Launcher()

    async def wait_for_bridge(profile: Any) -> _Bridge:
        return bridge

    session._wait_for_bridge = wait_for_bridge

    result = await session.start(body_name="agent")

    assert result["success"] is False
    assert "wrong singleplayer world" in result["error"]


async def test_session_rejects_paused_singleplayer_world(tmp_path: Path) -> None:
    """A loaded but paused world cannot claim general movement readiness."""

    bridge = _PausedWorldBridge()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(
            mc_home=tmp_path,
            bridge_ready_timeout_seconds=1,
            world_ready_timeout_seconds=0.01,
            shared_world_enabled=False,
        ),
    )
    session._launcher = _Launcher()

    async def wait_for_bridge(profile: Any) -> _Bridge:
        return bridge

    session._wait_for_bridge = wait_for_bridge

    result = await session.start(body_name="agent")

    assert result["success"] is False
    assert "world is paused" in result["error"]
    assert bridge.closed is True


async def test_session_rejects_missing_capability_and_bridge_version(
    tmp_path: Path,
) -> None:
    """A stale or partial mod build cannot enter an active session."""

    missing = _Bridge()
    missing.capabilities = tuple(
        item for item in missing.capabilities if item != "world.mine"
    )
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
    )
    session._launcher = _Launcher()

    async def missing_bridge(profile: Any) -> _Bridge:
        return missing

    session._wait_for_bridge = missing_bridge
    result = await session.start(body_name="agent")
    assert result["success"] is False
    assert "world.mine" in result["error"]

    stale = _Bridge()
    stale.hello_metadata = {"bridge_version": "0.1.0"}
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
    )
    session._launcher = _Launcher()

    async def stale_bridge(profile: Any) -> _Bridge:
        return stale

    session._wait_for_bridge = stale_bridge
    result = await session.start(body_name="agent")
    assert result["success"] is False
    assert "bridge version mismatch" in result["error"]


async def test_session_start_stop_are_idempotent(tmp_path: Path) -> None:
    """Repeated lifecycle calls do not create or release a second body."""

    session, bridge, _, _ = await _started_session(tmp_path)

    second_start = await session.start(body_name="agent")
    first_stop = await session.stop()
    second_stop = await session.stop()

    assert second_start["success"] is True
    assert second_start["already_active"] is True
    assert first_stop["success"] is True
    assert second_stop == {"success": True, "already_stopped": True}
    assert bridge.closed is True


async def test_failed_body_release_remains_retryable(tmp_path: Path) -> None:
    """A shutdown failure remains owned until a later close succeeds."""

    bridge = _FlakyCloseBridge()
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
    )
    session._launcher = _Launcher()

    async def wait_for_bridge(profile: Any) -> _Bridge:
        return bridge

    session._wait_for_bridge = wait_for_bridge
    assert (await session.start(body_name="agent"))["success"] is True

    failed = await session.close()
    blocked_start = await session.start(body_name="agent")
    retried = await session.close()

    assert failed["success"] is False
    assert failed["cleanup_pending"] is True
    assert "cleanup work" in blocked_start["error"]
    assert retried["success"] is True
    assert retried["cleanup_pending"] is False
    assert bridge.close_attempts == 2


async def test_large_recent_context_cannot_recurse_through_world_receipts(
    tmp_path: Path,
) -> None:
    """Repeated 1.5 MB continuity prompts produce bounded non-growing receipts."""

    session, _, _, world_events = await _started_session(tmp_path)
    base_content = "潜意识近期上下文" * 100_000
    contexts: list[RecentSubconsciousContext] = []

    async def get_recent_subconscious_context() -> RecentSubconsciousContext:
        context = _recent_subconscious(base_content)
        contexts.append(context)
        return context

    session._get_recent_subconscious_context = get_recent_subconscious_context
    session._planner = _Planner()

    for index in range(3):
        result = await session.do_intent(f"bounded round {index}")
        assert result["success"] is True

    trace_events = [
        item for item in world_events if item.get("predicate") == "embodied_trace"
    ]
    receipts = [item["value"] for item in trace_events]
    assert receipts
    assert all(
        item["occurrence_id"] == item["value"]["projection_id"] for item in trace_events
    )
    by_kind: dict[str, list[int]] = {}
    for receipt in receipts:
        encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
        assert "transient_prompt_context" not in encoded
        assert "transient_world_perception" not in encoded
        assert "recent_subconscious_context" not in encoded
        assert base_content not in encoded
        size = world_trace_receipt_size(receipt)
        assert size <= WORLD_TRACE_RECEIPT_MAX_BYTES
        by_kind.setdefault(receipt["trace_kind"], []).append(size)
    assert all(max(sizes) - min(sizes) < 128 for sizes in by_kind.values())
    assert len(contexts) == 3

    assert session._trace is not None
    records = await session._trace.verify()
    intent_records = [record for record in records if record.kind == "intent.issued"]
    assert len(intent_records) == 3
    for record, context in zip(intent_records, contexts, strict=True):
        serialized = json.dumps(record.to_wire(), ensure_ascii=False)
        assert context.content not in serialized
        assert "transient_prompt_context" not in record.payload
        assert record.payload["perception_reference"] is None
        reference = record.payload["durable_context"]["recent_subconscious_reference"]
        assert reference["delivered_bytes"] == context.delivered_bytes
        assert reference["projection_sha256"] == context.projection_sha256
    await session.stop()


async def test_failed_world_receipt_remains_retryable_and_uncommitted(
    tmp_path: Path,
) -> None:
    """A World write failure is not cached, committed, or reported as success."""

    session, _, _, _ = await _started_session(tmp_path)

    async def fail_world(report: str, **kwargs: Any) -> None:
        raise RuntimeError("World receipt unavailable")

    session._report_world_observation = fail_world
    session._planner = _Planner()
    result = await session.do_intent("do not fake World delivery")

    assert result["success"] is False
    assert "World receipt unavailable" in result["error"]
    assert session._trace is not None
    record = (await session._trace.verify())[-1]
    assert record.kind == "intent.issued"

    delivered: list[dict[str, Any]] = []

    async def report_world(report: str, **kwargs: Any) -> None:
        delivered.append({"report": report, **kwargs})

    session._report_world_observation = report_world
    await session._on_trace(record)
    await session._on_trace(record)

    assert len(delivered) == 1
    assert delivered[0]["value"]["schema"].endswith("projection.v1")
    assert delivered[0]["occurrence_id"] == delivered[0]["value"]["projection_id"]
    assert world_trace_receipt_size(delivered[0]["value"]) <= 8 * 1024
    await session.stop()


async def test_shared_world_preflight_skips_singleplayer_quick_play(
    tmp_path: Path,
) -> None:
    """Her own client joins the LAN world, so singleplayer gates do not apply."""

    token_file = tmp_path / "agent-token.json"
    token_file.write_text('{"authentication_token":"secret"}', encoding="utf-8")
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(
            mc_home=tmp_path,
            agent_token_file=token_file,
            shared_world_enabled=True,
        ),
    )
    session._launcher = _MissingQuickPlayLauncher()

    result = await session.preflight(body_name="agent")

    blockers = result.get("blockers") or []
    assert not any("--quickPlaySingleplayer" in item for item in blockers)
    assert not any("configured world does not exist" in item for item in blockers)


async def test_shared_world_agent_readiness_uses_server_semantics(
    tmp_path: Path,
) -> None:
    """Shared-world agents must not be blocked by singleplayer world name."""

    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(mc_home=tmp_path, shared_world_enabled=True),
    )
    assert session._body_profiles()["agent"].readiness_kind == "server_world"

    solo = MinecraftSession(
        workspace=tmp_path,
        mc_config=_body_only_config(mc_home=tmp_path, shared_world_enabled=False),
    )
    assert solo._body_profiles()["agent"].readiness_kind == "structured_world"
