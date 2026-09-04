"""Canonical AttentionThread integration contracts.

Model-facing reads and writes are covered by ``test_proactive_tools``.  This
module deliberately tests only downstream domain consumers so the retired
domain-specific tool cannot become a second public surface again.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.learning.scheduler import LearningScheduler
from plugins.life_engine.opportunity import OpportunityBus
from plugins.life_engine.prompts.sections import (
    OpportunitySection,
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


async def test_heartbeat_attention_opportunity_uses_canonical_projection(
    tmp_path: Path,
) -> None:
    queries: list[Any] = []

    async def _page(query: Any) -> Any:
        queries.append(query)
        return SimpleNamespace(
            items=(
                SimpleNamespace(
                    thread_id="thread-1",
                    status="open",
                    revision=1,
                    statement_excerpt="thread one",
                    excerpt_complete=True,
                    statement_bytes=10,
                ),
            )
        )

    async def _list_initiatives(*, include_released: bool = False) -> tuple[Any, ...]:
        return ()

    class _Curiosity:
        async def load_opportunity(self) -> None:
            return None

    service = SimpleNamespace(
        _proactive_authority=object(),
        page_attention_threads=_page,
        list_initiatives=_list_initiatives,
        _get_curiosity_engine=lambda: _Curiosity(),
        _learning_scheduler=None,
        _last_memory_maintenance_prompt_at=None,
        _cfg=lambda: SimpleNamespace(
            curiosity=SimpleNamespace(enabled=True, inject_to_heartbeat=True),
            narrative=None,
            learning=None,
        ),
        _workspace_dir=lambda: tmp_path,
    )
    service._opportunity_bus = OpportunityBus(service)
    ctx = SectionContext(
        service=service,
        config=service._cfg(),
        today_str="2026-08-06",
    )

    rendered = await OpportunitySection().render(ctx)

    assert rendered is not None
    assert "你留下的线索" in rendered
    assert "attention:thread-1" in rendered
    assert queries[0].projection_kind == "heartbeat_opportunity_continuity"
    assert queries[0].max_bytes == 4 * 1024
    assert queries[0].statuses == ("open", "paused")
