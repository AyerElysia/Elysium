"""Canonical AttentionThread integration contracts.

Model-facing reads and writes are covered by ``test_proactive_tools``.  This
module deliberately tests only downstream domain consumers so the retired
domain-specific tool cannot become a second public surface again.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.learning.scheduler import LearningScheduler
from plugins.life_engine.prompts.sections import (
    AttentionOpportunitySection,
    SectionContext,
)


async def test_learning_consumes_only_explicit_close_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = LearningScheduler.__new__(LearningScheduler)
    captured: list[dict[str, Any]] = []
    heartbeat_calls = 0

    async def _enqueue(**payload: Any) -> str:
        captured.append(payload)
        return "reflection:attention-close"

    async def _heartbeat() -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1

    monkeypatch.setattr(scheduler, "enqueue_reflection", _enqueue)
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
    assert heartbeat_calls == 0
    with pytest.raises(ValueError, match="requires statement"):
        await scheduler.on_attention_thread_closed(
            public_statement="",
            source_event_ids=["attention:event:1"],
            actor_consciousness_instance_id="consciousness:attention-tool",
        )


async def test_heartbeat_attention_opportunity_uses_canonical_projection() -> None:
    queries: list[Any] = []

    async def _page(query: Any) -> Any:
        queries.append(query)
        return SimpleNamespace(
            items=(object(),),
            content=(
                '<attention_threads algorithm="attention-thread-ref-v1" '
                'projection_kind="heartbeat_attention_opportunity">\n'
                "- thread_ref=attention:thread:1\n"
                "</attention_threads>"
            ),
        )

    class _Curiosity:
        async def format_for_prompt(self, *, max_chars: int) -> str:
            assert max_chars == 1200
            return ""

    service = SimpleNamespace(
        _proactive_authority=object(),
        page_attention_threads=_page,
        _get_curiosity_engine=lambda: _Curiosity(),
    )
    ctx = SectionContext(
        service=service,
        config=SimpleNamespace(curiosity=None),
        today_str="2026-08-06",
    )

    rendered = await AttentionOpportunitySection().render(ctx)

    assert rendered is not None
    assert "主体持续关注线索" in rendered
    assert "attention-thread-ref-v1" in rendered
    assert queries[0].max_bytes == 16 * 1024
    assert queries[0].statuses == ("open", "paused")
