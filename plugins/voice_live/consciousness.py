"""Lifecycle binding for the independent realtime voice consciousness."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .life_binding import (
    ConsciousnessInstance,
    PerceptionFilter,
    get_running_life_service,
    get_tool_manifest,
)
from .runtime_store import VoiceEpisodeStore

_PRESENCE_LEASE_SECONDS = 180


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
                session_id=self.episode_id,
                lease_duration_seconds=_PRESENCE_LEASE_SECONDS,
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
                session_id=self.episode_id,
                lease_duration_seconds=_PRESENCE_LEASE_SECONDS,
            )
            await service.register_consciousness_instance(instance)
        else:
            instance = existing
            if instance.is_suspended:
                instance.stream_ids = [self.stream_id]
                instance.perception_filter = build_voice_live_perception_filter()
                instance.metadata.update(metadata)
                instance.session_id = self.episode_id
                instance.lease_duration_seconds = _PRESENCE_LEASE_SECONDS
                await service.resume_consciousness_instance(
                    self.instance_id,
                    timestamp=now,
                    reason="voice_session_activated",
                )
            else:
                await service.touch_consciousness_instance(
                    self.instance_id,
                    timestamp=now,
                    reason="voice_session_activated",
                )

        await service.report_world_observation(
            "实时通话已连接",
            source_instance_id=self.instance_id,
            subject=self.stream_id,
            predicate="session_state",
            domain="voice_live",
            stream_id=self.stream_id,
            observed_at=now,
            value={
                "summary": "实时通话已连接",
                "provider": provider_name,
                "episode_id": self.episode_id,
            },
        )
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
            await service.touch_consciousness_instance(
                self.instance_id,
                timestamp=now,
                reason="voice_state_reported",
            )
            await service.report_world_observation(
                summary,
                source_instance_id=self.instance_id,
                subject=self.stream_id,
                predicate="session_state",
                domain="voice_live",
                stream_id=self.stream_id,
                observed_at=now,
            )
        await self._store.append_async("consciousness.state", {"summary": summary})

    async def suspend(self, *, reason: str = "normal") -> None:
        service = self._life_service()
        now = _now()
        if service is not None:
            await service.report_world_observation(
                f"通话已结束：{reason}",
                source_instance_id=self.instance_id,
                subject=self.stream_id,
                predicate="session_state",
                domain="voice_live",
                stream_id=self.stream_id,
                observed_at=now,
            )
            await service.suspend_consciousness_instance(
                self.instance_id,
                timestamp=now,
                reason=reason,
            )
        await self._store.append_async("consciousness.suspended", {"reason": reason})
        await self._store.checkpoint_async("suspended", reason=reason)
        self._instance = None

    async def prepare_perception(
        self,
        *,
        projection_kind: str = "voice_live",
        max_bytes: int = 16 * 1024,
    ) -> Any | None:
        """Prepare this voice instance's transient cross-scene perception."""

        service = self._life_service()
        if service is None:
            return None
        return await service.prepare_perception(
            self.instance_id,
            projection_kind=projection_kind,
            max_bytes=max_bytes,
        )

    async def commit_perception(self, prepared: Any, receipt: Any) -> None:
        """Acknowledge perception only after the provider accepted it."""

        service = self._life_service()
        if service is None:
            return
        await service.commit_perception(prepared, receipt)
