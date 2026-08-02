"""Lifecycle and retry-safe world perception for livestream consciousness."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from .domain import WorldPerceptionCheckpoint
from .life_binding import (
    ConsciousnessInstance,
    PerceptionFilter,
    PreparedPerception,
    get_running_life_service,
)


def _now() -> str:
    """Return one timezone-aware lifecycle timestamp."""

    return datetime.now(UTC).astimezone().isoformat()


class LivestreamConsciousnessManager:
    """Bind one manually started room to the shared subject's real presence."""

    def __init__(
        self,
        config: Any,
        session_id: str,
        *,
        service: Any | None = None,
    ) -> None:
        if not session_id.strip():
            raise ValueError("livestream consciousness session_id must not be empty")
        self._config = config
        self.session_id = session_id
        self.stream_id = (
            f"livestream:{config.platform.platform_type}:{config.platform.room_id}"
        )
        self.instance_id = f"livestream_{config.platform.room_id}"
        self._service = service
        self._instance: ConsciousnessInstance | None = None

    @property
    def instance(self) -> ConsciousnessInstance | None:
        return self._instance

    @property
    def is_active(self) -> bool:
        return bool(self._instance and self._instance.is_active)

    def life_service(self) -> Any:
        service = self._service or get_running_life_service()
        if service is None:
            raise RuntimeError(
                "LifeEngine is not running; livestream cannot create a second persona"
            )
        return service

    async def activate(self) -> ConsciousnessInstance:
        """Atomically claim room presence and append its opening observation."""

        service = self.life_service()
        registry = service.consciousness_registry
        now = _now()
        registry.reconcile_expired(timestamp=now)
        existing = registry.get(self.instance_id)
        if existing is not None and existing.kind != "livestream":
            raise RuntimeError(f"consciousness identity conflict: {self.instance_id}")
        if existing is not None and existing.stream_ids != [self.stream_id]:
            raise RuntimeError(
                f"livestream presence owns unexpected streams: {existing.stream_ids}"
            )
        if existing is not None and existing.is_active:
            if existing.process_epoch != registry.process_epoch:
                raise RuntimeError(
                    "livestream room is still leased by another Elysium process"
                )
            if existing.session_id != self.session_id:
                raise RuntimeError(
                    "livestream room is already owned by another active session"
                )
            registry.touch(
                self.instance_id,
                timestamp=now,
                reason="livestream_start_retried",
            )
            instance = existing
        else:
            if (
                existing is not None
                and existing.is_suspended
                and existing.session_id != self.session_id
            ):
                registry.terminate(
                    self.instance_id,
                    timestamp=now,
                    reason="livestream_new_session",
                )
                existing = registry.get(self.instance_id)
            if existing is None or existing.status == "terminated":
                instance = ConsciousnessInstance(
                    instance_id=self.instance_id,
                    kind="livestream",
                    display_name="直播中的爱莉希雅",
                    stream_ids=[self.stream_id],
                    status="active",
                    created_at=now,
                    last_active_at=now,
                    perception_filter=PerceptionFilter.full(),
                    metadata={
                        "platform": self._config.platform.platform_type,
                        "room_id": self._config.platform.room_id,
                        "director": "life_chatter",
                        "ledger_session_id": self.session_id,
                    },
                    session_id=self.session_id,
                    lease_duration_seconds=int(
                        self._config.server.presence_lease_seconds
                    ),
                )
                registry.register(instance)
            else:
                registry.resume(
                    self.instance_id,
                    timestamp=now,
                    reason="livestream_session_resumed",
                )
                instance = existing

        await asyncio.to_thread(service.save_consciousness_registry)
        self._instance = instance
        await self.report_state("直播已由操作员手动开始")
        return instance

    async def renew(self) -> None:
        """Renew the technical presence lease while the manual session is alive."""

        if not self.is_active:
            raise RuntimeError("livestream consciousness is not active")
        service = self.life_service()
        service.consciousness_registry.touch(
            self.instance_id,
            timestamp=_now(),
            reason="livestream_lease_renewal",
        )
        await asyncio.to_thread(service.save_consciousness_registry)

    async def report_state(self, state_text: str) -> dict[str, Any]:
        """Append one attributed observation without mutating a world snapshot."""

        text = str(state_text or "").strip()
        if not text:
            raise ValueError("livestream state observation must not be empty")
        if not self.is_active:
            raise RuntimeError("livestream consciousness is not active")
        service = self.life_service()
        now = _now()
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

    def prepare_perception(self) -> PreparedPerception:
        """Prepare this room's transient world view without moving its cursor."""

        if not self.is_active:
            raise RuntimeError("livestream consciousness is not active")
        return self.life_service().prepare_perception(self.instance_id)

    def commit_perception_checkpoint(
        self,
        checkpoint: WorldPerceptionCheckpoint,
    ) -> tuple[int, int]:
        """Idempotently confirm a durable director decision's world delivery."""

        if checkpoint.instance_id != self.instance_id:
            raise RuntimeError("world perception checkpoint belongs to another instance")
        service = self.life_service()
        current = service.prepare_perception(self.instance_id)
        if current.from_position >= checkpoint.through_position:
            return current.from_position, current.cursor_revision
        if current.from_position != checkpoint.from_position:
            raise RuntimeError(
                "world perception cursor diverged before durable acknowledgement"
            )
        prepared = PreparedPerception(
            instance_id=checkpoint.instance_id,
            from_position=checkpoint.from_position,
            through_position=checkpoint.through_position,
            cursor_revision=checkpoint.cursor_revision,
            content="",
            assertion_ids=(),
            change_positions=(),
        )
        return service.commit_perception(prepared)

    async def suspend(self, *, reason: str = "manual stop") -> None:
        """Append the closing observation and release the room stream claim."""

        if not self.is_active:
            self._instance = None
            return
        service = self.life_service()
        await self.report_state(f"直播已结束：{reason}")
        service.consciousness_registry.suspend(
            self.instance_id,
            timestamp=_now(),
            reason=reason,
        )
        await asyncio.to_thread(service.save_consciousness_registry)
        self._instance = None
