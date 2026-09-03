"""Commercial Minecraft session built on explicit evidence-driven bodies."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from src.kernel.concurrency import get_task_manager

from ..service.subconscious_context import RecentSubconsciousContext
from .bot_launcher import MinecraftBotLauncher
from .bridge_body import BridgeBody
from .bridge_client import (
    BridgeConfig,
    BridgeDisconnectedError,
    BridgeProtocolError,
    MinecraftBridgeClient,
)
from .capture import WindowCapture
from .consciousness import (
    ElysiumMinecraftDecisionSource,
    MinecraftConsciousnessDecision,
    MinecraftConsciousnessPerception,
    MinecraftConsciousnessRuntime,
    MinecraftConsciousnessTurnContext,
    MinecraftDecisionSource,
    MinecraftSubjectContextBinding,
)
from .embodiment_contracts import (
    ActionCommand,
    EmbodiedIntent,
    ExecutionResult,
    WorldObservation,
)
from .embodiment_runtime import EmbodimentRuntime
from .embodiment_trace import EmbodimentTrace, TraceRecord
from .launcher import MCConfig, MinecraftLauncher
from .model_planner import (
    AGENT_BRIDGE_GUIDANCE,
    BIOMIMETIC_GUIDANCE,
    BOT_BRIDGE_GUIDANCE,
    ElysiumModelDecisionSource,
    JsonIntentPlanner,
)
from .trace_projection import build_world_trace_receipt

_BODY_EVENT_WAKE_KINDS = frozenset(
    {
        "minecraft.body.disconnected",
        "minecraft.chat.received",
        "minecraft.player.died",
        "minecraft.player.health_changed",
        "minecraft.player.joined",
        "minecraft.player.left",
        "minecraft.task.cancelled",
        "minecraft.task.completed",
        "minecraft.task.failed",
        "minecraft.whisper.received",
    }
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
    required_operations: frozenset[str]
    readiness_kind: str


class ReadinessState(StrEnum):
    """Externally diagnosable lifecycle states for one Minecraft body."""

    IDLE = "idle"
    PREFLIGHT = "preflight"
    LAUNCHING = "launching"
    AWAITING_BRIDGE = "awaiting_bridge"
    AWAITING_WORLD = "awaiting_world"
    READY = "ready"
    ACTIVE = "active"
    DEGRADED = "degraded"
    FAILED = "failed"
    CLOSING = "closing"


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
    readiness: str = ReadinessState.IDLE
    readiness_detail: str = ""
    launch_pid: int | None = None
    window: dict[str, Any] | None = None
    game_instance_id: str | None = None
    bridge_version: str | None = None
    body_event_count: int = 0
    last_body_event: dict[str, Any] | None = None

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
        register_consciousness_instance: Any | None = None,
        touch_consciousness_instance: Any | None = None,
        resume_consciousness_instance: Any | None = None,
        terminate_consciousness_instance: Any | None = None,
        get_recent_subconscious_context: Any | None = None,
        get_subject_context_projection_snapshot: Any | None = None,
        record_minecraft_consciousness_decision: Any | None = None,
        record_minecraft_body_event: Any | None = None,
        record_conscious_model_turn: Any | None = None,
        report_world_observation: Any | None = None,
        consciousness_decision_source: MinecraftDecisionSource | None = None,
    ) -> None:
        """Create an inactive session with optional shared-world integrations."""

        self._workspace = workspace
        self._config = mc_config or MCConfig()
        self._launcher = MinecraftLauncher(self._config)
        self._bot_launcher = MinecraftBotLauncher()
        self._capture = WindowCapture()
        self._registry = consciousness_registry
        self._save_registry = save_consciousness_registry
        self._register_presence = register_consciousness_instance
        self._touch_presence = touch_consciousness_instance
        self._resume_presence = resume_consciousness_instance
        self._terminate_presence = terminate_consciousness_instance
        self._get_recent_subconscious_context = get_recent_subconscious_context
        self._get_subject_context_projection_snapshot = (
            get_subject_context_projection_snapshot
        )
        self._record_minecraft_consciousness_decision = (
            record_minecraft_consciousness_decision
        )
        self._record_minecraft_body_event = record_minecraft_body_event
        self._record_conscious_model_turn = record_conscious_model_turn
        self._report_world_observation = report_world_observation
        self._injected_consciousness_decision_source = consciousness_decision_source
        self._state = SessionState()
        self._runtime: EmbodimentRuntime | None = None
        self._bridge_client: MinecraftBridgeClient | None = None
        self._planner: JsonIntentPlanner | None = None
        self._trace: EmbodimentTrace | None = None
        self._execution_task: asyncio.Task[ExecutionResult] | None = None
        self._consciousness_runtime: MinecraftConsciousnessRuntime | None = None
        self._body_event_task: asyncio.Task[Any] | None = None
        self._body_event_task_id: str | None = None
        self._traced_body_event_ids: set[str] = set()
        self._subject_context_binding: MinecraftSubjectContextBinding | None = None
        self._intent_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._trace_projection_lock = asyncio.Lock()
        self._projected_trace_receipts: set[str] = set()
        self._presence_registered = False
        self._scene_open = False
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

    async def preflight(self, body_name: str = "") -> dict[str, Any]:
        """Inspect deployment facts without launching or binding a body endpoint."""

        selected_name = body_name.strip() or self._config.default_body
        profile = self._body_profiles().get(selected_name)
        if profile is None:
            return {
                "success": False,
                "error": f"body is not configured: {selected_name}",
                "configured_bodies": sorted(self._body_profiles()),
            }
        if selected_name == "bot":
            return await self._bot_preflight(profile)
        blockers: list[str] = []
        try:
            installation = await self._launcher.check_installation()
        except Exception as exception:  # noqa: BLE001 - diagnostic API boundary
            return {
                "success": False,
                "body_name": selected_name,
                "ready_to_start": False,
                "blockers": [f"installation inspection failed: {exception}"],
                "installation": None,
                "existing_window": None,
            }
        window_error: str | None = None
        try:
            window = await self._launcher.find_window()
        except Exception as exception:  # noqa: BLE001 - diagnostic API boundary
            window = None
            window_error = f"Windows control bridge is unavailable: {exception}"
            blockers.append(window_error)
        if not installation.get("exists"):
            blockers.append("Minecraft home is missing")
        if not installation.get("has_version"):
            blockers.append(
                f"NeoForge version for {self._config.mc_version} is missing"
            )
        if not installation.get("bat_exists"):
            blockers.append("launch script is missing")
        if (
            selected_name == "agent"
            and not self._in_shared_world_mode()
            and not installation.get("world_exists")
        ):
            blockers.append(
                f"configured world does not exist: {self._config.world_name}"
            )
        if selected_name == "agent" and not installation.get("bridge_mod_ready"):
            blockers.append(
                "the pinned Elysium NeoForge bridge artifact is not selected"
            )
        if selected_name == "agent" and not installation.get("baritone_mod_ready"):
            blockers.append(
                "the pinned official Baritone NeoForge artifact is not selected"
            )
        if (
            self._config.require_quick_play
            and not self._in_shared_world_mode()
            and not installation.get("quick_play_configured")
        ):
            blockers.append(
                "launch script does not enter the exact configured world with "
                "--quickPlaySingleplayer"
            )
        token_bootstraps_on_launch = selected_name == "agent" and window is None
        if not profile.token_file.exists() and not token_bootstraps_on_launch:
            blockers.append(f"body token file is missing: {profile.token_file}")
        return {
            "success": not blockers,
            "body_name": selected_name,
            "ready_to_start": not blockers,
            "blockers": blockers,
            "installation": installation,
            "existing_window": window,
            "windows_bridge_error": window_error,
            "required_operations": sorted(profile.required_operations),
            "expected_bridge_version": self._config.expected_bridge_version,
            "token_bootstraps_on_launch": token_bootstraps_on_launch,
        }

    async def _bot_preflight(self, profile: BodyProfile) -> dict[str, Any]:
        """Inspect headless bot deployment facts without Windows control."""

        blockers: list[str] = []
        node_check = await self._bot_launcher.check_node()
        if not node_check["available"]:
            blockers.append(f"node runtime is unavailable: {node_check['error']}")
        dependency_check = self._bot_launcher.check_dependencies()
        if not dependency_check["entrypoint_exists"]:
            blockers.append("bot entrypoint is missing from integrations/minecraft_bot")
        if not dependency_check.get("lockfile_exists", False):
            blockers.append("bot package-lock.json is missing")
        if not dependency_check["dependencies_installed"]:
            blockers.append(
                "bot dependencies are not installed; run npm ci in "
                "integrations/minecraft_bot; missing: "
                + ", ".join(dependency_check.get("missing_modules", ()))
            )
        server_check = await self._bot_launcher.check_server(
            self._config.bot_server_host,
            self._config.bot_server_port,
        )
        if not server_check["available"]:
            blockers.append(
                "shared Minecraft world is not reachable at "
                f"{server_check['host']}:{server_check['port']}; enter the world "
                f"and open it to LAN on port {self._config.bot_server_port}"
            )
        return {
            "success": not blockers,
            "body_name": profile.name,
            "ready_to_start": not blockers,
            "blockers": blockers,
            "installation": None,
            "existing_window": None,
            "bot_directory": str(self._bot_launcher.directory),
            "server_address": (
                f"{server_check['host']}:{server_check['port']}"
            ),
            "server": server_check,
            "required_operations": sorted(profile.required_operations),
            "expected_bridge_version": self._config.expected_bridge_version,
            "token_bootstraps_on_launch": True,
        }

    async def _launch_bot_body(self, profile: BodyProfile, session_id: str) -> str:
        """Start the owned headless bot process; return exact failure text."""

        try:
            token = await asyncio.to_thread(
                MinecraftBotLauncher.ensure_token, profile.token_file
            )
        except Exception as exception:  # noqa: BLE001 - launch boundary
            return f"bot token bootstrap failed: {exception}"
        result = await self._bot_launcher.start(
            bridge_uri=profile.listen_uri or profile.uri,
            token=token,
            server_host=self._config.bot_server_host,
            server_port=self._config.bot_server_port,
            username=self._config.bot_username,
            minecraft_version=self._config.mc_version,
            instance_id=f"bot_{session_id}",
            observation_interval_ms=self._config.bot_observation_interval_ms,
            entity_radius_blocks=self._config.bot_entity_radius_blocks,
        )
        if result["success"]:
            self._state.launch_pid = result["pid"]
            self._state.window = None
            return ""
        return str(result["error"] or "bot process launch failed")

    async def start(self, goal: str = "", body_name: str = "") -> dict[str, Any]:
        """Launch Minecraft and connect one explicitly named body."""

        async with self._lifecycle_lock:
            return await self._start_locked(goal=goal, body_name=body_name)

    async def _start_locked(
        self,
        *,
        goal: str,
        body_name: str,
    ) -> dict[str, Any]:
        """Run one serialized startup without treating connectivity as readiness."""

        selected_name = body_name.strip() or self._config.default_body
        profiles = self._body_profiles()
        profile = profiles.get(selected_name)
        if profile is None:
            return {
                "success": False,
                "error": f"body is not configured: {selected_name}",
                "configured_bodies": sorted(profiles),
            }
        if self._state.active:
            if self._state.body_name != selected_name:
                return {
                    "success": False,
                    "error": "a different Minecraft body is already active",
                    "active_body": self._state.body_name,
                }
            status = await self.get_status()
            return {"success": True, "already_active": True, **status}
        if self._has_cleanup_pending():
            return {
                "success": False,
                "error": (
                    "the previous Minecraft session still has cleanup work; "
                    "call stop again before starting"
                ),
                "readiness": str(self._state.readiness),
                "readiness_detail": self._state.readiness_detail,
            }

        self._state = SessionState(
            body_name=selected_name,
            session_goal=goal,
            readiness=ReadinessState.PREFLIGHT,
            readiness_detail="checking installation and exact world launch contract",
        )

        if selected_name == "bot":
            installation = {
                "exists": True,
                "has_version": True,
                "bat_exists": True,
                "quick_play_configured": True,
            }
        else:
            try:
                installation = await self._launcher.check_installation()
            except Exception as exception:  # noqa: BLE001 - public lifecycle boundary
                self._state.readiness = ReadinessState.FAILED
                self._state.readiness_detail = (
                    f"installation inspection failed: {exception}"
                )
                self._state.last_error = self._state.readiness_detail
                return {"success": False, "error": self._state.readiness_detail}
        blockers: list[str] = []
        if selected_name == "bot":
            node_check = await self._bot_launcher.check_node()
            if not node_check["available"]:
                blockers.append(f"node runtime is unavailable: {node_check['error']}")
            dependency_check = self._bot_launcher.check_dependencies()
            if not dependency_check["entrypoint_exists"]:
                blockers.append(
                    "bot entrypoint is missing from integrations/minecraft_bot"
                )
            if not dependency_check.get("lockfile_exists", False):
                blockers.append("bot package-lock.json is missing")
            if not dependency_check["dependencies_installed"]:
                blockers.append(
                    "bot dependencies are not installed; run npm ci in "
                    "integrations/minecraft_bot; missing: "
                    + ", ".join(dependency_check.get("missing_modules", ()))
                )
            server_check = await self._bot_launcher.check_server(
                self._config.bot_server_host,
                self._config.bot_server_port,
            )
            if not server_check["available"]:
                blockers.append(
                    "shared Minecraft world is not reachable at "
                    f"{server_check['host']}:{server_check['port']}; enter the world "
                    f"and open it to LAN on port {self._config.bot_server_port}"
                )
        if not installation.get("exists"):
            blockers.append("Minecraft home is missing")
        if not installation.get("has_version"):
            blockers.append(
                f"NeoForge version for {self._config.mc_version} is missing"
            )
        if not installation.get("bat_exists"):
            blockers.append("launch script is missing")
        if (
            selected_name == "agent"
            and not self._in_shared_world_mode()
            and not installation.get("world_exists")
        ):
            blockers.append(
                f"configured world does not exist: {self._config.world_name}"
            )
        if selected_name == "agent" and not installation.get("bridge_mod_ready"):
            blockers.append(
                "the pinned Elysium NeoForge bridge artifact is not selected"
            )
        if selected_name == "agent" and not installation.get("baritone_mod_ready"):
            blockers.append(
                "the pinned official Baritone NeoForge artifact is not selected"
            )
        if (
            self._config.require_quick_play
            and not self._in_shared_world_mode()
            and not installation.get("quick_play_configured")
        ):
            blockers.append(
                "launch script does not enter the exact configured world with "
                "--quickPlaySingleplayer"
            )
        if blockers:
            self._state.readiness = ReadinessState.FAILED
            self._state.readiness_detail = "; ".join(blockers)
            self._state.last_error = self._state.readiness_detail
            return {
                "success": False,
                "error": "Minecraft preflight failed",
                "blockers": blockers,
                "installation": installation,
            }

        if self._config.consciousness_enabled:
            try:
                self._subject_context_binding = (
                    await self._load_subject_context_binding()
                )
            except Exception as exception:  # noqa: BLE001 - identity fails closed
                self._subject_context_binding = None
                self._state.readiness = ReadinessState.FAILED
                self._state.readiness_detail = (
                    "Minecraft subject context binding failed: " + str(exception)
                )
                self._state.last_error = self._state.readiness_detail
                return {
                    "success": False,
                    "error": self._state.readiness_detail,
                    "body_name": selected_name,
                }

        self._state.readiness = ReadinessState.LAUNCHING
        session_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
        if selected_name == "bot":
            self._state.readiness_detail = (
                "starting the owned headless bot body process"
            )
            launch_error = await self._launch_bot_body(profile, session_id)
            if launch_error:
                self._state.readiness = ReadinessState.FAILED
                self._state.readiness_detail = launch_error
                self._state.last_error = launch_error
                return {"success": False, "error": launch_error}
        else:
            self._state.readiness_detail = (
                "dispatching or reusing the exact Minecraft client"
            )
            try:
                launch = await self._launcher.launch()
            except Exception as exception:  # noqa: BLE001 - public lifecycle boundary
                self._state.readiness = ReadinessState.FAILED
                self._state.readiness_detail = f"Minecraft launch failed: {exception}"
                self._state.last_error = self._state.readiness_detail
                return {"success": False, "error": self._state.readiness_detail}
            if not launch.success:
                self._state.readiness = ReadinessState.FAILED
                self._state.readiness_detail = launch.error or "launch dispatch failed"
                self._state.last_error = self._state.readiness_detail
                return {"success": False, "error": launch.error}
            self._state.launch_pid = launch.pid
            self._state.window = launch.window
        trace = EmbodimentTrace(
            self._workspace / "minecraft" / "traces" / f"{session_id}.jsonl"
        )
        await trace.open()
        self._projected_trace_receipts.clear()
        self._traced_body_event_ids.clear()
        runtime = EmbodimentRuntime(trace, self._on_trace)

        try:
            self._state.readiness = ReadinessState.AWAITING_BRIDGE
            self._state.readiness_detail = "waiting for authenticated body bridge"
            client = await self._wait_for_bridge(profile)
            missing_operations = sorted(
                profile.required_operations.difference(client.capabilities)
            )
            if missing_operations:
                raise RuntimeError(
                    "body bridge is missing required operations: "
                    + ", ".join(missing_operations)
                )
            metadata = getattr(client, "hello_metadata", {})
            bridge_version = str(metadata.get("bridge_version") or "")
            if bridge_version != self._config.expected_bridge_version:
                raise RuntimeError(
                    "bridge version mismatch: "
                    f"expected {self._config.expected_bridge_version}, "
                    f"received {bridge_version or 'absent'}"
                )
            body = BridgeBody(profile.name, client)
            runtime.register_body(body)
            await runtime.select_body(profile.name)
            self._state.readiness = ReadinessState.AWAITING_WORLD
            self._state.readiness_detail = (
                "waiting for a playable world and advancing observations"
            )
            observation = await self._wait_for_playable_observation(body, profile)
        except Exception as exception:  # noqa: BLE001 - return exact readiness failure
            cleanup_error = await self._cleanup_runtime(runtime)
            bot_cleanup_error = await self._cleanup_bot_after_failed_start(
                selected_name
            )
            if cleanup_error:
                self._runtime = runtime
                self._trace = trace
            self._state.readiness = ReadinessState.FAILED
            self._state.readiness_detail = str(exception)
            self._state.last_error = str(exception)
            error = f"Minecraft body did not become ready: {exception}"
            if cleanup_error:
                error = f"{error}; cleanup failed: {cleanup_error}"
            if bot_cleanup_error:
                error = f"{error}; bot cleanup failed: {bot_cleanup_error}"
            return {
                "success": False,
                "error": error,
                "body_name": selected_name,
                "readiness": str(self._state.readiness),
                "trace_path": str(trace.path),
            }

        planner = JsonIntentPlanner(
            ElysiumModelDecisionSource(
                self._config.planner_task_name,
                activity_recorder=self._record_minecraft_planner_activity,
            ),
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
            readiness=ReadinessState.READY,
            readiness_detail="playable world and required body capabilities verified",
            launch_pid=self._state.launch_pid,
            window=self._state.window,
            game_instance_id=client.instance_id,
            bridge_version=str(
                getattr(client, "hello_metadata", {}).get("bridge_version") or ""
            )
            or None,
        )
        try:
            await self._register_consciousness()
            await self._report_scene("body connected")
            self._scene_open = True
            if self._config.consciousness_enabled:
                self._consciousness_runtime = self._create_consciousness_runtime()
                self._consciousness_runtime.start()
            self._start_body_event_pump()
        except Exception as exception:  # noqa: BLE001 - lifecycle must not look ready
            cleanup_errors: list[str] = []
            try:
                await self._close_body_event_pump()
            except Exception as cleanup_exception:  # noqa: BLE001
                cleanup_errors.append(
                    f"body event pump cleanup failed: {cleanup_exception}"
                )
            if self._consciousness_runtime is not None:
                try:
                    await self._consciousness_runtime.close()
                except Exception as cleanup_exception:  # noqa: BLE001
                    cleanup_errors.append(
                        f"consciousness cleanup failed: {cleanup_exception}"
                    )
                else:
                    self._consciousness_runtime = None
            try:
                await runtime.close()
            except Exception as cleanup_exception:  # noqa: BLE001
                cleanup_errors.append(f"body cleanup failed: {cleanup_exception}")
            else:
                self._runtime = None
                self._bridge_client = None
            try:
                await self._terminate_consciousness()
            except Exception as cleanup_exception:  # noqa: BLE001
                cleanup_errors.append(f"Presence cleanup failed: {cleanup_exception}")
            bot_cleanup_error = await self._cleanup_bot_after_failed_start(
                selected_name
            )
            if bot_cleanup_error:
                cleanup_errors.append(f"bot cleanup failed: {bot_cleanup_error}")
            self._state.active = False
            self._state.readiness = ReadinessState.FAILED
            self._state.readiness_detail = str(exception)
            self._state.last_error = str(exception)
            self._planner = None
            self._subject_context_binding = None
            error = f"Minecraft session lifecycle initialization failed: {exception}"
            if cleanup_errors:
                error = f"{error}; {'; '.join(cleanup_errors)}"
            return {
                "success": False,
                "error": error,
                "body_name": selected_name,
                "trace_path": str(trace.path),
            }

        self._state.readiness = ReadinessState.ACTIVE
        self._state.readiness_detail = "Minecraft embodiment session is active"
        return {
            "success": True,
            "session_id": session_id,
            "body_name": profile.name,
            "stream_id": stream_id,
            "consciousness_instance_id": instance_id,
            "game_instance_id": client.instance_id,
            "bridge_version": self._state.bridge_version,
            "advertised_operations": list(client.capabilities),
            "observation": observation.to_wire(),
            "trace_path": str(trace.path),
            "consciousness": (
                self._consciousness_runtime.status()
                if self._consciousness_runtime is not None
                else {"enabled": False, "running": False, "phase": "disabled"}
            ),
        }

    async def _record_minecraft_planner_activity(
        self,
        response: Any,
        input_document: dict[str, Any],
    ) -> None:
        """Append each embodied planning generation before an action may execute."""

        recorder = self._record_conscious_model_turn
        if not callable(recorder):
            return
        stream_id = str(self._state.stream_id or "").strip()
        instance_id = str(
            self._state.consciousness_instance_id or ""
        ).strip()
        if not stream_id or not instance_id:
            raise RuntimeError(
                "Minecraft planner activity has no active consciousness identity"
            )
        raw_intent = input_document.get("intent")
        intent = dict(raw_intent) if isinstance(raw_intent, Mapping) else {}
        intent_id = str(intent.get("intent_id") or "").strip()
        revision = int(intent.get("revision") or 0)
        observations = input_document.get("observations")
        receipts = input_document.get("receipts")
        observation_ids = [
            str(item.get("observation_id") or "")
            for item in observations
            if isinstance(item, Mapping)
        ] if isinstance(observations, list) else []
        receipt_ids = [
            str(item.get("receipt_id") or "")
            for item in receipts
            if isinstance(item, Mapping)
        ] if isinstance(receipts, list) else []
        turn_identity = json.dumps(
            {
                "intent_id": intent_id,
                "revision": revision,
                "observation_ids": observation_ids,
                "receipt_ids": receipt_ids,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        turn_digest = hashlib.sha256(
            turn_identity.encode("utf-8")
        ).hexdigest()[:24]
        turn_occurrence_id = (
            f"minecraft:{self._state.session_id}:embodiment:"
            f"{intent_id or 'unknown'}:{turn_digest}"
        )
        await recorder(
            stream_id=stream_id,
            source_instance_id=instance_id,
            turn_occurrence_id=turn_occurrence_id,
            transport_request_id=str(
                getattr(response, "request_record_id", "")
                or f"{turn_occurrence_id}:transport"
            ),
            provider_reasoning_content=str(
                getattr(response, "reasoning_content", "") or ""
            ),
            assistant_message=str(getattr(response, "message", "") or ""),
            calls=[],
            surface="minecraft_embodiment_planner",
        )

    async def _record_failed_minecraft_scene_turn(
        self,
        response: Any,
        context: MinecraftConsciousnessTurnContext,
    ) -> None:
        """Persist a generated scene round that could not become a decision."""

        recorder = self._record_conscious_model_turn
        if not callable(recorder):
            raise TypeError(
                "Minecraft failed model turn has no durable activity recorder"
            )
        if (
            context.session_id != self._state.session_id
            or context.stream_id != self._state.stream_id
            or context.instance_id != self._state.consciousness_instance_id
        ):
            raise RuntimeError(
                "Minecraft failed model turn identity does not match the session"
            )
        turn_occurrence_id = (
            f"minecraft:{context.session_id}:scene:{context.turn_index}"
        )
        await _invoke_callback(
            recorder,
            stream_id=context.stream_id,
            source_instance_id=context.instance_id,
            turn_occurrence_id=turn_occurrence_id,
            transport_request_id=str(
                getattr(response, "request_record_id", "")
                or f"{turn_occurrence_id}:transport"
            ),
            provider_reasoning_content=str(
                getattr(response, "reasoning_content", "") or ""
            ),
            assistant_message=str(getattr(response, "message", "") or ""),
            calls=[],
            surface="minecraft_scene_consciousness_failed_turn",
        )

    async def stop(self) -> dict[str, Any]:
        """Interrupt work, release controls, close bridges, and end the scene."""

        async with self._lifecycle_lock:
            return await self._stop_locked()

    async def _stop_locked(self) -> dict[str, Any]:
        """Stop idempotently while retaining exact cleanup diagnostics."""

        if not self._state.active and not self._has_cleanup_pending():
            self._state.readiness = ReadinessState.IDLE
            self._state.readiness_detail = "no Minecraft session is active"
            return {"success": True, "already_stopped": True}
        self._state.readiness = ReadinessState.CLOSING
        self._state.readiness_detail = "releasing controls and ending the scene"
        runtime = self._runtime
        consciousness_runtime = self._consciousness_runtime
        errors: list[str] = []
        if consciousness_runtime is not None:
            consciousness_runtime.request_stop()
        try:
            await self._close_body_event_pump()
        except Exception as exception:  # noqa: BLE001
            errors.append(f"body event pump cleanup failed: {exception}")
        if runtime is not None:
            try:
                await runtime.interrupt("Minecraft session stopped")
            except Exception as exception:  # noqa: BLE001
                errors.append(f"body interrupt failed: {exception}")
        if consciousness_runtime is not None:
            try:
                await consciousness_runtime.close()
            except Exception as exception:  # noqa: BLE001
                errors.append(f"consciousness cleanup failed: {exception}")
            else:
                self._consciousness_runtime = None
        if runtime is not None:
            try:
                await runtime.close()
            except Exception as exception:  # noqa: BLE001
                errors.append(f"body cleanup failed: {exception}")
            else:
                self._runtime = None
                self._bridge_client = None
        if self._scene_open:
            try:
                await self._report_scene("session ended")
            except Exception as exception:  # noqa: BLE001
                errors.append(f"World scene close failed: {exception}")
            else:
                self._scene_open = False
        if self._presence_registered:
            try:
                await self._terminate_consciousness()
            except Exception as exception:  # noqa: BLE001
                errors.append(f"Presence cleanup failed: {exception}")
        if self._state.body_name == "bot":
            bot_stop = await self._bot_launcher.stop()
            if not bot_stop.get("success", False):
                errors.append(f"bot process cleanup failed: {bot_stop.get('error')}")
        duration = self._state.duration_seconds
        session_id = self._state.session_id
        trace_path = str(self._trace.path) if self._trace else ""
        self._state.active = False
        self._planner = None
        self._execution_task = None
        if self._consciousness_runtime is None:
            self._subject_context_binding = None
        self._state.readiness = (
            ReadinessState.DEGRADED if errors else ReadinessState.IDLE
        )
        self._state.readiness_detail = "; ".join(errors) if errors else "session ended"
        self._state.last_error = self._state.readiness_detail if errors else None
        return {
            "success": not errors,
            "session_id": session_id,
            "duration_seconds": duration,
            "conclusions": list(self._state.conclusions),
            "trace_path": trace_path,
            "game_left_running": self._state.body_name != "bot",
            "errors": errors,
            "cleanup_pending": self._has_cleanup_pending(),
        }

    async def close(self) -> dict[str, Any]:
        """Service-owned idempotent shutdown entrypoint."""

        return await self.stop()

    async def do_intent(
        self,
        intent: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Serialize an external intention with the dedicated scene runtime."""

        async with self._intent_lock:
            result = await self._do_intent_locked(
                intent,
                timeout,
                include_recent_subconscious=True,
            )
        consciousness = self._consciousness_runtime
        if consciousness is not None and consciousness.running:
            consciousness.wake("external_intention_finished")
        return result

    async def _execute_consciousness_intent(
        self,
        intent: str,
    ) -> dict[str, Any]:
        """Execute one scene-authored intention without manufacturing a wake."""

        async with self._intent_lock:
            runtime = self._consciousness_runtime
            decision_id = (
                str(runtime.status().get("active_decision_id") or "")
                if runtime is not None
                else ""
            )
            return await self._do_intent_locked(
                intent,
                None,
                include_recent_subconscious=False,
                consciousness_decision_id=decision_id,
            )

    async def _execute_consciousness_decision(
        self,
        decision: MinecraftConsciousnessDecision,
    ) -> dict[str, Any]:
        """Dispatch authored speech and one body-owned task without waiting for it."""

        async with self._intent_lock:
            client = self._bridge_client
            trace = self._trace
            if not self._state.active or client is None or trace is None:
                return {"success": False, "error": "no Minecraft session is active"}
            await self._refresh_consciousness(
                "minecraft_consciousness_action_requested"
            )
            receipts: list[dict[str, Any]] = []
            observation_id = (
                self._state.latest_observation.observation_id
                if self._state.latest_observation is not None
                else None
            )

            async def dispatch(
                *,
                suffix: str,
                operation: str,
                parameters: Mapping[str, Any],
            ) -> Any:
                command = ActionCommand(
                    command_id=f"{decision.decision_id}:{suffix}",
                    intent_id=decision.decision_id,
                    intent_revision=max(1, decision.turn_index),
                    issued_at=decision.authored_at,
                    operation=operation,
                    parameters=dict(parameters),
                    based_on_observation=observation_id,
                    timeout_seconds=15.0,
                )
                issued = await trace.append("command.issued", command.to_wire())
                await self._on_trace(issued)
                receipt = await client.act(command)
                if receipt.command_id != command.command_id:
                    raise RuntimeError(
                        "Minecraft scene receipt command identity changed"
                    )
                if receipt.intent_id != decision.decision_id:
                    raise RuntimeError(
                        "Minecraft scene receipt decision identity changed"
                    )
                recorded = await trace.append("command.receipt", receipt.to_wire())
                await self._on_trace(recorded)
                receipts.append(receipt.to_wire())
                if (
                    not receipt.accepted
                    or not receipt.completed
                    or receipt.interrupted
                    or receipt.error is not None
                ):
                    raise RuntimeError(
                        receipt.error
                        or f"Minecraft body rejected {operation} without an error"
                    )
                return receipt

            try:
                if decision.speech:
                    await dispatch(
                        suffix="speech",
                        operation="chat.send",
                        parameters={"message": decision.speech},
                    )
                task_id = ""
                if decision.task is not None:
                    task_id = f"{decision.decision_id}:task"
                    receipt = await dispatch(
                        suffix="task-start",
                        operation="task.start",
                        parameters={
                            "task_id": task_id,
                            "kind": decision.task.kind,
                            "arguments": dict(decision.task.arguments),
                            "replace_current": decision.task.replace_current,
                        },
                    )
                    if receipt.facts.get("task_accepted") is not True:
                        raise RuntimeError(
                            "Minecraft body did not prove high-level task acceptance"
                        )
            except Exception as exception:  # noqa: BLE001 - evidence boundary
                self._state.last_error = str(exception)
                return {
                    "success": False,
                    "decision_id": decision.decision_id,
                    "error": str(exception),
                    "receipts": receipts,
                }
            return {
                "success": True,
                "decision_id": decision.decision_id,
                "task_id": task_id,
                "task_dispatched": decision.task is not None,
                "speech_dispatched": bool(decision.speech),
                "receipts": receipts,
            }

    def _start_body_event_pump(self) -> None:
        """Start one owned durable consumer for pushed game occurrences."""

        if self._body_event_task is not None and not self._body_event_task.done():
            return
        client = self._bridge_client
        if client is None:
            raise RuntimeError("Minecraft body event pump has no bridge client")
        task_kinds = tuple(client.hello_metadata.get("task_kinds") or ())
        if self._record_minecraft_body_event is None:
            if task_kinds:
                raise RuntimeError(
                    "high-level Minecraft tasks require a durable body event recorder"
                )
            return
        task_info = get_task_manager().create_task(
            self._run_body_event_pump(client),
            name=f"minecraft_body_events:{self._state.session_id}",
            daemon=True,
            metadata={
                "component": "minecraft_body_events",
                "session_id": self._state.session_id,
                "instance_id": self._state.consciousness_instance_id,
            },
        )
        if task_info.task is None:
            raise RuntimeError("Minecraft body event task was not created")
        self._body_event_task_id = task_info.task_id
        self._body_event_task = task_info.task

    async def _run_body_event_pump(self, client: MinecraftBridgeClient) -> None:
        """Persist each FIFO event before acknowledgement and wake the scene."""

        while self._state.active and self._bridge_client is client:
            try:
                event = await client.next_event()
                if event.event_id not in self._traced_body_event_ids:
                    trace = self._trace
                    if trace is None:
                        raise RuntimeError("Minecraft body event trace is absent")
                    await trace.append("body.event", event.to_wire())
                    self._traced_body_event_ids.add(event.event_id)
                await _invoke_callback(
                    self._record_minecraft_body_event,
                    {"schema": "minecraft.body_event.v1", **event.to_wire()},
                    {
                        "schema": "minecraft.body_event_context.v1",
                        "session_id": self._state.session_id,
                        "stream_id": self._state.stream_id,
                        "instance_id": self._state.consciousness_instance_id,
                        "body_name": self._state.body_name,
                    },
                )
                self._state.body_event_count += 1
                self._state.last_body_event = event.to_wire()
                consciousness = self._consciousness_runtime
                if (
                    consciousness is not None
                    and consciousness.running
                    and event.kind in _BODY_EVENT_WAKE_KINDS
                ):
                    consciousness.wake(f"{event.kind}:{event.event_id}")
                await client.acknowledge_event(event.event_id)
            except asyncio.CancelledError:
                raise
            except BridgeProtocolError as exception:
                self._state.last_error = str(exception)
                self._state.readiness = ReadinessState.DEGRADED
                self._state.readiness_detail = (
                    f"Minecraft body event protocol failed: {exception}"
                )
                consciousness = self._consciousness_runtime
                if consciousness is not None and consciousness.running:
                    consciousness.wake("body_event_protocol_failed")
                return
            except (ConnectionError, OSError) as exception:
                self._state.last_error = str(exception)
                self._state.readiness = ReadinessState.DEGRADED
                self._state.readiness_detail = (
                    f"Minecraft body event bridge disconnected: {exception}"
                )
                consciousness = self._consciousness_runtime
                if consciousness is not None and consciousness.running:
                    consciousness.wake("body_event_bridge_disconnected")
                try:
                    await client.wait_until_connected()
                except BridgeDisconnectedError:
                    return
                if not self._state.active or self._bridge_client is not client:
                    return
                self._state.last_error = ""
                self._state.readiness = ReadinessState.ACTIVE
                self._state.readiness_detail = (
                    "Minecraft body bridge reconnected; durable event delivery resumed"
                )
                if consciousness is not None and consciousness.running:
                    consciousness.wake("body_event_bridge_reconnected")
            except Exception as exception:  # noqa: BLE001 - retain FIFO head
                self._state.last_error = str(exception)
                await asyncio.sleep(1.0)

    async def _close_body_event_pump(self) -> None:
        """Cancel and await the owned body-event consumer idempotently."""

        task = self._body_event_task
        self._body_event_task = None
        self._body_event_task_id = None
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if task.done() and not task.cancelled():
            exception = task.exception()
            if exception is not None:
                raise exception

    async def _do_intent_locked(
        self,
        intent: str,
        timeout: float | None,
        *,
        include_recent_subconscious: bool,
        consciousness_decision_id: str = "",
    ) -> dict[str, Any]:
        """Pursue one exact intention while the caller owns the intent lock."""

        if not self._state.active or self._runtime is None or self._planner is None:
            return {"success": False, "error": "no Minecraft session is active"}
        if not intent.strip():
            return {"success": False, "error": "intent must not be empty"}
        try:
            await self._refresh_consciousness("minecraft_intent_requested")
        except Exception as exception:  # noqa: BLE001 - Presence is part of success
            self._state.last_error = str(exception)
            return {"success": False, "error": str(exception)}
        durable_context: dict[str, Any] = {
            "session_id": self._state.session_id,
            "session_goal": self._state.session_goal,
            "stream_id": self._state.stream_id,
        }
        if consciousness_decision_id:
            durable_context["consciousness_decision_id"] = consciousness_decision_id
        transient_prompt_context: dict[str, Any] = {}
        try:
            if (
                include_recent_subconscious
                and self._get_recent_subconscious_context is not None
            ):
                recent_subconscious = await _invoke_callback(
                    self._get_recent_subconscious_context,
                    group_limit=self._config.consciousness_subconscious_group_limit,
                    max_bytes=self._config.consciousness_subconscious_max_bytes,
                    include_tool_payloads=False,
                )
                if not isinstance(recent_subconscious, RecentSubconsciousContext):
                    raise TypeError(
                        "Minecraft recent subconscious callback must return "
                        "RecentSubconsciousContext"
                    )
                encoded = recent_subconscious.content.encode("utf-8")
                if (
                    len(encoded) != recent_subconscious.delivered_bytes
                    or hashlib.sha256(encoded).hexdigest()
                    != recent_subconscious.projection_sha256
                ):
                    raise RuntimeError(
                        "Minecraft recent subconscious content does not match "
                        "its projection metadata"
                    )
                if recent_subconscious.content:
                    transient_prompt_context["recent_subconscious_context"] = (
                        recent_subconscious.content
                    )
                    durable_context["recent_subconscious_reference"] = {
                        "schema": "minecraft.recent_subconscious_reference.v1",
                        "algorithm_version": recent_subconscious.algorithm_version,
                        "projection_sha256": recent_subconscious.projection_sha256,
                        "delivered_bytes": recent_subconscious.delivered_bytes,
                        "from_sequence": recent_subconscious.from_sequence,
                        "through_sequence": recent_subconscious.through_sequence,
                        "group_count": recent_subconscious.group_count,
                        "source_group_count": recent_subconscious.source_group_count,
                        "omitted_group_count": recent_subconscious.omitted_group_count,
                        "truncated": recent_subconscious.truncated,
                    }
            embodied_intent = EmbodiedIntent(
                text=intent,
                body_name=self._state.body_name,
                durable_context=durable_context,
                transient_prompt_context=transient_prompt_context,
            )
        except Exception as exception:  # noqa: BLE001 - public intent boundary
            self._state.last_error = str(exception)
            return {"success": False, "error": str(exception)}
        self._state.active_intent = intent
        execution_timeout = (
            timeout if timeout is not None else self._config.intent_timeout_seconds
        )
        if execution_timeout is None:
            execution_timeout = 300.0
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
        await self._refresh_consciousness("minecraft_interrupt_requested")
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
        await self._refresh_consciousness("minecraft_look")
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

    def _in_shared_world_mode(self) -> bool:
        """True when her own client joins the human player's LAN world."""

        return bool(getattr(self._config, "shared_world_enabled", False))

    async def grab_vision_frame_bytes(self) -> bytes | None:
        """Return the latest first-person frame as JPEG bytes for her own eyes.

        The bytes feed her native multimodal model directly (image payload);
        nothing is translated into words first.  Returns None when no session
        is active or no renderable window exists (e.g. headless bot body).
        """

        if not self._state.active or self._runtime is None:
            return None
        if self._state.body_name == "bot":
            # The headless bot body renders nothing; capturing here would
            # steal whichever desktop window matches the title rule and
            # inject the human player's perspective as her own eyes.
            return None
        try:
            frame = await self._capture.grab_consciousness_frame()
        except Exception:  # noqa: BLE001 - vision must not break the scene loop
            return None
        if frame is None:
            return None

        def _encode() -> bytes:
            import io

            from PIL import Image as PILImage

            image = frame.image
            if image.width > 1280:
                ratio = 1280 / image.width
                image = image.resize(
                    (1280, max(1, int(image.height * ratio))), PILImage.LANCZOS
                )
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=85)
            return buffer.getvalue()

        try:
            return await asyncio.to_thread(_encode)
        except Exception:  # noqa: BLE001
            return None

    async def _load_subject_context_binding(
        self,
    ) -> MinecraftSubjectContextBinding:
        """Pin one unified subject projection before acquiring a game body."""

        callback = self._get_subject_context_projection_snapshot
        if callback is None:
            raise RuntimeError(
                "Minecraft consciousness requires the subject projection service"
            )
        snapshot = await _invoke_callback(
            callback,
            projection_kind="minecraft",
            max_bytes=self._config.consciousness_subject_context_max_bytes,
        )
        if not isinstance(snapshot, Mapping):
            raise TypeError(
                "Minecraft subject projection callback must return a mapping"
            )
        return MinecraftSubjectContextBinding.from_snapshot(
            snapshot,
            expected_max_bytes=(self._config.consciousness_subject_context_max_bytes),
        )

    def _create_consciousness_runtime(self) -> MinecraftConsciousnessRuntime:
        """Construct the independent scene loop after body and Presence are ready."""

        binding = self._subject_context_binding
        if binding is None:
            raise RuntimeError("Minecraft subject context was not pinned")
        if self._record_minecraft_consciousness_decision is None:
            raise RuntimeError(
                "Minecraft consciousness requires a durable decision recorder"
            )
        decision_source = self._injected_consciousness_decision_source
        if decision_source is None:
            decision_source = ElysiumMinecraftDecisionSource(
                self._config.consciousness_task_name,
                observation_max_bytes=(
                    self._config.consciousness_observation_max_bytes
                ),
                min_wait_seconds=self._config.consciousness_min_wait_seconds,
                max_wait_seconds=self._config.consciousness_max_wait_seconds,
                failed_turn_recorder=self._record_failed_minecraft_scene_turn,
            )
        client = self._bridge_client
        if client is None:
            raise RuntimeError("Minecraft consciousness bridge is absent")
        task_kinds = tuple(
            str(item).strip()
            for item in (client.hello_metadata.get("task_kinds") or ())
            if str(item).strip()
        )
        return MinecraftConsciousnessRuntime(
            session_id=self._state.session_id,
            stream_id=self._state.stream_id,
            instance_id=self._state.consciousness_instance_id,
            body_name=self._state.body_name,
            session_goal=self._state.session_goal,
            subject=binding,
            decision_source=decision_source,
            perception_source=self._perceive_for_consciousness,
            execute_intent=self._execute_consciousness_intent,
            execute_scene_decision=self._execute_consciousness_decision,
            record_decision=self._record_consciousness_decision,
            request_end_session=self._request_consciousness_session_end,
            refresh_presence=self._refresh_consciousness,
            recent_turn_limit=self._config.consciousness_recent_turn_limit,
            retry_base_seconds=self._config.consciousness_retry_base_seconds,
            retry_max_seconds=self._config.consciousness_retry_max_seconds,
            stop_timeout_seconds=self._config.consciousness_stop_timeout_seconds,
            max_session_seconds=float(self._config.max_session_minutes) * 60.0,
            task_kinds=task_kinds,
        )

    async def _perceive_for_consciousness(
        self,
    ) -> MinecraftConsciousnessPerception:
        """Persist a fresh structured observation and collect bounded scene input."""

        async with self._intent_lock:
            if (
                not self._state.active
                or self._bridge_client is None
                or self._trace is None
            ):
                raise RuntimeError("Minecraft consciousness body is not active")
            latest = self._state.latest_observation
            observation = await self._bridge_client.observe(
                latest.sequence if latest is not None else None
            )
            record = await self._trace.append("observation", observation.to_wire())
            await self._on_trace(record)
            frame_bytes = await self.grab_vision_frame_bytes()

        recent_subconscious = RecentSubconsciousContext.empty()
        if self._get_recent_subconscious_context is not None:
            recent_subconscious = await _invoke_callback(
                self._get_recent_subconscious_context,
                group_limit=self._config.consciousness_subconscious_group_limit,
                max_bytes=self._config.consciousness_subconscious_max_bytes,
                include_tool_payloads=False,
            )
        if not isinstance(recent_subconscious, RecentSubconsciousContext):
            raise TypeError(
                "Minecraft recent subconscious callback must return "
                "RecentSubconsciousContext"
            )
        encoded = recent_subconscious.content.encode("utf-8")
        if (
            len(encoded) != recent_subconscious.delivered_bytes
            or hashlib.sha256(encoded).hexdigest()
            != recent_subconscious.projection_sha256
        ):
            raise RuntimeError(
                "Minecraft recent subconscious content does not match its metadata"
            )
        return MinecraftConsciousnessPerception(
            observation=observation,
            frame_bytes=frame_bytes,
            recent_subconscious=recent_subconscious,
        )

    async def _record_consciousness_decision(
        self,
        decision: MinecraftConsciousnessDecision,
        context: MinecraftConsciousnessTurnContext,
    ) -> None:
        """Require an attributed Life Event before physical execution."""

        callback = self._record_minecraft_consciousness_decision
        if callback is None:
            raise RuntimeError("Minecraft consciousness decision recorder is absent")
        await _invoke_callback(
            callback,
            decision.to_record(),
            context.reference(),
        )

    async def _request_consciousness_session_end(self, reason: str) -> None:
        """Schedule lifecycle closure outside the consciousness task itself."""

        task_info = get_task_manager().create_task(
            self.stop(),
            name=f"minecraft_consciousness_end:{self._state.session_id}",
            daemon=True,
            metadata={
                "component": "minecraft_consciousness",
                "session_id": self._state.session_id,
                "reason": str(reason or "")[:240],
            },
        )
        if task_info.task is None:
            raise RuntimeError("Minecraft end-session task was not created")

    async def get_status(self) -> dict[str, Any]:
        """Return session, body, planner, and latest evidence status."""

        if self._state.active:
            await self._refresh_consciousness("minecraft_status")
        client = self._bridge_client
        observation = self._state.latest_observation
        return {
            "active": self._state.active,
            "readiness": str(self._state.readiness),
            "readiness_detail": self._state.readiness_detail,
            "session_id": self._state.session_id,
            "body_name": self._state.body_name,
            "stream_id": self._state.stream_id,
            "consciousness_instance_id": self._state.consciousness_instance_id,
            "duration_seconds": self._state.duration_seconds,
            "session_goal": self._state.session_goal,
            "active_intent": self._state.active_intent,
            "bridge_connected": bool(client and client.connected),
            "game_instance_id": (
                client.instance_id if client else self._state.game_instance_id
            ),
            "bridge_version": self._state.bridge_version,
            "body_event_count": self._state.body_event_count,
            "last_body_event": self._state.last_body_event,
            "launch_pid": self._state.launch_pid,
            "window": self._state.window,
            "advertised_operations": list(client.capabilities) if client else [],
            "latest_observation": observation.to_wire() if observation else None,
            "conclusions": list(self._state.conclusions),
            "last_error": self._state.last_error,
            "consciousness": (
                self._consciousness_runtime.status()
                if self._consciousness_runtime is not None
                else {
                    "enabled": bool(self._config.consciousness_enabled),
                    "running": False,
                    "phase": (
                        "not_started"
                        if self._config.consciousness_enabled
                        else "disabled"
                    ),
                }
            ),
            "cleanup_pending": self._has_cleanup_pending(),
        }

    async def _wait_for_playable_observation(
        self,
        body: BridgeBody,
        profile: BodyProfile,
    ) -> WorldObservation:
        """Require the selected body to produce two advancing ready observations."""

        deadline = time.monotonic() + self._config.world_ready_timeout_seconds
        observation: WorldObservation | None = None
        last_reason = "body has not emitted an observation"
        while time.monotonic() < deadline:
            try:
                observation = await body.observe(
                    observation.sequence if observation is not None else None
                )
            except TimeoutError as exception:
                last_reason = str(exception)
                continue
            ready, reason = self._observation_is_playable(observation, profile)
            if not ready:
                last_reason = reason
                continue
            confirmation = await body.observe(observation.sequence)
            confirmed, confirmation_reason = self._observation_is_playable(
                confirmation, profile
            )
            if confirmed:
                return confirmation
            observation = confirmation
            last_reason = confirmation_reason
        raise TimeoutError(
            "playable Minecraft world did not become ready before the deadline: "
            + last_reason
        )

    def _observation_is_playable(
        self,
        observation: WorldObservation,
        profile: BodyProfile,
    ) -> tuple[bool, str]:
        """Evaluate technical body readiness without assigning subjective meaning."""

        facts = observation.facts
        if profile.readiness_kind == "server_world":
            if facts.get("world_loaded") is not True:
                screen = facts.get("screen")
                screen_name = (
                    str(screen.get("class") or screen.get("title") or "unknown")
                    if isinstance(screen, dict)
                    else "unknown"
                )
                return (
                    False,
                    f"no playable server world is loaded; current screen={screen_name}",
                )
            world = facts.get("world")
            if not isinstance(world, dict):
                return False, "bridge observation is missing world identity"
            player = facts.get("player")
            if not isinstance(player, dict) or not str(player.get("uuid") or ""):
                return (
                    False,
                    "playable world observation is missing player identity",
                )
            return True, "server world is ready"
        if profile.readiness_kind == "structured_world":
            if facts.get("world_loaded") is not True:
                screen = facts.get("screen")
                screen_name = (
                    str(screen.get("class") or screen.get("title") or "unknown")
                    if isinstance(screen, dict)
                    else "unknown"
                )
                return (
                    False,
                    f"no playable world is loaded; current screen={screen_name}",
                )
            world = facts.get("world")
            if not isinstance(world, dict):
                return False, "bridge observation is missing world identity"
            actual_name = str(world.get("singleplayer_name") or "")
            if actual_name.casefold() != self._config.world_name.casefold():
                return False, (
                    "wrong singleplayer world is loaded: "
                    f"expected {self._config.world_name}, received {actual_name or 'unknown'}"
                )
            if facts.get("client_paused") is True:
                return (
                    False,
                    "loaded singleplayer world is paused; resume before embodiment",
                )
            player = facts.get("player")
            if not isinstance(player, dict) or not str(player.get("uuid") or ""):
                return False, "playable world observation is missing player identity"
            return True, "structured world is ready"

        frame_path = observation.frame_path
        window = facts.get("window")
        capture = facts.get("capture")
        if not frame_path:
            return False, "first-person body did not persist a frame"
        if not isinstance(window, dict) or not window.get("visible"):
            return False, "bound Minecraft window is not visible"
        if not isinstance(capture, dict):
            return False, "first-person capture metadata is missing"
        if int(capture.get("width") or 0) <= 0 or int(capture.get("height") or 0) <= 0:
            return False, "first-person frame dimensions are invalid"
        return True, "first-person frame body is ready"

    @staticmethod
    async def _cleanup_runtime(runtime: EmbodimentRuntime) -> str:
        """Close one partial runtime and return exact cleanup failure text."""

        try:
            await runtime.close()
        except Exception as exception:  # noqa: BLE001
            return str(exception)
        return ""

    async def _cleanup_bot_after_failed_start(self, body_name: str) -> str:
        """Release a bot process acquired before readiness was established."""

        if body_name != "bot":
            return ""
        try:
            result = await self._bot_launcher.stop()
        except Exception as exception:  # noqa: BLE001 - preserve retry state
            return str(exception)
        if result.get("success", False):
            return ""
        return str(result.get("error") or "bot process cleanup failed")

    def _body_profiles(self) -> dict[str, BodyProfile]:
        """Build exact configured profiles for the two requested body routes."""

        agent_readiness_kind = (
            "server_world" if self._in_shared_world_mode() else "structured_world"
        )
        return {
            "agent": BodyProfile(
                name="agent",
                uri=self._config.agent_bridge_uri,
                listen_uri=self._config.agent_bridge_listen_uri,
                token_file=self._config.agent_token_file,
                planner_guidance=AGENT_BRIDGE_GUIDANCE,
                required_operations=frozenset(
                    {
                        "chat.send",
                        "control.release_all",
                        "movement.input",
                        "navigation.goto",
                        "navigation.stop",
                        "player.respawn",
                        "world.mine",
                    }
                ),
                readiness_kind=agent_readiness_kind,
            ),
            "biomimetic": BodyProfile(
                name="biomimetic",
                uri=self._config.biomimetic_bridge_uri,
                listen_uri=self._config.biomimetic_bridge_listen_uri,
                token_file=self._config.biomimetic_token_file,
                planner_guidance=BIOMIMETIC_GUIDANCE,
                required_operations=frozenset(
                    {
                        "control.release_all",
                        "native.input_batch",
                    }
                ),
                readiness_kind="first_person_frame",
            ),
            "bot": BodyProfile(
                name="bot",
                uri=self._config.bot_bridge_uri,
                listen_uri=self._config.bot_bridge_listen_uri,
                token_file=self._workspace / self._config.bot_token_file,
                planner_guidance=BOT_BRIDGE_GUIDANCE,
                required_operations=frozenset(
                    {
                        "chat.send",
                        "control.release_all",
                        "movement.input",
                        "navigation.goto",
                        "navigation.stop",
                        "player.respawn",
                        "task.cancel",
                        "task.start",
                        "task.status",
                        "world.mine",
                    }
                ),
                readiness_kind="server_world",
            ),
        }

    async def _wait_for_bridge(self, profile: BodyProfile) -> MinecraftBridgeClient:
        """Wait for a launched body endpoint; never switch to another profile."""

        deadline = time.monotonic() + self._config.bridge_ready_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if profile.token_file.exists():
                try:
                    token = await asyncio.to_thread(
                        self._read_token, profile.token_file
                    )
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
                except (
                    OSError,
                    TimeoutError,
                    ConnectionError,
                    ValueError,
                ) as exception:
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

    async def _on_trace(self, record: TraceRecord) -> None:
        """Publish one bounded World receipt after durable trace persistence."""

        if record.kind == "observation":
            self._state.latest_observation = WorldObservation.from_wire(record.payload)
        await self._refresh_consciousness(f"minecraft_trace:{record.kind}")
        if self._report_world_observation is not None and self._state.stream_id:
            receipt = build_world_trace_receipt(
                record,
                session_id=self._state.session_id,
                stream_id=self._state.stream_id,
                body_name=self._state.body_name,
            )
            projection_id = str(receipt["projection_id"])
            async with self._trace_projection_lock:
                if projection_id in self._projected_trace_receipts:
                    return
                await _invoke_callback(
                    self._report_world_observation,
                    f"Minecraft embodied trace receipt: {projection_id}",
                    source_instance_id=self._state.consciousness_instance_id,
                    subject=self._state.stream_id,
                    predicate="embodied_trace",
                    domain="minecraft",
                    status="durable_receipt",
                    stream_id=self._state.stream_id,
                    observed_at=record.recorded_at,
                    valid_from=record.recorded_at,
                    occurrence_id=projection_id,
                    value=receipt,
                )
                self._projected_trace_receipts.add(projection_id)

    async def _refresh_consciousness(self, reason: str) -> None:
        """Resume an expired Presence lease or renew the active one durably."""

        if (
            self._registry is None
            or not self._presence_registered
            or not self._state.consciousness_instance_id
        ):
            return
        instance_id = self._state.consciousness_instance_id
        instance = await _invoke_callback(self._registry.get, instance_id)
        if instance is None:
            raise RuntimeError("Minecraft Presence instance is missing")
        if getattr(instance, "status", "") == "terminated":
            raise RuntimeError("Minecraft Presence instance is terminated")

        timestamp = datetime.now(UTC).isoformat()
        resumed = False
        if bool(getattr(instance, "is_suspended", False)):
            resume = self._resume_presence or getattr(self._registry, "resume", None)
            if resume is None:
                raise RuntimeError(
                    "Minecraft Presence registry cannot resume an expired lease"
                )
            resumed = bool(
                await _invoke_callback(
                    resume,
                    instance_id,
                    timestamp=timestamp,
                    reason=reason,
                )
            )
        if not resumed:
            touch = self._touch_presence or getattr(self._registry, "touch", None)
            if touch is None:
                raise RuntimeError("Minecraft Presence lifecycle cannot touch")
            await _invoke_callback(
                touch,
                instance_id,
                timestamp=timestamp,
                reason=reason,
            )
        if self._touch_presence is None and self._save_registry is not None:
            await _invoke_callback(self._save_registry)

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
        register = self._register_presence or getattr(
            self._registry,
            "register",
            None,
        )
        if register is None:
            raise RuntimeError("Minecraft Presence lifecycle cannot register")
        await _invoke_callback(register, instance)
        self._presence_registered = True
        if self._register_presence is None and self._save_registry is not None:
            await _invoke_callback(self._save_registry)

    async def _terminate_consciousness(self) -> None:
        """Terminate this session's registered consciousness instance."""

        if self._registry is None or not self._state.consciousness_instance_id:
            self._presence_registered = False
            return
        terminate = self._terminate_presence or getattr(
            self._registry,
            "terminate",
            None,
        )
        if terminate is None:
            raise RuntimeError("Minecraft Presence lifecycle cannot terminate")
        await _invoke_callback(
            terminate,
            self._state.consciousness_instance_id,
            reason="minecraft_session_ended",
        )
        if self._terminate_presence is None and self._save_registry is not None:
            await _invoke_callback(self._save_registry)
        self._presence_registered = False

    def _has_cleanup_pending(self) -> bool:
        """Return whether shutdown must retry any owned lifecycle resource."""

        return (
            self._runtime is not None
            or self._consciousness_runtime is not None
            or self._body_event_task is not None
            or self._presence_registered
            or self._scene_open
            or (
                self._state.body_name == "bot"
                and getattr(self._bot_launcher, "pid", None) is not None
            )
        )

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
