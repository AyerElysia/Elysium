"""Unified Trajectory Engine for Elysium.

Inspired by DeepSeek Harness (DSH):
1. \"Model-visible means logged\": Life is a continuous multi-turn Trajectory.
2. No synthetic, lossy \"projections\" or hand-glued pseudo-summaries.
3. Atomic TrajectoryTurn representing (USER observation -> ASSISTANT reflection/action -> TOOL result).
4. Compaction on context pressure, NOT sliding-window FIFO deletion.
   - Older tool outputs are pruned.
   - Older turns are folded into a structured compaction node.
   - Recent turns and pinned prefixes are strictly preserved.
5. Unified across primary consciousness (waking chatter) and subconsciousness (heartbeat).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from src.kernel.llm import (
    ROLE,
    Content,
    LLMPayload,
    ReasoningText,
    Text,
    ToolCall,
    ToolResult,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def estimate_payload_chars(payload: LLMPayload) -> int:
    """Estimate character footprint of an LLMPayload."""
    total = 0
    for part in payload.content:
        if isinstance(part, Text):
            total += len(part.text)
        elif isinstance(part, ReasoningText):
            total += len(part.text)
        elif isinstance(part, ToolCall):
            total += len(str(part.name)) + len(str(part.args))
        elif isinstance(part, ToolResult):
            total += len(part.to_text())
        else:
            total += len(str(part))
    return total


@dataclass(slots=True)
class TrajectoryTurn:
    """One logical turn in the multi-turn trajectory."""

    turn_id: str = field(default_factory=lambda: f"turn-{uuid4().hex[:12]}")
    created_at: str = field(default_factory=_now_iso)
    user_payload: LLMPayload | None = None
    assistant_payload: LLMPayload | None = None
    tool_payloads: list[LLMPayload] = field(default_factory=list)
    is_compacted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def total_chars(self) -> int:
        chars = 0
        if self.user_payload:
            chars += estimate_payload_chars(self.user_payload)
        if self.assistant_payload:
            chars += estimate_payload_chars(self.assistant_payload)
        for tp in self.tool_payloads:
            chars += estimate_payload_chars(tp)
        return chars


    def to_payloads(self) -> list[LLMPayload]:
        payloads: list[LLMPayload] = []
        if self.user_payload:
            payloads.append(self.user_payload)
        if self.assistant_payload:
            payloads.append(self.assistant_payload)
        payloads.extend(self.tool_payloads)
        return payloads

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "created_at": self.created_at,
            "is_compacted": self.is_compacted,
            "metadata": self.metadata,
            "user": _serialize_payload(self.user_payload) if self.user_payload else None,
            "assistant": _serialize_payload(self.assistant_payload) if self.assistant_payload else None,
            "tools": [_serialize_payload(tp) for tp in self.tool_payloads],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrajectoryTurn:
        turn = cls(
            turn_id=str(data.get("turn_id") or f"turn-{uuid4().hex[:12]}"),
            created_at=str(data.get("created_at") or _now_iso()),
            is_compacted=bool(data.get("is_compacted", False)),
            metadata=dict(data.get("metadata") or {}),
        )
        if data.get("user"):
            turn.user_payload = _deserialize_payload(data["user"])
        if data.get("assistant"):
            turn.assistant_payload = _deserialize_payload(data["assistant"])
        if data.get("tools"):
            turn.tool_payloads = [
                _deserialize_payload(item) for item in data["tools"] if item
            ]
        return turn


@dataclass(slots=True)
class CompactionResult:
    """Result of a compaction cycle."""

    triggered: bool
    before_chars: int
    after_chars: int
    compacted_turns_count: int
    pruned_tool_results_count: int


class LifeTrajectory:
    """Unified multi-turn life trajectory for primary and subconscious consciousness."""

    def __init__(
        self,
        domain: str = "subconscious",
        session_id: str | None = None,
    ) -> None:
        self.domain = domain
        self.session_id = session_id or f"session-{uuid4().hex[:12]}"
        self.turns: list[TrajectoryTurn] = []
        self._created_at = _now_iso()
        self._revision: int = 0

    @property
    def revision(self) -> int:
        return self._revision

    def total_chars(self) -> int:
        return sum(turn.total_chars() for turn in self.turns)

    @classmethod
    def from_payloads(
        cls,
        payloads: Sequence[LLMPayload],
        domain: str = "subconscious",
        session_id: str | None = None,
    ) -> LifeTrajectory:
        """Reconstruct LifeTrajectory from an existing flat LLMPayload list."""
        traj = cls(domain=domain, session_id=session_id)
        current_turn: TrajectoryTurn | None = None
        for payload in payloads:
            if not isinstance(payload, LLMPayload):
                continue
            role = getattr(payload, "role", None)
            if role in {ROLE.SYSTEM}:
                continue
            if role == ROLE.USER or current_turn is None:
                if role == ROLE.ASSISTANT and current_turn is None:
                    current_turn = TrajectoryTurn(assistant_payload=payload)
                    traj.turns.append(current_turn)
                    continue
                if role in {ROLE.TOOL, ROLE.TOOL_RESULT} and current_turn is None:
                    current_turn = TrajectoryTurn()
                    current_turn.tool_payloads.append(_as_tool_result_payload(payload))
                    traj.turns.append(current_turn)
                    continue
                current_turn = TrajectoryTurn(
                    user_payload=payload if role == ROLE.USER else None,
                )
                traj.turns.append(current_turn)
                if role == ROLE.ASSISTANT:
                    current_turn.assistant_payload = payload
                elif role in {ROLE.TOOL, ROLE.TOOL_RESULT}:
                    current_turn.tool_payloads.append(_as_tool_result_payload(payload))
            elif role == ROLE.ASSISTANT:
                if current_turn.assistant_payload is not None:
                    current_turn = TrajectoryTurn(assistant_payload=payload)
                    traj.turns.append(current_turn)
                else:
                    current_turn.assistant_payload = payload
            elif role in {ROLE.TOOL, ROLE.TOOL_RESULT}:
                current_turn.tool_payloads.append(_as_tool_result_payload(payload))
        return traj

    def to_payloads(self) -> list[LLMPayload]:
        """Export a detached payload list so request merges cannot mutate history."""
        payloads: list[LLMPayload] = []
        for turn in self.turns:
            for payload in turn.to_payloads():
                payloads.append(
                    LLMPayload(payload.role, list(payload.content or []))
                )
        return payloads

    def begin_turn(
        self,
        user_content: str | Content | list[Content],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> TrajectoryTurn:
        """Begin a new turn with an inbound user observation / wake event."""
        if isinstance(user_content, str):
            content_list: list[Content] = [Text(user_content)]
        elif isinstance(user_content, list):
            content_list = list(user_content)
        else:
            content_list = [user_content]

        turn = TrajectoryTurn(
            user_payload=LLMPayload(ROLE.USER, content_list),
            metadata=dict(metadata or {}),
        )
        self.turns.append(turn)
        self._revision += 1
        return turn

    def current_turn(self) -> TrajectoryTurn | None:
        return self.turns[-1] if self.turns else None

    def record_assistant_turn(
        self,
        assistant_content: str | Content | list[Content],
    ) -> None:
        """Record model thoughts/reply and optional tool calls."""
        if not self.turns:
            self.begin_turn("")

        turn = self.turns[-1]
        if turn.assistant_payload is not None:
            turn = TrajectoryTurn(metadata={"continuation": True})
            self.turns.append(turn)
        if isinstance(assistant_content, str):
            content_list: list[Content] = [Text(assistant_content)]
        elif isinstance(assistant_content, list):
            content_list = list(assistant_content)
        else:
            content_list = [assistant_content]

        turn.assistant_payload = LLMPayload(ROLE.ASSISTANT, content_list)
        self._revision += 1

    def record_tool_result(
        self,
        tool_result: ToolResult | list[ToolResult],
    ) -> None:
        """Record one or more tool results in the current active turn."""
        if not self.turns:
            self.begin_turn("")

        turn = self.turns[-1]
        results = tool_result if isinstance(tool_result, list) else [tool_result]
        turn.tool_payloads.append(LLMPayload(ROLE.TOOL_RESULT, results))
        self._revision += 1

    def compact(
        self,
        *,
        trigger_chars: int = 120_000,
        target_chars: int = 80_000,
        min_recent_turns: int = 3,
        tool_result_prune_threshold: int = 800,
    ) -> CompactionResult:
        """DeepSeek Harness style Compaction.

        Does NOT do FIFO sliding-window deletion.
        1. Prunes bloated tool results in older turns.
        2. Folds older turns into a structured compaction node while preserving
           the most recent `min_recent_turns` verbatim.
        """
        before_chars = self.total_chars()
        if before_chars < trigger_chars or len(self.turns) <= min_recent_turns:
            return CompactionResult(
                triggered=False,
                before_chars=before_chars,
                after_chars=before_chars,
                compacted_turns_count=0,
                pruned_tool_results_count=0,
            )

        pruned_count = 0
        # Phase 1: Tool Result Pruning in older turns
        older_turns = self.turns[:-min_recent_turns]
        for turn in older_turns:
            for tp in turn.tool_payloads:
                new_content: list[Content] = []
                for part in tp.content:
                    if isinstance(part, ToolResult):
                        text_val = part.to_text()
                        if len(text_val) > tool_result_prune_threshold:
                            digest = text_val[:200] + f"... [truncated {len(text_val)} chars]"
                            new_content.append(
                                ToolResult(
                                    value=digest,
                                    call_id=part.call_id,
                                    name=part.name,
                                )
                            )
                            pruned_count += 1
                            continue
                    new_content.append(part)
                tp.content = new_content

        current_chars = self.total_chars()
        if current_chars <= target_chars:
            self._revision += 1
            return CompactionResult(
                triggered=True,
                before_chars=before_chars,
                after_chars=current_chars,
                compacted_turns_count=0,
                pruned_tool_results_count=pruned_count,
            )

        # Phase 2: Folding older turns into a structured Compaction Node
        preserved_recent_turns = list(self.turns[-min_recent_turns:])
        turns_to_fold = list(self.turns[:-min_recent_turns])

        summary_lines: list[str] = [
            "<compacted_trajectory_history>",
            f"以下为早期 {len(turns_to_fold)} 轮连续生命轨迹的压缩归档（严格保留因果要点，无滑动丢失）：",
        ]

        for idx, t in enumerate(turns_to_fold, start=1):
            user_text = ""
            if t.user_payload:
                user_text = " ".join(
                    p.text for p in t.user_payload.content if isinstance(p, Text)
                ).strip()
            asst_text = ""
            tool_calls_text = ""
            if t.assistant_payload:
                asst_text = " ".join(
                    p.text for p in t.assistant_payload.content if isinstance(p, (Text, ReasoningText))
                ).strip()
                calls = [
                    f"{p.name}({str(p.args)[:80]})"
                    for p in t.assistant_payload.content
                    if isinstance(p, ToolCall)
                ]
                if calls:
                    tool_calls_text = f" -> 动作: {', '.join(calls)}"

            brief_user = user_text[:120] if user_text else "(无新输入)"
            brief_asst = asst_text[:160] if asst_text else "(静思)"
            summary_lines.append(
                f"- [Turn {idx} {t.created_at[:19]}] 事件: {brief_user} | 认知: {brief_asst}{tool_calls_text}"
            )

        summary_lines.append("</compacted_trajectory_history>")
        folded_user_text = "\n".join(summary_lines)

        compacted_turn = TrajectoryTurn(
            turn_id=f"compacted-{uuid4().hex[:8]}",
            created_at=turns_to_fold[-1].created_at if turns_to_fold else _now_iso(),
            user_payload=LLMPayload(ROLE.USER, [Text(folded_user_text)]),
            assistant_payload=LLMPayload(ROLE.ASSISTANT, [Text("已将早期轨迹连贯沉淀，注意力平滑对齐当前视窗。")]),
            is_compacted=True,
            metadata={"compacted_turns": len(turns_to_fold)},
        )

        self.turns = [compacted_turn, *preserved_recent_turns]
        self._revision += 1
        after_chars = self.total_chars()

        return CompactionResult(
            triggered=True,
            before_chars=before_chars,
            after_chars=after_chars,
            compacted_turns_count=len(turns_to_fold),
            pruned_tool_results_count=pruned_count,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "domain": self.domain,
            "session_id": self.session_id,
            "created_at": self._created_at,
            "revision": self._revision,
            "turns": [turn.to_dict() for turn in self.turns],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LifeTrajectory:
        traj = cls(
            domain=str(data.get("domain") or "subconscious"),
            session_id=str(data.get("session_id") or ""),
        )
        traj._created_at = str(data.get("created_at") or _now_iso())
        traj._revision = int(data.get("revision", 0))
        traj.turns = [TrajectoryTurn.from_dict(t) for t in data.get("turns", [])]
        return traj

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> LifeTrajectory:
        path = Path(path)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)


def _as_tool_result_payload(payload: LLMPayload) -> LLMPayload:
    if payload.role == ROLE.TOOL_RESULT:
        return payload
    results = [part for part in payload.content if isinstance(part, ToolResult)]
    if not results:
        return payload
    return LLMPayload(ROLE.TOOL_RESULT, results)


def compact_request_payloads(
    payloads: Sequence[LLMPayload],
    *,
    trigger_chars: int,
    target_chars: int,
    min_recent_turns: int = 4,
) -> list[LLMPayload]:
    """Compact conversational payloads and leave system and tool schemas in place."""

    prefix: list[LLMPayload] = []
    body: list[LLMPayload] = []
    for payload in payloads:
        if not isinstance(payload, LLMPayload):
            continue
        if payload.role == ROLE.SYSTEM:
            prefix.append(payload)
            continue
        if payload.role == ROLE.TOOL and not any(
            isinstance(part, ToolResult) for part in payload.content
        ):
            prefix.append(payload)
            continue
        body.append(payload)
    trajectory = LifeTrajectory.from_payloads(body, domain="request")
    if trajectory.total_chars() < trigger_chars:
        return [payload for payload in payloads if isinstance(payload, LLMPayload)]
    trajectory.compact(
        trigger_chars=trigger_chars,
        target_chars=target_chars,
        min_recent_turns=min_recent_turns,
    )
    return [*prefix, *trajectory.to_payloads()]


def _serialize_payload(payload: LLMPayload | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    items: list[dict[str, Any]] = []
    for part in payload.content:
        if isinstance(part, Text):
            items.append({"type": "text", "text": part.text})
        elif isinstance(part, ReasoningText):
            items.append({"type": "reasoning", "text": part.text})
        elif isinstance(part, ToolCall):
            items.append({"type": "tool_call", "id": part.id, "name": part.name, "args": part.args})
        elif isinstance(part, ToolResult):
            items.append({"type": "tool_result", "call_id": part.call_id, "name": part.name, "value": part.value})
        else:
            items.append({"type": "other", "repr": str(part)})
    return {"role": payload.role.value, "content": items}


def _deserialize_payload(data: dict[str, Any]) -> LLMPayload:
    role = ROLE(data["role"])
    content_list: list[Content] = []
    for item in data.get("content", []):
        t = item.get("type")
        if t == "text":
            content_list.append(Text(str(item.get("text", ""))))
        elif t == "reasoning":
            content_list.append(ReasoningText(str(item.get("text", ""))))
        elif t == "tool_call":
            content_list.append(ToolCall(id=item.get("id"), name=str(item.get("name", "")), args=item.get("args", {})))
        elif t == "tool_result":
            content_list.append(ToolResult(value=item.get("value"), call_id=item.get("call_id"), name=item.get("name")))
        else:
            content_list.append(Text(str(item.get("repr", ""))))
    return LLMPayload(role, content_list)
