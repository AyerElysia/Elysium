"""Rule-based attention routing for the unified life event stream."""

from __future__ import annotations

from dataclasses import dataclass, field

from .event_builder import EventType, LifeEngineEvent, _now_iso, _shorten_text


@dataclass(slots=True)
class AttentionWindow:
    selected_events: list[LifeEngineEvent]
    summary_events: list[LifeEngineEvent]
    high_water: int
    dropped_count: int = 0
    source_stats: dict[str, int] = field(default_factory=dict)
    context_char_count: int = 0

    @property
    def events(self) -> list[LifeEngineEvent]:
        merged = [*self.summary_events, *self.selected_events]
        return sorted(merged, key=lambda event: int(event.sequence or 0))


class AttentionRouter:
    """Selects the small conscious window from a batch of pending events."""

    def __init__(
        self,
        *,
        max_events: int = 40,
        max_chars: int = 6000,
        max_summary_events: int = 8,
    ) -> None:
        self.max_events = max(1, int(max_events))
        self.max_chars = max(500, int(max_chars))
        self.max_summary_events = max(1, int(max_summary_events))

    def select(
        self,
        events: list[LifeEngineEvent],
        *,
        cursor: int = 0,
        current_stream_id: str = "",
        max_events: int | None = None,
        max_chars: int | None = None,
    ) -> AttentionWindow:
        limit_events = max_events if max_events is not None else self.max_events
        limit_chars = max_chars if max_chars is not None else self.max_chars

        candidates = [
            event for event in sorted(events, key=lambda item: int(item.sequence or 0))
            if int(event.sequence or 0) > int(cursor or 0)
        ]
        high_water = max((int(event.sequence or 0) for event in candidates), default=int(cursor or 0))
        if not candidates:
            return AttentionWindow([], [], high_water)

        source_stats: dict[str, int] = {}
        for event in candidates:
            source = str(event.source or "unknown")
            source_stats[source] = source_stats.get(source, 0) + 1

        def event_size(event: LifeEngineEvent) -> int:
            return (
                len(str(event.content or ""))
                + len(str(event.source_detail or ""))
                + len(str(event.sender or ""))
                + 32
            )

        # Sort candidates by priority (highest score first, then earliest sequence first)
        scored_candidates = sorted(
            candidates,
            key=lambda e: (self._score_event(e, current_stream_id=current_stream_id), -int(e.sequence or 0)),
            reverse=True,
        )

        selected: list[LifeEngineEvent] = []
        omitted: list[LifeEngineEvent] = []
        current_chars = 0

        for event in scored_candidates:
            size = event_size(event)
            if len(selected) < limit_events and (current_chars + size) <= limit_chars:
                selected.append(event)
                current_chars += size
            else:
                omitted.append(event)

        # Re-sort selected events by sequence to preserve chronological order
        selected.sort(key=lambda event: int(event.sequence or 0))

        summary_events = self._summarize_omitted(omitted) if omitted else []

        return AttentionWindow(
            selected_events=selected,
            summary_events=summary_events,
            high_water=high_water,
            dropped_count=len(omitted),
            source_stats=source_stats,
            context_char_count=current_chars + self._events_text_size(summary_events),
        )


    @staticmethod
    def _events_text_size(events: list[LifeEngineEvent]) -> int:
        return sum(
            len(str(event.content or ""))
            + len(str(event.source_detail or ""))
            + len(str(event.sender or ""))
            + 32
            for event in events
        )

    @staticmethod
    def _score_event(event: LifeEngineEvent, *, current_stream_id: str = "") -> int:
        content_type = str(event.content_type or "").strip().lower()
        stream_id = str(event.stream_id or "").strip()
        if event.event_type == EventType.TOOL_RESULT and event.tool_success is False:
            return 100
        if content_type in {"direct_message", "dfc_message"}:
            return 95
        if content_type == "autonomy_intent_due":
            return 93
        if content_type == "proactive_opportunity":
            return 90
        if content_type in {"autonomy_intent_scheduled", "autonomy_intent_silence"}:
            return 84
        if event.event_type == EventType.AGENT_RESULT:
            return 88 if event.tool_success is not False else 96
        if content_type == "chatter_inner_monologue":
            return 78
        if current_stream_id and stream_id == current_stream_id and content_type != "text":
            return 76
        if event.event_type == EventType.MESSAGE:
            return 62
        if event.event_type == EventType.TOOL_RESULT:
            return 40
        if event.event_type == EventType.TOOL_CALL:
            return 30
        if event.event_type == EventType.HEARTBEAT:
            return 25
        return 10

    def _summarize_omitted(self, events: list[LifeEngineEvent]) -> list[LifeEngineEvent]:
        if not events:
            return []

        groups: dict[tuple[str, str], list[LifeEngineEvent]] = {}
        for event in events:
            key = (
                str(event.source or "unknown"),
                str(event.content_type or event.event_type.value or "unknown"),
            )
            groups.setdefault(key, []).append(event)

        ranked = sorted(
            groups.items(),
            key=lambda item: (len(item[1]), max(int(event.sequence or 0) for event in item[1])),
            reverse=True,
        )[: self.max_summary_events]

        lines: list[str] = []
        max_sequence = max(int(event.sequence or 0) for event in events)
        for (source, content_type), grouped in ranked:
            latest = grouped[-1]
            sample = _shorten_text(str(latest.content or ""), max_length=80)
            lines.append(f"- {source}/{content_type}: {len(grouped)} 条，最新：{sample}")

        content = "潜意识已压缩低显著事件：\n" + "\n".join(lines)
        return [
            LifeEngineEvent(
                event_id=f"attention_summary_{max_sequence}",
                event_type=EventType.HEARTBEAT,
                timestamp=_now_iso(),
                sequence=max_sequence,
                source="system",
                source_detail="注意力路由 | 潜意识摘要",
                content=content,
                content_type="attention_summary",
                heartbeat_index=-1,
            )
        ]
