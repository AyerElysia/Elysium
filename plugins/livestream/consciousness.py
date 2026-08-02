"""Lifecycle and shared-perception binding for livestream consciousness."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import LivestreamConfig


def _now_iso() -> str:
    """Return one timezone-aware lifecycle timestamp."""

    return datetime.now(UTC).astimezone().isoformat()


class LivestreamConsciousnessManager:
    """Bind a running livestream to one real consciousness instance."""

    def __init__(
        self,
        config: LivestreamConfig,
        *,
        service: Any | None = None,
    ) -> None:
        """Create an inactive manager without inventing a separate identity."""

        self._config = config
        self._service = service
        self._instance: Any | None = None

    @property
    def stream_id(self) -> str:
        """Return the unified stream identity for this configured room."""

        return f"livestream_{self._config.platform.room_id}"

    @property
    def instance_id(self) -> str:
        """Return the stable consciousness identity for this room runtime."""

        return self.stream_id

    @property
    def is_active(self) -> bool:
        """Return whether the bound registry instance is active."""

        return bool(self._instance and self._instance.is_active)

    def _life_service(self) -> Any:
        """Resolve the supported LifeEngine service integration point."""

        if self._service is not None:
            return self._service
        from plugins.life_engine.service.core import LifeEngineService

        service = LifeEngineService.get_instance()
        if service is None:
            raise RuntimeError(
                "LifeEngine is not running; livestream consciousness cannot start"
            )
        return service

    async def activate(self) -> Any:
        """Register or resume this room and append its opening observation."""

        from plugins.life_engine.service.consciousness import ConsciousnessInstance
        from plugins.life_engine.service.world_state import PerceptionFilter

        service = self._life_service()
        registry = service.consciousness_registry
        existing = registry.get(self.instance_id)
        now = _now_iso()
        if existing is not None and existing.kind != "livestream":
            raise RuntimeError(
                f"consciousness instance ID conflict: {self.instance_id}"
            )
        if existing is None or existing.status == "terminated":
            instance = ConsciousnessInstance(
                instance_id=self.instance_id,
                kind="livestream",
                display_name="直播意识",
                stream_ids=[self.stream_id],
                status="active",
                created_at=now,
                last_active_at=now,
                perception_filter=PerceptionFilter.full(),
                metadata={
                    "platform": self._config.platform.platform_type,
                    "room_id": self._config.platform.room_id,
                },
                session_id=f"livestream:{self._config.platform.room_id}",
                lease_duration_seconds=300,
            )
            registry.register(instance)
        else:
            instance = existing
            if instance.is_suspended:
                registry.resume(
                    self.instance_id,
                    timestamp=now,
                    reason="livestream_started",
                )
            else:
                registry.touch(
                    self.instance_id,
                    timestamp=now,
                    reason="livestream_started",
                )
        await asyncio.to_thread(service.save_consciousness_registry)
        self._instance = instance
        await self.report_state("直播已开始")
        return instance

    async def suspend(self, *, reason: str = "livestream_stopped") -> None:
        """Append the closing observation and release stream ownership."""

        if not self.is_active:
            return
        service = self._life_service()
        await self.report_state(f"直播已结束：{reason}")
        service.consciousness_registry.suspend(
            self.instance_id,
            timestamp=_now_iso(),
            reason=reason,
        )
        await asyncio.to_thread(service.save_consciousness_registry)
        self._instance = None

    async def report_state(self, state_text: str) -> dict[str, Any]:
        """Append one livestream observation with trusted instance attribution."""

        text = str(state_text or "").strip()
        if not text:
            raise ValueError("livestream state observation must not be empty")
        service = self._life_service()
        now = _now_iso()
        service.consciousness_registry.touch(
            self.instance_id,
            timestamp=now,
            reason="livestream_state_reported",
        )
        await asyncio.to_thread(service.save_consciousness_registry)
        return await service.report_world_observation(
            text,
            source_instance_id=self.instance_id,
            subject=self.stream_id,
            predicate="session_state",
            domain="livestream",
            stream_id=self.stream_id,
            observed_at=now,
        )

    def prepare_perception(self) -> Any:
        """Prepare this room's retryable transient world delivery."""

        return self._life_service().prepare_perception(self.instance_id)

    def commit_perception(self, prepared: Any) -> None:
        """Acknowledge world delivery after a successful model response."""

        self._life_service().commit_perception(prepared)
