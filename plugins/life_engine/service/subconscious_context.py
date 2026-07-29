"""Deterministic subconscious context preparation for life events.

This module is intentionally synchronous and domain-only. It groups causal event
chains, maintains one structured summary, and prepares a character-bounded delta
without invoking an LLM or token counter.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .event_builder import EventType, LifeEngineEvent

logger = logging.getLogger(__name__)


SUMMARY_SCHEMA_VERSION = 1
_HIGH_VALUE_MESSAGE_TYPES = {
    "direct_message",
    "dfc_message",
    "inner_dialogue",
    "proactive_opportunity",
    "autonomy_intent_due",
    "autonomy_intent_scheduled",
    "autonomy_intent_silence",
}


@dataclass(slots=True)
class SummaryEntry:
    """One durable fact retained by the deterministic summary."""

    kind: str
    text: str
    event_ids: list[str] = field(default_factory=list)
    sequences: list[int] = field(default_factory=list)
    source: str = ""
    tool_name: str | None = None

    @property
    def content(self) -> str:
        return self.text

    @property
    def event_id(self) -> str | None:
        return self.event_ids[-1] if self.event_ids else None

    @property
    def sequence(self) -> int:
        return self.sequences[-1] if self.sequences else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "event_ids": list(self.event_ids),
            "sequences": list(self.sequences),
            "source": self.source,
            "tool_name": self.tool_name,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "SummaryEntry":
        raw = json.loads(value)
        if not isinstance(raw, dict):
            raise ValueError("summary entry JSON must be an object")
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SummaryEntry":
        event_ids = data.get("event_ids")
        if not isinstance(event_ids, list):
            legacy_event_id = data.get("event_id")
            event_ids = [legacy_event_id] if legacy_event_id else []
        sequences = data.get("sequences")
        if not isinstance(sequences, list):
            legacy_sequence = data.get("sequence")
            sequences = [legacy_sequence] if legacy_sequence is not None else []
        return cls(
            kind=str(data.get("kind") or data.get("event_type") or "fact"),
            text=str(data.get("text") or data.get("content") or ""),
            event_ids=[str(value) for value in event_ids if value],
            sequences=sorted(
                {
                    int(value)
                    for value in sequences
                    if value is not None and str(value).strip()
                }
            ),
            source=str(data.get("source") or ""),
            tool_name=(
                str(data["tool_name"])
                if data.get("tool_name") is not None
                else None
            ),
        )


@dataclass(slots=True)
class SubconsciousSummary:
    """Canonical, JSON-serializable summary of folded life events."""

    schema_version: int = SUMMARY_SCHEMA_VERSION
    covered_from_sequence: int = 0
    covered_through_sequence: int = 0
    entries: list[SummaryEntry] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "covered_from_sequence": int(self.covered_from_sequence),
            "covered_through_sequence": int(self.covered_through_sequence),
            "entries": [entry.to_dict() for entry in self.entries],
            "stats": {str(key): int(value) for key, value in self.stats.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubconsciousSummary":
        raw_entries = data.get("entries")
        entries = [
            SummaryEntry.from_dict(item)
            for item in raw_entries if isinstance(item, dict)
        ] if isinstance(raw_entries, list) else []
        raw_stats = data.get("stats")
        stats: dict[str, int] = {}
        if isinstance(raw_stats, dict):
            for key, value in raw_stats.items():
                try:
                    stats[str(key)] = int(value or 0)
                except (TypeError, ValueError):
                    continue
        return cls(
            schema_version=int(data.get("schema_version") or SUMMARY_SCHEMA_VERSION),
            covered_from_sequence=int(data.get("covered_from_sequence") or 0),
            covered_through_sequence=int(data.get("covered_through_sequence") or 0),
            entries=entries,
            stats=stats,
        )

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "SubconsciousSummary":
        raw = json.loads(value)
        if not isinstance(raw, dict):
            raise ValueError("subconscious summary JSON must be an object")
        return cls.from_dict(raw)


@dataclass(slots=True)
class EventGroup:
    """An atomic causal group that must not be split during normal selection."""

    group_id: str
    events: list[LifeEngineEvent]
    closed: bool = True
    protected: bool = False
    heartbeat_run_id: str | None = None
    call_ids: list[str] = field(default_factory=list)

    @property
    def from_sequence(self) -> int:
        return min((int(event.sequence or 0) for event in self.events), default=0)

    @property
    def through_sequence(self) -> int:
        return max((int(event.sequence or 0) for event in self.events), default=0)

    @property
    def event_ids(self) -> list[str]:
        return [str(event.event_id) for event in self.events]

    @property
    def is_closed(self) -> bool:
        return self.closed

    @property
    def is_protected(self) -> bool:
        return self.protected


@dataclass(slots=True)
class PreparedHeartbeatContext:
    """Character-bounded context plus state to retain after this snapshot."""

    content: str
    snapshot_high_water: int
    selected_event_ids: list[str]
    acknowledged_event_ids: list[str]
    summary_event_ids: list[str]
    before_chars: int
    after_chars: int
    dropped_count: int
    target_reached: bool
    updated_summary: SubconsciousSummary
    recent_history: list[LifeEngineEvent] = field(default_factory=list)
    summary_event: LifeEngineEvent | None = None
    has_inbound_messages: bool = False

    @property
    def summary(self) -> SubconsciousSummary:
        return self.updated_summary

    @property
    def updated_recent_history(self) -> list[LifeEngineEvent]:
        return self.recent_history

    @property
    def history_events(self) -> list[LifeEngineEvent]:
        events = list(self.recent_history)
        if self.summary_event is not None:
            events.append(self.summary_event)
        return sorted(
            events,
            key=lambda event: (
                int(event.sequence or 0),
                0 if event.event_type == EventType.SUMMARY else 1,
            ),
        )


class SubconsciousContextManager:
    """Groups, summarizes, and renders heartbeat deltas deterministically."""

    def __init__(
        self,
        *,
        max_chars: int = 16000,
        recent_group_count: int = 5,
        summary_max_chars: int = 4000,
        entry_max_chars: int = 480,
        summary_max_entries: int = 60,
    ) -> None:
        self.max_chars = max(0, int(max_chars))
        self.recent_group_count = max(0, int(recent_group_count))
        self.summary_max_chars = max(0, int(summary_max_chars))
        self.entry_max_chars = max(40, int(entry_max_chars))
        self.summary_max_entries = max(10, int(summary_max_entries))

    def prepare(
        self,
        events: list[LifeEngineEvent],
        cursor: int,
        existing_summary: SubconsciousSummary | dict[str, Any] | str | None = None,
        *,
        max_chars: int | None = None,
        recent_group_count: int | None = None,
    ) -> PreparedHeartbeatContext:
        """Prepare only events newer than ``cursor`` at the snapshot boundary."""

        budget = self.max_chars if max_chars is None else max(0, int(max_chars))
        keep_recent = (
            self.recent_group_count
            if recent_group_count is None
            else max(0, int(recent_group_count))
        )
        sorted_events = self._sort_raw_events(events)
        summary, recent_history, _ = self._compact_state(
            events,
            cursor=int(cursor or 0),
            existing_summary=existing_summary,
            keep_recent_groups=keep_recent,
        )
        effective_cursor = max(
            int(cursor or 0),
            int(summary.covered_through_sequence or 0),
        )
        snapshot_high_water = max(
            [effective_cursor, *(int(event.sequence or 0) for event in events)],
        )
        delta = [
            event
            for event in sorted_events
            if int(event.sequence or 0) > effective_cursor
            and int(event.sequence or 0) <= snapshot_high_water
            and not event.heartbeat_context_consumed
        ]
        summary_event = self._build_summary_event(summary)

        if not delta:
            return PreparedHeartbeatContext(
                content="",
                snapshot_high_water=snapshot_high_water,
                selected_event_ids=[],
                acknowledged_event_ids=[],
                summary_event_ids=[],
                before_chars=0,
                after_chars=0,
                dropped_count=0,
                target_reached=True,
                updated_summary=summary,
                recent_history=recent_history,
                summary_event=summary_event,
            )

        groups = self.group_events(sorted_events)
        delta_ids = {event.event_id for event in delta}
        delta_groups = [
            group
            for group in groups
            if any(event.event_id in delta_ids for event in group.events)
        ]
        delta_group_ids = {group.group_id for group in delta_groups}
        old_groups = [
            group
            for group in groups
            if group.group_id not in delta_group_ids
            and group.through_sequence <= int(cursor or 0)
        ]
        protected_groups = [group for group in old_groups if group.protected]
        recent_closed = [group for group in old_groups if group.closed]
        if keep_recent:
            recent_closed = recent_closed[-keep_recent:]
        else:
            recent_closed = []
        recent_candidates = sorted(
            {
                group.group_id: group
                for group in [*protected_groups, *recent_closed]
            }.values(),
            key=lambda group: (group.from_sequence, group.through_sequence),
        )

        full_summary = self._render_summary(summary)
        full_recent = [self._render_group(group) for group in recent_candidates]
        full_delta = [self._render_group(group) for group in delta_groups]
        before_chars = len(self._assemble(full_summary, full_recent, full_delta))

        delta_blocks, selected_ids, delta_complete = self._fit_delta_groups(
            delta_groups,
            budget,
        )
        acknowledged_ids = {
            event_id
            for group in delta_groups
            if delta_complete
            and group.closed
            and all(event_id in selected_ids for event_id in group.event_ids)
            for event_id in group.event_ids
        }
        used_by_delta = len(self._assemble("", [], delta_blocks))
        remaining = max(0, budget - used_by_delta)

        summary_text = ""
        recent_blocks: list[str] = []
        if remaining > 0:
            summary_cap = min(self.summary_max_chars, remaining)
            if recent_candidates:
                summary_cap = min(summary_cap, remaining // 2)
            summary_text = self._render_summary(summary, max_chars=summary_cap)

            for group in reversed(recent_candidates):
                block = self._render_group(group)
                trial = self._assemble(
                    summary_text,
                    [block, *recent_blocks],
                    delta_blocks,
                )
                if len(trial) <= budget:
                    recent_blocks.insert(0, block)
                    selected_ids.update(group.event_ids)

            available_for_summary = budget - len(
                self._assemble("", recent_blocks, delta_blocks)
            )
            if recent_blocks or delta_blocks:
                available_for_summary -= 2
            summary_text = self._render_summary(
                summary,
                max_chars=max(0, min(self.summary_max_chars, available_for_summary)),
            )

        content = self._assemble(summary_text, recent_blocks, delta_blocks)
        if len(content) > budget:
            content = self._fit_text(content, budget)
            delta_complete = False
            # The final fit may cut into a rendered causal group. Do not
            # acknowledge any delta whose complete representation is uncertain.
            acknowledged_ids.clear()

        candidate_ids = {
            event.event_id
            for group in [*recent_candidates, *delta_groups]
            for event in group.events
        }
        dropped_count = len(candidate_ids - selected_ids)
        rendered_summary_ids = (
            [summary_event.event_id]
            if summary_event is not None and summary_text
            else []
        )

        # 结构化诊断日志：追踪潜意识上下文健康度
        logger.debug(
            "潜意识上下文 prepare: "
            f"input_events={len(sorted_events)} "
            f"delta_events={len(delta)} "
            f"selected={len(selected_ids)} "
            f"dropped={dropped_count} "
            f"summary_entries={len(summary.entries)} "
            f"summary_chars={len(self._render_summary(summary))} "
            f"content_chars={len(content)}/{budget} "
            f"target_reached={delta_complete} "
            f"high_water={snapshot_high_water}"
        )

        return PreparedHeartbeatContext(
            content=content,
            snapshot_high_water=snapshot_high_water,
            selected_event_ids=sorted(
                selected_ids,
                key=lambda event_id: self._sequence_for_id(sorted_events, event_id),
            ),
            acknowledged_event_ids=sorted(
                acknowledged_ids,
                key=lambda event_id: self._sequence_for_id(sorted_events, event_id),
            ),
            summary_event_ids=rendered_summary_ids,
            before_chars=before_chars,
            after_chars=len(content),
            dropped_count=dropped_count,
            target_reached=delta_complete,
            updated_summary=summary,
            recent_history=recent_history,
            summary_event=summary_event,
        )

    def group_events(self, events: Iterable[LifeEngineEvent]) -> list[EventGroup]:
        """Build atomic groups from explicit causality and legacy tool adjacency."""

        sorted_events = self._sort_raw_events(list(events))
        if not sorted_events:
            return []

        parents = list(range(len(sorted_events)))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        by_event_id = {
            str(event.event_id): index
            for index, event in enumerate(sorted_events)
            if event.event_id
        }
        by_call_id: dict[str, list[int]] = {}
        by_run_id: dict[str, list[int]] = {}
        for index, event in enumerate(sorted_events):
            call_id = str(event.call_id or "").strip()
            if call_id:
                by_call_id.setdefault(call_id, []).append(index)
            run_id = str(event.heartbeat_run_id or "").strip()
            if run_id:
                by_run_id.setdefault(run_id, []).append(index)

        for indexes in [*by_call_id.values(), *by_run_id.values()]:
            for index in indexes[1:]:
                union(indexes[0], index)

        for index, event in enumerate(sorted_events):
            for reference in (event.parent_event_id, event.causation_id):
                reference_id = str(reference or "").strip()
                if reference_id in by_event_id:
                    union(index, by_event_id[reference_id])
                for referenced_index in by_call_id.get(reference_id, []):
                    union(index, referenced_index)

        legacy_pairs: dict[int, int] = {}
        for index in range(1, len(sorted_events)):
            call = sorted_events[index - 1]
            result = sorted_events[index]
            if (
                call.event_type == EventType.TOOL_CALL
                and result.event_type == EventType.TOOL_RESULT
                and not call.call_id
                and not result.call_id
                and str(call.tool_name or "") == str(result.tool_name or "")
            ):
                union(index - 1, index)
                legacy_pairs[index - 1] = index

        grouped_indexes: dict[int, list[int]] = {}
        for index in range(len(sorted_events)):
            grouped_indexes.setdefault(find(index), []).append(index)

        groups: list[EventGroup] = []
        for indexes in grouped_indexes.values():
            group_events = [sorted_events[index] for index in indexes]
            matched_calls = self._matched_tool_call_indexes(
                sorted_events,
                indexes,
                legacy_pairs,
            )
            call_indexes = {
                index
                for index in indexes
                if sorted_events[index].event_type == EventType.TOOL_CALL
            }
            closed = call_indexes.issubset(matched_calls)
            run_ids = [
                str(event.heartbeat_run_id)
                for event in group_events
                if event.heartbeat_run_id
            ]
            call_ids = list(
                dict.fromkeys(
                    str(event.call_id)
                    for event in group_events
                    if event.call_id
                )
            )
            first = group_events[0]
            if run_ids:
                group_id = f"heartbeat:{run_ids[0]}"
            elif call_ids:
                group_id = f"call:{call_ids[0]}"
            elif len(group_events) > 1 and call_indexes:
                group_id = f"legacy-tool:{first.event_id}"
            else:
                group_id = f"event:{first.event_id}"
            groups.append(
                EventGroup(
                    group_id=group_id,
                    events=group_events,
                    closed=closed,
                    protected=bool(call_indexes and not closed),
                    heartbeat_run_id=run_ids[0] if run_ids else None,
                    call_ids=call_ids,
                )
            )

        return sorted(
            groups,
            key=lambda group: (group.from_sequence, group.through_sequence),
        )

    def compact_history(
        self,
        events: list[LifeEngineEvent],
        cursor: int,
        existing_summary: SubconsciousSummary | dict[str, Any] | str | None = None,
        *,
        keep_recent_groups: int | None = None,
    ) -> list[LifeEngineEvent]:
        """Fold a confirmed closed prefix and retain one typed summary event."""

        keep_count = (
            self.recent_group_count
            if keep_recent_groups is None
            else max(0, int(keep_recent_groups))
        )
        summary, retained, _ = self._compact_state(
            events,
            cursor=int(cursor or 0),
            existing_summary=existing_summary,
            keep_recent_groups=keep_count,
        )
        result = list(retained)
        summary_event = self._build_summary_event(summary)
        if summary_event is not None:
            result.append(summary_event)
        return sorted(
            result,
            key=lambda event: (
                int(event.sequence or 0),
                0 if event.event_type == EventType.SUMMARY else 1,
            ),
        )

    def _compact_state(
        self,
        events: list[LifeEngineEvent],
        *,
        cursor: int,
        existing_summary: SubconsciousSummary | dict[str, Any] | str | None,
        keep_recent_groups: int,
    ) -> tuple[SubconsciousSummary, list[LifeEngineEvent], list[str]]:
        summary, summary_event_ids = self._summary_from_inputs(
            existing_summary,
            events,
        )
        sorted_events = [
            event
            for event in self._sort_raw_events(events)
            if int(event.sequence or 0) > summary.covered_through_sequence
        ]
        groups = self.group_events(sorted_events)

        confirmed_prefix: list[EventGroup] = []
        for group in groups:
            if not group.closed:
                break
            if group.through_sequence > cursor and not all(
                event.heartbeat_context_consumed for event in group.events
            ):
                break
            confirmed_prefix.append(group)

        absorb_count = max(0, len(confirmed_prefix) - keep_recent_groups)
        absorbed_group_ids = {
            group.group_id for group in confirmed_prefix[:absorb_count]
        }
        absorbed = [
            event
            for group in groups
            if group.group_id in absorbed_group_ids
            for event in group.events
        ]
        retained = [
            event
            for group in groups
            if group.group_id not in absorbed_group_ids
            for event in group.events
        ]
        summary, absorbed_ids = self._merge_events_into_summary(summary, absorbed)
        return summary, retained, list(
            dict.fromkeys([*summary_event_ids, *absorbed_ids])
        )

    @staticmethod
    def _sort_raw_events(events: list[LifeEngineEvent]) -> list[LifeEngineEvent]:
        return sorted(
            (event for event in events if event.event_type != EventType.SUMMARY),
            key=lambda event: (int(event.sequence or 0), str(event.event_id or "")),
        )

    @staticmethod
    def _sequence_for_id(events: list[LifeEngineEvent], event_id: str) -> int:
        return next(
            (
                int(event.sequence or 0)
                for event in events
                if event.event_id == event_id
            ),
            0,
        )

    @staticmethod
    def _matched_tool_call_indexes(
        events: list[LifeEngineEvent],
        indexes: list[int],
        legacy_pairs: dict[int, int],
    ) -> set[int]:
        calls = {
            index
            for index in indexes
            if events[index].event_type == EventType.TOOL_CALL
        }
        matched = {index for index in calls if index in legacy_pairs}
        for result_index in indexes:
            result = events[result_index]
            if result.event_type != EventType.TOOL_RESULT:
                continue
            references = {
                str(value)
                for value in (
                    result.call_id,
                    result.parent_event_id,
                    result.causation_id,
                )
                if value
            }
            for call_index in calls:
                call = events[call_index]
                if references.intersection(
                    {str(call.event_id), str(call.call_id or "")}
                ):
                    matched.add(call_index)
        return matched

    def _summary_from_inputs(
        self,
        existing_summary: SubconsciousSummary | dict[str, Any] | str | None,
        events: list[LifeEngineEvent],
    ) -> tuple[SubconsciousSummary, list[str]]:
        summary = self._coerce_summary(existing_summary)
        summary_events: list[tuple[LifeEngineEvent, SubconsciousSummary]] = []
        for event in events:
            if event.event_type != EventType.SUMMARY:
                continue
            try:
                parsed = SubconsciousSummary.from_json(str(event.content or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            summary_events.append((event, parsed))

        summary_ids: list[str] = []
        for event, parsed in sorted(
            summary_events,
            key=lambda item: item[1].covered_through_sequence,
        ):
            summary_ids.append(event.event_id)
            summary = self._merge_summaries(summary, parsed)
        return self._deduplicate_summary(summary), summary_ids

    def _merge_summaries(
        self,
        left: SubconsciousSummary,
        right: SubconsciousSummary,
    ) -> SubconsciousSummary:
        covered_from_values = [
            value
            for value in (
                int(left.covered_from_sequence or 0),
                int(right.covered_from_sequence or 0),
            )
            if value > 0
        ]
        stats = dict(left.stats)
        for key, value in right.stats.items():
            stats[key] = max(stats.get(key, 0), int(value or 0))
        return self._deduplicate_summary(
            SubconsciousSummary(
                schema_version=SUMMARY_SCHEMA_VERSION,
                covered_from_sequence=(
                    min(covered_from_values) if covered_from_values else 0
                ),
                covered_through_sequence=max(
                    int(left.covered_through_sequence or 0),
                    int(right.covered_through_sequence or 0),
                ),
                entries=[*left.entries, *right.entries],
                stats=stats,
            )
        )

    @staticmethod
    def _coerce_summary(
        value: SubconsciousSummary | dict[str, Any] | str | None,
    ) -> SubconsciousSummary:
        if isinstance(value, SubconsciousSummary):
            return SubconsciousSummary.from_dict(value.to_dict())
        if isinstance(value, dict):
            return SubconsciousSummary.from_dict(value)
        if isinstance(value, str) and value.strip():
            try:
                return SubconsciousSummary.from_json(value)
            except (ValueError, json.JSONDecodeError):
                return SubconsciousSummary()
        return SubconsciousSummary()

    def _merge_events_into_summary(
        self,
        summary: SubconsciousSummary,
        events: Iterable[LifeEngineEvent],
    ) -> tuple[SubconsciousSummary, list[str]]:
        updated = SubconsciousSummary.from_dict(summary.to_dict())
        new_events = [
            event
            for event in sorted(
                events,
                key=lambda item: (int(item.sequence or 0), str(item.event_id or "")),
            )
            if event.event_type != EventType.SUMMARY
            and int(event.sequence or 0) > updated.covered_through_sequence
        ]
        if not new_events:
            return self._deduplicate_summary(updated), []

        first_sequence = int(new_events[0].sequence or 0)
        last_sequence = int(new_events[-1].sequence or 0)
        if updated.covered_from_sequence <= 0:
            updated.covered_from_sequence = first_sequence
        else:
            updated.covered_from_sequence = min(
                updated.covered_from_sequence,
                first_sequence,
            )
        updated.covered_through_sequence = max(
            updated.covered_through_sequence,
            last_sequence,
        )

        summarized_ids: list[str] = []
        for event in new_events:
            summarized_ids.append(event.event_id)
            self._increment_stats(updated.stats, event)
            entry = self._summary_entry_for_event(event)
            if entry is not None:
                updated.entries.append(entry)
        return self._deduplicate_summary(updated), summarized_ids

    @staticmethod
    def _increment_stats(stats: dict[str, int], event: LifeEngineEvent) -> None:
        stats["total_events"] = stats.get("total_events", 0) + 1
        if event.event_type == EventType.MESSAGE:
            stats["message_count"] = stats.get("message_count", 0) + 1
        elif event.event_type == EventType.HEARTBEAT:
            stats["heartbeat_count"] = stats.get("heartbeat_count", 0) + 1
        elif event.event_type == EventType.TOOL_CALL:
            stats["tool_call_count"] = stats.get("tool_call_count", 0) + 1
        elif event.event_type == EventType.TOOL_RESULT:
            if event.tool_success is False:
                stats["tool_failure_count"] = stats.get("tool_failure_count", 0) + 1
            else:
                stats["tool_success_count"] = stats.get("tool_success_count", 0) + 1
        elif event.event_type == EventType.AGENT_RESULT:
            stats["agent_result_count"] = stats.get("agent_result_count", 0) + 1

    def _summary_entry_for_event(
        self,
        event: LifeEngineEvent,
    ) -> SummaryEntry | None:
        content_type = str(event.content_type or "").strip().lower()
        if event.event_type == EventType.MESSAGE and content_type in _HIGH_VALUE_MESSAGE_TYPES:
            kind = content_type
        elif event.event_type == EventType.AGENT_RESULT:
            kind = "agent_result"
        else:
            # tool_failure 等操作噪音不进入潜意识——
            # 它们仍被 stats 计数器记录，但不会作为独立条目占据提示词空间。
            return None

        text = self._normalize_display_text(event.content)
        if not text:
            text = str(event.tool_name or event.source_detail or kind)
        # 只过滤没有实际内容的结构壳；短文本也可能承载完整事实，不能按长度机械丢弃。
        if not text.strip(" \t\n{}[]()\u3000"):
            return None
        return SummaryEntry(
            kind=kind,
            text=self._shorten(text, self.entry_max_chars),
            event_ids=[str(event.event_id)],
            sequences=[int(event.sequence or 0)],
            source=str(event.source or ""),
            tool_name=str(event.tool_name) if event.tool_name else None,
        )

    # 允许存在于摘要中的 kind 白名单（不在此集合中的旧条目会在去重时被清洗）
    _VALID_SUMMARY_KINDS: frozenset[str] = frozenset({
        *_HIGH_VALUE_MESSAGE_TYPES,
        "relationship", "emotional_state", "body_state",
        "ongoing_thread", "commitment", "agent_result",
    })

    def _deduplicate_summary(
        self,
        summary: SubconsciousSummary,
    ) -> SubconsciousSummary:
        entries_by_content: dict[str, SummaryEntry] = {}
        for entry in summary.entries:
            # 清洗已废弃的 kind（如 tool_failure）和退化内容
            if entry.kind not in self._VALID_SUMMARY_KINDS:
                continue
            normalized = self._normalize_key(entry.text)
            if not normalized:
                continue
            existing = entries_by_content.get(normalized)
            if existing is None:
                entries_by_content[normalized] = SummaryEntry.from_dict(entry.to_dict())
                continue
            existing.event_ids = list(
                dict.fromkeys([*existing.event_ids, *entry.event_ids])
            )
            existing.sequences = sorted(
                set([*existing.sequences, *entry.sequences])
            )
            if not existing.source:
                existing.source = entry.source
            if not existing.tool_name:
                existing.tool_name = entry.tool_name

        entries = sorted(
            entries_by_content.values(),
            key=lambda entry: (
                entry.sequences[0] if entry.sequences else 0,
                entry.kind,
                entry.text,
            ),
        )
        # 容量裁剪：只保留最新的 N 条，避免摘要无限膨胀
        if len(entries) > self.summary_max_entries:
            entries = entries[-self.summary_max_entries:]
        return SubconsciousSummary(
            schema_version=SUMMARY_SCHEMA_VERSION,
            covered_from_sequence=max(0, int(summary.covered_from_sequence or 0)),
            covered_through_sequence=max(0, int(summary.covered_through_sequence or 0)),
            entries=entries,
            stats={key: max(0, int(value)) for key, value in summary.stats.items()},
        )

    def _build_summary_event(
        self,
        summary: SubconsciousSummary,
    ) -> LifeEngineEvent | None:
        if summary.covered_through_sequence <= 0 and not summary.entries and not summary.stats:
            return None
        sequence = int(summary.covered_through_sequence or 0)
        return LifeEngineEvent(
            event_id=f"subconscious_summary_{sequence}",
            event_type=EventType.SUMMARY,
            timestamp="",
            sequence=sequence,
            source="system",
            source_detail="潜意识上下文 | 规范摘要",
            content=summary.to_json(),
            content_type="subconscious_summary",
        )

    # 摘要条目的语义分组顺序（未匹配的 kind 归入“其他”）
    _SUMMARY_KIND_ORDER: list[tuple[str, str]] = [
        ("relationship", "关系"),
        ("emotional_state", "情绪"),
        ("body_state", "身体"),
        ("ongoing_thread", "未闭合"),
        ("commitment", "承诺"),
        ("direct_message", "直接消息"),
        ("dfc_message", "信息差"),
        ("inner_dialogue", "内心对话"),
        ("proactive_opportunity", "主动机会"),
        ("autonomy_intent_due", "自主意向"),
        ("agent_result", "智能体结果"),
    ]

    def _render_summary(
        self,
        summary: SubconsciousSummary,
        *,
        max_chars: int | None = None,
    ) -> str:
        if summary.covered_through_sequence <= 0 and not summary.entries and not summary.stats:
            return ""
        lines = [
            "【潜意识规范摘要】",
            (
                f"覆盖序列 {summary.covered_from_sequence}-"
                f"{summary.covered_through_sequence}"
            ),
        ]

        # 按 kind 分组渲染，让潜意识 LLM 更容易抓住结构
        grouped: dict[str, list[SummaryEntry]] = {}
        for entry in summary.entries:
            grouped.setdefault(entry.kind, []).append(entry)

        rendered_kinds: set[str] = set()
        for kind_key, kind_label in self._SUMMARY_KIND_ORDER:
            entries = grouped.get(kind_key)
            if not entries:
                continue
            rendered_kinds.add(kind_key)
            lines.append(f"  [{kind_label}]")
            for entry in entries:
                sequences = ",".join(str(v) for v in entry.sequences)
                seq_tag = f" @{sequences}" if sequences else ""
                lines.append(f"  - {entry.text}{seq_tag}")

        # 未匹配的 kind 归入“其他”
        other_kinds = set(grouped.keys()) - rendered_kinds
        if other_kinds:
            lines.append("  [其他]")
            for kind in sorted(other_kinds):
                for entry in grouped[kind]:
                    sequences = ",".join(str(v) for v in entry.sequences)
                    seq_tag = f" @{sequences}" if sequences else ""
                    lines.append(f"  - ({kind}) {entry.text}{seq_tag}")

        if summary.stats:
            stats = ", ".join(
                f"{key}={summary.stats[key]}" for key in sorted(summary.stats)
            )
            lines.append(f"- folded_stats: {stats}")
        text = "\n".join(lines)
        if max_chars is None:
            return text
        return self._fit_text(text, max(0, int(max_chars)))

    def _fit_delta_groups(
        self,
        groups: list[EventGroup],
        budget: int,
    ) -> tuple[list[str], set[str], bool]:
        if not groups or budget <= 0:
            return [], set(), not groups

        heading_size = len("【本次新增 delta】\n")
        block_budget = budget - heading_size
        if block_budget <= 0:
            return [], set(), False

        full_blocks = [(group, self._render_group(group)) for group in groups]
        if len("\n\n".join(block for _, block in full_blocks)) <= block_budget:
            return (
                [block for _, block in full_blocks],
                {event_id for group in groups for event_id in group.event_ids},
                True,
            )

        selected: list[tuple[EventGroup, str]] = []
        selected_ids: set[str] = set()
        ranked = sorted(
            full_blocks,
            key=lambda item: (
                self._group_priority(item[0]),
                item[0].through_sequence,
            ),
            reverse=True,
        )
        for group, block in ranked:
            if len(block) > block_budget and not selected:
                compact, represented_ids = self._render_group_compact(group, block_budget)
                if compact:
                    selected.append((group, compact))
                    selected_ids.update(represented_ids)
                continue

            trial = "\n\n".join([*(value for _, value in selected), block])
            if len(trial) <= block_budget:
                selected.append((group, block))
                selected_ids.update(group.event_ids)

        selected.sort(key=lambda item: (item[0].from_sequence, item[0].through_sequence))
        return [block for _, block in selected], selected_ids, False

    def _render_group(self, group: EventGroup) -> str:
        label = group.group_id
        state = "open" if not group.closed else "closed"
        lines = [
            f"[因果组 {group.from_sequence}-{group.through_sequence} | {label} | {state}]"
        ]
        lines.extend(self._render_event(event) for event in group.events)
        return "\n".join(lines)

    def _render_group_compact(
        self,
        group: EventGroup,
        max_chars: int,
    ) -> tuple[str, set[str]]:
        if max_chars <= 0:
            return "", set()
        header = f"[因果组 {group.from_sequence}-{group.through_sequence} | 截断]"
        if len(header) >= max_chars:
            return self._fit_text(header, max_chars), set()

        represented: list[LifeEngineEvent] = []
        lines = [header]
        ranked = sorted(
            group.events,
            key=lambda event: (
                self._event_priority(event),
                int(event.sequence or 0),
            ),
            reverse=True,
        )
        for event in ranked:
            remaining = max_chars - len("\n".join(lines)) - 1
            if remaining <= 0:
                break
            line = self._render_event(event)
            fitted = self._fit_text(line, remaining)
            if fitted:
                lines.append(fitted)
                represented.append(event)
            if len(fitted) < len(line):
                break

        represented.sort(key=lambda event: int(event.sequence or 0))
        if len(represented) > 1:
            lines = [header, *(self._render_event(event) for event in represented)]
            text = self._fit_text("\n".join(lines), max_chars)
        else:
            text = self._fit_text("\n".join(lines), max_chars)
        return text, {event.event_id for event in represented}

    def _render_event(self, event: LifeEngineEvent) -> str:
        sequence = int(event.sequence or 0)
        content = self._normalize_display_text(event.content)
        content_type = str(event.content_type or event.event_type.value)
        if event.event_type == EventType.MESSAGE:
            sender = str(event.sender or event.source or "unknown")
            return f"- #{sequence} MESSAGE/{content_type} {sender}: {content}"
        if event.event_type == EventType.HEARTBEAT:
            return f"- #{sequence} HEARTBEAT {content}"
        if event.event_type == EventType.TOOL_CALL:
            args = json.dumps(
                event.tool_args or {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return (
                f"- #{sequence} TOOL_CALL {event.tool_name or 'tool'} "
                f"call_id={event.call_id or 'legacy'} args={args}"
            )
        if event.event_type == EventType.TOOL_RESULT:
            status = "success" if event.tool_success else "failure"
            return (
                f"- #{sequence} TOOL_RESULT {event.tool_name or 'tool'} {status} "
                f"call_id={event.call_id or 'legacy'}: {content}"
            )
        if event.event_type == EventType.AGENT_RESULT:
            status = "success" if event.tool_success is not False else "failure"
            return (
                f"- #{sequence} AGENT_RESULT {event.tool_name or 'agent'} "
                f"{status}: {content}"
            )
        return f"- #{sequence} {event.event_type.value.upper()}: {content}"

    @staticmethod
    def _assemble(
        summary_text: str,
        recent_blocks: list[str],
        delta_blocks: list[str],
    ) -> str:
        sections: list[str] = []
        if summary_text:
            sections.append(summary_text)
        if recent_blocks:
            sections.append("【最近完整因果组】\n" + "\n\n".join(recent_blocks))
        if delta_blocks:
            sections.append("【本次新增 delta】\n" + "\n\n".join(delta_blocks))
        return "\n\n".join(sections)

    def _group_priority(self, group: EventGroup) -> int:
        return max((self._event_priority(event) for event in group.events), default=0)

    @staticmethod
    def _event_priority(event: LifeEngineEvent) -> int:
        content_type = str(event.content_type or "").strip().lower()
        if event.event_type == EventType.TOOL_RESULT and event.tool_success is False:
            return 100
        if event.event_type == EventType.AGENT_RESULT:
            return 98 if event.tool_success is False else 88
        if content_type in {"direct_message", "dfc_message", "inner_dialogue"}:
            return 95
        if content_type.startswith("autonomy_intent_"):
            return 93
        if content_type == "proactive_opportunity":
            return 92
        if event.event_type == EventType.MESSAGE:
            return 70
        if event.event_type == EventType.TOOL_RESULT:
            return 40
        if event.event_type == EventType.TOOL_CALL:
            return 35
        if event.event_type == EventType.HEARTBEAT:
            return 30
        return 10

    @staticmethod
    def _normalize_display_text(value: Any) -> str:
        return " ".join(str(value or "").split())

    @staticmethod
    def _normalize_key(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

    @staticmethod
    def _shorten(value: str, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        if max_chars <= 1:
            return value[:max_chars]
        return value[: max_chars - 1] + "…"

    @staticmethod
    def _fit_text(value: str, max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        if len(value) <= max_chars:
            return value
        if max_chars == 1:
            return "…"
        return value[: max_chars - 1].rstrip() + "…"
