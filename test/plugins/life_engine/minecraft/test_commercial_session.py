"""Lifecycle tests for the evidence-driven Minecraft session."""

from __future__ import annotations

import json
from hashlib import sha256
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
from plugins.life_engine.minecraft.model_planner import VerifiedPerceptionDelivery
from plugins.life_engine.minecraft.session import MinecraftSession
from plugins.life_engine.minecraft.trace_projection import (
    WORLD_TRACE_RECEIPT_MAX_BYTES,
    world_trace_receipt_size,
)
from plugins.life_engine.service.consciousness import ConsciousnessRegistry
from plugins.life_engine.service.perception_gateway import PerceptionDeliveryReceipt


def _prepared_perception(instance_id: str, content: str) -> SimpleNamespace:
    """Create a complete content/reference pair used by Minecraft consumers."""

    encoded = content.encode("utf-8")
    digest = sha256(encoded).hexdigest()
    return SimpleNamespace(
        instance_id=instance_id,
        projection_kind="world_perception",
        from_position=2,
        through_position=5,
        source_frontier=7,
        cursor_revision=1,
        content=content,
        assertion_ids=("assertion-a", "assertion-b"),
        change_positions=(3, 5),
        delivery_id=f"delivery-{digest[:24]}",
        projection_sha256=digest,
        algorithm_version="world-perception-page-v2",
        delivered_bytes=len(encoded),
        source_payload_bytes=len(encoded),
        omitted_assertion_count=0,
        omitted_change_count=0,
        omitted_source_bytes=0,
        snapshot_continuation_token="",
        has_more_changes=True,
    )


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

    def __init__(self, *, prove_delivery: bool = True) -> None:
        self.contexts: list[dict[str, Any]] = []
        self._prove_delivery = prove_delivery
        self._deliveries: dict[str, VerifiedPerceptionDelivery] = {}

    def reset_perception_delivery(self, reference: Any) -> None:
        """Discard a prior intent's proof for the same prepared projection."""

        self._deliveries.pop(reference.delivery_id, None)

    def consume_perception_delivery(
        self,
        reference: Any,
    ) -> VerifiedPerceptionDelivery:
        """Return the proof created by the final fake planning turn."""

        delivery = self._deliveries.pop(reference.delivery_id, None)
        if delivery is None:
            raise RuntimeError(
                "Minecraft planner produced no exact Perception delivery proof"
            )
        return delivery

    async def decide(
        self,
        intent: EmbodiedIntent,
        observations: tuple[WorldObservation, ...],
        receipts: tuple[ActionReceipt, ...],
    ) -> PlannerTurn:
        """Act once, then cite both receipt and post-action observation."""

        self.contexts.append(dict(intent.context))
        reference = intent.perception_reference
        if self._prove_delivery and reference is not None:
            self._deliveries[reference.delivery_id] = VerifiedPerceptionDelivery(
                delivery_id=reference.delivery_id,
                projection_sha256=reference.content_sha256,
                delivered_bytes=reference.content_bytes,
                transport_request_id="fake-request",
            )

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

    def prepare_perception(instance_id: str) -> Any:
        return _prepared_perception(instance_id, "shared-presence")

    def commit_perception(
        prepared: Any,
        receipt: PerceptionDeliveryReceipt,
    ) -> None:
        observations.append(
            {"perception_commit": prepared, "delivery_receipt": receipt}
        )

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
    assert planner.contexts[0]["world_perception"] == "shared-presence"


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

    calls: list[str] = []

    async def prepare_perception(instance_id: str) -> Any:
        calls.append(f"prepare:{instance_id}")
        return _prepared_perception(instance_id, "async shared perception")

    receipts: list[PerceptionDeliveryReceipt] = []

    async def commit_perception(
        value: Any,
        receipt: PerceptionDeliveryReceipt,
    ) -> None:
        receipts.append(receipt)
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
    assert planner.contexts[0]["world_perception"] == "async shared perception"
    assert calls == [
        f"prepare:{session.state.consciousness_instance_id}",
        "commit",
    ]
    assert len(receipts) == 1
    assert receipts[0].exact is True
    assert receipts[0].transport_request_id == "fake-request"
    await session.stop()


async def test_session_reports_async_perception_commit_failure(
    tmp_path: Path,
) -> None:
    """A failed async cursor commit cannot be reported as intent success."""

    async def prepare_perception(instance_id: str) -> Any:
        return _prepared_perception(instance_id, f"prepared:{instance_id}")

    async def commit_perception(
        value: Any,
        receipt: PerceptionDeliveryReceipt,
    ) -> None:
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


async def test_session_keeps_cursor_without_exact_perception_delivery(
    tmp_path: Path,
) -> None:
    """Missing final-attempt proof fails closed before the cursor callback."""

    commits: list[PerceptionDeliveryReceipt] = []

    async def prepare_perception(instance_id: str) -> Any:
        return _prepared_perception(instance_id, "must reach the effective request")

    async def commit_perception(
        value: Any,
        receipt: PerceptionDeliveryReceipt,
    ) -> None:
        commits.append(receipt)

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
    assert (await session.start(body_name="agent"))["success"] is True
    session._planner = _Planner(prove_delivery=False)

    result = await session.do_intent("inspect without a false cursor commit")

    assert result["success"] is False
    assert "no exact Perception delivery proof" in result["error"]
    assert commits == []
    await session.stop()


