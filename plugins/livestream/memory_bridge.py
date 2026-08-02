"""Replayable projection from livestream facts into LifeEngine experience."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from .domain import PlatformEvent
from .ledger import LedgerRecord, LivestreamLedger
from .life_binding import LifeEngineEvent, LifeEventType, get_running_life_service


class LifeEventPublisher(Protocol):
    async def publish(self, record: LedgerRecord) -> None:
        """Idempotently project one supported ledger record into LifeEngine."""


class RunningLifeEventPublisher:
    """Publish stable events to the active LifeEngine raw event bus."""

    def __init__(self, *, service: Any | None = None) -> None:
        self._service = service

    def require_service(self) -> Any:
        service = self._service or get_running_life_service()
        if service is None:
            raise RuntimeError("LifeEngine service is unavailable")
        return service

    async def publish(self, record: LedgerRecord) -> None:
        event = self._to_life_event(record)
        if event is None:
            return
        service = self.require_service()
        await service._get_event_bus().publish_legacy_event(event)

    @staticmethod
    def _to_life_event(record: LedgerRecord) -> Any | None:
        timestamp = datetime.fromtimestamp(
            record.occurred_at,
            tz=UTC,
        ).astimezone().isoformat()
        if record.kind == "platform.event":
            event = PlatformEvent.from_payload(record.payload)
            content = (
                f"[直播间外部事件/{event.kind}] {event.display_text}"
            )
            return LifeEngineEvent(
                event_id=f"livestream:{record.session_id}:{record.record_id}",
                event_type=LifeEventType.MESSAGE,
                timestamp=timestamp,
                sequence=record.sequence,
                source="livestream",
                source_detail=f"{event.platform} room {event.room_id}",
                content=content,
                content_type="livestream_audience_observation",
                sender=event.user_name,
                chat_type="group",
                stream_id=f"livestream:{event.platform}:{event.room_id}",
                causation_id=record.causation_id,
            )
        if record.kind in {"performance.completed", "performance.interrupted"}:
            spoken = str(record.payload.get("spoken_text", "")).strip()
            partial = str(record.payload.get("partial_chunk_text", "")).strip()
            played_ms = max(0, int(record.payload.get("partial_played_ms", 0) or 0))
            if not spoken and not (partial and played_ms > 0):
                return None
            traces: list[str] = []
            if spoken:
                traces.append(f"[直播中实际完整说出] {spoken}")
            if partial and played_ms > 0:
                traces.append(
                    "[随后发言被打断] "
                    f"已确认音频播放 {played_ms}ms；该片段原计划文本：{partial}"
                )
            return LifeEngineEvent(
                event_id=f"livestream:{record.session_id}:{record.record_id}",
                event_type=LifeEventType.MESSAGE,
                timestamp=timestamp,
                sequence=record.sequence,
                source="livestream",
                source_detail="acknowledged stage playback",
                content="\n".join(traces),
                content_type=(
                    "livestream_spoken_interrupted"
                    if partial and played_ms > 0
                    else "livestream_spoken"
                ),
                sender="self",
                chat_type="group",
                stream_id=f"livestream:session:{record.session_id}",
                causation_id=record.causation_id,
            )
        return None


class LivestreamMemoryBridge:
    """Advance only after a whole projection batch is accepted by LifeEngine."""

    def __init__(
        self,
        ledger: LivestreamLedger,
        publisher: LifeEventPublisher,
        *,
        session_id: str,
        consumer_name: str = "livestream.life-memory.v1",
        batch_limit: int = 100,
    ) -> None:
        self.ledger = ledger
        self.publisher = publisher
        self.session_id = session_id
        self.consumer_name = consumer_name
        self.batch_limit = batch_limit

    async def run_once(self) -> int:
        cursor = await self.ledger.get_cursor(self.session_id, self.consumer_name)
        records = await self.ledger.read_since(
            cursor,
            session_id=self.session_id,
            kinds={
                "platform.event",
                "performance.completed",
                "performance.interrupted",
            },
            limit=self.batch_limit,
        )
        if not records:
            return 0
        for record in records:
            await self.publisher.publish(record)
        await self.ledger.commit_cursor(
            self.session_id,
            self.consumer_name,
            records[-1].sequence,
        )
        return len(records)
