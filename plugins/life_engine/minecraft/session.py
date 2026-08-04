"""Commercial Minecraft session built on explicit evidence-driven bodies."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bridge_body import BridgeBody
from .bridge_client import (
    BridgeConfig,
    BridgeProtocolError,
    MinecraftBridgeClient,
)
from .capture import WindowCapture
from .embodiment_contracts import ExecutionResult, WorldObservation
from .embodiment_runtime import EmbodimentRuntime
from .embodiment_trace import EmbodimentTrace
from .launcher import MCConfig, MinecraftLauncher
from .model_planner import (
    AGENT_BRIDGE_GUIDANCE,
    BIOMIMETIC_GUIDANCE,
    ElysiumModelDecisionSource,
    JsonIntentPlanner,
)


async def _invoke_callback(
    callback: Any,
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Await async callbacks and offload legacy synchronous callbacks."""

    async_callable_object = callable(callback) and inspect.iscoroutinefunction(
        type(callback).__call__
    )
    if inspect.iscoroutinefunction(callback) or async_callable_object:
        return await callback(*args, **kwargs)
    result = await asyncio.to_thread(callback, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


@dataclass(frozen=True, slots=True)
class BodyProfile:
    """Explicit endpoint and planning contract for one body."""

    name: str
    uri: str
    listen_uri: str | None
    token_file: Path
    planner_guidance: str


@dataclass(slots=True)
class SessionState:
    """Factual state for one Minecraft embodiment session."""

    active: bool = False
    session_id: str = ""
    stream_id: str = ""
    consciousness_instance_id: str = ""
    started_at: str = ""
    start_monotonic: float = 0.0
    body_name: str = ""
    session_goal: str = ""
    active_intent: str = ""
    latest_observation: WorldObservation | None = None
    conclusions: list[dict[str, Any]] = field(default_factory=list)
    last_error: str | None = None

    @property
    def duration_seconds(self) -> float:
        """Return live monotonic duration without altering persisted timestamps."""

        if not self.active:
            return 0.0
        return max(0.0, time.monotonic() - self.start_monotonic)


class MinecraftSession:
    """Own one selected game body, planner, trace, and lifecycle registration."""

    def __init__(
        self,
        workspace: Path,
        mc_config: MCConfig | None = None,
        llm_helper: Any | None = None,
        consciousness_registry: Any | None = None,
        save_consciousness_registry: Any | None = None,
        prepare_perception: Any | None = None,
        commit_perception: Any | None = None,
        report_world_observation: Any | None = None,
    ) -> None:
        """Create an inactive session with optional shared-world integrations."""

        self._workspace = workspace
        self._config = mc_config or MCConfig()
        self._launcher = MinecraftLauncher(self._config)
        self._capture = WindowCapture()
        self._registry = consciousness_registry
        self._save_registry = save_consciousness_registry
        self._prepare_perception = prepare_perception
        self._commit_perception = commit_perception
        self._report_world_observation = report_world_observation
        self._state = SessionState()
        self._runtime: EmbodimentRuntime | None = None
        self._bridge_client: MinecraftBridgeClient | None = None
        self._planner: JsonIntentPlanner | None = None
        self._trace: EmbodimentTrace | None = None
        self._execution_task: asyncio.Task[ExecutionResult] | None = None
        # Kept in the signature for callers during migration.  Planning now uses
        # the configured model stack directly and never falls back to rule parsing.
        self._legacy_llm_helper = llm_helper

    @property
    def state(self) -> SessionState:
        """Return current factual session state."""

        return self._state

    @property
    def is_active(self) -> bool:
        """Return whether a body is connected for this session."""

        return self._state.active

    async def start(self, goal: str = "", body_name: str = "") -> dict[str, Any]:
        """Launch Minecraft and connect one explicitly named body."""

        if self._state.active:
            return {"success": False, "error": "a Minecraft session is already active"}
        selected_name = body_name.strip() or self._config.default_body
        profiles = self._body_profiles()
        profile = profiles.get(selected_name)
        if profile is None:
            return {
                "success": False,
                "error": f"body is not configured: {selected_name}",
                "configured_bodies": sorted(profiles),
            }

        installation = await self._launcher.check_installation()
        if not installation.get("exists") or not installation.get("bat_exists"):
            return {
                "success": False,
                "error": "Minecraft installation or launch script is missing",
                "installation": installation,
            }
        launch = await self._launcher.launch()
        if not launch.success:
            return {"success": False, "error": launch.error}

        session_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
        trace = EmbodimentTrace(
            self._workspace / "minecraft" / "traces" / f"{session_id}.jsonl"
        )
        await trace.open()
        runtime = EmbodimentRuntime(trace, self._on_trace)

        try:
            client = await self._wait_for_bridge(profile)
            body = BridgeBody(profile.name, client)
            runtime.register_body(body)
            await runtime.select_body(profile.name)
            observation = await body.observe()
        except Exception as exception:  # noqa: BLE001 - return exact readiness failure
            await runtime.close()
            return {
                "success": False,
                "error": f"Minecraft body did not become ready: {exception}",
                "body_name": selected_name,
            }

        planner = JsonIntentPlanner(
            ElysiumModelDecisionSource(self._config.planner_task_name),
            lambda: client.capabilities,
            profile.planner_guidance,
        )
        stream_id = f"game.minecraft.{session_id}"
        instance_id = f"minecraft_{session_id}"
        self._trace = trace
        self._runtime = runtime
        self._bridge_client = client
        self._planner = planner
        self._state = SessionState(
            active=True,
            session_id=session_id,
            stream_id=stream_id,
            consciousness_instance_id=instance_id,
            started_at=datetime.now(UTC).isoformat(),
            start_monotonic=time.monotonic(),
            body_name=profile.name,
            session_goal=goal,
            latest_observation=observation,
        )
        try:
            await self._register_consciousness()
            await self._report_scene("body connected")
        except Exception as exception:  # noqa: BLE001 - lifecycle must not look ready
            cleanup_errors: list[str] = []
            try:
                await runtime.close()
            except Exception as cleanup_exception:  # noqa: BLE001
                cleanup_errors.append(f"body cleanup failed: {cleanup_exception}")
            try:
                await self._terminate_consciousness()
            except Exception as cleanup_exception:  # noqa: BLE001
                cleanup_errors.append(f"Presence cleanup failed: {cleanup_exception}")
            self._state.active = False
            self._state.last_error = str(exception)
            self._runtime = None
            self._bridge_client = None
            self._planner = None
            error = f"Minecraft session lifecycle initialization failed: {exception}"
            if cleanup_errors:
                error = f"{error}; {'; '.join(cleanup_errors)}"
            return {
                "success": False,
                "error": error,
                "body_name": selected_name,
                "trace_path": str(trace.path),
            }
        return {
            "success": True,
            "session_id": session_id,
            "body_name": profile.name,
            "stream_id": stream_id,
            "consciousness_instance_id": instance_id,
            "game_instance_id": client.instance_id,
            "advertised_operations": list(client.capabilities),
            "observation": observation.to_wire(),
            "trace_path": str(trace.path),
        }

    async def stop(self) -> dict[str, Any]:
        """Interrupt work, release controls, close bridges, and end the scene."""

        if not self._state.active:
            return {"success": False, "error": "no Minecraft session is active"}
        runtime = self._runtime
        if runtime is not None:
            await runtime.interrupt("Minecraft session stopped")
            await runtime.close()
        await self._report_scene("session ended")
        await self._terminate_consciousness()
        duration = self._state.duration_seconds
        session_id = self._state.session_id
        trace_path = str(self._trace.path) if self._trace else ""
        self._state.active = False
        self._runtime = None
        self._bridge_client = None
        self._planner = None
        self._execution_task = None
        return {
            "success": True,
            "session_id": session_id,
            "duration_seconds": duration,
            "conclusions": list(self._state.conclusions),
            "trace_path": trace_path,
            "game_left_running": True,
        }

    async def do_intent(
        self,
        intent: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Let the selected model planner pursue one exact intention."""

        if not self._state.active or self._runtime is None or self._planner is None:
            return {"success": False, "error": "no Minecraft session is active"}
        if not intent.strip():
            return {"success": False, "error": "intent must not be empty"}
        from .embodiment_contracts import EmbodiedIntent

        perception = None
        intent_context: dict[str, Any] = {
            "session_id": self._state.session_id,
            "session_goal": self._state.session_goal,
            "stream_id": self._state.stream_id,
        }
        if self._prepare_perception is not None:
            perception = await _invoke_callback(
                self._prepare_perception,
                self._state.consciousness_instance_id,
            )
            intent_context["transient_world_perception"] = perception.content
        embodied_intent = EmbodiedIntent(
            text=intent,
            body_name=self._state.body_name,
            context=intent_context,
        )
        self._state.active_intent = intent
        execution_timeout = (
            timeout if timeout is not None else self._config.intent_timeout_seconds
        )
        try:
            task = asyncio.create_task(
                self._runtime.execute(
                    embodied_intent,
                    self._planner,
                    timeout_seconds=execution_timeout,
                ),
                name=f"minecraft_intent:{embodied_intent.intent_id}",
            )
            self._execution_task = task
            result = await task
            if perception is not None and self._commit_perception is not None:
                await _invoke_callback(self._commit_perception, perception)
        except Exception as exception:  # noqa: BLE001 - report planner/bridge evidence failure
            self._state.last_error = str(exception)
            return {
                "success": False,
                "intent_id": embodied_intent.intent_id,
                "error": str(exception),
            }
        finally:
            self._execution_task = None
            self._state.active_intent = ""

        if result.observations:
            self._state.latest_observation = result.observations[-1]
        conclusion = result.conclusion.to_wire() if result.conclusion else None
        if conclusion is not None:
            self._state.conclusions.append(conclusion)
        await self._report_scene(
            "intention concluded" if conclusion is not None else "intention interrupted"
        )
        return {
            "success": conclusion is not None and result.error is None,
            "intent_id": embodied_intent.intent_id,
            "conclusion": conclusion,
            "interrupted": result.interrupted,
            "error": result.error,
            "receipts": [item.to_wire() for item in result.receipts],
            "observations": [item.to_wire() for item in result.observations],
        }

    async def interrupt(self, reason: str) -> dict[str, Any]:
        """Interrupt the active intention without ending the game session."""

        if self._runtime is None:
            return {"success": False, "error": "no Minecraft session is active"}
        await self._runtime.interrupt(reason)
        return {"success": True, "reason": reason}

    async def look(self) -> dict[str, Any]:
        """Return latest structured state and a first-person client screenshot."""

        if not self._state.active or self._runtime is None:
            return {"success": False, "error": "no Minecraft session is active"}
        current = self._state.latest_observation
        after = current.sequence if current is not None else None
        body_client = self._bridge_client
        if body_client is None:
            return {"success": False, "error": "selected body bridge is absent"}
        observation = await body_client.observe(after)
        self._state.latest_observation = observation
        frame = await self._capture.grab_consciousness_frame()
        screenshot_path: str | None = None
        if frame is not None:
            directory = self._workspace / "minecraft" / "screenshots"
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
            saved = await asyncio.to_thread(
                frame.save,
                directory / f"{observation.observation_id}.png",
            )
            screenshot_path = str(saved)
        return {
            "success": True,
            "observation": observation.to_wire(),
            "screenshot_path": screenshot_path,
        }

    async def get_status(self) -> dict[str, Any]:
        """Return session, body, planner, and latest evidence status."""

        client = self._bridge_client
        observation = self._state.latest_observation
        return {
            "active": self._state.active,
            "session_id": self._state.session_id,
            "body_name": self._state.body_name,
            "stream_id": self._state.stream_id,
            "consciousness_instance_id": self._state.consciousness_instance_id,
            "duration_seconds": self._state.duration_seconds,
            "session_goal": self._state.session_goal,
            "active_intent": self._state.active_intent,
            "bridge_connected": bool(client and client.connected),
            "game_instance_id": client.instance_id if client else None,
            "advertised_operations": list(client.capabilities) if client else [],
            "latest_observation": observation.to_wire() if observation else None,
            "conclusions": list(self._state.conclusions),
            "last_error": self._state.last_error,
        }

    def _body_profiles(self) -> dict[str, BodyProfile]:
        """Build exact configured profiles for the two requested body routes."""

        return {
            "agent": BodyProfile(
                name="agent",
                uri=self._config.agent_bridge_uri,
                listen_uri=self._config.agent_bridge_listen_uri,
                token_file=self._config.agent_token_file,
                planner_guidance=AGENT_BRIDGE_GUIDANCE,
            ),
            "biomimetic": BodyProfile(
                name="biomimetic",
                uri=self._config.biomimetic_bridge_uri,
                listen_uri=self._config.biomimetic_bridge_listen_uri,
                token_file=self._config.biomimetic_token_file,
                planner_guidance=BIOMIMETIC_GUIDANCE,
            ),
        }

    async def _wait_for_bridge(self, profile: BodyProfile) -> MinecraftBridgeClient:
        """Wait for a launched body endpoint; never switch to another profile."""

        deadline = time.monotonic() + self._config.bridge_ready_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if profile.token_file.exists():
                try:
                    token = await asyncio.to_thread(self._read_token, profile.token_file)
                    remaining = max(0.1, deadline - time.monotonic())
                    client = MinecraftBridgeClient(
                        BridgeConfig(
                            uri=profile.uri,
                            token=token,
                            listen_uri=profile.listen_uri,
                            open_timeout_seconds=min(10.0, remaining),
                        )
                    )
                    await client.open()
                    return client
                except BridgeProtocolError:
                    raise
                except (OSError, TimeoutError, ConnectionError) as exception:
                    last_error = exception
            await asyncio.sleep(1.0)
        raise TimeoutError(
            f"body endpoint was not ready at {profile.uri}: {last_error}"
        )

    @staticmethod
    def _read_token(path: Path) -> str:
        """Read the exact generated authentication token from a bridge config."""

        payload = json.loads(path.read_text(encoding="utf-8"))
        token = str(payload.get("authentication_token") or "")
        if not token:
            raise ValueError(f"authentication_token is absent in {path}")
        return token

    async def _on_trace(self, kind: str, payload: dict[str, object]) -> None:
        """Update latest factual scene state after durable trace persistence."""

        if kind == "observation":
            self._state.latest_observation = WorldObservation.from_wire(payload)
        if self._registry is not None and self._state.consciousness_instance_id:
            await _invoke_callback(
                self._registry.touch,
                self._state.consciousness_instance_id,
                timestamp=datetime.now(UTC).isoformat(),
                reason=f"minecraft_trace:{kind}",
            )
            if self._save_registry is not None:
                await _invoke_callback(self._save_registry)
        if self._report_world_observation is not None and self._state.stream_id:
            await _invoke_callback(
                self._report_world_observation,
                f"Minecraft trace persisted: {kind}",
                source_instance_id=self._state.consciousness_instance_id,
                subject=self._state.stream_id,
                predicate="embodied_trace",
                domain="minecraft",
                stream_id=self._state.stream_id,
                value={"trace_kind": kind, "payload": payload},
            )

    async def _register_consciousness(self) -> None:
        """Register an independent Minecraft consciousness scene when available."""

        if self._registry is None:
            return
        from ..service.consciousness import ConsciousnessInstance
        from ..service.world_state import PerceptionFilter

        instance = ConsciousnessInstance(
            instance_id=self._state.consciousness_instance_id,
            kind="minecraft",
            display_name="Minecraft",
            stream_ids=[self._state.stream_id],
            status="active",
            created_at=self._state.started_at,
            last_active_at=self._state.started_at,
            perception_filter=PerceptionFilter.full(),
            metadata={
                "body_name": self._state.body_name,
                "session_id": self._state.session_id,
            },
            session_id=self._state.session_id,
            lease_duration_seconds=300,
        )
        await _invoke_callback(self._registry.register, instance)
        if self._save_registry is not None:
            await _invoke_callback(self._save_registry)

    async def _terminate_consciousness(self) -> None:
        """Terminate this session's registered consciousness instance."""

        if self._registry is None or not self._state.consciousness_instance_id:
            return
        await _invoke_callback(
            self._registry.terminate,
            self._state.consciousness_instance_id,
            reason="minecraft_session_ended",
        )
        if self._save_registry is not None:
            await _invoke_callback(self._save_registry)

    async def _report_scene(self, status: str) -> None:
        """Append factual session lifecycle as an attributed observation."""

        if self._report_world_observation is None or not self._state.stream_id:
            return
        await _invoke_callback(
            self._report_world_observation,
            status,
            source_instance_id=self._state.consciousness_instance_id,
            subject=self._state.stream_id,
            predicate="session_state",
            domain="minecraft",
            stream_id=self._state.stream_id,
            value={
                "summary": status,
                "body_name": self._state.body_name,
                "session_id": self._state.session_id,
            },
        )