async def test_preflight_rejects_launcher_without_exact_quick_play(
    tmp_path: Path,
) -> None:
    """A launch script that stops at the menu is never described as ready."""

    token_file = tmp_path / "agent-token.json"
    token_file.write_text('{"authentication_token":"secret"}', encoding="utf-8")
    session = MinecraftSession(
        workspace=tmp_path,
        mc_config=MCConfig(
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
        mc_config=MCConfig(mc_home=tmp_path, agent_token_file=token_file),
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
        mc_config=MCConfig(mc_home=tmp_path, agent_token_file=token_file),
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
        mc_config=MCConfig(
            mc_home=tmp_path,
            bridge_ready_timeout_seconds=1,
            world_ready_timeout_seconds=0.01,
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
        mc_config=MCConfig(
            mc_home=tmp_path,
            bridge_ready_timeout_seconds=1,
            world_ready_timeout_seconds=0.01,
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
        mc_config=MCConfig(
            mc_home=tmp_path,
            bridge_ready_timeout_seconds=1,
            world_ready_timeout_seconds=0.01,
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
        mc_config=MCConfig(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
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
        mc_config=MCConfig(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
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
        mc_config=MCConfig(mc_home=tmp_path, bridge_ready_timeout_seconds=1),
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


async def test_large_perception_cannot_recurse_through_world_receipts(
    tmp_path: Path,
) -> None:
    """Repeated 1.5 MB prompt deliveries produce bounded non-growing receipts."""

    session, _, _, world_events = await _started_session(tmp_path)
    base_content = "瞬态世界投影" * 150_000
    prepared_values: list[SimpleNamespace] = []

    async def prepare_perception(instance_id: str) -> Any:
        prior_receipts = [
            item["value"]
            for item in world_events
            if item.get("predicate") == "embodied_trace"
        ]
        feedback = json.dumps(prior_receipts, ensure_ascii=False, sort_keys=True)
        prepared = _prepared_perception(instance_id, base_content + feedback)
        prepared_values.append(prepared)
        return prepared

    committed: list[tuple[SimpleNamespace, PerceptionDeliveryReceipt]] = []

    async def commit_perception(
        prepared: SimpleNamespace,
        receipt: PerceptionDeliveryReceipt,
    ) -> None:
        committed.append((prepared, receipt))

    session._prepare_perception = prepare_perception
    session._commit_perception = commit_perception
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
        item["occurrence_id"] == item["value"]["projection_id"]
        for item in trace_events
    )
    by_kind: dict[str, list[int]] = {}
    for receipt in receipts:
        encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
        assert "transient_prompt_context" not in encoded
        assert "transient_world_perception" not in encoded
        assert base_content not in encoded
        size = world_trace_receipt_size(receipt)
        assert size <= WORLD_TRACE_RECEIPT_MAX_BYTES
        by_kind.setdefault(receipt["trace_kind"], []).append(size)
    assert all(max(sizes) - min(sizes) < 128 for sizes in by_kind.values())
    assert [prepared for prepared, _receipt in committed] == prepared_values
    assert all(receipt.exact for _prepared, receipt in committed)
    assert all(
        receipt.projection_sha256 == prepared.projection_sha256
        and receipt.delivered_bytes == prepared.delivered_bytes
        for prepared, receipt in committed
    )

    assert session._trace is not None
    records = await session._trace.verify()
    intent_records = [record for record in records if record.kind == "intent.issued"]
    assert len(intent_records) == 3
    for record, prepared in zip(intent_records, prepared_values, strict=True):
        serialized = json.dumps(record.to_wire(), ensure_ascii=False)
        assert prepared.content not in serialized
        assert "transient_prompt_context" not in record.payload
        reference = record.payload["perception_reference"]
        assert reference["bytes"] == len(prepared.content.encode("utf-8"))
    await session.stop()


async def test_failed_world_receipt_remains_retryable_and_uncommitted(
    tmp_path: Path,
) -> None:
    """A World write failure is not cached, committed, or reported as success."""

    session, _, _, world_events = await _started_session(tmp_path)
    prior_commits = sum("perception_commit" in item for item in world_events)

    async def fail_world(report: str, **kwargs: Any) -> None:
        raise RuntimeError("World receipt unavailable")

    session._report_world_observation = fail_world
    session._planner = _Planner()
    result = await session.do_intent("do not fake World delivery")

    assert result["success"] is False
    assert "World receipt unavailable" in result["error"]
    assert sum("perception_commit" in item for item in world_events) == prior_commits
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
    assert (
        delivered[0]["occurrence_id"]
        == delivered[0]["value"]["projection_id"]
    )
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
        mc_config=MCConfig(
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
