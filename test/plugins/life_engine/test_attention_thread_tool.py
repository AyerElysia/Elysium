"""Subject-facing AttentionThread tool contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.attention_threads import (
    AttentionThreadCommit,
    build_attention_thread_projection,
)
from plugins.life_engine.attention_threads.tools import (
    LifeEngineManageAttentionThreadTool,
)
from plugins.life_engine.learning.scheduler import LearningScheduler
from plugins.life_engine.prompts.sections import (
    AttentionOpportunitySection,
    SectionContext,
)
from src.core.models.message import Message


class _Service:
    def __init__(self, *, active: bool = True) -> None:
        self.commands: list[Any] = []
        self.queries: list[Any] = []
        self.consciousness_registry = SimpleNamespace(
            get=lambda _identity: SimpleNamespace(is_active=active)
        )

    def resolve_consciousness_instance(self, stream_id: str) -> str:
        assert stream_id == "stream:attention-tool"
        return "consciousness:attention-tool"

    async def decide_attention_thread(self, command: Any) -> AttentionThreadCommit:
        self.commands.append(command)
        return AttentionThreadCommit(
            event_id="attention:event:tool",
            occurrence_id=command.occurrence_id,
            thread_id=command.thread_id,
            revision=command.expected_revision + 1,
            status="open",
            idempotent_replay=len(self.commands) > 1,
        )

    async def page_attention_threads(self, query: Any) -> Any:
        self.queries.append(query)
        return build_attention_thread_projection(
            (),
            source_frontier=3,
            projection_revision=3,
            max_bytes=query.max_bytes,
            projection_kind=query.projection_kind,
        )


def _tool(service: _Service, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: service,
    )
    tool = LifeEngineManageAttentionThreadTool(SimpleNamespace())
    tool._bind_runtime_context(
        stream_id="stream:attention-tool",
        message=Message(
            message_id="message:attention-tool:1",
            time=1785960000.0,
            stream_id="stream:attention-tool",
        ),
    )
    return tool


async def test_attention_tool_binds_actor_and_occurrence_to_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service()
    tool = _tool(service, monkeypatch)
    first_ok, first = await tool.execute(
        "open",
        statement="我愿意让未来的自己继续知道这条关注。",
    )
    second_ok, second = await tool.execute(
        "open",
        statement="我愿意让未来的自己继续知道这条关注。",
    )
    assert first_ok and second_ok
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first["thread_id"] == second["thread_id"]
    assert service.commands[0] == service.commands[1]
    assert service.commands[0].actor_consciousness_instance_id == (
        "consciousness:attention-tool"
    )
    assert service.commands[0].source_occurrence_ids == (
        "message:message:attention-tool:1",
    )
    assert not hasattr(service.commands[0], "reasoning")


async def test_attention_tool_lists_bounded_projection_and_ignores_pause_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service()
    tool = _tool(service, monkeypatch)
    ok, result = await tool.execute("list", include_closed=True)
    assert ok and isinstance(result, dict)
    assert result["delivered_bytes"] <= 32 * 1024
    assert service.queries[0].statuses == ("open", "paused", "closed")
    assert service.queries[0].focus_instance_id == "consciousness:attention-tool"

    ok, _ = await tool.execute(
        "pause",
        thread_id="attention:thread:1",
        expected_revision=2,
        statement="这段文字不得被 pause 偷偷持久化。",
    )
    assert ok
    assert service.commands[-1].public_statement == ""


async def test_attention_tool_rejects_inactive_runtime_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service(active=False)
    tool = _tool(service, monkeypatch)
    ok, result = await tool.execute(
        "open",
        statement="后台知道 ID 也不能冒充活跃意识。",
    )
    assert not ok
    assert result == "持续关注线索操作失败: PermissionError"
    assert service.commands == []


async def test_learning_consumes_only_explicit_close_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = LearningScheduler.__new__(LearningScheduler)
    captured: list[dict[str, Any]] = []
    heartbeat_calls = 0

    async def _submit(**payload: Any) -> list[Any]:
        captured.append(payload)
        return []

    async def _heartbeat() -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1

    monkeypatch.setattr(scheduler, "submit_reflection", _submit)
    monkeypatch.setattr(scheduler, "on_heartbeat", _heartbeat)
    await scheduler.on_attention_thread_closed(
        public_statement="我选择结束这条关注，这是我愿意公开保留的表述。",
        source_event_ids=["attention:event:1", "life:event:2"],
        actor_consciousness_instance_id="consciousness:attention-tool",
    )
    assert captured == [
        {
            "reflection_kind": "introspection",
            "reflection_text": "我选择结束这条关注，这是我愿意公开保留的表述。",
            "context": "主体明确关闭的持续关注线索公开表述",
            "source_event_ids": ["attention:event:1", "life:event:2"],
            "actor_consciousness_instance_id": "consciousness:attention-tool",
        }
    ]
    assert heartbeat_calls == 1
    with pytest.raises(ValueError, match="requires statement"):
        await scheduler.on_attention_thread_closed(
            public_statement="",
            source_event_ids=["attention:event:1"],
            actor_consciousness_instance_id="consciousness:attention-tool",
        )


async def test_heartbeat_attention_opportunity_prefers_canonical_projection() -> None:
    service = _Service()
    service._attention_thread_service = object()

    class _LegacyManager:
        def format_for_prompt(self, **_kwargs: Any) -> str:
            raise AssertionError("canonical authority must not read legacy streams")

    class _Curiosity:
        async def format_for_prompt(self, *, max_chars: int) -> str:
            assert max_chars == 1200
            return ""

    service._thought_manager = _LegacyManager()
    service._get_curiosity_engine = lambda: _Curiosity()
    query_seen: list[Any] = []

    async def _page(query: Any) -> Any:
        query_seen.append(query)
        return SimpleNamespace(
            items=(object(),),
            content=(
                '<attention_threads algorithm="attention-thread-ref-v1" '
                'projection_kind="heartbeat_attention_opportunity">\n'
                "- thread_ref=attention:thread:1\n"
                "</attention_threads>"
            ),
        )

    service.page_attention_threads = _page
    config = SimpleNamespace(streams=None, curiosity=None)
    ctx = SectionContext(service=service, config=config, today_str="2026-08-06")
    section = AttentionOpportunitySection()
    assert section.enabled(ctx)
    rendered = await section.render(ctx)
    assert rendered is not None
    assert "主体持续关注线索" in rendered
    assert "attention-thread-ref-v1" in rendered
    assert query_seen[0].max_bytes == 16 * 1024
    assert query_seen[0].statuses == ("open", "paused")
