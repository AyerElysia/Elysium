"""Per-instance delivery of shared presence and subjective world projection.

The gateway prepares transient context without advancing a cursor.  Callers
must commit the returned delivery only after the consciousness runtime has
successfully accepted it.  This preserves retryability across model failures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .consciousness import ConsciousnessRegistry
from .event_bus import RawEventStore
from .world_projection import (
    WorldAssertion,
    WorldProjectionChange,
    WorldProjectionStore,
)


@dataclass(frozen=True, slots=True)
class PreparedPerception:
    """One stable world snapshot and cursor window prepared for an instance."""

    instance_id: str
    from_position: int
    through_position: int
    cursor_revision: int
    content: str
    assertion_ids: tuple[str, ...]
    change_positions: tuple[int, ...]


class PerceptionGateway:
    """Coordinate projector catch-up and reliable transient perception."""

    def __init__(
        self,
        registry: ConsciousnessRegistry,
        ledger: RawEventStore,
        projection: WorldProjectionStore,
    ) -> None:
        """Bind current operational presence to one projection and ledger."""

        self._registry = registry
        self._ledger = ledger
        self._projection = projection

    @property
    def projection(self) -> WorldProjectionStore:
        """Expose the derived store for diagnostics and explicit queries."""

        return self._projection

    @staticmethod
    def _json(value: Any) -> str:
        """Render exact structured values without truncating cognitive content."""

        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _render_presence(self, instance_id: str) -> list[str]:
        """Render the minimum existence awareness for every active window."""

        active = sorted(
            self._registry.get_active(),
            key=lambda item: item.instance_id,
        )
        lines = [
            "### 同一主体当前存在的意识窗口",
            "这些窗口属于同一个你；它们是不同场景中的局部运行视角，不是其他人格。",
        ]
        for item in active:
            relation = "当前窗口" if item.instance_id == instance_id else "同时存在"
            streams = self._json(item.stream_ids)
            lines.append(
                f"- {relation}: instance_id={item.instance_id}; "
                f"kind={item.kind}; name={item.display_name or item.instance_id}; "
                f"streams={streams}; last_active_at={item.last_active_at}; "
                f"presence_revision={item.revision}"
            )
        if not active:
            lines.append("- 当前 Presence Registry 没有 active 窗口。")
        return lines

    def _render_assertions(self, assertions: list[WorldAssertion]) -> list[str]:
        """Render assertions with provenance while preserving contradictions."""

        if not assertions:
            return []
        lines = [
            "### 潜意识世界投影",
            "以下是带来源的当前认知记录；相互矛盾的记录会并存，不代表系统已替你裁决真伪。",
        ]
        for item in assertions:
            provenance = (
                f"source_instance_id={item.source_instance_id or '-'}; "
                f"source_event_id={item.source_event_id}; "
                f"occurrence_id={item.occurrence_id}; "
                f"observed_at={item.observed_at}; valid_from={item.valid_from or '-'}; "
                f"valid_to={item.valid_to or '-'}; recorded_at={item.recorded_at or '-'}"
            )
            state = (
                f"status={item.status or '-'}; retracted_at={item.retracted_at or '-'}; "
                f"supersedes={item.supersedes_assertion_id or '-'}"
            )
            lines.append(
                f"- assertion_id={item.assertion_id}; domain={item.domain or '-'}; "
                f"subject={item.subject}; predicate={item.predicate}; "
                f"value={self._json(item.value)}; {state}; {provenance}"
            )
        return lines

    def _render_changes(self, changes: list[WorldProjectionChange]) -> list[str]:
        """Render only changes after this instance's committed cursor."""

        if not changes:
            return []
        lines = ["### 自上次成功感知以来的变化"]
        for item in changes:
            lines.append(
                f"- ingest_position={item.ingest_position}; "
                f"change_kind={item.change_kind}; event_type={item.event_type}; "
                f"source_instance_id={item.source_instance_id or '-'}; "
                f"stream_id={item.stream_id or '-'}; occurred_at={item.occurred_at}; "
                f"payload={self._json(item.payload)}"
            )
        return lines

    def prepare(self, instance_id: str) -> PreparedPerception:
        """Prepare current presence, projection, and unacknowledged deltas."""

        identity = str(instance_id or "").strip()
        if not identity:
            raise ValueError("perception instance_id must not be empty")
        through = self._projection.catch_up(self._ledger)
        from_position, revision = self._projection.perception_cursor(identity)
        assertions = self._projection.list_assertions(include_retracted=True)
        changes = self._projection.changes_since(
            from_position,
            through_position=through,
        )
        sections: list[str] = []
        sections.extend(self._render_presence(identity))
        assertion_lines = self._render_assertions(assertions)
        if assertion_lines:
            sections.extend(["", *assertion_lines])
        change_lines = self._render_changes(changes)
        if change_lines:
            sections.extend(["", *change_lines])
        sections.extend(
            [
                "",
                "这是一段只属于本轮的可替换运行态感知，不应原样写入长期对话历史。",
            ]
        )
        return PreparedPerception(
            instance_id=identity,
            from_position=from_position,
            through_position=through,
            cursor_revision=revision,
            content="\n".join(sections),
            assertion_ids=tuple(item.assertion_id for item in assertions),
            change_positions=tuple(item.ingest_position for item in changes),
        )

    def commit(self, prepared: PreparedPerception) -> tuple[int, int]:
        """Advance exactly the cursor represented by a successful delivery."""

        return self._projection.commit_perception_cursor(
            prepared.instance_id,
            expected_position=prepared.from_position,
            through_position=prepared.through_position,
        )

    def query(self, instance_id: str, query: str) -> str:
        """Return the full attributable projection for reflective model judgment."""

        question = str(query or "").strip()
        if not question:
            raise ValueError("world query must not be empty")
        prepared = self.prepare(instance_id)
        return (
            f"当前意识窗口提出的内在查询：{question}\n\n"
            f"{prepared.content}\n\n"
            "请由当前意识实例结合来源、时间与矛盾记录自行判断；查询本身不会改写投影。"
        )
