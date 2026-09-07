"""Plugin-owned Minecraft lifecycle; shared subject identity and event authority."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

from src.core.components.base.service import BaseService
from plugins.life_engine.service.registry import get_life_engine_service
from plugins.life_engine.service.scene_extensions import (
    SceneExtension, register_scene_extension, unregister_scene_extension,
)

from .events import MinecraftEventBuilder
from .launcher import MCConfig
from .session import MinecraftSession


MINECRAFT_EXTENSION = SceneExtension(
    kind="minecraft",
    tools=(
        "tool-nucleus_opportunity_query", "tool-nucleus_opportunity_command",
        "tool-nucleus_capability_call", "tool-nucleus_minecraft",
        "tool-nucleus_proactive_query", "tool-nucleus_proactive_command",
        "action-life_send_text", "action-report_state",
    ),
    chat_tools=("tool-nucleus_minecraft",),
    result_before_reply_tools=("tool-nucleus_minecraft",),
)


class MinecraftService(BaseService):
    """Own exactly one game session, without owning a second mind or memory store."""

    service_name = "minecraft"
    service_description = "独立 Minecraft 会话、身体与统一意识接入"
    version = "0.1.0"
    dependencies = ["life_engine:service:life_engine"]
    _instance: ClassVar[MinecraftService | None] = None

    def __new__(cls, plugin: Any):
        # The component manager may request multiple service handles. Ownership
        # remains with this plugin's one service, including partially closed state.
        existing = getattr(plugin, "_service", None)
        if isinstance(existing, cls):
            return existing
        instance = super().__new__(cls)
        plugin._service = instance
        return instance

    def __init__(self, plugin: Any) -> None:
        if getattr(self, "_initialized", False):
            return
        super().__init__(plugin)
        self._initialized = True
        self._session: MinecraftSession | None = None
        self._life: Any | None = None
        self._lock = asyncio.Lock()
        self._events = MinecraftEventBuilder()

    @classmethod
    def get_instance(cls) -> MinecraftService | None:
        return cls._instance

    @property
    def session(self) -> MinecraftSession | None:
        return self._session

    def _create_session(self, life: Any) -> MinecraftSession:
        section = self.plugin.config.settings
        raw = section.model_dump()
        fields = set(MCConfig.__dataclass_fields__)
        values = {name: value for name, value in raw.items() if name in fields}
        for name in ("mc_home", "agent_token_file", "biomimetic_token_file"):
            if name in values:
                values[name] = Path(values[name])
        return MinecraftSession(
            workspace=life.workspace_directory,
            mc_config=MCConfig(**values),
            consciousness_registry=life.consciousness_registry,
            register_consciousness_instance=life.register_consciousness_instance,
            touch_consciousness_instance=life.touch_consciousness_instance,
            resume_consciousness_instance=life.resume_consciousness_instance,
            terminate_consciousness_instance=life.terminate_consciousness_instance,
            get_recent_subconscious_context=life.get_recent_subconscious_context,
            get_subject_context_projection_snapshot=(
                life.get_subject_context_projection_snapshot
            ),
            record_minecraft_consciousness_decision=(
                self.record_minecraft_consciousness_decision
            ),
            record_minecraft_body_event=self.record_minecraft_body_event,
            record_conscious_model_turn=life.record_conscious_model_turn,
            report_world_observation=life.report_world_observation,
        )

    async def start(self) -> None:
        """Initialize an inactive capability; loading never launches a game."""

        async with self._lock:
            if self._session is not None:
                return
            if not self.plugin.config.settings.enabled:
                return
            if type(self)._instance not in (None, self):
                raise RuntimeError("MinecraftServiceAlreadyOwned")
            life = get_life_engine_service()
            if life is None:
                raise RuntimeError("MinecraftRequiresRunningLifeEngine")
            self._life = life
            try:
                register_scene_extension(self, replace(
                    MINECRAFT_EXTENSION,
                    evidence_budget_bytes=self.plugin.config.settings.evidence_max_result_bytes,
                ))
                self._session = self._create_session(life)
                type(self)._instance = self
            except BaseException:
                unregister_scene_extension(self)
                self._life = None
                raise

    async def stop(self) -> None:
        """Close only owned resources; retain the handle when cleanup fails."""

        async with self._lock:
            if self._session is not None:
                result = await self._session.close()
                if isinstance(result, dict) and result.get("success") is False:
                    raise RuntimeError("MinecraftSessionCloseFailed")
                self._session = None
            unregister_scene_extension(self)
            if type(self)._instance is self:
                type(self)._instance = None
            self._life = None

    async def record_minecraft_consciousness_decision(
        self, decision: Mapping[str, Any], context_reference: Mapping[str, Any],
    ) -> Any:
        if self._life is None:
            raise RuntimeError("MinecraftEventAuthorityUnavailable")
        event = self._events.build_minecraft_consciousness_decision_event(
            dict(decision), dict(context_reference),
        )
        return await self._life.append_external_event(event)

    async def record_minecraft_body_event(
        self, body_event: Mapping[str, Any], context_reference: Mapping[str, Any],
    ) -> Any:
        if self._life is None:
            raise RuntimeError("MinecraftEventAuthorityUnavailable")
        event = self._events.build_minecraft_body_event(
            dict(body_event), dict(context_reference),
        )
        return await self._life.append_external_event(event)


def get_minecraft_service() -> MinecraftService | None:
    return MinecraftService.get_instance()
