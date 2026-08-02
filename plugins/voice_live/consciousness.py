"""Lifecycle binding for the independent realtime voice consciousness."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .life_binding import (
    ConsciousnessInstance,
    PerceptionFilter,
    SceneState,
    get_running_life_service,
    get_tool_manifest,
)
from .runtime_store import VoiceEpisodeStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_voice_live_perception_filter() -> PerceptionFilter:
    """Voice calls receive an unabridged semantic slice of the shared world."""
    return PerceptionFilter.full()


class VoiceLiveConsciousnessManager:
    """Own a real registry instance, scene binding and durable episode."""

    def __init__(
        self,
        config: Any,
        episode_id: str,
        store: VoiceEpisodeStore,
        *,
        service: Any | None = None,
    ) -> None:
        session = config.session
        self.episode_id = episode_id
        self.instance_id = f"{session.instance_id_prefix}_{episode_id}"
        self.stream_id = f"{session.stream_id_prefix}_{episode_id}"
        self._config = config
        self._store = store
        self._service = service
        self._instance: ConsciousnessInstance | None = None

    @property
    def instance(self) -> ConsciousnessInstance | None:
        return self._instance

    @property
    def is_active(self) -> bool:
        return bool(self._instance and self._instance.is_active)

    def _life_service(self) -> Any | None:
        if self._service is not None:
            return self._service
        return get_running_life_service()

    async def activate(self, provider_name: str) -> ConsciousnessInstance:
        service = self._life_service()
        if service is None:
            if self._config.session.require_life_engine:
                raise RuntimeError("LifeEngine 未运行，不能创建真实 voice_live 意识实例")
            instance = ConsciousnessInstance(
                instance_id=self.instance_id,
                kind="voice_live",
                display_name=self._config.session.display_name,
                stream_ids=[self.stream_id],
                status="active",
                created_at=_now(),
                last_active_at=_now(),
                perception_filter=build_voice_live_perception_filter(),
                metadata={"episode_id": self.episode_id, "provider": provider_name},
            )
            self._instance = instance
            await self._store.append_async("consciousness.activated", instance.to_dict())
            return instance

        registry = service.consciousness_registry
        existing = registry.get(self.instance_id)
        now = _now()
        metadata = {
            "episode_id": self.episode_id,
            "provider": provider_name,
            "cross_scene_awareness": self._config.session.cross_scene_awareness,
            "tool_manifest": get_tool_manifest("voice_live"),
        }

        if existing is not None and existing.kind != "voice_live":
            raise RuntimeError(f"意识实例 ID 冲突: {self.instance_id}")
        if existing is None or existing.status == "terminated":
            instance = ConsciousnessInstance(
                instance_id=self.instance_id,
                kind="voice_live",
                display_name=self._config.session.display_name,
                stream_ids=[self.stream_id],
                status="active",
                created_at=now,
                last_active_at=now,
                perception_filter=build_voice_live_perception_filter(),
                metadata=metadata,
            )
            registry.register(instance)
        else:
            instance = existing
            instance.stream_ids = [self.stream_id]
            instance.perception_filter = build_voice_live_perception_filter()
            instance.metadata.update(metadata)
            if instance.is_suspended:
                registry.resume(self.instance_id, timestamp=now)
            else:
                registry.touch(self.instance_id, timestamp=now)

        service.world_state.upsert_scene(
            SceneState(
                scene_id=self.stream_id,
                kind="voice_live",
                display_name=self._config.session.stream_name,
                status_summary="实时通话已连接",
                last_active_at=now,
                consciousness_instance_id=self.instance_id,
                context_tags=[provider_name, self.episode_id],
            )
        )
        await asyncio.to_thread(service.save_consciousness_registry)
        await asyncio.to_thread(service.save_world_state)
        self._instance = instance
        await self._store.append_async("consciousness.activated", instance.to_dict())
        await self._store.checkpoint_async("consciousness_active", provider=provider_name)
        return instance

    async def report_state(self, summary: str) -> None:
        if not summary:
            return
        service = self._life_service()
        now = _now()
        if service is not None:
            existing = service.world_state.active_scenes.get(self.stream_id)
            service.world_state.upsert_scene(
                SceneState(
                    scene_id=self.stream_id,
                    kind="voice_live",
                    display_name=self._config.session.stream_name,
                    status_summary=summary,
                    last_active_at=now,
                    consciousness_instance_id=self.instance_id,
                    context_tags=list(existing.context_tags) if existing else [self.episode_id],
                )
            )
            service.consciousness_registry.touch(self.instance_id, timestamp=now)
            await asyncio.to_thread(service.save_consciousness_registry)
            await asyncio.to_thread(service.save_world_state)
        await self._store.append_async("consciousness.state", {"summary": summary})

    async def suspend(self, *, reason: str = "normal") -> None:
        service = self._life_service()
        now = _now()
        if service is not None:
            service.consciousness_registry.suspend(self.instance_id, timestamp=now)
            existing = service.world_state.active_scenes.get(self.stream_id)
            service.world_state.upsert_scene(
                SceneState(
                    scene_id=self.stream_id,
                    kind="voice_live",
                    display_name=self._config.session.stream_name,
                    status_summary=f"通话已结束：{reason}",
                    last_active_at=now,
                    consciousness_instance_id=self.instance_id,
                    context_tags=list(existing.context_tags) if existing else [self.episode_id],
                )
            )
            await asyncio.to_thread(service.save_consciousness_registry)
            await asyncio.to_thread(service.save_world_state)
        await self._store.append_async("consciousness.suspended", {"reason": reason})
        await self._store.checkpoint_async("suspended", reason=reason)
        self._instance = None

    def render_world_state(self) -> str:
        service = self._life_service()
        if service is None:
            return ""
        # Use the complete JSON representation.  Do not impose a local character
        # budget or a hand-picked category filter on cognition.
        return service.world_state.to_json()
